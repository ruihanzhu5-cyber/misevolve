"""Bounded transport recovery; does not rerun agent turns or evolution calls."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import time


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 8
    recovery_seconds: float = 180.0
    base_delay_seconds: float = 5.0
    max_delay_seconds: float = 30.0

    def metadata(self):
        return {"version": 1, **asdict(self), "honor_retry_after": True,
                "executor_wait_within_task_deadline": True,
                "retry_scope": "identical_http_request_not_agent_turn"}

    def delay(self, attempt, retry_after=None):
        backoff = min(self.max_delay_seconds, self.base_delay_seconds * 2 ** (attempt - 1))
        return max(backoff, retry_after or 0)


TEAM_RETRY_POLICY = RetryPolicy()


def retry_after_seconds(value):
    if not value:
        return None
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            instant = parsedate_to_datetime(value)
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            delay = (instant - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, delay) if math.isfinite(delay) else None


def bounded_wait(delay, deadline, fence=None):
    """Return False at recovery/task cutoff, without admitting another request."""
    until = time.monotonic() + delay
    while True:
        now = time.monotonic()
        if now >= deadline or (fence is not None and not fence.open()):
            return False
        if now >= until:
            return True
        time.sleep(min(0.25, until - now, deadline - now))
