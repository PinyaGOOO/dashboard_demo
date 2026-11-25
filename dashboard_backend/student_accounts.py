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
MAX_BATCH = 100


def init_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS student_accounts (
        login TEXT PRIMARY KEY, group_name TEXT NOT NULL, salt BLOB NOT NULL,
        verifier BLOB NOT NULL, format TEXT NOT NULL, created REAL NOT NULL)""")
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
    return db.execute("SELECT group_name,salt,verifier,format FROM student_accounts WHERE login=?",
                      (login,)).fetchone()


def stamp(row):
    if row is None or row["format"] != _FORMAT:
        return None
    return _stamp(row["salt"], row["verifier"])


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
