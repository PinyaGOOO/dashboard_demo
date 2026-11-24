"""On-demand cluster-wide measurements, independent of Deployer-managed stands."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .database import utc_now
from .resource_consumers import build_report


class ConsumerReportUnavailable(RuntimeError):
    pass


class ConsumerMonitor:
    interval = 15
    max_rate_interval = 120
    max_stale_age = 300
    coverage_note = ("Показаны ресурсы, доступные учётной записи Proxmox, независимо от пулов "
                     "и рабочей области Deployer. Шаблоны не участвуют в рейтинге гостей.")

    def __init__(self, read: Callable[[], list[dict]], mode: str):
        self._read = read
        self._mode = mode
        self._lock = threading.Lock()
        self._last_attempt: float | None = None
        self._sample_time: float | None = None
        self._rows: list[dict] | None = None
        self._report: dict[str, Any] | None = None
        self._read_failed = False

    def report(self) -> dict[str, Any]:
        # Share a sample across operators. No browser on this page -> no polling of PVE.
        with self._lock:
            now = time.monotonic()
            if self._last_attempt is None or now - self._last_attempt >= self.interval:
                try:
                    rows = self._read()
                except Exception:
                    # Do not echo upstream URLs, tokens or internal API errors to the browser.
                    self._read_failed = True
                    self._last_attempt = time.monotonic()
                else:
                    stamp = time.monotonic()
                    elapsed = None if self._sample_time is None else stamp - self._sample_time
                    prev = self._rows if elapsed is not None and 0 < elapsed <= self.max_rate_interval else None
                    report = build_report(rows, prev, elapsed if prev is not None else None)
                    self._report = dict(report, observed_at=utc_now(), mode=self._mode, coverage_note=self.coverage_note)
                    self._rows, self._sample_time = rows, stamp
                    self._last_attempt, self._read_failed = stamp, False
            if self._report is None or (self._read_failed and time.monotonic() - self._sample_time > self.max_stale_age):
                raise ConsumerReportUnavailable("Нет свежих данных от Proxmox. Проверьте подключение и права на чтение ресурсов.")
            return dict(self._report, stale=self._read_failed, warning=(
                "Proxmox не ответил. Показан последний успешный замер." if self._read_failed else ""
            ))
