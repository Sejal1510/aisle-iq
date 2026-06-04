# Data Flow

This document describes how data moves through the Store Intelligence Platform. It distinguishes implemented submission paths from roadmap paths where needed.

Implemented paths: sample/generated event ingestion, video event generation, staff/group heuristic inference, POS ingestion, visit-to-POS correlation, analytics APIs, and the dashboard. Roadmap paths: raw-event replay storage, production identity aliasing, ReID enrichment, materialized snapshots, and advanced anomaly workflows.

## End-To-End Flow

```text
CCTV videos
  -> frame decoding
  -> YOLOv8 person detection
  -> ByteTrack tracking
  -> identity alias capture
  -> zone/entry/queue state machines
  -> raw event preservation
  -> normalized event ingestion
  -> session and queue lifecycle updates
  -> analytics tables/services
  -> FastAPI intelligence APIs
  -> dashboard

POS CSV
  -> POS validation
  -> store normalization
  -> transaction/item persistence
  -> visit correlation
  -> conversion analytics
  -> APIs/dashboard
```

The sample JSONL event stream enters at the raw event preservation step and uses the same normalization and persistence path as generated CV events.

## CCTV Video To Detections

Inputs:

- Store 1 entry, zone, and billing videos.
- Store 2 entry, zone, and billing videos.
- Camera role metadata.
- Store layout and calibration metadata.

Processing:

- Read video metadata.
- Decode frames at configured sampling FPS.
- Run YOLOv8 person detection.
- Keep person detections above confidence threshold.
- Store or stream detection outputs with bounding boxes and confidence scores.

Output detection fields:

- `store_id`
- `camera_id`
- `frame_index`
- `timestamp`
- `bbox_x1`, `bbox_y1`, `bbox_x2`, `bbox_y2`
- `confidence`
- `class_name`

## Detections To Tracks

Processing:

- Feed detections into ByteTrack per camera.
- Produce stable per-camera `track_id` values.
- Track each person's bounding box and footpoint over time.
- Retain track confidence and lifecycle state.

Important rule:

- ByteTrack IDs are aliases, not canonical visitor IDs.

Output track fields:

- `source_system = bytetrack`
- `source_track_id`
- `store_id`
- `camera_id`
- `timestamp`
- `bbox`
- `footpoint`
- `tracking_confidence`

## Tracks To Events

Event generation depends on camera role and layout geometry.

Entry cameras:

- Line crossing into the store emits `ENTRY`.
- Line crossing out of the store emits `EXIT`.
- Returning within a configured time window emits or links `REENTRY`.

Zone cameras:

- Footpoint enters zone polygon emits `ZONE_ENTER`.
- Footpoint exits zone polygon emits `ZONE_EXIT`.
- Time spent inside a zone emits or derives `ZONE_DWELL`.
- Hotspot coordinates feed heatmaps.

Billing cameras:

- Footpoint enters queue polygon emits `BILLING_QUEUE_JOIN`.
- Visitor reaches served position and exits through billing emits `BILLING_QUEUE_COMPLETE`.
- Visitor leaves queue polygon without service emits `BILLING_QUEUE_ABANDON`.
- Queue length is derived from active tracks in queue polygon.

## Raw Event Preservation

All event sources should first produce a raw event record.

Raw event fields:

- Source name, for example `sample_jsonl`, `api`, or `video_pipeline`.
- Original event type.
- Original payload JSON.
- Source event ID where available, such as `queue_event_id`.
- Ingestion timestamp.
- Validation status.
- Error message if quarantined.

Raw preservation supports replay when schemas or identity resolution improve.

## Event Normalization

Normalization responsibilities:

- Normalize store IDs, for example `store_1076 -> ST1076`.
- Map source event types to canonical event types.
- Normalize timestamp field names.
- Normalize identity fields into identity aliases.
- Coerce source-safe values such as numeric `track_id` into stable string aliases.
- Normalize zone metadata and revenue-zone flags.
- Preserve source fields that do not fit the canonical event table in raw payload or detail tables.

Canonical event output:

- `event_type`
- `store_id`
- `original_store_id`
- `camera_id`
- `zone_id`
- `tracked_entity_id`
- `source_identity_type`
- `source_identity_value`
- `timestamp`
- `hotspot_x`
- `hotspot_y`
- `confidence`
- `raw_event_id`

## Identity Resolution Flow

Status: roadmap, with only partial deterministic session linking implemented today.

```text
source identity
  -> normalize namespace
  -> lookup IdentityAlias
  -> resolve TrackedEntity
  -> update alias timestamps/confidence
  -> attach event to active VisitSession
```

Resolution order:

1. Exact alias match.
2. Active session match in the same store and time window.
3. Entry/zone/queue heuristic match using timing and demographics.
4. ReID embedding match when available.
5. Create new tracked entity with an inferred session.

This avoids treating `id_token`, `track_id`, and ByteTrack IDs as interchangeable.

## Session Lifecycle Flow

```text
ENTRY
  -> create or reopen VisitSession
ZONE_ENTER / ZONE_EXIT / ZONE_DWELL
  -> attach to active session
BILLING_QUEUE_JOIN / COMPLETE / ABANDON
  -> attach queue facts to active session
EXIT
  -> complete VisitSession and compute dwell
timeout
  -> mark VisitSession ORPHANED
```

Zone or queue events without a known entry can create inferred sessions so analytics do not drop useful observations.

## Staff And Group Inference Flow

Status: implemented with deterministic heuristics.

```text
VisitSession + Event history
  -> staff signal scoring
  -> is_staff + staff_confidence_score + reason
  -> non-staff session clustering
  -> group_id + group_size
  -> analytics/dashboard metrics
```

Staff scoring signals:

- Explicit source `is_staff`.
- Known staff/BOH zone IDs.
- Repeated long-duration store presence.
- Repeated billing-area behavior without customer-like shopping movement.

Group clustering signals:

- Entry timestamps within the configured group window.
- Overlapping visit windows.
- Similar zone paths.
- Nearby hotspot coordinates.
- Conservative maximum group size to avoid over-clustering dense store traffic.

The inference service updates existing `TrackedEntity` rows and is called after event ingestion. The same service can be run in batch for a store after generated events are loaded.

## Queue Event Flow

Sample queue aggregate:

```text
queue_completed
  queue_join_ts
  queue_served_ts
  queue_exit_ts
  wait_seconds
  queue_position_at_join
  abandoned=false
```

Normalized interpretation:

- Join timestamp marks queue entry.
- Served timestamp marks service start.
- Exit timestamp marks queue completion.
- Wait seconds is stored and can be recomputed as served minus join.

For abandonment:

```text
queue_abandoned
  queue_join_ts
  queue_served_ts=null
  queue_exit_ts
  abandoned=true
```

Normalized interpretation:

- Join timestamp marks queue entry.
- Exit timestamp marks abandonment.
- Wait seconds is time spent before leaving.

## POS Flow

POS CSV fields:

- `order_id`
- `order_date`
- `order_time`
- `store_id`
- `product_id`
- `brand_name`
- `total_amount`

Processing:

- Validate required fields.
- Normalize store ID.
- Parse date and time into transaction timestamp.
- Determine transaction grouping by `order_id`.
- Persist transaction and item rows.
- Preserve source row metadata for auditability.

Output:

- `PosTransaction`
- `PosTransactionItem`

## Correlation Flow

Status: implemented for same-store temporal matching using queue completion, exit/session timing, staff exclusion, confidence scores, explanations, ambiguous matches, and no-match rows. Product-zone and group behavior scoring are roadmap enhancements.

```text
VisitSession
  + queue lifecycle
  + POS transactions
  + zone dwell
  + product-zone map
  -> candidate generation
  -> confidence scoring
  -> TransactionCorrelation
```

Candidate filters:

- Same normalized store.
- POS timestamp within configurable window of queue completion or visit exit.
- Customer sessions only by default.

Scoring features:

- Time distance from queue completion.
- Time distance from exit.
- Queue completion versus abandonment.
- Zone dwell matching product/brand category.
- Group size and group behavior.
- Store match.

No correlation is created when confidence is below threshold, and no-match counts are exposed for conversion analysis.

## Analytics Flow

Status: implemented for footfall, unique visitors, staff-filtered counts, solo visitors, groups, average group size, dwell, queue abandonment, conversion, attributed revenue, funnels, heatmaps, deterministic insights, and store comparison inputs. Materialized snapshots and anomaly analytics are roadmap items.

Base facts:

- Visits.
- Events.
- Queue details.
- Zone dwell.
- Heatmap points.
- POS transactions.
- Transaction correlations.

Derived metrics:

- Footfall.
- Unique visitors.
- Active visitors.
- Average dwell time.
- Zone engagement.
- Queue wait and abandonment.
- Conversion rate.
- Revenue per visitor.
- Store comparison.
- Anomalies.

APIs compute metrics from facts first. Materialized snapshots can be added when dashboard latency requires them.

## Path Analytics Flow

Status: implemented as read-only derived analytics.

```text
VisitSession
  + ordered zone/queue events
  + matched correlations
  -> zone path reconstruction
  -> path conversion/drop-off metrics
  -> FastAPI paths endpoint
  -> dashboard path panel
```

Path analytics does not change event ingestion, identity, staff/group inference, or POS correlation. It derives journeys from already persisted facts.

## Insights Flow

Status: implemented with deterministic rules.

```text
Store metrics + funnel + zone dwell
  -> threshold rules
  -> severity-ranked insights
  -> FastAPI insights endpoint
  -> dashboard insights panel
```

The insights layer does not call an LLM. It uses fixed rules for queue risk, zone activity, conversion drop-offs, group/staff patterns, and revenue opportunities.

## Dashboard Flow

Status: implemented for local API-backed dashboard rendering. Versioned contracts exist alongside unversioned dashboard convenience routes; advanced filters and anomaly views are roadmap items.

```text
Dashboard filters
  -> FastAPI metrics/funnel/heatmap/insights/paths endpoints
  -> backend analytics services
  -> JSON responses
  -> charts, tables, recommendations, floorplan overlays
```

The dashboard should not query the database directly. It should rely on versioned API contracts so backend and UI can evolve independently.
