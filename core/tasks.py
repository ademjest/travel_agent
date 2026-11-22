from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


TASK_TTL = timedelta(minutes=30)
READ_TASK_TYPES = {'weather', 'forecast', 'route', 'traffic', 'places', 'walking', 'transit', 'rail', 'hotel'}
ROUTE_TASK_TYPES = {'route','traffic','walking','transit'}


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    task_type: str
    status: str
    initial_request: str
    slots: dict
    missing_slots: tuple[str, ...]
    version: int
    expires_at: str
    last_event_id: str
    reference_message_id: str = ''

    def is_expired(self, now: datetime) -> bool:
        # Legacy records may omit the UTC offset used by task storage.
        deadline = datetime.fromisoformat(self.expires_at)
        deadline = deadline.replace(tzinfo=deadline.tzinfo or timezone.utc)
        return deadline <= now.replace(tzinfo=now.tzinfo or timezone.utc)


@dataclass(frozen=True)
class TaskUpdate:
    task_type: str
    status: str
    initial_request: str
    slots: dict
    missing_slots: tuple[str, ...] = ()
    task_id: str = ""
    expected_version: int | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
