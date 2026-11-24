"""Read-only view of pool members added outside Deployer."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from .database import DashboardStore, utc_now
from .proxmox_gateway import DemoProxmoxGateway, LiveProxmoxGateway


@dataclass(frozen=True)
class PoolSample:
    members: tuple[dict, ...]
    updated_at: str
    sampled_at: float


class PoolInventory:
    interval = 30

    def __init__(self, store: DashboardStore, gateway: DemoProxmoxGateway | LiveProxmoxGateway):
        self.store = store
        self.gateway = gateway
        self._lock = threading.Lock()
        self._samples: dict[str, PoolSample] = {}
        self._failed: set[str] = set()

    def refresh(self) -> None:
        pools = [row["pool_id"] for row in self.store.query_all(
            "SELECT DISTINCT pool_id FROM stands WHERE pool_id != ''"
        )]
        try:
            result = self.gateway.pool_inventory(pools) if pools else {}
        except Exception:
            logging.getLogger(__name__).warning("Could not refresh Proxmox pool inventory", exc_info=True)
            result = {}
        now, stamp = time.monotonic(), utc_now()
        with self._lock:
            samples = {}
            for pool in pools:
                if pool in result:
                    samples[pool] = PoolSample(tuple(result[pool]), stamp, now)
                elif pool in self._samples:
                    samples[pool] = self._samples[pool]
            self._samples = samples
            self._failed = set(pools) - result.keys()

    def monitor(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.refresh()
            except Exception:
                logging.getLogger(__name__).exception("Pool inventory monitor could not refresh")
            stop.wait(self.interval)

    def describe(self, stands: list[dict[str, Any]], *, detail: bool = False) -> None:
        if not stands:
            return
        pools = {s["pool_id"] for s in stands}
        # Exclude members belonging to *any* stand in a shared pool, including
        # clones with planned names awaiting their final VMID commit.
        sql = """SELECT s.pool_id, v.vmid, v.name FROM stand_vms v
                 JOIN stands s ON s.id = v.stand_id"""
        params = ()
        if len(pools) == 1:
            sql += " WHERE s.pool_id = ?"
            params = (next(iter(pools)),)
        known: dict[str, set[int]] = {}
        planned: dict[str, set[str]] = {}
        for vm in self.store.query_all(sql, params):
            if vm["vmid"] is None:
                planned.setdefault(vm["pool_id"], set()).add(vm["name"])
            else:
                known.setdefault(vm["pool_id"], set()).add(vm["vmid"])

        now = time.monotonic()
        with self._lock:
            samples, failed = self._samples, self._failed
        for stand in stands:
            pool = stand["pool_id"]
            sample = samples.get(pool)
            state = "unavailable" if pool in failed else "pending"
            extras = []
            if sample is not None:
                state = "stale" if pool in failed or now - sample.sampled_at > 2 * self.interval else "fresh"
                ids, names = known.get(pool, set()), planned.get(pool, set())
                extras = [vm for vm in sample.members if vm["vmid"] not in ids and vm["name"] not in names]
            stand["pool_inventory_state"] = state
            stand["pool_inventory_updated_at"] = sample.updated_at if sample else None
            stand["pool_extra_vm_count"] = len(extras)
            if detail:
                stand["pool_extra_vms"] = [
                    {key: vm[key] for key in ("vmid", "name", "node", "status")} for vm in extras
                ]
                if stand["origin"] == "imported" and sample and state == "fresh":
                    stand["pool_inventory_vmids"] = [vm["vmid"] for vm in sample.members]
