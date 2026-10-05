from __future__ import annotations

from sqlalchemy import ColumnElement, or_, select

from app.models.event import Event
from app.models.tracking import TrackedEntity


def customer_events() -> ColumnElement[bool]:
    """WHERE clause keeping only events from people not flagged as staff.

    Footfall and visitor metrics already excluded staff; queue metrics did
    not, so a cashier standing inside the billing-queue polygon was counted
    as a customer queue visit. Every queue query uses this one definition so
    Overview, Funnel, Live Analytics and Insights agree. An event with no
    tracked entity (legacy aggregate rows) is kept, as before.
    """
    staff_entity_ids = select(TrackedEntity.id).where(TrackedEntity.is_staff.is_(True))
    return or_(Event.tracked_entity_id.is_(None), Event.tracked_entity_id.not_in(staff_entity_ids))
