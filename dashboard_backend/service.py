from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import DashboardStore, utc_now
from .passwords import PROXMOX_PASSWORD_MAX_LENGTH, generate_password
from .proxmox_gateway import (
    CredentialRestoreError,
    DemoProxmoxGateway,
    LiveProxmoxGateway,
    RollbackSnapshotError,
    existing_pool_vm_name,
)


Gateway = DemoProxmoxGateway | LiveProxmoxGateway
SESSION_IDLE_AFTER = timedelta(minutes=2)
SESSION_EXPIRE_AFTER = timedelta(minutes=30)


class NotFoundError(ValueError):
    pass


class ConflictError(ValueError):
    pass


class ValidationError(ValueError):
    pass


class DashboardService:
    def __init__(self, store: DashboardStore, gateway: Gateway):
        self.store = store
        self.gateway = gateway
        self._jobs: dict[int, threading.Thread] = {}
        self._job_lock = threading.Lock()
        self._stand_operation_locks: dict[int, threading.RLock] = {}
        self._bulk_rollback_lock = threading.RLock()
        self._bulk_rollback_job: threading.Thread | None = None
        self._bulk_rollback_pending: set[int] = set()
        self._web_activity_lock = threading.Lock()
        self._web_activity_cache: dict[str, Any] | None = None
        self._web_activity_cache_at = 0.0
        self._mark_interrupted_rollbacks()
        if gateway.mode == "demo":
            self._resume_demo_deployments()

    def _mark_interrupted_rollbacks(self) -> None:
        """A background rollback cannot survive a dashboard process restart."""
        interrupted = self.store.query_all(
            "SELECT id, name FROM stands WHERE status = 'resetting'",
        )
        for stand in interrupted:
            message = (
                "Возврат к snapshot start был прерван перезапуском dashboard. "
                "Проверьте задачи Proxmox и запустите возврат повторно."
            )
            self.store.execute(
                "UPDATE stands SET status = 'error', last_error = ?, updated_at = ? WHERE id = ?",
                (message, utc_now(), int(stand["id"])),
            )
            self.store.execute(
                """UPDATE stand_vms SET status = 'error', credential_valid = 0
                WHERE stand_id = ? AND status = 'resetting'""",
                (int(stand["id"]),),
            )
            self.store.add_activity(
                "rollback", "Возврат был прерван", str(stand["name"]), "error", "Система",
            )

    def _resume_demo_deployments(self) -> None:
        """Let the seeded progress card complete instead of remaining at 72%."""
        pending = self.store.query_all(
            """SELECT s.id, s.blueprint_id FROM stands s
            WHERE s.status = 'provisioning' AND s.blueprint_id IS NOT NULL"""
        )
        for item in pending:
            blueprint = self.store.query_one("SELECT * FROM blueprints WHERE id = ?", (item["blueprint_id"],))
            if not blueprint:
                continue
            stand_id = int(item["id"])
            thread = threading.Thread(
                target=self._deploy_job,
                args=(stand_id, blueprint),
                name=f"demo-resume-{stand_id}",
                daemon=True,
            )
            self._jobs[stand_id] = thread
            thread.start()

    def _stand_operation_lock(self, stand_id: int) -> threading.RLock:
        """Serialize mutating requests for one stand across HTTP threads."""
        with self._job_lock:
            return self._stand_operation_locks.setdefault(stand_id, threading.RLock())

    def _assert_not_bulk_rollback_pending(self, stand_id: int) -> None:
        with self._bulk_rollback_lock:
            if int(stand_id) in self._bulk_rollback_pending:
                raise ConflictError("Стенд уже поставлен в очередь массового возврата к snapshot start")

    def integration(self) -> dict[str, Any]:
        info = self.gateway.integration_info()
        return {
            "mode": info.mode, "connected": info.connected, "host": info.host,
            "cluster": info.cluster, "message": info.message,
        }

    def list_templates(self) -> list[dict[str, Any]]:
        return self.gateway.list_templates()

    def list_pools(self) -> list[dict[str, Any]]:
        tracked: dict[str, list[dict[str, Any]]] = {}
        for row in self.store.query_all(
            "SELECT id, pool_id, origin, workspace FROM stands WHERE pool_id != '' ORDER BY id"
        ):
            tracked.setdefault(str(row["pool_id"]), []).append(row)
        pools = self.gateway.list_pools()
        for pool in pools:
            stands = tracked.get(str(pool["pool_id"]), [])
            stand_ids = [int(row["id"]) for row in stands]
            origins = {str(row.get("origin") or "deployed") for row in stands}
            pool["imported"] = bool(stands)
            pool["stand_id"] = stand_ids[0] if stand_ids else None
            pool["stand_ids"] = stand_ids
            pool["stand_count"] = len(stand_ids)
            pool["workspaces"] = sorted({str(row.get("workspace") or "demoexam") for row in stands})
            # A pool selected explicitly as an external/shared destination may
            # hold multiple independent Deployer stands. Pools wholly imported
            # into one card or created/owned by Deployer remain exclusive.
            pool["available_for_deploy"] = not (origins & {"imported", "deployed"})
        return pools

    @staticmethod
    def _canonical_ip(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            address = ipaddress.ip_interface(text).ip
        except ValueError:
            return ""
        return str(address) if address.version == 4 else ""

    @staticmethod
    def _ipam_plan(
        connection: Any,
        requested_cidr: str,
        start_ip: str,
        count: int,
    ) -> tuple[str, int, list[str]]:
        """Allocate IPv4 addresses atomically, retaining the entered host part.

        ``ip_network(..., strict=False)`` loses the host part of a CIDR.  An
        interface keeps it, so 10.39.4.0/16 starts at 10.39.4.0 instead of
        silently becoming 10.39.0.0.  Only the *actual* /16 network and
        broadcast addresses are skipped.
        """
        if not requested_cidr:
            if start_ip:
                raise ValidationError("Для начального IP укажите IPv4 CIDR")
            return "", 0, []
        try:
            interface = ipaddress.ip_interface(requested_cidr)
        except ValueError as exc:
            raise ValidationError(
                "Диапазон должен быть в формате IPv4 CIDR, например 10.39.4.0/16"
            ) from exc
        if interface.version != 4:
            raise ValidationError("Для развёртывания поддерживается только IPv4")
        network = interface.network
        if start_ip:
            try:
                start = ipaddress.ip_interface(start_ip).ip
            except ValueError as exc:
                raise ValidationError("Начальный IP должен быть корректным IPv4-адресом") from exc
            if start.version != 4 or start not in network:
                raise ValidationError("Начальный IP должен входить в указанный CIDR")
        else:
            start = interface.ip

        if network.prefixlen <= 30 and int(start) in {
            int(network.network_address), int(network.broadcast_address),
        }:
            # A boundary entered as the start is a pool hint, not an address
            # assignment.  In both cases begin at the first usable host.
            start = ipaddress.ip_address(int(network.network_address) + 1)

        used: set[str] = {
            str(row[0])
            for row in connection.execute("SELECT address FROM ipam_reservations").fetchall()
        }
        # Include legacy/imported VM rows even if an older installation could
        # not backfill them into IPAM because it already contained a duplicate.
        for row in connection.execute("SELECT ip FROM stand_vms WHERE trim(ip) != ''").fetchall():
            canonical = DashboardService._canonical_ip(row[0])
            if canonical:
                used.add(canonical)

        allocated: list[str] = []
        candidate = int(start)
        end = int(network.broadcast_address)
        actual_network = int(network.network_address)
        actual_broadcast = int(network.broadcast_address)
        while candidate <= end and len(allocated) < count:
            # RFC 3021 makes both /31 addresses usable; /32 is also a valid
            # single-host route.  For wider networks, skip only their genuine
            # network/broadcast boundaries.
            boundary = network.prefixlen <= 30 and candidate in {actual_network, actual_broadcast}
            address = str(ipaddress.ip_address(candidate))
            if not boundary and address not in used:
                allocated.append(address)
                used.add(address)
            candidate += 1
        if len(allocated) != count:
            raise ConflictError(
                "Начиная с указанного IP недостаточно свободных адресов в CIDR для всех VM"
            )
        return str(start), int(network.prefixlen), allocated

    def list_ipam(self) -> dict[str, Any]:
        reservations = self.store.query_all(
            """SELECT r.*, s.name AS stand_name, s.status AS stand_status,
                      v.vmid, v.name AS vm_name, v.node AS vm_node
            FROM ipam_reservations r
            JOIN stands s ON s.id = r.stand_id
            LEFT JOIN stand_vms v ON v.id = r.stand_vm_id
            ORDER BY r.requested_cidr, r.stand_id, r.vm_index"""
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for reservation in reservations:
            grouped.setdefault(str(reservation["requested_cidr"]), []).append(reservation)
        pools: list[dict[str, Any]] = []
        for requested_cidr, items in grouped.items():
            ordered = sorted(items, key=lambda item: int(ipaddress.ip_address(item["address"])))
            pools.append({
                "requested_cidr": requested_cidr,
                "prefix_length": int(items[0]["prefix_length"]),
                "first_address": ordered[0]["address"],
                "last_address": ordered[-1]["address"],
                "reservations": len(items),
                "assigned": sum(1 for item in items if item["status"] == "assigned"),
                "stands": len({int(item["stand_id"]) for item in items}),
            })
        assigned = sum(1 for item in reservations if item["status"] == "assigned")
        reserved = len(reservations) - assigned
        return {
            "summary": {
                "total": len(reservations),
                "reserved": reserved,
                "assigned": assigned,
                "stands": len({int(item["stand_id"]) for item in reservations}),
                "pools": len(pools),
            },
            "reservations": reservations,
            "pools": pools,
        }

    def list_blueprints(self) -> list[dict[str, Any]]:
        return self.store.query_all("SELECT * FROM blueprints ORDER BY status = 'draft', updated_at DESC")

    def get_blueprint(self, blueprint_id: int) -> dict[str, Any]:
        blueprint = self.store.query_one("SELECT * FROM blueprints WHERE id = ?", (blueprint_id,))
        if not blueprint:
            raise NotFoundError("Сценарий не найден")
        return blueprint

    def save_blueprint(self, payload: dict[str, Any], blueprint_id: int | None = None) -> dict[str, Any]:
        fields = {
            "code", "name", "description", "category", "version", "status", "template_vmid",
            "tags", "deploy_script", "autocheck_script", "checks_count",
        }
        data = {key: payload[key] for key in fields if key in payload}
        if blueprint_id is None and (not str(data.get("name", "")).strip() or not str(data.get("code", "")).strip()):
            raise ValidationError("Укажите название и код сценария")
        if "name" in data and not str(data["name"]).strip():
            raise ValidationError("Название сценария не может быть пустым")
        if "code" in data and not str(data["code"]).strip():
            raise ValidationError("Код сценария не может быть пустым")
        for field in ("template_vmid", "checks_count"):
            if field in data:
                try:
                    data[field] = int(data[field])
                except (TypeError, ValueError) as exc:
                    raise ValidationError(f"Поле {field} должно быть числом") from exc
        if "template_vmid" in data and data["template_vmid"] < 0:
            raise ValidationError("VMID шаблона не может быть отрицательным")
        if "checks_count" in data and not 0 <= data["checks_count"] <= 1000:
            raise ValidationError("Количество проверок должно быть от 0 до 1000")
        if "status" in data and data["status"] not in {"active", "draft", "archived"}:
            raise ValidationError("Неизвестный статус сценария")
        if "tags" in data:
            if not isinstance(data["tags"], list):
                data["tags"] = [part.strip() for part in str(data["tags"]).split(",") if part.strip()]
            data["tags"] = json.dumps(data["tags"], ensure_ascii=False)
        now = utc_now()
        if blueprint_id is None:
            defaults: dict[str, Any] = {
                "description": "", "category": "Общий", "version": "1.0", "status": "draft",
                "vm_count": 1, "template_vmid": 0, "clone_type": "linked", "storage": "",
                "bridge": "", "subnet": "", "estimated_minutes": 8,
                "tags": "[]", "deploy_script": "#!/usr/bin/env bash\nset -euo pipefail\n",
                "autocheck_script": "#!/usr/bin/env bash\nset -euo pipefail\n", "checks_count": 0,
            }
            defaults.update(data)
            columns = list(defaults) + ["updated_at", "created_at"]
            values = [defaults[column] for column in defaults] + [now, now]
            placeholders = ", ".join("?" for _ in values)
            new_id = self.store.execute(
                f"INSERT INTO blueprints ({', '.join(columns)}) VALUES ({placeholders})", tuple(values),
            )
            self.store.add_activity("script", "Создан сценарий", str(defaults["name"]), "success")
            return self.get_blueprint(new_id)
        self.get_blueprint(blueprint_id)
        if not data:
            return self.get_blueprint(blueprint_id)
        data["updated_at"] = now
        assignments = ", ".join(f"{column} = ?" for column in data)
        self.store.execute(f"UPDATE blueprints SET {assignments} WHERE id = ?", tuple(data.values()) + (blueprint_id,))
        updated = self.get_blueprint(blueprint_id)
        self.store.add_activity("script", "Сценарий обновлён", updated["name"], "success")
        return updated

    def duplicate_blueprint(self, blueprint_id: int) -> dict[str, Any]:
        source = self.get_blueprint(blueprint_id)
        payload = {key: value for key, value in source.items() if key not in {"id", "created_at", "updated_at"}}
        payload["name"] = f"{source['name']} · копия"
        payload["code"] = f"{source['code']}-COPY"
        payload["version"] = "1.0"
        payload["status"] = "draft"
        return self.save_blueprint(payload)

    def delete_blueprint(self, blueprint_id: int) -> None:
        blueprint = self.get_blueprint(blueprint_id)
        count = self.store.query_one("SELECT COUNT(*) AS count FROM stands WHERE blueprint_id = ?", (blueprint_id,))
        if count and int(count["count"]) > 0:
            raise ConflictError("Сценарий используется стендами. Сначала удалите или переназначьте их")
        self.store.execute("DELETE FROM blueprints WHERE id = ?", (blueprint_id,))
        self.store.add_activity("script", "Сценарий удалён", blueprint["name"], "warning")

    def list_stands(self) -> list[dict[str, Any]]:
        sql = """
        SELECT s.*, b.name AS blueprint_name, b.code AS blueprint_code, b.category AS blueprint_category,
               (SELECT COUNT(*) FROM stand_vms v WHERE v.stand_id = s.id) AS actual_vm_count
        FROM stands s LEFT JOIN blueprints b ON b.id = s.blueprint_id
        ORDER BY CASE s.status WHEN 'provisioning' THEN 0 WHEN 'resetting' THEN 0 WHEN 'error' THEN 1 WHEN 'running' THEN 2 ELSE 3 END,
                 s.updated_at DESC
        """
        stands = self.store.query_all(sql)
        with self._bulk_rollback_lock:
            pending = set(self._bulk_rollback_pending)
        for stand in stands:
            stand["bulk_rollback_pending"] = int(stand["id"]) in pending
        return stands

    def get_stand(self, stand_id: int) -> dict[str, Any]:
        stand = next((item for item in self.list_stands() if int(item["id"]) == stand_id), None)
        if not stand:
            raise NotFoundError("Стенд не найден")
        raw_vms = self.store.query_all("SELECT * FROM stand_vms WHERE stand_id = ? ORDER BY id", (stand_id,))
        stand["vms"] = [self._public_vm(vm) for vm in raw_vms]
        stand["checks"] = self.store.query_all("SELECT * FROM check_runs WHERE stand_id = ? ORDER BY started_at DESC LIMIT 10", (stand_id,))
        return stand

    @staticmethod
    def _vm_web_url(ip: str) -> str:
        """Build a password-free URL to the stand web UI.

        Installations whose nested Proxmox UI is exposed through HTTPS without
        port 8006 can set, for example, STAND_WEB_URL_TEMPLATE=https://{ip}/.
        Credentials are deliberately never embedded in this URL.
        """
        host = str(ip or "").strip().split("/", 1)[0]
        if not host:
            return ""
        scheme = os.environ.get("STAND_WEB_SCHEME", "https").strip().lower()
        if scheme not in {"http", "https"}:
            scheme = "https"
        port = os.environ.get("STAND_WEB_PORT", "8006").strip()
        if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
            port = "8006"
        authority = host if not port else f"{host}:{port}"
        default_url = f"{scheme}://{authority}/"
        template = os.environ.get("STAND_WEB_URL_TEMPLATE", "").strip()
        if not template:
            return default_url
        try:
            url = template.format(ip=host)
        except (KeyError, ValueError):
            url = default_url
        return url if url.startswith(("https://", "http://")) else default_url

    def _public_vm(self, vm: dict[str, Any]) -> dict[str, Any]:
        item = dict(vm)
        secret = str(item.pop("credential_password", "") or "")
        credential_valid = bool(item.pop("credential_valid", 1))
        guest_username = str(item.pop("credential_username", "root") or "root")
        web_username = str(item.pop("web_username", "root@pam") or "root@pam")
        web_url = self._vm_web_url(str(item.get("ip", "")))
        item.update({
            "username": web_username,
            "web_username": web_username,
            "guest_username": guest_username,
            "credential_available": bool(secret) and credential_valid,
            "credential_recoverable": bool(secret),
            "has_start_snapshot": bool(item.get("has_start_snapshot")),
            "web_url": web_url,
            "access_url": web_url,
        })
        return item

    def stand_credentials(self, stand_id: int) -> dict[str, Any]:
        stand = self.get_stand(stand_id)
        rows = self.store.query_all("SELECT * FROM stand_vms WHERE stand_id = ? ORDER BY id", (stand_id,))
        return {
            "stand_id": stand_id,
            "stand_name": stand["name"],
            "credentials": [self._credential(row) for row in rows],
            "auto_login_supported": False,
            "auto_login_message": (
                "Браузер не разрешает дашборду установить cookie на другом домене. "
                "Откройте web_url и используйте указанные логин и пароль."
            ),
        }

    def vm_credentials(self, stand_id: int, vmid: int) -> dict[str, Any]:
        self.get_stand(stand_id)
        row = self.store.query_one(
            "SELECT * FROM stand_vms WHERE stand_id = ? AND vmid = ?", (stand_id, vmid),
        )
        if not row:
            raise NotFoundError("VM не найдена в этом стенде")
        return self._credential(row)

    def _credential(self, vm: dict[str, Any]) -> dict[str, Any]:
        credential_valid = bool(vm.get("credential_valid", 1))
        stored_password = str(vm.get("credential_password") or "")
        return {
            "vmid": vm.get("vmid"),
            "name": vm.get("name", ""),
            "ip": vm.get("ip", ""),
            "web_url": self._vm_web_url(str(vm.get("ip", ""))),
            "access_url": self._vm_web_url(str(vm.get("ip", ""))),
            "username": str(vm.get("web_username") or "root@pam"),
            "web_username": str(vm.get("web_username") or "root@pam"),
            "guest_username": str(vm.get("credential_username") or "root"),
            "password": stored_password if credential_valid else "",
            "credential_available": bool(stored_password) and credential_valid,
            "credential_recoverable": bool(stored_password),
            "password_updated_at": vm.get("password_updated_at"),
            "reveal_once": False,
        }

    @staticmethod
    def _pool_id(value: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-.").lower()
        if not value:
            value = f"exam-{int(time.time())}"
        return value[:48]

    @staticmethod
    def _workspace(value: Any) -> str:
        workspace = str(value or "demoexam").strip().lower()
        if workspace not in {"demoexam", "mdk02.01", "mdk.03.02"}:
            raise ValidationError("Неизвестная рабочая область")
        return workspace

    def import_pool(self, payload: dict[str, Any]) -> dict[str, Any]:
        pool_id = str(payload.get("pool_id", "")).strip()
        if not pool_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", pool_id):
            raise ValidationError("Выберите существующий Proxmox pool")
        if self.store.query_one("SELECT id FROM stands WHERE pool_id = ?", (pool_id,)):
            raise ConflictError("Этот pool уже добавлен в дашборд")
        raw_blueprint_id = payload.get("blueprint_id")
        blueprint_id: int | None = None
        blueprint: dict[str, Any] | None = None
        if raw_blueprint_id not in (None, ""):
            try:
                blueprint_id = int(raw_blueprint_id)
            except (TypeError, ValueError) as exc:
                raise ValidationError("Некорректный сценарий импортируемого pool") from exc
            blueprint = self.get_blueprint(blueprint_id)
        members = self.gateway.pool_members(pool_id)
        if not members:
            raise ConflictError("В выбранном pool нет QEMU VM, доступных для добавления")
        vmids = [int(member["vmid"]) for member in members]
        placeholders = ",".join("?" for _ in vmids)
        tracked = self.store.query_all(
            f"SELECT vmid FROM stand_vms WHERE vmid IN ({placeholders})",
            tuple(vmids),
        )
        if tracked:
            values = ", ".join(str(row["vmid"]) for row in tracked)
            raise ConflictError(f"Некоторые VM уже закреплены за другим стендом: {values}")
        name = str(payload.get("name", "")).strip() or pool_id
        owner = str(payload.get("owner", "Администратор")).strip() or "Администратор"
        workspace = self._workspace(payload.get("workspace"))
        statuses = {str(member.get("status", "stopped")) for member in members}
        status = "running" if "running" in statuses else "stopped"
        nodes = sorted({str(member.get("node", "")) for member in members if member.get("node")})
        node_label = ", ".join(nodes)
        cpu = round(sum(float(member.get("cpu") or 0) for member in members), 1)
        ram_values = [float(member.get("ram") or 0) for member in members]
        ram = round(sum(ram_values) / max(len(ram_values), 1), 1)
        member_addresses = [self._canonical_ip(member.get("ip", "")) for member in members]
        nonempty_addresses = [address for address in member_addresses if address]
        if len(nonempty_addresses) != len(set(nonempty_addresses)):
            raise ConflictError("В импортируемом pool один IP указан у нескольких VM")
        now = utc_now()
        with self.store.transaction() as connection:
            for address in nonempty_addresses:
                if connection.execute(
                    "SELECT stand_id FROM ipam_reservations WHERE address = ?", (address,),
                ).fetchone():
                    raise ConflictError(f"IP {address} уже зарезервирован другим стендом")
            cursor = connection.execute(
                """INSERT INTO stands
                (name, blueprint_id, status, progress, node, pool_id, workspace, owner,
                 vm_count, cpu, ram, disk, ip_range, check_status, origin,
                 created_at, updated_at)
                VALUES (?, ?, ?, 100, ?, ?, ?, ?, ?, ?, ?, 0, '', 'idle', 'imported', ?, ?)""",
                (name, blueprint_id, status, node_label, pool_id, workspace, owner,
                 len(members), cpu, ram, now, now),
            )
            stand_id = int(cursor.lastrowid)
            for index, member in enumerate(members, 1):
                address = member_addresses[index - 1]
                vm_cursor = connection.execute(
                    """INSERT INTO stand_vms (stand_id, vmid, name, node, ip, status, cpu, ram)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (stand_id, member["vmid"], member["name"], member.get("node", ""),
                     address or member.get("ip", ""), member.get("status", "stopped"),
                     member.get("cpu", 0), member.get("ram", 0)),
                )
                if address:
                    connection.execute(
                        """INSERT INTO ipam_reservations
                        (stand_id, stand_vm_id, address, requested_cidr, prefix_length,
                         vm_index, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, 32, ?, 'assigned', ?, ?)""",
                        (stand_id, int(vm_cursor.lastrowid), address, f"{address}/32",
                         index, now, now),
                    )
        activity_detail = f"{pool_id} · {len(members)} VM"
        if blueprint:
            activity_detail += f" · {blueprint['name']}"
        self.store.add_activity("import", "Существующий pool добавлен", activity_detail, "success")
        return self.get_stand(stand_id)

    def create_stand(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            blueprint_id = int(payload.get("blueprint_id"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("Выберите сценарий развёртывания") from exc
        blueprint = self.get_blueprint(blueprint_id)
        if blueprint["status"] != "active":
            raise ConflictError("Развернуть можно только активный сценарий")
        name = str(payload.get("name", "")).strip()
        if not name:
            raise ValidationError("Укажите название стенда")
        use_existing_value = payload.get("use_existing_pool", False)
        use_existing_pool = use_existing_value is True or str(use_existing_value).strip().lower() in {
            "1", "true", "yes", "on",
        }
        raw_pool_id = str(payload.get("pool_id", "")).strip()
        if use_existing_pool and not raw_pool_id:
            raise ValidationError("Выберите существующий Proxmox pool")
        if use_existing_pool:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", raw_pool_id):
                raise ValidationError("Выберите существующий Proxmox pool")
            # Proxmox pool IDs are case-sensitive. Existing IDs come from the
            # API and must not pass through _pool_id(), which intentionally
            # normalizes newly-created pool names to lowercase.
            pool_id = raw_pool_id
            existing_pool_ids = {
                str(pool.get("pool_id") or "").strip()
                for pool in self.gateway.list_pools()
            }
            if pool_id not in existing_pool_ids:
                raise ValidationError(f"Существующий Proxmox pool {pool_id} не найден")
        else:
            pool_id = self._pool_id(raw_pool_id or f"exam-{int(time.time())}")
        try:
            vm_count = int(payload.get("vm_count", 1))
        except (TypeError, ValueError) as exc:
            raise ValidationError("Количество VM должно быть числом") from exc
        if not 1 <= vm_count <= 50:
            raise ValidationError("Количество VM должно быть от 1 до 50")
        subnet = str(payload.get("subnet", "")).strip()
        start_ip = str(payload.get("start_ip", "")).strip()
        bridge = str(payload.get("bridge", "")).strip()
        if bridge and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", bridge):
            raise ValidationError("Некорректное имя сетевого bridge")
        credential_username = str(payload.get("username", "root")).strip() or "root"
        if not re.fullmatch(r"[^\x00-\x1f\x7f]{1,64}", credential_username):
            raise ValidationError("Некорректное имя пользователя гостевой VM")
        web_username = str(payload.get("web_username", "root@pam")).strip() or "root@pam"
        if not re.fullmatch(r"[^\x00-\x1f\x7f]{1,128}", web_username):
            raise ValidationError("Некорректный логин веб-интерфейса VM")
        credentials = [
            {
                "guest_username": credential_username,
                "web_username": web_username,
                "password": self._password(),
            }
            for _ in range(vm_count)
        ]
        now = utc_now()
        requested_node = str(payload.get("node", "auto"))
        owner = str(payload.get("owner", "Администратор"))
        workspace = self._workspace(payload.get("workspace"))
        # The stand, placeholder VM rows and addresses are committed together.
        # Concurrent requests therefore cannot reserve the same address.
        with self.store.transaction() as connection:
            tracked_pool_rows = connection.execute(
                "SELECT id, origin FROM stands WHERE pool_id = ?", (pool_id,),
            ).fetchall()
            if use_existing_pool:
                if any(str(row["origin"] or "deployed") != "existing" for row in tracked_pool_rows):
                    raise ConflictError(
                        "Этот pool целиком подключён к другому стенду или управляется Deployer"
                    )
            elif tracked_pool_rows:
                raise ConflictError("Pool ID уже используется")
            canonical_start, prefix_length, allocated_ips = self._ipam_plan(
                connection, subnet, start_ip, vm_count,
            )
            cursor = connection.execute(
                """INSERT INTO stands
                (name, blueprint_id, status, progress, node, pool_id, workspace, owner,
                 vm_count, cpu, ram, disk, ip_range, ip_start, check_status,
                 expires_at, origin, created_at, updated_at)
                VALUES (?, ?, 'provisioning', 4, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, 'idle', ?, ?, ?, ?)""",
                (name, blueprint_id, requested_node, pool_id, workspace, owner, vm_count,
                 subnet, canonical_start, None,
                 "existing" if use_existing_pool else "deployed", now, now),
            )
            stand_id = int(cursor.lastrowid)
            for index in range(1, vm_count + 1):
                ip = allocated_ips[index - 1] if index <= len(allocated_ips) else ""
                credential = credentials[index - 1]
                vm_name = (
                    existing_pool_vm_name(pool_id, stand_id, index)
                    if use_existing_pool else f"{pool_id}-{index}"
                )
                vm_cursor = connection.execute(
                    """INSERT INTO stand_vms
                    (stand_id, vmid, name, node, ip, status, cpu, ram,
                     credential_username, web_username, credential_password,
                     password_updated_at, check_status)
                    VALUES (?, NULL, ?, ?, ?, 'provisioning', 0, 0, ?, ?, ?, NULL, 'idle')""",
                    (stand_id, vm_name, requested_node, ip,
                     credential["guest_username"], credential["web_username"],
                     credential["password"]),
                )
                if ip:
                    connection.execute(
                        """INSERT INTO ipam_reservations
                        (stand_id, stand_vm_id, address, requested_cidr,
                         prefix_length, vm_index, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?)""",
                        (stand_id, int(vm_cursor.lastrowid), ip, subnet,
                         prefix_length, index, now, now),
                    )
        deployment = dict(blueprint)
        deployment.update({
            "vm_count": vm_count,
            "subnet": subnet,
            "start_ip": canonical_start,
            "allocated_ips": allocated_ips,
            "bridge": bridge,
            "clone_type": "linked",
            "storage": "",
            "credentials": credentials,
            "use_existing_pool": use_existing_pool,
        })
        self.store.add_activity("deploy", "Развёртывание запущено", f"{name} · {blueprint['name']} · {vm_count} VM", "progress")
        thread = threading.Thread(
            target=self._deploy_job,
            args=(stand_id, deployment),
            name=f"deploy-{stand_id}",
            daemon=True,
        )
        with self._job_lock:
            self._jobs[stand_id] = thread
        thread.start()
        return self.get_stand(stand_id)

    def update_stand(self, stand_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self._assert_not_bulk_rollback_pending(stand_id)
        stand = self.get_stand(stand_id)
        if "ip_range" in payload and str(payload["ip_range"]).strip() != str(stand.get("ip_range") or "").strip():
            raise ValidationError(
                "Диапазон работающего стенда управляется IPAM и не редактируется как метаданные. "
                "Для другой адресации разверните новый стенд."
            )
        allowed = {"name", "owner"}
        data = {key: payload[key] for key in allowed if key in payload}
        if "name" in data and not str(data["name"]).strip():
            raise ValidationError("Название стенда не может быть пустым")
        raw_vm_ips = payload.get("vm_ips")
        vm_ip_updates: dict[int, str] | None = None
        if raw_vm_ips is not None:
            if not isinstance(raw_vm_ips, dict):
                raise ValidationError("Адреса VM должны быть переданы как объект VMID → IPv4")
            vm_ip_updates = {}
            for raw_vmid, raw_ip in raw_vm_ips.items():
                try:
                    vmid = int(raw_vmid)
                except (TypeError, ValueError) as exc:
                    raise ValidationError("Некорректный VMID в списке адресов") from exc
                text = str(raw_ip or "").strip()
                address = self._canonical_ip(text)
                if text and not address:
                    raise ValidationError(f"Для VM {vmid} указан некорректный IPv4-адрес")
                vm_ip_updates[vmid] = address
        if not data and vm_ip_updates is None:
            return stand

        now = utc_now()
        with self.store.transaction() as connection:
            vm_rows = connection.execute(
                "SELECT * FROM stand_vms WHERE stand_id = ? ORDER BY id", (stand_id,),
            ).fetchall()
            by_vmid = {
                int(row["vmid"]): row for row in vm_rows if row["vmid"] is not None
            }
            if vm_ip_updates is not None:
                unknown = sorted(set(vm_ip_updates) - set(by_vmid))
                if unknown:
                    raise ValidationError(
                        "VM не принадлежат этому стенду: " + ", ".join(map(str, unknown))
                    )
                desired = {
                    vmid: self._canonical_ip(row["ip"]) for vmid, row in by_vmid.items()
                }
                desired.update(vm_ip_updates)
                addresses = [address for address in desired.values() if address]
                if len(addresses) != len(set(addresses)):
                    raise ConflictError("В одном стенде нельзя назначить одинаковый IP нескольким VM")
                occupied: set[str] = set()
                for row in connection.execute(
                    "SELECT ip FROM stand_vms WHERE stand_id != ? AND trim(ip) != ''", (stand_id,),
                ).fetchall():
                    address = self._canonical_ip(row["ip"])
                    if address:
                        occupied.add(address)
                occupied.update(
                    str(row["address"])
                    for row in connection.execute(
                        "SELECT address FROM ipam_reservations WHERE stand_id != ?", (stand_id,),
                    ).fetchall()
                )
                conflicts = sorted(set(addresses) & occupied, key=ipaddress.ip_address)
                if conflicts:
                    raise ConflictError("IP уже используется другим стендом: " + ", ".join(conflicts))

                reservations = connection.execute(
                    "SELECT * FROM ipam_reservations WHERE stand_id = ? ORDER BY vm_index", (stand_id,),
                ).fetchall()
                reservations_by_vm = {
                    int(row["stand_vm_id"]): row
                    for row in reservations if row["stand_vm_id"] is not None
                }
                connection.execute("DELETE FROM ipam_reservations WHERE stand_id = ?", (stand_id,))
                for index, row in enumerate(vm_rows, 1):
                    if row["vmid"] is None:
                        continue
                    vmid = int(row["vmid"])
                    address = desired.get(vmid, "")
                    connection.execute(
                        "UPDATE stand_vms SET ip = ? WHERE id = ?", (address, int(row["id"])),
                    )
                    if not address:
                        continue
                    previous = reservations_by_vm.get(int(row["id"]))
                    requested_cidr = str(previous["requested_cidr"]) if previous else f"{address}/32"
                    prefix_length = int(previous["prefix_length"]) if previous else 32
                    vm_index = int(previous["vm_index"]) if previous else index
                    created_at = str(previous["created_at"]) if previous else now
                    connection.execute(
                        """INSERT INTO ipam_reservations
                        (stand_id, stand_vm_id, address, requested_cidr, prefix_length,
                         vm_index, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'assigned', ?, ?)""",
                        (stand_id, int(row["id"]), address, requested_cidr,
                         prefix_length, vm_index, created_at, now),
                    )

            data["updated_at"] = now
            connection.execute(
                f"UPDATE stands SET {', '.join(f'{key} = ?' for key in data)} WHERE id = ?",
                tuple(data.values()) + (stand_id,),
            )
        detail = str(data.get("name", stand["name"]))
        if vm_ip_updates is not None:
            detail += f" · адреса Deployer: {len(vm_ip_updates)} VM"
        self.store.add_activity("edit", "Параметры стенда обновлены", detail, "success")
        return self.get_stand(stand_id)

    def _deploy_job(self, stand_id: int, blueprint: dict[str, Any]) -> None:
        heartbeat_stop = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        try:
            stand = self.get_stand(stand_id)
            blueprint = dict(blueprint)
            vm_count = int(blueprint.get("vm_count") or stand.get("vm_count") or 1)
            credentials = blueprint.get("credentials")
            if not isinstance(credentials, list) or len(credentials) < vm_count:
                blueprint["credentials"] = [
                    {"guest_username": "root", "web_username": "root@pam", "password": self._password()}
                    for _ in range(vm_count)
                ]
            if not isinstance(blueprint.get("allocated_ips"), list):
                blueprint["allocated_ips"] = [
                    row["address"]
                    for row in self.store.query_all(
                        "SELECT address FROM ipam_reservations WHERE stand_id = ? ORDER BY vm_index",
                        (stand_id,),
                    )
                ]

            progress_lock = threading.Lock()
            progress_value = max(1, min(int(stand.get("progress") or 1), 99))

            def progress(value: int, message: str) -> None:
                nonlocal progress_value
                with progress_lock:
                    progress_value = max(progress_value, min(int(value), 100))
                    stored_value = progress_value
                self.store.execute("UPDATE stands SET progress = ?, updated_at = ? WHERE id = ?", (stored_value, utc_now(), stand_id))
                if value in {31, 72}:
                    self.store.add_activity("deploy", message, stand["name"], "progress", "Система")

            def keep_progress_alive() -> None:
                # Some Proxmox discovery and task endpoints are synchronous
                # and can take tens of seconds without an intermediate UPID.
                # Keep the estimated UI progress moving, but leave the final
                # four percent to confirmed gateway stages and completion.
                nonlocal progress_value
                while not heartbeat_stop.wait(3):
                    with progress_lock:
                        if progress_value >= 96:
                            continue
                        progress_value += 1
                        stored_value = progress_value
                    self.store.execute(
                        "UPDATE stands SET progress = ?, updated_at = ? WHERE id = ? AND status = 'provisioning'",
                        (stored_value, utc_now(), stand_id),
                    )

            heartbeat_thread = threading.Thread(
                target=keep_progress_alive,
                name=f"deploy-progress-{stand_id}",
                daemon=True,
            )
            heartbeat_thread.start()
            try:
                vms = self.gateway.deploy(stand, blueprint, progress)
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=1)
            validation_error = ""
            normalized_vms: list[dict[str, Any]] = []
            if not isinstance(vms, list):
                validation_error = "gateway вернул результат не в виде списка"
            else:
                indexes: list[int] = []
                vmids: list[int] = []
                try:
                    for ordinal, raw_vm in enumerate(vms, 1):
                        if not isinstance(raw_vm, dict):
                            raise ValueError(f"элемент {ordinal} не является объектом VM")
                        vm = dict(raw_vm)
                        vm_index = int(vm.get("index") or ordinal)
                        vmid = int(vm.get("vmid"))
                        if vmid <= 0:
                            raise ValueError(f"VM #{vm_index} вернула некорректный VMID")
                        if not str(vm.get("name") or "").strip():
                            raise ValueError(f"VM #{vm_index} вернула пустое имя")
                        vm["index"] = vm_index
                        vm["vmid"] = vmid
                        indexes.append(vm_index)
                        vmids.append(vmid)
                        normalized_vms.append(vm)
                    expected_indexes = set(range(1, vm_count + 1))
                    if len(normalized_vms) != vm_count:
                        raise ValueError(
                            f"ожидалось {vm_count} VM, получено {len(normalized_vms)}"
                        )
                    if set(indexes) != expected_indexes or len(indexes) != len(set(indexes)):
                        raise ValueError("gateway вернул неполный или повторяющийся набор индексов VM")
                    if len(vmids) != len(set(vmids)):
                        raise ValueError("gateway вернул повторяющиеся VMID")
                except (TypeError, ValueError) as exc:
                    validation_error = str(exc)
            if validation_error:
                cleanup_vmids: list[int] = []
                for raw_vm in vms if isinstance(vms, list) else []:
                    try:
                        vmid = int(raw_vm.get("vmid")) if isinstance(raw_vm, dict) else 0
                    except (TypeError, ValueError):
                        vmid = 0
                    if vmid > 0 and vmid not in cleanup_vmids:
                        cleanup_vmids.append(vmid)
                cleanup_vmids.sort()
                cleanup_error = ""
                try:
                    self.gateway.delete_stand(stand, cleanup_vmids)
                except Exception as exc:
                    cleanup_error = f"; автоматический откат также завершился ошибкой: {exc}"
                raise RuntimeError(f"Неполный результат развёртывания: {validation_error}{cleanup_error}")
            vms = normalized_vms
            with self.store.transaction() as connection:
                placeholder_rows = connection.execute(
                    "SELECT * FROM stand_vms WHERE stand_id = ? ORDER BY id", (stand_id,),
                ).fetchall()
                reservations = {
                    int(row["vm_index"]): row
                    for row in connection.execute(
                        "SELECT * FROM ipam_reservations WHERE stand_id = ? ORDER BY vm_index",
                        (stand_id,),
                    ).fetchall()
                }
                for ordinal, vm in enumerate(vms, 1):
                    vm_index = int(vm.get("index") or ordinal)
                    reservation = reservations.get(vm_index)
                    stand_vm_id = (
                        int(reservation["stand_vm_id"])
                        if reservation and reservation["stand_vm_id"] is not None
                        else int(placeholder_rows[vm_index - 1]["id"])
                        if vm_index <= len(placeholder_rows)
                        else 0
                    )
                    assigned_ip = str(reservation["address"]) if reservation else str(vm.get("ip", ""))
                    values = (
                        vm.get("vmid"), vm["name"], vm.get("node", ""), assigned_ip,
                        vm.get("status", "running"),
                        vm.get("guest_username", vm.get("username", "root")),
                        vm.get("web_username", "root@pam"), vm.get("password", ""),
                        vm.get("password_updated_at"), vm.get("last_snapshot", "start"),
                        1 if vm.get("last_snapshot", "start") == "start" else 0,
                    )
                    if stand_vm_id:
                        connection.execute(
                            """UPDATE stand_vms SET vmid = ?, name = ?, node = ?, ip = ?,
                            status = ?, cpu = 0, ram = 0, credential_username = ?,
                            web_username = ?, credential_password = ?, credential_valid = 1,
                            password_updated_at = ?,
                            last_snapshot = ?, has_start_snapshot = ? WHERE id = ?""",
                            values + (stand_vm_id,),
                        )
                    else:
                        cursor = connection.execute(
                            """INSERT INTO stand_vms
                            (stand_id, vmid, name, node, ip, status, cpu, ram,
                             credential_username, web_username, credential_password, credential_valid,
                             password_updated_at, last_snapshot, has_start_snapshot)
                            VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, 1, ?, ?, ?)""",
                            (stand_id,) + values,
                        )
                        stand_vm_id = int(cursor.lastrowid)
                    if reservation:
                        connection.execute(
                            """UPDATE ipam_reservations SET stand_vm_id = ?, status = 'assigned',
                            updated_at = ? WHERE id = ?""",
                            (stand_vm_id, utc_now(), reservation["id"]),
                        )
                    elif self._canonical_ip(assigned_ip):
                        address = self._canonical_ip(assigned_ip)
                        requested_cidr = str(stand.get("ip_range") or "").strip()
                        try:
                            requested_interface = ipaddress.ip_interface(requested_cidr)
                            if ipaddress.ip_address(address) not in requested_interface.network:
                                raise ValueError
                        except ValueError:
                            requested_cidr = str(blueprint.get("subnet") or "").strip()
                            try:
                                requested_interface = ipaddress.ip_interface(requested_cidr)
                                if ipaddress.ip_address(address) not in requested_interface.network:
                                    raise ValueError
                            except ValueError:
                                requested_cidr = f"{address}/32"
                                requested_interface = ipaddress.ip_interface(requested_cidr)
                        connection.execute(
                            """INSERT INTO ipam_reservations
                            (stand_id, stand_vm_id, address, requested_cidr,
                             prefix_length, vm_index, status, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, 'assigned', ?, ?)""",
                            (stand_id, stand_vm_id, address, requested_cidr,
                             requested_interface.network.prefixlen, vm_index,
                             utc_now(), utc_now()),
                        )
            primary_node = vms[0].get("node", "") if vms else ""
            password_updated_at = utc_now() if any(vm.get("password") for vm in vms) else None
            self.store.execute(
                """UPDATE stands SET status = 'running', progress = 100, node = ?, cpu = 4.8,
                ram = 8.2, last_error = '', password_updated_at = ?, expires_at = NULL,
                updated_at = ? WHERE id = ?""",
                (primary_node, password_updated_at, utc_now(), stand_id),
            )
            self.store.add_activity(
                "deploy", "Стенд развёрнут", f"{stand['name']} · {vm_count} VM", "success", "Система",
            )
        except Exception as exc:
            heartbeat_stop.set()
            if heartbeat_thread and heartbeat_thread.is_alive():
                heartbeat_thread.join(timeout=1)
            self.store.execute(
                "UPDATE stands SET status = 'error', last_error = ?, updated_at = ? WHERE id = ?",
                (str(exc)[-1000:], utc_now(), stand_id),
            )
            self.store.execute(
                "UPDATE stand_vms SET status = 'error' WHERE stand_id = ? AND vmid IS NULL",
                (stand_id,),
            )
            self.store.add_activity("deploy", "Ошибка развёртывания", f"Стенд #{stand_id}: {exc}", "error", "Система")
        finally:
            with self._job_lock:
                self._jobs.pop(stand_id, None)

    def stand_action(
        self,
        stand_id: int,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        _bulk_reserved: bool = False,
    ) -> dict[str, Any]:
        if not _bulk_reserved:
            self._assert_not_bulk_rollback_pending(stand_id)
        with self._stand_operation_lock(stand_id):
            return self._stand_action_locked(stand_id, action, payload)

    def _stand_action_locked(self, stand_id: int, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        stand = self.get_stand(stand_id)
        vmids = [int(vm["vmid"]) for vm in stand["vms"] if vm.get("vmid") is not None]
        if stand["status"] in {"provisioning", "resetting"}:
            label = "развёртывания" if stand["status"] == "provisioning" else "возврата к исходному состоянию"
            raise ConflictError(f"Действие недоступно во время {label}")
        with self._job_lock:
            if stand_id in self._jobs:
                raise ConflictError("Для стенда уже выполняется фоновая операция")
        if action == "rollback_start":
            raw_vms = self.store.query_all(
                "SELECT * FROM stand_vms WHERE stand_id = ? ORDER BY id", (stand_id,),
            )
            return self._enqueue_rollback_start(stand, raw_vms, "все VM")
        if action in {"start", "stop", "restart"}:
            if not vmids:
                raise ConflictError("В стенде нет VM для управления питанием")
            self.gateway.power_action(vmids, action)
            new_status = "stopped" if action == "stop" else "running"
            self.store.execute("UPDATE stands SET status = ?, cpu = ?, updated_at = ? WHERE id = ?", (new_status, 0 if new_status == "stopped" else max(float(stand["cpu"]), 7.2), utc_now(), stand_id))
            self.store.execute("UPDATE stand_vms SET status = ? WHERE stand_id = ?", (new_status, stand_id))
            labels = {"start": "Стенд запущен", "stop": "Стенд остановлен", "restart": "Стенд перезапущен"}
            self.store.add_activity("power", labels[action], stand["name"], "success")
            return {"stand": self.get_stand(stand_id), "message": labels[action]}
        if action == "snapshot":
            label = self._snapshot_label(payload.get("name"))
            if not vmids:
                raise ConflictError("В стенде нет VM для создания снимка")
            description = str(payload.get("description", ""))[:255]
            self.gateway.create_snapshot(vmids, label, description)
            self.store.execute(
                """UPDATE stand_vms SET last_snapshot = ?,
                has_start_snapshot = CASE WHEN ? = 'start' THEN 1 ELSE has_start_snapshot END
                WHERE stand_id = ?""",
                (label, label, stand_id),
            )
            self.store.add_activity("snapshot", "Создан снимок", f"{stand['name']} · {label}", "success")
            return {"stand": self.get_stand(stand_id), "message": f"Снимок «{label}» создан на всех VM"}
        if action == "delete_pool_stands":
            if str(stand.get("origin") or "") != "imported":
                raise ValidationError("Удаление всех VM доступно только для подключённой карточки pool")
            if stand.get("check_status") == "running" or any(
                vm.get("check_status") == "running" for vm in stand.get("vms", [])
            ):
                raise ConflictError("Сначала дождитесь завершения автопроверки")
            members = self.gateway.pool_members(str(stand["pool_id"]))
            current_vms = [
                dict(vm) for vm in members
                if vm.get("vmid") is not None
            ]
            current_vmids = [int(vm["vmid"]) for vm in current_vms]
            if not current_vmids:
                raise ConflictError("В pool нет VM для удаления")
            deletion_scope = {**stand, "origin": "existing", "vms": current_vms}
            self.gateway.delete_stand(deletion_scope, current_vmids)
            self.store.execute("DELETE FROM stand_vms WHERE stand_id = ?", (stand_id,))
            self.store.execute(
                """UPDATE stands SET status = 'stopped', progress = 100, vm_count = 0,
                cpu = 0, ram = 0, disk = 0, last_error = '', updated_at = ? WHERE id = ?""",
                (utc_now(), stand_id),
            )
            self.store.add_activity(
                "delete", "Все VM pool удалены",
                f"{stand['pool_id']} · удалено {len(current_vmids)} VM", "warning",
            )
            return {
                "stand": self.get_stand(stand_id),
                "message": f"Удалено VM: {len(current_vmids)}; pool сохранён",
                "deleted_count": len(current_vmids),
            }
        if action == "rotate_password":
            username = str(payload.get("username", "root")).strip()
            web_username = str(payload.get("web_username", "root@pam")).strip() or "root@pam"
            password = str(payload.get("password", "")) or self._password()
            if stand["status"] != "running":
                raise ConflictError("Смена пароля доступна только для запущенного стенда")
            if not re.fullmatch(r"[^\x00-\x1f\x7f]{1,64}", username):
                raise ValidationError("Укажите имя пользователя")
            if not re.fullmatch(r"[^\x00-\x1f\x7f]{1,128}", web_username):
                raise ValidationError("Некорректный логин веб-интерфейса VM")
            if len(password) > PROXMOX_PASSWORD_MAX_LENGTH:
                raise ValidationError(
                    f"Proxmox принимает пароль длиной не более {PROXMOX_PASSWORD_MAX_LENGTH} символов"
                )
            if not vmids:
                raise ConflictError("В стенде нет VM для смены пароля")
            self.gateway.rotate_password(vmids, username, password)
            changed_at = utc_now()
            self.store.execute(
                """UPDATE stand_vms SET credential_username = ?, web_username = ?,
                credential_password = ?, credential_valid = 1,
                password_updated_at = ? WHERE stand_id = ?""",
                (username, web_username, password, changed_at, stand_id),
            )
            self.store.execute(
                "UPDATE stands SET password_updated_at = ?, updated_at = ? WHERE id = ?",
                (changed_at, changed_at, stand_id),
            )
            self.store.add_activity("password", "Пароль стенда обновлён", f"{stand['name']} · пользователь {username}", "success")
            return {
                "stand": self.get_stand(stand_id),
                "message": "Пароль обновлён на всех VM",
                "credential": {"username": web_username, "guest_username": username, "password": password, "reveal_once": True},
            }
        if action == "run_check":
            run = self.start_check(stand_id)
            return {"stand": self.get_stand(stand_id), "message": "Автопроверка запущена", "run": run}
        raise ValidationError("Неизвестное действие")

    def rollback_all_stands(self, workspace: str | None = None) -> dict[str, Any]:
        selected_workspace = self._workspace(workspace) if workspace is not None else None
        with self._bulk_rollback_lock:
            if self._bulk_rollback_job and self._bulk_rollback_job.is_alive():
                raise ConflictError("Массовый возврат стендов уже выполняется")

            eligible: list[int] = []
            skipped: list[dict[str, Any]] = []
            with self._job_lock:
                busy_ids = set(self._jobs)
            for summary in self.list_stands():
                if selected_workspace and str(summary.get("workspace") or "demoexam") != selected_workspace:
                    continue
                stand_id = int(summary["id"])
                stand = self.get_stand(stand_id)
                reason = ""
                if str(stand.get("origin") or "deployed") == "imported":
                    reason = "подключённый существующий pool"
                elif stand_id in busy_ids or stand.get("status") in {"provisioning", "resetting"}:
                    reason = "уже выполняется фоновая операция"
                elif stand.get("check_status") == "running" or any(
                    vm.get("check_status") == "running" for vm in stand["vms"]
                ):
                    reason = "выполняется автопроверка"
                elif not stand["vms"]:
                    reason = "нет VM"
                elif any(vm.get("vmid") is None or not vm.get("has_start_snapshot") for vm in stand["vms"]):
                    reason = "не у всех VM есть snapshot start"
                elif any(not vm.get("credential_recoverable") for vm in stand["vms"]):
                    reason = "не у всех VM сохранён пароль"
                if reason:
                    skipped.append({"stand_id": stand_id, "name": stand["name"], "reason": reason})
                else:
                    eligible.append(stand_id)

            if not eligible:
                raise ConflictError("Нет стендов, готовых к массовому возврату к snapshot start")
            thread = threading.Thread(
                target=self._bulk_rollback_start_job,
                args=(eligible,),
                name="rollback-start-all-stands",
                daemon=True,
            )
            self._bulk_rollback_job = thread
            self._bulk_rollback_pending = set(eligible)
            try:
                thread.start()
            except Exception:
                self._bulk_rollback_job = None
                self._bulk_rollback_pending.clear()
                raise
        try:
            self.store.add_activity(
                "rollback", "Массовый возврат стендов запущен",
                f"Запланировано {len(eligible)}, пропущено {len(skipped)}", "progress",
            )
        except Exception:
            pass
        return {
            "message": f"Массовый возврат запущен для {len(eligible)} стендов",
            "scheduled": eligible,
            "scheduled_count": len(eligible),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }

    def _bulk_rollback_start_job(self, stand_ids: list[int]) -> None:
        try:
            try:
                configured = int(os.environ.get("PROXMOX_BULK_ROLLBACK_STANDS", "1"))
            except ValueError:
                configured = 1
            workers = max(1, min(configured, 4, len(stand_ids)))

            def reset_one(stand_id: int) -> tuple[int, str]:
                try:
                    self.stand_action(
                        stand_id,
                        "rollback_start",
                        {"action": "rollback_start"},
                        _bulk_reserved=True,
                    )
                    with self._job_lock:
                        worker = self._jobs.get(stand_id)
                    if worker and worker is not threading.current_thread():
                        worker.join()
                    stand = self.get_stand(stand_id)
                    if stand.get("status") == "error":
                        return stand_id, str(stand.get("last_error") or "ошибка возврата")
                    return stand_id, ""
                except Exception as exc:
                    try:
                        self.store.execute(
                            "UPDATE stands SET last_error = ?, updated_at = ? WHERE id = ?",
                            (f"Массовый возврат пропущен: {str(exc)[-900:]}", utc_now(), stand_id),
                        )
                    except Exception:
                        pass
                    return stand_id, str(exc)
                finally:
                    # A completed stand can be managed again even while later
                    # stands are still waiting in the global queue.
                    with self._bulk_rollback_lock:
                        self._bulk_rollback_pending.discard(stand_id)

            failures: list[tuple[int, str]] = []
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rollback-stand") as executor:
                futures = {executor.submit(reset_one, stand_id): stand_id for stand_id in stand_ids}
                for future in as_completed(futures):
                    stand_id, error = future.result()
                    if error:
                        failures.append((stand_id, error))
            try:
                self.store.add_activity(
                    "rollback",
                    "Массовый возврат стендов завершён" if not failures else "Массовый возврат завершён с ошибками",
                    f"Успешно {len(stand_ids) - len(failures)} из {len(stand_ids)}"
                    + (f" · ошибки: {', '.join(str(item[0]) for item in failures)}" if failures else ""),
                    "success" if not failures else "warning",
                    "Система",
                )
            except Exception:
                pass
        finally:
            with self._bulk_rollback_lock:
                self._bulk_rollback_job = None
                self._bulk_rollback_pending.difference_update(stand_ids)

    def _enqueue_rollback_start(
        self,
        stand: dict[str, Any],
        raw_vms: list[dict[str, Any]],
        scope_label: str,
    ) -> dict[str, Any]:
        stand_id = int(stand["id"])
        if stand.get("check_status") == "running" or any(
            vm.get("check_status") == "running" for vm in stand.get("vms", [])
        ):
            raise ConflictError("Сначала дождитесь завершения автопроверки")
        if not raw_vms:
            raise ConflictError("Нет VM для возврата к исходному состоянию")
        missing_snapshots = [
            str(vm.get("vmid") or vm.get("name") or "неизвестная VM")
            for vm in raw_vms
            if vm.get("vmid") is None or not vm.get("has_start_snapshot")
        ]
        if missing_snapshots:
            raise ConflictError(
                "Возврат недоступен: snapshot start отсутствует у VM "
                + ", ".join(missing_snapshots)
            )
        missing_credentials = [
            str(vm.get("vmid") or vm.get("name") or "неизвестная VM")
            for vm in raw_vms
            if not str(vm.get("credential_password") or "")
        ]
        if missing_credentials:
            raise ConflictError(
                "Возврат недоступен: dashboard не хранит пароль VM "
                + ", ".join(missing_credentials)
                + ". Сначала задайте пароль для этих VM."
            )
        vmids = [int(vm["vmid"]) for vm in raw_vms]
        target_ids = [int(vm["id"]) for vm in raw_vms]
        placeholders = ",".join("?" for _ in target_ids)
        count_row = self.store.query_one(
            "SELECT COUNT(*) AS total FROM stand_vms WHERE stand_id = ?",
            (stand_id,),
        )
        full_stand = len(target_ids) == int(count_row["total"] if count_row else 0)
        thread = threading.Thread(
            target=self._rollback_start_job,
            args=(stand_id, str(stand["name"]), raw_vms, scope_label, str(stand["status"])),
            name=f"rollback-start-{stand_id}-{'all' if len(vmids) > 1 else vmids[0]}",
            daemon=True,
        )
        with self._job_lock:
            if stand_id in self._jobs:
                raise ConflictError("Для стенда уже выполняется фоновая операция")
            now = utc_now()
            with self.store.transaction() as connection:
                if full_stand:
                    connection.execute(
                        """UPDATE stands SET status = 'resetting', progress = 5, last_error = '',
                        updated_at = ? WHERE id = ?""",
                        (now, stand_id),
                    )
                else:
                    connection.execute(
                        """UPDATE stands SET status = 'resetting', progress = 5,
                        updated_at = ? WHERE id = ?""",
                        (now, stand_id),
                    )
                connection.execute(
                    f"""UPDATE stand_vms SET status = 'resetting', credential_valid = 0
                    WHERE stand_id = ? AND id IN ({placeholders})""",
                    (stand_id, *target_ids),
                )
            self._jobs[stand_id] = thread
        try:
            try:
                self.store.add_activity(
                    "rollback", "Возврат к исходному состоянию запущен",
                    f"{stand['name']} · {scope_label} · snapshot start", "progress",
                )
            except Exception:
                pass
            thread.start()
        except Exception:
            with self._job_lock:
                self._jobs.pop(stand_id, None)
            with self.store.transaction() as connection:
                connection.execute(
                    """UPDATE stands SET status = ?, progress = 100, last_error = ?,
                    updated_at = ? WHERE id = ?""",
                    (str(stand["status"]), str(stand.get("last_error") or ""), utc_now(), stand_id),
                )
                for vm in raw_vms:
                    connection.execute(
                        "UPDATE stand_vms SET status = ?, credential_valid = ? WHERE id = ?",
                        (
                            str(vm.get("status") or "stopped"),
                            1 if vm.get("credential_valid", 1) else 0,
                            int(vm["id"]),
                        ),
                    )
            raise
        return {
            "stand": self.get_stand(stand_id),
            "message": f"Возврат {scope_label} к snapshot start запущен",
            "vmids": vmids,
        }

    def _rollback_start_job(
        self,
        stand_id: int,
        stand_name: str,
        raw_vms: list[dict[str, Any]],
        scope_label: str = "все VM",
        original_stand_status: str = "",
    ) -> None:
        vmids = [int(vm["vmid"]) for vm in raw_vms if vm.get("vmid") is not None]
        rollback_applied = False
        restored_vmids: set[int] = set()
        try:
            announced: set[int] = set()

            def progress(value: int, message: str) -> None:
                safe_value = max(5, min(int(value), 90))
                try:
                    self.store.execute(
                        "UPDATE stands SET progress = ?, updated_at = ? WHERE id = ?",
                        (safe_value, utc_now(), stand_id),
                    )
                    milestone = 20 if safe_value >= 20 else 0
                    milestone = 70 if safe_value >= 70 else milestone
                    milestone = 90 if safe_value >= 90 else milestone
                    if milestone and milestone not in announced:
                        announced.add(milestone)
                        self.store.add_activity("rollback", message, stand_name, "progress", "Система")
                except Exception:
                    # A visual progress/audit write must never interrupt a
                    # destructive Proxmox operation that is already running.
                    pass

            self.gateway.rollback_snapshot(vmids, "start", start=True, progress=progress)
            rollback_applied = True
            self.store.execute(
                "UPDATE stands SET progress = 92, updated_at = ? WHERE id = ?",
                (utc_now(), stand_id),
            )
            credentials = [
                {
                    "vmid": int(vm["vmid"]),
                    "username": str(vm.get("credential_username") or "root"),
                    "password": str(vm.get("credential_password") or ""),
                }
                for vm in raw_vms
                if vm.get("vmid") is not None and str(vm.get("credential_password") or "")
            ]
            restored = self.gateway.restore_credentials(credentials)
            restored_vmids = {
                int(vmid) for vmid in (restored if restored is not None else [item["vmid"] for item in credentials])
            }
            now = utc_now()
            target_ids = [int(vm["id"]) for vm in raw_vms]
            placeholders = ",".join("?" for _ in target_ids)
            count_row = self.store.query_one(
                "SELECT COUNT(*) AS total FROM stand_vms WHERE stand_id = ?",
                (stand_id,),
            )
            total_vm_count = int(count_row["total"] if count_row else 0)
            full_stand = len(target_ids) == total_vm_count
            with self.store.transaction() as connection:
                connection.execute(
                    f"""UPDATE stand_vms SET status = 'running', cpu = 0, ram = 0,
                    last_snapshot = 'start', has_start_snapshot = 1, credential_valid = 1,
                    check_score = NULL, check_status = 'idle', last_check = NULL
                    WHERE stand_id = ? AND id IN ({placeholders})""",
                    (stand_id, *target_ids),
                )
                vm_statuses = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT status FROM stand_vms WHERE stand_id = ?",
                        (stand_id,),
                    ).fetchall()
                }
                preserve_unrelated_stand_error = (
                    not full_stand
                    and original_stand_status == "error"
                    and all(str(vm.get("status") or "") != "error" for vm in raw_vms)
                )
                aggregate_status = (
                    "error" if "error" in vm_statuses or preserve_unrelated_stand_error
                    else "running" if "running" in vm_statuses
                    else "stopped"
                )
                if full_stand:
                    connection.execute(
                        """UPDATE stands SET status = ?, progress = 100, cpu = 0, ram = 0,
                        check_score = NULL, check_status = 'idle', last_check = NULL,
                        last_error = '', updated_at = ? WHERE id = ?""",
                        (aggregate_status, now, stand_id),
                    )
                else:
                    connection.execute(
                        """UPDATE stands SET status = ?, progress = 100,
                        check_score = NULL, check_status = 'idle', last_check = NULL,
                        last_error = CASE WHEN ? = 'error' THEN last_error ELSE '' END,
                        updated_at = ? WHERE id = ?""",
                        (aggregate_status, aggregate_status, now, stand_id),
                    )
            try:
                self.store.add_activity(
                    "rollback", "Возврат к исходному состоянию завершён",
                    f"{stand_name} · {scope_label} · snapshot start · текущие пароли восстановлены", "success", "Система",
                )
            except Exception:
                # The state transition above is authoritative; audit failure
                # must not turn a completed rollback into an operational error.
                pass
        except Exception as exc:
            detail = str(exc)[-1200:]
            affected_vmids = (
                set(exc.affected_vmids) if isinstance(exc, RollbackSnapshotError) else set()
            )
            if isinstance(exc, CredentialRestoreError):
                restored_vmids.update(exc.restored_vmids)
            with self.store.transaction() as connection:
                connection.execute(
                    """UPDATE stands SET status = 'error', last_error = ?, updated_at = ?
                    WHERE id = ?""",
                    (f"Ошибка возврата к snapshot start: {detail}", utc_now(), stand_id),
                )
                for vm in raw_vms:
                    vmid = int(vm["vmid"]) if vm.get("vmid") is not None else None
                    original_valid = 1 if vm.get("credential_valid", 1) else 0
                    if isinstance(exc, CredentialRestoreError) or rollback_applied:
                        credential_valid = 1 if vmid in restored_vmids else 0
                    elif isinstance(exc, RollbackSnapshotError):
                        credential_valid = 0 if vmid in affected_vmids else original_valid
                    else:
                        credential_valid = original_valid
                    connection.execute(
                        "UPDATE stand_vms SET status = 'error', credential_valid = ? WHERE id = ?",
                        (credential_valid, int(vm["id"])),
                    )
            self.store.add_activity(
                "rollback", "Ошибка возврата к исходному состоянию",
                f"{stand_name} · {scope_label}: {detail}", "error", "Система",
            )
        finally:
            with self._job_lock:
                self._jobs.pop(stand_id, None)

    @staticmethod
    def _snapshot_label(value: Any) -> str:
        label = str(value or "").strip()
        if not label:
            label = datetime.now(timezone.utc).strftime("manual-%Y%m%d-%H%M%S")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", label):
            raise ValidationError("Имя снимка: до 64 латинских букв, цифр, '-' или '_'")
        return label

    def vm_action(
        self,
        stand_id: int,
        vmid: int,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._assert_not_bulk_rollback_pending(stand_id)
        with self._stand_operation_lock(stand_id):
            return self._vm_action_locked(stand_id, vmid, action, payload)

    def _vm_action_locked(
        self,
        stand_id: int,
        vmid: int,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        stand = self.get_stand(stand_id)
        if stand["status"] in {"provisioning", "resetting"}:
            raise ConflictError("Действие VM недоступно во время фоновой операции стенда")
        with self._job_lock:
            if stand_id in self._jobs:
                raise ConflictError("Для стенда уже выполняется фоновая операция")
        vm = next((item for item in stand["vms"] if int(item.get("vmid") or -1) == vmid), None)
        if not vm:
            raise NotFoundError("VM не найдена в этом стенде")
        if action == "rollback_start":
            raw_vm = self.store.query_one(
                "SELECT * FROM stand_vms WHERE stand_id = ? AND vmid = ?",
                (stand_id, vmid),
            )
            if not raw_vm:
                raise NotFoundError("VM не найдена в этом стенде")
            return self._enqueue_rollback_start(stand, [raw_vm], f"VM {vmid}")
        if action == "snapshot":
            label = self._snapshot_label(payload.get("name"))
            description = str(payload.get("description", ""))[:255]
            self.gateway.create_snapshot([vmid], label, description)
            self.store.execute(
                """UPDATE stand_vms SET last_snapshot = ?,
                has_start_snapshot = CASE WHEN ? = 'start' THEN 1 ELSE has_start_snapshot END
                WHERE stand_id = ? AND vmid = ?""",
                (label, label, stand_id, vmid),
            )
            self.store.add_activity(
                "snapshot", "Создан снимок VM", f"{stand['name']} · VM {vmid} · {label}", "success",
            )
            refreshed = self.get_stand(stand_id)
            return {
                "stand": refreshed,
                "vm": next(item for item in refreshed["vms"] if int(item.get("vmid") or -1) == vmid),
                "message": f"Снимок «{label}» создан на VM {vmid}",
            }
        if action == "rotate_password":
            if stand["status"] != "running" or vm.get("status") != "running":
                raise ConflictError("Смена пароля доступна только для запущенной VM")
            guest_username = str(payload.get("username", vm.get("guest_username", "root"))).strip()
            web_username = str(payload.get("web_username", vm.get("web_username", "root@pam"))).strip() or "root@pam"
            password = str(payload.get("password", "")) or self._password()
            if not re.fullmatch(r"[^\x00-\x1f\x7f]{1,64}", guest_username):
                raise ValidationError("Укажите имя пользователя")
            if not re.fullmatch(r"[^\x00-\x1f\x7f]{1,128}", web_username):
                raise ValidationError("Некорректный логин веб-интерфейса VM")
            if len(password) > PROXMOX_PASSWORD_MAX_LENGTH:
                raise ValidationError(
                    f"Proxmox принимает пароль длиной не более {PROXMOX_PASSWORD_MAX_LENGTH} символов"
                )
            self.gateway.rotate_password([vmid], guest_username, password)
            changed_at = utc_now()
            self.store.execute(
                """UPDATE stand_vms SET credential_username = ?, web_username = ?,
                credential_password = ?, credential_valid = 1, password_updated_at = ?
                WHERE stand_id = ? AND vmid = ?""",
                (guest_username, web_username or "root@pam", password, changed_at, stand_id, vmid),
            )
            self.store.execute(
                "UPDATE stands SET password_updated_at = ?, updated_at = ? WHERE id = ?",
                (changed_at, changed_at, stand_id),
            )
            self.store.add_activity(
                "password", "Пароль VM обновлён",
                f"{stand['name']} · VM {vmid} · пользователь {guest_username}", "success",
            )
            refreshed = self.get_stand(stand_id)
            return {
                "stand": refreshed,
                "vm": next(item for item in refreshed["vms"] if int(item.get("vmid") or -1) == vmid),
                "message": f"Пароль VM {vmid} обновлён",
                "credential": {
                    "username": web_username or "root@pam", "guest_username": guest_username,
                    "password": password, "reveal_once": True,
                },
            }
        if action == "run_check":
            run = self.start_vm_check(stand_id, vmid)
            refreshed = self.get_stand(stand_id)
            return {
                "stand": refreshed,
                "vm": next(item for item in refreshed["vms"] if int(item.get("vmid") or -1) == vmid),
                "message": f"Автопроверка VM {vmid} запущена",
                "run": run,
            }
        raise ValidationError("Неизвестное действие VM")

    @staticmethod
    def _password(length: int = 8) -> str:
        return generate_password(length)

    def delete_stand(self, stand_id: int) -> None:
        self._assert_not_bulk_rollback_pending(stand_id)
        with self._stand_operation_lock(stand_id):
            self._delete_stand_locked(stand_id)

    def _delete_stand_locked(self, stand_id: int) -> None:
        stand = self.get_stand(stand_id)
        if stand["status"] in {"provisioning", "resetting"}:
            raise ConflictError("Нельзя удалить стенд во время фоновой операции")
        with self._job_lock:
            if stand_id in self._jobs:
                raise ConflictError("Нельзя удалить стенд во время фоновой операции")
        if stand["check_status"] == "running":
            raise ConflictError("Нельзя удалить стенд во время автопроверки")
        if any(vm.get("check_status") == "running" for vm in stand["vms"]):
            raise ConflictError("Нельзя удалить стенд во время автопроверки VM")
        vmids = [int(vm["vmid"]) for vm in stand["vms"] if vm.get("vmid") is not None]
        imported = str(stand.get("origin") or "deployed") == "imported"
        # Even when a failed background deploy did not persist VMIDs, ask the
        # live gateway to inspect the owned pool.  This makes a second cleanup
        # attempt possible if the automatic rollback only partially succeeded.
        if not imported:
            self.gateway.delete_stand(stand, vmids)
        self.store.execute("DELETE FROM stands WHERE id = ?", (stand_id,))
        self.store.add_activity(
            "import" if imported else "delete",
            "Pool отключён от дашборда" if imported else "Стенд удалён",
            stand["name"],
            "info" if imported else "warning",
        )

    def list_sessions(self) -> list[dict[str, Any]]:
        self._refresh_session_states()
        return self.store.query_all(
            """SELECT x.*, s.name AS stand_name, s.pool_id FROM sessions x
            JOIN stands s ON s.id = x.stand_id
            ORDER BY CASE x.status WHEN 'active' THEN 0 WHEN 'idle' THEN 1 ELSE 2 END, x.last_seen DESC"""
        )

    def end_session(self, session_id: int) -> dict[str, Any]:
        now = utc_now()
        changed = False
        with self.store.transaction() as connection:
            session_row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not session_row:
                raise NotFoundError("Сессия не найдена")
            session = dict(session_row)
            if session["status"] != "ended":
                connection.execute(
                    "UPDATE sessions SET status = 'ended', ended_at = ?, last_seen = ? WHERE id = ?",
                    (now, now, session_id),
                )
                changed = True
        if changed:
            self.store.add_activity("session", "Сессия завершена", session["user_name"], "warning")
        return self.store.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,)) or {}

    def _refresh_session_states(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        idle_before = (now - SESSION_IDLE_AFTER).isoformat()
        expire_before = (now - SESSION_EXPIRE_AFTER).isoformat()
        now_iso = now.isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                """UPDATE sessions SET status = 'ended', ended_at = COALESCE(ended_at, ?)
                WHERE status IN ('active', 'idle') AND last_seen < ?""",
                (now_iso, expire_before),
            )
            connection.execute(
                "UPDATE sessions SET status = 'idle' WHERE status = 'active' AND last_seen < ?",
                (idle_before,),
            )

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            stand_id = int(payload.get("stand_id"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("Укажите стенд") from exc
        user_name = str(payload.get("user_name", "")).strip()
        if not user_name:
            raise ValidationError("Укажите имя пользователя")
        self._refresh_session_states()
        now = utc_now()
        with self.store.transaction() as connection:
            stand_row = connection.execute(
                "SELECT id, name, status, max_participants FROM stands WHERE id = ?", (stand_id,),
            ).fetchone()
            if not stand_row:
                raise NotFoundError("Стенд не найден")
            if stand_row["status"] != "running":
                raise ConflictError("Подключение возможно только к запущенному стенду")
            active_count = connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE stand_id = ? AND status IN ('active', 'idle')", (stand_id,),
            ).fetchone()[0]
            if int(active_count) >= int(stand_row["max_participants"]):
                raise ConflictError("На стенде нет свободных мест")
            cursor = connection.execute(
                """INSERT INTO sessions
                (stand_id, user_name, login, ip, device, role, status, started_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (stand_id, user_name, str(payload.get("login", "guest")), str(payload.get("ip", "")),
                 str(payload.get("device", "Dashboard gateway")), str(payload.get("role", "Участник")), now, now),
            )
            session_id = int(cursor.lastrowid)
            stand_name = str(stand_row["name"])
        self.store.add_activity("session", "Новая сессия", f"{user_name} · {stand_name}", "info", "Gateway")
        return self.store.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,)) or {}

    def heartbeat(self, session_id: int) -> dict[str, Any]:
        with self.store.transaction() as connection:
            session = connection.execute("SELECT status FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not session:
                raise NotFoundError("Сессия не найдена")
            if session["status"] == "ended":
                raise ConflictError("Завершённую сессию нельзя возобновить heartbeat-запросом")
            connection.execute(
                "UPDATE sessions SET status = 'active', last_seen = ? WHERE id = ?", (utc_now(), session_id),
            )
        return self.store.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,)) or {}

    def list_checks(self) -> list[dict[str, Any]]:
        return self.store.query_all(
            """SELECT r.*, s.name AS stand_name, b.name AS blueprint_name, b.code AS blueprint_code
            FROM check_runs r JOIN stands s ON s.id = r.stand_id
            LEFT JOIN blueprints b ON b.id = r.blueprint_id
            ORDER BY r.started_at DESC LIMIT 100"""
        )

    def start_check(self, stand_id: int) -> dict[str, Any]:
        self._assert_not_bulk_rollback_pending(stand_id)
        with self._stand_operation_lock(stand_id):
            return self._start_check_locked(stand_id)

    def _start_check_locked(self, stand_id: int) -> dict[str, Any]:
        started = utc_now()
        with self.store.transaction() as connection:
            stand = connection.execute(
                """SELECT s.id, s.name, s.blueprint_id, s.status, s.check_status,
                          b.autocheck_script
                FROM stands s LEFT JOIN blueprints b ON b.id = s.blueprint_id
                WHERE s.id = ?""",
                (stand_id,),
            ).fetchone()
            if not stand:
                raise NotFoundError("Стенд не найден")
            if stand["status"] != "running":
                raise ConflictError("Автопроверка доступна только для запущенного стенда")
            if stand["check_status"] == "running":
                raise ConflictError("На стенде уже выполняется автопроверка")
            vm_check_count = connection.execute(
                "SELECT COUNT(*) FROM stand_vms WHERE stand_id = ? AND check_status = 'running'",
                (stand_id,),
            ).fetchone()[0]
            if int(vm_check_count):
                raise ConflictError("На одной из VM уже выполняется автопроверка")
            vm_count = connection.execute(
                "SELECT COUNT(*) FROM stand_vms WHERE stand_id = ? AND vmid IS NOT NULL", (stand_id,),
            ).fetchone()[0]
            if int(vm_count) == 0:
                raise ConflictError("В стенде нет VM для автопроверки")
            autocheck_script = str(stand["autocheck_script"] or "")
            if not autocheck_script.strip():
                raise ConflictError("Скрипт автопроверки сценария пуст")
            cursor = connection.execute(
                """INSERT INTO check_runs (stand_id, blueprint_id, status, details, output, started_at)
                VALUES (?, ?, 'running', '[]', 'Подключение к VM…', ?)""",
                (stand_id, stand["blueprint_id"], started),
            )
            run_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE stands SET check_status = 'running', updated_at = ? WHERE id = ?", (started, stand_id),
            )
            stand_name = str(stand["name"])
        self.store.add_activity("check", "Автопроверка запущена", stand_name, "progress")
        threading.Thread(
            target=self._check_job,
            args=(run_id, stand_id, autocheck_script),
            name=f"check-{run_id}",
            daemon=True,
        ).start()
        return self.store.query_one("SELECT * FROM check_runs WHERE id = ?", (run_id,)) or {}

    def start_vm_check(self, stand_id: int, vmid: int) -> dict[str, Any]:
        with self._stand_operation_lock(stand_id):
            return self._start_vm_check_locked(stand_id, vmid)

    def _start_vm_check_locked(self, stand_id: int, vmid: int) -> dict[str, Any]:
        started = utc_now()
        with self.store.transaction() as connection:
            row = connection.execute(
                """SELECT s.name AS stand_name, s.blueprint_id, s.status AS stand_status,
                          s.check_status AS stand_check_status, v.id AS stand_vm_id,
                          v.name AS vm_name, v.status AS vm_status, v.check_status,
                          b.autocheck_script
                FROM stands s
                JOIN stand_vms v ON v.stand_id = s.id
                LEFT JOIN blueprints b ON b.id = s.blueprint_id
                WHERE s.id = ? AND v.vmid = ?""",
                (stand_id, vmid),
            ).fetchone()
            if not row:
                raise NotFoundError("VM не найдена в этом стенде")
            if row["stand_status"] != "running" or row["vm_status"] != "running":
                raise ConflictError("Автопроверка доступна только для запущенной VM")
            if row["stand_check_status"] == "running":
                raise ConflictError("Сейчас выполняется общая автопроверка стенда")
            if row["check_status"] == "running":
                raise ConflictError("На этой VM уже выполняется автопроверка")
            script = str(row["autocheck_script"] or "")
            if not script.strip():
                raise ConflictError("Скрипт автопроверки сценария пуст")
            cursor = connection.execute(
                """INSERT INTO check_runs
                (stand_id, vmid, blueprint_id, status, details, output, started_at)
                VALUES (?, ?, ?, 'running', '[]', 'Подключение к VM…', ?)""",
                (stand_id, vmid, row["blueprint_id"], started),
            )
            run_id = int(cursor.lastrowid)
            connection.execute(
                """UPDATE stand_vms SET check_status = 'running', last_check = ?
                WHERE id = ?""",
                (started, row["stand_vm_id"]),
            )
            stand_name = str(row["stand_name"])
            vm_name = str(row["vm_name"])
        self.store.add_activity(
            "check", "Автопроверка VM запущена",
            f"{stand_name} · {vm_name} (VM {vmid})", "progress",
        )
        threading.Thread(
            target=self._vm_check_job,
            args=(run_id, stand_id, vmid, script),
            name=f"vm-check-{run_id}",
            daemon=True,
        ).start()
        return self.store.query_one("SELECT * FROM check_runs WHERE id = ?", (run_id,)) or {}

    def _vm_check_job(self, run_id: int, stand_id: int, vmid: int, script: str) -> None:
        started = time.monotonic()
        try:
            details = self.gateway.run_autocheck([vmid], script)
            total = max(len(details), 1)
            passed = sum(1 for item in details if item.get("ok"))
            score = round(passed / total * 100)
            status = "passed" if score >= 90 else "warning" if score >= 70 else "failed"
            finished = utc_now()
            duration = round((time.monotonic() - started) * 1000)
            output = f"{passed}/{total} проверок пройдено"
            with self.store.transaction() as connection:
                connection.execute(
                    """UPDATE check_runs SET status = ?, score = ?, passed = ?, total = ?,
                    duration_ms = ?, details = ?, output = ?, finished_at = ? WHERE id = ?""",
                    (status, score, passed, total, duration,
                     json.dumps(details, ensure_ascii=False), output, finished, run_id),
                )
                connection.execute(
                    """UPDATE stand_vms SET check_score = ?, check_status = ?, last_check = ?
                    WHERE stand_id = ? AND vmid = ?""",
                    (score, status, finished, stand_id, vmid),
                )
            stand = self.get_stand(stand_id)
            self.store.add_activity(
                "check", "Автопроверка VM завершена",
                f"{stand['name']} · VM {vmid} · результат {score}%",
                "success" if score >= 90 else "warning", "Система",
            )
        except Exception as exc:
            finished = utc_now()
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE check_runs SET status = 'failed', output = ?, finished_at = ? WHERE id = ?",
                    (str(exc)[-1000:], finished, run_id),
                )
                connection.execute(
                    """UPDATE stand_vms SET check_status = 'failed', last_check = ?
                    WHERE stand_id = ? AND vmid = ?""",
                    (finished, stand_id, vmid),
                )
            self.store.add_activity(
                "check", "Ошибка автопроверки VM",
                f"Стенд #{stand_id} · VM {vmid}: {exc}", "error", "Система",
            )

    def _check_job(self, run_id: int, stand_id: int, autocheck_script: str) -> None:
        started = time.monotonic()
        try:
            stand = self.get_stand(stand_id)
            vmids = [int(vm["vmid"]) for vm in stand["vms"] if vm.get("vmid") is not None]
            details = self.gateway.run_autocheck(vmids, autocheck_script)
            total = max(len(details), 1)
            passed = sum(1 for item in details if item.get("ok"))
            score = round(passed / total * 100)
            status = "passed" if score >= 90 else "warning" if score >= 70 else "failed"
            finished = utc_now()
            duration = round((time.monotonic() - started) * 1000)
            self.store.execute(
                """UPDATE check_runs SET status = ?, score = ?, passed = ?, total = ?, duration_ms = ?,
                details = ?, output = ?, finished_at = ? WHERE id = ?""",
                (status, score, passed, total, duration, json.dumps(details, ensure_ascii=False), f"{passed}/{total} проверок пройдено", finished, run_id),
            )
            self.store.execute(
                "UPDATE stands SET check_score = ?, check_status = ?, last_check = ?, updated_at = ? WHERE id = ?",
                (score, status, finished, finished, stand_id),
            )
            self.store.add_activity("check", "Автопроверка завершена", f"{stand['name']} · результат {score}%", "success" if score >= 90 else "warning", "Система")
        except Exception as exc:
            finished = utc_now()
            self.store.execute("UPDATE check_runs SET status = 'failed', output = ?, finished_at = ? WHERE id = ?", (str(exc), finished, run_id))
            self.store.execute("UPDATE stands SET check_status = 'failed', last_check = ?, updated_at = ? WHERE id = ?", (finished, finished, stand_id))
            self.store.add_activity("check", "Ошибка автопроверки", f"Стенд #{stand_id}: {exc}", "error", "Система")

    def metrics(self) -> dict[str, Any]:
        vm_rows = self.store.query_all("SELECT vmid FROM stand_vms WHERE vmid IS NOT NULL")
        return self.gateway.cluster_metrics([int(row["vmid"]) for row in vm_rows])

    def web_activity(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._web_activity_cache is not None and now - self._web_activity_cache_at < 20:
            return self._web_activity_cache
        with self._web_activity_lock:
            now = time.monotonic()
            if not force and self._web_activity_cache is not None and now - self._web_activity_cache_at < 20:
                return self._web_activity_cache
            rows = self.store.query_all(
                """SELECT v.vmid, v.name AS vm_name, v.ip AS vm_ip, v.node,
                          s.id AS stand_id, s.name AS stand_name, s.pool_id
                FROM stand_vms v JOIN stands s ON s.id = v.stand_id
                WHERE v.vmid IS NOT NULL AND v.status = 'running'
                ORDER BY s.id, v.id"""
            )
            vmids = [int(row["vmid"]) for row in rows]
            payload = self.gateway.web_activity(vmids, window_seconds=180)
            metadata = {int(row["vmid"]): row for row in rows}
            activity: list[dict[str, Any]] = []
            for raw in payload.get("activity", []):
                item = dict(raw)
                vmid = int(item["vmid"])
                item.update(metadata.get(vmid, {}))
                activity.append(item)
            errors: list[dict[str, Any]] = []
            for raw in payload.get("errors", []):
                item = dict(raw)
                vmid = int(item["vmid"])
                item.update(metadata.get(vmid, {}))
                errors.append(item)
            result = {
                **payload,
                "activity": activity,
                "errors": errors,
                "exact_sessions": False,
                "method": "pveproxy access.log через QEMU Guest Agent",
                "notice": (
                    "Proxmox не хранит точный список браузерных сессий. "
                    "Показана недавняя активность по IP и логину; похожий запрос может отправить и API-клиент."
                ),
            }
            self._web_activity_cache = result
            self._web_activity_cache_at = time.monotonic()
            return result

    def overview(self) -> dict[str, Any]:
        stands = self.list_stands()
        running = [stand for stand in stands if stand["status"] in {"running", "resetting"}]
        scores = [int(stand["check_score"]) for stand in stands if stand["check_score"] is not None]
        return {
            "active_stands": len(running), "total_stands": len(stands),
            "total_vms": sum(int(stand.get("actual_vm_count") or 0) for stand in stands),
            "average_score": round(sum(scores) / max(len(scores), 1)),
            "attention": sum(1 for stand in stands if stand["status"] == "error" or stand["check_status"] in {"warning", "failed"}),
        }

    def activity(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.store.query_all("SELECT * FROM activity ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 100)),))

    def bootstrap(self) -> dict[str, Any]:
        return {
            "integration": self.integration(), "overview": self.overview(), "stands": self.list_stands(),
            "blueprints": self.list_blueprints(), "templates": self.list_templates(), "pools": self.list_pools(),
            "checks": self.list_checks(),
            "metrics": self.metrics(), "activity": self.activity(), "server_time": utc_now(),
        }
