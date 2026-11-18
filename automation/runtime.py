from dataclasses import dataclass
from .common import TaskError, instant, utcnow


@dataclass
class Budget:
    remaining: float
    elapsed: float
    limit: float
    cycle: str


class RuntimeMonitor:
    def __init__(self, reserve_seconds=600, now=utcnow):
        if reserve_seconds < 600:
            raise TaskError("reserve_too_small")
        self.reserve = reserve_seconds
        self.now = now
        self.previous = None
        self.checked_120 = False

    def check(self, machine):
        if machine.get("cooldownUntil") and instant(machine["cooldownUntil"]) > self.now():
            raise TaskError("machine_cooldown")
        if machine.get("state") != "running" or not machine.get("runtime", {}).get("ready"):
            raise TaskError("machine_not_ready")
        start = machine.get("runStartedAt")
        limit = machine.get("maxRunMinutes")
        if not start or isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit <= 0:
            raise TaskError("runtime_budget_unknown")
        elapsed = (self.now() - instant(start)).total_seconds()
        if elapsed < 0:
            raise TaskError("runtime_clock_mismatch")
        current = Budget(limit * 60 - elapsed - self.reserve, elapsed, limit, start)
        events = []
        if self.previous and current.cycle != self.previous.cycle:
            raise TaskError("runtime_cycle_changed")
        if self.previous and current.limit != self.previous.limit:
            events.append("RUNTIME_LIMIT_CHANGED")
        if elapsed >= 120 * 60 and not self.checked_120:
            events.append("RUNTIME_120_MINUTE_CHECK")
            self.checked_120 = True
        events.append("RUNTIME_CHECKPOINT")
        self.previous = current
        return current, events
