"""Read-only identity/lease check for the single nested-Proxmox teaching pilot."""
from __future__ import annotations

import ipaddress
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path


_PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in
                          ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_MATCH = """
    SELECT 1
      FROM stands AS s
      JOIN blueprints AS b ON b.id=s.blueprint_id
      JOIN stand_vms AS v ON v.stand_id=s.id
      JOIN ipam_reservations AS r
        ON r.stand_id=s.id AND r.stand_vm_id=v.id AND r.address=v.ip
     WHERE s.id=? AND s.pool_id=? AND s.origin='deployed' AND s.vm_count=1
       AND s.status='running' AND b.id=2 AND b.template_vmid=276
       AND v.vmid=? AND v.ip=? AND v.status='running' AND v.credential_valid=1
       AND v.web_username='root@pam' AND r.status='assigned'
       AND NOT EXISTS (SELECT 1 FROM stands AS other WHERE other.pool_id=s.pool_id AND other.id!=s.id)
       AND NOT EXISTS (
           SELECT 1 FROM stand_vms AS other WHERE other.id!=v.id AND
           (other.stand_id=s.id OR other.vmid=v.vmid OR
            trim(substr(other.ip, 1, instr(other.ip || '/', '/') - 1))=v.ip)
       )
"""


def make_pilot_validator(path: Path) -> Callable[[dict], bool]:
    """Check a binding against *current* operator records; never provision, probe, or fetch secrets.

    Deliberately do not use SQLite immutable mode: live WAL commits, including an
    operator changing a lease or removing a VM, must be visible on the next read.
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"

    def valid(binding: dict) -> bool:
        if not isinstance(binding, dict):
            return False
        if binding.get("student_login") != "student01" or binding.get("assignment_id") != "lab-2-276":
            return False
        student_stand = binding.get("stand_id")
        if not isinstance(student_stand, str) or not re.fullmatch(r"[a-f0-9]{24}", student_stand):
            return False
        stand_id, vmid = binding.get("deployer_stand_id"), binding.get("vmid")
        if type(stand_id) is not int or not 0 < stand_id < 2**63:
            return False
        if type(vmid) is not int or not 100 <= vmid <= 999_999_999 or vmid == 276:
            return False
        pool, ip = binding.get("pool_id"), binding.get("guest_ipv4")
        if not isinstance(pool, str) or not pool.strip() or not isinstance(ip, str):
            return False
        try:
            address = ipaddress.IPv4Address(ip)
            if str(address) != ip or not any(address in net for net in _PRIVATE_NETWORKS):
                return False
            # One statement, one consistent snapshot. A missing/outdated database or
            # a busy disk must hide access rather than falling back to the saved IP.
            with closing(sqlite3.connect(uri, uri=True, timeout=0.2)) as db:
                return db.execute(_MATCH, (stand_id, pool, vmid, ip)).fetchone() is not None
        except (ValueError, sqlite3.Error, OSError):
            return False

    return valid
