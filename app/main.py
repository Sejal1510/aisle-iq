from contextlib import asynccontextmanager
from fastapi import FastAPI
import structlog

from app.core.config import get_settings
from app.core.logging import setup_logging

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
    yield
    logger.info("application_shutdown")

app = FastAPI(
    title=settings.app_name,
    description="API for converting raw CCTV footage into business intelligence",
    version="1.0.0",
    lifespan=lifespan
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
