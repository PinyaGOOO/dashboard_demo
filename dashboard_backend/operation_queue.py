from __future__ import annotations

import itertools
import math
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator


QueuedCallback = Callable[[int], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _bounded_int(value: Any, default: int, *, low: int = 0, high: int = 100) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))


def _percentage(value: Any, *, ratio: bool | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    if ratio is True or (ratio is None and parsed <= 1):
        parsed *= 100
    return max(0.0, min(parsed, 100.0))


class ProxmoxOperationQueue:
    """Adaptive, process-wide admission control for Proxmox mutations.

    Reservations consume global, node and storage capacity atomically. No
    caller can hold one resource while waiting for another, which keeps the
    multidimensional scheduler free of hold-and-wait deadlocks.

    The queue never contacts Proxmox itself. ``update_pressure`` installs a
    cached telemetry snapshot gathered elsewhere, so admission does not add an
    API round trip to an operator action.
    """

    _CONTROL_KINDS = {
        "control", "power", "password", "start", "stop", "restart", "reboot",
    }
    _CONTROL_TASK_TYPES = {
        "qmstart", "qmstop", "qmreboot", "start", "stop", "reboot", "power",
        "password",
    }
    _HEAVY_TASK_TYPES = {
        "clone", "qmclone", "snapshot", "qmsnapshot", "rollback", "qmrollback",
        "delete", "destroy", "qmdestroy", "migrate", "qmigrate", "qmmigrate",
        "backup", "vzdump", "restore", "qmrestore", "move", "qmmove",
    }
    # Long-lived console proxy workers represent interactive sessions, not
    # mutating Proxmox jobs. Counting them would permanently consume scheduler
    # capacity merely because an operator has a console tab open.
    _IGNORED_TASK_TYPES = {
        "vncproxy", "vncshell", "termproxy", "spiceproxy", "spiceshell",
    }

    def __init__(
        self,
        capacity: int = 10,
        *,
        node_capacity: int = 4,
        storage_capacity: int = 6,
        control_reserve: int = 2,
        node_control_reserve: int = 1,
        telemetry_stale_seconds: float = 20.0,
        max_bypass_seconds: float = 10.0,
    ):
        self.capacity = _bounded_int(capacity, 10, low=1)
        self.node_capacity = _bounded_int(node_capacity, 4, low=1)
        self.storage_capacity = _bounded_int(storage_capacity, 6, low=1)
        self.control_reserve = _bounded_int(
            control_reserve, 2, low=0, high=max(0, self.capacity - 1),
        )
        self.node_control_reserve = _bounded_int(
            node_control_reserve, 1, low=0, high=max(0, self.node_capacity - 1),
        )
        try:
            stale_seconds = float(telemetry_stale_seconds)
        except (TypeError, ValueError):
            stale_seconds = 20.0
        try:
            bypass_seconds = float(max_bypass_seconds)
        except (TypeError, ValueError):
            bypass_seconds = 10.0
        self.telemetry_stale_seconds = max(0.1, stale_seconds)
        self.max_bypass_seconds = max(0.0, bypass_seconds)

        self._condition = threading.Condition(threading.RLock())
        self._sequence = itertools.count(1)
        self._waiting: list[dict[str, Any]] = []
        self._active: dict[int, dict[str, Any]] = {}
        self._used = 0

        self._pressure_updated_at = ""
        self._pressure_monotonic: float | None = None
        self._external_monotonic: float | None = None
        self._pressure_error = ""
        self._pressure_warnings: list[str] = []
        self._pressure_sources: dict[str, bool] = {}
        self._pressure_partial = False
        self._pressure_stale_hint = False
        self._cluster_pressure: dict[str, Any] = {}
        self._node_pressure: dict[str, dict[str, Any]] = {}
        self._storage_pressure: dict[str, dict[str, Any]] = {}
        self._external_tasks: list[dict[str, Any]] = []

    @staticmethod
    def _lane(kind: str, lane: str | None) -> str:
        selected = str(lane or "").strip().lower()
        if selected in {"control", "heavy"}:
            return selected
        return (
            "control"
            if str(kind).strip().lower() in ProxmoxOperationQueue._CONTROL_KINDS
            else "heavy"
        )

    @staticmethod
    def _resource_weights(raw: Mapping[str, Any] | None) -> dict[str, int]:
        if not raw:
            return {}
        if not isinstance(raw, Mapping):
            raise TypeError("resource weights must be a mapping")
        result: dict[str, int] = {}
        for raw_name, raw_weight in raw.items():
            name = str(raw_name).strip()
            if not name:
                continue
            try:
                weight = int(raw_weight)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid capacity weight for {name}") from exc
            if weight > 0:
                result[name] = weight
        return result

    @staticmethod
    def _items_by_name(raw: Any, *keys: str) -> list[tuple[str, Mapping[str, Any]]]:
        if isinstance(raw, Mapping):
            result = []
            for raw_name, raw_value in raw.items():
                item = raw_value if isinstance(raw_value, Mapping) else {}
                result.append((str(raw_name).strip(), item))
            return result
        if not isinstance(raw, list):
            return []
        result = []
        for value in raw:
            if not isinstance(value, Mapping):
                continue
            name = ""
            for key in keys:
                name = str(value.get(key) or "").strip()
                if name:
                    break
            result.append((name, value))
        return result

    @staticmethod
    def _storage_key(name: str, item: Mapping[str, Any]) -> tuple[str, str, bool, str]:
        """Return canonical key, storage ID, shared flag and node.

        Normalized live telemetry supplies ``scheduler_key`` explicitly.
        Older adapters without a ``shared`` field retain the historical bare
        storage-ID semantics; an explicit non-shared row is node-qualified.
        """
        storage_id = str(
            item.get("storage_id") or item.get("storage") or item.get("name") or name
        ).strip()
        node = str(item.get("node") or "").strip()
        raw_shared = item.get("shared")
        if isinstance(raw_shared, str):
            shared = raw_shared.strip().lower() not in {
                "", "0", "false", "no", "off", "disabled",
            }
        else:
            shared = bool(raw_shared)
        explicit_key = str(
            item.get("scheduler_key") or item.get("resource_key") or item.get("key") or ""
        ).strip()
        if explicit_key:
            key = explicit_key
        elif raw_shared is None or shared or not node:
            key = storage_id
        else:
            key = f"{node}/{storage_id}"
        return key, storage_id, shared, node

    @staticmethod
    def _pressure_factor(item: Mapping[str, Any], *, storage: bool = False) -> float:
        status = str(item.get("status") or "online").strip().lower()
        availability = item.get("available") if storage else item.get("online")
        if availability is False or status in {
            "offline", "unknown", "unavailable", "disabled", "error",
        }:
            return 0.0

        values: list[float] = []
        keys = (
            ("usage", None), ("used_percent", False), ("used_ratio", True),
            ("disk", None), ("io", None), ("io_pressure", None),
        ) if storage else (
            ("cpu_percent", False), ("cpu_ratio", True), ("cpu", None),
            ("memory_percent", False), ("memory_ratio", True),
            ("ram", None), ("memory", None), ("mem", None),
        )
        for key, ratio in keys:
            parsed = _percentage(item.get(key), ratio=ratio)
            if parsed is not None:
                values.append(parsed)
        pressure = max(values, default=0.0)
        if pressure >= 95:
            return 0.25
        if pressure >= 90:
            return 0.5
        if pressure >= 80:
            return 0.75
        return 1.0

    @classmethod
    def _pressure_resource(
        cls,
        name: str,
        item: Mapping[str, Any],
        default_capacity: int,
        *,
        storage: bool = False,
    ) -> dict[str, Any]:
        base = _bounded_int(
            item.get("base_capacity", item.get("capacity", default_capacity)),
            default_capacity,
            low=1,
        )
        explicit = item.get("effective_capacity")
        if explicit is None:
            factor = cls._pressure_factor(item, storage=storage)
            effective = 0 if factor <= 0 else max(1, math.floor(base * factor))
        else:
            effective = _bounded_int(explicit, base, low=0, high=base)
        result = {
            "name": name,
            "status": str(item.get("status") or "online"),
            "base_capacity": base,
            "effective_capacity": effective,
        }
        cpu = next((
            parsed for key, ratio in (
                ("cpu_percent", False), ("cpu_ratio", True), ("cpu", None),
            )
            for parsed in [_percentage(item.get(key), ratio=ratio)]
            if parsed is not None
        ), None)
        memory = next((
            parsed for key, ratio in (
                ("memory_percent", False), ("memory_ratio", True),
                ("ram", None), ("memory", None), ("mem", None),
            )
            for parsed in [_percentage(item.get(key), ratio=ratio)]
            if parsed is not None
        ), None)
        used = next((
            parsed for key, ratio in (
                ("used_percent", False), ("used_ratio", True), ("usage", None),
                ("disk", None),
            )
            for parsed in [_percentage(item.get(key), ratio=ratio)]
            if parsed is not None
        ), None)
        if cpu is not None:
            result["cpu_percent"] = round(cpu, 1)
        if memory is not None:
            result["memory_percent"] = round(memory, 1)
        if used is not None:
            result["used_percent"] = round(used, 1)
        if effective <= 0:
            result.update({"state": "blocked", "reason": "Ресурс Proxmox недоступен"})
        elif effective < base:
            result.update({"state": "throttled", "reason": "Высокая нагрузка — лимит временно снижен"})
        else:
            result.update({"state": "healthy", "reason": "Ресурс готов к новым операциям"})
        return result

    @staticmethod
    def _resource_peak(item: Mapping[str, Any]) -> float:
        values = [
            float(item[key])
            for key in ("cpu_percent", "memory_percent", "used_percent")
            if item.get(key) is not None
        ]
        return max(values, default=0.0)

    @classmethod
    def _apply_hysteresis(
        cls,
        current: dict[str, Any],
        previous: Mapping[str, Any] | None,
        *,
        explicit_capacity: bool,
    ) -> dict[str, Any]:
        if explicit_capacity or not previous:
            return current
        base = max(1, int(current.get("base_capacity") or 1))
        old_effective = int(previous.get("effective_capacity") or 0)
        new_effective = int(current.get("effective_capacity") or 0)
        if old_effective <= 0 or new_effective <= old_effective or old_effective >= base:
            return current
        old_ratio = old_effective / base
        recovery_threshold = 90.0 if old_ratio <= 0.25 else 85.0 if old_ratio <= 0.5 else 75.0
        if cls._resource_peak(current) >= recovery_threshold:
            current["effective_capacity"] = old_effective
            current["state"] = "throttled"
            current["reason"] = "Лимит удерживается до устойчивого снижения нагрузки"
        return current

    @classmethod
    def _external_task(cls, raw: Mapping[str, Any], index: int) -> dict[str, Any] | None:
        status = str(raw.get("status") or "running").strip().lower()
        if status in {"stopped", "finished", "complete", "completed", "ok", "error", "failed"}:
            return None
        if raw.get("running") is False:
            return None
        if bool(raw.get("managed") or raw.get("internal")):
            return None

        kind = str(
            raw.get("kind") or raw.get("type") or raw.get("worker_type") or "external"
        ).strip().lower()
        if kind in cls._IGNORED_TASK_TYPES:
            return None
        explicit_lane = str(raw.get("lane") or "").strip().lower()
        if explicit_lane in {"control", "heavy"}:
            lane = explicit_lane
        elif kind in cls._CONTROL_TASK_TYPES:
            lane = "control"
        else:
            # Unknown mutations are conservatively treated as heavy work.
            lane = "heavy"
        default_weight = 2 if kind in cls._HEAVY_TASK_TYPES else 1
        weight = _bounded_int(
            raw.get("weight", raw.get("global_weight", default_weight)),
            default_weight,
            low=1,
        )

        node_weights = cls._resource_weights(
            raw.get("node_weights") if isinstance(raw.get("node_weights"), Mapping) else None,
        )
        node = str(raw.get("node") or "").strip()
        if node and not node_weights:
            node_weights[node] = _bounded_int(raw.get("node_weight", weight), weight, low=1)

        storage_weights = cls._resource_weights(
            raw.get("storage_weights")
            if isinstance(raw.get("storage_weights"), Mapping) else None,
        )
        storage_name = str(raw.get("storage") or raw.get("storage_id") or "").strip()
        if storage_name and not storage_weights:
            storage_key, _, _, _ = cls._storage_key(storage_name, raw)
            storage_weights[storage_key] = _bounded_int(
                raw.get("storage_weight", weight), weight, low=1,
            )

        return {
            "id": str(raw.get("upid") or raw.get("id") or f"external-{index}"),
            "upid": str(raw.get("upid") or ""),
            "kind": kind or "external",
            "label": str(
                raw.get("label") or raw.get("description") or kind or "External Proxmox task"
            ),
            "lane": lane,
            "weight": weight,
            "node_weights": node_weights,
            "storage_weights": storage_weights,
            "user": str(raw.get("user") or raw.get("user_name") or ""),
            "started_at": str(
                raw.get("started_at") or raw.get("start_time") or raw.get("starttime") or ""
            ),
            "external": True,
        }

    def update_pressure(self, snapshot: Mapping[str, Any]) -> None:
        """Atomically install cached cluster pressure and running PVE tasks.

        ``nodes`` and ``storages`` may be lists or mappings, while cluster data
        may live under ``cluster`` or ``global``. An explicit
        ``effective_capacity`` wins; otherwise CPU/RAM/storage percentages are
        converted to conservative capacity bands.
        """
        if not isinstance(snapshot, Mapping):
            raise TypeError("pressure snapshot must be a mapping")

        with self._condition:
            previous_nodes = {
                name: dict(item) for name, item in self._node_pressure.items()
            }
            previous_storages = {
                name: dict(item) for name, item in self._storage_pressure.items()
            }

        sources = snapshot.get("sources")
        if isinstance(sources, Mapping):
            resources_fresh = bool(sources.get("resources"))
            tasks_fresh = bool(sources.get("tasks"))
        else:
            # Hand-written adapters and tests predate the source flags. Treat
            # their supplied snapshot as authoritative for compatibility.
            resources_fresh = True
            tasks_fresh = True
        stale_hint = bool(snapshot.get("stale"))

        raw_cluster = snapshot.get("cluster", snapshot.get("global", {}))
        if not isinstance(raw_cluster, Mapping):
            raw_cluster = {}
        cluster = self._pressure_resource("cluster", raw_cluster, self.capacity)

        nodes: dict[str, dict[str, Any]] = {}
        for name, item in self._items_by_name(snapshot.get("nodes", []), "name", "node"):
            if name:
                nodes[name] = self._apply_hysteresis(
                    self._pressure_resource(name, item, self.node_capacity),
                    previous_nodes.get(name),
                    explicit_capacity=item.get("effective_capacity") is not None,
                )

        storages: dict[str, dict[str, Any]] = {}
        for name, item in self._items_by_name(
            snapshot.get("storages", []), "name", "storage", "storage_id",
        ):
            if name:
                storage_key, storage_id, shared, node = self._storage_key(name, item)
                if not storage_key:
                    continue
                normalized = self._pressure_resource(
                    storage_key, item, self.storage_capacity, storage=True,
                )
                normalized = self._apply_hysteresis(
                    normalized,
                    previous_storages.get(storage_key),
                    explicit_capacity=item.get("effective_capacity") is not None,
                )
                normalized["storage"] = storage_id
                normalized["storage_id"] = storage_id
                normalized["scheduler_key"] = storage_key
                normalized["shared"] = shared
                normalized["node"] = node
                normalized["nodes"] = [node] if node else []
                previous = storages.get(storage_key)
                if previous:
                    # cluster/resources returns one row per node even for a
                    # shared NAS. It is one bottleneck, not N independent units.
                    previous["nodes"] = sorted(set(previous["nodes"]) | set(normalized["nodes"]))
                    previous["shared"] = bool(previous.get("shared") or normalized["shared"])
                    available_rows = [
                        row for row in (previous, normalized)
                        if int(row.get("effective_capacity") or 0) > 0
                    ]
                    if available_rows:
                        worst = min(
                            available_rows,
                            key=lambda row: int(row.get("effective_capacity") or 0),
                        )
                        previous.update({
                            key: value for key, value in worst.items()
                            if key not in {"nodes", "shared"}
                        })
                    else:
                        previous.update({
                            key: value for key, value in normalized.items()
                            if key not in {"nodes", "shared"}
                        })
                else:
                    storages[storage_key] = normalized

        # The gateway intentionally does not invent a cluster aggregate. Node
        # limits already protect the selected placement, so one hot node must
        # not freeze independent work elsewhere: use the mean online-node band.
        # Storage pressure is deliberately *not* folded into this global band:
        # a hot shared NAS must constrain only tickets that name it in
        # storage_weights, not unrelated work on another datastore.
        explicit_cluster = bool(raw_cluster)
        if not explicit_cluster:
            node_ratios = [
                float(item["effective_capacity"]) / max(float(item["base_capacity"]), 1)
                for item in nodes.values()
                if int(item.get("effective_capacity") or 0) > 0
            ]
            node_factor = sum(node_ratios) / len(node_ratios) if node_ratios else 1.0
            factor = node_factor
            cluster = {
                "name": "cluster",
                "status": "online" if nodes else "unknown",
                "base_capacity": self.capacity,
                "effective_capacity": max(1, math.floor(self.capacity * factor)),
                "state": "throttled" if factor < 1 else "healthy",
                "reason": (
                    "Лимит снижен по совокупной нагрузке кластера"
                    if factor < 1 else "Кластер готов к новым операциям"
                ),
            }

        raw_tasks = snapshot.get("external_tasks", snapshot.get("tasks", []))
        if isinstance(raw_tasks, Mapping):
            task_values = [
                {**dict(value), "id": str(value.get("id") or key)}
                if isinstance(value, Mapping) else {"id": str(key)}
                for key, value in raw_tasks.items()
            ]
        elif isinstance(raw_tasks, list):
            task_values = raw_tasks
        else:
            task_values = []
        external_tasks = [
            normalized
            for index, raw in enumerate(task_values, 1)
            if isinstance(raw, Mapping)
            for normalized in [self._external_task(raw, index)]
            if normalized is not None
        ]

        warnings = [
            str(item)[:500]
            for item in snapshot.get("warnings", [])
            if str(item).strip()
        ] if isinstance(snapshot.get("warnings", []), list) else []
        now = time.monotonic()
        with self._condition:
            if resources_fresh and not stale_hint:
                self._cluster_pressure = cluster
                self._node_pressure = nodes
                self._storage_pressure = storages
                self._pressure_updated_at = str(
                    snapshot.get("collected_at") or snapshot.get("updated_at") or _utc_now()
                )
                self._pressure_monotonic = now
            if tasks_fresh and not stale_hint:
                self._external_tasks = external_tasks
                self._external_monotonic = now
            self._pressure_sources = {
                "resources": resources_fresh,
                "tasks": tasks_fresh,
            }
            self._pressure_partial = bool(snapshot.get("partial"))
            self._pressure_stale_hint = stale_hint
            self._pressure_warnings = warnings[-6:]
            self._pressure_error = "" if not warnings else "; ".join(warnings)[-1000:]
            self._condition.notify_all()

    def mark_pressure_error(self, message: str) -> None:
        """Record sampler failure without discarding a still-fresh snapshot."""
        with self._condition:
            self._pressure_error = str(
                message or "Не удалось обновить телеметрию Proxmox"
            )[-1000:]
            self._condition.notify_all()

    def _external_is_fresh_locked(self, now: float) -> bool:
        return (
            self._external_monotonic is not None
            and now - self._external_monotonic <= self.telemetry_stale_seconds
        )

    def _telemetry_locked(self, now: float) -> tuple[bool, float | None, str]:
        if self._pressure_monotonic is None:
            return False, None, "unknown"
        age = max(0.0, now - self._pressure_monotonic)
        fresh = age <= self.telemetry_stale_seconds
        if not fresh:
            return False, age, "stale"
        return True, age, "degraded" if self._pressure_error else "fresh"

    def _task_telemetry_degraded_locked(self, fresh: bool, now: float) -> bool:
        return (
            fresh
            and self._pressure_sources.get("tasks") is False
            and not self._external_is_fresh_locked(now)
        )

    def _effective_global_locked(self, fresh: bool, now: float | None = None) -> int:
        if not fresh:
            return self.capacity
        effective = _bounded_int(
            self._cluster_pressure.get("effective_capacity", self.capacity),
            self.capacity,
            low=0,
            high=self.capacity,
        )
        current = time.monotonic() if now is None else now
        if self._task_telemetry_degraded_locked(fresh, current):
            # When cluster/tasks cannot be observed we cannot prove that the
            # cluster is otherwise idle. Keep a modest uncertainty margin while
            # preserving the dedicated control lane. A completely stale
            # resources snapshot still follows the documented static fallback.
            uncertainty = max(self.control_reserve, math.ceil(self.capacity * 0.2))
            degraded_cap = max(1, self.capacity - uncertainty)
            effective = min(effective, degraded_cap)
        return effective

    def _heavy_global_limit_locked(
        self,
        effective_global: int,
        fresh: bool,
        now: float | None = None,
    ) -> int:
        current = time.monotonic() if now is None else now
        if self._task_telemetry_degraded_locked(fresh, current):
            # The degraded global cap already withholds at least the configured
            # control reserve from heavy work. Subtracting the reserve again
            # would make otherwise valid large tickets impossible to admit for
            # as long as task telemetry remains unavailable.
            return max(0, effective_global)
        return max(0, effective_global - self.control_reserve)

    def _node_capacities_locked(self, name: str, fresh: bool) -> tuple[int, int]:
        item = self._node_pressure.get(name) if fresh else None
        if not item:
            return self.node_capacity, self.node_capacity
        base = _bounded_int(item.get("base_capacity"), self.node_capacity, low=1)
        effective = _bounded_int(item.get("effective_capacity"), base, low=0, high=base)
        return base, effective

    def _storage_capacities_locked(self, name: str, fresh: bool) -> tuple[int, int]:
        item = self._storage_pressure.get(name) if fresh else None
        if not item:
            return self.storage_capacity, self.storage_capacity
        base = _bounded_int(item.get("base_capacity"), self.storage_capacity, low=1)
        effective = _bounded_int(item.get("effective_capacity"), base, low=0, high=base)
        return base, effective

    def _external_locked(self, now: float) -> list[dict[str, Any]]:
        # External state is authoritative only while its telemetry is fresh;
        # a vanished sampler must not permanently freeze the queue.
        return self._external_tasks if self._external_is_fresh_locked(now) else []

    def _usage_locked(self, now: float) -> dict[str, Any]:
        lanes = {
            "control": {"global": 0, "nodes": {}, "storages": {}},
            "heavy": {"global": 0, "nodes": {}, "storages": {}},
        }

        def add(item: Mapping[str, Any]) -> None:
            lane = str(item.get("lane") or "heavy")
            if lane not in lanes:
                lane = "heavy"
            target = lanes[lane]
            target["global"] += int(item.get("weight") or 0)
            for name, resource_weight in dict(item.get("node_weights") or {}).items():
                target["nodes"][name] = (
                    target["nodes"].get(name, 0) + int(resource_weight)
                )
            for name, resource_weight in dict(item.get("storage_weights") or {}).items():
                target["storages"][name] = (
                    target["storages"].get(name, 0) + int(resource_weight)
                )

        for item in self._active.values():
            add(item)
        external = self._external_locked(now)
        for item in external:
            add(item)

        nodes = {
            name: (
                lanes["control"]["nodes"].get(name, 0)
                + lanes["heavy"]["nodes"].get(name, 0)
            )
            for name in set(lanes["control"]["nodes"]) | set(lanes["heavy"]["nodes"])
        }
        storages = {
            name: (
                lanes["control"]["storages"].get(name, 0)
                + lanes["heavy"]["storages"].get(name, 0)
            )
            for name in set(lanes["control"]["storages"])
            | set(lanes["heavy"]["storages"])
        }
        return {
            "global": lanes["control"]["global"] + lanes["heavy"]["global"],
            "nodes": nodes,
            "storages": storages,
            "lanes": lanes,
            "external": external,
        }

    def _blocking_reasons_locked(
        self,
        ticket: Mapping[str, Any],
        usage: Mapping[str, Any],
        fresh: bool,
    ) -> list[str]:
        reasons: list[str] = []
        lane = str(ticket["lane"])
        weight = int(ticket["weight"])
        effective_global = self._effective_global_locked(fresh)
        if effective_global <= 0:
            # A reserve protects emergency work from heavy-operation
            # saturation; it must not turn an explicitly offline cluster into
            # usable capacity.
            reasons.append("global_capacity")
        elif lane == "control":
            control_used = int(usage["lanes"]["control"]["global"])
            # External/manual heavy work may already have consumed the whole
            # advertised cluster capacity. The reserved emergency lane remains
            # usable for stop/start/reboot up to its own small limit.
            if (
                int(usage["global"]) + weight > effective_global
                and control_used + weight > self.control_reserve
            ):
                reasons.append("global_capacity")
        else:
            if int(usage["global"]) + weight > effective_global:
                reasons.append("global_capacity")
            heavy_limit = self._heavy_global_limit_locked(
                effective_global, fresh,
            )
            if int(usage["lanes"]["heavy"]["global"]) + weight > heavy_limit:
                reasons.append("control_reserve")

        for name, requested in dict(ticket["node_weights"]).items():
            base, effective = self._node_capacities_locked(name, fresh)
            if effective <= 0:
                # Node control reserve has the same semantics as the global
                # reserve and cannot bypass a hard offline/zero-capacity node.
                reasons.append(f"node:{name}")
            elif lane == "control":
                control_used = int(
                    usage["lanes"]["control"]["nodes"].get(name, 0)
                )
                if (
                    int(usage["nodes"].get(name, 0)) + int(requested) > effective
                    and control_used + int(requested) > self.node_control_reserve
                ):
                    reasons.append(f"node:{name}")
            else:
                if int(usage["nodes"].get(name, 0)) + int(requested) > effective:
                    reasons.append(f"node:{name}")
                # Dynamic pressure has already reduced the node's advertised
                # capacity.  Withholding the control reserve from that smaller
                # value again can make the largest normally valid ticket
                # impossible to admit forever while a busy node stays hot.
                # Keep the dedicated reserve only at the static/base limit;
                # control work may still use its own lane above a throttled
                # effective limit, matching the global reserve semantics.
                reserve = (
                    min(self.node_control_reserve, effective)
                    if effective >= base
                    else 0
                )
                heavy_limit = max(0, effective - reserve)
                heavy_used = int(usage["lanes"]["heavy"]["nodes"].get(name, 0))
                if heavy_used + int(requested) > heavy_limit:
                    reasons.append(f"node_control_reserve:{name}")

        for name, requested in dict(ticket["storage_weights"]).items():
            _, effective = self._storage_capacities_locked(name, fresh)
            if int(usage["storages"].get(name, 0)) + int(requested) > effective:
                reasons.append(f"storage:{name}")
        return list(dict.fromkeys(reasons))

    def _fits_locked(self, ticket: Mapping[str, Any], now: float) -> bool:
        fresh, _, _ = self._telemetry_locked(now)
        usage = self._usage_locked(now)
        return not self._blocking_reasons_locked(ticket, usage, fresh)

    @staticmethod
    def _resource_conflict(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
        first_nodes = set(first.get("node_weights") or {})
        second_nodes = set(second.get("node_weights") or {})
        first_storages = set(first.get("storage_weights") or {})
        second_storages = set(second.get("storage_weights") or {})
        if not (first_nodes or first_storages) or not (second_nodes or second_storages):
            return True
        return bool(first_nodes & second_nodes or first_storages & second_storages)

    def _pair_fits_locked(
        self,
        first: Mapping[str, Any],
        second: Mapping[str, Any],
        now: float,
    ) -> bool:
        if first["lane"] != second["lane"]:
            return True
        merged_nodes = dict(first.get("node_weights") or {})
        for name, weight in dict(second.get("node_weights") or {}).items():
            merged_nodes[name] = merged_nodes.get(name, 0) + int(weight)
        merged_storages = dict(first.get("storage_weights") or {})
        for name, weight in dict(second.get("storage_weights") or {}).items():
            merged_storages[name] = merged_storages.get(name, 0) + int(weight)
        merged = {
            "lane": first["lane"],
            "weight": int(first["weight"]) + int(second["weight"]),
            "node_weights": merged_nodes,
            "storage_weights": merged_storages,
        }
        fresh, _, _ = self._telemetry_locked(now)
        return not self._blocking_reasons_locked(
            merged, self._usage_locked(now), fresh,
        )

    def _can_start_locked(self, ticket: dict[str, Any], now: float) -> bool:
        if not self._waiting or ticket not in self._waiting or not self._fits_locked(ticket, now):
            return False

        # Lanes are independent: an aged deployment must never stop an
        # operator's power command from using reserved control capacity.
        for earlier in self._waiting:
            if earlier is ticket:
                break
            if earlier["lane"] != ticket["lane"]:
                continue
            if self._fits_locked(earlier, now):
                if not self._pair_fits_locked(earlier, ticket, now):
                    return False
                continue
            aged = now - float(earlier["queued_monotonic"]) >= self.max_bypass_seconds
            if not aged:
                continue
            fresh, _, _ = self._telemetry_locked(now)
            reasons = self._blocking_reasons_locked(
                earlier, self._usage_locked(now), fresh,
            )
            protects_global = any(
                reason in {"global_capacity", "control_reserve"}
                for reason in reasons
            )
            if protects_global or self._resource_conflict(earlier, ticket):
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
        node_weights: Mapping[str, int] | None = None,
        storage_weights: Mapping[str, int] | None = None,
        lane: str | None = None,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[dict[str, Any]]:
        selected_lane = self._lane(kind, lane)
        heavy_limit = max(1, self.capacity - self.control_reserve)
        weight_limit = self.capacity if selected_lane == "control" else heavy_limit
        safe_weight = _bounded_int(weight, 1, low=1, high=weight_limit)

        normalized_nodes = self._resource_weights(node_weights)
        normalized_storages = self._resource_weights(storage_weights)
        node_limit = (
            self.node_capacity
            if selected_lane == "control"
            else max(1, self.node_capacity - self.node_control_reserve)
        )
        normalized_nodes = {
            name: min(value, node_limit) for name, value in normalized_nodes.items()
        }
        normalized_storages = {
            name: min(value, self.storage_capacity)
            for name, value in normalized_storages.items()
        }

        ticket = {
            "id": next(self._sequence),
            "kind": str(kind),
            "label": str(label),
            "lane": selected_lane,
            "weight": safe_weight,
            "vm_count": max(0, int(vm_count)),
            "node_weights": normalized_nodes,
            "storage_weights": normalized_storages,
            "queued_at": _utc_now(),
            "queued_monotonic": time.monotonic(),
        }
        if timeout is None:
            deadline = None
        else:
            try:
                timeout_value = max(0.0, float(timeout))
            except (TypeError, ValueError) as exc:
                raise ValueError("timeout must be a number") from exc
            deadline = time.monotonic() + timeout_value

        admitted = False
        queued_notified = False
        with self._condition:
            self._waiting.append(ticket)
            self._condition.notify_all()
        try:
            while not admitted:
                callback_position: int | None = None
                with self._condition:
                    now = time.monotonic()
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError(
                            "Proxmox operation was cancelled while queued"
                        )
                    if deadline is not None and now >= deadline:
                        raise TimeoutError(
                            "Timed out waiting for Proxmox operation capacity"
                        )
                    if self._can_start_locked(ticket, now):
                        self._waiting.remove(ticket)
                        ticket["started_at"] = _utc_now()
                        ticket["started_monotonic"] = now
                        ticket["wait_seconds"] = round(
                            now - float(ticket["queued_monotonic"]), 3,
                        )
                        self._active[int(ticket["id"])] = ticket
                        self._used += safe_weight
                        admitted = True
                        self._condition.notify_all()
                        continue
                    if not queued_notified and on_queued is not None:
                        queued_notified = True
                        callback_position = self._waiting.index(ticket) + 1
                    else:
                        wait_for = 1.0
                        if cancel_event is not None:
                            wait_for = min(wait_for, 0.1)
                        if deadline is not None:
                            wait_for = max(0.001, min(wait_for, deadline - now))
                        self._condition.wait(timeout=wait_for)

                if callback_position is not None:
                    # Never execute arbitrary persistence/UI callbacks while
                    # holding the scheduler condition lock.
                    try:
                        on_queued(callback_position)  # type: ignore[misc]
                    except Exception:
                        pass

            yield {
                key: value
                for key, value in ticket.items()
                if not key.endswith("_monotonic")
            }
        finally:
            with self._condition:
                if ticket in self._waiting:
                    self._waiting.remove(ticket)
                removed = self._active.pop(int(ticket["id"]), None)
                if removed is not None:
                    self._used = max(0, self._used - safe_weight)
                self._condition.notify_all()

    @staticmethod
    def _public_ticket(
        item: Mapping[str, Any], now: float, *, queued: bool = False,
    ) -> dict[str, Any]:
        result = {
            "id": item["id"],
            "kind": item["kind"],
            "label": item["label"],
            "lane": item.get("lane", "heavy"),
            "weight": item["weight"],
            "vm_count": item["vm_count"],
            "node_weights": dict(item.get("node_weights") or {}),
            "storage_weights": dict(item.get("storage_weights") or {}),
            "external": bool(item.get("external", False)),
        }
        if queued:
            result.update({
                "queued_at": item["queued_at"],
                "wait_seconds": round(now - float(item["queued_monotonic"]), 1),
            })
        else:
            started_monotonic = item.get("started_monotonic")
            result.update({
                "started_at": item.get("started_at", ""),
                "duration_seconds": (
                    round(now - float(started_monotonic), 1)
                    if started_monotonic is not None else None
                ),
                "wait_seconds": item.get("wait_seconds", 0),
            })
        return result

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._condition:
            fresh, telemetry_age, telemetry_status = self._telemetry_locked(now)
            usage = self._usage_locked(now)
            effective_capacity = self._effective_global_locked(fresh, now)
            task_telemetry_degraded = self._task_telemetry_degraded_locked(
                fresh, now,
            )
            external = [dict(item) for item in usage["external"]]

            active = [
                self._public_ticket(item, now) for item in self._active.values()
            ]

            queued = []
            for position, item in enumerate(self._waiting, 1):
                public = self._public_ticket(item, now, queued=True)
                public["position"] = position
                public["blocking_reasons"] = self._blocking_reasons_locked(
                    item, usage, fresh,
                )
                public["block_reason"] = (
                    public["blocking_reasons"][0]
                    if public["blocking_reasons"] else "earlier_operation"
                )
                queued.append(public)

            all_node_names = (
                set(self._node_pressure)
                | set(usage["nodes"])
                | {
                    name
                    for item in self._waiting
                    for name in item["node_weights"]
                }
            )
            nodes = []
            for name in sorted(all_node_names):
                base, effective = self._node_capacities_locked(name, fresh)
                internal_used = sum(
                    int(item["node_weights"].get(name, 0))
                    for item in self._active.values()
                )
                total_used = int(usage["nodes"].get(name, 0))
                external_tasks_count = sum(
                    1 for task in external if int(task["node_weights"].get(name, 0)) > 0
                )
                pressure_item = self._node_pressure.get(name, {}) if fresh else {}
                nodes.append({
                    **pressure_item,
                    "name": name,
                    "node": name,
                    "state": pressure_item.get("state", "unknown"),
                    "reason": pressure_item.get(
                        "reason", "Нет актуальной телеметрии ноды",
                    ),
                    "cpu_percent": pressure_item.get("cpu_percent"),
                    "memory_percent": pressure_item.get("memory_percent"),
                    "base_capacity": base,
                    "configured_capacity": base,
                    "effective_capacity": effective,
                    "control_reserve": min(self.node_control_reserve, effective),
                    "used": internal_used,
                    "internal_used": internal_used,
                    "total_used": total_used,
                    "external_used": total_used - internal_used,
                    "external_tasks_count": external_tasks_count,
                    "control_used": int(
                        usage["lanes"]["control"]["nodes"].get(name, 0)
                    ),
                    "heavy_used": int(
                        usage["lanes"]["heavy"]["nodes"].get(name, 0)
                    ),
                    "free": max(0, effective - total_used),
                })

            all_storage_names = (
                set(self._storage_pressure)
                | set(usage["storages"])
                | {
                    name
                    for item in self._waiting
                    for name in item["storage_weights"]
                }
            )
            storages = []
            for name in sorted(all_storage_names):
                base, effective = self._storage_capacities_locked(name, fresh)
                internal_used = sum(
                    int(item["storage_weights"].get(name, 0))
                    for item in self._active.values()
                )
                total_used = int(usage["storages"].get(name, 0))
                pressure_item = self._storage_pressure.get(name, {}) if fresh else {}
                storages.append({
                    **pressure_item,
                    "name": name,
                    "key": name,
                    "scheduler_key": name,
                    "storage": pressure_item.get("storage", name),
                    "storage_id": pressure_item.get("storage_id", pressure_item.get("storage", name)),
                    "state": pressure_item.get("state", "unknown"),
                    "reason": pressure_item.get(
                        "reason", "Нет актуальной телеметрии хранилища",
                    ),
                    "base_capacity": base,
                    "configured_capacity": base,
                    "effective_capacity": effective,
                    "used": internal_used,
                    "internal_used": internal_used,
                    "total_used": total_used,
                    "external_used": total_used - internal_used,
                    "external_tasks_count": sum(
                        1 for task in external
                        if int(task["storage_weights"].get(name, 0)) > 0
                    ),
                    "free": max(0, effective - total_used),
                })

            total_used = int(usage["global"])
            internal_used = sum(
                int(item["weight"]) for item in self._active.values()
            )
            external_used = total_used - internal_used
            heavy_limit = self._heavy_global_limit_locked(
                effective_capacity, fresh, now,
            )
            internal_control_used = sum(
                int(item["weight"])
                for item in self._active.values() if item["lane"] == "control"
            )
            internal_heavy_used = internal_used - internal_control_used
            control_total_used = int(usage["lanes"]["control"]["global"])
            heavy_total_used = int(usage["lanes"]["heavy"]["global"])
            pressure_stale = (
                telemetry_status in {"unknown", "stale"}
                or self._pressure_stale_hint
            )
            if telemetry_status == "unknown":
                pressure_state = "unknown"
                pressure_reason = "Телеметрия Proxmox ещё не получена; действуют настроенные лимиты"
            elif telemetry_status == "stale":
                pressure_state = "stale"
                pressure_reason = "Телеметрия устарела; действуют настроенные лимиты"
            elif task_telemetry_degraded:
                pressure_state = "degraded"
                pressure_reason = (
                    "Список активных задач Proxmox устарел; действует безопасный "
                    "пониженный лимит"
                )
            elif self._pressure_error or self._pressure_partial:
                pressure_state = "degraded"
                pressure_reason = self._pressure_error or "Получена только часть телеметрии Proxmox"
            elif effective_capacity < self.capacity:
                pressure_state = "throttled"
                pressure_reason = "Лимит временно снижен из-за нагрузки Proxmox"
            else:
                pressure_state = "healthy"
                pressure_reason = "Телеметрия актуальна"
            pressure = {
                "status": telemetry_status,
                "state": pressure_state,
                "reason": pressure_reason,
                "stale": pressure_stale,
                "updated_at": self._pressure_updated_at,
                "collected_at": self._pressure_updated_at,
                "age_seconds": (
                    round(telemetry_age, 1) if telemetry_age is not None else None
                ),
                "stale_after_seconds": self.telemetry_stale_seconds,
                "error": self._pressure_error,
                "warnings": list(self._pressure_warnings),
                "sources": dict(self._pressure_sources),
                "partial": self._pressure_partial,
                "tasks_stale": task_telemetry_degraded,
                "degraded_capacity": (
                    effective_capacity if task_telemetry_degraded else None
                ),
                "cluster": dict(self._cluster_pressure) if fresh else {},
            }
            return {
                # Backward-compatible fields used by the current dashboard.
                "capacity": self.capacity,
                "configured_capacity": self.capacity,
                "used": internal_used,
                "total_used": total_used,
                "free": max(0, effective_capacity - total_used),
                "utilization": round(
                    internal_used / max(self.capacity, 1) * 100, 1,
                ),
                "active_count": len(active),
                "queued_count": len(queued),
                "active": active,
                "queued": queued,
                # Adaptive scheduler detail.
                "effective_capacity": effective_capacity,
                "effective_utilization": round(
                    total_used / max(effective_capacity, 1) * 100, 1,
                ),
                "internal_used": internal_used,
                "external_used": external_used,
                "external_tasks_count": len(external),
                "total_active_count": len(active) + len(external),
                "control_reserve": self.control_reserve,
                "node_capacity": self.node_capacity,
                "node_control_reserve": self.node_control_reserve,
                "storage_capacity": self.storage_capacity,
                "lanes": {
                    "control": {
                        "used": internal_control_used,
                        "total_used": control_total_used,
                        "capacity": effective_capacity,
                        "active_count": sum(
                            1 for item in active if item.get("lane") == "control"
                        ),
                        "queued_count": sum(
                            1 for item in queued if item.get("lane") == "control"
                        ),
                    },
                    "heavy": {
                        "used": internal_heavy_used,
                        "total_used": heavy_total_used,
                        "capacity": heavy_limit,
                        "active_count": sum(
                            1 for item in active if item.get("lane") == "heavy"
                        ),
                        "queued_count": sum(
                            1 for item in queued if item.get("lane") == "heavy"
                        ),
                    },
                },
                "nodes": nodes,
                "storages": storages,
                "external": {
                    "active_count": len(external),
                    "used": external_used,
                    "tasks": external,
                },
                "external_tasks": external,
                "pressure": pressure,
                "telemetry": pressure,
            }
