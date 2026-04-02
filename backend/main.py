import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import (
    router,
    set_database,
    restore_runtime_state,
    connector,
    finnhub_adapter,
    current_research_cycle_service,
    apply_cloud_brain_command,
    cloud_brain_snapshot,
)
from api.admin_routes import router as admin_router, set_database as set_admin_database
from api.admin_routes import supabase_admin
from storage.db import Database
from config import config
from services.meta_training_scheduler import MetaTrainingScheduler
from services.cloud_sync_service import CloudSyncService
from services.cloud_brain_service import CloudBrainService

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trading_agent.log"),
    ],
)
logger = logging.getLogger(__name__)

db = Database(config.DB_PATH)
meta_training_scheduler: MetaTrainingScheduler | None = None
cloud_brain_service: CloudBrainService | None = None
cloud_sync_service = CloudSyncService(
    enabled=config.CLOUD_SYNC_ENABLED,
    supabase_url=config.SUPABASE_URL,
    service_role_key=config.SUPABASE_SERVICE_ROLE_KEY,
    table=config.CLOUD_LOG_TABLE,
    timeout_seconds=config.CLOUD_SYNC_TIMEOUT_SECONDS,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global meta_training_scheduler
    global cloud_brain_service
    await db.initialize()
    db.set_cloud_log_sink(cloud_sync_service.emit)
    set_database(db)
    set_admin_database(db)
    await restore_runtime_state()
    active_research_service = current_research_cycle_service()
    if active_research_service is not None:
        meta_training_scheduler = MetaTrainingScheduler(
            db=db,
            research_cycle_service=active_research_service,
            enabled=config.AUTO_META_TRAINING_ENABLED,
            interval_seconds=config.AUTO_META_TRAIN_INTERVAL_SECONDS,
            min_closed_trades=config.AUTO_META_TRAIN_MIN_CLOSED_TRADES,
            auto_approve=config.AUTO_META_AUTO_APPROVE,
            min_precision=config.AUTO_META_MIN_PRECISION,
            min_f1=config.AUTO_META_MIN_F1,
        )
        meta_training_scheduler.start()
    else:
        logger.warning("Meta training scheduler not started: research_cycle_service unavailable.")
    finnhub_health = finnhub_adapter.healthcheck()
    if finnhub_health.get("degraded"):
        logger.warning("Finnhub integration started in degraded mode: %s", finnhub_health.get("reason"))
    cloud_brain_service = CloudBrainService(
        enabled=config.CLOUD_BRAIN_ENABLED,
        supabase_url=config.SUPABASE_URL,
        service_role_key=config.SUPABASE_SERVICE_ROLE_KEY,
        table=config.CLOUD_BRAIN_TABLE,
        timeout_seconds=config.CLOUD_SYNC_TIMEOUT_SECONDS,
        poll_seconds=config.CLOUD_BRAIN_POLL_SECONDS,
        apply_command=apply_cloud_brain_command,
    )
    cloud_brain_service.start()
    logger.info("Cloud brain state: %s", cloud_brain_snapshot())
    logger.info("MT5 Demo Trading Agent backend started")
    yield
    # Shutdown: disconnect MT5 and close DB
    if meta_training_scheduler is not None:
        await meta_training_scheduler.stop()
        meta_training_scheduler = None
    if cloud_brain_service is not None:
        await cloud_brain_service.stop()
        cloud_brain_service = None
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
    public_paths = {
        "/api/auth/bootstrap-admin",
        "/api/public/register",
    }
    if path in public_paths:
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return JSONResponse(status_code=401, content={"detail": "Authentication required."})
    token = auth[7:].strip()
    if not token:
        return JSONResponse(status_code=401, content={"detail": "Authentication required."})
    try:
        supabase_admin.get_user_from_token(token)
    except Exception as exc:
        return JSONResponse(status_code=401, content={"detail": str(exc)})
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
