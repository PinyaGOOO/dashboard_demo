"""Names of the resource pools used only by personal classroom deployments."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata


STUDENT_POOL_ROOT = "student-labs"
_PILOT = "pilot"
_GROUP = re.compile(r"[a-z][a-z0-9-]{0,21}")
_CARD = re.compile(r"[0-9a-f]{24}")
_RUSSIAN = str.maketrans(dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    "a b v g d e yo zh z i y k l m n o p r s t u f kh ts ch sh sch _ y _ e yu ya".split(),
)))


def is_student_pool_namespace(pool_id: str) -> bool:
    return pool_id == STUDENT_POOL_ROOT or pool_id.startswith(STUDENT_POOL_ROOT + "/")


def student_pool_parents(pool_id: str) -> tuple[str, str]:
    """Only a generated *leaf* is an addressable personal stand (never an ancestor)."""
    parts = pool_id.split("/") if isinstance(pool_id, str) else []
    if (len(parts) != 3 or parts[0] != STUDENT_POOL_ROOT or not _GROUP.fullmatch(parts[1])
            or not parts[2].startswith("lab-") or not _CARD.fullmatch(parts[2][4:])):
        raise ValueError("Некорректный путь личного учебного пула.")
    return parts[0], "/".join(parts[:2])


def _slug(group: str) -> str:
    text = unicodedata.normalize("NFKD", group.casefold().translate(_RUSSIAN))
    # Non-ASCII letters are separators, not allowed to run adjacent words together.
    text = "".join(c for c in text if not unicodedata.combining(c))
    base = re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "group"
    if not base[0].isalpha():
        base = "g-" + base
    return base[:22].rstrip("-")


def allocate_student_pool(conn: sqlite3.Connection, group: str | None, card_id: str) -> str:
    """Called inside the same BEGIN IMMEDIATE transaction as the personal receipt.

    A human-readable group segment is stable after first use. Distinct exact names
    never share a segment, even on transliteration, truncation or a hash collision.
    The private pilot without an issued group has a reserved, separate namespace.
    """
    if not isinstance(card_id, str) or not _CARD.fullmatch(card_id):
        raise ValueError("Некорректный идентификатор личной учебной карточки.")
    if group is None:
        segment = _PILOT
    else:
        if (not isinstance(group, str) or not 1 <= len(group) <= 80 or not group.isprintable()
                or group != group.strip()):
            raise ValueError("Некорректная учебная группа.")
        found = conn.execute("SELECT segment FROM student_pool_groups WHERE group_name=?", (group,)).fetchone()
        if found is not None:
            segment = found[0]
        else:
            base = segment = _slug(group)
            attempt = 0
            while segment == _PILOT or conn.execute(
                    "SELECT 1 FROM student_pool_groups WHERE segment=?", (segment,)).fetchone():
                digest = hashlib.sha256(f"{group}\0{attempt}".encode()).hexdigest()[:10]
                segment = f"{base[:11].rstrip('-')}-{digest}"
                attempt += 1
            conn.execute("INSERT INTO student_pool_groups(group_name,segment) VALUES (?,?)", (group,segment))
    pool_id = f"{STUDENT_POOL_ROOT}/{segment}/lab-{card_id}"
    student_pool_parents(pool_id)
    return pool_id
