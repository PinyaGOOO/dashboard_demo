"""Isolated, simulated student labs. This module has no Proxmox or dashboard DB access."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
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


class StudentPortal:
    SESSION_TTL = 8 * 3600
    DEPLOY_SECONDS = 8

    def __init__(self, db_path: str | Path, catalog: Callable[[], list[dict]], *, clock=time.time):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._catalog = catalog
        self._clock = clock
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
            return {"stand": stand, "assignment": dict(json.loads(row["task_json"]), stand=stand)}
