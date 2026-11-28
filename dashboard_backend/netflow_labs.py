"""Trusted Netflow job -> existing, receipt-backed Deployer personal lab.

The mapping lives in the Deployer DB, not in student_accounts. No UI session,
Netflow login, password, cached IP or user-provided template is authoritative.
"""
from __future__ import annotations

import ipaddress
import re
import secrets
import sqlite3

from .assignment_audience import parse_stored_groups
from .student_errors import StudentError
from .student_network import STUDENT_NETWORK


_UUID = re.compile(r"[0-9a-f]{32}\Z")
_TASK = re.compile(r"lab-([1-9][0-9]{0,9})-([1-9][0-9]{0,8})\Z")
# netflow_lab_jobs.released: 0 owns the subject's slot. Preserve the reason
# after release: consumers can retry a rejected (never-created) job explicitly.
_REMOVED, _REJECTED = 1, 2


class NetflowRetry(Exception):
    """No authoritative result is available. Leave the job for redelivery."""


def _result(status, progress=0):
    return {"status": status, "progress": progress}


def _allowed_group(db, blueprint, vmid, group):
    row = db.execute("""SELECT assignment_template_name AS name,student_groups FROM blueprints
        WHERE id=? AND template_vmid=? AND student_visible=1 AND status='active'
        AND length(trim(assignment_markdown))>0 AND assignment_template_name!=''""", (blueprint, vmid)).fetchone()
    if not row:
        return None
    groups = parse_stored_groups(row["student_groups"])
    if groups is None:
        return row["name"], group
    # Netflow stores group labels. Only use a case-insensitive alias when it
    # resolves to exactly one local group; never merge distinct audiences.
    if group in groups:
        return row["name"], group
    matches = [g for g in groups if g.casefold() == group.casefold()]
    return (row["name"], matches[0]) if len(matches) == 1 else None


def _safe_url(access):
    url = access.get("url") if isinstance(access, dict) and access.get("kind") == "proxmox" else None
    m = re.fullmatch(r"https://([0-9.]{7,15}):8006/", url) if isinstance(url, str) else None
    if not m:
        return None
    try:
        ip = ipaddress.IPv4Address(m[1])
    except ValueError:
        return None
    return url if STUDENT_NETWORK.network_address < ip < STUDENT_NETWORK.broadcast_address else None


class NetflowLabs:
    def __init__(self, service, labs):
        self.store = service.store
        self.labs = labs
        with self.store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS netflow_lab_jobs(
                job_id TEXT PRIMARY KEY NOT NULL, subject TEXT NOT NULL, assignment_id TEXT NOT NULL,
                student_group TEXT NOT NULL, template_name TEXT NOT NULL, card_id TEXT UNIQUE NOT NULL,
                stand_id INTEGER UNIQUE, released INTEGER NOT NULL DEFAULT 0,
                revoked INTEGER NOT NULL DEFAULT 0)""")
            if "revoked" not in {r[1] for r in db.execute("PRAGMA table_info(netflow_lab_jobs)")}:
                db.execute("ALTER TABLE netflow_lab_jobs ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS netflow_one_lab_per_student
                ON netflow_lab_jobs(subject) WHERE released=0""")
            # The stand id is pinned in the SAME commit as the application creates
            # its receipt. No new stand can be allocated on a lost response, even
            # if a damaged local database has subsequently lost the receipt.
            db.execute("""CREATE TRIGGER IF NOT EXISTS netflow_pin_receipt AFTER INSERT ON student_lab_receipts
                WHEN EXISTS(SELECT 1 FROM netflow_lab_jobs WHERE card_id=NEW.card_id) BEGIN
                  UPDATE netflow_lab_jobs SET stand_id=NEW.stand_id WHERE card_id=NEW.card_id AND
                    released=0 AND stand_id IS NULL AND assignment_id=NEW.assignment_id AND
                    NEW.login='nf-'||subject||'-external';
                  SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT, 'invalid Netflow receipt') END;
                END""")

    @staticmethod
    def _withhold(db, row, job_id, subject, assignment, *, revoke):
        """No VM is deleted or probed. The caller holds BEGIN IMMEDIATE.

        A delayed submit whose receipt has not committed is fenced by 'released'
        in the receipt trigger. If its transaction won instead, the non-NULL
        pin keeps the subject's active slot occupied until the stand is removed.
        """
        if row is None:
            db.execute("""INSERT INTO netflow_lab_jobs
                (job_id,subject,assignment_id,student_group,template_name,card_id,released,revoked)
                VALUES(?,?,?,'','',?,1,1)""", (job_id, subject, assignment, secrets.token_hex(12)))
        elif row["stand_id"] is None:
            db.execute("UPDATE netflow_lab_jobs SET released=1,revoked=1 WHERE job_id=?", (job_id,))
        else:
            if revoke and not row["revoked"]:
                db.execute("UPDATE netflow_lab_jobs SET revoked=1 WHERE job_id=?", (job_id,))
            return _result("error"), None
        return _result("removed"), None

    def _prepare(self, job_id, subject, assignment, group, bp, vmid, *, reserve, reconcile=False):
        with self.store.transaction() as db:
            # Tombstone remains forever. Reimporting the same VM/VMID does
            # not resurrect its former binding. No active job is released
            # merely because the status is 'error' or the assignment is hidden.
            db.execute("""UPDATE netflow_lab_jobs SET released=1 WHERE subject=? AND released=0
                AND stand_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM stands s WHERE s.id=netflow_lab_jobs.stand_id)""", (subject,))
            row = db.execute("SELECT * FROM netflow_lab_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row and (row["subject"], row["assignment_id"]) != (subject, assignment):
                return _result("error"), None
            if row and row["released"]:
                if row["released"] == _REJECTED:
                    if not reconcile:
                        return _result("rejected"), None
                    db.execute("UPDATE netflow_lab_jobs SET released=?,revoked=1 WHERE job_id=?", (_REMOVED, job_id))
                return _result("removed"), None
            if reconcile:
                return self._withhold(db, row, job_id, subject, assignment, revoke=True)
            if row and row["revoked"]:
                return _result("error"), None
            if row and row["student_group"].casefold() != group.casefold():
                return self._withhold(db, row, job_id, subject, assignment, revoke=False)
            authorized = _allowed_group(db, bp, vmid, group)
            if not authorized:
                return self._withhold(db, row, job_id, subject, assignment, revoke=False)
            template, canonical_group = authorized
            if row is None:
                if not reserve or db.execute("""SELECT 1 FROM netflow_lab_jobs
                    WHERE subject=? AND released=0""", (subject,)).fetchone():
                    return _result("error"), None
                card = secrets.token_hex(12)
                db.execute("""INSERT INTO netflow_lab_jobs
                    (job_id,subject,assignment_id,student_group,template_name,card_id)
                    VALUES(?,?,?,?,?,?)""", (job_id, subject, assignment, canonical_group, template, card))
                pin = None
            else:
                if row["template_name"] != template or row["student_group"] != canonical_group:
                    return self._withhold(db, row, job_id, subject, assignment, revoke=False)
                card, pin = row["card_id"], row["stand_id"]
            owner = f"nf-{subject}-external"  # Local teacher-issued logins always end in a serial number.
            if pin is not None and not db.execute("""SELECT 1 FROM student_lab_receipts
                WHERE card_id=? AND stand_id=? AND login=? AND assignment_id=? AND template_vmid=?""",
                (card, pin, owner, assignment, vmid)).fetchone():
                return _result("error"), None
            return None, (owner, {"id": assignment, "template_name": template, "template_vmid": vmid},
                          card, canonical_group, pin)

    def _reject_unallocated(self, jid, subject, assignment, card):
        """Fence other submitters or let the winner's real enrollment be inspected."""
        with self.store.transaction() as db:
            row = db.execute("""SELECT released,stand_id FROM netflow_lab_jobs WHERE job_id=?
                AND subject=? AND assignment_id=? AND card_id=?""", (jid, subject, assignment, card)).fetchone()
            if not row:
                return _result("error")
            if row["released"]:
                return _result("rejected" if row["released"] == _REJECTED else "removed")
            if row["stand_id"] is not None:
                return None
            if db.execute("SELECT 1 FROM student_lab_receipts WHERE card_id=?", (card,)).fetchone():
                # Inconsistent DB: never free a subject if a real VM may exist.
                return _result("error")
            db.execute("UPDATE netflow_lab_jobs SET released=? WHERE job_id=?", (_REJECTED, jid))
            return _result("rejected")

    def handle(self, job):
        """Return only public status/progress and, upon proof, the private VPN-reachable URL."""
        if not isinstance(job, dict):
            return _result("error")
        jid, sub, assignment, group = (job.get(k) for k in ("job_id", "subject", "assignment_id", "group"))
        action = job.get("action", "deploy")
        match = _TASK.fullmatch(assignment) if isinstance(assignment, str) else None
        if (not all(isinstance(i, str) and _UUID.fullmatch(i) for i in (jid, sub)) or not match or
                action not in ("deploy", "reconcile") or (action == "deploy" and
                (not isinstance(group, str) or not 1 <= len(group) <= 80 or group != group.strip()
                 or not group.isprintable()))):
            return _result("error")
        args = (jid, sub, assignment, group, int(match[1]), int(match[2]))
        try:
            result, binding = self._prepare(*args, reserve=action=="deploy", reconcile=action=="reconcile")
            if result:
                return result
            owner, task, card, canonical_group, pin = binding
            if pin is None:
                try:
                    self.labs.submit(owner, task, card, student_group=canonical_group)
                except StudentError as exc:
                    if exc.status not in (400, 403, 404, 409):
                        raise
                    rejection = self._reject_unallocated(jid, sub, assignment, card)
                    if rejection:
                        return rejection
            snap = self.labs.inspect(owner, task, card, detail=True)
            # Authorization and ownership may change during a slow live VM probe.
            result, checked = self._prepare(*args, reserve=False)
            if result:
                return result
            if checked[-1] is None:
                raise NetflowRetry("Receipt not committed")
            state = snap["status"]
            if state == "ready":
                url = _safe_url(snap.get("access"))
                return dict(_result("ready", 100), web_url=url) if url else _result("error")
            if state == "pending":
                raise NetflowRetry("Receipt unavailable")
            state = {"provisioning": "building", "awaiting_confirmation":"awaiting_confirmation",
                     "stopped":"stopped", "expired":"expired", "error":"error"}.get(state, "error")
            progress = snap.get("progress", 0)
            return _result(state, max(0, min(progress, 99)) if type(progress) is int else 0)
        except StudentError as exc:
            if exc.status == 429 or exc.status >= 500:
                raise NetflowRetry("Local deployer temporarily unavailable") from None
            return _result("error")
        except sqlite3.OperationalError:
            raise NetflowRetry("Local database unavailable") from None
