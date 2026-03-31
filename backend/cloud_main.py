import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.admin_routes import router as admin_router, set_database as set_admin_database
from storage.db import Database
from config import config

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

db = Database(config.DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.initialize()
    set_admin_database(db)
    logger.info("LinkTrade cloud admin backend started")
    yield
    await db.close()


app = FastAPI(
    title="LinkTrade Cloud Backend",
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

app.include_router(admin_router, prefix="/api")


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "linktrade-cloud-backend"}
