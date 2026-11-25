"""Teacher-issued logins for the student portal, independent of the guest VM login."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading

from .passwords import generate_password
from .student_errors import StudentError


# Bound CPU/memory consumption without holding the SQLite writer lock during hashing.
_BATCHES = threading.BoundedSemaphore(2)
_AUTH = threading.BoundedSemaphore(4)
_PREFIX = re.compile(r"[a-z][a-z0-9-]{0,15}\Z")
_FORMAT = "scrypt-v1"
_BLOCKED_FORMAT = "blocked:scrypt-v1"  # Older portal versions reject unknown formats too.
_PUBLIC_COLUMNS = "login, group_name, format, version, created, last_login"
MAX_BATCH = 100


def init_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS student_accounts (
        login TEXT PRIMARY KEY, group_name TEXT NOT NULL, salt BLOB NOT NULL,
        verifier BLOB NOT NULL, format TEXT NOT NULL, created REAL NOT NULL)""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(student_accounts)")}
    if "version" not in columns:
        db.execute("ALTER TABLE student_accounts ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    if "last_login" not in columns:
        db.execute("ALTER TABLE student_accounts ADD COLUMN last_login REAL")
    db.execute("""CREATE TABLE IF NOT EXISTS student_account_sequences (
        prefix TEXT PRIMARY KEY, last_number INTEGER NOT NULL CHECK(last_number>0))""")


def _hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**15, r=8, p=1,
                          maxmem=64*1024*1024, dklen=32)


def _stamp(salt, verifier):
    # Server-side epoch for this login, not a sign-in credential. Sessions from a
    # deleted or reissued account never authorize a new account with the same name.
    return "account:" + hashlib.sha256(b"student/session/v1\0" + salt + verifier).hexdigest()


def lookup(db, login):
    return db.execute("SELECT group_name,salt,verifier,format,version FROM student_accounts WHERE login=?",
                      (login,)).fetchone()


def stamp(row):
    if row is None or row["format"] != _FORMAT:
        return None
    base = _stamp(row["salt"], row["verifier"])
    # Preserve existing sessions on first upgrade. Every later management
    # action changes the epoch, even if password and enabled state end up unchanged.
    return base if row["version"] == 1 else f"{base}:{row['version']}"


def authenticate(row, password):
    if not _AUTH.acquire(timeout=5):
        raise StudentError(429, "Кабинет обрабатывает другие входы. Попробуйте ещё раз.")
    try:
        # Take the same expensive path for unknown logins.
        candidate = _hash(password, row["salt"] if row is not None else bytes(16))
        if row is not None and hmac.compare_digest(candidate, row["verifier"]):
            return stamp(row)
        return None
    finally:
        _AUTH.release()


def _prepared(count):
    values, seen = [], set()
    while len(values) < count:
        password = generate_password(16)
        if password not in seen:
            seen.add(password)
            salt = secrets.token_bytes(16)
            values.append((password, salt, _hash(password, salt)))
    return values


def _public_account(row):
    return {"login": row["login"], "group": row["group_name"], "blocked": row["format"] != _FORMAT,
            "version": row["version"], "created": row["created"], "last_login": row["last_login"]}


def list_accounts(open_db, reserved, search="", offset=0, limit=50):
    if (not isinstance(search, str) or len(search) > 128 or (search and not search.isprintable())
            or type(offset) is not int or not 0 <= offset <= 2**31 - 1
            or type(limit) is not int or not 1 <= limit <= 100):
        raise StudentError(400, "Проверьте строку поиска и параметры страницы списка.")
    excluded = ",".join("?" for _ in reserved)
    where = f"WHERE login NOT IN ({excluded})"
    params = list(reserved)
    term = search.strip().casefold()
    with open_db() as db:
        if term:
            db.create_function("casefold", 1, str.casefold)
            where += " AND (instr(casefold(login), ?) > 0 OR instr(casefold(group_name), ?) > 0)"
            params.extend((term, term))
        db.execute("BEGIN")
        total = db.execute("SELECT count(*) FROM student_accounts " + where, params).fetchone()[0]
        rows = db.execute(f"SELECT {_PUBLIC_COLUMNS} FROM student_accounts {where} ORDER BY login LIMIT ? OFFSET ?",
                          (*params, limit, offset))
        return {"accounts": [_public_account(row) for row in rows], "total": total, "offset": offset, "limit": limit}


def manage_account(open_db, reserved, login, action, payload):
    if not isinstance(login, str) or login in reserved:
        raise StudentError(404, "Выданная студенту учётная запись не найдена.")
    if action not in ("reset-password", "block", "unblock", "revoke-sessions"):
        raise StudentError(400, "Неизвестное действие с учётной записью.")
    if not isinstance(payload, dict) or set(payload) != {"version"} or type(payload["version"]) is not int or payload["version"] < 1:
        raise StudentError(400, "Для изменения нужна версия учётной записи из списка.")

    def current(db):
        row = db.execute(f"SELECT {_PUBLIC_COLUMNS} FROM student_accounts WHERE login=?", (login,)).fetchone()
        if row is None:
            raise StudentError(404, "Выданная студенту учётная запись не найдена.")
        if row["version"] != payload["version"]:
            raise StudentError(409, "Учётную запись уже изменили. Обновите список и проверьте её состояние.")
        if row["format"] not in (_FORMAT, _BLOCKED_FORMAT):
            raise StudentError(409, "Формат учётной записи не поддерживается.")
        return row

    password = None
    if action == "reset-password":
        with open_db() as db:
            current(db)
        if not _BATCHES.acquire(blocking=False):
            raise StudentError(429, "Кабинет занят выдачей паролей. Повторите запрос позже.")
        try:
            (password, salt, verifier), = _prepared(1)
        finally:
            _BATCHES.release()

    with open_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = current(db)
        if action in ("block", "unblock"):
            new_format = _BLOCKED_FORMAT if action == "block" else _FORMAT
            if row["format"] == new_format:
                raise StudentError(409, "Учётка уже заблокирована." if action == "block" else "Учётка уже активна.")
            db.execute("UPDATE student_accounts SET format=? WHERE login=?", (new_format, login))
        elif action == "reset-password":
            db.execute("UPDATE student_accounts SET salt=?, verifier=? WHERE login=?", (salt, verifier, login))
        db.execute("UPDATE student_accounts SET version=version+1 WHERE login=?", (login,))
        db.execute("DELETE FROM student_sessions WHERE login=?", (login,))
        updated = db.execute(f"SELECT {_PUBLIC_COLUMNS} FROM student_accounts WHERE login=?", (login,)).fetchone()
        result = {"account": _public_account(updated)}
    if password is not None:
        result["password"] = password
    return result


def create_batch(open_db, payload, reserved, clock):
    if not isinstance(payload, dict) or set(payload) - {"count", "group", "prefix"}:
        raise StudentError(400, "Укажите только группу, количество и при необходимости префикс.")
    count, group, prefix = payload.get("count"), payload.get("group"), payload.get("prefix", "student")
    if type(count) is not int or not 1 <= count <= MAX_BATCH:
        raise StudentError(400, f"Количество учёток должно быть целым числом от 1 до {MAX_BATCH}.")
    if not isinstance(group, str) or not group.isprintable() or not 1 <= len(group.strip()) <= 80:
        raise StudentError(400, "Укажите название группы длиной до 80 символов.")
    if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
        raise StudentError(400, "Префикс: от 1 до 16 строчных латинских букв, цифр или дефисов; первый символ — буква.")
    group = group.strip()
    if not _BATCHES.acquire(blocking=False):
        raise StudentError(429, "Уже создаются учётные записи. Повторите запрос после завершения.")
    try:
        prepared = _prepared(count)
        accounts = []
        with open_db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT last_number FROM student_account_sequences WHERE prefix=?", (prefix,)).fetchone()
            number = row[0] if row else 0
            created = clock()
            for password, salt, verifier in prepared:
                while True:
                    number += 1
                    login = f"{prefix}{number:02d}"
                    if login not in reserved and not db.execute(
                            "SELECT 1 FROM student_accounts WHERE login=?", (login,)).fetchone():
                        break
                db.execute("""INSERT INTO student_accounts(login,group_name,salt,verifier,format,created)
                              VALUES (?,?,?,?,?,?)""", (login,group,salt,verifier,_FORMAT,created))
                accounts.append({"login":login, "name":login, "group":group, "password":password})
            db.execute("""INSERT INTO student_account_sequences(prefix,last_number) VALUES (?,?)
                          ON CONFLICT(prefix) DO UPDATE SET last_number=excluded.last_number""", (prefix,number))
        return {"count":len(accounts), "accounts":accounts}
    finally:
        _BATCHES.release()
