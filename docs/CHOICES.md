# Engineering Choices

This document records architectural decisions, tradeoffs, and rationale for the Store Intelligence Platform.

## FastAPI For Backend APIs

Category: API architecture.

- Decision: use FastAPI for ingestion, intelligence APIs, and dashboard-facing endpoints.
- Rationale: typed request/response schemas, generated OpenAPI docs, dependency injection, and a small amount of operational boilerplate.
- Tradeoff: FastAPI is not a complete application framework, so repository/service boundaries must be kept disciplined manually.

## SQLAlchemy 2.0 ORM

- Decision: use SQLAlchemy 2.0 declarative models and sessions.
- Rationale: the project already uses SQLAlchemy 2.0 style, and it supports SQLite now with a clean path to PostgreSQL later.
- Tradeoff: migrations and relationship design need care. Alembic should be configured before schema evolution becomes large.

## SQLite First, PostgreSQL Ready

- Decision: keep SQLite as the default development and challenge database.
- Rationale: zero external infrastructure, easy local reproducibility, and compatible with the current repository state.
- Tradeoff: SQLite is not the target for high-concurrency production ingestion. Schema and query code should avoid SQLite-only assumptions.

## Preserve Raw Events

- Decision: every source event should be stored immutably before or alongside normalization.
- Rationale: sample events have multiple schemas, and future CV-generated events will evolve. Raw preservation allows replay, debugging, evaluator traceability, and improved identity matching.
- Tradeoff: more storage and an extra model/table, but the data volume is acceptable for the challenge and essential for correctness.

## Canonical Event Model With Source Aliases

Category: schema design.

- Decision: normalize source events into canonical event types while preserving the original source event type.
- Rationale: sample events use values like `zone_entered` and `queue_completed`, while the target event layer uses business concepts such as `ZONE_ENTER` and `BILLING_QUEUE_COMPLETE`.
- Tradeoff: ingestion code must maintain a mapping layer, but analytics become simpler and more stable.

## Identity Mapping Instead Of Direct ID Reuse

- Decision: use internal `TrackedEntity` IDs and a separate identity alias/mapping strategy.
- Rationale: `id_token`, `track_id`, ByteTrack IDs, and future ReID IDs are different namespaces. Treating them as the same ID would corrupt visitor counts and session histories.
- Tradeoff: early implementation is more complex, but it avoids a foundational analytics error.

## Deterministic Store Normalization

- Decision: normalize store identifiers to `ST####` and preserve original identifiers.
- Rationale: sample events mix `store_1076` and `ST1076`. Analytics should group them as the same store while retaining source fidelity.
- Tradeoff: malformed store IDs need explicit validation/quarantine behavior.

## Queue Events As A Lifecycle

Category: schema design.

- Decision: model queue events as a lifecycle with join and terminal states, even when source data arrives as an aggregate record.
- Rationale: queue analytics need wait time, abandonment, completion, position, and throughput. A lifecycle model supports both explicit future events and current aggregate sample events.
- Tradeoff: the sample `queue_completed` record may produce multiple normalized facts or one event plus detail. Phase 2 should choose the minimal backward-compatible representation.

## POS Correlation Uses Confidence Scores

- Decision: link visits to POS transactions through confidence-scored correlations, not hard joins.
- Rationale: CCTV and POS have no guaranteed shared visitor key. The current POS sample store does not match the event sample store, so no-match outcomes must be valid.
- Tradeoff: correlation is probabilistic and requires explainable scoring features for reviewer trust.

## YOLOv8 For Person Detection

Category: model selection.

- Decision: use YOLOv8 as the detector for person bounding boxes.
- Rationale: mature, well-known, easy to run locally, and strong enough for retail CCTV person detection.
- Tradeoff: model weights and runtime dependencies are heavier than the current backend stack. The system must keep sample-event ingestion independent from CV setup.

## ByteTrack For Tracking

Category: model selection.

- Decision: use ByteTrack for per-camera person tracking.
- Rationale: proven MOT algorithm, handles low-confidence detections well, and pairs naturally with YOLO detection outputs.
- Tradeoff: ByteTrack track IDs are camera/session-scoped and must not become canonical visitor IDs without identity resolution.

## ReID As Future Enrichment

- Decision: design for ReID but do not block early phases on it.
- Rationale: cross-camera identity is needed for robust visitor lifecycle tracking, but the challenge can progress through sample events and deterministic heuristics first.
- Tradeoff: initial identity mapping may be heuristic until embedding-based ReID is added.
- Status: roadmap. ReID is not claimed as a completed submission feature.

## Staff Detection Is Confidence-Based

- Decision: represent staff classification as an attribute with confidence and source, not just a permanent boolean.
- Rationale: staff labels may come from sample events, uniforms, BOH behavior, or repeated long sessions. Some signals are uncertain.
- Tradeoff: analytics must default to excluding likely staff while allowing filters and review.
- Status: implemented with source flags, staff/BOH zone names, long-presence signals, and billing-worker behavior.

## Group Detection Is Optional But Preserved

- Decision: preserve source group IDs and support later inferred group assignments.
- Rationale: group size affects visitor counts, conversion interpretation, and shopping behavior analytics.
- Tradeoff: group inference can be noisy; individual visitor metrics remain the primary baseline.
- Status: implemented with entry-time, overlap, hotspot proximity, and zone-path consistency heuristics.

## Backend-Owned Analytics

- Decision: compute metrics, funnels, heatmaps, anomalies, and conversion analytics in backend services.
- Rationale: keeps dashboard thin, testable, and consistent across API consumers.
- Tradeoff: backend query complexity increases, which reinforces the need for repositories or focused query services later.

## Deterministic Insights Before LLMs

- Decision: generate retail recommendations from fixed threshold rules instead of language model calls.
- Rationale: evaluator output must be reproducible, explainable, and directly traceable to analytics values.
- Tradeoff: recommendations are less flexible than free-form reasoning, but every insight can be tested and audited.
- Status: implemented for queue, zone, conversion, visitor, and revenue insight categories.

## Dashboard Polling Before Streaming

- Decision: start with dashboard polling against metrics APIs; add streaming only if needed.
- Rationale: challenge data is file/sample oriented, and polling is simpler to test and demo.
- Tradeoff: not ideal for sub-second monitoring, but acceptable for store intelligence dashboards.
- Status: implemented as local API fetches; streaming is roadmap.

## Tests Grow With Each Phase

- Decision: add targeted tests with every behavioral implementation phase.
- Rationale: the current repository has no collected tests, and ingestion/correlation logic can silently corrupt analytics.
- Tradeoff: implementation velocity slows slightly, but production-quality confidence improves.

## Keep Framework Surface Small

- Decision: do not introduce unnecessary frameworks beyond the existing FastAPI, SQLAlchemy, Pydantic, and later CV/dashboard dependencies.
- Rationale: the challenge benefits from clarity and evaluator friendliness.
- Tradeoff: some plumbing is written in project code instead of outsourced to large frameworks.
