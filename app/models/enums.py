import enum

class EventType(str, enum.Enum):
    ENTRY = "entry"
    EXIT = "exit"
    ZONE_ENTERED = "zone_entered"
    ZONE_EXITED = "zone_exited"

class SessionStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ORPHANED = "orphaned"

class ZoneType(str, enum.Enum):
    SHELF = "SHELF"
    DISPLAY = "DISPLAY"
    CHECKOUT = "CHECKOUT"
    ENTRY_DOOR = "ENTRY_DOOR"
    OTHER = "OTHER"
