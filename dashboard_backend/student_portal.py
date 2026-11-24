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
from pathlib import Path


class StudentError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# Fictitious, shared demonstration personas, not institutional user identities.
# Each stores a different salt and a PBKDF2-SHA256 verifier (180,000 rounds).
_ACCOUNTS = {
    "student01": ("Алина Соколова", "ДЭ-24", "602c84f60efaf80fd55c76facbf01528",
                  "7f65fdc72154710317e376969259de0b696787f411bb13e54d67870192404f6d"),
    "student02": ("Максим Орлов", "ДЭ-25", "e1d707a018c3e62a1ca06ce66fa54d1c",
                  "3c5b33aaa5ff044e6004fa20c00ff66eb6096442543f04898eb4c76004cc789f"),
}
_APP_ID = 0x53545544  # STUD: never initialize in someone else's SQLite database.
_PRIVATE_NETS = tuple(map(ipaddress.IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
_BINDING_FIELDS = {"student_login", "assignment_id", "stand_id", "guest_ipv4", "vmid",
                   "deployer_stand_id", "pool_id"}


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
    if str(ip) != value["guest_ipv4"] or not any(ip in net for net in _PRIVATE_NETS):
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
                 pilot_validator: Callable[[dict], bool] | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._catalog = catalog
        self._clock = clock
        self._pilot_validator = pilot_validator
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
            db.execute("""CREATE TABLE IF NOT EXISTS student_sessions (
                token_hash BLOB PRIMARY KEY, login TEXT NOT NULL, expires REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS student_stands (
                id TEXT PRIMARY KEY, login TEXT NOT NULL, assignment_id TEXT NOT NULL,
                task_json TEXT NOT NULL, started REAL NOT NULL, UNIQUE(login, assignment_id))""")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
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

    def _student(self, db: sqlite3.Connection, token: str | None) -> str:
        row = db.execute("SELECT login FROM student_sessions WHERE token_hash=? AND expires>?",
                         (self._token_hash(token), self._clock())).fetchone()
        if row is None:
            raise StudentError(401, "Сессия завершена. Войдите снова")
        return row["login"]

    def login(self, login: str, password: str) -> str:
        if not isinstance(login, str) or not isinstance(password, str) or len(password) > 256:
            raise StudentError(401, "Неверный логин или пароль")
        login = login.strip().lower()
        entry = _ACCOUNTS.get(login)
        salt, verifier = (entry or _ACCOUNTS["student01"])[2:]
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 180_000)
        if not hmac.compare_digest(candidate, bytes.fromhex(verifier)) or entry is None:
            raise StudentError(401, "Неверный логин или пароль")
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._db() as db:
            db.execute("DELETE FROM student_sessions WHERE expires<=?", (now,))
            db.execute("INSERT INTO student_sessions VALUES (?,?,?)",
                       (self._token_hash(token), login, now + self.SESSION_TTL))
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
        for source in self._catalog():
            key = source["id"]
            if not isinstance(key, str) or not key or key in unique:
                continue
            task = {k: source[k] for k in ("id", "title", "description", "template_name", "template_vmid")}
            task["steps"] = list(source["steps"])
            unique[key] = task
        keys = list(unique)
        if len(keys) < 2:
            selected = keys
        elif login == "student01":
            selected = keys[:2] + keys[3::2]
        else:
            selected = keys[1:3] + keys[4::2]
        return {key: unique[key] for key in selected}

    def _stand(self, row: sqlite3.Row) -> dict:
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
        return {"id": row["id"], "name": f"{row['login']}-{row['id'][:8]}",
                "status": "ready" if progress == 100 else "provisioning",
                "progress": progress, "phase": phase}

    def get_dashboard(self, token: str | None) -> dict:
        with self._db() as db:
            login = self._student(db, token)
            tasks = {key: dict(task, stand=None) for key, task in self._assigned(login).items()}
            rows = db.execute("SELECT * FROM student_stands WHERE login=? ORDER BY started, id", (login,))
            for row in rows:
                # Stands retain the exact assignment and template they started
                # with, even when a teacher removes/edits the catalogue entry.
                tasks[row["assignment_id"]] = dict(json.loads(row["task_json"]), stand=self._stand(row))
        name, group = _ACCOUNTS[login][:2]
        return {"student": {"login": login, "name": name, "group": group},
                "assignments": list(tasks.values())}

    def deploy(self, token: str | None, assignment_id: str) -> dict:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            login = self._student(db, token)
            row = db.execute("SELECT * FROM student_stands WHERE login=? AND assignment_id=?",
                             (login, assignment_id)).fetchone()
            if row is None:
                task = self._assigned(login).get(assignment_id)
                if task is None:
                    raise StudentError(404, "Задание не найдено")
                stand_id = secrets.token_hex(12)
                db.execute("INSERT INTO student_stands VALUES (?,?,?,?,?)",
                           (stand_id, login, assignment_id, json.dumps(task, ensure_ascii=False), self._clock()))
                row = db.execute("SELECT * FROM student_stands WHERE id=? AND login=?", (stand_id, login)).fetchone()
            return self._stand(row)

    def get_stand(self, token: str | None, stand_id: str) -> dict:
        with self._db() as db:
            login = self._student(db, token)
            row = db.execute("SELECT * FROM student_stands WHERE id=? AND login=?", (stand_id, login)).fetchone()
            if row is None:
                raise StudentError(404, "Стенд не найден")
        stand = self._stand(row)
        assignment = dict(json.loads(row["task_json"]), stand=dict(stand))
        if self._pilot_validator is not None and stand["status"] == "ready":
            try:
                binding = _binding(self.db_path.with_name("student-pilot.json"))
                if (binding and binding["student_login"] == login
                        and binding["stand_id"] == row["id"] and binding["assignment_id"] == row["assignment_id"]
                        and self._pilot_validator(dict(binding)) is True):
                    stand["access"] = {"kind": "proxmox", "url": f"https://{binding['guest_ipv4']}:8006/",
                                       "vmid": binding["vmid"], "login": "root@pam"}
            except Exception:
                # A missing/malformed binding or unavailable validator must never turn a
                # guessed, stale, or reassigned IP into a clickable sign-in destination.
                pass
        if self._pilot_validator is not None and "access" not in stand:
            intent = read_pilot_request(self.db_path.with_name("student-pilot-request.json"))
            if (intent and intent["student_login"] == login and intent["stand_id"] == row["id"]
                    and intent["assignment_id"] == row["assignment_id"]):
                stand["pilot_pending"] = True
        return {"stand": stand, "assignment": assignment}
