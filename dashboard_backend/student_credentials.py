"""Authenticate a personal pilot account and release credentials only for its own live guest."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .student_network import STUDENT_NETWORK
from .student_pilot_validation import _MATCH, make_pilot_validator
from .student_portal import StudentError, _binding


_DOMAIN = b"deployer/student-guest-credentials/v1\0"
_SESSION_DOMAIN = b"deployer/student-personal-session/v1\0"
_NOT_FOUND = "Доступы этого стенда сейчас недоступны."
_BAD_CODE = "Неверный код доступа к этой VM."
# _MATCH contains the entire live enrollment/IPAM predicate. The conditional
# secret read and the eligibility check must see the *same* SQLite snapshot.
_QUERY = """
    WITH eligible AS (""" + _MATCH + """)
    SELECT {projection} FROM stands s JOIN stand_vms v ON v.stand_id=s.id
     WHERE s.id=? AND v.vmid=? AND v.ip=? AND s.owner=?
       AND EXISTS (SELECT 1 FROM eligible)
       AND (s.duration_minutes IS NULL OR
            (s.duration_minutes BETWEEN 1 AND 10080 AND julianday(s.expires_at)>julianday('now')))
"""
# No secret in this projection. A legitimately stopped stand no longer
# satisfies _MATCH (and must never be offered credentials).
_LEASE_QUERY = """
    SELECT s.duration_minutes,
           CASE WHEN s.duration_minutes IS NOT NULL THEN strftime('%Y-%m-%dT%H:%M:%SZ',s.expires_at) END,
           CASE WHEN s.duration_minutes IS NOT NULL AND
                   coalesce(julianday(s.expires_at)>julianday('now'),0)=0 THEN 'expired'
                WHEN s.status='stopped' THEN 'stopped' ELSE 'active' END
      FROM stands s JOIN stand_vms v ON v.stand_id=s.id
      JOIN blueprints b ON b.id=s.blueprint_id
      JOIN ipam_reservations r ON r.stand_id=s.id AND r.stand_vm_id=v.id AND r.address=v.ip
     WHERE s.id=? AND s.pool_id=? AND v.vmid=? AND v.ip=? AND r.prefix_length=?
       AND s.owner=? AND s.origin='deployed' AND s.vm_count=1 AND b.id=2 AND b.template_vmid=276
       AND s.status IN ('running','stopped') AND v.status=s.status
       AND v.web_username='root@pam' AND r.status='assigned'
       AND (s.duration_minutes IS NULL OR s.duration_minutes BETWEEN 1 AND 10080)
       AND NOT EXISTS (SELECT 1 FROM stands other WHERE other.pool_id=s.pool_id AND other.id!=s.id)
       AND NOT EXISTS (SELECT 1 FROM stand_vms other WHERE other.id!=v.id AND
              (other.stand_id=s.id OR other.vmid=v.vmid OR
               trim(substr(other.ip,1,instr(other.ip||'/','/')-1))=v.ip))
"""


class PilotCredentials:
    def __init__(self, admin_db: Path, admin_token: str):
        path = Path(admin_db).resolve()
        self._binding_path = path.with_name("student-pilot.json")
        self._uri = path.as_uri() + "?mode=ro"
        self._valid = make_pilot_validator(path)
        self._key = admin_token.encode("utf-8") if isinstance(admin_token, str) and admin_token.strip() else None

    def _live_binding(self) -> dict | None:
        if self._key is None:
            return None
        try:
            b = _binding(self._binding_path)
            if (not b or b["student_login"] != "student01" or b["assignment_id"] != "lab-2-276" or
                    not isinstance(b["stand_id"], str) or not re.fullmatch(r"[a-f0-9]{24}", b["stand_id"]) or
                    b["vmid"] == 276):
                return None
            ip = ipaddress.IPv4Address(b["guest_ipv4"])
            return b if (ip in STUDENT_NETWORK and ip not in (STUDENT_NETWORK.network_address,
                          STUDENT_NETWORK.broadcast_address)) else None
        except (OSError, ValueError, RecursionError):
            return None

    def _digest(self, binding: dict, domain: bytes) -> bytes:
        encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        return hmac.digest(self._key, domain + encoded, hashlib.sha256)

    def _code(self, binding: dict) -> str:
        # Keep the already-issued personal code valid as the account sign-in password.
        return "LAB-" + base64.b32encode(self._digest(binding, _DOMAIN)[:20]).decode("ascii")

    def _stamp(self, binding: dict) -> str:
        # Server-side scope marker, never the sign-in password, never a browser credential.
        return self._digest(binding, _SESSION_DOMAIN).hex()

    def private_login_required(self, login: str) -> bool:
        # Live-mode selection happens at construction/wiring. A malformed/absent
        # binding must *not* revert this personal account to its published demo password.
        return login == "student01"

    def authenticate_login(self, login: str, password: str) -> str | None:
        if (not self.private_login_required(login) or not isinstance(password, str) or
                len(password) != 36 or not password.isascii()):
            return None
        b = self._live_binding()
        if not b or b["student_login"] != login or not hmac.compare_digest(password, self._code(b)):
            return None
        return self._stamp(b) if self._live_binding() == b else None

    def session_stamp(self, login: str) -> str | None:
        if not self.private_login_required(login):
            return None
        b = self._live_binding()
        return self._stamp(b) if b and b["student_login"] == login else None

    def _row(self, b: dict, *, with_password: bool):
        # Only fixed projections can reach SQL. The common/operator path does
        # not even select the guest password column.
        projection = "v.web_username, v.credential_password" if with_password else "1"
        ident = b["deployer_stand_id"], b["pool_id"], b["vmid"], b["guest_ipv4"], STUDENT_NETWORK.prefixlen
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=0.3)) as db:
                return db.execute(_QUERY.format(projection=projection),
                                  (*ident, b["deployer_stand_id"], b["vmid"], b["guest_ipv4"],
                                   b["student_login"])).fetchone()
        except (sqlite3.Error, OSError):
            return None

    def operator_code(self, stand_id: int, vmid: int) -> str | None:
        """Only call from an existing admin-authenticated endpoint."""
        if type(stand_id) is not int or type(vmid) is not int:
            return None
        b = self._live_binding()
        if not b or (stand_id, vmid) != (b["deployer_stand_id"], b["vmid"]):
            return None
        if not self._valid(b) or not self._row(b, with_password=False) or self._live_binding() != b:
            return None
        return self._code(b)

    def operator_login_password(self, stand_id: int, vmid: int) -> str | None:
        """Admin-only delivery of the account password also for a stopped or expired enrolled lab."""
        if type(stand_id) is not int or type(vmid) is not int:
            return None
        b = self._live_binding()
        if not b or (stand_id, vmid) != (b["deployer_stand_id"], b["vmid"]):
            return None
        if self.lease(b) is None or self._live_binding() != b:
            return None
        return self._code(b)

    def authorize(self, binding: dict, access_code: str) -> None:
        """Verify the second factor without selecting the VM password."""
        b = self._live_binding()
        if (not b or binding != b or not self._valid(b) or not self._row(b, with_password=False)
                or self._live_binding() != b):
            raise StudentError(404, _NOT_FOUND)
        if (not isinstance(access_code, str) or not access_code.isascii() or len(access_code) > 64
                or not hmac.compare_digest(access_code.strip(), self._code(b))):
            raise StudentError(403, _BAD_CODE)

    def for_student(self, binding: dict, access_code: str) -> dict[str, str]:
        """Use a server-resolved binding for the owner of the authenticated card."""
        self.authorize(binding, access_code)
        return self._guest_credentials(binding)

    def for_session(self, binding: dict, stamp: str) -> dict[str, str]:
        """Caller resolves both parameters from the authenticated session and owned assignment."""
        b = self._live_binding()
        if not b or b != binding:
            raise StudentError(404, _NOT_FOUND)
        if not isinstance(stamp, str) or not stamp.isascii() or not hmac.compare_digest(stamp, self._stamp(b)):
            raise StudentError(403, "Войдите в личную учётную запись")
        if not self._valid(b):
            raise StudentError(404, _NOT_FOUND)
        return self._guest_credentials(b)

    def _guest_credentials(self, b: dict) -> dict[str, str]:
        # _row verifies ownership, VM state, IPAM and expiry in the same read as the password.
        row = self._row(b, with_password=True)
        if (not row or row[0] != "root@pam" or not isinstance(row[1], str) or not row[1]
                or self._live_binding() != b):
            raise StudentError(404, _NOT_FOUND)
        return {"username": row[0], "password": row[1]}

    def lease(self, binding: dict) -> dict | None:
        """Expose safe timing metadata only for the still-enrolled VM."""
        b = self._live_binding()
        if not b or binding != b:
            return None
        try:
            with closing(sqlite3.connect(self._uri, uri=True, timeout=0.3)) as db:
                row = db.execute(_LEASE_QUERY, (b["deployer_stand_id"], b["pool_id"], b["vmid"],
                                 b["guest_ipv4"], STUDENT_NETWORK.prefixlen, b["student_login"])).fetchone()
        except (sqlite3.Error, OSError):
            return None
        if not row or self._live_binding() != b:
            return None
        return {"duration_minutes": row[0], "expires_at": row[1], "status": row[2]}
