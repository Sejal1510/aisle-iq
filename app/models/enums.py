import enum


class EventType(str, enum.Enum):
    ENTRY = "entry"
    EXIT = "exit"
    ZONE_ENTERED = "zone_entered"
    ZONE_EXITED = "zone_exited"
    ZONE_DWELL = "zone_dwell"
    BILLING_QUEUE_JOIN = "billing_queue_join"
    QUEUE_COMPLETED = "queue_completed"
    QUEUE_ABANDONED = "queue_abandoned"
    REENTRY = "reentry"

class SessionStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ORPHANED = "orphaned"

class CorrelationStatus(str, enum.Enum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"

class ZoneType(str, enum.Enum):
    SHELF = "SHELF"
    DISPLAY = "DISPLAY"
    CHECKOUT = "CHECKOUT"
    ENTRY_DOOR = "ENTRY_DOOR"
    OTHER = "OTHER"
    # Operator-drawn area where only staff stand (e.g. behind a till). A
    # camera-frame coverage polygon of this type is what classifies a track
    # as staff -- see app.services.video_run_finalizer. Never a shopping
    # zone: no zone-visit events are generated for it.
    STAFF_AREA = "STAFF_AREA"

class Role(str, enum.Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    ANALYST = "analyst"

class IdentityLinkReason(str, enum.Enum):
    """Why IdentityLinkingService.evaluate_candidates did or did not accept a
    pairwise cross-camera candidate. See app.models.identity_linking.
    IdentityLinkCandidate -- a row always has exactly one of these, including
    accepted rows, so the evidence is equally explainable either way."""

    ACCEPTED = "accepted"
    AMBIGUOUS = "ambiguous"
    BELOW_THRESHOLD = "below_threshold"
