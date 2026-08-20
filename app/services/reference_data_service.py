from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.models.enums import ZoneType
from app.models.store import Camera, Organization, Store, Zone


DEFAULT_ORGANIZATION_ID = "default"
DEFAULT_ORGANIZATION_NAME = "Default Organization"


class ReferenceDataService:
    """Get-or-create helpers for the Organization -> Store -> Camera/Zone
    hierarchy.

    CCTV and POS ingestion reference stores/cameras/zones by id before any
    admin workflow has necessarily created them, and those ids are real
    foreign keys as of this phase -- ingestion is responsible for
    provisioning a minimal row the first time an id is seen, the same way
    PosIngestionService already auto-created Store rows before this phase.
    Once real store/camera/zone configuration exists (a later, dashboard-facing
    phase), this stays a safe no-op for ids that are already properly
    configured; it never overwrites an existing row's fields.
    """

    def __init__(self, db: Session):
        self.db = db

    def ensure_organization(self, organization_id: str = DEFAULT_ORGANIZATION_ID) -> Organization:
        organization = self.db.get(Organization, organization_id)
        if organization is None:
            organization = Organization(id=organization_id, name=DEFAULT_ORGANIZATION_NAME)
            self.db.add(organization)
            self.db.flush()
        return organization

    def ensure_store(self, store_id: str) -> Store:
        store = self.db.get(Store, store_id)
        if store is None:
            organization = self.ensure_organization()
            store = Store(id=store_id, organization_id=organization.id, name=None)
            self.db.add(store)
            self.db.flush()
        return store

    def ensure_camera(self, store_id: str, camera_id: Optional[str], *, role: Optional[str] = None) -> Optional[Camera]:
        if camera_id is None:
            return None
        camera = self.db.get(Camera, camera_id)
        if camera is None:
            self.ensure_store(store_id)
            camera = Camera(id=camera_id, store_id=store_id, role=role)
            self.db.add(camera)
            self.db.flush()
        return camera

    def ensure_zone(
        self,
        store_id: str,
        zone_id: Optional[str],
        *,
        name: Optional[str] = None,
        zone_type: Optional[str] = None,
        is_revenue_zone: Optional[bool] = None,
    ) -> Optional[Zone]:
        if zone_id is None:
            return None
        zone = self.db.get(Zone, zone_id)
        if zone is None:
            self.ensure_store(store_id)
            zone = Zone(
                id=zone_id,
                store_id=store_id,
                name=name or zone_id,
                type=_parse_zone_type(zone_type),
                is_revenue_zone=bool(is_revenue_zone) if is_revenue_zone is not None else False,
            )
            self.db.add(zone)
            self.db.flush()
        return zone


def _parse_zone_type(value: Optional[str]) -> ZoneType:
    if value is None:
        return ZoneType.OTHER
    try:
        return ZoneType(value)
    except ValueError:
        # The video pipeline's zone type strings (e.g. "BILLING") don't fully
        # overlap with the ZoneType enum (which has CHECKOUT, not BILLING) --
        # this was never exercised before because Zone rows were never
        # auto-created from ingested events. Fall back to OTHER rather than
        # raising, since an unrecognized zone type is a labeling mismatch, not
        # a reason to fail ingestion.
        return ZoneType.OTHER
