"""Daily free-tier guard.

Deterministic rules, enforced before any request leaves the machine:
  * usage today = max(local ledger, Cloudflare-reported usage when available)
  * a request starts only if usage + its conservative estimate <= daily cap
  * once the provider reports the allocation exhausted (3036) nothing else
    runs until the next 00:00 UTC reset
The ledger lives in the workspace, so progress and spend survive restarts.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from weaver.state import atomic_write_json


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def day_key(now: datetime | None = None) -> str:
    return (now or utc_now()).strftime("%Y-%m-%d")


def next_reset(now: datetime | None = None) -> datetime:
    now = now or utc_now()
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


class BudgetExceeded(Exception):
    """Starting the next request could exceed today's free allocation."""


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"days": {}}

    def _day(self, now: datetime | None = None) -> dict:
        return self.data["days"].setdefault(day_key(now), {"neurons": 0.0, "requests": 0, "exhausted_at": None})

    def used_today(self, now: datetime | None = None) -> float:
        return float(self.data["days"].get(day_key(now), {}).get("neurons", 0.0))

    def exhausted_today(self, now: datetime | None = None) -> bool:
        return bool(self.data["days"].get(day_key(now), {}).get("exhausted_at"))

    def record(self, neurons: float, now: datetime | None = None) -> None:
        day = self._day(now)
        day["neurons"] = round(day["neurons"] + neurons, 2)
        day["requests"] += 1
        self.save()

    def mark_exhausted(self, now: datetime | None = None) -> None:
        self._day(now)["exhausted_at"] = (now or utc_now()).isoformat()
        self.save()

    def save(self) -> None:
        atomic_write_json(self.path, self.data)


@dataclass
class QuotaGuard:
    ledger: Ledger
    daily_cap: float
    provider_reported: float | None = None

    def __post_init__(self) -> None:
        # Local spend already counted when the provider's figure was read.
        self._local_at_report = self.ledger.used_today()

    def used(self, now: datetime | None = None) -> float:
        local = self.ledger.used_today(now)
        if self.provider_reported is None:
            return local
        since_report = max(0.0, local - self._local_at_report)
        return max(local, self.provider_reported + since_report)

    def remaining(self, now: datetime | None = None) -> float:
        return max(0.0, self.daily_cap - self.used(now))

    def check(self, estimate: float, now: datetime | None = None) -> None:
        reset = next_reset(now).strftime("%Y-%m-%d 00:00 UTC")
        if self.ledger.exhausted_today(now):
            raise BudgetExceeded(f"Cloudflare reported today's free allocation as used up. Resumes after {reset}.")
        if self.used(now) + estimate > self.daily_cap:
            raise BudgetExceeded(
                f"Next request (~{estimate:.0f} neurons) would pass today's cap "
                f"({self.used(now):.0f}/{self.daily_cap:.0f} used). Resumes after {reset}."
            )


class RunLock:
    """Prevents two generation runs from spending the same budget concurrently."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = self.path.read_text(encoding="utf-8").strip() or "?"
            if pid.isdigit() and not _pid_alive(int(pid)):
                self.path.unlink(missing_ok=True)
                return self.__enter__()
            raise RuntimeError(f"Another generation run is active (pid {pid}). Lock: {self.path}")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return self

    def __exit__(self, *exc) -> None:
        self.path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
