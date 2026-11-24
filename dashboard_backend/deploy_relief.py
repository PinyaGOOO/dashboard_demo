"""Operator-confirmed load relief before retrying an unstarted deployment."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .database import utc_now

if TYPE_CHECKING:
    from .service import DashboardService


def _eligible_vms(service: DashboardService, stand: dict, workspace: str) -> list[dict]:
    if (stand["workspace"] != workspace or stand["origin"] not in {"deployed", "existing"}
            or stand["status"] != "running" or stand["check_status"] == "running"
            or stand["bulk_rollback_pending"]):
        return []
    with service._job_lock:
        if stand["id"] in service._jobs:
            return []
    vms = stand["vms"]
    if len(vms) != stand["vm_count"] or any(
        vm["vmid"] is None or vm["check_status"] == "running"
        or vm["status"] not in {"running", "stopped"} for vm in vms
    ):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    if service.store.query_one(
        """SELECT 1 FROM sessions WHERE stand_id = ? AND status IN ('active','idle')
           AND last_seen >= ? LIMIT 1""", (stand["id"], cutoff),
    ):
        return []
    return [vm for vm in vms if vm["status"] == "running"]


def _quiet_vmids(service: DashboardService, vmids: list[int]) -> set[int]:
    if not vmids:
        return set()
    report = service.gateway.web_activity(vmids, window_seconds=900)
    errors = report["errors"]
    unavailable = {int(item["vmid"]) for item in errors}
    # Every requested VM must have a positive read or an identified error.
    if int(report["scanned_vms"]) != len(set(vmids) - unavailable):
        return set()
    return set(vmids) - unavailable - {int(item["vmid"]) for item in report["activity"]}


def candidates(service: DashboardService, stand_id: int) -> dict[str, Any]:
    from .service import ConflictError

    target = service.get_stand(stand_id)
    if target["status"] != "awaiting_confirmation":
        raise ConflictError("Задача уже вышла из ожидания подтверждения")
    options = []
    for row in service.store.query_all(
        """SELECT id FROM stands WHERE workspace = ? AND status = 'running'
           AND origin IN ('deployed','existing') ORDER BY name, id""", (target["workspace"],),
    ):
        stand = service.get_stand(row["id"])
        vms = _eligible_vms(service, stand, target["workspace"])
        if vms:
            options.append((stand, {vm["vmid"] for vm in vms}))
    quiet = _quiet_vmids(service, sorted({vmid for _, vmids in options for vmid in vmids}))
    return {"stands": [{"id": stand["id"], "name": stand["name"], "vm_count": len(vmids)}
                       for stand, vmids in options if vmids <= quiet]}


def stop_selected(service: DashboardService, stand_id: int, ids: list[int]) -> None:
    """Validate again under each stand's power-operation gate; never force stop."""
    from .service import ConflictError

    target = service.get_stand(stand_id)
    workspace = target["workspace"]
    # Reject invalid selections as a batch before submitting the first shutdown.
    selected = [service.get_stand(source_id) for source_id in ids]
    for stand in selected:
        if stand["id"] == stand_id or not _eligible_vms(service, stand, workspace):
            raise ConflictError(f"Стенд «{stand['name']}» больше недоступен для выключения")

    for source in selected:
        source_id = source["id"]
        with service._stand_admitted_operation(
            source_id, "power", f"{source['name']} · освободить ресурсы", 1,
            vm_count=len(source["vms"]), **service._operation_scope("power", source["vms"]),
            only_if_idle=True, timeout=15,
        ):
            stand = service.get_stand(source_id)
            vms = _eligible_vms(service, stand, workspace)
            if not vms or _quiet_vmids(service, [vm["vmid"] for vm in vms]) != {vm["vmid"] for vm in vms}:
                raise ConflictError(f"На стенде «{stand['name']}» есть активность или его состояние изменилось")
            # Match every tracked VM to the pool, also catching a previously stopped VM
            # started outside Deployer (its activity was not included in our probe).
            if service.gateway.mode == "live":
                members = {vm["vmid"]: vm for vm in service.gateway.pool_members(stand["pool_id"])}
                for vm in stand["vms"]:
                    member = members.get(vm["vmid"], {})
                    if any(member.get(key) != vm[key] for key in ("name", "node", "status")):
                        raise ConflictError(f"Состав или состояние пула {stand['pool_id']} изменились")
            for index, vm in enumerate(vms):
                if index and _quiet_vmids(service, [vm["vmid"]]) != {vm["vmid"]}:
                    raise ConflictError(f"Активность VM {vm['vmid']} изменилась во время выключения стенда")
                service.gateway.shutdown_idle_vm(vm)
                with service.store.transaction() as connection:
                    connection.execute(
                        "UPDATE stand_vms SET status = 'stopped', cpu = 0, ram = 0 WHERE stand_id = ? AND vmid = ?",
                        (source_id, vm["vmid"]),
                    )
                    connection.execute(
                        """UPDATE stands SET status = 'stopped', cpu = 0, ram = 0, updated_at = ?
                           WHERE id = ? AND NOT EXISTS
                           (SELECT 1 FROM stand_vms WHERE stand_id = ? AND status != 'stopped')""",
                        (utc_now(), source_id, source_id),
                    )
                service.store.add_activity("power", "VM выключена перед повтором развёртывания",
                                           f"{stand['name']} · VM {vm['vmid']} · задача #{stand_id}")
