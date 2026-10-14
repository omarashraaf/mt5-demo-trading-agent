import logging
import os
import sys
import asyncio
from contextlib import asynccontextmanager

_backend_tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp")
try:
    os.makedirs(_backend_tmp_dir, exist_ok=True)
    os.environ["TMP"] = _backend_tmp_dir
    os.environ["TEMP"] = _backend_tmp_dir
    os.environ["SQLITE_TMPDIR"] = _backend_tmp_dir
except Exception:
    pass

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import api.routes as runtime_routes
from api.routes import (
    router,
    set_database,
    restore_runtime_state,
    hydrate_gemini_credentials_from_cloud,
    _current_gemini_status,
    connector,
    benzinga_adapter,
    current_research_cycle_service,
    apply_cloud_brain_command,
    cloud_brain_snapshot,
    kill_switch_service,
)
from api.admin_routes import (
    router as admin_router,
    set_database as set_admin_database,
    set_api_key_health_service,
)
from api.admin_routes import supabase_admin
from api.auth_guard import require_api_access
from storage.db import Database
from config import config
from services.meta_training_scheduler import MetaTrainingScheduler
from services.cloud_sync_service import CloudSyncService
from services.cloud_brain_service import CloudBrainService
from services.cloud_archive_service import CloudArchiveService
from services.api_key_health_service import (
    API_KEY_HEALTH_SETTINGS_STATE_KEY,
    ApiKeyHealthConfig,
    ApiKeyHealthService,
)

_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
_is_serverless_runtime = bool((os.getenv("VERCEL", "") or "").strip())
if not _is_serverless_runtime:
    try:
        _log_handlers.append(logging.FileHandler("trading_agent.log"))
    except Exception:
        # Keep backend bootable even if filesystem is not writable.
        pass

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger(__name__)
logger.info("LinkTrade startup config: %s", config.safe_startup_summary())

db = Database(config.DB_PATH)
meta_training_scheduler: MetaTrainingScheduler | None = None
cloud_brain_service: CloudBrainService | None = None
cloud_archive_service: CloudArchiveService | None = None
api_key_health_service: ApiKeyHealthService | None = None
meta_activation_task: asyncio.Task | None = None
reconciliation_task: asyncio.Task | None = None
cloud_sync_service = CloudSyncService(
    enabled=bool(config.CLOUD_SYNC_ENABLED and config.service_mode_pro_enabled),
    supabase_url=config.SUPABASE_URL,
    service_role_key=config.SUPABASE_SERVICE_ROLE_KEY,
    table=config.CLOUD_LOG_TABLE,
    timeout_seconds=config.CLOUD_SYNC_TIMEOUT_SECONDS,
)


async def _ensure_meta_model_active_startup(max_attempts: int = 6) -> None:
    """Best-effort startup activation for meta model.

    This keeps meta mode ON by default after backend boot, even if early DB
    initialization is still warming up.
    """
    service = current_research_cycle_service()
    if service is None or service.meta_model_service is None:
        logger.warning("Meta model bootstrap skipped: service unavailable.")
        return
    meta_service = service.meta_model_service
    for attempt in range(1, max_attempts + 1):
        try:
            await asyncio.wait_for(meta_service.refresh_active_model(force=True), timeout=6.0)
            if meta_service.is_active and str(meta_service.active_version_id or "").strip():
                logger.info(
                    "Meta model active by default: version=%s (attempt=%s).",
                    meta_service.active_version_id,
                    attempt,
                )
                return
        except Exception as exc:
            logger.warning("Meta model startup activation attempt %s failed: %s", attempt, exc)
        await asyncio.sleep(min(10.0, 1.5 * attempt))
    logger.warning("Meta model did not activate during startup retries; running in fallback mode.")


async def _scheduled_reconciliation_loop(interval_seconds: int = 300) -> None:
    while True:
        await asyncio.sleep(max(60, int(interval_seconds or 300)))
        try:
            active_connector = getattr(runtime_routes, "connector", None)
            if not bool(getattr(active_connector, "connected", False)):
                continue
            await runtime_routes.reconciliation_service.run(limit=300)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Scheduled reconciliation skipped after error: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global meta_training_scheduler
    global cloud_brain_service
    global cloud_archive_service
    global api_key_health_service
    global meta_activation_task
    global reconciliation_task
    await db.initialize()
    db.set_cloud_log_sink(cloud_sync_service.emit)
    set_database(db)
    set_admin_database(db)
    await kill_switch_service.load()
    try:
        await asyncio.wait_for(restore_runtime_state(), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("Startup restore_runtime_state timed out; continuing with defaults.")
    except Exception as exc:
        logger.warning("Startup restore_runtime_state failed; continuing: %s", exc)

    if config.service_mode_pro_enabled and config.CLOUD_SECRETS_ENABLED:
        try:
            cloud_gemini_state = await asyncio.wait_for(hydrate_gemini_credentials_from_cloud(), timeout=12.0)
        except asyncio.TimeoutError:
            cloud_gemini_state = {
                "source": "cloud",
                "configured": True,
                "loaded": False,
                "reason": "startup timeout while hydrating cloud Gemini credentials",
            }
            logger.warning("Startup cloud Gemini hydration timed out; using local runtime credentials.")
        except Exception as exc:
            cloud_gemini_state = {
                "source": "cloud",
                "configured": True,
                "loaded": False,
                "reason": str(exc),
            }
            logger.warning("Startup cloud Gemini hydration failed; using local runtime credentials: %s", exc)
    else:
        cloud_gemini_state = {
            "source": "local",
            "configured": False,
            "loaded": False,
            "reason": f"cloud secret hydration disabled in {config.APP_SERVICE_MODE} service mode",
        }
    if cloud_gemini_state.get("loaded"):
        logger.info(
            "Gemini credentials loaded from cloud secrets (model=%s).",
            cloud_gemini_state.get("model"),
        )
    else:
        logger.info(
            "Gemini cloud secret hydration skipped/fallback: %s",
            cloud_gemini_state.get("reason", "using local environment"),
        )
    active_research_service = current_research_cycle_service()
    if config.service_mode_pro_enabled and active_research_service is not None:
        meta_training_scheduler = MetaTrainingScheduler(
            db=db,
            research_cycle_service=active_research_service,
            enabled=config.AUTO_META_TRAINING_ENABLED,
            interval_seconds=config.AUTO_META_TRAIN_INTERVAL_SECONDS,
            min_closed_trades=config.AUTO_META_TRAIN_MIN_CLOSED_TRADES,
            auto_approve=config.AUTO_META_AUTO_APPROVE,
            min_precision=config.AUTO_META_MIN_PRECISION,
            min_f1=config.AUTO_META_MIN_F1,
            min_positive_samples=config.AUTO_META_MIN_POSITIVE_SAMPLES,
            target_column=config.AUTO_META_TARGET_COLUMN,
            should_skip=lambda: bool(getattr(auto_trader, "is_running", False)),
        )
        meta_training_scheduler.start()
        meta_activation_task = asyncio.create_task(_ensure_meta_model_active_startup())
    elif not config.service_mode_pro_enabled:
        logger.info("Meta training scheduler skipped in %s service mode.", config.APP_SERVICE_MODE)
    else:
        logger.warning("Meta training scheduler not started: research_cycle_service unavailable.")
    benzinga_health = benzinga_adapter.healthcheck()
    if benzinga_health.get("degraded"):
        logger.warning("Benzinga integration started in degraded mode: %s", benzinga_health.get("reason"))
    cloud_brain_service = CloudBrainService(
        enabled=bool(config.CLOUD_BRAIN_ENABLED and config.service_mode_pro_enabled),
        supabase_url=config.SUPABASE_URL,
        service_role_key=config.SUPABASE_SERVICE_ROLE_KEY,
        table=config.CLOUD_BRAIN_TABLE,
        timeout_seconds=config.CLOUD_SYNC_TIMEOUT_SECONDS,
        poll_seconds=config.CLOUD_BRAIN_POLL_SECONDS,
        apply_command=apply_cloud_brain_command,
    )
    cloud_brain_service.start()
    cloud_archive_service = CloudArchiveService(
        db=db,
        enabled=bool(config.CLOUD_ARCHIVE_ENABLED and config.service_mode_pro_enabled),
        interval_seconds=config.CLOUD_ARCHIVE_INTERVAL_SECONDS,
        retention_days={
            "connection_events": config.LOCAL_RETENTION_CONNECTION_LOGS_DAYS,
            "ai_activity": config.LOCAL_RETENTION_AI_ACTIVITY_DAYS,
            "evaluation_journal": config.LOCAL_RETENTION_EVALUATION_JOURNAL_DAYS,
            "trade_candidates": config.LOCAL_RETENTION_TRADE_CANDIDATES_DAYS,
            "feature_snapshots": config.LOCAL_RETENTION_FEATURE_SNAPSHOTS_DAYS,
            "external_events": config.LOCAL_RETENTION_EXTERNAL_EVENTS_DAYS,
            "event_fetch_runs": config.LOCAL_RETENTION_EVENT_FETCH_RUNS_DAYS,
        },
        batch_sizes={
            "feature_snapshots": 20,
            "trade_candidates": 20,
            "evaluation_journal": 20,
            "ai_activity": 50,
            "connection_events": 50,
            "errors": 50,
            "event_fetch_runs": 50,
            "external_events": 20,
        },
        max_batches_per_run=12,
    )
    cloud_archive_service.start()
    api_key_health_service = ApiKeyHealthService(
        gemini_status_getter=_current_gemini_status,
        benzinga_adapter=benzinga_adapter,
        config=ApiKeyHealthConfig(
            enabled=bool(config.API_HEALTH_ALERTS_ENABLED),
            check_interval_seconds=int(config.API_HEALTH_ALERT_INTERVAL_SECONDS),
            smtp_host=config.API_HEALTH_SMTP_HOST,
            smtp_port=int(config.API_HEALTH_SMTP_PORT),
            smtp_user=config.API_HEALTH_SMTP_USER,
            smtp_password=config.API_HEALTH_SMTP_PASSWORD,
            sender_email=config.API_HEALTH_ALERT_SENDER_EMAIL,
            receiver_email=config.API_HEALTH_ALERT_RECEIVER_EMAIL,
            alert_cooldown_seconds=int(config.API_HEALTH_ALERT_COOLDOWN_SECONDS),
        ),
    )
    try:
        api_health_state = await db.get_runtime_state(API_KEY_HEALTH_SETTINGS_STATE_KEY)
        if api_health_state and "email_alerts_enabled" in api_health_state:
            api_key_health_service.set_alerts_enabled(bool(api_health_state.get("email_alerts_enabled")))
    except Exception as exc:
        logger.warning("Failed to restore API key health settings: %s", exc)
    set_api_key_health_service(api_key_health_service)
    if config.service_mode_pro_enabled or config.API_HEALTH_ALERTS_ENABLED:
        await api_key_health_service.start()
    else:
        logger.info("API key health background worker skipped in %s service mode.", config.APP_SERVICE_MODE)
    if config.service_mode_auto_enabled:
        reconciliation_task = asyncio.create_task(_scheduled_reconciliation_loop())
    else:
        logger.info("Scheduled reconciliation skipped in %s service mode.", config.APP_SERVICE_MODE)
    logger.info("Cloud brain state: %s", cloud_brain_snapshot())
    logger.info("MT5 Demo Trading Agent backend started")
    yield
    # Shutdown: disconnect MT5 and close DB
    if reconciliation_task is not None and not reconciliation_task.done():
        reconciliation_task.cancel()
        try:
            await reconciliation_task
        except asyncio.CancelledError:
            pass
        reconciliation_task = None
    if meta_training_scheduler is not None:
        await meta_training_scheduler.stop()
        meta_training_scheduler = None
    if cloud_brain_service is not None:
        await cloud_brain_service.stop()
        cloud_brain_service = None
    if cloud_archive_service is not None:
        await cloud_archive_service.stop()
        cloud_archive_service = None
    if api_key_health_service is not None:
        await api_key_health_service.stop()
        api_key_health_service = None
    if meta_activation_task is not None and not meta_activation_task.done():
        meta_activation_task.cancel()
        meta_activation_task = None
    if connector.connected:
        connector.disconnect()
        logger.info("MT5 disconnected on shutdown")
    await db.close()
    logger.info("Database closed. Goodbye.")


app = FastAPI(
    title="MT5 Demo Trading Agent",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def optional_auth_guard(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if not path.startswith("/api"):
        return await call_next(request)
    if not config.AUTH_REQUIRED:
        return await call_next(request)
    try:
        await require_api_access(
            path=path,
            authorization=request.headers.get("authorization"),
            auth_required=bool(config.AUTH_REQUIRED),
            database=db,
            supabase=supabase_admin,
        )
    except Exception as exc:
        status_code = int(getattr(exc, "status_code", 401) or 401)
        detail = getattr(exc, "detail", None) or str(exc) or "Authentication required."
        return JSONResponse(status_code=status_code, content={"detail": detail})
    return await call_next(request)


app.include_router(router, prefix="/api")
app.include_router(admin_router, prefix="/api")


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=config.API_HOST,
        port=config.API_PORT,
        reload=False,
    )
