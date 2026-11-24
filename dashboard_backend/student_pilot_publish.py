"""After the opted-in deploy only, attest the nested guest and publish its private portal binding."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import ssl
import tempfile
from contextlib import closing
from http.client import HTTPResponse
from pathlib import Path

from .student_pilot_validation import make_pilot_validator
from .student_portal import read_pilot_request

_TLS_CHECK = ["sh", "-c", "timeout 5 openssl s_client -connect 127.0.0.1:8006 </dev/null 2>/dev/null "
              "| openssl x509 -noout -fingerprint -sha256"]


def _intent(path, stand_id):
    data = read_pilot_request(path)
    return data if (data and data["student_login"] == "student01" and data["assignment_id"] == "lab-2-276"
                    and data["deployer_stand_id"] == stand_id) else None


def _student_exists(directory, request):
    uri = (directory / "student-demo-state.db").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0.2)) as db:
        return (db.execute("PRAGMA application_id").fetchone()[0] == 0x53545544 and
                db.execute("SELECT 1 FROM student_stands WHERE login=? AND assignment_id=? AND id=?",
                           (request["student_login"], request["assignment_id"], request["stand_id"])).fetchone() is not None)


def _guest(gateway, node, vmid, cmd):
    result = gateway._guest_command(node, vmid, cmd, timeout=8)
    if result["exit_code"] != 0 or not isinstance(result["stdout"], str) or len(result["stdout"]) > 262144:
        raise ValueError("guest probe failed")
    return result["stdout"]


def _active_iface(row):
    return (row.get("ifname") not in ("lo", "", None) and "LOOPBACK" not in row.get("flags", [])
            and "UP" in row.get("flags", []) and row.get("operstate") in ("UP", "UNKNOWN"))


def _guest_mac(get, ip):
    rows = json.loads(get(["ip", "-j", "-4", "addr", "show"]))
    matches = [r for r in rows if _active_iface(r) and any(
        a.get("family") == "inet" and a.get("local") == ip and a.get("scope") == "global"
        for a in r.get("addr_info", []))]
    if len(matches) != 1:
        return None
    row = matches[0]
    mac = row.get("address", "")
    if not mac:  # Some iproute2 builds omit link-layer fields with -4.
        links = json.loads(get(["ip", "-j", "link", "show"]))
        matches = [r for r in links if _active_iface(r) and r.get("ifindex") == row.get("ifindex")
                   and r["ifname"] == row["ifname"]]
        if len(matches) != 1:
            return None
        mac = matches[0].get("address", "")
    return mac if re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac) and (
        mac.lower() != "00:00:00:00:00:00" and not (int(mac[:2], 16) & 1)) else None


def _verify_endpoint(get, ip):
    fingerprint = get(_TLS_CHECK).strip()
    match = re.fullmatch(r"sha256\s+fingerprint\s*=\s*((?:[a-f0-9]{2}:){31}[a-f0-9]{2})", fingerprint, re.I)
    if not match:
        return False
    pinned = bytes.fromhex(match[1].replace(":", ""))
    # This is a credential-free certificate pin check, not an unverified HTTP request.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((ip, 8006), timeout=4) as raw, ctx.wrap_socket(raw) as tls:
        if hashlib.sha256(tls.getpeercert(binary_form=True)).digest() != pinned:
            return False
        # Certificates can themselves be cloned. Prove that this exact open TCP
        # connection has reached the enrolled guest, not its template or a sibling.
        client_ip, client_port = tls.getsockname()[:2]
        expected = [(ip, 8006), (client_ip, client_port)]
        # pveproxy commonly listens on a dual-stack IPv6 socket; ss -4 would
        # hide IPv4-mapped connections on those templates.
        rows = get(["ss", "-Hnt", "state", "established", "sport", "=", ":8006"]).splitlines()
        if not any([_peer(value) for value in row.split()[-2:]] == expected for row in rows):
            return False
        # This exact authenticated channel must also serve the nested PVE UI:
        # merely accepting TLS does not imply pveproxy can render its login page.
        tls.settimeout(4)
        tls.sendall((f"GET / HTTP/1.1\r\nHost: {ip}:8006\r\nAccept: text/html\r\n"
                     "Accept-Encoding: identity\r\nConnection: close\r\n\r\n").encode("ascii"))
        limit = 128 * 1024
        with HTTPResponse(tls, method="GET") as response:
            response.begin()
            if response.status != 200 or response.headers.get_content_type() != "text/html":
                return False
            length = response.length
            if length is not None and length > limit:
                return False
            html = response.read(limit + 1).lower()
            return (len(html) <= limit and (length is None or len(html) == length)
                    and all(marker in html for marker in (b"<html", b"proxmox", b"/pve2/")))


def _peer(value):
    try:
        host, port = value.rsplit(":", 1)
        address = ipaddress.ip_address(host.strip("[]"))
        if isinstance(address, ipaddress.IPv6Address):
            address = address.ipv4_mapped
        return str(address), int(port)
    except ValueError:
        return None


def _publish(path, binding):
    if os.path.lexists(path):
        return False
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".student-pilot-", suffix=".tmp", delete=False) as stream:
            tmp = Path(stream.name)
            json.dump(binding, stream)
            stream.flush()
            os.fsync(stream.fileno())
        # An atomic, complete binding that never clobbers a decision made concurrently.
        os.link(tmp, path)
        return True
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def publish_pilot_if_ready(state_db: Path, gateway, completed_stand_id: int) -> bool:
    """Return False on an absent request or unmet proof; never affect deployment success."""
    try:
        if type(completed_stand_id) is not int or not 0 < completed_stand_id < 2**63 or gateway.mode != "live":
            return False
        db_path = Path(state_db).resolve()
        request_path = db_path.with_name("student-pilot-request.json")
        final = db_path.with_name("student-pilot.json")
        if os.path.lexists(final):
            return False
        request = _intent(request_path, completed_stand_id)
        if not request or not _student_exists(db_path.parent, request):
            return False
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            rows = db.execute("SELECT vmid,node FROM stand_vms WHERE stand_id=? AND ip=? AND status='running'",
                              (completed_stand_id, request["guest_ipv4"])).fetchall()
        if len(rows) != 1:
            return False
        vmid, node = rows[0]
        binding = {**request, "vmid": vmid}
        valid = make_pilot_validator(db_path)
        if not node or not valid(binding):
            return False
        members = gateway.pool_members(request["pool_id"])
        if len(members) != 1 or (members[0].get("vmid"), members[0].get("node"), members[0].get("status")) != (vmid, node, "running"):
            return False
        def get(cmd):
            return _guest(gateway, node, vmid, cmd)
        ip = request["guest_ipv4"]
        if not _guest_mac(get, ip) or not _verify_endpoint(get, ip):
            return False
        return (request == _intent(request_path, completed_stand_id) and _student_exists(db_path.parent, request)
                and valid(binding) and _publish(final, binding))
    except Exception:
        # No addresses, credentials, command output or guest exceptions in the student UI.
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Проверить готовность ранее развёрнутой пилотной VM без её изменения")
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--stand-id", required=True, type=int)
    args = parser.parse_args(argv)
    from .proxmox_gateway import create_gateway
    try:
        db_path = args.source_db.resolve()
        if (db_path.is_file() and not os.path.lexists(db_path.with_name("student-pilot.json"))
                and _intent(db_path.with_name("student-pilot-request.json"), args.stand_id)
                and publish_pilot_if_ready(db_path, create_gateway(), args.stand_id)):
            print("Пилотная VM подтверждена. Ссылка опубликована.")
            return 0
    except Exception:
        pass
    print("Новая ссылка не опубликована: уже существует либо готовность назначенной VM не подтверждена.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
