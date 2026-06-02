from contextlib import asynccontextmanager
from fastapi import FastAPI
import structlog

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import init_db
# Import the events router
from app.api import events as events_module

settings = get_settings()

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

# Register the events router
app.include_router(
    events_module.router,
    prefix="/api/v1/events",
    tags=["events"]
)

@app.get("/")
async def root():
    return {"message": f"{settings.app_name} API"}

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "environment": settings.environment,
        "app_name": settings.app_name
    }
