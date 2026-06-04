# Dashboard Architecture

The dashboard is a lightweight static application served by FastAPI from `dashboard/`.

## Structure

```text
dashboard/
  index.html
  styles.css
  app.js
```

FastAPI mounts the directory at `/dashboard`, while the analytics routes remain available at:

- `GET /stores/{store_id}/metrics`
- `GET /stores/{store_id}/funnel`
- `GET /stores/{store_id}/heatmap`
- `GET /stores/{store_id}/insights`
- `GET /stores/{store_id}/paths`
- `GET /stores/{store_id}/layout`
- `GET /api/v1/stores/{store_id}/metrics`
- `GET /api/v1/stores/{store_id}/funnel`
- `GET /api/v1/stores/{store_id}/heatmap`
- `GET /api/v1/stores/{store_id}/insights`
- `GET /api/v1/stores/{store_id}/paths`
- `GET /api/v1/stores/{store_id}/layout`

The dashboard fetches the unversioned store routes from the same origin. This avoids CORS setup and keeps local challenge execution simple.

## Views

- Overview: total visitors, unique visitors, inferred staff filtered from customer metrics, solo visitors, detected groups, average group size, conversion rate, attributed revenue, average dwell time, queue abandonment rate, store heatmap, zone dwell, and revenue attribution.
- Funnel: visitors -> queue join -> queue complete -> purchase.
- Paths: most common customer journeys, purchase journeys, and drop-off paths.
- Insights: deterministic queue, zone, conversion, visitor, and revenue recommendations with severity, explanation, and action.
- Store Comparison: comparison-ready layout for multiple stores, currently loading the selected store and `ST1002`.

## Heatmap

The Overview heatmap renders the store layout PNG returned by `/stores/{store_id}/layout` and overlays bucketed hotspot points from `/stores/{store_id}/heatmap`.

The heatmap API returns normalized coordinates:

- `x`: horizontal position from `0.0` left to `1.0` right.
- `y`: vertical position from `0.0` top to `1.0` bottom.
- `intensity`: bucket intensity from `0.0` to `1.0`, normalized against the store's highest-count bucket.
- `count`: raw event count in that bucket.

The dashboard positions each point with CSS percentages over the rendered image. Intensity is shown with larger point diameter, stronger opacity, and warmer color. The summary displays bucket point count and the total source hotspot event count.

Store switching is handled by the topbar store selector. `ST1001` and `ST1002` are available because both have generated hotspot events and layout images in `data/`.

## Demo POS Fixture

The original challenge POS file remains untouched:

```text
data/POS - sample transactionsb1e826f (1).csv
```

That file belongs to `ST1008`, while the CCTV-generated dashboard stores are `ST1001` and `ST1002`. Because the correlation engine correctly requires same-store evidence, the original POS sample should produce no attributed revenue for the dashboard stores.

For demo/business-alignment runs, the project includes a generated fixture:

```text
data/demo_pos_st1001_st1002.csv
```

Generate it with:

```powershell
python -m pipeline.generate_demo_pos
```

The generator reads actual visitor sessions already stored for `ST1001` and `ST1002`, reuses product and brand patterns from the original POS sample, and writes believable demo purchase rows. It also inserts deterministic generated checkout-completion events for those same sessions so the unchanged correlation service has a strong, explainable timing signal. These generated checkout events and POS rows are demo alignment facts, not original challenge observations.

The intended demo flow is:

```powershell
python -m pipeline.generate_demo_pos
python -m pipeline.ingest_pos data/demo_pos_st1001_st1002.csv
```

Then run the existing correlation service with `replace_existing=True` before opening the dashboard:

```powershell
python -c "from app.db.session import init_db, SessionLocal; from app.services.correlation_service import CorrelationService; init_db(); db=SessionLocal(); CorrelationService(db).correlate_all(replace_existing=True); db.commit(); db.close()"
```

The dashboard metrics remain backed by normal `PosTransaction`, `PosTransactionItem`, and `TransactionCorrelation` records; no dashboard metric is hardcoded.

## Local Run

Start the FastAPI app:

```powershell
uvicorn app.main:app --reload
```

Open:

```text
http://127.0.0.1:8000/dashboard
```

The dashboard will request analytics from the running FastAPI app. If the local database has no facts for the selected store, the UI renders zero-value metrics.

## Testing

Dashboard tests assert:

- FastAPI registers the `/dashboard` mount.
- Required static assets exist.
- `app.js` consumes the required analytics and heatmap APIs.
- The HTML contains the required dashboard views, insights panel, and heatmap surface.
- Heatmap rendering uses the API contract fields and CSS overlay classes.
- Insights rendering uses severity, category, explanation, and recommendation fields.
- Path rendering uses most common paths, purchase journeys, drop-off paths, and conversion/drop-off rates.
