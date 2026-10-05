from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import PlainSerializer


def utc_isoformat(value: datetime) -> str:
    """Serialize a timestamp as an explicit-UTC ISO-8601 string.

    The database stores naive datetimes that are UTC by convention (see
    app.db.base.utcnow and occupancy_service.to_naive, which converts every
    timezone-aware input to naive UTC). Emitting them without an offset made
    browsers parse them as *local* time, so a client in IST read
    "10:01" as 10:01 IST, then sent "04:31Z" back as the end of its range --
    a 5.5 hour shift that emptied every Live Analytics chart. Marking the
    output as UTC lets any client convert it correctly for its own timezone.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# Python-side values stay plain datetimes (naive UTC, as stored); only the
# JSON representation gains its explicit UTC marker.
UtcDatetime = Annotated[datetime, PlainSerializer(utc_isoformat, return_type=str, when_used="json")]
