"""Student lab state. A verified operator binding may expose a guest URL, never cluster access."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import student_accounts
from .student_errors import StudentError
from .student_network import STUDENT_NETWORK


# Fictitious, shared demonstration personas, not institutional user identities.
# Each stores a different salt and a PBKDF2-SHA256 verifier (180,000 rounds).
_ACCOUNTS = {
    "student01": ("Алина Соколова", "ДЭ-24", "602c84f60efaf80fd55c76facbf01528",
                  "7f65fdc72154710317e376969259de0b696787f411bb13e54d67870192404f6d"),
    "student02": ("Максим Орлов", "ДЭ-25", "e1d707a018c3e62a1ca06ce66fa54d1c",
                  "3c5b33aaa5ff044e6004fa20c00ff66eb6096442543f04898eb4c76004cc789f"),
}
_APP_ID = 0x53545544  # STUD: never initialize in someone else's SQLite database.
_BINDING_FIELDS = {"student_login", "assignment_id", "stand_id", "guest_ipv4", "vmid",
                   "deployer_stand_id", "pool_id"}
_STANDS = """SELECT s.*, t.duration_minutes, r.stand_id AS real_request FROM student_stands s
             LEFT JOIN student_demo_leases t ON t.stand_id=s.id
             LEFT JOIN student_lab_requests r ON r.stand_id=s.id"""


def _binding(path: Path, *, pending: bool = False) -> dict | None:
    """Read one bounded, operator-owned binding. Re-read every time so revocation is immediate."""
    if path.is_symlink():
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or
                (os.name == "posix" and (info.st_mode & 0o022 or info.st_uid not in (0, os.geteuid())))):
            return None
        raw = stream.read(4097)
    if len(raw) > 4096:
        return None
    value = json.loads(raw.decode("utf-8"))
    required = _BINDING_FIELDS - {"vmid"} if pending else _BINDING_FIELDS
    if not isinstance(value, dict) or set(value) != required:
        return None
    if not pending and (type(value["vmid"]) is not int or not 100 <= value["vmid"] <= 999999999):
        return None
    if (type(value["deployer_stand_id"]) is not int or not 0 < value["deployer_stand_id"] <= 2**63 - 1
            or not isinstance(value["pool_id"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value["pool_id"])
            or value["pool_id"] in {".", ".."} or not isinstance(value["guest_ipv4"], str)):
        return None
    ip = ipaddress.IPv4Address(value["guest_ipv4"])
    if str(ip) != value["guest_ipv4"] or ip not in STUDENT_NETWORK or ip in (STUDENT_NETWORK.network_address, STUDENT_NETWORK.broadcast_address):
        return None
    return value


def read_pilot_request(path: Path) -> dict | None:
    """Read the ops-only intent, never a source of access URLs or authorization."""
    try:
        value = _binding(path, pending=True)
        if (not value or not isinstance(value["student_login"], str) or value["student_login"] not in _ACCOUNTS
                or not isinstance(value["stand_id"], str) or not re.fullmatch(r"[a-f0-9]{24}", value["stand_id"])
                or not isinstance(value["assignment_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_:-][A-Za-z0-9._:-]{0,127}", value["assignment_id"])):
            return None
        return value
    except (OSError, ValueError, RecursionError):
        return None


class StudentPortal:
    SESSION_TTL = 8 * 3600
    DEPLOY_SECONDS = 8

    def __init__(self, db_path: str | Path, catalog: Callable[[], list[dict]], *, clock=time.time,
                 pilot_validator: Callable[[dict], bool] | None = None, pilot_grant=None, set_duration=None,
                 labs=None, published_catalog=None, image_reader=None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._catalog = catalog
        self._published_catalog = published_catalog
        self._image_reader = image_reader
        self._clock = clock
        self._pilot_validator = pilot_validator
        self._pilot_grant = pilot_grant
        self._set_duration = set_duration
        self._labs = labs
        try:
            fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            if app_id == 0 and db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None:
                db.execute(f"PRAGMA application_id={_APP_ID}")
            elif app_id != _APP_ID:
                raise ValueError("Для студенческого демо требуется отдельный файл SQLite")
            student_accounts.init_schema(db)
            db.execute("""CREATE TABLE IF NOT EXISTS student_sessions (
                token_hash BLOB PRIMARY KEY, login TEXT NOT NULL, expires REAL NOT NULL)""")
            # Old releases insert positional session rows. No existing/public
            # session acquires personal-account privileges during this upgrade.
            db.execute("""CREATE TABLE IF NOT EXISTS student_private_sessions (
                token_hash BLOB PRIMARY KEY REFERENCES student_sessions(token_hash) ON DELETE CASCADE,
                binding_stamp TEXT NOT NULL)""")
            for login in _ACCOUNTS:
                if self._private_login(login):
                    db.execute("""DELETE FROM student_sessions WHERE login=? AND NOT EXISTS (
                        SELECT 1 FROM student_private_sessions p WHERE p.token_hash=student_sessions.token_hash
                    )""", (login,))
            db.execute("""CREATE TABLE IF NOT EXISTS student_stands (
                id TEXT PRIMARY KEY, login TEXT NOT NULL, assignment_id TEXT NOT NULL,
                task_json TEXT NOT NULL, started REAL NOT NULL, UNIQUE(login, assignment_id))""")
            # The previous release inserts all five columns positionally. Keep
            # that schema intact so an application rollback can still create labs.
            db.execute("""CREATE TABLE IF NOT EXISTS student_demo_leases (
                stand_id TEXT PRIMARY KEY REFERENCES student_stands(id) ON DELETE CASCADE,
                duration_minutes INTEGER NOT NULL CHECK (duration_minutes BETWEEN 1 AND 10080))""")
            # A durable request is committed before calling the external deployer.
            # Reading a card never starts a clone, even after a crash between commits.
            db.execute("""CREATE TABLE IF NOT EXISTS student_lab_requests (
                stand_id TEXT PRIMARY KEY REFERENCES student_stands(id) ON DELETE CASCADE)""")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _token_hash(token: str | None) -> bytes:
        if not isinstance(token, str) or len(token) != 43 or not token.isascii():
            raise StudentError(401, "Войдите в учебный кабинет")
        return hashlib.sha256(token.encode("ascii")).digest()

    def _private_login(self, login: str) -> bool:
        return self._pilot_grant is not None and self._pilot_grant.private_login_required(login)

    def demo_profiles(self) -> list[str]:
        return [login for login in _ACCOUNTS if not self._private_login(login)]

    def create_accounts(self, payload):
        """Only call through the authenticated teacher endpoint; never a student route."""
        return student_accounts.create_batch(self._db, payload, _ACCOUNTS, self._clock)

    def list_accounts(self, search="", offset=0, limit=50, sort="login"):
        return student_accounts.list_accounts(self._db, _ACCOUNTS, search, offset, limit, sort)

    def account_login_history(self, login):
        return student_accounts.account_login_history(self._db, _ACCOUNTS, login)

    def manage_account(self, login, action, payload):
        return student_accounts.manage_account(self._db, _ACCOUNTS, login, action, payload)

    def _managed(self, login, db=None):
        if login in _ACCOUNTS:
            return False
        if db is None:
            with self._db() as connection:
                return self._managed(login, connection)
        return db.execute("SELECT 1 FROM student_accounts WHERE login=?", (login,)).fetchone() is not None

    def _session(self, db: sqlite3.Connection, token: str | None) -> tuple[str, str | None]:
        row = db.execute("""SELECT s.login, p.binding_stamp FROM student_sessions s
                         LEFT JOIN student_private_sessions p ON p.token_hash=s.token_hash
                         WHERE s.token_hash=? AND s.expires>?""",
                         (self._token_hash(token), self._clock())).fetchone()
        if row is None:
            raise StudentError(401, "Сессия завершена. Войдите снова")
        login, stamp = row
        if login not in _ACCOUNTS:
            current = student_accounts.stamp(student_accounts.lookup(db, login))
            if not current or not stamp or not hmac.compare_digest(stamp, current):
                raise StudentError(401, "Сессия завершена. Войдите снова")
        elif self._private_login(login):
            current = self._pilot_grant.session_stamp(login) if stamp else None
            if not current or not hmac.compare_digest(stamp, current):
                raise StudentError(401, "Сессия завершена. Войдите снова")
        elif stamp is not None:
            raise StudentError(401, "Сессия завершена. Войдите снова")
        return login, stamp

    def login(self, login: str, password: str) -> str:
        if not isinstance(login, str) or not isinstance(password, str) or len(password) > 256:
            raise StudentError(401, "Неверный логин или пароль")
        login = login.strip().lower()
        entry = _ACCOUNTS.get(login)
        stamp = None
        if entry is None:
            with self._db() as db:
                account = student_accounts.lookup(db, login)
            stamp = student_accounts.authenticate(account, password)
            if not stamp:
                raise StudentError(401, "Неверный логин или пароль")
        elif self._private_login(login):
            stamp = self._pilot_grant.authenticate_login(login, password)
            if not stamp:
                raise StudentError(401, "Неверный логин или пароль")
        else:
            salt, verifier = entry[2:]
            candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 180_000)
            if not hmac.compare_digest(candidate, bytes.fromhex(verifier)):
                raise StudentError(401, "Неверный логин или пароль")
        token = secrets.token_urlsafe(32)
        digest = self._token_hash(token)
        now = self._clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if entry is None:
                # Recheck after hashing, under the same writer lock used for
                # administrative changes. A revoked login must not mint a session.
                current = student_accounts.stamp(student_accounts.lookup(db, login))
                if not current or not hmac.compare_digest(stamp, current):
                    raise StudentError(401, "Неверный логин или пароль")
                db.execute("UPDATE student_accounts SET last_login=? WHERE login=?", (now, login))
            db.execute("DELETE FROM student_sessions WHERE expires<=?", (now,))
            db.execute("INSERT INTO student_sessions VALUES (?,?,?)",
                       (digest, login, now + self.SESSION_TTL))
            if stamp is not None:
                db.execute("INSERT INTO student_private_sessions (token_hash,binding_stamp) VALUES (?,?)",
                           (digest, stamp))
        return token

    def logout(self, token: str | None) -> None:
        try:
            digest = self._token_hash(token)
        except StudentError:
            return
        with self._db() as db:
            db.execute("DELETE FROM student_sessions WHERE token_hash=?", (digest,))

    def _assigned(self, login: str) -> dict[str, dict]:
        # The catalogue provider supplies public teaching material only. Do not
        # let additional fields such as scripts/credentials into the student API.
        unique = {}
        classroom = self._published_catalog is not None and (self._managed(login) or self._private_login(login))
        for source in (self._published_catalog() if classroom else self._catalog()):
            key = source["id"]
            if not isinstance(key, str) or not key or key in unique:
                continue
            task = {k: source[k] for k in ("id", "title", "description", "template_name", "template_vmid")}
            task["steps"] = list(source["steps"])
            if isinstance(source.get("assignment_markdown"), str):
                task["assignment_markdown"] = source["assignment_markdown"]
                if classroom and self._image_reader is not None:
                    task["images_available"] = True
            unique[key] = task
        keys = list(unique)
        if classroom or len(keys) < 2 or self._managed(login):
            selected = keys
        elif login == "student01":
            selected = keys[:2] + keys[3::2]
        else:
            selected = keys[1:3] + keys[4::2]
        return {key: unique[key] for key in selected}

    def assignment_image(self, token, assignment_id, image_id):
        with self._db() as db:
            login, stamp = self._session(db, token)
            if not (stamp and self._image_reader and self._published_catalog and
                    (self._managed(login, db) or self._private_login(login))):
                raise StudentError(404, "Изображение недоступно")
        if assignment_id not in self._assigned(login):
            raise StudentError(404, "Изображение недоступно")
        # Reader verifies the active publication AND membership of this exact
        # attachment in the same SQL statement that returns its bytes.
        return self._image_reader(assignment_id, image_id)

    @staticmethod
    def _duration(value):
        if value is not None and (type(value) is not int or not 1 <= value <= 10080):
            raise StudentError(400, "Укажите целое число минут от 1 до 10080 или выберите бессрочный режим")
        return value

    @staticmethod
    def _matches(binding, row):
        return binding and all(binding[k] == row[col] for k, col in (
            ("student_login", "login"), ("assignment_id", "assignment_id"), ("stand_id", "id")))

    def _owned_binding(self, row):
        if self._pilot_validator is not None:
            try:
                binding = _binding(self.db_path.with_name("student-pilot.json"))
                if self._matches(binding, row):
                    return binding
            except (OSError, ValueError, RecursionError):
                pass
        return None

    def _verified(self, binding):
        try:
            return self._pilot_validator(binding) is True
        except Exception:
            return False

    def _stand(self, row: sqlite3.Row, *, detail=False) -> dict:
        if row["real_request"]:
            return self._real_state(row, detail=detail)
        elapsed = max(0.0, self._clock() - row["started"])
        progress = min(100, int(elapsed / self.DEPLOY_SECONDS * 100))
        if progress == 100:
            phase = "Стенд готов"
        elif progress >= 70:
            phase = "Прикрепляем задание"
        elif progress >= 30:
            phase = "Готовим учебную среду"
        else:
            phase = "Создаём персональный стенд"
        status = "ready" if progress == 100 else "provisioning"
        minutes = row["duration_minutes"]
        deadline = row["started"] + self.DEPLOY_SECONDS + minutes*60 if minutes else None
        stand = {"id": row["id"], "name": f"{row['login']}-{row['id'][:8]}", "status": status,
                 "progress": progress, "phase": phase, "duration_minutes": minutes,
                 "expires_at": datetime.fromtimestamp(deadline, timezone.utc).isoformat() if deadline else None}
        binding = self._owned_binding(row)
        if binding:
            lease = self._pilot_grant.lease(binding) if self._pilot_grant is not None else None
            if lease:
                stand.update(actual_lease=lease, duration_minutes=lease["duration_minutes"], expires_at=lease["expires_at"])
                if lease["status"] in ("expired", "stopped"):
                    stand.update(status=lease["status"], phase="Срок истёк" if lease["status"] == "expired" else "Остановлен")
                    return stand
            if detail and status == "ready" and self._verified(binding):
                stand["access"] = {"kind": "proxmox", "url": f"https://{binding['guest_ipv4']}:8006/",
                                   "vmid": binding["vmid"], "login": "root@pam"}
                return stand
            if lease or not detail:
                return stand
        pending_pilot = self._pilot_validator is not None and self._matches(
            read_pilot_request(self.db_path.with_name("student-pilot-request.json")), row)
        if pending_pilot:
            if detail:
                stand["pilot_pending"] = True
        elif not binding and self._labs is not None and self._private_login(row["login"]):
            # Old simulated assignment of the now-personal pilot account. Tell the
            # truth and require an explicit new click; a page view never creates a VM.
            return self._real_state(row, snapshot={"status":"pending", "progress":0,
                                        "phase":"Для этого задания реальный стенд ещё не заказан"})
        elif deadline and self._clock() >= deadline:
            stand.update(status="expired", phase="Срок истёк")
        return stand

    def get_dashboard(self, token: str | None) -> dict:
        with self._db() as db:
            login, _ = self._session(db, token)
            tasks = {key: dict(task, stand=None) for key, task in self._assigned(login).items()}
            rows = db.execute(_STANDS + " WHERE s.login=? ORDER BY s.started, s.id", (login,))
            for row in rows:
                key = row["assignment_id"]
                if self._published_catalog is None:
                    task = json.loads(row["task_json"])  # standalone demo snapshot
                elif key in tasks:
                    task = tasks[key]
                else:
                    task = self._withdrawn_task(row)
                    if task is None:
                        continue
                tasks[key] = dict(task, stand=self._stand(row))
            if login in _ACCOUNTS:
                name, group = _ACCOUNTS[login][:2]
                managed = self._private_login(login)
            else:
                account = db.execute("SELECT group_name FROM student_accounts WHERE login=?", (login,)).fetchone()
                if account is None:
                    raise StudentError(401, "Сессия завершена. Войдите снова")
                name, group, managed = login, account[0], True
        return {"student": {"login": login, "name": name, "group": group, "managed": managed},
                "assignments": list(tasks.values())}

    def _real_state(self, row, *, detail=False, snapshot=None):
        state = snapshot if snapshot is not None else (
            self._labs.inspect(row["login"], json.loads(row["task_json"]), row["id"], detail=detail)
            if self._labs else {"status": "paused", "phase": "Сервис развёртывания недоступен"})
        result = {"id": row["id"], "name": f"{row['login']}-{row['id'][:8]}", "real": True,
                  "duration_minutes": row["duration_minutes"], "expires_at": None, "progress": 0}
        for key in ("status", "phase", "progress", "duration_minutes", "expires_at"):
            if key in state and not (key == "duration_minutes" and state.get("status") == "pending"):
                result[key] = state[key]
        if detail and state.get("status") == "ready" and state.get("access"):
            result["access"] = state["access"]
        return result

    def _withdrawn_task(self, row):
        # Keep the owner's existing VM usable without revealing a withdrawn
        # assignment or the stale Markdown pinned in its original request.
        if row["real_request"]:
            if self._real_state(row)["status"] in {"pending", "removed"}:
                return None
        elif not self._owned_binding(row) and not self._matches(
                read_pilot_request(self.db_path.with_name("student-pilot-request.json")), row):
            return None
        return {"id": row["assignment_id"], "title": "Мой стенд", "withdrawn": True,
                "description": "", "steps": [], "template_name": "—", "template_vmid": None}

    def _prepare_real(self, db, login, assignment_id, row, minutes, assigned):
        if self._labs is None:
            raise StudentError(503, "Сервис реальных стендов не настроен. Обратитесь к преподавателю.")
        # One retained personal VM per student. Concurrent tabs share this SQLite
        # write transaction; a pending request also reserves the slot.
        for other in db.execute(_STANDS + " WHERE s.login=?", (login,)):
            if other["assignment_id"] == assignment_id:
                continue
            if other["real_request"]:
                status = self._real_state(other)["status"]
                if status == "removed" or (status == "pending" and self._published_catalog is not None
                                           and other["assignment_id"] not in assigned):
                    continue
            elif not self._owned_binding(other) and not self._matches(
                    read_pilot_request(self.db_path.with_name("student-pilot-request.json")), other):
                continue
            raise StudentError(409, "У вас уже есть личный стенд или заявка. Для смены задания обратитесь к преподавателю.")
        if row and row["real_request"] and self._real_state(row)["status"] != "removed":
            return row  # reuse the exact saved task, duration and request ID
        task = assigned.get(assignment_id)
        if not task:
            raise StudentError(404, "Задание не найдено среди опубликованных")
        if row:
            db.execute("DELETE FROM student_stands WHERE id=? AND login=?", (row["id"], login))
        sid = secrets.token_hex(12)
        db.execute("INSERT INTO student_stands (id,login,assignment_id,task_json,started) VALUES (?,?,?,?,?)",
                   (sid,login,assignment_id,json.dumps(task,ensure_ascii=False),self._clock()))
        db.execute("INSERT INTO student_lab_requests (stand_id) VALUES (?)", (sid,))
        if minutes is not None:
            db.execute("INSERT INTO student_demo_leases VALUES (?,?)", (sid,minutes))
        return db.execute(_STANDS + " WHERE s.id=? AND s.login=?", (sid,login)).fetchone()

    def deploy(self, token: str | None, assignment_id: str, duration_minutes=None) -> dict:
        self._duration(duration_minutes)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            login, _ = self._session(db, token)
            assigned = self._assigned(login)
            if self._published_catalog is not None and assignment_id not in assigned:
                raise StudentError(404, "Задание не опубликовано или недоступно")
            row = db.execute(_STANDS + " WHERE s.login=? AND s.assignment_id=?",
                             (login, assignment_id)).fetchone()
            personal = self._managed(login, db) or (self._labs is not None and self._private_login(login))
            if personal:
                if row and not row["real_request"] and (self._owned_binding(row) or self._matches(
                        read_pilot_request(self.db_path.with_name("student-pilot-request.json")), row)):
                    return self._stand(row)
                row = self._prepare_real(db, login, assignment_id, row, duration_minutes, assigned)
            elif row is not None and self._stand(row)["status"] == "expired" and not self._owned_binding(row):
                intent = read_pilot_request(self.db_path.with_name("student-pilot-request.json"))
                if not self._matches(intent, row):
                    db.execute("DELETE FROM student_stands WHERE id=? AND login=?", (row["id"], login))
                    row = None
            if row is None:
                task = assigned.get(assignment_id)
                if task is None:
                    raise StudentError(404, "Задание не найдено")
                stand_id = secrets.token_hex(12)
                db.execute("INSERT INTO student_stands (id,login,assignment_id,task_json,started) VALUES (?,?,?,?,?)",
                           (stand_id, login, assignment_id, json.dumps(task, ensure_ascii=False), self._clock()))
                if duration_minutes is not None:
                    db.execute("INSERT INTO student_demo_leases VALUES (?,?)", (stand_id,duration_minutes))
                row = db.execute(_STANDS + " WHERE s.id=? AND s.login=?", (stand_id, login)).fetchone()
            if not personal:
                return self._stand(row)
        snapshot = self._labs.submit(login, json.loads(row["task_json"]), row["id"], row["duration_minutes"])
        return self._real_state(row, snapshot=snapshot)

    def _own_row_and_stamp(self, token, stand_id):
        with self._db() as db:
            login, stamp = self._session(db, token)
            row = db.execute(_STANDS + " WHERE s.id=? AND s.login=?", (stand_id, login)).fetchone()
            if row is None:
                raise StudentError(404, "Стенд не найден")
        return row, stamp

    def get_stand(self, token, stand_id):
        row, _ = self._own_row_and_stamp(token, stand_id)
        if self._published_catalog is None:
            task = json.loads(row["task_json"])
        else:
            task = self._assigned(row["login"]).get(row["assignment_id"]) or self._withdrawn_task(row)
            if task is None:
                raise StudentError(404, "Задание не опубликовано или недоступно")
        stand = self._stand(row, detail=True)
        assignment = dict(task, stand={k:v for k,v in stand.items() if k not in ("access", "pilot_pending")})
        return {"stand": stand, "assignment": assignment}

    def guest_action(self, token, stand_id, action, data):
        row, stamp = self._own_row_and_stamp(token, stand_id)
        if row["real_request"]:
            if not self._labs or not stamp:
                raise StudentError(403, "Войдите в личный кабинет студента")
            if action != "credentials" or data != {}:
                raise StudentError(400, "Срок личного стенда задаётся при развёртывании")
            return self._labs.credentials(row["login"], json.loads(row["task_json"]), row["id"])
        binding = self._owned_binding(row)
        if not binding or not stamp or self._pilot_grant is None:
            raise StudentError(404, "Доступ к реальному стенду не настроен")
        if action == "credentials":
            if data != {}:
                raise StudentError(400, "Для копирования пароля дополнительные параметры не нужны")
            return self._pilot_grant.for_session(binding, stamp)
        if action != "duration" or self._set_duration is None:
            raise StudentError(404, "Операция недоступна")
        minutes = self._duration(data["duration_minutes"])
        self._pilot_grant.authorize(binding, data["access_code"])
        return self._set_duration(binding, minutes)
