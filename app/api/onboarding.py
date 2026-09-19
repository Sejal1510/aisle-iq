from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import grant_store_access, require_store_role, require_user
from app.core.storage import (
    CAMERA_REFERENCE_ALLOWED_CONTENT_TYPES,
    CAMERA_REFERENCE_ALLOWED_EXTENSIONS,
    CAMERA_REFS_DIR,
    MAP_ALLOWED_CONTENT_TYPES,
    MAP_ALLOWED_EXTENSIONS,
    MAPS_DIR,
    VIDEO_ALLOWED_CONTENT_TYPES,
    VIDEO_ALLOWED_EXTENSIONS,
    VIDEOS_DIR,
    StorageError,
    UploadTooLargeError,
    resolve_relative_path,
    resolve_video_path,
    save_upload,
)
from app.db.session import get_db
from app.models.auth import User
from app.models.enums import Role
from app.models.spatial import GEOMETRY_KIND_LINE, GEOMETRY_KIND_POLYGON
from app.models.store import Camera, Store, Zone
from app.schemas.spatial import (
    CameraCreateRequest,
    CameraOut,
    CameraUpdateRequest,
    CoverageCreateRequest,
    CoverageOut,
    CreateStoreRequest,
    GeometryOut,
    MapOut,
    StoreOut,
    ZoneCreateRequest,
    ZoneOut,
    ZoneUpdateRequest,
)
from app.services.reference_data_service import ReferenceDataService
from app.services.store_config_service import (
    LineGeometry,
    PolygonGeometry,
    StoreConfigError,
    StoreConfigService,
    parse_geometry_json,
)

router = APIRouter()

_write_access = Depends(require_store_role(Role.ADMIN))
_read_access = Depends(require_store_role(Role.ANALYST))


def _run(callable_):
    try:
        return callable_()
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from None
    except (StoreConfigError, StorageError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# ----------------------------------------------------------------------
# Store creation. Not gated by require_store_role -- that dependency checks
# access to a store that, at this point, does not exist yet. Any
# authenticated user may create a store; doing so grants them ADMIN on it,
# the same "first user becomes the admin" pattern the README's bootstrap
# script already uses for the very first store. This is still the existing
# JWT/User/StoreAccess system, not a second auth mechanism -- see the P7
# audit's Authorization section.
# ----------------------------------------------------------------------
@router.post("/stores", response_model=StoreOut, status_code=201)
def create_store(
    payload: CreateStoreRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> StoreOut:
    if db.get(Store, payload.store_id) is not None:
        raise HTTPException(status_code=409, detail=f"Store '{payload.store_id}' already exists.")

    organization = ReferenceDataService(db).ensure_organization()
    store = Store(id=payload.store_id, organization_id=organization.id, name=payload.name)
    db.add(store)
    db.flush()
    grant_store_access(db, user.id, store.id, Role.ADMIN)
    db.commit()
    return StoreOut(store_id=store.id, name=store.name)


# ----------------------------------------------------------------------
# Maps
# ----------------------------------------------------------------------
@router.post("/stores/{store_id}/config/maps", response_model=MapOut, status_code=201)
def upload_map(
    store_id: str,
    file: UploadFile = File(...),
    name: str | None = Form(None),
    db: Session = Depends(get_db),
    access=_write_access,
) -> MapOut:
    _, relative_path = _run(
        lambda: save_upload(
            file,
            MAPS_DIR,
            subdir=store_id,
            max_bytes=get_settings().max_map_upload_bytes,
            allowed_extensions=MAP_ALLOWED_EXTENSIONS,
            allowed_content_types=MAP_ALLOWED_CONTENT_TYPES,
        )
    )
    map_row = _run(
        lambda: StoreConfigService(db).create_map(
            store_id,
            name=name,
            file_path=relative_path,
            content_type=file.content_type,
            width_px=None,
            height_px=None,
        )
    )
    db.commit()
    return _map_to_out(map_row)


@router.get("/stores/{store_id}/config/maps", response_model=list[MapOut])
def list_maps(store_id: str, db: Session = Depends(get_db), access=_read_access) -> list[MapOut]:
    return [_map_to_out(m) for m in StoreConfigService(db).list_maps(store_id)]


@router.get("/stores/{store_id}/config/maps/{map_id}/file")
def get_map_file(store_id: str, map_id: str, db: Session = Depends(get_db), access=_read_access) -> FileResponse:
    from app.models.spatial import Map as MapModel

    map_row = db.get(MapModel, map_id)
    if map_row is None or map_row.store_id != store_id:
        raise HTTPException(status_code=404, detail="Map not found.")
    path = resolve_relative_path(map_row.file_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Map file is missing on disk.")
    return FileResponse(path, media_type=map_row.content_type or "application/octet-stream")


# ----------------------------------------------------------------------
# Zones
# ----------------------------------------------------------------------
@router.post("/stores/{store_id}/config/zones", response_model=ZoneOut, status_code=201)
def create_zone(
    store_id: str, payload: ZoneCreateRequest, db: Session = Depends(get_db), access=_write_access
) -> ZoneOut:
    polygon = PolygonGeometry(points=tuple(payload.map_polygon)) if payload.map_polygon else None
    zone = _run(
        lambda: StoreConfigService(db).create_zone(
            store_id,
            payload.zone_id,
            name=payload.name,
            zone_type=payload.zone_type,
            is_revenue_zone=payload.is_revenue_zone,
            map_polygon=polygon,
        )
    )
    db.commit()
    return _zone_to_out(zone)


@router.patch("/stores/{store_id}/config/zones/{zone_id}", response_model=ZoneOut)
def update_zone(
    store_id: str,
    zone_id: str,
    payload: ZoneUpdateRequest,
    db: Session = Depends(get_db),
    access=_write_access,
) -> ZoneOut:
    polygon = PolygonGeometry(points=tuple(payload.map_polygon)) if payload.map_polygon else None
    zone = _run(
        lambda: StoreConfigService(db).update_zone(
            zone_id,
            name=payload.name,
            zone_type=payload.zone_type,
            is_revenue_zone=payload.is_revenue_zone,
            map_polygon=polygon,
        )
    )
    if zone.store_id != store_id:
        raise HTTPException(status_code=404, detail="Zone not found for this store.")
    db.commit()
    return _zone_to_out(zone)


@router.get("/stores/{store_id}/config/zones", response_model=list[ZoneOut])
def list_zones(store_id: str, db: Session = Depends(get_db), access=_read_access) -> list[ZoneOut]:
    return [_zone_to_out(z) for z in StoreConfigService(db).list_zones(store_id)]


# ----------------------------------------------------------------------
# Cameras
# ----------------------------------------------------------------------
@router.post("/stores/{store_id}/config/cameras", response_model=CameraOut, status_code=201)
def create_camera(
    store_id: str, payload: CameraCreateRequest, db: Session = Depends(get_db), access=_write_access
) -> CameraOut:
    def _apply() -> Camera:
        # video_path here is a plain string field (not routed through
        # save_upload -- see upload_camera_video above), so it must be
        # re-validated at this write boundary rather than trusted: see
        # resolve_video_path's docstring for why.
        if payload.video_path is not None:
            resolve_video_path(payload.video_path)
        return StoreConfigService(db).create_camera(
            store_id,
            payload.camera_id,
            name=payload.name,
            role=payload.role,
            video_path=payload.video_path,
            start_time=payload.start_time,
            sample_fps=payload.sample_fps,
            confidence_threshold=payload.confidence_threshold,
            queue_completion_seconds=payload.queue_completion_seconds,
            queue_abandonment_seconds=payload.queue_abandonment_seconds,
        )

    camera = _run(_apply)
    db.commit()
    return _camera_to_out(camera)


@router.patch("/stores/{store_id}/config/cameras/{camera_id}", response_model=CameraOut)
def update_camera(
    store_id: str,
    camera_id: str,
    payload: CameraUpdateRequest,
    db: Session = Depends(get_db),
    access=_write_access,
) -> CameraOut:
    fields = payload.model_dump()

    def _apply() -> Camera:
        if fields.get("video_path") is not None:
            resolve_video_path(fields["video_path"])
        return StoreConfigService(db).update_camera(camera_id, **fields)

    camera = _run(_apply)
    if camera.store_id != store_id:
        raise HTTPException(status_code=404, detail="Camera not found for this store.")
    db.commit()
    return _camera_to_out(camera)


@router.get("/stores/{store_id}/config/cameras", response_model=list[CameraOut])
def list_cameras(store_id: str, db: Session = Depends(get_db), access=_read_access) -> list[CameraOut]:
    return [_camera_to_out(c) for c in StoreConfigService(db).list_cameras(store_id)]


@router.post("/stores/{store_id}/config/cameras/{camera_id}/reference-image", response_model=CameraOut)
def upload_camera_reference_image(
    store_id: str,
    camera_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    access=_write_access,
) -> CameraOut:
    _, relative_path = _run(
        lambda: save_upload(
            file,
            CAMERA_REFS_DIR,
            subdir=store_id,
            max_bytes=get_settings().max_camera_reference_upload_bytes,
            allowed_extensions=CAMERA_REFERENCE_ALLOWED_EXTENSIONS,
            allowed_content_types=CAMERA_REFERENCE_ALLOWED_CONTENT_TYPES,
        )
    )
    camera = _run(lambda: StoreConfigService(db).update_camera(camera_id, reference_image_path=relative_path))
    if camera.store_id != store_id:
        raise HTTPException(status_code=404, detail="Camera not found for this store.")
    db.commit()
    return _camera_to_out(camera)


@router.get("/stores/{store_id}/config/cameras/{camera_id}/reference-image")
def get_camera_reference_image(
    store_id: str, camera_id: str, db: Session = Depends(get_db), access=_read_access
) -> FileResponse:
    camera = db.get(Camera, camera_id)
    if camera is None or camera.store_id != store_id or not camera.reference_image_path:
        raise HTTPException(status_code=404, detail="Reference image not found.")
    path = resolve_relative_path(camera.reference_image_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Reference image file is missing on disk.")
    return FileResponse(path)


# ----------------------------------------------------------------------
# P9: recorded video, uploaded per-camera as a processing input (not a
# playback/library asset -- see docs/CHOICES.md's P9 entry). ADMIN-gated,
# the same tier as every other camera config write -- this is structural
# setup, not the operational "start processing" action
# (app/api/video_processing.py, gated MANAGER).
# ----------------------------------------------------------------------
@router.post("/stores/{store_id}/config/cameras/{camera_id}/video", response_model=CameraOut, status_code=201)
def upload_camera_video(
    store_id: str,
    camera_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    access=_write_access,
) -> CameraOut:
    _, relative_path = _run(
        lambda: save_upload(
            file,
            VIDEOS_DIR,
            subdir=store_id,
            max_bytes=get_settings().max_video_upload_bytes,
            allowed_extensions=VIDEO_ALLOWED_EXTENSIONS,
            allowed_content_types=VIDEO_ALLOWED_CONTENT_TYPES,
        )
    )
    camera = _run(lambda: StoreConfigService(db).update_camera(camera_id, video_path=relative_path))
    if camera.store_id != store_id:
        raise HTTPException(status_code=404, detail="Camera not found for this store.")
    db.commit()
    return _camera_to_out(camera)


# ----------------------------------------------------------------------
# Camera coverage
# ----------------------------------------------------------------------
@router.post(
    "/stores/{store_id}/config/cameras/{camera_id}/coverage", response_model=CoverageOut, status_code=201
)
def create_coverage(
    store_id: str,
    camera_id: str,
    payload: CoverageCreateRequest,
    db: Session = Depends(get_db),
    access=_write_access,
) -> CoverageOut:
    camera = db.get(Camera, camera_id)
    if camera is None or camera.store_id != store_id:
        raise HTTPException(status_code=404, detail="Camera not found for this store.")

    geometry = payload.geometry
    domain_geometry = (
        PolygonGeometry(points=tuple(geometry.points))
        if geometry.kind == "polygon"
        else LineGeometry(
            axis=geometry.axis,
            position=geometry.position,
            inside_greater_than_position=geometry.inside_greater_than_position,
        )
    )
    coverage = _run(
        lambda: StoreConfigService(db).set_coverage(camera_id, zone_id=payload.zone_id, geometry=domain_geometry)
    )
    db.commit()
    return _coverage_to_out(coverage)


@router.get("/stores/{store_id}/config/cameras/{camera_id}/coverage", response_model=list[CoverageOut])
def list_coverage(
    store_id: str, camera_id: str, db: Session = Depends(get_db), access=_read_access
) -> list[CoverageOut]:
    camera = db.get(Camera, camera_id)
    if camera is None or camera.store_id != store_id:
        raise HTTPException(status_code=404, detail="Camera not found for this store.")
    return [_coverage_to_out(c) for c in StoreConfigService(db).list_coverage(camera_id)]


@router.delete("/stores/{store_id}/config/cameras/{camera_id}/coverage/{coverage_id}", status_code=204)
def delete_coverage(
    store_id: str,
    camera_id: str,
    coverage_id: str,
    db: Session = Depends(get_db),
    access=_write_access,
) -> None:
    _run(lambda: StoreConfigService(db).delete_coverage(coverage_id))
    db.commit()


# ----------------------------------------------------------------------
# Response builders
# ----------------------------------------------------------------------
def _map_to_out(map_row) -> MapOut:
    return MapOut(
        map_id=map_row.id,
        store_id=map_row.store_id,
        name=map_row.name,
        content_type=map_row.content_type,
        width_px=map_row.width_px,
        height_px=map_row.height_px,
        is_active=map_row.is_active,
        created_at=map_row.created_at,
        file_url=f"/stores/{map_row.store_id}/config/maps/{map_row.id}/file",
    )


def _zone_to_out(zone: Zone) -> ZoneOut:
    polygon = None
    if zone.map_polygon_json:
        geometry = parse_geometry_json(GEOMETRY_KIND_POLYGON, zone.map_polygon_json)
        polygon = list(geometry.points)
    return ZoneOut(
        zone_id=zone.id,
        store_id=zone.store_id,
        name=zone.name,
        zone_type=zone.type,
        is_revenue_zone=zone.is_revenue_zone,
        map_polygon=polygon,
    )


def _camera_to_out(camera: Camera) -> CameraOut:
    return CameraOut(
        camera_id=camera.id,
        store_id=camera.store_id,
        name=camera.name,
        role=camera.role,
        video_path=camera.video_path,
        reference_image_path=camera.reference_image_path,
        reference_image_url=(
            f"/stores/{camera.store_id}/config/cameras/{camera.id}/reference-image"
            if camera.reference_image_path
            else None
        ),
        start_time=camera.start_time,
        sample_fps=camera.sample_fps,
        confidence_threshold=camera.confidence_threshold,
        queue_completion_seconds=camera.queue_completion_seconds,
        queue_abandonment_seconds=camera.queue_abandonment_seconds,
    )


def _coverage_to_out(coverage) -> CoverageOut:
    geometry = parse_geometry_json(coverage.geometry_kind, coverage.geometry_json)
    if coverage.geometry_kind == GEOMETRY_KIND_POLYGON:
        geometry_out = GeometryOut(kind=GEOMETRY_KIND_POLYGON, points=list(geometry.points))
    else:
        geometry_out = GeometryOut(
            kind=GEOMETRY_KIND_LINE,
            axis=geometry.axis,
            position=geometry.position,
            inside_greater_than_position=geometry.inside_greater_than_position,
        )
    return CoverageOut(
        coverage_id=coverage.id,
        camera_id=coverage.camera_id,
        zone_id=coverage.zone_id,
        geometry=geometry_out,
    )
