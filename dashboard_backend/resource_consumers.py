"""A point-in-time, read-only report from Proxmox /cluster/resources samples."""
from __future__ import annotations

from itertools import groupby
import math
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _uint(value: Any) -> int | None:
    # Keep integer counters exact: subtracting floats loses bytes beyond 2**53.
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 <= number <= 2**64 - 1 else None


def _fraction(value: Any) -> float | None:
    n = _number(value)
    return n if n is not None and n <= 1 else None


def _ratio_pct(used: int | None, total: int | None) -> float | None:
    if used is None or not total:
        return None
    return round(100 * used / total, 2)


def _flag(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _counter_rate(row: dict, old: dict | None, elapsed: float | None,
                  counters: tuple[str, str]) -> float | None:
    # Very short intervals race pvestatd updates; long gaps cannot describe current I/O.
    if old is None or elapsed is None or not 1 <= elapsed <= 300:
        return None
    if row.get("status") != "running" or old.get("status") != "running":
        return None
    if _flag(old.get("template")) or row.get("uid") != old.get("uid"):
        return None
    now_uptime, old_uptime = _uint(row.get("uptime")), _uint(old.get("uptime"))
    # A running guest with identical uptime on two reads usually means the
    # same pvestatd sample was served twice, not a measured zero-byte interval.
    if now_uptime is not None and (now_uptime < elapsed or
                                   (old_uptime is not None and now_uptime <= old_uptime)):
        return None
    delta = 0
    for key in counters:
        current, previous = _uint(row.get(key)), _uint(old.get(key))
        if current is None or previous is None or current < previous:
            return None
        delta += current - previous
    return round(delta / elapsed, 2)


def _guest_key(row: dict) -> tuple:
    return row.get("type"), _uint(row.get("vmid")), _text(row.get("node"))


def _storage_rows(rows: list[dict], offline: set[str]) -> list[dict]:
    # Local storages often share a NAME only. A shared ID, conversely, is the
    # same storage reported by several nodes: keep one real sample, not their sum.
    def key(row: dict) -> tuple[str, str]:
        return _text(row.get("storage")), "" if _flag(row.get("shared")) else _text(row.get("node"))

    storages = []
    for (name, local_node), group in groupby(sorted(rows, key=key), key=key):
        observations = []
        nodes = set()
        for row in group:
            node = _text(row.get("node"))
            nodes.add(node)
            if row.get("status") not in {"available", "active", "online"} or node in offline:
                continue
            used, total = _uint(row.get("disk")), _uint(row.get("maxdisk"))
            pct = _ratio_pct(used, total)
            observations.append((pct, used, total, node))
        # If node observations differ, show the most occupied observed view.
        pct, used, total, _ = max(
            observations, key=lambda s: (s[0] is not None, s[0] or 0, s[1] is not None, s[3]),
            default=(None, None, None, ""),
        )
        storages.append({"name": name, "node": local_node or ", ".join(sorted(nodes)),
                         "shared": not bool(local_node), "used_pct": pct,
                         "used_bytes": used, "total_bytes": total})
    return storages


def _highest(rows: list[dict], field: str) -> dict | None:
    return max((r for r in rows if r[field] is not None), key=lambda r: r[field], default=None)


def _fmt(value: float) -> str:
    return f"{value:.1f}".replace(".", ",").removesuffix(",0")


def _recommend(guests: list[dict], nodes: list[dict], storages: list[dict]) -> list[dict]:
    result = []
    volume = _highest(storages, "used_pct")
    if volume and volume["used_pct"] >= 85:
        left = max(0, volume["total_bytes"] - volume["used_bytes"]) / 1024**3
        result.append({
            "title": f"Проверьте свободное место: {volume['name']}",
            "detail": f"Занято {_fmt(volume['used_pct'])}% на {volume['node']}, "
                      f"свободно {_fmt(left)} ГиБ. Проверьте динамику заполнения и сроки хранения "
                      "резервных копий и снимков, прежде чем расширять хранилище.",
        })
    for node_metric, guest_metric, threshold, unit, subject, advice in (
        ("cpu_pct", "cpu_cores", 85, "ядра CPU", "CPU",
         "Сверьте график узла с расписанием тяжёлых задач. Если пики повторяются одновременно, "
         "попробуйте разнести время их запуска."),
        ("ram_pct", "ram_bytes", 90, "ГиБ", "RAM",
         "Проверьте историю памяти и подкачку на узле перед изменением лимитов или переносом гостей."),
    ):
        node = _highest(nodes, node_metric)
        if node is None or node[node_metric] < threshold:
            continue
        top = _highest([g for g in guests if g["node"] == node["name"]], guest_metric)
        detail = f"Сейчас занято {_fmt(node[node_metric])}% {subject} узла."
        if top and top[guest_metric] > 0:
            value = top[guest_metric] / 1024**3 if guest_metric == "ram_bytes" else top[guest_metric]
            detail += f" Среди видимых гостей лидирует {top['name']} ({top['vmid']}): {_fmt(value)} {unit}."
        result.append({"title": f"Проверьте нагрузку {subject}: {node['name']}", "detail": f"{detail} {advice}"})
    measured = [g for g in guests if g["io_bps"] is not None]
    if len(measured) >= 2:
        leader = max(measured, key=lambda g: g["io_bps"])
        total = sum(g["io_bps"] for g in measured)
        throughput = leader["io_bps"] / 1024**2
        if throughput >= 50 and leader["io_bps"] >= total / 2:
            result.append({
                "title": f"Сверьте дисковую активность: {leader['name']} ({leader['vmid']})",
                "detail": f"Чтение и запись — {_fmt(throughput)} МиБ/с: "
                          f"{_fmt(100 * leader['io_bps'] / total)}% потока среди гостей с измерениями. "
                          "Сопоставьте интервал с расписанием бэкапов и задержками хранилища. "
                          "Большой поток сам по себе не означает перегрузку.",
            })
    return result


def build_report(resources: list[dict], previous_resources: list[dict] | None = None,
                 elapsed: float | None = None) -> dict:
    """Project only monitoring fields; absence/ACL restrictions are not zero usage."""
    rows = [r for r in resources if isinstance(r, dict)]
    prior = [r for r in previous_resources or [] if isinstance(r, dict)]
    previously_offline = {_text(r.get("node")) for r in prior
                          if r.get("type") == "node" and r.get("status") == "offline"}
    previous = {_guest_key(r): r for r in prior
                if _text(r.get("type")) in {"qemu", "lxc"} and _text(r.get("node")) not in previously_offline}
    elapsed = _number(elapsed)
    guests, nodes = [], []
    offline = {_text(r.get("node")) for r in rows if r.get("type") == "node" and r.get("status") == "offline"}
    volumes = []
    for row in rows:
        kind, node = _text(row.get("type")), _text(row.get("node"))
        status = _text(row.get("status")) or "unknown"
        if kind in {"qemu", "lxc"}:
            key = _guest_key(row)
            vmid = key[1]
            if not vmid or _flag(row.get("template")):
                continue
            if node in offline:
                status = "unknown"
            cpu, cpus = _fraction(row.get("cpu")), _number(row.get("maxcpu"))
            old = previous.get(key) if node and status == "running" else None
            guests.append({
                "vmid": vmid, "name": _text(row.get("name")) or f"{kind.upper()} {vmid}",
                "kind": kind, "node": node, "status": status,
                "cpu_cores": round(cpu * cpus, 4) if status == "running" and cpu is not None and cpus else None,
                "ram_bytes": _uint(row.get("mem")) if status in {"running", "paused"} else None,
                "disk_capacity_bytes": _uint(row.get("maxdisk")),
                "io_bps": _counter_rate(row, old, elapsed, ("diskread", "diskwrite")),
                "net_bps": _counter_rate(row, old, elapsed, ("netin", "netout")),
            })
        elif kind == "node" and node:
            online = status == "online"
            cpu = _fraction(row.get("cpu")) if online else None
            nodes.append({"name": node, "status": status,
                          "cpu_pct": round(cpu * 100, 2) if cpu is not None else None,
                          "ram_pct": _ratio_pct(_uint(row.get("mem")), _uint(row.get("maxmem"))) if online else None})
        elif kind == "storage" and node and _text(row.get("storage")):
            volumes.append(row)

    guests.sort(key=lambda r: (r["vmid"], r["node"]))
    nodes.sort(key=lambda r: r["name"])
    storages = _storage_rows(volumes, offline)
    return {"guests": guests, "nodes": nodes, "storages": storages,
            "recommendations": _recommend(guests, nodes, storages)}
