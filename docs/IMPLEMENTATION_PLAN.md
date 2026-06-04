# Implementation Plan

This plan records the phase history from the approved Phase 0 audit and separates completed submission work from remaining roadmap work.

## Current Baseline

Implemented:

- FastAPI bootstrap and health endpoint.
- Pydantic settings.
- structlog setup.
- SQLAlchemy base/session management.
- ORM models for stores, cameras, zones, tracked entities, sessions, events, POS transactions, POS items, and transaction correlations.
- Event ingestion API.
- Batch JSONL ingestion script.
- SQLite development database.

Remaining roadmap gaps:

- Full identity alias table and cross-camera ReID workflow.
- Separate raw-event archive and replay workflow.
- Production migration setup.
- Advanced anomaly detection.
- Streaming dashboard updates.

## Phase 1: Documentation

Files:

- `docs/DESIGN.md`
- `docs/CHOICES.md`
- `docs/DATA_FLOW.md`
- `docs/IMPLEMENTATION_PLAN.md`

Deliverables:

- Final target architecture.
- Entity relationships.
- Identity mapping strategy.
- Store normalization strategy.
- Queue event lifecycle.
- POS correlation architecture.
- YOLOv8 and ByteTrack integration architecture.
- Staff detection strategy.
- Group detection strategy.
- Dashboard architecture.

Acceptance criteria:

- Documentation reflects Phase 0 audit findings.
- No runtime implementation changes are made.
- Phase 2 is not started.

## Phase 2: Queue Event Support

Findings to address:

- `queue_completed` and `queue_abandoned` are rejected by schemas.
- Event enums do not include queue values.
- ORM does not preserve queue-specific fields.
- Service logic only handles entry/exit and zone payloads.
- Numeric `track_id` values fail validation.

Expected files:

- `app/models/enums.py`
- `app/models/event.py`
- Optional new queue detail model.
- `app/schemas/event.py`
- `app/services/event_ingestion_service.py`
- `pipeline/ingest_events.py` if validation adapter behavior changes.
- New tests under `tests/`.

Design decisions:

- Preserve source event values while mapping to canonical queue lifecycle events.
- Accept numeric or string `track_id` as a source alias and normalize it to a string alias.
- Keep backward compatibility for existing entry, exit, zone entered, and zone exited events.
- Prefer a queue detail table if queue fields would otherwise bloat the generic event table.

Acceptance criteria:

- `queue_completed` validates and persists.
- `queue_abandoned` validates and persists.
- Sample JSONL can ingest without queue validation failures.
- Existing event ingestion behavior remains compatible.
- Tests cover queue schemas, persistence, and batch ingestion behavior.

## Phase 3: POS Ingestion

Expected files:

- POS schemas.
- POS ingestion service.
- POS API route or batch pipeline.
- Tests.
- README/docs update if a command is added.

Design decisions:

- Normalize store IDs during POS ingestion.
- Parse `order_date` and `order_time` into one timestamp.
- Determine whether `order_id` is order-level or row-level from dataset behavior.
- Persist item rows and preserve enough source metadata for auditability.

Acceptance criteria:

- POS CSV validates and ingests.
- Transactions and items are persisted.
- Duplicate ingestion is idempotent or clearly handled.
- Bad rows are reported without crashing the entire batch unless strict mode is selected.

## Phase 4: Correlation Engine

Expected files:

- Correlation service.
- Correlation schemas/API or batch command.
- Tests.

Design decisions:

- Use confidence-scored probabilistic matching.
- Require normalized store match for eligible candidates.
- Use queue completion and exit time as primary temporal signals.
- Treat no-match as a valid outcome.
- Exclude staff by default.

Acceptance criteria:

- Correlation candidates can be generated.
- Confidence scores are persisted with method/explanation.
- No-match scenario is tested using current sample store mismatch.
- Conversion metrics can distinguish visitors, purchasers, and uncorrelated POS rows.

## Phase 5A: YOLOv8, ByteTrack, Video To Event Generation

Expected files:

- Video metadata helpers.
- Detector abstraction.
- YOLOv8 adapter.
- ByteTrack adapter.
- Camera configuration.
- Zone/line state machines.
- Event writer using existing ingestion path.
- Tests for non-model logic.

Design decisions:

- Keep CV dependencies isolated from core API imports.
- Allow sample-event ingestion without installing GPU/CV stack.
- Use model adapters so detector/tracker choices can be swapped.
- Store confidence scores from detection and tracking.
- Generate the same event contract accepted by FastAPI.

Acceptance criteria:

- Pipeline can inspect configured videos.
- Person detections feed tracker states.
- Entry, zone, and billing event candidates can be emitted.
- Non-CV logic is testable without model weights.

## Phase 5B: Staff, Group, And ReID Support

Expected files:

- Identity/ReID models or fields.
- Staff classification service.
- Group assignment service.
- Tests.

Design decisions:

- Staff classification is confidence-based.
- Preserve source `is_staff`, `group_id`, and `group_size`.
- ReID embeddings and aliases link back to canonical tracked entities.
- Group inference should not overwrite source groups without provenance.

Acceptance criteria:

- Staff can be excluded from customer metrics.
- Source groups are persisted.
- Inferred group assignments include confidence/provenance.
- ReID-ready identity mapping exists even if embedding model is optional.

## Phase 6: Intelligence APIs

Expected files:

- Metrics API.
- Funnel API.
- Heatmap API.
- Anomaly API.
- Analytics services and schemas.
- Tests.

Design decisions:

- APIs read from normalized facts and correlations.
- Dashboard does not query DB directly.
- Heatmaps use layout coordinates when available and camera coordinates otherwise.
- Anomalies start rule-based and can become statistical later.

Acceptance criteria:

- Footfall and unique visitor metrics are exposed.
- Zone dwell and engagement metrics are exposed.
- Queue wait/completion/abandonment metrics are exposed.
- Funnel and conversion metrics are exposed.
- Heatmap data is returned in a dashboard-ready shape.

## Phase 7: Dashboard

Expected files:

- Dashboard app files under `dashboard/`.
- UI configuration and API client.
- Optional static assets/layout references.
- Basic dashboard tests if framework supports them.

Design decisions:

- Dashboard is API-backed and thin.
- First screen is the actual operational dashboard.
- Layout images are used for heatmap overlays.
- Filters include store, time range, camera, zone, staff/customer, and group mode.
- Polling is sufficient initially.

Acceptance criteria:

- Dashboard shows real-time or latest metrics.
- Heatmap overlay renders over store layouts.
- Queue monitoring is visible.
- Funnel/conversion analytics are visible.
- Store comparison is visible.

## Cross-Phase Quality Gates

- Add tests with every behavior change.
- Preserve backward compatibility where possible.
- Keep raw source data.
- Keep store IDs normalized in analytics.
- Keep identity aliases separate from canonical tracked entities.
- Use SQLAlchemy 2.0 style.
- Keep FastAPI architecture intact.
- Avoid unnecessary frameworks.
