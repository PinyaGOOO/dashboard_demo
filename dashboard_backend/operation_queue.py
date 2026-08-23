from __future__ import annotations

import itertools
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator


QueuedCallback = Callable[[int], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ProxmoxOperationQueue:
    """A process-wide weighted admission queue for expensive Proxmox work.

    Capacity units are deliberately abstract: they describe expected pressure
    rather than a number of threads. Small power operations can therefore run
    beside one deployment, while two large clone waves are serialized.
    """

    def __init__(self, capacity: int = 10, *, max_bypass_seconds: float = 10.0):
        self.capacity = max(1, min(int(capacity), 100))
        self.max_bypass_seconds = max(0.0, float(max_bypass_seconds))
        self._condition = threading.Condition(threading.RLock())
        self._sequence = itertools.count(1)
        self._waiting: list[dict[str, Any]] = []
        self._active: dict[int, dict[str, Any]] = {}
        self._used = 0

    def _can_start(self, ticket: dict[str, Any], now: float) -> bool:
        if self._used + int(ticket["weight"]) > self.capacity:
            return False
        if not self._waiting or ticket not in self._waiting:
            return False
        first = self._waiting[0]
        if first is not ticket and now - float(first["queued_monotonic"]) >= self.max_bypass_seconds:
            return False
        for earlier in self._waiting:
            if earlier is ticket:
                break
            if self._used + int(earlier["weight"]) <= self.capacity:
                return False
        return True

    @contextmanager
    def reserve(
        self,
        kind: str,
        label: str,
        weight: int,
        *,
        vm_count: int = 0,
        on_queued: QueuedCallback | None = None,
    ) -> Iterator[dict[str, Any]]:
        safe_weight = max(1, min(int(weight), self.capacity))
        ticket = {
            "id": next(self._sequence),
            "kind": str(kind),
            "label": str(label),
            "weight": safe_weight,
            "vm_count": max(0, int(vm_count)),
            "queued_at": _utc_now(),
            "queued_monotonic": time.monotonic(),
        }
        queued_notified = False
        with self._condition:
            self._waiting.append(ticket)
            while not self._can_start(ticket, time.monotonic()):
                if not queued_notified and on_queued is not None:
                    queued_notified = True
                    try:
                        on_queued(self._waiting.index(ticket) + 1)
                    except Exception:
                        # Admission control must remain safe even if a visual
                        # progress or audit callback cannot be persisted.
                        pass
                self._condition.wait(timeout=1.0)
            self._waiting.remove(ticket)
            ticket["started_at"] = _utc_now()
            ticket["started_monotonic"] = time.monotonic()
            ticket["wait_seconds"] = round(
                float(ticket["started_monotonic"]) - float(ticket["queued_monotonic"]), 3,
            )
            self._active[int(ticket["id"])] = ticket
            self._used += safe_weight
            self._condition.notify_all()
        try:
            yield dict(ticket)
        finally:
            with self._condition:
                removed = self._active.pop(int(ticket["id"]), None)
                if removed is not None:
                    self._used = max(0, self._used - safe_weight)
                self._condition.notify_all()

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._condition:
            active = [
                {
                    "id": item["id"], "kind": item["kind"], "label": item["label"],
                    "weight": item["weight"], "vm_count": item["vm_count"],
                    "started_at": item["started_at"],
                    "duration_seconds": round(now - float(item["started_monotonic"]), 1),
                    "wait_seconds": item["wait_seconds"],
                }
                for item in self._active.values()
            ]
            queued = [
                {
                    "id": item["id"], "kind": item["kind"], "label": item["label"],
                    "weight": item["weight"], "vm_count": item["vm_count"],
                    "queued_at": item["queued_at"], "position": position,
                    "wait_seconds": round(now - float(item["queued_monotonic"]), 1),
                }
                for position, item in enumerate(self._waiting, 1)
            ]
            return {
                "capacity": self.capacity,
                "used": self._used,
                "free": self.capacity - self._used,
                "utilization": round(self._used / self.capacity * 100, 1),
                "active_count": len(active),
                "queued_count": len(queued),
                "active": active,
                "queued": queued,
            }
