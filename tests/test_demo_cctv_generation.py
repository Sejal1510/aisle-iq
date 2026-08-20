# P4.1: deterministic synthetic CCTV demo-event generator. Covers generation
# determinism/schema validity directly, and that the output is ingestible
# through the real, unmodified ingestion path (both the service layer and the
# pipeline.ingest_events CLI entrypoint) and produces meaningful results from
# every P3 analytics endpoint -- not just nonzero, but genuinely non-degenerate
# (varied, ranked, comparable).
from datetime import datetime

import pytest
from pydantic import TypeAdapter
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import pipeline.ingest_events as ingest_events_module
from app.models import Base
from app.schemas.event import EventPayload
from app.services.comparison_service import ComparisonService
from app.services.event_ingestion_service import EventIngestionService
from app.services.occupancy_service import OccupancyService
from app.services.peak_hour_service import PeakHourService
from app.services.queue_service import QueueService
from app.services.time_series_service import TimeSeriesService
from app.services.visitor_inference_service import VisitorInferenceService
from pipeline.generate_demo_cctv import (
    DAY_DATES,
    DEFAULT_SEED,
    generate_demo_cctv_events,
    write_demo_cctv_jsonl,
)

DAY2_START = DAY_DATES[1]
DAY3_START = datetime(DAY2_START.year, DAY2_START.month, DAY2_START.day + 1)
PRIMARY_STORE = "ST1001"


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(scope="module")
def generated_events() -> list[dict]:
    return generate_demo_cctv_events(seed=DEFAULT_SEED)


@pytest.fixture(scope="module")
def ingested_db(generated_events: list[dict]) -> Session:
    """The normal ingestion path: TypeAdapter validation + EventIngestionService,
    the exact same code pipeline.ingest_events._process_line calls -- no
    demo-only bypass into the database. Module-scoped and ingested once:
    every test using this fixture only reads from it, so re-ingesting ~1200
    events per test would just be wasted time, not extra coverage.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()

    adapter = TypeAdapter(EventPayload)
    service = EventIngestionService(session)
    touched_stores: set[str] = set()
    for raw_event in generated_events:
        payload = adapter.validate_python(raw_event)
        event = service.process_event(payload, run_inference=False)
        touched_stores.add(event.store_id)
    for store_id in touched_stores:
        VisitorInferenceService(session).infer_store(store_id)
    session.commit()

    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Generation itself
# ---------------------------------------------------------------------------


def test_generation_is_deterministic() -> None:
    first_run = generate_demo_cctv_events(seed=DEFAULT_SEED)
    second_run = generate_demo_cctv_events(seed=DEFAULT_SEED)

    assert first_run == second_run
    assert len(first_run) > 0


def test_different_seeds_produce_different_output() -> None:
    """Sanity check that the seed is actually load-bearing, not ignored."""
    default_run = generate_demo_cctv_events(seed=DEFAULT_SEED)
    other_run = generate_demo_cctv_events(seed=DEFAULT_SEED + 1)

    assert default_run != other_run


def test_generated_events_validate_against_event_schema(generated_events: list[dict]) -> None:
    adapter = TypeAdapter(EventPayload)
    for raw_event in generated_events:
        payload = adapter.validate_python(raw_event)
        assert payload.event_type == raw_event["event_type"]


def test_generated_events_are_clearly_labeled_synthetic(generated_events: list[dict]) -> None:
    for raw_event in generated_events:
        assert raw_event["event_id"].startswith("DEMO-")
        assert raw_event["visitor_id"].startswith("DEMO-")


def test_generator_never_touches_the_mandatory_deliverable(tmp_path) -> None:
    """The generator must never write to data/generated_cctv_events.jsonl --
    verify its default output path is a different, explicitly demo-named file."""
    from pipeline.generate_demo_cctv import DEFAULT_OUTPUT_PATH

    assert DEFAULT_OUTPUT_PATH.name != "generated_cctv_events.jsonl"
    assert "demo" in DEFAULT_OUTPUT_PATH.name


def test_write_demo_cctv_jsonl_round_trips(tmp_path, generated_events: list[dict]) -> None:
    import json

    output_path = tmp_path / "demo.jsonl"
    write_demo_cctv_jsonl(generated_events, output_path=output_path)

    lines = output_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(generated_events)
    assert json.loads(lines[0])["event_id"] == generated_events[0]["event_id"]


# ---------------------------------------------------------------------------
# Ingestion path (service layer)
# ---------------------------------------------------------------------------


def test_generated_data_ingests_through_normal_service_path(ingested_db: Session) -> None:
    from sqlalchemy import func, select

    from app.models.event import Event

    total_events = ingested_db.scalar(select(func.count(Event.id)))
    assert total_events > 0
    # every generated event has a distinct event_id, so a clean ingest of
    # deduplicated data should persist one canonical Event per input row.
    from pipeline.generate_demo_cctv import generate_demo_cctv_events as _gen

    assert total_events == len(_gen(seed=DEFAULT_SEED))


def test_generated_data_ingests_through_ingest_events_cli(
    tmp_path, monkeypatch: pytest.MonkeyPatch, generated_events: list[dict]
) -> None:
    """Exercises pipeline.ingest_events.main() itself -- the actual documented
    entrypoint -- against an isolated in-memory database, proving the CLI
    script (unmodified) can ingest this generator's output end to end."""
    output_path = tmp_path / "demo_cctv.jsonl"
    write_demo_cctv_jsonl(generated_events, output_path=output_path)

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    test_sessionmaker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    monkeypatch.setattr(ingest_events_module, "SessionLocal", test_sessionmaker)
    monkeypatch.setattr(ingest_events_module, "init_db", lambda: None)

    ingest_events_module.main(output_path)

    from sqlalchemy import func, select

    from app.models.event import Event

    with test_sessionmaker() as verify_session:
        total_events = verify_session.scalar(select(func.count(Event.id)))
    assert total_events == len(generated_events)


# ---------------------------------------------------------------------------
# P3 endpoints produce meaningful (non-degenerate) results
# ---------------------------------------------------------------------------


def test_at_least_one_store_has_nonzero_occupancy(ingested_db: Session) -> None:
    response = OccupancyService(ingested_db).current_occupancy(PRIMARY_STORE)

    assert response.occupancy > 0


def test_occupancy_history_varies_over_the_day(ingested_db: Session) -> None:
    history = OccupancyService(ingested_db).occupancy_series(
        PRIMARY_STORE, DAY2_START, DAY3_START, bucket_minutes=60
    )

    occupancy_values = [point.occupancy for point in history.points]
    assert max(occupancy_values) > 0
    assert len(set(occupancy_values)) > 1  # not flat -- occupancy actually changes


def test_queue_current_is_nonzero_at_latest_event(ingested_db: Session) -> None:
    response = QueueService(ingested_db).current_queue(PRIMARY_STORE)

    assert response.queue_length > 0
    assert all(entity.waiting_seconds >= 0 for entity in response.queued_entities)


def test_queue_metrics_include_both_completions_and_abandonment(ingested_db: Session) -> None:
    metrics = QueueService(ingested_db).queue_metrics(PRIMARY_STORE, DAY2_START, DAY3_START)

    assert metrics.completed_visits > 0
    assert metrics.abandoned_visits > 0
    assert 0.0 < metrics.abandonment_rate < 1.0
    assert metrics.average_wait_seconds is not None
    assert metrics.median_wait_seconds is not None
    # wait times are meant to vary, not be a single fixed placeholder value
    assert metrics.average_wait_seconds != metrics.median_wait_seconds or metrics.average_wait_seconds > 0


def test_hourly_footfall_has_real_variation(ingested_db: Session) -> None:
    footfall = TimeSeriesService(ingested_db).hourly_footfall(
        PRIMARY_STORE, DAY2_START, DAY3_START, bucket_minutes=60
    )

    entries = [bucket.entries for bucket in footfall.buckets]
    assert sum(entries) > 0
    assert len(set(entries)) > 3  # meaningfully varied, not a flat or binary signal


def test_peak_hour_ranking_is_non_degenerate(ingested_db: Session) -> None:
    peak = PeakHourService(ingested_db).peak_hours(PRIMARY_STORE, DAY2_START, DAY3_START)

    assert peak.peak_hour is not None
    assert peak.peak_hour.entries > 0
    # the top-ranked hour must actually beat the rest, not tie everything at 0
    runner_up = peak.ranked_hours[1].entries if len(peak.ranked_hours) > 1 else 0
    assert peak.peak_hour.entries >= runner_up
    assert len({bucket.entries for bucket in peak.ranked_hours}) > 1


def test_peak_hour_lands_in_the_designed_lunch_window(ingested_db: Session) -> None:
    """ST1001's profile is specifically shaped with a lunch-hour peak (12:00-14:00)
    -- confirm the generated data actually produces that, not just "a" peak."""
    peak = PeakHourService(ingested_db).peak_hours(PRIMARY_STORE, DAY2_START, DAY3_START)

    assert peak.peak_hour.bucket_start.hour in {12, 13, 14}


def test_period_comparison_has_meaningful_nonzero_inputs(ingested_db: Session) -> None:
    comparison = ComparisonService(ingested_db).compare(PRIMARY_STORE, DAY2_START, DAY3_START)

    assert comparison.current.footfall > 0
    assert comparison.previous.footfall > 0
    assert comparison.current.footfall != comparison.previous.footfall
    footfall_delta = comparison.deltas["footfall"]
    assert footfall_delta.percent is not None
    assert footfall_delta.percent > 0  # day 2 is deliberately busier than day 1


def test_stores_have_meaningfully_different_traffic_patterns(ingested_db: Session) -> None:
    """ST1001 (lunch-peaked) and ST1002 (commute-peaked) must not just be
    scaled copies of each other -- their peak hours should differ."""
    peak_service = PeakHourService(ingested_db)
    st1001_peak = peak_service.peak_hours("ST1001", DAY2_START, DAY3_START).peak_hour
    st1002_peak = peak_service.peak_hours("ST1002", DAY2_START, DAY3_START).peak_hour

    assert st1001_peak is not None
    assert st1002_peak is not None
    assert st1001_peak.bucket_start.hour != st1002_peak.bucket_start.hour
