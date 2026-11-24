"""One-shot, persisted leases; only the specific originally enrolled QEMU guests may be stopped."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone


def duration(value):
    if value is not None and (type(value) is not int or not 1 <= value <= 10080):
        raise ValueError("Срок стенда — целое число от 1 до 10080 минут или без ограничения")
    return value


def deadline(minutes, now=None):
    if minutes is None:
        return None
    return ((now or datetime.now(timezone.utc)) + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


class StandTimer:
    def __init__(self, service):
        self.service = service
        self.db = service.store
        self.gateway = service.gateway
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = None

    def start(self):
        # Indefinite-only installations and legacy databases have no polling thread.
        with self.lock:
            if self.thread is not None or self.stop.is_set():
                return
            self.thread = threading.Thread(target=self._monitor, name="stand-expiry", daemon=True)
            self.thread.start()

    def _monitor(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                # A busy database/queue must not kill expiry handling after restart.
                pass
            self.stop.wait(10)

    def identity(self, vm):
        return "demo" if self.gateway.mode == "demo" else self.gateway.timer_vm_uuid(vm)

    def register(self, conn, stand_id, pool, vm, identity):
        row = conn.execute("""SELECT v.id,s.owner FROM stand_vms v JOIN stands s ON s.id=v.stand_id
                           WHERE v.stand_id=? AND v.vmid=? AND v.name=?""",
                           (stand_id, vm["vmid"], vm["name"])).fetchone()
        if row is None:
            raise RuntimeError("Не удалось сопоставить созданную VM с таймером")
        conn.execute("""INSERT INTO stand_timer_targets
            (stand_id, vmid, vm_row_id, pool_id, owner, vm_name, guest_uuid, mode, state)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (stand_id, vm["vmid"], row[0], pool, row["owner"], vm["name"], identity, self.gateway.mode,
             "waiting" if identity else "blocked"))

    def set_existing(self, stand_id, minutes, vmid, pool, ip):
        # Called by the *scoped*, separately authorized pilot endpoint only.
        minutes = duration(minutes)
        if minutes is None:
            raise ValueError("Для уже запущенного стенда выберите срок в минутах")
        vm = self.db.query_one("""SELECT v.vmid,v.node,v.name FROM stand_vms v
            WHERE v.stand_id=? AND v.vmid=? AND v.ip=?""", (stand_id, vmid, ip))
        if not vm:
            raise ValueError("VM стенда изменилась")
        with self.service._stand_admitted_operation(stand_id, "power", "Привязка срока стенда", 1,
                only_if_idle=True, timeout=5, **self.service._operation_scope("power", [vm])):
            query = """SELECT s.duration_minutes,s.expires_at,v.name,v.vmid,v.node FROM stands s
                 JOIN stand_vms v ON v.stand_id=s.id
                 JOIN ipam_reservations r ON r.stand_id=s.id AND r.stand_vm_id=v.id AND r.address=v.ip
                 WHERE s.id=? AND s.origin='deployed' AND s.pool_id=? AND s.owner='student01'
                 AND s.vm_count=1 AND s.status='running' AND v.vmid=? AND v.ip=? AND v.status='running'
                 AND r.status='assigned' AND v.credential_valid=1
                 AND NOT EXISTS (SELECT 1 FROM stand_vms x WHERE x.stand_id=s.id AND x.id!=v.id)
                 AND NOT EXISTS (SELECT 1 FROM stands x WHERE x.pool_id=s.pool_id AND x.id!=s.id)"""
            params = (stand_id, pool, vmid, ip)
            current = self.db.query_one(query, params)
            if not current:
                raise ValueError("Назначение или состояние VM изменилось")
            if current["duration_minutes"] is not None:
                if (current["duration_minutes"] != minutes or not current["expires_at"] or
                        datetime.fromisoformat(current["expires_at"]) <= datetime.now(timezone.utc)):
                    raise ValueError("Срок уже назначен или истёк")
                return {"duration_minutes": minutes, "expires_at": current["expires_at"]}
            members = self.gateway.pool_members(pool)
            if len(members) != 1 or (members[0]["vmid"], members[0]["name"], members[0]["status"]) != (
                    vmid, current["name"], "running"):
                raise ValueError("Не удалось подтвердить личную VM в пуле")
            token = self.identity(members[0])
            if not token:
                raise ValueError("Не удалось подтвердить идентичность VM")
            expires = deadline(minutes)
            with self.db.transaction() as conn:
                check = conn.execute(query, params).fetchone()
                if not check or check["duration_minutes"] is not None or check["name"] != current["name"]:
                    raise ValueError("Назначение стенда изменилось; обновите карточку")
                self.register(conn, stand_id, pool, members[0], token)
                conn.execute("UPDATE stands SET duration_minutes=?,expires_at=? WHERE id=?", (minutes, expires, stand_id))
        self.start()
        return {"duration_minutes": minutes, "expires_at": expires}

    def tick(self, now=None):
        now = int(time.time()) if now is None else int(now)
        iso = datetime.fromtimestamp(now, timezone.utc).isoformat()
        targets = self.db.query_all("""SELECT t.*,v.node FROM stand_timer_targets t JOIN stands s ON s.id=t.stand_id
            LEFT JOIN stand_vms v ON v.id=t.vm_row_id
            WHERE t.state='waiting' AND t.mode=? AND t.next_try<=? AND s.duration_minutes IS NOT NULL
            AND s.expires_at<=? ORDER BY t.next_try,s.expires_at,t.stand_id,t.vmid LIMIT 8""",
            (self.gateway.mode, now, iso))
        for t in targets:
            if self.stop.is_set():
                return
            key = (t["stand_id"], t["vmid"])
            try:
                with self.service._stand_admitted_operation(key[0], "power", "Выключение по сроку", 1,
                        only_if_idle=True, timeout=3, **self.service._operation_scope("power", [t])):
                    # This persisted claim also prevents two simultaneous ticks from submitting twice.
                    with self.db.transaction() as conn:
                        if not conn.execute("""UPDATE stand_timer_targets SET next_try=?,attempts=attempts+1
                             WHERE stand_id=? AND vmid=? AND state='waiting' AND next_try<=?""",
                             (now + 240, *key, now)).rowcount:
                            continue
                    self._expire(t)
            except Exception:
                self.db.execute("""UPDATE stand_timer_targets SET next_try=?,attempts=max(attempts,?)
                    WHERE stand_id=? AND vmid=? AND state='waiting' AND next_try IN (?,?)""",
                    (now + min(300, 15 * 2 ** min(t["attempts"], 5)), t["attempts"] + 1, *key, t["next_try"], now + 240))
                if t["attempts"] in {0, 3}:
                    self.db.add_activity("power", "Автовыключение отложено",
                        f"Стенд #{key[0]}, VM {key[1]}: повторим штатное выключение без принудительной остановки.", "warning")

    def _block(self, t):
        self.db.execute("UPDATE stand_timer_targets SET state='blocked' WHERE stand_id=? AND vmid=?",
                        (t["stand_id"], t["vmid"]))
        self.db.add_activity("power", "Автовыключение отменено", f"Стенд #{t['stand_id']}, VM {t['vmid']}: "
                             "назначение или идентичность VM изменились. Требуется проверка.", "warning")

    def _expire(self, t):
        sid, vmid = t["stand_id"], t["vmid"]
        current = self.db.query_one("""SELECT s.status AS stand_status, s.check_status,
            v.status AS vm_status,v.node FROM stands s JOIN stand_vms v ON v.stand_id=s.id
            WHERE s.id=? AND s.origin='deployed' AND s.pool_id=? AND s.owner=? AND v.id=? AND v.vmid=? AND v.name=?
            AND NOT EXISTS (SELECT 1 FROM stands x WHERE x.id!=s.id AND x.pool_id=s.pool_id)
            AND NOT EXISTS (SELECT 1 FROM stand_vms x WHERE x.id!=v.id AND x.vmid=v.vmid)""",
            (sid,t["pool_id"],t["owner"],t["vm_row_id"],vmid,t["vm_name"]))
        if not current:
            self._block(t)
            return
        if current["stand_status"] not in ("running", "stopped") or current["check_status"] == "running":
            raise RuntimeError("Стенд занят")
        has_extra_running = False
        if self.gateway.mode == "live":
            members = self.gateway.pool_members(t["pool_id"])
            matches = [m for m in members if m.get("vmid") == vmid and m.get("name") == t["vm_name"]]
            if len(matches) != 1:
                self._block(t)
                return
            vm = matches[0]
            if vm["node"] != current["node"]:
                # Do not use a reservation for the old node after a migration.
                raise RuntimeError("Ожидаем обновления размещения VM")
            # VMIDs can be reused even within the same pool with the same name.
            if self.identity(vm) != t["guest_uuid"]:
                self._block(t)
                return
            has_extra_running = any(m.get("vmid") != vmid and m.get("status") == "running" for m in members)
            if vm.get("status") == "running":
                self.gateway.shutdown_idle_vm(dict(vm, timer_uuid=t["guest_uuid"], timer_pool=t["pool_id"]))
            elif vm.get("status") != "stopped":
                raise RuntimeError("Состояние VM не подтверждено")
        elif self.gateway.mode != "demo" or t["guest_uuid"] != "demo":
            self._block(t)
            return
        with self.db.transaction() as conn:
            conn.execute("UPDATE stand_timer_targets SET state='done' WHERE stand_id=? AND vmid=?", (sid, vmid))
            conn.execute("UPDATE stand_vms SET status='stopped',cpu=0 WHERE id=? AND stand_id=? AND vmid=?",
                         (t["vm_row_id"],sid,vmid))
            if not has_extra_running:
                conn.execute("""UPDATE stands SET status='stopped',cpu=0 WHERE id=? AND NOT EXISTS (
                    SELECT 1 FROM stand_vms WHERE stand_id=? AND status!='stopped')""", (sid,sid))
        self.db.add_activity("power", "Истёк срок стенда", f"Стенд #{sid}, VM {vmid}: остановлена без удаления дисков.", "info")
