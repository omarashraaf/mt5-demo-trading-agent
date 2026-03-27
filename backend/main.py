import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router, set_database, restore_runtime_state, connector, finnhub_adapter
from storage.db import Database
from config import config

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.initialize()
    set_database(db)
    await restore_runtime_state()
    finnhub_health = finnhub_adapter.healthcheck()
    if finnhub_health.get("degraded"):
        logger.warning("Finnhub integration started in degraded mode: %s", finnhub_health.get("reason"))
    logger.info("MT5 Demo Trading Agent backend started")
    yield
    # Shutdown: disconnect MT5 and close DB
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
