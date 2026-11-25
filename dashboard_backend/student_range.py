"""Read-only subnet recommendation; optional, bounded ICMP reachability check."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sqlite3
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .student_network import STUDENT_NETWORK


class InventoryError(Exception):
    pass


_MAX_CANDIDATES = 3
_PARALLEL = 8


def _address(value):
    if not isinstance(value, str):
        return None
    try:
        ip = ipaddress.IPv4Interface(value.strip()).ip
    except ValueError:
        # Do not recommend potentially occupied space when a target-network
        # address in the durable inventory is ambiguous or malformed.
        if value.strip().startswith("10.39."):
            raise InventoryError("Некорректный IPv4 в инвентаре учебной сети.") from None
        return None
    return ip if ip in STUDENT_NETWORK else None


def _inventory(source_db):
    uri = Path(source_db).resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            sources = [{ip for (value,) in db.execute(query) if (ip := _address(value)) is not None}
                       for query in ("SELECT address FROM ipam_reservations", "SELECT ip FROM stand_vms")]
    except (OSError, sqlite3.Error):
        raise InventoryError("Не удалось прочитать обе таблицы IPAM и VM.") from None
    if not any(sources):
        raise InventoryError("В IPAM и инвентаре VM нет ни одного адреса из 10.39.0.0/16.")
    return sources


def _ip_table(table):
    try:
        # Local netlink queries only; no discovery traffic or ICMP is sent.
        p = subprocess.run(["ip","-j","-4",table,"show"], capture_output=True, timeout=3, check=True)
        value = json.loads(p.stdout)
        return value if isinstance(value, list) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _local_evidence(neighbours, interfaces):
    peers, local, connected = set(), set(), []
    for peer in neighbours or ():
        if not isinstance(peer, dict):
            continue
        state = peer.get("state") or []
        if isinstance(state, str):
            state = state.split()
        if not isinstance(state, list) or set(state).intersection({"FAILED", "INCOMPLETE", "NONE"}):
            continue
        if peer.get("lladdr") or "PERMANENT" in state:
            ip = _address(peer.get("dst"))
            if ip:
                peers.add(ip)
    for dev in interfaces or ():
        if not isinstance(dev, dict):
            continue
        flags = set(dev.get("flags") or ())
        for addr in dev.get("addr_info") or ():
            if not isinstance(addr, dict) or addr.get("family") != "inet":
                continue
            ip = _address(addr.get("local"))
            if ip is None:
                continue
            local.add(ip)
            prefix = addr.get("prefixlen")
            if ("UP" in flags and not flags.intersection({"NOARP","POINTOPOINT","LOOPBACK"})
                    and type(prefix) is int and 0 < prefix <= 32):
                connected.append(ipaddress.IPv4Network((ip,prefix),strict=False))
    return peers, local, connected


def _has_route(ip):
    try:
        p = subprocess.run(["ip","-j","-4","route","get",str(ip)], capture_output=True, timeout=3, check=True)
        rows = json.loads(p.stdout)
        return bool(isinstance(rows, list) and any(isinstance(r,dict) and r.get("dev")
                    and r.get("type","unicast") == "unicast" for r in rows))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _echo(ip):
    # Values come exclusively from our fixed .20-.219 candidate windows in
    # STUDENT_NETWORK. Do not accept a host, shell expression or port from a caller.
    try:
        p = subprocess.run(["ping","-n","-c","1","-W","1","-w","2",str(ip)], capture_output=True,
                           timeout=4, env={**os.environ,"LC_ALL":"C"})
    except (OSError, subprocess.SubprocessError):
        raise InventoryError("Не удалось выполнить ICMP-проверку (ping). Рекомендация не выдана.") from None
    if p.returncode == 0:
        return True
    # iputils exit 1 can mean a timeout *or* an explicit ICMP/routing error.
    # Reject ambiguous/unsupported output, including packet filtering.
    output = (p.stdout+b" "+p.stderr).lower()
    if (p.returncode == 1 and b"0 received" in output and not p.stderr.strip()
            and not any(word in output for word in
                        (b"unreachable",b"prohibit",b"no route",b"filtered",b"errors",b"exceeded",b"redirect"))):
        return False
    raise InventoryError("Ошибка ping, маршрута или фильтрации ICMP. Рекомендация не выдана.")


def _probe_window(block):
    first = block.network_address+20
    if not all(_has_route(target) for target in (first,block.network_address+119,block.network_address+219)):
        raise InventoryError("Нет подтверждённого IPv4-маршрута до выбранного сектора.")
    sent = 0
    with ThreadPoolExecutor(max_workers=_PARALLEL) as executor:
        for offset in range(0,200,_PARALLEL):
            chunk = [first+i for i in range(offset,min(200,offset+_PARALLEL))]
            replies = list(executor.map(_echo,chunk))
            sent += len(chunk)
            if any(replies):
                return False,sent
    return True,sent


def recommend(source_db, *, neighbours=None, interfaces=None, probe=False):
    reserved, vms = _inventory(source_db)
    peers, local, connected = _local_evidence(neighbours, interfaces)
    known = reserved | vms | peers | local
    base = int(STUDENT_NETWORK.network_address)
    buckets = Counter((int(ip)-base)//256 for ip in known)
    blocked = {(int(ip)-base)//256 for ip in known if 20 <= int(ip)%256 <= 219}
    # Prefer an entirely unused /24, then less activity in nearby /24s. Ties
    # go toward the middle, avoiding common infrastructure at either end.
    choices = [(buckets[n], sum(buckets[i] for i in range(n-4,n+5)), abs(n-128), n)
               for n in range(1,255) if n not in blocked]
    if not choices:
        raise InventoryError("Не найдено окно .20–.219 без известных адресов в допустимых /24.")
    total = 0
    for tried,(count,_,_,number) in enumerate(sorted(choices)[:_MAX_CANDIDATES], start=1):
        block = ipaddress.IPv4Network((base+number*256, 24))
        if not probe:
            break
        clear,sent = _probe_window(block)
        total += sent
        if clear:
            # A class can start deploying during the echo pass. Never present
            # a candidate that has since acquired a recorded reservation.
            current_reservations,current_vms = _inventory(source_db)
            if any(ip in block and 20 <= int(ip)%256 <= 219 for ip in current_reservations | current_vms):
                raise InventoryError("Инвентарь изменился во время проверки. Повторите выбор.")
            break
    else:
        raise InventoryError("В проверенных секторах получены ICMP-ответы; диапазон не предложен.")
    return {
        "network":str(STUDENT_NETWORK), "subnet":str(block), "prefix_length":STUDENT_NETWORK.prefixlen,
        "start":str(block.network_address+20), "end":str(block.network_address+219), "capacity":200,
        "selected_known":count, "window_known":0, "verified_free":False,
        "inventory":{"ipam":len(reserved), "stand_vms":len(vms), "local_neighbours":len(peers),
                     "local_ips":len(local), "known_total":len(known),
                     "neighbours_available":neighbours is not None, "interfaces_available":interfaces is not None,
                     "local_l2_covers_candidate":any(block.subnet_of(net) for net in connected)},
        "coverage":"partial",
        "checked_at":datetime.now(timezone.utc).isoformat(),
        "icmp":{"enabled":bool(probe), "candidates_checked":tried if probe else 0,
                "addresses_probed":total, "selected_addresses_without_echo":200 if probe else None},
        "note":"Рекомендация по инвентарю Deployer и локальной видимости. Это не резервация и не доказательство свободы. Сверьте диапазон с сетевым администратором и DHCP перед выдачей.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Предложить блок для студенческих VM без изменения сети, базы или гостевых машин.")
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--probe",action="store_true",help="Linux: по одному ICMP echo на адрес .20–.219; до трёх секторов, без проверки портов.")
    args = parser.parse_args(argv)
    try:
        result = recommend(args.source_db, neighbours=_ip_table("neigh"), interfaces=_ip_table("addr"),probe=args.probe)
    except InventoryError as exc:
        print(json.dumps({"error":str(exc), "verified_free":False},ensure_ascii=True))
        return 1
    print(json.dumps(result,ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
