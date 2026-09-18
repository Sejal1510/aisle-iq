import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.api import auth as auth_module
from app.api import events as events_module
from app.api import onboarding as onboarding_module
from app.api import replay as replay_module
from app.api import stores as stores_module
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import SessionLocal, init_db
from app.models.event import Event

settings = get_settings()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(environment=settings.environment, debug=settings.debug)
    logger = structlog.get_logger()
    logger.info(
        "application_startup",
        app_name=settings.app_name,
        environment=settings.environment,
        debug=settings.debug
    )

    # Initialize the database tables on startup
    init_db()

    yield
    logger.info("application_shutdown")

app = FastAPI(
    title=settings.app_name,
    description="API for converting raw CCTV footage into business intelligence",
    version="1.0.0",
    lifespan=lifespan
)

app.include_router(
    events_module.router,
    prefix="/api/v1/events",
    tags=["events"]
)
app.include_router(
    events_module.router,
    prefix="/events",
    tags=["events"]
)
app.include_router(
    stores_module.router,
    prefix="/api/v1",
    tags=["stores"]
)
app.include_router(
    stores_module.router,
    tags=["stores"]
)
app.include_router(
    auth_module.router,
    prefix="/api/v1",
    tags=["auth"]
)
app.include_router(
    auth_module.router,
    tags=["auth"]
)
app.include_router(
    onboarding_module.router,
    prefix="/api/v1",
    tags=["onboarding"]
)
app.include_router(
    onboarding_module.router,
    tags=["onboarding"]
)
app.include_router(
    replay_module.router,
    prefix="/api/v1",
    tags=["replay"]
)
app.include_router(
    replay_module.router,
    tags=["replay"]
)
app.mount("/dashboard", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")
ONBOARDING_DIR = PROJECT_ROOT / "onboarding"
if ONBOARDING_DIR.is_dir():
    app.mount("/onboarding", StaticFiles(directory=ONBOARDING_DIR, html=True), name="onboarding")


@app.middleware("http")
async def structured_request_logging(request: Request, call_next):
    clear_contextvars()
    trace_id = request.headers.get("x-trace-id", str(uuid4()))
    endpoint = request.url.path
    store_id = _store_id_from_path(endpoint)
    event_count = None
    if endpoint.endswith("/events/ingest"):
        try:
            body = await request.body()
            parsed = json.loads(body) if body else []
            event_count = len(parsed) if isinstance(parsed, list) else 1
        except json.JSONDecodeError:
            event_count = None

    bind_contextvars(trace_id=trace_id)
    start = time.perf_counter()
    logger = structlog.get_logger("request")
    try:
        response = await call_next(request)
    except Exception:
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.exception(
            "request_failed",
            trace_id=trace_id,
            endpoint=endpoint,
            store_id=store_id,
            event_count=event_count,
            latency_ms=latency_ms,
            status_code=500,
        )
        raise

    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["x-trace-id"] = trace_id
    logger.info(
        "request_completed",
        trace_id=trace_id,
        endpoint=endpoint,
        store_id=store_id,
        event_count=event_count,
        latency_ms=latency_ms,
        status_code=response.status_code,
    )
    clear_contextvars()
    return response


@app.exception_handler(SQLAlchemyError)
async def database_exception_handler(request: Request, exc: SQLAlchemyError):
    trace_id = request.headers.get("x-trace-id", str(uuid4()))
    structlog.get_logger(__name__).warning(
        "database_unavailable",
        trace_id=trace_id,
        endpoint=request.url.path,
        error=str(exc),
    )
    return JSONResponse(
        status_code=503,
        content={
            "error": "database_unavailable",
            "message": "The database is temporarily unavailable.",
            "trace_id": trace_id,
        },
    )


@app.get("/")
async def root():
    return {"message": f"{settings.app_name} API"}

@app.get("/health")
async def health_check():
    db_status = "ok"
    stores: dict[str, dict[str, str | None]] = {}
    warnings: list[dict[str, str]] = []
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            last_events = db.execute(
                select(Event.store_id, func.max(Event.timestamp)).group_by(Event.store_id)
            ).all()
            now = time.time()
            for store_id, last_timestamp in last_events:
                timestamp_text = last_timestamp.isoformat() if last_timestamp else None
                status = "ok"
                if last_timestamp and now - last_timestamp.timestamp() > 600:
                    status = "STALE_FEED"
                    warnings.append({"store_id": store_id, "warning": "STALE_FEED"})
                stores[store_id] = {"last_event_timestamp": timestamp_text, "status": status}
    except SQLAlchemyError:
        db_status = "unavailable"
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "database": db_status,
                "environment": settings.environment,
                "app_name": settings.app_name,
                "stores": stores,
                "warnings": [{"warning": "DATABASE_UNAVAILABLE"}],
            },
        )

    return {
        "status": "ok",
        "database": db_status,
        "environment": settings.environment,
        "app_name": settings.app_name,
        "stores": stores,
        "warnings": warnings,
    }


def _store_id_from_path(path: str) -> str | None:
    match = re.search(r"/stores/([^/]+)", path)
    if match:
        return match.group(1)
    return None
