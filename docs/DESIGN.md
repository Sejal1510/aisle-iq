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
