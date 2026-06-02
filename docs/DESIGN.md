# System Design: Store Intelligence System

This document describes *how* the system works. It will evolve throughout development.

## 1. System Architecture

Currently in initialization phase. The planned architecture involves:
- **API/Backend**: FastAPI application serving business intelligence.
- **Database**: SQLite for local testing and development.
- **Computer Vision Pipeline**: (To be designed) YOLOv8 + ByteTrack + OSNet ReID for processing CCTV feeds.
- **Dashboard**: (To be designed) Streamlit application for data visualization.

## 2. Component Responsibilities

* `app/api`: FastAPI routes and endpoints.
* `app/core`: Configuration, logging, and foundational setup.
* `app/db`: Database session management and connections.
* `app/models`: SQLAlchemy ORM models.
* `app/repositories`: Data access layer (Repository Pattern).
* `app/schemas`: Pydantic models for request/response validation.
* `app/services`: Core business logic.
* `pipeline`: Computer vision and event generation scripts.
* `dashboard`: Analytics UI.

*(More sections to be added as development progresses)*

## 3. Application Bootstrap

- **Framework**: The application is built using FastAPI.
- **Entrypoint**: `app/main.py` serves as the ASGI entrypoint. It initializes the FastAPI application instance with metadata (title, description, version) and registers the fundamental routing.
- **Health Endpoint**: A dedicated `/health` endpoint is exposed. This allows load balancers, container orchestrators (like Docker Compose or Kubernetes), and monitoring tools to verify deterministically that the application is alive and ready to accept traffic.

## 4. Configuration and Logging

- **Configuration Management**: Configuration is managed via the `pydantic-settings` library. The `app.core.config.Settings` class defines the schema for environment variables (`app_name`, `environment`, `debug`, `database_url`). A singleton settings object is provided via a cached dependency `get_settings()` to ensure environment variables are evaluated only once at startup.
- **Structured Logging**: Logging is handled by `structlog`. It provides deterministic, JSON-formatted structured logs in production environments, and colorized, human-readable output in development. Standard library logs are intercepted and unified into the structlog pipeline.
