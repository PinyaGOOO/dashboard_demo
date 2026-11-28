"""Durable Netflow-side mailbox. Install beside the existing Netflow HTTP handler.

The caller authenticates the browser/worker. This module re-reads the authoritative
user record; no login, password, session token or caller-supplied owner is replicated.
"""
import copy
import hashlib
import hmac
import ipaddress
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class QueueError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


_ASSIGNMENT = re.compile(r"lab-[1-9][0-9]{0,9}-[1-9][0-9]{0,8}\Z")
_UUID = re.compile(r"[0-9a-f]{32}\Z")
_TOKEN = re.compile(r"[a-zA-Z0-9_-]{16,180}\Z")
_STATES = frozenset(("queued", "building", "awaiting_confirmation", "ready", "stopped",
                     "expired", "error", "removed", "rejected"))
_REJECTION_VISIBLE_SECONDS = 24 * 3600
_NETWORK = ipaddress.ip_network("10.39.0.0/16")


def _query(db, statement, *args):
    cursor = db.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(statement, args)


def _group(value):
    if isinstance(value, str) and 1 <= len(value.strip()) <= 60 and value.strip().isprintable():
        return value.strip().casefold()
    return None


def _validate_scenarios(scenarios):
    if not isinstance(scenarios, dict) or len(scenarios) > 512:
        raise ValueError("Invalid scenario configuration")
    config = {}
    for assignment, groups in scenarios.items():
        if (not isinstance(assignment, str) or not _ASSIGNMENT.fullmatch(assignment) or
                not isinstance(groups, list) or len(groups) > 256):
            raise ValueError("Invalid scenario configuration")
        allowed = {}
        for spelling in groups:
            key = _group(spelling)
            if not key or key in allowed:
                raise ValueError("Invalid or ambiguous group in scenario configuration")
            allowed[key] = spelling.strip()
        config[assignment] = allowed  # an empty list grants nobody access
    return config


def _url(value):
    if not isinstance(value, str) or len(value) > 96:
        raise QueueError(400, "Некорректный адрес стенда.")
    m = re.fullmatch(r"https://(10\.39\.[0-9]{1,3}\.[0-9]{1,3}):8006/", value)
    try:
        ip = ipaddress.IPv4Address(m[1]) if m else None
    except ValueError:
        ip = None
    if not ip or not _NETWORK.network_address < ip < _NETWORK.broadcast_address:
        raise QueueError(400, "Некорректный адрес стенда.")
    return value


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS nf_deployer_subjects (
        subject TEXT PRIMARY KEY NOT NULL, user_id INTEGER UNIQUE
        REFERENCES users(id) ON DELETE SET NULL)""",
    """CREATE TABLE IF NOT EXISTS nf_deployer_jobs (
        job_id TEXT PRIMARY KEY NOT NULL, subject TEXT NOT NULL REFERENCES nf_deployer_subjects(subject),
        assignment_id TEXT NOT NULL, group_key TEXT NOT NULL, group_label TEXT, status TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, due_at REAL NOT NULL,
        checked_at REAL, lease_hash BLOB, lease_kind TEXT, web_url TEXT)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS nf_deployer_one_stand_v2
        ON nf_deployer_jobs(subject) WHERE status NOT IN ('removed','rejected')""",
    """CREATE INDEX IF NOT EXISTS nf_deployer_next_v2
        ON nf_deployer_jobs(due_at, created_at) WHERE status NOT IN ('removed','rejected')""",
    """CREATE INDEX IF NOT EXISTS nf_deployer_history ON nf_deployer_jobs(subject)""",
    # Netflow can delete users with foreign_keys OFF. Also cover SQLite REPLACE
    # (implicit deletion doesn't fire delete triggers with recursive_triggers OFF).
    """CREATE TRIGGER IF NOT EXISTS nf_deployer_retire BEFORE DELETE ON users BEGIN
        UPDATE nf_deployer_subjects SET user_id=NULL WHERE user_id=OLD.id;
        END""",
    """CREATE TRIGGER IF NOT EXISTS nf_deployer_new_identity AFTER INSERT ON users BEGIN
        UPDATE nf_deployer_subjects SET user_id=NULL WHERE user_id=NEW.id;
        END""",
    """CREATE TRIGGER IF NOT EXISTS nf_deployer_subject_changed_v2 AFTER UPDATE OF user_id ON nf_deployer_subjects
        WHEN NEW.user_id IS NOT OLD.user_id BEGIN
        UPDATE nf_deployer_jobs SET checked_at=NULL, web_url=NULL, lease_hash=NULL, due_at=0
            WHERE status NOT IN ('removed','rejected') AND subject=NEW.subject;
        END""",
    """CREATE TRIGGER IF NOT EXISTS nf_deployer_recheck_v2 AFTER UPDATE OF grp,active,admin,must_change ON users
        WHEN NEW.grp IS NOT OLD.grp OR NEW.active IS NOT OLD.active OR NEW.admin IS NOT OLD.admin
            OR NEW.must_change IS NOT OLD.must_change
        BEGIN
        UPDATE nf_deployer_jobs SET checked_at=NULL, web_url=NULL, lease_hash=NULL, due_at=0
            WHERE status NOT IN ('removed','rejected') AND subject IN
                (SELECT subject FROM nf_deployer_subjects WHERE user_id=NEW.id);
        END""",
)


class StandQueue:
    """Construct once at startup. Pass .using(c) inside Netflow's BEGIN IMMEDIATE.

    Standalone methods own their transactions; the bound facade never controls
    the caller's transaction, connection or row factory. No worker thread or
    remote calls are started here.
    """
    def __init__(self, db_path, scenarios, *, clock=time.time, lease_seconds=90,
                 poll_seconds=10, freshness_seconds=60):
        self._uri = Path(db_path).resolve().as_uri() + "?mode=rw"
        self._config = _validate_scenarios(scenarios)
        if (any(type(i) is not int or not 1 <= i <= 86400
                for i in (lease_seconds, poll_seconds, freshness_seconds))
                or freshness_seconds <= poll_seconds):
            raise ValueError("Invalid queue timing")
        self._lease, self._poll, self._fresh = lease_seconds, poll_seconds, freshness_seconds
        self._clock = clock
        self._bound = None
        with self._transaction(write=True) as c:
            _query(c, "SELECT id,grp,admin,active,must_change FROM users LIMIT 0")
            for stmt in _SCHEMA:
                c.execute(stmt)
            # The old unique index considered a pre-VM rejection an active stand.
            # Remove it only AFTER its replacement exists, in the same transaction.
            c.execute("DROP INDEX IF EXISTS nf_deployer_one_stand")
            c.execute("DROP INDEX IF EXISTS nf_deployer_next")
            c.execute("DROP TRIGGER IF EXISTS nf_deployer_recheck")
            c.execute("DROP TRIGGER IF EXISTS nf_deployer_subject_changed")
            columns = {r["name"] for r in _query(c, "PRAGMA table_info(nf_deployer_jobs)")}
            if "lease_kind" not in columns:
                c.execute("ALTER TABLE nf_deployer_jobs ADD COLUMN lease_kind TEXT")
                c.execute("UPDATE nf_deployer_jobs SET lease_hash=NULL,due_at=0 WHERE lease_hash IS NOT NULL")
            if "group_label" not in columns:
                c.execute("ALTER TABLE nf_deployer_jobs ADD COLUMN group_label TEXT")

    def using(self, connection):
        """Caller must own an active transaction on this same Netflow database."""
        obj = copy.copy(self)
        obj._bound = connection
        return obj

    @contextmanager
    def _transaction(self, *, write=False):
        if self._bound is not None:
            if not self._bound.in_transaction:
                raise RuntimeError("Bound Netflow connection must already be in a transaction")
            yield self._bound
            return
        c = sqlite3.connect(self._uri, uri=True, timeout=5, isolation_level=None)
        try:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield c
            c.commit()
        finally:
            c.close()  # also rolls back on an exception

    def _member(self, c, user_id):
        if type(user_id) is not int or not 0 < user_id <= 2**63-1:
            raise QueueError(403, "Требуется действующая учебная учётная запись.")
        member = _query(c, """SELECT u.grp, s.subject FROM users u
            LEFT JOIN nf_deployer_subjects s ON s.user_id=u.id WHERE u.id=?
            AND u.active=1 AND u.admin=0 AND u.must_change=0""", user_id).fetchone()
        if member is None:
            raise QueueError(403, "Требуется действующая учебная учётная запись.")
        return member

    def _allowed(self, job, group):
        key = _group(group)
        return (key is not None and key == job["group_key"]
                and key in self._config.get(job["assignment_id"], {}))

    def _kind(self, job):
        return ("deploy" if (job["active"], job["admin"], job["must_change"]) == (1, 0, 0)
                and self._allowed(job, job["grp"]) else "reconcile")

    def scenarios(self, user_id):
        with self._transaction() as c:
            group = _group(self._member(c, user_id)["grp"])
            return {"scenarios": [{"assignment_id": a} for a, groups in self._config.items() if group in groups]}

    @staticmethod
    def _active(c, subject):
        return _query(c, "SELECT * FROM nf_deployer_jobs WHERE subject=? AND status NOT IN ('removed','rejected')",
                      subject).fetchone()

    def _public(self, job):
        if job is None:
            return {"stand": None}
        state = job["status"]
        url = None
        if state == "ready":
            now = self._clock()
            if job["checked_at"] is not None and 0 <= now - job["checked_at"] < self._fresh:
                url = job["web_url"]
            else:
                state = "verifying"
        return {"stand": {"job_id": job["job_id"], "assignment_id": job["assignment_id"],
                          "status": state, "progress": job["progress"], "web_url": url}}

    def get(self, user_id):
        with self._transaction() as c:
            member = self._member(c, user_id)
            job = self._active(c, member["subject"])
            if job and not self._allowed(job, member["grp"]):
                raise QueueError(403, "Задание больше не доступно этой группе.")
            if job is None and member["subject"]:
                # Only the most recent request qualifies. Do not resurrect an older
                # rejection when a later stand is released or its audience changes.
                last = _query(c, """SELECT * FROM nf_deployer_jobs WHERE subject=?
                    ORDER BY rowid DESC LIMIT 1""", member["subject"]).fetchone()
                if (last and last["status"] == "rejected" and self._allowed(last, member["grp"])
                        and last["checked_at"] is not None and
                        0 <= self._clock() - last["checked_at"] < _REJECTION_VISIBLE_SECONDS):
                    job = last
            return self._public(job)

    def request(self, user_id, assignment_id):
        if not isinstance(assignment_id, str) or not _ASSIGNMENT.fullmatch(assignment_id):
            raise QueueError(400, "Некорректный сценарий.")
        with self._transaction(write=True) as c:
            member = self._member(c, user_id)
            key = _group(member["grp"])
            if key not in self._config.get(assignment_id, {}):
                raise QueueError(403, "Сценарий не разрешён для этой группы.")
            subject = member["subject"]
            if subject is None:
                subject = uuid.uuid4().hex
                _query(c, "INSERT INTO nf_deployer_subjects(subject,user_id) VALUES (?,?)", subject, user_id)
            job = self._active(c, subject)
            if job is not None:
                if self._allowed(job, member["grp"]) and job["assignment_id"] == assignment_id:
                    return self._public(job)
                raise QueueError(409, "Сначала преподаватель должен освободить предыдущий стенд.")
            now = self._clock()
            _query(c, """INSERT INTO nf_deployer_jobs(job_id,subject,assignment_id,group_key,group_label,status,created_at,due_at)
                VALUES(?,?,?,?,?,'queued',?,?)""", uuid.uuid4().hex, subject, assignment_id, key,
                   self._config[assignment_id][key], now, now)
            return self._public(self._active(c, subject))

    def claim(self):
        now = self._clock()
        with self._transaction(write=True) as c:
            j = _query(c, """SELECT j.*, u.grp,u.active,u.admin,u.must_change FROM nf_deployer_jobs j
                JOIN nf_deployer_subjects s ON j.subject=s.subject LEFT JOIN users u ON u.id=s.user_id
                WHERE j.status NOT IN ('removed','rejected') AND j.due_at <= ?
                ORDER BY j.due_at,j.created_at,j.rowid LIMIT 1""", now).fetchone()
            if j is None:
                return {"job": None}
            kind = self._kind(j)
            token = secrets.token_urlsafe(32)
            _query(c, "UPDATE nf_deployer_jobs SET lease_kind=?,lease_hash=?,due_at=? WHERE job_id=?",
                   kind, hashlib.sha256(token.encode("ascii")).digest(), now+self._lease, j["job_id"])
            job = {"job_id": j["job_id"], "subject": j["subject"], "assignment_id": j["assignment_id"],
                   "group": self._config.get(j["assignment_id"], {}).get(j["group_key"], j["group_label"] or j["group_key"]),
                   "lease_token": token}
            if kind == "reconcile":
                # Never ask the worker to deploy without authorization. Reconciliation
                # checks the *old* subject/receipt and can only report a closed/no-link state.
                job["action"] = "reconcile"
                _query(c, "UPDATE nf_deployer_jobs SET checked_at=NULL,web_url=NULL WHERE job_id=?", j["job_id"])
            return {"job": job}

    def report(self, job_id, lease_token, status, progress, web_url=None):
        if (not isinstance(job_id, str) or not _UUID.fullmatch(job_id)
                or not isinstance(lease_token, str) or not _TOKEN.fullmatch(lease_token)
                or not isinstance(status, str) or status not in _STATES
                or type(progress) is not int or not 0 <= progress <= 100
                or (web_url is not None and status != "ready")):
            raise QueueError(400, "Некорректный отчёт обработчика.")
        link = _url(web_url) if web_url is not None else None
        token_hash = hashlib.sha256(lease_token.encode("ascii")).digest()
        with self._transaction(write=True) as c:
            now = self._clock()
            job = _query(c, """SELECT j.*,u.grp,u.active,u.admin,u.must_change FROM nf_deployer_jobs j
                JOIN nf_deployer_subjects s ON s.subject=j.subject LEFT JOIN users u ON u.id=s.user_id
                WHERE j.job_id=? AND j.status NOT IN ('removed','rejected')""",
                         job_id).fetchone()
            if (not job or job["lease_kind"] != self._kind(job) or not job["lease_hash"]
                    or now >= job["due_at"] or not hmac.compare_digest(job["lease_hash"], token_hash)):
                raise QueueError(409, "Подтверждение устарело. Получите новую заявку.")
            if job["lease_kind"] == "reconcile" and (status not in ("error", "removed") or link is not None):
                raise QueueError(409, "Для этой заявки разрешена только сверка состояния.")
            _query(c, """UPDATE nf_deployer_jobs SET status=?,progress=?,web_url=?,checked_at=?,
                lease_hash=NULL,lease_kind=NULL,due_at=? WHERE job_id=?""", status, progress, link, now, now+self._poll, job_id)
            return {"ok": True}
