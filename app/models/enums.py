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
