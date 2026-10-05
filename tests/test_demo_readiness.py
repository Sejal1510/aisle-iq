"""Demo-readiness fixes: UTC-marked API timestamps, the accessible-store
list, the annotated-video endpoint, exit debounce / end-of-video zone
closing in the event generator, and end-of-video session finalization with
counter-staff detection."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.video_processing as video_api
import app.main as main_module
from app.core.security import create_access_token, create_user, grant_store_access
from app.db.session import get_db
from app.main import app
from app.models import Base
from app.models.enums import Role, SessionStatus
from app.models.store import Camera, Store
from app.models.tracking import IdentityAlias, TrackedEntity, VisitSession
from app.models.video_processing import VideoProcessingJob, VideoProcessingStatus
from app.schemas.common import utc_isoformat
from app.services.video_run_finalizer import VideoRunFinalizer
from pipeline.video.annotation import (
    AnnotatedVideoRenderer,
    AnnotationRequest,
    RunStats,
    annotated_output_paths,
    interpolate_snapshots,
    write_run_summary,
)
from pipeline.video.config import CameraRole, Point, PolygonZone, VideoProcessingConfig
from pipeline.video.events import VideoEventGenerator
from pipeline.video.processing import process_camera
from pipeline.video.tracking import TrackSnapshot

BASE_TIME = datetime(2026, 6, 1, 10, 0, 0)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def test_sessionmaker():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture()
def db_session(test_sessionmaker) -> Session:
    session = test_sessionmaker()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(test_sessionmaker, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def override_get_db():
        db = test_sessionmaker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(main_module, "SessionLocal", test_sessionmaker)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _auth_header(db_session: Session, *, email: str, grants: dict[str, Role]) -> dict:
    for store_id in grants:
        if db_session.get(Store, store_id) is None:
            db_session.add(Store(id=store_id, name=f"Store {store_id}"))
    db_session.commit()
    user = create_user(db_session, email=email, raw_password="pw")
    db_session.commit()
    for store_id, role in grants.items():
        grant_store_access(db_session, user.id, store_id, role)
    db_session.commit()
    token, _ = create_access_token(user.id)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# UTC serialization
# ---------------------------------------------------------------------------
def test_utc_isoformat_marks_naive_utc_timestamps_explicitly() -> None:
    assert utc_isoformat(datetime(2026, 6, 1, 10, 1, 53)) == "2026-06-01T10:01:53Z"


def test_live_analytics_timestamps_are_emitted_as_utc(client: TestClient, db_session: Session) -> None:
    headers = _auth_header(db_session, email="a@x.io", grants={"ST_UTC": Role.ANALYST})

    body = client.get(
        "/stores/ST_UTC/queue/metrics",
        params={"start": "2026-06-01T00:00:00Z", "end": "2026-06-02T00:00:00Z"},
        headers=headers,
    ).json()

    assert body["start"] == "2026-06-01T00:00:00Z"
    assert body["end"] == "2026-06-02T00:00:00Z"


# ---------------------------------------------------------------------------
# accessible stores
# ---------------------------------------------------------------------------
def test_store_list_only_returns_stores_the_caller_can_access(client: TestClient, db_session: Session) -> None:
    db_session.add(Store(id="ST_OTHER", name="Not yours"))
    db_session.commit()
    headers = _auth_header(db_session, email="b@x.io", grants={"ST_A": Role.ADMIN, "ST_B": Role.ANALYST})

    body = client.get("/stores", headers=headers).json()

    assert [store["store_id"] for store in body] == ["ST_A", "ST_B"]
    assert body[0]["name"] == "Store ST_A"
    assert body[0]["event_count"] == 0
    assert body[0]["last_event_timestamp"] is None


def test_store_rename_requires_admin(client: TestClient, db_session: Session) -> None:
    admin = _auth_header(db_session, email="c@x.io", grants={"ST_R": Role.ADMIN})
    analyst = _auth_header(db_session, email="d@x.io", grants={"ST_R": Role.ANALYST})

    assert client.patch("/stores/ST_R", json={"name": "Flagship"}, headers=analyst).status_code == 403
    response = client.patch("/stores/ST_R", json={"name": "Flagship"}, headers=admin)
    assert response.status_code == 200
    assert response.json()["name"] == "Flagship"


# ---------------------------------------------------------------------------
# annotated video endpoint
# ---------------------------------------------------------------------------
def _seed_job(db_session: Session, store_id: str) -> VideoProcessingJob:
    db_session.add(Camera(id="CAM_V", store_id=store_id, role="entry", start_time=BASE_TIME))
    job = VideoProcessingJob(
        store_id=store_id, camera_id="CAM_V", video_path="data/videos/x.mp4", status=VideoProcessingStatus.COMPLETED
    )
    db_session.add(job)
    db_session.commit()
    return job


def test_annotated_video_is_served_to_store_members_only(
    client: TestClient, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(video_api, "VIDEOS_DIR", tmp_path)
    headers = _auth_header(db_session, email="e@x.io", grants={"ST_V": Role.ANALYST})
    outsider = _auth_header(db_session, email="f@x.io", grants={"ST_ELSE": Role.ADMIN})
    job = _seed_job(db_session, "ST_V")
    url = f"/stores/ST_V/video-processing/{job.id}/annotated-video"

    assert client.get(url, headers=headers).status_code == 404  # nothing rendered yet
    assert client.get(f"/stores/ST_V/video-processing/{job.id}", headers=headers).json()[
        "annotated_video_available"
    ] is False

    stem, summary_path = annotated_output_paths(tmp_path, "ST_V", job.id)
    stem.parent.mkdir(parents=True)
    stem.with_suffix(".mp4").write_bytes(b"fake-mp4")
    stats = RunStats(frames_sampled=3, person_detections=4, max_people_in_frame=2)
    stats.track_last_seen = {"CAM_V:1": BASE_TIME, "CAM_V:2": BASE_TIME}
    stats.annotated_file = stem.with_suffix(".mp4").name
    stats.annotated_content_type = "video/mp4"
    write_run_summary(summary_path, stats)

    response = client.get(url, headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.content == b"fake-mp4"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=outsider).status_code == 403

    job_body = client.get(f"/stores/ST_V/video-processing/{job.id}", headers=headers).json()
    assert job_body["annotated_video_available"] is True
    assert job_body["run_summary"]["person_detections"] == 4
    assert job_body["run_summary"]["unique_tracks"] == 2


def test_job_creation_for_a_path_outside_video_storage_is_a_400_not_a_500(
    client: TestClient, db_session: Session
) -> None:
    headers = _auth_header(db_session, email="g@x.io", grants={"ST_P": Role.MANAGER})
    db_session.add(
        Camera(id="CAM_P", store_id="ST_P", role="entry", start_time=BASE_TIME, video_path="data/Store 2/entry 1.mp4")
    )
    db_session.commit()

    response = client.post("/stores/ST_P/video-processing", json={"camera_id": "CAM_P"}, headers=headers)

    assert response.status_code == 400
    assert "video storage directory" in response.json()["detail"]


# ---------------------------------------------------------------------------
# event generator: exit debounce + end-of-video zone closing
# ---------------------------------------------------------------------------
_ZONE = PolygonZone(
    id="Z1", name="Zone", type="SHELF", is_revenue_zone=True,
    polygon=(Point(0.25, 0.25), Point(0.75, 0.25), Point(0.75, 0.75), Point(0.25, 0.75)),
)


def _snap(seconds: float, footpoint: tuple[float, float], *, frame: int = 0, track_id: str = "1") -> TrackSnapshot:
    x, y = footpoint[0] * 100, footpoint[1] * 100
    return TrackSnapshot(
        track_id=track_id, store_id="ST", camera_id="CAM", frame_index=frame,
        timestamp=BASE_TIME + timedelta(seconds=seconds), bbox_xyxy=(x - 5, y - 20, x + 5, y),
        confidence=0.9, footpoint=(x, y), frame_size=(100, 100),
    )


def _config(role: CameraRole, **overrides) -> VideoProcessingConfig:
    values = {
        "store_id": "ST", "camera_id": "CAM", "role": role, "video_path": Path("v.mp4"),
        "start_time": BASE_TIME, "zones": (_ZONE,), "exit_grace_seconds": 2.0,
    }
    values.update(overrides)
    return VideoProcessingConfig(**values)


def test_zone_edge_jitter_within_the_grace_period_is_one_visit() -> None:
    generator = VideoEventGenerator(_config(CameraRole.ZONE))
    events: list[dict] = []
    for frame, (seconds, point) in enumerate(
        [(0, (0.5, 0.5)), (0.5, (0.8, 0.5)), (1.0, (0.5, 0.5)), (1.5, (0.8, 0.5)), (2.0, (0.5, 0.5))]
    ):
        events += generator.process_snapshot(_snap(seconds, point, frame=frame))

    assert [event["event_type"] for event in events] == ["ZONE_ENTER"]


def test_zone_exit_is_confirmed_after_the_grace_period_at_the_first_outside_time() -> None:
    generator = VideoEventGenerator(_config(CameraRole.ZONE))
    events = generator.process_snapshot(_snap(0, (0.5, 0.5), frame=0))
    events += generator.process_snapshot(_snap(10, (0.9, 0.9), frame=1))
    events += generator.process_snapshot(_snap(12, (0.9, 0.9), frame=2))

    assert [event["event_type"] for event in events] == ["ZONE_ENTER", "ZONE_EXIT"]
    assert events[1]["timestamp"] == (BASE_TIME + timedelta(seconds=10)).isoformat()


def test_finalize_closes_a_zone_visit_at_the_last_observed_time() -> None:
    generator = VideoEventGenerator(_config(CameraRole.ZONE))
    generator.process_snapshot(_snap(0, (0.5, 0.5), frame=0))
    generator.process_snapshot(_snap(30, (0.5, 0.5), frame=1))

    closing = generator.finalize()

    assert [event["event_type"] for event in closing] == ["ZONE_EXIT"]
    assert closing[0]["timestamp"] == (BASE_TIME + timedelta(seconds=30)).isoformat()
    assert generator.finalize() == []


def test_billing_edge_jitter_does_not_emit_repeated_queue_joins() -> None:
    generator = VideoEventGenerator(
        _config(CameraRole.BILLING, queue_zone_id="Z1", queue_abandonment_seconds=5, queue_completion_seconds=30)
    )
    events: list[dict] = []
    for frame, seconds in enumerate(range(0, 20)):
        point = (0.5, 0.5) if seconds % 2 == 0 else (0.8, 0.5)
        events += generator.process_snapshot(_snap(seconds * 0.5, point, frame=frame))

    assert [event["event_type"] for event in events] == ["BILLING_QUEUE_JOIN"]


# ---------------------------------------------------------------------------
# end-of-video finalization
# ---------------------------------------------------------------------------
def _seed_open_session(db_session: Session, identity: str, entry: datetime) -> TrackedEntity:
    if db_session.get(Store, "ST_F") is None:
        db_session.add(Store(id="ST_F", name=None))
    entity = TrackedEntity(store_id="ST_F", is_staff=False)
    db_session.add(entity)
    db_session.flush()
    db_session.add(
        IdentityAlias(
            tracked_entity_id=entity.id, store_id="ST_F", camera_id="CAM_F", source_field="visitor_id",
            source_value=identity, first_seen_at=entry, last_seen_at=entry,
        )
    )
    db_session.add(
        VisitSession(tracked_entity_id=entity.id, store_id="ST_F", entry_time=entry, session_status=SessionStatus.IN_PROGRESS)
    )
    db_session.commit()
    return entity


def test_finalizer_closes_open_sessions_at_last_seen(db_session: Session) -> None:
    shopper = _seed_open_session(db_session, "CAM_F:2", BASE_TIME + timedelta(seconds=40))

    result = VideoRunFinalizer(db_session).finalize(
        store_id="ST_F",
        camera_id="CAM_F",
        track_last_seen={"CAM_F:2": BASE_TIME + timedelta(seconds=70)},
        track_frames={"CAM_F:2": 60},
        track_staff_area_frames={},
    )
    db_session.commit()

    assert result.sessions_closed == 1
    assert result.staff_tracks == ()
    session = db_session.query(VisitSession).filter_by(tracked_entity_id=shopper.id).one()
    assert session.session_status == SessionStatus.COMPLETED
    assert session.dwell_seconds == 30


def test_staff_requires_most_sightings_inside_the_configured_staff_area(db_session: Session) -> None:
    cashier = _seed_open_session(db_session, "CAM_F:1", BASE_TIME)
    long_waiting_customer = _seed_open_session(db_session, "CAM_F:2", BASE_TIME)
    reached_across = _seed_open_session(db_session, "CAM_F:3", BASE_TIME)

    result = VideoRunFinalizer(db_session).finalize(
        store_id="ST_F",
        camera_id="CAM_F",
        track_last_seen={key: BASE_TIME + timedelta(seconds=110) for key in ("CAM_F:1", "CAM_F:2", "CAM_F:3")},
        # The customer stayed the whole clip but never stood in the staff
        # area; the third person stepped into it for a few frames only.
        track_frames={"CAM_F:1": 200, "CAM_F:2": 220, "CAM_F:3": 40},
        track_staff_area_frames={"CAM_F:1": 180, "CAM_F:3": 4},
    )

    assert result.staff_tracks == ("CAM_F:1",)
    assert cashier.is_staff is True
    assert "staff area" in cashier.staff_inference_reason
    assert long_waiting_customer.is_staff is False
    assert reached_across.is_staff is False


def test_a_short_track_inside_the_staff_area_is_not_enough_evidence(db_session: Session) -> None:
    entity = _seed_open_session(db_session, "CAM_F:9", BASE_TIME)

    result = VideoRunFinalizer(db_session).finalize(
        store_id="ST_F",
        camera_id="CAM_F",
        track_last_seen={"CAM_F:9": BASE_TIME + timedelta(seconds=3)},
        track_frames={"CAM_F:9": 6},
        track_staff_area_frames={"CAM_F:9": 6},  # 100% inside, but only 3 s of evidence
    )

    assert result.staff_tracks == ()
    assert entity.is_staff is False


def test_staff_track_without_any_event_is_reported_but_creates_no_entity(db_session: Session) -> None:
    result = VideoRunFinalizer(db_session).finalize(
        store_id="ST_F",
        camera_id="CAM_F",
        track_last_seen={"CAM_F:14": BASE_TIME},
        track_frames={"CAM_F:14": 90},
        track_staff_area_frames={"CAM_F:14": 85},
    )

    assert result.staff_tracks == ("CAM_F:14",)
    assert db_session.query(TrackedEntity).count() == 0


_STAFF_AREA = PolygonZone(
    id="STAFF", name="Behind till", type="STAFF_AREA", is_revenue_zone=False,
    polygon=(Point(0.0, 0.6), Point(1.0, 0.6), Point(1.0, 1.0), Point(0.0, 1.0)),
)


def test_staff_areas_never_produce_zone_visits_or_become_the_queue() -> None:
    zone_generator = VideoEventGenerator(_config(CameraRole.ZONE, zones=(_STAFF_AREA, _ZONE)))
    billing_generator = VideoEventGenerator(_config(CameraRole.BILLING, zones=(_STAFF_AREA, _ZONE)))

    staff_snapshot = _snap(0, (0.5, 0.8))
    zone_events = zone_generator.process_snapshot(staff_snapshot) + zone_generator.finalize()
    billing_events = billing_generator.process_snapshot(staff_snapshot) + billing_generator.finalize()

    assert zone_events == []
    assert billing_events == []
    assert billing_generator._queue_zone() is _ZONE


def test_process_camera_counts_sightings_inside_staff_areas() -> None:
    class _FakeTracker:
        def track_video(self, config, max_frames=None, frame_callback=None):
            for index, footpoint in enumerate([(0.5, 0.8), (0.5, 0.9), (0.5, 0.3)]):
                snapshot = _snap(index * 0.5, footpoint, frame=index)
                yield snapshot
                if frame_callback is not None:
                    frame_callback(None, [snapshot], snapshot.timestamp, index)

    request = AnnotationRequest(output_stem=Path("unused"))
    config = _config(CameraRole.BILLING, zones=(_STAFF_AREA, _ZONE), annotation=request)

    list(process_camera(config, _FakeTracker()))

    assert request.stats.track_frames == {"CAM:1": 3}
    assert request.stats.track_staff_area_frames == {"CAM:1": 2}


def test_run_summary_round_trips(tmp_path: Path) -> None:
    stats = RunStats()
    stats.observe([_snap(0, (0.5, 0.5)), _snap(0, (0.4, 0.4), track_id="2")])
    stats.observe([_snap(0.5, (0.5, 0.5))])
    path = tmp_path / "s.json"

    write_run_summary(path, stats)
    summary = json.loads(path.read_text(encoding="utf-8"))

    assert summary["frames_sampled"] == 2
    assert summary["person_detections"] == 3
    assert summary["unique_tracks"] == 2
    assert summary["max_people_in_frame"] == 2


# ---------------------------------------------------------------------------
# annotated video rendering (display only)
# ---------------------------------------------------------------------------
def test_interpolation_moves_shared_tracks_and_holds_one_sided_tracks_to_the_nearest_half() -> None:
    before = [_snap(0, (0.2, 0.5), track_id="1"), _snap(0, (0.8, 0.5), track_id="2")]
    after = [_snap(0.5, (0.4, 0.5), track_id="1"), _snap(0.5, (0.6, 0.6), track_id="3")]

    early = interpolate_snapshots(before, after, 0.25)
    late = interpolate_snapshots(before, after, 0.75)

    assert sorted(s.track_id for s in early) == ["1", "2"]
    assert sorted(s.track_id for s in late) == ["1", "3"]
    moved = next(s for s in early if s.track_id == "1")
    assert moved.bbox_xyxy[0] == pytest.approx(15 + (35 - 15) * 0.25)


def _synthetic_video(path: Path, frames: int, fps: float = 10.0) -> None:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (100, 100))
    for index in range(frames):
        writer.write(np.full((100, 100, 3), index * 10 % 255, np.uint8))
    writer.release()


def test_renderer_writes_every_source_frame_while_detections_stay_at_the_sampled_rate(tmp_path: Path) -> None:
    cv2 = pytest.importorskip("cv2")
    source = tmp_path / "source.mp4"
    _synthetic_video(source, frames=9)
    request = AnnotationRequest(output_stem=tmp_path / "out" / "job")
    renderer = AnnotatedVideoRenderer(request, fps=2.5, source_path=source, source_fps=10.0)

    for frame_index in (0, 4, 8):  # stride 4: only these frames were analysed
        renderer.on_frame(None, [_snap(frame_index / 10, (0.5, 0.5), frame=frame_index)], BASE_TIME, frame_index)
    renderer.close()

    assert request.stats.annotation_error is None
    assert request.stats.frames_sampled == 3  # analytics stats count analysed frames only
    output = cv2.VideoCapture(str(tmp_path / "out" / request.stats.annotated_file))
    assert int(output.get(cv2.CAP_PROP_FRAME_COUNT)) == 9
    assert output.get(cv2.CAP_PROP_FPS) == pytest.approx(10.0)
    output.release()


def test_renderer_falls_back_to_the_sampled_rate_when_the_source_cannot_be_reopened(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    request = AnnotationRequest(output_stem=tmp_path / "out" / "job")
    renderer = AnnotatedVideoRenderer(request, fps=2.0, source_path=tmp_path / "missing.mp4", source_fps=25.0)
    frame = np.zeros((100, 100, 3), np.uint8)

    renderer.on_frame(frame, [_snap(0, (0.5, 0.5))], BASE_TIME, 0)
    renderer.close()

    assert renderer.fps == 2.0
    assert request.stats.annotated_file is not None
