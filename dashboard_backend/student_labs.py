"""Provision personal, one-VM labs through the normal Deployer queue.

Only a receipt written in the same transaction as the original clone request is
authority. Pool names, mutable inventory and the student's request body are not.
"""
from __future__ import annotations

import ipaddress
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError

from .service import ConflictError, ValidationError
from .student_network import STUDENT_NETWORK
from .student_pilot_publish import _guest, _guest_mac, _verify_endpoint
from .student_errors import StudentError


_ROW = """SELECT r.*, s.id AS current_stand, s.status, s.progress, s.duration_minutes, s.expires_at,
    s.owner, s.origin, s.pool_id AS current_pool, v.status AS vm_status, v.node,
    (s.duration_minutes IS NOT NULL AND s.expires_at IS NOT NULL AND
        COALESCE(julianday(s.expires_at) > julianday('now'), 0)=0) AS expired,
    (s.origin='deployed' AND s.owner=r.login AND s.vm_count=1 AND s.pool_id=r.pool_id
        AND s.blueprint_id=r.blueprint_id AND s.status='running' AND v.status='running'
        AND v.stand_id=r.stand_id AND v.vmid=r.vmid AND v.name=r.vm_name AND v.ip=r.guest_ip
        AND v.web_username='root@pam' AND v.credential_valid=1 AND r.vmid!=r.template_vmid
        AND r.guest_uuid IS NOT NULL AND r.guest_uuid!=''
        AND (s.duration_minutes IS NULL OR julianday(s.expires_at)>julianday('now'))
        AND EXISTS (SELECT 1 FROM ipam_reservations i WHERE i.stand_id=r.stand_id
            AND i.stand_vm_id=r.vm_row_id AND i.address=r.guest_ip AND i.status='assigned' AND i.prefix_length=16)
        AND NOT EXISTS (SELECT 1 FROM stands other WHERE other.pool_id=r.pool_id AND other.id!=s.id)
        AND NOT EXISTS (SELECT 1 FROM stand_vms other WHERE other.id!=r.vm_row_id AND
            (other.stand_id=r.stand_id OR other.vmid=r.vmid OR
            trim(substr(other.ip,1,instr(other.ip||'/','/')-1))=r.guest_ip))) AS enrolled
    FROM student_lab_receipts r LEFT JOIN stands s ON s.id=r.stand_id LEFT JOIN stand_vms v ON v.id=r.vm_row_id
    WHERE r.card_id=? AND r.login=? AND r.assignment_id=? AND r.template_vmid=?"""


class StudentLabs:
    def __init__(self, service, ip_start=None, ip_end=None):
        self.service = service
        self.ip_start, self.ip_end = ip_start, ip_end
        self._proofs = OrderedDict()
        self._lock = threading.Lock()
        self._inflight = {}
        self._verify_slots = threading.BoundedSemaphore(4)

    def _range(self):
        try:
            lo, hi = (ipaddress.IPv4Address(x) for x in (self.ip_start, self.ip_end))
            if not (STUDENT_NETWORK.network_address < lo <= hi < STUDENT_NETWORK.broadcast_address):
                raise ValueError
            return str(lo), str(hi)
        except (TypeError, ValueError):
            raise StudentError(503, "Преподавателю нужно задать выделенный диапазон IP для учебных VM.") from None

    @staticmethod
    def _key(login, task, card_id):
        # Assignment and card must be resolved by the portal from an authenticated row.
        ident = task.get("id") if isinstance(task, dict) else None
        m = re.fullmatch(r"lab-([1-9][0-9]{0,9})-([1-9][0-9]{0,8})", ident) if isinstance(ident, str) else None
        if (not m or type(task.get("template_vmid")) is not int or int(m[2])!=task["template_vmid"] or
                not isinstance(login, str) or not re.fullmatch(r"[a-z][a-z0-9-]{2,47}", login) or
                not isinstance(card_id, str) or not re.fullmatch(r"[0-9a-f]{24}", card_id)):
            raise StudentError(404, "Учебная карточка не найдена.")
        return (card_id, login, ident, int(m[2]))

    def _row(self, key):
        return self.service.store.query_one(_ROW, key)

    def submit(self, login, task, card_id, duration_minutes=None, *, student_group=None):
        key = self._key(login, task, card_id)
        if self._row(key) is not None:
            return self.inspect(login, task, card_id)
        if self.service.gateway.mode != "live":
            raise StudentError(503, "Реальное развёртывание сейчас недоступно.")
        if duration_minutes is not None and (type(duration_minutes) is not int or not 1 <= duration_minutes <= 10080):
            raise StudentError(400, "Укажите целое число минут от 1 до 10080.")
        lo, hi = self._range()
        try:
            if not any(t.get("vmid")==key[3] and t.get("name")==task.get("template_name")
                       for t in self.service.gateway.list_templates()):
                raise StudentError(409, "Шаблон задания изменился или недоступен. Обратитесь к преподавателю.")
            blueprint_id = int(key[2].split("-")[1])
            self.service.create_stand(dict(name=f"{login}-{card_id[:8]}", pool_id=f"lab-{card_id}",
                owner=login, blueprint_id=blueprint_id, vm_count=1, subnet=f"{lo}/16",
                start_ip=lo, duration_minutes=duration_minutes), student_receipt={
                    "card_id":card_id, "assignment_id":key[2], "template_vmid":key[3],
                    "template_name":task["template_name"], "ip_end":hi, "student_group":student_group})
        except StudentError:
            raise
        except Exception as exc:
            # A competing request can have committed the receipt, or a response
            # can have been lost AFTER commit. Never issue a second clone then.
            if self._row(key) is None:
                if isinstance(exc, ConflictError) and "диапазоне учебных VM" in str(exc):
                    raise StudentError(409, "Свободные адреса учебных VM закончились. Обратитесь к преподавателю.") from None
                status = 409 if isinstance(exc, (ConflictError, ValidationError)) else 503
                raise StudentError(status, "Заявку не удалось принять. Проверьте сценарий и ресурсы Deployer с преподавателем.") from None
        return self.inspect(login, task, card_id)

    def _endpoint(self, r, *, fresh=False):
        fingerprint = (r["stand_id"],r["vm_row_id"],r["vmid"],r["node"],r["guest_uuid"],r["guest_ip"])
        if self.service.gateway.mode != "live":
            return False
        now = time.monotonic()
        with self._lock:
            cached = self._proofs.get(fingerprint)
            if not fresh and cached and cached[0] > now:
                return cached[1]
            existing = self._inflight.get(fingerprint)
            if existing is None:
                flight = self._inflight[fingerprint] = Future()
        if existing is not None:
            if not fresh:
                return False  # The next poll reuses the completed proof.
            try:
                return existing.result(timeout=12)
            except TimeoutError:
                raise StudentError(503, "Проверяем доступ к VM. Попробуйте ещё раз через несколько секунд.") from None
        # No work queue for automatic polls. Only an explicit password click
        # waits briefly; concurrent requests for the same guest share the proof.
        slot = self._verify_slots.acquire(timeout=3 if fresh else 0)
        ok = False
        try:
            if slot:
                ok = self._live_endpoint(r)
            elif fresh:
                raise StudentError(503, "Проверка учебных VM занята. Попробуйте ещё раз через несколько секунд.")
            return ok
        finally:
            with self._lock:
                if slot:
                    self._proofs[fingerprint] = (time.monotonic() + (30 if ok else 5), ok)
                    self._proofs.move_to_end(fingerprint)
                    if len(self._proofs)>256:
                        self._proofs.popitem(last=False)
                flight.set_result(ok)
                del self._inflight[fingerprint]
            if slot:
                self._verify_slots.release()

    def _live_endpoint(self, r):
        try:
            ip = ipaddress.IPv4Address(r["guest_ip"])
            if not STUDENT_NETWORK.network_address < ip < STUDENT_NETWORK.broadcast_address:
                return False
            gw = self.service.gateway
            members = gw.pool_members(r["pool_id"])
            if (len(members)==1 and (members[0].get("vmid"),members[0].get("name"),members[0].get("status"),
                    members[0].get("node")) == (r["vmid"],r["vm_name"],"running",r["node"]) and
                    gw.timer_vm_uuid(members[0])==r["guest_uuid"]):
                def get(cmd):
                    return _guest(gw, r["node"], r["vmid"], cmd)
                return bool(_guest_mac(get, str(ip)) and _verify_endpoint(get, str(ip)))
        except Exception:
            pass  # Never surface gateway responses/guest command output.
        return False

    def inspect(self, login, task, card_id, detail=False):
        key = self._key(login, task, card_id)
        r = self._row(key)
        result = {"status":"pending","phase":"Заявка ещё не принята Deployer","progress":0,
                  "duration_minutes":None,"expires_at":None}
        if not r:
            return result
        if r["current_stand"] is None:
            return dict(result, status="removed", phase="Стенд убран из Deployer")
        if (r["owner"],r["origin"],r["current_pool"]) != (login,"deployed",r["pool_id"]):
            return dict(result, status="error", phase="Привязка стенда изменилась. Обратитесь к преподавателю")
        result.update(duration_minutes=r["duration_minutes"],expires_at=r["expires_at"])
        if r["expired"]:
            return dict(result, status="expired",phase="Срок работы истёк")
        if r["status"]=="awaiting_confirmation":
            return dict(result,status="awaiting_confirmation",phase="Ожидает подтверждения преподавателя в Deployer")
        if r["status"] in ("provisioning","starting","resetting","stopping","deleting"):
            return dict(result,status="provisioning",phase="Deployer выполняет операцию",
                        progress=max(0,min(int(r["progress"] or 0),99)))
        if r["status"]=="stopped" or r["vm_status"]=="stopped":
            return dict(result,status="stopped",phase="Стенд остановлен")
        if not r["enrolled"]:
            return dict(result,status="error",phase="Готовность VM не подтверждена. Обратитесь к преподавателю")
        if detail:
            if not self._endpoint(r) or self._row(key)!=r:
                return dict(result,status="provisioning",phase="Проверяем доступность личного Proxmox",progress=99)
            result["access"] = {"kind":"proxmox","url":f"https://{r['guest_ip']}:8006/","vmid":r["vmid"],"login":"root@pam"}
        return dict(result,status="ready",phase="Стенд готов",progress=100)

    def credentials(self, login, task, card_id):
        key = self._key(login, task, card_id)
        r = self._row(key)
        if r and r["enrolled"] and self._endpoint(r, fresh=True):
            # A single statement revalidates enrollment/expiry at the moment we select the secret.
            row = self.service.store.query_one("""WITH lab AS (""" + _ROW + """)
                SELECT v.credential_password AS password FROM lab l JOIN stand_vms v ON v.id=l.vm_row_id
                WHERE l.enrolled AND l.guest_uuid=? AND l.vmid=? AND l.guest_ip=? AND l.node=?""",
                (*key, r["guest_uuid"],r["vmid"],r["guest_ip"],r["node"]))
            if row and row["password"]:
                return {"username":"root@pam","password":row["password"]}
        raise StudentError(404, "Доступ к вашей VM пока не подтверждён.")
