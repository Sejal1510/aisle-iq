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

## P7: Store-Specific Configuration Moves From Source Code To Database

- Decision: `pipeline/video/config.py`'s `default_video_configs()` (hardcoded ST1001/ST1002 cameras, zone polygons, and video paths) and `app/services/heatmap_service.py`'s `STORE_LAYOUTS` dict (hardcoded layout image paths) are deleted. `Camera`, `Zone`, `Map`, and the new `CameraCoverage` table now hold this information, populated through `/stores` + `/stores/{store_id}/config/...` (`app/api/onboarding.py`) or the `onboarding/` UI.
- Rationale: an independent product/architecture audit found these two hardcoded structures were the concrete, in-code violations of "store-specific information belongs in configuration/data, not Python source code" -- everything below the `Zone`/`Camera` layer (events, sessions, identity, POS, analytics) was already generic.
- Tradeoff: onboarding a new store now requires either the new API/UI or the one-off `pipeline.migrate_legacy_store_config` script (kept only for ST1001/ST1002's historical migration, not a general tool) instead of editing a Python file -- more moving parts for a first-time setup, but the only way a second, unrelated store (proven in `tests/test_store_agnostic_onboarding.py` with a fictitious "Northwind Electronics" store) can exist without a code change.

## P7: Camera-To-Map Geometry Stays Deliberately Simple

- Decision: `CameraCoverage` stores a camera-frame polygon or entry line (the same shape `pipeline/video/events.py` always tested against), associated with a `Zone`. A `Zone`'s own `map_polygon_json` is a separate, purely cosmetic outline for displaying that zone on the store map. No camera calibration, homography, or automatic camera-to-map pixel transform is computed anywhere.
- Rationale: nothing in the supplied inputs (no real floor plan or calibration data was ever available -- see the P7 audit) justifies building calibration machinery, and the existing point-in-polygon logic already works correctly as a same-frame test as long as a camera's coverage geometry is defined in that camera's own frame. Human-drawn geometry (in the `onboarding/` UI, against an optional per-camera reference still image) is simpler and more auditable than an inferred transform.
- Tradeoff: the map's zone outline and a camera's coverage geometry for that zone are not mathematically related to each other -- they can visually disagree (e.g. a zone drawn slightly differently on the map vs. what a camera's polygon actually covers) since nothing enforces consistency between them beyond a human drawing both carefully. Acceptable for this phase; a future calibrated-topology phase could tighten this without changing the event/analytics layers, per principle 6 of the P7 spec.

## P7: `python-multipart` Added For File Uploads

- Decision: add `python-multipart` to `requirements.txt` (the core API runtime set, not `requirements-video.txt`).
- Rationale: FastAPI's `UploadFile` (used by the onboarding API's map and camera-reference-image upload routes) raises at import time without it. It is a small, pure-Python package with no further transitive dependencies -- nothing like the CUDA/torch situation the "Dependency Footprint" entry above describes; the Docker image size impact is negligible (tens of KB).
- Tradeoff: none identified; this is a required dependency for a feature that must exist (map upload), not an optional convenience.

## P7: Store Creation Is Open To Any Authenticated User

- Decision: `POST /stores` requires only a valid bearer token (`require_user`), not existing access to the store being created (which cannot exist yet). The creator is automatically granted `ADMIN` on the new store.
- Rationale: `require_store_role`'s existing dependency checks a `StoreAccess` row scoped to a `store_id` that, for a brand-new store, cannot exist before the store does -- there is no way to gate store creation on a role for that store without a bootstrapping exception somewhere. Granting the creator ADMIN is the same pattern the README's manual bootstrap script already uses for the very first store/user; this does not introduce a second authentication system, only a new, narrow entry point into the existing `User`/`StoreAccess` model.
- Tradeoff: any authenticated user can create an arbitrary number of stores. Acceptable for this phase (single-organization deployment, per `Organization`'s existing single-row-stub status); a future multi-tenant phase would likely gate this behind an organization-level role instead.

## P7: Legacy "BILLING" Zone-Type String Corrected To `ZoneType.CHECKOUT`

- Decision: `pipeline.migrate_legacy_store_config` writes ST1001/ST1002's queue zones with `type=ZoneType.CHECKOUT`, not the pre-P7 literal string `"BILLING"`.
- Rationale: `ZoneType` (`app/models/enums.py`) has never had a `BILLING` member -- `ReferenceDataService._parse_zone_type` silently fell back to `OTHER` whenever this string reached it. This was a latent, pre-existing labeling bug, not part of the P7 audit's core scope, but fixing it during migration (a one-time, reviewable data choice) was strictly cheaper than preserving a known-wrong value. No polygon coordinate changed.
- Tradeoff: none -- `tests/test_legacy_config_migration.py` documents and asserts this specific, intentional divergence from the otherwise-byte-identical migrated snapshot.

## P7 Security Hardening: Identifier Validation, Upload Size Limits, Upload Type Allowlisting

Following a dedicated read-only security review of the P7 onboarding API, three issues were fixed before commit:

- **Path traversal via `store_id`.** `store_id` is used as a filesystem directory name for uploads (`app/core/storage.py:save_upload`, called with `subdir=store_id`), but was accepted with no format constraint -- `POST /stores` (open to any authenticated user) could create a `Store` row with an id like `"../../../../tmp/evil"`, and a subsequent map/reference-image upload for that store would then write outside `data/maps`/`data/camera_refs` entirely.
  - Fix, two independent layers: (1) `app/schemas/spatial.py`'s new `SafeIdentifier` type (`Field(pattern=SAFE_IDENTIFIER_PATTERN, max_length=64)`) is applied to `CreateStoreRequest.store_id`, `ZoneCreateRequest.zone_id`, and `CameraCreateRequest.camera_id` -- rejects anything but `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` at the API boundary, before an unsafe id can ever be persisted. (2) `app/core/storage.py:save_upload` independently re-validates (`ensure_safe_identifier`) and, more importantly, resolves the target directory and confirms containment with `target_dir.resolve().relative_to(directory.resolve())` before writing anything -- this is the actual guarantee (verified in `tests/test_storage_security.py` by monkeypatching the identifier check out entirely and confirming the containment check alone still blocks the escape), not just the regex.
  - The safe-identifier pattern accepts every id already in use (`ST1001`, `ST1001_BILLING_QUEUE`, `ST1001_CAM_ZONE_1`, etc.) -- verified in `tests/test_onboarding_api.py::test_create_store_accepts_legacy_and_new_style_ids`.
- **Unbounded upload size.** `save_upload` streamed to disk with no cap. Fix: `Settings.max_map_upload_bytes` (20MB default) / `Settings.max_camera_reference_upload_bytes` (10MB default), both env-configurable (`MAX_MAP_UPLOAD_BYTES`/`MAX_CAMERA_REFERENCE_UPLOAD_BYTES`), enforced during the existing chunked write loop -- the file is never buffered whole in memory, and a partial file is deleted the moment the limit is crossed rather than being left on disk.
- **No file-type restriction.** Fix: `app/core/storage.py` now allowlists both the filename extension and the declared content type -- maps accept PNG/JPEG/WEBP/GIF/PDF, camera reference images accept PNG/JPEG/WEBP/GIF. SVG is deliberately excluded from both, despite being a common raster-image substitute: it's XML and can embed `<script>`, which would reopen the content-type-replay risk noted below on a file type that looks innocuous.
- Tradeoff: extension+content-type allowlisting is declared-metadata validation, not content sniffing (magic-byte inspection) -- a determined uploader can still rename/mislabel a file to pass both checks. Real content sniffing was deliberately not added: it would need either a dependency or hand-rolled magic-byte tables, and the review that identified this risk classified it as impractical to fully close without one. This is a known, accepted residual gap, not an oversight.

**Deferred (documented, not fixed in this pass -- lower severity, tracked here as follow-up hardening):**

- **Client-controlled `content_type` is still replayed when serving a file back.** `GET /stores/{id}/config/maps/{map_id}/file` and the camera-reference equivalent serve the file with the `content_type` the *uploader* declared at upload time (now constrained to the allowlist above, which meaningfully narrows this, but does not eliminate it -- e.g. a `.png` file could still be uploaded with a mislabeled-but-allowlisted content type). A future pass should serve these routes with a fixed, server-determined content type (or at minimum add `Content-Disposition: attachment` / `X-Content-Type-Options: nosniff`) rather than trusting the upload's declared type at all.
- **`POST /stores` has a check-then-insert race (TOCTOU).** Two concurrent requests creating the same `store_id` both pass the `db.get(Store, ...) is None` check before either commits; the second's `db.add`/`db.commit` would raise an unhandled integrity error (surfacing as a 500) instead of the intended 409. Low impact (store creation is not a high-concurrency path), not fixed here to avoid touching store-creation logic beyond the identifier-validation fix above.

## Keep Framework Surface Small

- Decision: do not introduce unnecessary frameworks beyond the existing FastAPI, SQLAlchemy, Pydantic, and later CV/dashboard dependencies.
- Rationale: the challenge benefits from clarity and evaluator friendliness.
- Tradeoff: some plumbing is written in project code instead of outsourced to large frameworks.
