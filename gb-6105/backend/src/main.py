import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from migrations.v002_directory import upgrade as upgrade_v002
from scripts.seed_demo import seed, setup_schema
from src.config.settings import SEED_DEMO
from src.domain.scenario import CONFIG
from src.middlewares.errors import catch_errors
from src.middlewares.request_id import request_id
from src.routes import cases, directory, runs, session, system
from src.services import directory as directory_service
from src.config.resources import engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app):
    setup_schema()
    with engine.begin() as db:
        upgrade_v002(db)
        directory_service.bootstrap(db)
    if SEED_DEMO:
        try:
            seed()
        except Exception:
            logger.exception("Demo seed failed")
    yield


app = FastAPI(title=CONFIG["title"], lifespan=lifespan)
app.middleware("http")(catch_errors)
app.middleware("http")(request_id)
for group in (system, session, cases, runs, directory):
    app.include_router(group.router)
