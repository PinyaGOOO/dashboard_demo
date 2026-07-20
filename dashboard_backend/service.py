from __future__ import annotations

import json
import re
import secrets
import string
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import DashboardStore, utc_now
from .proxmox_gateway import DemoProxmoxGateway, LiveProxmoxGateway


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
        if gateway.mode == "demo":
            self._resume_demo_deployments()

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

    def integration(self) -> dict[str, Any]:
        info = self.gateway.integration_info()
        return {
            "mode": info.mode, "connected": info.connected, "host": info.host,
            "cluster": info.cluster, "message": info.message,
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
            "code", "name", "description", "category", "version", "status", "vm_count",
            "template_vmid", "clone_type", "storage", "bridge", "subnet", "estimated_minutes",
            "tags", "deploy_script", "autocheck_script", "checks_count",
        }
        data = {key: payload[key] for key in fields if key in payload}
        if blueprint_id is None and (not str(data.get("name", "")).strip() or not str(data.get("code", "")).strip()):
            raise ValidationError("Укажите название и код сценария")
        if "name" in data and not str(data["name"]).strip():
            raise ValidationError("Название сценария не может быть пустым")
        if "code" in data and not str(data["code"]).strip():
            raise ValidationError("Код сценария не может быть пустым")
        for field in ("vm_count", "template_vmid", "estimated_minutes", "checks_count"):
            if field in data:
                try:
                    data[field] = int(data[field])
                except (TypeError, ValueError) as exc:
                    raise ValidationError(f"Поле {field} должно быть числом") from exc
        if "vm_count" in data and not 1 <= data["vm_count"] <= 50:
            raise ValidationError("Количество VM должно быть от 1 до 50")
        if "template_vmid" in data and data["template_vmid"] < 0:
            raise ValidationError("VMID шаблона не может быть отрицательным")
        if "estimated_minutes" in data and not 1 <= data["estimated_minutes"] <= 1440:
            raise ValidationError("Оценка времени должна быть от 1 до 1440 минут")
        if "checks_count" in data and not 0 <= data["checks_count"] <= 1000:
            raise ValidationError("Количество проверок должно быть от 0 до 1000")
        if "clone_type" in data and data["clone_type"] not in {"full", "linked"}:
            raise ValidationError("Тип клонирования: full или linked")
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
                "vm_count": 1, "template_vmid": 0, "clone_type": "full", "storage": "",
                "bridge": "vmbr0", "subnet": "", "estimated_minutes": 8,
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
        self._refresh_session_states()
        sql = """
        SELECT s.*, b.name AS blueprint_name, b.code AS blueprint_code, b.category AS blueprint_category,
               (SELECT COUNT(*) FROM stand_vms v WHERE v.stand_id = s.id) AS actual_vm_count,
               (SELECT COUNT(*) FROM sessions x WHERE x.stand_id = s.id AND x.status IN ('active','idle')) AS active_sessions
        FROM stands s LEFT JOIN blueprints b ON b.id = s.blueprint_id
        ORDER BY CASE s.status WHEN 'provisioning' THEN 0 WHEN 'error' THEN 1 WHEN 'running' THEN 2 ELSE 3 END,
                 s.updated_at DESC
        """
        rows = self.store.query_all(sql)
        for row in rows:
            row["participants"] = row["active_sessions"]
        return rows

    def get_stand(self, stand_id: int) -> dict[str, Any]:
        stand = next((item for item in self.list_stands() if int(item["id"]) == stand_id), None)
        if not stand:
            raise NotFoundError("Стенд не найден")
        stand["vms"] = self.store.query_all("SELECT * FROM stand_vms WHERE stand_id = ? ORDER BY id", (stand_id,))
        stand["sessions"] = self.store.query_all("SELECT * FROM sessions WHERE stand_id = ? ORDER BY last_seen DESC", (stand_id,))
        stand["checks"] = self.store.query_all("SELECT * FROM check_runs WHERE stand_id = ? ORDER BY started_at DESC LIMIT 10", (stand_id,))
        return stand

    @staticmethod
    def _pool_id(value: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-.").lower()
        if not value:
            value = f"exam-{int(time.time())}"
        return value[:48]

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
        pool_id = self._pool_id(str(payload.get("pool_id", "")) or f"exam-{int(time.time())}")
        existing = self.store.query_one("SELECT id FROM stands WHERE pool_id = ?", (pool_id,))
        if existing:
            raise ConflictError("Pool ID уже используется")
        try:
            max_participants = max(1, min(100, int(payload.get("max_participants", 12))))
            ttl_hours = max(1, min(720, int(payload.get("ttl_hours", 8))))
        except (TypeError, ValueError) as exc:
            raise ValidationError("Вместимость и срок жизни должны быть числами") from exc
        expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).replace(microsecond=0).isoformat()
        now = utc_now()
        stand_id = self.store.execute(
            """INSERT INTO stands
            (name, blueprint_id, status, progress, node, pool_id, owner, participants, max_participants,
             vm_count, cpu, ram, disk, ip_range, check_status, expires_at, created_at, updated_at)
            VALUES (?, ?, 'provisioning', 4, ?, ?, ?, 0, ?, ?, 0, 0, 0, ?, 'idle', ?, ?, ?)""",
            (name, blueprint_id, str(payload.get("node", "auto")), pool_id, str(payload.get("owner", "Администратор")),
             max_participants, int(blueprint["vm_count"]), str(blueprint.get("subnet", "")), expires, now, now),
        )
        self.store.add_activity("deploy", "Развёртывание запущено", f"{name} · {blueprint['name']}", "progress")
        thread = threading.Thread(
            target=self._deploy_job,
            args=(stand_id, dict(blueprint)),
            name=f"deploy-{stand_id}",
            daemon=True,
        )
        with self._job_lock:
            self._jobs[stand_id] = thread
        thread.start()
        return self.get_stand(stand_id)

    def update_stand(self, stand_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        stand = self.get_stand(stand_id)
        allowed = {"name", "owner", "max_participants", "expires_at", "ip_range"}
        data = {key: payload[key] for key in allowed if key in payload}
        if "name" in data and not str(data["name"]).strip():
            raise ValidationError("Название стенда не может быть пустым")
        if "max_participants" in data:
            try:
                data["max_participants"] = max(1, min(100, int(data["max_participants"])))
            except (TypeError, ValueError) as exc:
                raise ValidationError("Вместимость должна быть числом") from exc
        if not data:
            return stand
        data["updated_at"] = utc_now()
        self.store.execute(
            f"UPDATE stands SET {', '.join(f'{key} = ?' for key in data)} WHERE id = ?",
            tuple(data.values()) + (stand_id,),
        )
        self.store.add_activity("edit", "Параметры стенда обновлены", str(data.get("name", stand["name"])), "success")
        return self.get_stand(stand_id)

    def _deploy_job(self, stand_id: int, blueprint: dict[str, Any]) -> None:
        try:
            stand = self.get_stand(stand_id)

            def progress(value: int, message: str) -> None:
                self.store.execute("UPDATE stands SET progress = ?, updated_at = ? WHERE id = ?", (value, utc_now(), stand_id))
                if value in {31, 72, 100}:
                    self.store.add_activity("deploy", message, stand["name"], "progress" if value < 100 else "success", "Система")

            vms = self.gateway.deploy(stand, blueprint, progress)
            self.store.execute("DELETE FROM stand_vms WHERE stand_id = ?", (stand_id,))
            for vm in vms:
                self.store.execute(
                    """INSERT INTO stand_vms (stand_id, vmid, name, node, ip, status, cpu, ram)
                    VALUES (?, ?, ?, ?, ?, ?, 0, 0)""",
                    (stand_id, vm.get("vmid"), vm["name"], vm.get("node", ""), vm.get("ip", ""), vm.get("status", "running")),
                )
            primary_node = vms[0].get("node", "") if vms else ""
            self.store.execute(
                "UPDATE stands SET status = 'running', progress = 100, node = ?, cpu = 4.8, ram = 8.2, updated_at = ? WHERE id = ?",
                (primary_node, utc_now(), stand_id),
            )
        except Exception as exc:
            self.store.execute("UPDATE stands SET status = 'error', updated_at = ? WHERE id = ?", (utc_now(), stand_id))
            self.store.add_activity("deploy", "Ошибка развёртывания", f"Стенд #{stand_id}: {exc}", "error", "Система")
        finally:
            with self._job_lock:
                self._jobs.pop(stand_id, None)

    def stand_action(self, stand_id: int, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        stand = self.get_stand(stand_id)
        vmids = [int(vm["vmid"]) for vm in stand["vms"] if vm.get("vmid") is not None]
        if action in {"start", "stop", "restart"}:
            if stand["status"] == "provisioning":
                raise ConflictError("Действие недоступно во время развёртывания")
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
            label = str(payload.get("name", "manual-point")).strip() or "manual-point"
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", label):
                raise ValidationError("Имя снимка: до 64 латинских букв, цифр, '-' или '_'")
            if not vmids:
                raise ConflictError("В стенде нет VM для создания снимка")
            description = str(payload.get("description", ""))[:255]
            self.gateway.create_snapshot(vmids, label, description)
            self.store.add_activity("snapshot", "Создан снимок", f"{stand['name']} · {label}", "success")
            return {"stand": stand, "message": f"Снимок «{label}» создан на всех VM"}
        if action == "rotate_password":
            username = str(payload.get("username", "root")).strip()
            password = str(payload.get("password", "")) or self._password()
            if stand["status"] != "running":
                raise ConflictError("Смена пароля доступна только для запущенного стенда")
            if not username:
                raise ValidationError("Укажите имя пользователя")
            if len(password) < 10:
                raise ValidationError("Пароль должен содержать не менее 10 символов")
            if not vmids:
                raise ConflictError("В стенде нет VM для смены пароля")
            self.gateway.rotate_password(vmids, username, password)
            self.store.execute("UPDATE stands SET password_updated_at = ?, updated_at = ? WHERE id = ?", (utc_now(), utc_now(), stand_id))
            self.store.add_activity("password", "Пароль стенда обновлён", f"{stand['name']} · пользователь {username}", "success")
            return {"stand": self.get_stand(stand_id), "message": "Пароль обновлён на всех VM", "credential": {"username": username, "password": password, "reveal_once": True}}
        if action == "run_check":
            run = self.start_check(stand_id)
            return {"stand": self.get_stand(stand_id), "message": "Автопроверка запущена", "run": run}
        raise ValidationError("Неизвестное действие")

    @staticmethod
    def _password(length: int = 16) -> str:
        alphabet = string.ascii_letters + string.digits + "!@#$%"
        while True:
            value = "".join(secrets.choice(alphabet) for _ in range(length))
            if any(char.islower() for char in value) and any(char.isupper() for char in value) and any(char.isdigit() for char in value):
                return value

    def delete_stand(self, stand_id: int) -> None:
        stand = self.get_stand(stand_id)
        if stand["status"] == "provisioning":
            raise ConflictError("Нельзя удалить стенд во время развёртывания")
        if stand["check_status"] == "running":
            raise ConflictError("Нельзя удалить стенд во время автопроверки")
        vmids = [int(vm["vmid"]) for vm in stand["vms"] if vm.get("vmid") is not None]
        self.gateway.delete_stand(stand, vmids)
        self.store.execute("DELETE FROM stands WHERE id = ?", (stand_id,))
        self.store.add_activity("delete", "Стенд удалён", stand["name"], "warning")

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
            vm_count = connection.execute(
                "SELECT COUNT(*) FROM stand_vms WHERE stand_id = ? AND vmid IS NOT NULL", (stand_id,),
            ).fetchone()[0]
            if int(vm_count) == 0:
                raise ConflictError("В стенде нет VM для автопроверки")
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
            autocheck_script = str(stand["autocheck_script"] or "")
        self.store.add_activity("check", "Автопроверка запущена", stand_name, "progress")
        threading.Thread(
            target=self._check_job,
            args=(run_id, stand_id, autocheck_script),
            name=f"check-{run_id}",
            daemon=True,
        ).start()
        return self.store.query_one("SELECT * FROM check_runs WHERE id = ?", (run_id,)) or {}

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

    def overview(self) -> dict[str, Any]:
        stands = self.list_stands()
        sessions = self.list_sessions()
        running = [stand for stand in stands if stand["status"] == "running"]
        active_sessions = [session for session in sessions if session["status"] in {"active", "idle"}]
        scores = [int(stand["check_score"]) for stand in stands if stand["check_score"] is not None]
        return {
            "active_stands": len(running), "total_stands": len(stands),
            "active_sessions": len(active_sessions),
            "average_score": round(sum(scores) / max(len(scores), 1)),
            "attention": sum(1 for stand in stands if stand["status"] == "error" or stand["check_status"] in {"warning", "failed"}),
            "available_slots": sum(max(0, int(stand["max_participants"]) - int(stand["participants"])) for stand in running),
        }

    def activity(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.store.query_all("SELECT * FROM activity ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 100)),))

    def bootstrap(self) -> dict[str, Any]:
        return {
            "integration": self.integration(), "overview": self.overview(), "stands": self.list_stands(),
            "blueprints": self.list_blueprints(), "sessions": self.list_sessions(), "checks": self.list_checks(),
            "metrics": self.metrics(), "activity": self.activity(), "server_time": utc_now(),
        }
