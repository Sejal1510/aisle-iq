# System Design

This document defines the target architecture for the Store Intelligence Platform. It includes both implemented submission behavior and explicitly future-facing roadmap items.

Implemented submission scope includes event ingestion, queue fields, POS ingestion, confidence-scored correlation, analytics APIs, deterministic insights, a static dashboard, tests, and the local video-to-event pipeline. Roadmap items are called out where they are not fully implemented.

## Product Goal

Convert store CCTV footage and POS records into business intelligence:

```text
CCTV Video
-> Detection
-> Tracking
-> Event Generation
-> Intelligence APIs
-> Analytics
-> Dashboard
```

The platform must support local challenge execution with SQLite while keeping the architecture portable to PostgreSQL and production video/event workloads.

## Target Architecture

Status: mixed implemented and roadmap. The local FastAPI, SQLite, ingestion, POS, correlation, analytics, deterministic insights, dashboard, video pipeline, staff heuristics, and group heuristics paths are implemented. Production-grade raw-event replay, identity alias tables, ReID, materialized aggregates, and streaming dashboard behavior are roadmap items.

```text
data/
  videos, layouts, sample events, POS CSV

pipeline/
  video metadata
  YOLOv8 person detection
  ByteTrack person tracking
  zone and line-crossing logic
  queue state machine
  staff and group heuristic enrichers
  event writers

app/
  FastAPI routes
  Pydantic schemas
  SQLAlchemy models
  ingestion services
  normalization services
  identity mapping services
  analytics services
  deterministic insight rules
  path analytics services
  POS ingestion and correlation services

dashboard/
  real-time metrics
  heatmaps
  queue monitoring
  conversion analytics
  retail insights
  customer path analytics
  store comparison

database/
  raw events
  normalized events
  identities
  visits
  zones/layouts
  POS transactions
  correlations
  derived aggregates
```

## Module Responsibilities

- `app/api`: HTTP routes for event ingestion, POS ingestion, metrics, funnels, heatmaps, anomalies, and dashboard-facing queries.
- `app/core`: settings, logging, runtime configuration, and cross-cutting helpers.
- `app/db`: SQLAlchemy engine, sessions, migrations, and database bootstrap.
- `app/models`: ORM entities for stores, cameras, zones, identities, events, visits, POS, correlations, and analytics outputs.
- `app/schemas`: request and response schemas, including source-specific event variants.
- `app/services`: business logic for normalization, identity mapping, event ingestion, queue handling, POS ingestion, correlation, and analytics.
- `app/repositories`: focused persistence helpers once query complexity warrants them.
- `pipeline`: offline and near-real-time video processing that emits the same event contracts accepted by the API.
- `dashboard`: operator and business dashboard backed by FastAPI APIs.
- `docs`: system architecture, choices, data flow, audit, and implementation plan.

## Entity Model

Core entities:

- `Organization` / `Store`: tenant boundary and normalized store identity.
- `Camera`: camera within a store, with role metadata (entry/zone/billing) and, as of P7, its video-processing configuration (`video_path`, `start_time`, `sample_fps`, `confidence_threshold`, queue timing, and an optional `reference_image_path` still image used as an onboarding drawing backdrop) -- see "Spatial Configuration (P7)" below.
- `Zone`: business/layout area within a store, including type (`ZoneType`), revenue flag, and (P7) an optional `map_polygon_json` outline for display on the store's map.
- `Map` (P7): a retailer-uploaded floor plan asset (image/PDF) belonging to a store. Plain file storage plus display metadata -- no automatic parsing of its contents.
- `CameraCoverage` (P7): the camera-frame geometry (polygon or entry line) a camera's video processing tests against, associated with the `Zone` it represents. See "Spatial Configuration (P7)" below for why this is deliberately not a calibrated camera-to-map transform.
- `IdentityAlias`: mapping from source identity values to canonical tracked entities.
- `TrackedEntity`: canonical visitor/person representation across cameras and sessions.
- `VisitSession`: one continuous store visit by a tracked entity.
- `RawEvent`: immutable source event payload and ingestion metadata.
- `Event`: normalized event used by analytics.
- `PosTransaction`: POS order header.
- `PosTransactionItem`: product/brand/amount line item.
- `TransactionCorrelation`: confidence-scored link between a visit and a POS transaction.

Recommended relationship shape:

```text
Organization 1--N Store
Store 1--N Camera
Store 1--N Zone
Store 1--N Map
Camera N--N Zone (through CameraCoverage)
Store 1--N TrackedEntity
TrackedEntity 1--N IdentityAlias
TrackedEntity 1--N VisitSession
VisitSession 1--N Event
RawEvent 1--0..1 Event
Store 1--N PosTransaction
PosTransaction 1--N PosTransactionItem
VisitSession N--N PosTransaction through TransactionCorrelation
Zone 1--N Event
Camera 1--N Event
```

All of the above is implemented (not roadmap) as of P7. Still-roadmap items: a `QueueEventDetail`/materialized-aggregate model, and calibrated camera-to-map geometry beyond the deliberately simple camera-frame-polygon approach described below.

## Spatial Configuration (P7)

Status: implemented. Replaces the pre-P7 state, in which ST1001/ST1002's cameras, zone polygons, and layout image paths were hardcoded Python literals in `pipeline/video/config.py`'s `default_video_configs()` and `app/services/heatmap_service.py`'s `STORE_LAYOUTS` dict -- both now deleted.

- A retailer's store map is a plain uploaded image/PDF (`Map`), stored on local disk and referenced by path. No OCR, floor-plan parsing, or automatic zone extraction is performed on it -- zone boundaries are drawn (or numerically entered) by a human via the onboarding UI (`onboarding/`) and confirmed before being saved.
- A `Zone`'s `map_polygon_json` is purely a display/visualization outline on the map. It has no bearing on video processing.
- Camera-to-zone spatial mapping is deliberately conservative (see the P7 audit's "Camera -> Map -> Zone Mapping" section): a `CameraCoverage` row stores the polygon (or, for an entry camera, a threshold line) **in that camera's own frame coordinates**, associated with the `Zone` it represents. `pipeline/video/events.py`'s existing point-in-polygon/line-side logic is unchanged -- it was always a same-frame test; P7 only moved where its input geometry comes from (persisted configuration instead of Python literals). No camera intrinsic/extrinsic calibration, homography, or automatic camera-to-map reconstruction is implemented or planned for this phase; see the audit for why that would be premature given the available inputs.
- `pipeline.video.config.load_video_configs_from_db` builds the same `VideoProcessingConfig`/`PolygonZone`/`EntryLine` objects the video pipeline always used, from `Camera`/`Zone`/`CameraCoverage` rows. A camera missing a role, `video_path`, or `start_time` is silently skipped (onboarding can be a work in progress) rather than failing the whole store.
- `pipeline.migrate_legacy_store_config` is a one-off, throwaway script (not a general onboarding tool) that wrote ST1001/ST1002's former hardcoded configuration into this model; `tests/test_legacy_config_migration.py` checks the migrated configuration reconstructs byte-for-byte-equivalent polygons/lines to the deleted literals.
- Onboarding (creating a store, uploading its map, drawing zones, registering cameras, and defining coverage) happens through `/stores` + `/stores/{store_id}/config/...` (`app/api/onboarding.py`) or the minimal `onboarding/` UI on top of it -- never by editing Python source. The store-agnostic proof of this is `tests/test_store_agnostic_onboarding.py`, which onboards a fictitious non-Purplle "Northwind Electronics" store purely through this API and runs the unmodified event-ingestion/analytics stack against it.

## Identity Mapping Strategy

Status: roadmap, with partial deterministic behavior in current ingestion/session handling.

The system must not assume `id_token == track_id`.

Identity inputs come from multiple namespaces:

- Entry/exit source events use `id_token`, for example `ID_60001`.
- Zone and queue events use `track_id`, for example `101`.
- YOLOv8 plus ByteTrack will emit per-camera track IDs.
- Future ReID will emit embeddings and cross-camera match candidates.

Design:

- `TrackedEntity.id` is an internal canonical ID generated by the platform.
- `IdentityAlias` stores each external identifier with fields such as `source_system`, `source_field`, `source_value`, `store_id`, `camera_id`, `first_seen_at`, `last_seen_at`, and `confidence`.
- Entry aliases, zone aliases, queue aliases, tracker aliases, and ReID aliases can all point to the same `TrackedEntity`.
- Identity resolution is performed by an `IdentityResolutionService`, not inside route handlers.
- Resolution starts deterministic and becomes richer over time:
  - Exact existing alias match.
  - Same store/time-window match between entry and zone/queue tracks.
  - ReID embedding match when available.
  - Manual/debug override support if needed for challenge datasets.

Every normalized event stores both the canonical tracked entity ID and enough raw/source identity metadata to audit how it was resolved.

## Store Normalization Strategy

All downstream analytics use normalized store IDs.

Rules:

- Accept input variants such as `store_1076`, `ST1076`, and case-insensitive equivalents.
- Normalize numeric store suffixes to `ST####`, for example `store_1076 -> ST1076`.
- Preserve the original identifier in raw events and store alias metadata.
- Ensure `Store` exists before linking cameras, zones, events, POS rows, and sessions.
- Treat unknown/unparseable store IDs as validation or quarantine cases, depending on ingestion mode.

This prevents the current fragmentation where entry events use `store_1076` and zone/queue events use `ST1076`.

## Event Model

Target normalized event types:

- `ENTRY`
- `EXIT`
- `ZONE_ENTER`
- `ZONE_EXIT`
- `ZONE_DWELL`
- `BILLING_QUEUE_JOIN`
- `BILLING_QUEUE_COMPLETE`
- `BILLING_QUEUE_ABANDON`
- `REENTRY`

Compatibility aliases:

- `zone_entered` maps to `ZONE_ENTER`.
- `zone_exited` maps to `ZONE_EXIT`.
- `queue_completed` maps to `BILLING_QUEUE_COMPLETE`.
- `queue_abandoned` maps to `BILLING_QUEUE_ABANDON`.

Each event should contain:

- Canonical event ID.
- Raw event ID or source event ID if provided.
- Raw event relationship.
- Normalized store ID.
- Original store ID.
- Camera ID.
- Zone ID where applicable.
- Canonical tracked entity ID.
- Source identity fields.
- Event timestamp.
- Hotspot coordinates where applicable.
- Confidence fields where generated by CV.

## Visitor Session Lifecycle

Session lifecycle states:

- `IN_PROGRESS`: entry seen or inferred.
- `COMPLETED`: exit seen.
- `ORPHANED`: event stream ended or timeout elapsed without exit.
- `REENTERED`: visitor exits and returns within a configurable reentry window.

Lifecycle rules:

- `ENTRY` opens a session unless one is already active in the reentry window.
- `EXIT` closes the active session and computes dwell time.
- Zone and queue events without entry create an inferred session with lower confidence.
- `REENTRY` links a new active period to the prior visitor identity without merging unrelated visits.
- Staff sessions are retained but excluded from customer metrics by default.

## Queue Event Lifecycle

Queue analytics derive from billing-zone occupancy and explicit queue events.

Source queue events in the sample include:

- `queue_completed`
- `queue_abandoned`

Normalized lifecycle:

```text
BILLING_QUEUE_JOIN
  -> BILLING_QUEUE_COMPLETE
```

or

```text
BILLING_QUEUE_JOIN
  -> BILLING_QUEUE_ABANDON
```

For sample events that arrive as a single aggregate queue record:

- `queue_join_ts` becomes a `BILLING_QUEUE_JOIN` event or queue detail timestamp.
- `queue_served_ts` indicates service start for completed queues.
- `queue_exit_ts` closes the queue lifecycle.
- `wait_seconds` is preserved as source wait time and can be recomputed for validation.
- `queue_position_at_join` supports queue length and abandonment analysis.
- `abandoned` determines whether the terminal event is complete or abandon.

Queue metrics:

- Current queue length.
- Average wait time.
- Median and percentile wait time.
- Completion rate.
- Abandonment rate.
- Queue position distribution.
- Billing throughput.

## POS Correlation Architecture

POS ingestion loads CSV rows into normalized transaction tables.

Pipeline:

```text
CSV row
-> POS schema validation
-> store normalization
-> date/time parsing
-> transaction grouping
-> item persistence
-> correlation candidate generation
-> confidence scoring
```

Correlation inputs actually implemented (`app/services/correlation_service.py`):

- Visit session store ID.
- Visit exit time.
- Billing queue completion time.
- POS transaction timestamp.

Correlation scoring actually implemented:

- Rejection: store mismatch scores 0.0 (`method="store_mismatch"`).
- Otherwise: a single time-proximity score between the POS transaction timestamp and either the visit's queue-exit time (`method="queue_exit_time"`, preferred when a queue event exists) or its session exit time (`method="session_exit_time"`).

**Not implemented** (corrected from an earlier draft of this document that described them as inputs): zone dwell history and product/brand-to-zone mapping are not read by `CorrelationService` at all -- there is no code path connecting a visit's zone-dwell events or a transaction's line-item brands to the correlation score. `PosTransactionItem.brand_name` is stored and returned by ingestion/analytics, but never joined against `Zone`. If zone/brand-aware correlation is wanted later, it is new work, not something partially there already.
- Negative signal: store mismatch, staff identity, abandoned queue, time-window miss.

The output is `TransactionCorrelation` with `confidence_score`, `correlation_method`, and explainable features. No-match outcomes are valid and should be surfaced, especially because the current POS sample uses `ST1008` while sample events use `ST1076`.

## Detection And Tracking Architecture

The video pipeline uses YOLOv8 for person detection and ByteTrack for frame-to-frame tracking.

Stages:

1. Load video metadata and camera configuration.
2. Decode frames at configured FPS.
3. Run YOLOv8 person detection.
4. Filter detections by class and confidence.
5. Feed detections into ByteTrack.
6. Emit track states with bounding boxes, track IDs, timestamps, and confidence.
7. Project footpoints/hotspots into layout coordinates where calibration exists.
8. Evaluate entrance lines, zone polygons, and billing queue polygons.
9. Generate normalized event payloads.
10. Persist events through the same ingestion service/API path as sample events.

Camera roles:

- Entry cameras generate entry, exit, and reentry candidates.
- Zone cameras generate zone enter, zone exit, dwell, and heatmap points.
- Billing cameras generate queue join, queue complete, queue abandon, wait time, and queue length candidates.

The pipeline must keep model inference separate from event semantics so the challenge can run on sample events even when CV dependencies are unavailable.

## Staff Detection Strategy

Status: implemented with explainable retail heuristics. Uniform/color heuristics and staffing anomaly workflows remain roadmap items.

Staff should be retained in the data model but excluded from customer intelligence by default.

Signals:

- Source event `is_staff`.
- Known staff zones such as BOH, back office, stock, storage, or employee-only areas.
- Repeated long-duration presence across many sessions.
- Billing-area worker behavior, especially repeated billing/cash/checkout events without normal shopping-zone movement.
- Manual camera/zone naming rules for staff-only areas.

Strategy:

- Store `is_staff` as a confidence-scored attribute, not only a boolean.
- Store the human-readable inference reason beside the confidence score.
- Prefer conservative classification: people are marked staff only once confidence reaches the configured threshold.
- Exclude inferred staff from customer analytics and POS correlation candidates.
- Keep staff events for operational insights such as queue assistance and staffing anomalies.

## Group Detection Strategy

Status: implemented with explainable proximity and movement heuristics.

Group detection supports group conversion, family shopping patterns, and fair visitor counts.

Signals:

- Source event `group_id` and `group_size`.
- Entry-time proximity.
- Overlapping visit windows.
- Spatial proximity using normalized hotspot coordinates.
- Similar zone paths and synchronized zone transitions.

Strategy:

- Preserve source group IDs as aliases.
- Infer groups only for non-staff visitors.
- Assign deterministic store-scoped `group_id` values and persist `group_size`.
- Allow a visitor to have no group, a source group, or an inferred group.
- Metrics expose solo visitors, detected groups, and average group size.

## Dashboard Architecture

The dashboard is a business-facing application backed by FastAPI analytics endpoints.

Status: implemented for overview, staff-filtered counts, group metrics, funnel, heatmap, insights, revenue attribution, and store comparison. Active queue monitoring, anomaly views, date/camera/zone filters, staff/customer toggles, group mode filtering, and streaming refresh are roadmap items.

Views:

- Real-time overview: footfall, unique visitors, active visitors, conversion, queue length.
- Heatmaps: layout-based density by store, camera, zone, and time window.
- Zone analytics: dwell time, engagement, revenue-zone performance.
- Queue monitoring: wait time, abandonment, throughput, current queue.
- Funnel analytics: entry -> zone engagement -> billing queue -> POS conversion.
- Conversion analytics: visit-to-transaction correlation and revenue per visitor.
- Retail insights: deterministic recommendations for queue, zone, conversion, visitor, and revenue opportunities.
- Customer path analytics: common zone journeys, purchase journeys, and drop-off paths.
- Store comparison: Store 1 vs Store 2 metrics and anomalies.

Implementation direction:

- Keep dashboard logic thin.
- Compute metrics in backend services.
- Use layout images as the visual base for heatmaps.
- Provide predictable filters: store, date range, camera, zone, staff/customer, group mode.
- Support refresh polling first; add streaming only if later phases need it.

## Retail Insights Layer

Status: implemented with deterministic rules and no LLM calls.

The insights service reads existing analytics and converts metric thresholds into actionable recommendations. Each insight includes `severity`, `title`, `explanation`, and `recommendation`, plus a category for dashboard grouping.

Rule categories:

- Queue: high queue abandonment and long average wait time.
- Zone: hot zones, low-traffic dead zones, and unusually high dwell.
- Conversion: high conversion, low conversion, and funnel step drop-offs.
- Visitor: high group traffic and high staff/customer ratios.
- Revenue: high-value transaction mix and revenue opportunity zones.

The API exposes insights at `/stores/{store_id}/insights` and `/api/v1/stores/{store_id}/insights`.

## Customer Path Analytics

Status: implemented as a read-only analytics layer.

Path analytics reconstructs visitor journeys from existing event rows. It reads zone-entry and queue terminal events in timestamp order, collapses repeated consecutive steps, and aggregates the resulting path strings by session.

Outputs:

- Most common paths by visitor count.
- Top purchase journeys using matched `TransactionCorrelation` rows.
- Path drop-offs using non-purchase sessions and queue abandonment evidence.
- Per-path conversion and abandonment rates.

The API exposes path analytics at `/stores/{store_id}/paths` and `/api/v1/stores/{store_id}/paths`. It does not mutate sessions, events, or correlation records.

## Lightweight ReID Feasibility

Status: reviewed but not implemented for final submission stability.

The current repository has camera roles, timestamps, normalized footpoints, zones, and per-camera ByteTrack IDs. It does not yet have calibrated cross-camera topology, camera handoff zones, homography confidence, or appearance embeddings. Because ByteTrack IDs are camera-local, automatically stitching identities across cameras would risk merging unrelated customers and destabilizing visitor counts, dwell, conversion, and staff/group metrics.

Safe roadmap:

- Add explicit camera adjacency and handoff windows per store layout.
- Record per-track start/end zones and timestamps.
- Score identity stitching candidates with temporal continuity, zone adjacency, and optional appearance features.
- Keep stitching disabled by default and expose confidence/explanation fields before allowing analytics to consume stitched identities.

Expected accuracy without deep ReID is moderate only in constrained handoff areas and low in crowded zones. Therefore the final submission keeps deterministic session behavior unchanged.

## AI-Assisted Decisions

AI tools were used as an engineering assistant for design exploration, implementation scaffolding, test planning, and documentation polish. The final system behavior remains deterministic: event generation, ingestion, staff/group inference, analytics, correlation, and insights are implemented with explicit code paths and fixed rules rather than LLM calls.

AI-assisted work was reviewed against these constraints:

- Model choices must be explainable and practical for local challenge execution.
- Event schemas must remain valid JSONL and compatible with the provided sample event shapes.
- APIs must expose typed FastAPI/Pydantic contracts that can be tested without the dashboard.
- Recommendations must come from deterministic thresholds, not generated text.
- Roadmap items must be clearly labeled so evaluators can separate implemented behavior from future production extensions.

Key AI-assisted decisions:

- Use YOLOv8 plus ByteTrack because they are mature open-source components permitted by the challenge rules and avoid training a new detector during the hackathon window.
- Keep sample-event ingestion independent from CV dependencies so API and analytics tests remain reproducible even without GPU/video execution.
- Use confidence-scored POS correlation because CCTV and POS lack a shared visitor key.
- Use heuristic staff and group detection because it is auditable and suitable for retail operations without new model training.
- Use deterministic insight rules so recommendations are reproducible and directly tied to analytics values.

## Non-Functional Requirements

- Preserve raw source data.
- Maintain deterministic local execution.
- Keep APIs typed and documented through FastAPI/OpenAPI.
- Prefer explicit services over framework-heavy abstractions.
- Keep ingestion idempotent where source IDs exist.
- Add tests with each behavioral phase.
- Keep SQLite compatibility for the challenge, but avoid SQLite-specific business logic.
