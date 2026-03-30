import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import (
    router,
    set_database,
    restore_runtime_state,
    connector,
    finnhub_adapter,
    current_research_cycle_service,
)
from storage.db import Database
from config import config
from services.meta_training_scheduler import MetaTrainingScheduler

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global meta_training_scheduler
    await db.initialize()
    set_database(db)
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
    logger.info("MT5 Demo Trading Agent backend started")
    yield
    # Shutdown: disconnect MT5 and close DB
    if meta_training_scheduler is not None:
        await meta_training_scheduler.stop()
        meta_training_scheduler = None
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

app.include_router(router, prefix="/api")


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config.API_HOST,
        port=config.API_PORT,
        reload=False,
    )
