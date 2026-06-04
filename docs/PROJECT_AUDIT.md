# Project Audit

Phase 0 audit for the Purplle Tech Challenge 2026 Store Intelligence Platform.

Status note: this is the original baseline audit. Several gaps listed below have since been implemented, including queue support, POS ingestion, correlation, analytics APIs, dashboard, staff/group heuristics, insights, and customer path analytics. Keep this document as historical traceability; use `README.md`, `docs/DESIGN.md`, `docs/DATA_FLOW.md`, and `docs/DASHBOARD.md` for final submission status.

## Scope

This audit inspected the repository structure, source files, existing documentation, tests, local SQLite schema/state, sample datasets, store layout images, and camera video inventory. Generated/runtime folders such as `.git`, `.venv`, `__pycache__`, and placeholder `.gitkeep` files were not treated as project source, except where they affected repository state.

No application code was changed in this phase.

## Repository State

- Current branch: `master`.
- Git history matches the supplied phase history, ending at `f1d07e4 Add batch event ingestion pipeline`.
- Worktree was clean before creating this audit document.
- Main implemented areas:
  - FastAPI application bootstrap.
  - Pydantic settings.
  - structlog setup.
  - SQLAlchemy 2.0 declarative base/session lifecycle.
  - ORM models for stores, cameras, zones, tracked entities, visit sessions, events, POS transactions, POS items, and transaction correlations.
  - Event ingestion API at `/api/v1/events/`.
  - Batch JSONL event ingestion script.
  - SQLite development database.
- Empty/stub areas:
  - `dashboard/` contains only a placeholder.
  - `repositories/` contains only a package placeholder.
  - `tests/` contains only a placeholder.
  - Detection/tracking/video processing is not implemented.
  - Analytics/intelligence APIs are not implemented.

## File Inventory Reviewed

Top-level project files:

- `README.md`
- `requirements.txt`
- `.env.example`
- `.gitignore`
- `Dockerfile`
- `docker-compose.yml`
- `store_intelligence.db`

Application source:

- `app/main.py`
- `app/core/config.py`
- `app/core/logging.py`
- `app/db/base.py`
- `app/db/session.py`
- `app/api/events.py`
- `app/schemas/event.py`
- `app/services/event_ingestion_service.py`
- `app/models/enums.py`
- `app/models/event.py`
- `app/models/store.py`
- `app/models/tracking.py`
- `app/models/pos.py`
- package `__init__.py` files

Pipeline and docs:

- `pipeline/ingest_events.py`
- `docs/DESIGN.md`
- `docs/CHOICES.md`

Datasets/assets:

- `data/sample_eventsbe42122 (1).jsonl`
- `data/POS - sample transactionsb1e826f (1).csv`
- Store 1 layout PNG and MP4 files.
- Store 2 layout PNG and MP4 files.

## Architecture Assessment

The repository has a clean early backend foundation. The FastAPI app is small, the configuration surface is simple, logging is centralized, and SQLAlchemy 2.0 style is used consistently. The existing ORM already anticipates later phases by modeling POS transactions and transaction correlations, which is a good architectural starting point.

The main architectural mismatch is identity handling. Current ingestion treats `id_token` and `track_id` as direct `TrackedEntity.id` values. The challenge explicitly says not to assume `id_token == track_id`; identity mapping and future ReID support need a separate model/service boundary.

Store identifier normalization is also not implemented. Entry events use `store_code` such as `store_1076`, while zone and queue events use normalized IDs such as `ST1076`. Current ingestion persists these as separate store IDs, which fragments sessions and analytics.

Raw event preservation is not implemented. The `event` table stores normalized columns but does not retain the source JSON payload, source event ID, or source schema variant. This will make debugging, reprocessing, and challenge traceability weaker.

## Current API Behavior

`app/main.py` defines:

- `GET /`
- `GET /health`
- `POST /api/v1/events/`

The event endpoint accepts a discriminated Pydantic union from `app/schemas/event.py` and delegates persistence to `EventIngestionService`.

The endpoint currently catches all exceptions and returns HTTP 500 for ingestion failures. Validation errors raised by FastAPI before entering the route should still produce 422 responses, but service-level domain errors are not separated from infrastructure errors.

## Current Data Model

Implemented tables in SQLite:

- `store`
- `camera`
- `zone`
- `tracked_entity`
- `visit_session`
- `event`
- `pos_transaction`
- `pos_transaction_item`
- `transaction_correlation`

The tracked database currently contains:

- 1 tracked entity.
- 1 in-progress visit session.
- 2 entry events.
- No stores, cameras, zones, POS transactions, POS items, or correlations.

Important schema gaps:

- No identity mapping table between source identities, track IDs, id tokens, sessions, and future ReID identities.
- No raw payload/source event table or raw JSON column.
- No queue-specific fields on events.
- No dwell-specific event support.
- No explicit camera metadata beyond ID/store relationship.
- No layout geometry, polygon zones, homography, or coordinate calibration tables.
- No uniqueness constraints for idempotent ingestion.
- No migration setup despite `alembic` being listed as a dependency.

## Event Support Assessment

Currently supported event enum values:

- `entry`
- `exit`
- `zone_entered`
- `zone_exited`

Target event layer requires:

- `ENTRY`
- `EXIT`
- `ZONE_ENTER`
- `ZONE_EXIT`
- `ZONE_DWELL`
- `BILLING_QUEUE_JOIN`
- `BILLING_QUEUE_ABANDON`
- `REENTRY`

Dataset event values observed:

- `entry`: 3
- `exit`: 1
- `zone_entered`: 3
- `zone_exited`: 3
- `queue_completed`: 2
- `queue_abandoned`: 1

Queue events are not supported because:

- `app/schemas/event.py` has no queue event Pydantic models.
- The `EventPayload` discriminated union excludes `queue_completed` and `queue_abandoned`.
- `app/models/enums.py` excludes queue event values.
- `app/services/event_ingestion_service.py` only normalizes entry/exit and zone payload shapes.
- `app/models/event.py` has no queue fields such as join, served, exit timestamps, wait seconds, queue position, abandonment flag, or source queue event ID.

Additional discovered ingestion issue:

- Zone and queue sample events use numeric `track_id` values, but schemas declare `track_id: str`. Pydantic v2 rejects these numeric IDs without coercion or a broader type.

## Dataset Assessment

### sample_events JSONL

The JSONL file contains 13 events and represents a reference event stream, not a complete production stream.

Schema variants:

- Entry/exit events use `id_token`, `store_code`, `event_timestamp`, and demographic fields named `gender_pred`/`age_pred`.
- Zone events use `track_id`, `store_id`, `event_time`, zone metadata, and hotspot coordinates.
- Queue events use `queue_event_id`, `queue_join_ts`, `queue_served_ts`, `queue_exit_ts`, `wait_seconds`, `queue_position_at_join`, and `abandoned`.

Important data issue:

- Entry/exit identity (`ID_60001`, etc.) and zone/queue identity (`101`, `102`, `103`) are separate namespaces. Correlation must be explicit.

### POS CSV

The POS CSV contains 101 rows with headers:

- `order_id`
- `order_date`
- `order_time`
- `store_id`
- `product_id`
- `brand_name`
- `total_amount`

Observed POS store ID:

- `ST1008`

The POS sample does not align with the sample event store `ST1076`/`store_1076`, so correlation logic must handle no-match scenarios and expose confidence rather than assuming a transaction exists for every visit.

Rows appear item-like: multiple rows can share the same `order_date`/`order_time`, but the observed `order_id` values are unique per row in the sample. Phase 3 should determine whether `order_id` is an order-level ID or row/item ID before grouping.

## Layout Image Assessment

### Store 1

The Store 1 layout is a compact FOH plan. The entrance/glazing is on the left side. Billing/cash counter is on the right. Product/display areas include wall units and labeled zones such as Salm, TFS, Minimalis, Aqualogi, Foxtal, JC, Faces Canada, Mars+Nybae, Mens, L'Oreal, Beauty, Accessories, fragrance/makeup units, and central makeup units.

Implications:

- Entry camera events should be mapped to the left entrance boundary.
- Billing queue analytics should focus on the right-side cash counter area.
- Zone analytics need layout-derived polygons, not only string zone IDs.

### Store 2

The Store 2 layout has an entrance at the bottom center, a large FOH area, a BOH area at the top/right, perimeter wall units, center gondolas, makeup units, and a cash counter near the top-middle partition.

Implications:

- Store 2 needs separate FOH/BOH semantics for staff filtering and customer-accessible heatmaps.
- Zone geometry should distinguish wall units, gondolas, makeup units, billing, entrance, and non-customer BOH space.

## Video Asset Inventory

Store 1 videos:

- `CAM 1 - zone.mp4`
- `CAM 2 - zone.mp4`
- `CAM 3 - entry.mp4`
- `CAM 5 - billing.mp4`

Store 2 videos:

- `billing_area.mp4`
- `entry 1.mp4`
- `entry 2.mp4`
- `zone.mp4`

`ffprobe` and OpenCV are not available in the current environment, so codec, frame count, FPS, and resolution were not extracted during Phase 0. Phase 5A should add explicit video metadata inspection as part of the detection pipeline setup.

## Batch Ingestion Check

Running the batch importer against the sample JSONL revealed:

- 13 total records processed.
- 4 succeeded.
- 9 failed.

Failure causes:

- 6 zone records failed because numeric `track_id` values do not satisfy `track_id: str`.
- 3 queue records failed because queue event types are not part of the discriminated union.

The SQLite database was restored to its original tracked state after this audit check.

## Test Assessment

`pytest -q` currently reports no tests collected.

Required near-term coverage:

- Pydantic validation for all sample event variants.
- Store ID normalization.
- Identity mapping between source IDs and tracked entities.
- Event persistence idempotency.
- Session lifecycle behavior.
- Queue completed/abandoned ingestion.
- POS CSV ingestion and grouping behavior.
- Correlation confidence/no-match behavior.

## Documentation Assessment

Existing documentation is preliminary:

- `README.md` states the project goal but has no runnable quickstart.
- `docs/DESIGN.md` outlines the intended components but does not yet define data flow, entities, event lifecycle, detection architecture, analytics APIs, or dashboard architecture.
- `docs/CHOICES.md` explains early technology decisions but does not yet cover identity mapping, raw event preservation, store normalization, ReID, queue modeling, POS correlation, or dashboard trade-offs.

Phase 1 should expand documentation before further implementation.

## Risks And Gaps

- Identity model is currently too simple for multi-camera tracking and ReID.
- Store IDs are not normalized, causing fragmented analytics.
- Queue event support is absent across schema, enum, ORM, and service layers.
- Zone event ingestion currently fails on the provided sample because `track_id` is numeric.
- Raw source events are not preserved.
- POS ORM exists, but no ingestion service/API/pipeline exists.
- Correlation ORM exists, but no correlation engine exists.
- There are no analytics APIs.
- There is no dashboard implementation.
- There is no CV pipeline despite YOLO/ByteTrack being part of the target.
- There are no tests.
- Alembic is listed but not configured.
- Existing docs are not yet sufficient for reviewer understanding.

## Recommended Phase Order

Follow the requested phase order without jumping ahead:

1. Phase 1: Write complete architecture and data-flow documentation.
2. Phase 2: Add queue event support and tests, while preserving backward compatibility.
3. Phase 3: Implement POS ingestion.
4. Phase 4: Implement visitor-to-transaction correlation.
5. Phase 5A: Implement video-to-event generation with YOLOv8 and ByteTrack.
6. Phase 5B: Add staff detection, group detection, and ReID support.
7. Phase 6: Add metrics, funnel, heatmap, and anomaly APIs.
8. Phase 7: Build the dashboard.

## Phase 0 Acceptance Criteria

- Repository structure inspected.
- Source files reviewed.
- Existing documentation reviewed.
- Datasets reviewed.
- Layout images inspected.
- Local database schema/state inspected.
- Current test state verified.
- Event support gaps identified.
- `docs/PROJECT_AUDIT.md` created.
