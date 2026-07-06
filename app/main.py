from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .admin_api import router as admin_api_router
from .db import init_db, log_event
from .onebot import router as onebot_router
from .paths import STATIC_DIR
from .settings import ensure_local_config
from .web import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_local_config()
    log_event("info", "system", "QQbot_v2 started")
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="QQbot_v2", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(admin_api_router)
    app.include_router(onebot_router)
    app.include_router(web_router)
    return app


app = create_app()
