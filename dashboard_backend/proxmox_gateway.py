from __future__ import annotations

import base64
import ipaddress
import json
import math
import os
import re
import shlex
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .passwords import generate_password


ProgressCallback = Callable[[int, str], None]


def existing_pool_vm_name(pool_id: str, stand_id: int, index: int) -> str:
    """Build a unique PVE-compatible VM name without exceeding its 63-char limit."""
    suffix = f"-deployer-{int(stand_id)}-{int(index)}"
    prefix = str(pool_id)[:max(1, 63 - len(suffix))]
    return f"{prefix}{suffix}"


class RollbackSnapshotError(RuntimeError):
    """A rollback failed after one or more VM snapshots may have been applied."""

    def __init__(self, message: str, affected_vmids: list[int]):
        super().__init__(message)
        self.affected_vmids = sorted({int(vmid) for vmid in affected_vmids})


class CredentialRestoreError(RuntimeError):
    """Current credentials could not be reapplied to every rolled-back VM."""

    def __init__(self, failures: dict[int, str], restored_vmids: list[int]):
        details = "; ".join(f"VM {vmid}: {message}" for vmid, message in sorted(failures.items()))
        super().__init__("Не удалось восстановить текущие пароли: " + details)
        self.failed_vmids = sorted(failures)
        self.restored_vmids = sorted({int(vmid) for vmid in restored_vmids})


_VKLVIKL_NETWORK_MARKER = "# Сетевой bootstrap из исходного vklvikl.py."

_LINUX_NETWORK_READY_SCRIPT = r"""#!/usr/bin/env bash
set -u

network_ready() {
  command -v ip >/dev/null 2>&1 || return 1
  [[ -d "/sys/class/net/${NETWORK_INTERFACE}" ]] || return 1
  local flags
  flags="$(cat "/sys/class/net/${NETWORK_INTERFACE}/flags" 2>/dev/null)" || return 1
  (( (flags & 1) == 1 )) || return 1
  ip -o -4 addr show dev "${NETWORK_INTERFACE}" scope global 2>/dev/null |
    awk -v wanted="${EXPECTED_CIDR}" '
      { count++ }
      $4 == wanted { found=1 }
      END { exit(found && count == 1 ? 0 : 1) }
    ' || return 1

  # For a Linux bridge, admin-UP plus an address is not enough for external
  # access: at least one enslaved port must also have carrier.
  if [[ -d "/sys/class/net/${NETWORK_INTERFACE}/bridge" ]]; then
    local port port_name port_flags carrier operstate
    for port in "/sys/class/net/${NETWORK_INTERFACE}/brif/"*; do
      [[ -e "${port}" ]] || continue
      port_name="${port##*/}"
      port_flags="$(cat "/sys/class/net/${port_name}/flags" 2>/dev/null || echo 0)"
      carrier="$(cat "/sys/class/net/${port_name}/carrier" 2>/dev/null || echo 0)"
      operstate="$(cat "/sys/class/net/${port_name}/operstate" 2>/dev/null || true)"
      if (( (port_flags & 1) == 1 )) && [[ "${carrier}" == "1" || "${operstate}" == "up" ]]; then
        return 0
      fi
    done
    return 1
  fi
  return 0
}

network_diagnostics() {
  echo "Интерфейс ${NETWORK_INTERFACE} не готов; ожидался ${EXPECTED_CIDR}" >&2
  ip -details link show dev "${NETWORK_INTERFACE}" >&2 2>/dev/null || true
  ip -o -4 addr show dev "${NETWORK_INTERFACE}" >&2 2>/dev/null || true
  ip route show >&2 2>/dev/null || true
  if command -v bridge >/dev/null 2>&1; then
    bridge link show master "${NETWORK_INTERFACE}" >&2 2>/dev/null || true
  fi
}

printf 'BOOT_ID=%s\n' "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
if network_ready; then
  echo "NETWORK_READY ${NETWORK_INTERFACE} ${EXPECTED_CIDR}"
  exit 0
fi

if [[ "${NETWORK_REPAIR:-0}" != "1" ]]; then
  network_diagnostics
  exit 1
fi

for attempt in 1 2; do
  echo "Попытка ${attempt}: применяем сетевую конфигурацию ${NETWORK_INTERFACE}" >&2
  if command -v ifreload >/dev/null 2>&1; then
    syntax_output="$(ifreload -a -s 2>&1)" || {
      echo "Ошибка синтаксиса /etc/network/interfaces: ${syntax_output}" >&2
      network_diagnostics
      exit 2
    }
    ifreload -a >&2 2>&1 || true
  else
    echo "ifreload не найден; применяем конфигурацию безопасной перезагрузкой VM" >&2
    network_diagnostics
    exit 1
  fi
  ip link set dev "${NETWORK_INTERFACE}" up >&2 2>&1 || true

  for _ in $(seq 1 15); do
    if network_ready; then
      echo "NETWORK_READY ${NETWORK_INTERFACE} ${EXPECTED_CIDR}"
      exit 0
    fi
    sleep 1
  done
done

network_diagnostics
exit 1
"""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class IntegrationInfo:
    mode: str
    connected: bool
    host: str
    cluster: str
    message: str = ""


class DemoProxmoxGateway:
    mode = "demo"

    def integration_info(self) -> IntegrationInfo:
        return IntegrationInfo("demo", True, "demo-cluster.local", "DEMO-PVE", "Демонстрационные данные")

    def list_templates(self) -> list[dict[str, Any]]:
        return [
            {"vmid": 9001, "name": "debian-12-network", "node": "pve-01"},
            {"vmid": 9002, "name": "windows-server-2022", "node": "pve-02"},
            {"vmid": 9003, "name": "debian-12-security", "node": "pve-03"},
        ]

    def list_pools(self) -> list[dict[str, Any]]:
        return [
            {"pool_id": "existing-network-lab", "comment": "Существующий учебный пул", "vm_count": 2},
            {"pool_id": "reserve-demo", "comment": "Резервные машины", "vm_count": 1},
        ]

    def pool_members(self, pool_id: str) -> list[dict[str, Any]]:
        pools = {
            "existing-network-lab": [
                {"vmid": 3101, "name": "legacy-router-1", "node": "pve-01", "status": "running", "cpu": 4.2, "ram": 18.0, "ip": ""},
                {"vmid": 3102, "name": "legacy-router-2", "node": "pve-02", "status": "running", "cpu": 3.1, "ram": 16.0, "ip": ""},
            ],
            "reserve-demo": [
                {"vmid": 3201, "name": "reserve-1", "node": "pve-03", "status": "stopped", "cpu": 0.0, "ram": 0.0, "ip": ""},
            ],
        }
        if pool_id not in pools:
            raise RuntimeError(f"Пул Proxmox {pool_id} не найден")
        return pools[pool_id]

    def cluster_metrics(self, tracked_vmids: list[int] | None = None) -> dict[str, Any]:
        phase = time.time() / 24
        nodes_seed = [
            ("pve-01", 48, 62, 71, 17, 22),
            ("pve-02", 67, 74, 64, 26, 31),
            ("pve-03", 31, 46, 58, 5, 9),
            ("pve-04", 54, 59, 69, 18, 24),
        ]
        nodes = []
        for index, (name, cpu, ram, disk, exam_cpu, exam_ram) in enumerate(nodes_seed):
            wobble = math.sin(phase + index * 1.7) * 2.2
            nodes.append({
                "name": name,
                "status": "online",
                "cpu": round(_clamp(cpu + wobble, 0, 100), 1),
                "ram": round(_clamp(ram + wobble * 0.6, 0, 100), 1),
                "disk": disk,
                "exam_cpu": round(_clamp(exam_cpu + wobble * 0.35, 0, 100), 1),
                "exam_ram": round(_clamp(exam_ram + wobble * 0.25, 0, 100), 1),
                "running_vms": [12, 16, 9, 13][index],
                "exam_vms": [3, 6, 1, 4][index],
                "uptime_days": [42, 38, 57, 19][index],
            })
        cpu = round(sum(node["cpu"] for node in nodes) / len(nodes), 1)
        ram = round(sum(node["ram"] for node in nodes) / len(nodes), 1)
        disk = round(sum(node["disk"] for node in nodes) / len(nodes), 1)
        exam_cpu = round(sum(node["exam_cpu"] for node in nodes) / len(nodes), 1)
        exam_ram = round(sum(node["exam_ram"] for node in nodes) / len(nodes), 1)
        history = []
        for offset in range(23, -1, -1):
            label_hour = (datetime.now().hour - offset) % 24
            base = 34 + 8 * math.sin((label_hour - 4) / 3.4) + 11 * math.exp(-((label_hour - 13) ** 2) / 18)
            total_cpu = _clamp(base + 11, 18, 82)
            exam = _clamp(base * 0.48 + (3 if 9 <= label_hour <= 17 else 0), 5, total_cpu - 4)
            history.append({"time": f"{label_hour:02d}:00", "total": round(total_cpu, 1), "exam": round(exam, 1)})
        return {
            "cluster": {
                "cpu": cpu, "ram": ram, "disk": disk, "exam_cpu": exam_cpu, "exam_ram": exam_ram,
                "exam_cpu_share": round(exam_cpu / max(cpu, 0.1) * 100, 1),
                "exam_ram_share": round(exam_ram / max(ram, 0.1) * 100, 1),
                "online_nodes": 4, "total_nodes": 4,
            },
            "nodes": nodes,
            "history": history,
            "attribution_method": "Сумма потребления VM из пулов стендов",
            "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

    def deploy(self, stand: dict[str, Any], blueprint: dict[str, Any], progress: ProgressCallback) -> list[dict[str, Any]]:
        steps = [(14, "Создаём пул"), (31, "Клонируем шаблон"), (54, "Настраиваем сеть"), (72, "Запускаем guest agent"), (89, "Проверяем конфигурацию"), (100, "Стенд готов")]
        for value, message in steps:
            time.sleep(0.08)
            progress(value, message)
        nodes = ["pve-01", "pve-02", "pve-03", "pve-04"]
        vm_count = int(blueprint.get("vm_count") or 1)
        credentials = blueprint.get("credentials") if isinstance(blueprint.get("credentials"), list) else []
        allocated_ips = blueprint.get("allocated_ips") if isinstance(blueprint.get("allocated_ips"), list) else []
        return [
            {
                "index": index,
                # A stand can contain up to 50 VM.  Keep a 100-ID stride so
                # neighbouring demo stands never receive the same VMID.
                "vmid": 2000 + int(stand["id"]) * 100 + index,
                "name": (
                    existing_pool_vm_name(stand["pool_id"], stand["id"], index)
                    if str(stand.get("origin") or "deployed") == "existing"
                    else f"{stand['pool_id']}-{index}"
                ),
                "node": nodes[(int(stand["id"]) + index) % len(nodes)],
                "ip": str(allocated_ips[index - 1]) if index <= len(allocated_ips) else self._ip_for(blueprint.get("subnet", ""), index),
                "status": "running",
                "guest_username": str((credentials[index - 1] if index <= len(credentials) else {}).get("guest_username", "root")),
                "web_username": str((credentials[index - 1] if index <= len(credentials) else {}).get("web_username", "root@pam")),
                "password": str((credentials[index - 1] if index <= len(credentials) else {}).get("password", "")),
                "password_updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "last_snapshot": "start",
            }
            for index in range(1, vm_count + 1)
        ]

    @staticmethod
    def _ip_for(subnet: str, index: int) -> str:
        try:
            interface = ipaddress.ip_interface(str(subnet))
        except ValueError:
            return ""
        if interface.version != 4:
            return ""
        network = interface.network
        candidate = int(interface.ip)
        if network.prefixlen <= 30 and candidate in {
            int(network.network_address), int(network.broadcast_address),
        }:
            candidate = int(network.network_address) + 1
        remaining = max(1, index)
        while candidate <= int(network.broadcast_address):
            boundary = network.prefixlen <= 30 and candidate in {
                int(network.network_address), int(network.broadcast_address),
            }
            if not boundary:
                remaining -= 1
                if remaining == 0:
                    return str(ipaddress.ip_address(candidate))
            candidate += 1
        return ""

    def power_action(self, vmids: list[int], action: str) -> None:
        time.sleep(0.25)

    def rotate_password(self, vmids: list[int], username: str, password: str) -> None:
        time.sleep(0.35)

    def create_snapshot(self, vmids: list[int], name: str, description: str = "") -> None:
        time.sleep(0.25)

    def rollback_snapshot(
        self,
        vmids: list[int],
        name: str = "start",
        *,
        start: bool = True,
        progress: ProgressCallback | None = None,
    ) -> None:
        for value, message in ((20, "Проверяем snapshot"), (65, "Откатываем VM"), (90, "Запускаем VM")):
            time.sleep(0.05)
            if progress:
                progress(value, message)

    def restore_credentials(self, credentials: list[dict[str, Any]]) -> list[int]:
        time.sleep(0.08)
        return [int(item["vmid"]) for item in credentials if item.get("vmid") is not None]

    def web_activity(self, vmids: list[int], window_seconds: int = 180) -> dict[str, Any]:
        return {
            "activity": [], "errors": [], "scanned_vms": len(vmids),
            "window_seconds": window_seconds,
            "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

    def run_autocheck(self, vmids: list[int], script: str) -> list[dict[str, Any]]:
        time.sleep(0.9)
        names = []
        for line in script.splitlines():
            if "check \"" in line:
                try:
                    names.append(line.split('check "', 1)[1].split('"', 1)[0])
                except IndexError:
                    pass
        if not names:
            names = ["Доступность узлов", "Сетевые интерфейсы", "Системные службы", "Конфигурация модуля"]
        result = []
        for index, name in enumerate(dict.fromkeys(names)):
            result.append({"name": name, "ok": index != len(names) - 1 or len(names) < 4, "duration": 180 + index * 137, "message": "Проверка выполнена"})
        return result

    def delete_stand(self, stand: dict[str, Any], vmids: list[int]) -> None:
        time.sleep(0.3)


class LiveProxmoxGateway:
    mode = "live"

    def __init__(self) -> None:
        self.host = os.environ.get("PROXMOX_HOST", "").strip()
        port_value = os.environ.get("PROXMOX_PORT", "8006").strip()
        try:
            self.port = int(port_value)
        except ValueError as exc:
            raise RuntimeError("PROXMOX_PORT должен быть целым числом") from exc
        if not 1 <= self.port <= 65535:
            raise RuntimeError("PROXMOX_PORT должен быть в диапазоне 1..65535")
        self.user = os.environ.get("PROXMOX_USER", "").strip()
        self.token_name = os.environ.get("PROXMOX_TOKEN_NAME", "").strip()
        self.token_value = os.environ.get("PROXMOX_TOKEN_VALUE", "").strip()
        self.verify_ssl = os.environ.get("PROXMOX_VERIFY_SSL", "true").lower() not in {"0", "false", "no"}
        missing = [name for name, value in {
            "PROXMOX_HOST": self.host, "PROXMOX_USER": self.user,
            "PROXMOX_TOKEN_NAME": self.token_name, "PROXMOX_TOKEN_VALUE": self.token_value,
        }.items() if not value]
        if missing:
            raise RuntimeError("Не заданы переменные: " + ", ".join(missing))
        try:
            from proxmoxer import ProxmoxAPI
        except ImportError as exc:  # pragma: no cover - depends on deployment image
            raise RuntimeError("Для live-режима установите proxmoxer") from exc
        self.client = ProxmoxAPI(
            self.host, user=self.user, token_name=self.token_name,
            token_value=self.token_value, verify_ssl=self.verify_ssl,
            port=self.port,
        )
        self._metrics_history: deque[dict[str, Any]] = deque(maxlen=120)
        self._metrics_lock = threading.RLock()
        self._metrics_cache_key: frozenset[int] | None = None
        self._metrics_cache_at = 0.0
        self._metrics_cache_value: dict[str, Any] | None = None
        # PVE's nextid endpoint only reports a free ID; it does not reserve it.
        # Keep allocation and clone submission indivisible between concurrent
        # dashboard deployment threads.
        self._clone_submit_lock = threading.Lock()

    def integration_info(self) -> IntegrationInfo:
        endpoint = self.host if self.port == 443 else f"{self.host}:{self.port}"
        try:
            nodes = self.client.nodes.get()
            return IntegrationInfo("live", True, endpoint, os.environ.get("PROXMOX_CLUSTER_NAME", "Proxmox VE"), f"{len(nodes)} нод")
        except Exception as exc:
            return IntegrationInfo("live", False, endpoint, "Proxmox VE", str(exc))

    def list_templates(self) -> list[dict[str, Any]]:
        templates: list[dict[str, Any]] = []
        for resource in self.client.cluster.resources.get(type="vm"):
            if resource.get("type") != "qemu" or int(resource.get("template") or 0) != 1:
                continue
            templates.append({
                "vmid": int(resource["vmid"]),
                "name": str(resource.get("name") or f"template-{resource['vmid']}"),
                "node": str(resource.get("node") or ""),
            })
        return sorted(templates, key=lambda item: (item["name"].lower(), item["vmid"]))

    def list_pools(self) -> list[dict[str, Any]]:
        pools: list[dict[str, Any]] = []
        for pool in self.client.pools.get():
            pool_id = str(pool.get("poolid") or "").strip()
            if not pool_id:
                continue
            pools.append({
                "pool_id": pool_id,
                "comment": str(pool.get("comment") or ""),
                "vm_count": None,
            })
        return sorted(pools, key=lambda item: item["pool_id"].lower())

    def pool_members(self, pool_id: str) -> list[dict[str, Any]]:
        try:
            pool = self.client.pools(pool_id).get()
        except Exception as exc:
            raise RuntimeError(f"Пул Proxmox {pool_id} не найден") from exc
        members: list[dict[str, Any]] = []
        for member in pool.get("members", []):
            if member.get("type") != "qemu" or member.get("vmid") is None or int(member.get("template") or 0) == 1:
                continue
            maxmem = max(float(member.get("maxmem") or 0), 1)
            members.append({
                "vmid": int(member["vmid"]),
                "name": str(member.get("name") or f"vm-{member['vmid']}"),
                "node": str(member.get("node") or ""),
                "status": str(member.get("status") or "stopped"),
                "cpu": round(float(member.get("cpu") or 0) * 100, 1),
                "ram": round(float(member.get("mem") or 0) / maxmem * 100, 1),
                "ip": "",
            })
        return sorted(members, key=lambda item: item["vmid"])

    def _wait_task(self, node: str, upid: str, timeout: int = 1800) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.client.nodes(node).tasks(upid).status.get()
            if status.get("status") == "stopped":
                if status.get("exitstatus") != "OK":
                    raise RuntimeError(f"Задача Proxmox завершилась: {status.get('exitstatus')}")
                return status
            time.sleep(1)
        raise TimeoutError("Истекло время ожидания задачи Proxmox")

    def _wait_tasks(self, tasks: list[tuple[str, str]], timeout: int = 1800) -> None:
        """Wait for already submitted Proxmox tasks as one parallel batch."""
        pending = {(str(node), str(upid)) for node, upid in tasks if upid}
        if not pending:
            return
        deadline = time.monotonic() + timeout
        failures: list[str] = []
        while pending and time.monotonic() < deadline:
            completed: list[tuple[str, str]] = []
            for node, upid in tuple(pending):
                try:
                    status = self.client.nodes(node).tasks(upid).status.get()
                except Exception:
                    # A transient API error must not turn a successfully running
                    # Proxmox task into a failed dashboard operation.
                    continue
                if status.get("status") != "stopped":
                    continue
                completed.append((node, upid))
                if status.get("exitstatus") != "OK":
                    failures.append(f"{upid}: {status.get('exitstatus')}")
            pending.difference_update(completed)
            if pending:
                time.sleep(0.75)
        if pending:
            raise TimeoutError(f"Истекло время ожидания {len(pending)} задач Proxmox")
        if failures:
            raise RuntimeError("Задачи Proxmox завершились с ошибкой: " + "; ".join(failures[:5]))

    @staticmethod
    def _batch_limit(variable: str, default: int = 6) -> int:
        try:
            configured = int(os.environ.get(variable, str(default)))
        except ValueError:
            configured = default
        return max(1, min(configured, 12))

    def _vm_inventory(
        self,
        vmids: list[int],
        *,
        require_all: bool = True,
    ) -> dict[int, dict[str, Any]]:
        requested = {int(vmid) for vmid in vmids}
        found: dict[int, dict[str, Any]] = {}
        for resource in self.client.cluster.resources.get(type="vm"):
            if resource.get("type") != "qemu" or resource.get("vmid") is None:
                continue
            vmid = int(resource["vmid"])
            if vmid in requested:
                found[vmid] = {
                    "node": str(resource.get("node") or ""),
                    "status": str(resource.get("status") or "unknown"),
                }
        missing = sorted(requested - set(found))
        if missing and require_all:
            raise RuntimeError("VM не найдены: " + ", ".join(str(vmid) for vmid in missing))
        return found

    def _find_template_node(self, vmid: int) -> str:
        for node in self.client.nodes.get():
            if node.get("status") != "online":
                continue
            name = node["node"]
            if any(int(vm.get("vmid", -1)) == vmid for vm in self.client.nodes(name).qemu.get()):
                return name
        raise RuntimeError(f"Шаблон VMID {vmid} не найден")

    def _rank_nodes(self) -> list[str]:
        online = [node for node in self.client.nodes.get() if node.get("status") == "online"]
        online.sort(key=lambda node: (float(node.get("mem", 0)) / max(float(node.get("maxmem", 1)), 1)) * 0.65 + float(node.get("cpu", 0)) * 0.35)
        if not online:
            raise RuntimeError("В кластере нет доступных нод")
        return [node["node"] for node in online]

    @staticmethod
    def _enabled_flag(value: Any, default: bool = True) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in {"", "0", "false", "no", "off"}
        return bool(value)

    @staticmethod
    def _template_volume_refs(config: dict[str, Any]) -> dict[str, set[str]]:
        """Return storage IDs and attached volume IDs required by a template.

        A linked-clone volume such as
        ``NAS1:283/base-283-disk-0.qcow2/300/vm-300-disk-0.qcow2`` is a valid
        compound PVE volume ID.  It can only be used on another node when the
        template's base volume is genuinely visible there.
        """
        refs: dict[str, set[str]] = {}
        disk_key = re.compile(r"^(?:ide|sata|scsi|virtio)\d+$|^(?:efidisk|tpmstate)\d+$")
        for key, raw_value in config.items():
            if not disk_key.fullmatch(str(key)):
                continue
            value = str(raw_value or "").strip()
            volume_id = value.split(",", 1)[0].strip()
            if not volume_id or volume_id in {"none", "cdrom"} or ":" not in volume_id:
                continue
            storage_id = volume_id.split(":", 1)[0].strip()
            if storage_id:
                refs.setdefault(storage_id, set()).add(volume_id)
        return refs

    def _node_has_template_volumes(
        self,
        node: str,
        template_vmid: int,
        required: dict[str, set[str]],
    ) -> bool:
        """Conservatively prove that every template storage is usable on node."""
        try:
            node_storages = {
                str(item.get("storage") or ""): item
                for item in self.client.nodes(node).storage.get()
                if item.get("storage")
            }
            for storage_id, volume_ids in required.items():
                state = node_storages.get(storage_id)
                if not state:
                    return False
                if not self._enabled_flag(state.get("enabled"), True):
                    return False
                if not self._enabled_flag(state.get("active"), True):
                    return False

                # A shared flag alone does not mount or synchronise a storage.
                # Verify that the target node can enumerate the actual backing
                # volumes of the template before putting a linked clone there.
                expected_iso_ids = {
                    volume_id
                    for volume_id in volume_ids
                    if (
                        volume_id.split(":", 1)[-1].lower().startswith("iso/")
                        or volume_id.split(":", 1)[-1].lower().endswith(".iso")
                    )
                }
                expected_vm_ids = set(volume_ids) - expected_iso_ids
                if expected_vm_ids:
                    visible = self.client.nodes(node).storage(storage_id).content.get(
                        vmid=template_vmid,
                    )
                    visible_ids = {
                        str(item.get("volid") or "")
                        for item in visible
                        if item.get("volid")
                    }
                    if not expected_vm_ids.issubset(visible_ids):
                        return False
                if expected_iso_ids:
                    visible_iso = self.client.nodes(node).storage(storage_id).content.get(
                        content="iso",
                    )
                    visible_iso_ids = {
                        str(item.get("volid") or "")
                        for item in visible_iso
                        if item.get("volid")
                    }
                    if not expected_iso_ids.issubset(visible_iso_ids):
                        return False
            return True
        except Exception:
            # Missing audit permission, an old endpoint or a transient storage
            # error must never make cross-node linked clones look safe.
            return False

    def _linked_clone_nodes(
        self,
        template_node: str,
        template_vmid: int,
        ranked_nodes: list[str],
    ) -> tuple[list[str], list[str], list[str]]:
        """Return verified target nodes and storage IDs for a linked clone.

        Local storage, missing storage metadata, or an inconclusive content
        check intentionally falls back to the template node.  True shared
        storage still permits load-aware distribution across the cluster.
        """
        config = self.client.nodes(template_node).qemu(template_vmid).config.get()
        required = self._template_volume_refs(config)
        storage_ids = sorted(required)
        linked_disk_storages = sorted(
            storage_id
            for storage_id, volume_ids in required.items()
            if any("base-" in volume_id for volume_id in volume_ids)
        )
        if not required:
            return [template_node], storage_ids, linked_disk_storages

        try:
            definitions = {
                str(item.get("storage") or ""): item
                for item in self.client.storage.get()
                if item.get("storage")
            }
        except Exception:
            return [template_node], storage_ids, linked_disk_storages

        for storage_id in storage_ids:
            definition = definitions.get(storage_id)
            if not definition or not self._enabled_flag(definition.get("shared"), False):
                return [template_node], storage_ids, linked_disk_storages

        candidates: list[str] = []
        for node in ranked_nodes:
            if node == template_node:
                candidates.append(node)
                continue
            if self._node_has_template_volumes(node, template_vmid, required):
                candidates.append(node)
        if template_node not in candidates:
            candidates.append(template_node)
        return candidates, storage_ids, linked_disk_storages

    def _wait_linked_clone_visible(
        self,
        node: str,
        template_node: str,
        vmid: int,
        storage_ids: list[str],
        timeout: int = 15,
    ) -> None:
        """Wait until a cross-node target can see every new qcow2 overlay."""
        if node == template_node or not storage_ids:
            return
        clone_config = self.client.nodes(node).qemu(vmid).config.get()
        clone_refs = self._template_volume_refs(clone_config)
        expected_counts: dict[str, int] = {}
        for storage_id in storage_ids:
            owned_refs = [
                volume_id
                for volume_id in clone_refs.get(storage_id, set())
                if (
                    f"/{vmid}/" in f"/{volume_id.split(':', 1)[-1]}"
                    or re.search(rf"(?:^|[/_-])vm-{vmid}-", volume_id.split(":", 1)[-1])
                )
            ]
            # A linked template storage always creates at least one overlay;
            # for multi-disk templates wait for every VM-owned volume.
            expected_counts[storage_id] = max(1, len(owned_refs))
        deadline = time.monotonic() + max(1, timeout)
        pending = set(storage_ids)
        while pending and time.monotonic() < deadline:
            for storage_id in tuple(pending):
                try:
                    visible = self.client.nodes(node).storage(storage_id).content.get(vmid=vmid)
                    visible_count = sum(1 for item in visible if item.get("volid"))
                    if visible_count >= expected_counts[storage_id]:
                        pending.remove(storage_id)
                except Exception:
                    pass
            if pending:
                time.sleep(0.5)
        if pending:
            raise RuntimeError(
                f"После linked clone нода {node} не видит overlay VMID {vmid} "
                f"на storage: {', '.join(sorted(pending))}"
            )

    @staticmethod
    def _is_powershell(script: str) -> bool:
        meaningful = [line.strip() for line in script.splitlines() if line.strip()]
        first = meaningful[0].lower() if meaningful else ""
        return (
            first.startswith("# powershell")
            or first.startswith("$psversiontable")
            or "$erroractionpreference" in script.lower()
            or "convertto-json" in script.lower()
        )

    def _wait_guest_agent(self, node: str, vmid: int, timeout: int = 180) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.client.nodes(node).qemu(vmid).agent("ping").post()
                return
            except Exception:
                time.sleep(1)
        raise TimeoutError(f"Guest agent VM {vmid} не ответил за {timeout} секунд")

    def _guest_script(
        self,
        node: str,
        vmid: int,
        script: str,
        purpose: str,
        environment: dict[str, str] | None = None,
        timeout: int = 180,
    ) -> dict[str, Any]:
        if not script.strip():
            raise RuntimeError("Скрипт пуст")
        self._wait_guest_agent(node, vmid, timeout=min(max(1, timeout), 180))
        environment = environment or {}
        if self._is_powershell(script):
            prefix = "\n".join(
                f"$env:{key} = '{str(value).replace(chr(39), chr(39) * 2)}'" for key, value in environment.items()
            )
            content = f"{prefix}\n{script}" if prefix else script
            remote_path = rf"C:\Windows\Temp\demoexam-{purpose}.ps1"
            encoded_path = f"{remote_path}.b64"
            wrapper = (
                "$ErrorActionPreference='Stop';"
                f"$encodedPath='{encoded_path}';"
                f"$scriptPath='{remote_path}';"
                "$bytes=[Convert]::FromBase64String([IO.File]::ReadAllText($encodedPath));"
                "[IO.File]::WriteAllBytes($scriptPath,$bytes);"
                "& $scriptPath"
            )
            command = [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", wrapper,
            ]
        else:
            prefix = "\n".join(f"export {key}={shlex.quote(str(value))}" for key, value in environment.items())
            content = f"{prefix}\n{script}" if prefix else script
            remote_path = f"/tmp/demoexam-{purpose}.sh"
            encoded_path = f"{remote_path}.b64"
            command = [
                "bash", "-c",
                f"base64 -d {shlex.quote(encoded_path)} > {shlex.quote(remote_path)} "
                f"&& chmod 700 {shlex.quote(remote_path)} && bash {shlex.quote(remote_path)}",
            ]
        # PVE's agent/file-write endpoint passes the value through Perl's
        # byte-oriented Base64 encoder. Sending Python Unicode directly makes
        # Perl fail with "Wide character in subroutine entry". Upload an ASCII
        # Base64 envelope and decode it inside the guest before execution.
        encoded_content = base64.b64encode(content.encode("utf-8")).decode("ascii")
        api = self.client.nodes(node).qemu(vmid).agent
        api("file-write").post(file=encoded_path, content=encoded_content)
        task = api("exec").post(command=command)
        try:
            pid = int(task["pid"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Guest agent VM {vmid} не вернул PID") from exc
        started = time.monotonic()
        deadline = started + timeout
        payload: dict[str, Any] = {}
        while time.monotonic() < deadline:
            payload = api("exec-status").get(pid=pid)
            if payload.get("exited"):
                break
            time.sleep(1)
        else:
            raise TimeoutError(f"Скрипт VM {vmid} превысил {timeout} секунд")
        return {
            "exit_code": int(payload.get("exitcode", 1)),
            "stdout": str(payload.get("out-data", "") or ""),
            "stderr": str(payload.get("err-data", "") or ""),
            "duration": round((time.monotonic() - started) * 1000),
        }

    def _guest_command(
        self,
        node: str,
        vmid: int,
        command: list[str],
        timeout: int = 10,
    ) -> dict[str, Any]:
        """Execute one fixed argv command through QGA without invoking a shell."""
        self._wait_guest_agent(node, vmid, timeout=min(max(1, timeout), 10))
        api = self.client.nodes(node).qemu(vmid).agent
        task = api("exec").post(command=command)
        try:
            pid = int(task["pid"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Guest agent VM {vmid} не вернул PID") from exc
        deadline = time.monotonic() + timeout
        payload: dict[str, Any] = {}
        while time.monotonic() < deadline:
            payload = api("exec-status").get(pid=pid)
            if payload.get("exited"):
                break
            time.sleep(0.25)
        else:
            raise TimeoutError(f"Команда Guest Agent VM {vmid} превысила {timeout} секунд")
        return {
            "exit_code": int(payload.get("exitcode", 1)),
            "stdout": str(payload.get("out-data", "") or ""),
            "stderr": str(payload.get("err-data", "") or ""),
        }

    def _linux_network_target(
        self,
        script: str,
        vm_ip: str,
        subnet: str,
    ) -> tuple[str, str] | None:
        """Return guest interface and exact CIDR for vklvikl-style Linux networking."""
        if not script.strip() or not vm_ip.strip() or self._is_powershell(script):
            return None
        looks_like_network_bootstrap = _VKLVIKL_NETWORK_MARKER in script or (
            "VM_IP" in script
            and "/etc/network/interfaces" in script
            and ("ifup" in script or "ifreload" in script or "GUEST_INTERFACE" in script)
        )
        if not looks_like_network_bootstrap:
            return None

        default_match = re.search(
            r"GUEST_INTERFACE\s*=\s*[\"']?\$\{GUEST_INTERFACE:-([A-Za-z0-9_.:-]+)\}",
            script,
        )
        fixed_match = re.search(
            r"GUEST_INTERFACE\s*=\s*[\"']([A-Za-z0-9_.:-]+)[\"']",
            script,
        )
        configured_interface = os.environ.get("PROXMOX_GUEST_INTERFACE", "").strip()
        # A hard assignment in an editable script overrides an exported env
        # value at runtime, so the verifier must follow the same precedence.
        interface = (
            fixed_match.group(1)
            if fixed_match
            else configured_interface
            or (default_match.group(1) if default_match else "vmbr0")
        )
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", interface):
            raise RuntimeError(f"Недопустимое имя гостевого сетевого интерфейса: {interface}")

        try:
            address = ipaddress.ip_address(vm_ip.strip())
        except ValueError as exc:
            raise RuntimeError(f"Некорректный IP VM для проверки сети: {vm_ip}") from exc
        if address.version != 4:
            return None
        try:
            prefix = ipaddress.ip_interface(subnet.strip()).network.prefixlen
        except ValueError:
            # The original vklvikl bootstrap uses /16 when STAND_SUBNET has no
            # prefix. Keep the verifier consistent with that behaviour.
            prefix = 16
        return interface, f"{address}/{prefix}"

    @staticmethod
    def _network_result_detail(result: dict[str, Any] | None) -> str:
        if not result:
            return "проверка не вернула результат"
        detail = str(result.get("stderr") or result.get("stdout") or "").strip()
        return detail[-1200:] if detail else f"exit code {result.get('exit_code', 1)}"

    @staticmethod
    def _network_boot_id(result: dict[str, Any] | None) -> str:
        if not result:
            return ""
        match = re.search(r"^BOOT_ID=([0-9a-fA-F-]+)$", str(result.get("stdout") or ""), re.MULTILINE)
        return match.group(1).lower() if match else ""

    def _run_linux_network_readiness(
        self,
        node: str,
        vmid: int,
        interface: str,
        expected_cidr: str,
        *,
        repair: bool,
        timeout: int,
    ) -> dict[str, Any]:
        return self._guest_script(
            node,
            vmid,
            _LINUX_NETWORK_READY_SCRIPT,
            "network-ready",
            {
                "NETWORK_INTERFACE": interface,
                "EXPECTED_CIDR": expected_cidr,
                "NETWORK_REPAIR": "1" if repair else "0",
            },
            timeout=timeout,
        )

    def _ensure_linux_guest_network(
        self,
        node: str,
        vmid: int,
        interface: str,
        expected_cidr: str,
    ) -> None:
        """Converge guest networking, then require admin-UP and the exact IPv4."""
        initial: dict[str, Any] | None = None
        initial_error = ""
        try:
            initial = self._run_linux_network_readiness(
                node, vmid, interface, expected_cidr, repair=True, timeout=75,
            )
        except Exception as exc:
            initial_error = str(exc)
        if initial and int(initial.get("exit_code", 1)) == 0:
            return
        if initial and int(initial.get("exit_code", 1)) == 2:
            raise RuntimeError(
                f"Сеть VM {vmid} не применена: ошибка конфигурации внутри гостя. "
                f"{self._network_result_detail(initial)}"
            )

        old_boot_id = self._network_boot_id(initial)
        first_detail = initial_error or self._network_result_detail(initial)
        try:
            upid = self.client.nodes(node).qemu(vmid).status.reboot.post()
            if upid:
                self._wait_task(node, str(upid), timeout=180)
        except Exception as exc:
            raise RuntimeError(
                f"Сеть VM {vmid} ({interface}, ожидался {expected_cidr}) не поднялась, "
                f"а автоматическая перезагрузка не запустилась: {exc}. Диагностика: {first_detail}"
            ) from exc

        try:
            ready_timeout = int(os.environ.get("PROXMOX_NETWORK_READY_TIMEOUT", "180"))
        except ValueError:
            ready_timeout = 180
        ready_timeout = max(30, min(ready_timeout, 600))
        deadline = time.monotonic() + ready_timeout
        last_detail = first_detail
        while time.monotonic() < deadline:
            remaining = max(1, round(deadline - time.monotonic()))
            try:
                result = self._run_linux_network_readiness(
                    node,
                    vmid,
                    interface,
                    expected_cidr,
                    repair=False,
                    timeout=min(20, remaining),
                )
                new_boot_id = self._network_boot_id(result)
                if old_boot_id and new_boot_id == old_boot_id:
                    last_detail = "QEMU Guest Agent ещё отвечает из предыдущей загрузки"
                elif int(result.get("exit_code", 1)) == 0:
                    return
                else:
                    last_detail = self._network_result_detail(result)
            except Exception as exc:
                last_detail = str(exc)
            if time.monotonic() < deadline:
                time.sleep(2)
        raise RuntimeError(
            f"Сеть VM {vmid} не готова после восстановления и автоматической перезагрузки: "
            f"интерфейс {interface}, ожидался {expected_cidr}. {last_detail}"
        )

    def _cleanup_failed_deploy(
        self,
        created: list[tuple[str, int]],
        pool_id: str,
        expected_marker: str,
        *,
        preserve_pool: bool = False,
        expected_names: dict[int, str] | None = None,
    ) -> tuple[list[tuple[str, int]], list[str]]:
        attempt_errors: list[str] = []
        candidate_ids = {int(vmid) for _, vmid in created}
        expected_names = expected_names or {}

        # Never stop or delete a VM merely because its numeric ID was returned
        # by nextid.  An external creator can win the same ID.  Only resources
        # that PVE confirms as members of this exact dashboard-owned pool are
        # eligible for rollback.
        pool: dict[str, Any] | None = None
        for attempt in range(11 if candidate_ids else 1):
            try:
                pool = self.client.pools(pool_id).get()
            except Exception as exc:
                return [], [f"не удалось проверить пул {pool_id}; он сохранён: {exc}"]
            if not preserve_pool and str(pool.get("comment") or "").strip() != expected_marker:
                return [], [f"метка владельца пула {pool_id} изменилась; автоочистка отменена"]
            member_ids = {
                int(member["vmid"])
                for member in pool.get("members", [])
                if member.get("type") == "qemu" and member.get("vmid") is not None
            }
            if candidate_ids.issubset(member_ids) or attempt == 10:
                break
            # Covers an accepted clone whose HTTP response was lost before the
            # new config became visible in the cluster pool.
            time.sleep(0.5)

        assert pool is not None
        pool_members = {
            int(member["vmid"]): str(member.get("node") or "")
            for member in pool.get("members", [])
            if member.get("type") == "qemu" and member.get("vmid") is not None
        }
        pool_member_names = {
            int(member["vmid"]): str(member.get("name") or "")
            for member in pool.get("members", [])
            if member.get("type") == "qemu" and member.get("vmid") is not None
        }
        unknown_pool_ids = sorted(set(pool_members) - candidate_ids)
        if unknown_pool_ids and not preserve_pool:
            ids = ", ".join(str(vmid) for vmid in unknown_pool_ids)
            return [
                (pool_members[vmid], vmid) for vmid in unknown_pool_ids
            ], [f"в пуле {pool_id} есть VMID {ids}, не создававшиеся этим запуском; автоочистка отменена"]

        mismatched_candidate_ids = sorted(
            vmid for vmid in candidate_ids & set(pool_members)
            if preserve_pool and pool_member_names.get(vmid) != expected_names.get(vmid)
        )
        owned = [
            (pool_members[vmid], vmid)
            for vmid in sorted(candidate_ids & set(pool_members))
            if not preserve_pool or pool_member_names.get(vmid) == expected_names.get(vmid)
        ]
        unconfirmed_ids = sorted(candidate_ids - set(pool_members))
        stop_tasks: list[tuple[str, str]] = []
        for node, vmid in reversed(owned):
            api = self.client.nodes(node).qemu(vmid)
            try:
                current = api.status.current.get()
                if current.get("status") == "running":
                    upid = api.status.stop.post()
                    if upid:
                        stop_tasks.append((node, str(upid)))
            except Exception:
                pass
        try:
            self._wait_tasks(stop_tasks, timeout=180)
        except Exception as exc:
            attempt_errors.append(f"остановка VM: {exc}")
        delete_tasks: list[tuple[str, str]] = []
        for node, vmid in reversed(owned):
            api = self.client.nodes(node).qemu(vmid)
            try:
                api.config.put(**{"delete": "lock"})
            except Exception:
                pass
            try:
                # destroy-unreferenced-disks is not available in older PVE
                # schemas. Destroying the VM already removes referenced disks.
                upid = api.delete(purge=1)
                if upid:
                    delete_tasks.append((node, str(upid)))
            except Exception as exc:
                attempt_errors.append(f"удаление VM {vmid}: {exc}")
        try:
            self._wait_tasks(delete_tasks, timeout=600)
        except Exception as exc:
            attempt_errors.append(f"ожидание удаления VM: {exc}")

        owned_ids = {int(vmid) for _, vmid in owned}
        try:
            remaining = [
                (str(item.get("node") or ""), int(item["vmid"]))
                for item in self.client.cluster.resources.get(type="vm")
                if item.get("vmid") is not None and int(item["vmid"]) in owned_ids
            ]
        except Exception as exc:
            # Without a successful inventory refresh, deleting the ownership
            # pool could turn surviving VMs into untracked orphans.
            remaining = list(dict.fromkeys((str(node), int(vmid)) for node, vmid in owned))
            attempt_errors.append(f"проверка отката: {exc}")

        if remaining:
            ids = ", ".join(str(vmid) for _, vmid in remaining)
            errors = [f"не удалены VMID {ids}; пул {pool_id} сохранён для повторной очистки"]
            errors.extend(attempt_errors[-3:])
            return remaining, errors

        if mismatched_candidate_ids:
            ids = ", ".join(str(vmid) for vmid in mismatched_candidate_ids)
            return [], [
                f"VMID {ids} в существующем пуле {pool_id} не принадлежат этому развёртыванию; "
                "автоочистка этих VM отменена"
            ]

        if unconfirmed_ids and not preserve_pool:
            ids = ", ".join(str(vmid) for vmid in unconfirmed_ids)
            return [], [
                f"VMID {ids} не подтверждены как члены пула {pool_id}; "
                "пул сохранён для безопасной повторной проверки"
            ]

        if preserve_pool:
            return [], []

        try:
            latest_pool = self.client.pools(pool_id).get()
            if str(latest_pool.get("comment") or "").strip() != expected_marker:
                return [], [f"метка владельца пула {pool_id} изменилась перед удалением"]
            late_members = [
                member
                for member in latest_pool.get("members", [])
                if member.get("type") in {"qemu", "lxc"} and member.get("vmid") is not None
            ]
            if late_members:
                late_resources = [
                    (str(member.get("node") or ""), int(member["vmid"]))
                    for member in late_members
                ]
                ids = ", ".join(str(vmid) for _, vmid in late_resources)
                return late_resources, [
                    f"перед удалением в пуле {pool_id} появились VMID {ids}; пул сохранён"
                ]
            self.client.pools(pool_id).delete()
        except Exception as exc:
            return [], [f"не удалён пул {pool_id}: {exc}"]
        return [], []

    @staticmethod
    def _network_with_bridge(network: str, bridge: str) -> str:
        parts = [part.strip() for part in str(network).split(",") if part.strip()]
        if not parts:
            parts = ["virtio"]
        inherited_bridge = next(
            (part.split("=", 1)[1] for part in parts if part.startswith("bridge=")),
            "",
        )
        # A template can be saved with Proxmox's "Disconnect" checkbox.  Do
        # not propagate link_down=1 (or an unnecessary link_down=0) to clones.
        parts = [
            part for part in parts
            if not part.startswith("bridge=") and not part.startswith("link_down=")
        ]
        selected_bridge = str(bridge).strip() or inherited_bridge
        if selected_bridge:
            parts.append(f"bridge={selected_bridge}")
        return ",".join(parts)

    def deploy(self, stand: dict[str, Any], blueprint: dict[str, Any], progress: ProgressCallback) -> list[dict[str, Any]]:
        pool_id = stand["pool_id"]
        pools = {pool["poolid"] for pool in self.client.pools.get()}
        use_existing_pool = str(stand.get("origin") or "deployed") == "existing"
        if use_existing_pool and pool_id not in pools:
            raise RuntimeError(f"Существующий пул Proxmox {pool_id} не найден")
        if not use_existing_pool and pool_id in pools:
            raise RuntimeError(f"Пул Proxmox {pool_id} уже существует")
        expected_marker = f"DEMOEXAM dashboard stand_id={stand['id']}"
        if not use_existing_pool:
            self.client.pools.post(poolid=pool_id, comment=expected_marker)
        created: list[tuple[str, int]] = []
        expected_names: dict[int, str] = {}
        try:
            progress(12, "Выбран существующий пул" if use_existing_pool else "Пул создан")
            template_vmid = int(blueprint["template_vmid"])
            if template_vmid <= 0:
                raise RuntimeError("Не указан VMID шаблона")
            template_node = self._find_template_node(template_vmid)
            ranked_nodes = self._rank_nodes()
            target_nodes, template_storages, linked_disk_storages = self._linked_clone_nodes(
                template_node, template_vmid, ranked_nodes,
            )
            requested_node = str(stand.get("node", "")).strip()
            if requested_node and requested_node != "auto":
                if requested_node not in ranked_nodes:
                    raise RuntimeError(f"Нода {requested_node} недоступна")
                if requested_node not in target_nodes:
                    storage_label = ", ".join(template_storages) or "хранилище шаблона"
                    raise RuntimeError(
                        f"Linked clone шаблона VMID {template_vmid} нельзя разместить "
                        f"на ноде {requested_node}: {storage_label} не подтверждено "
                        f"как общее и доступное. Выберите «Автоматически» "
                        f"или ноду шаблона {template_node}."
                    )
                target_nodes = [requested_node]
            elif target_nodes == [template_node] and len(ranked_nodes) > 1:
                storage_label = ", ".join(template_storages) or "хранилище шаблона"
                progress(
                    12,
                    f"Linked clone: {storage_label} не подтверждено на всех нодах; "
                    f"размещение на {template_node}",
                )
            vm_count = int(blueprint.get("vm_count") or 1)
            credentials = blueprint.get("credentials") if isinstance(blueprint.get("credentials"), list) else []
            allocated_ips = blueprint.get("allocated_ips") if isinstance(blueprint.get("allocated_ips"), list) else []
            plans: list[dict[str, Any]] = []
            clone_tasks: list[tuple[str, str]] = []
            clone_batch = self._batch_limit("PROXMOX_CLONE_BATCH")

            # Submit linked clones in bounded parallel batches.  Allocation and
            # submission are locked together across deployment threads because
            # nextid reports availability but does not reserve the number.
            for index in range(1, vm_count + 1):
                target_node = target_nodes[(index - 1) % len(target_nodes)]
                name = (
                    existing_pool_vm_name(pool_id, stand["id"], index)
                    if use_existing_pool else f"{pool_id}-{index}"
                )
                vm_ip = (
                    str(allocated_ips[index - 1])
                    if index <= len(allocated_ips)
                    else DemoProxmoxGateway._ip_for(str(blueprint.get("subnet", "")), index)
                )
                credential = dict(credentials[index - 1]) if index <= len(credentials) and isinstance(credentials[index - 1], dict) else {}
                if not credential.get("password"):
                    credential["password"] = generate_password(18)
                credential.setdefault("guest_username", "root")
                credential.setdefault("web_username", "root@pam")
                with self._clone_submit_lock:
                    new_vmid = int(self.client.cluster.nextid.get())
                    # Kept as a candidate before POST so an accepted request
                    # with a lost HTTP response can be recovered from the pool.
                    # Rollback still verifies pool ownership before deletion.
                    created.append((target_node, new_vmid))
                    expected_names[new_vmid] = name
                    params: dict[str, Any] = {
                        "newid": new_vmid, "name": name, "full": 0,
                        "target": target_node, "pool": pool_id,
                    }
                    upid = self.client.nodes(template_node).qemu(template_vmid).clone.post(**params)
                plans.append({
                    "index": index, "node": target_node, "vmid": new_vmid,
                    "name": name, "ip": vm_ip, "credential": credential,
                })
                if upid:
                    clone_tasks.append((template_node, str(upid)))
                progress(14 + round(index / vm_count * 10), f"Клонирование VM {index} из {vm_count} запущено")
                if len(clone_tasks) >= clone_batch:
                    self._wait_tasks(clone_tasks)
                    clone_tasks.clear()

            self._wait_tasks(clone_tasks)
            progress(38, f"Клонировано VM: {vm_count}")

            start_tasks: list[tuple[str, str]] = []
            try:
                for plan in plans:
                    target_node = str(plan["node"])
                    new_vmid = int(plan["vmid"])
                    self._wait_linked_clone_visible(
                        target_node,
                        template_node,
                        new_vmid,
                        linked_disk_storages,
                    )
                    config: dict[str, Any] = {"agent": "1"}
                    vm_api = self.client.nodes(target_node).qemu(new_vmid)
                    current_config = vm_api.config.get()
                    current_network = str(current_config.get("net0", ""))
                    requested_bridge = str(blueprint.get("bridge") or "")
                    if current_network or requested_bridge:
                        config["net0"] = self._network_with_bridge(
                            current_network, requested_bridge,
                        )
                    vm_api.config.put(**config)
                    upid = vm_api.status.start.post()
                    if upid:
                        start_tasks.append((target_node, str(upid)))
                self._wait_tasks(start_tasks, timeout=600)
            except Exception as exc:
                detail = str(exc)
                storage_problem = (
                    ("volume '" in detail and "does not exist" in detail)
                    or "не видит overlay" in detail
                )
                if storage_problem:
                    nodes_label = ", ".join(dict.fromkeys(str(plan["node"]) for plan in plans))
                    storage_label = ", ".join(template_storages) or "хранилище шаблона"
                    raise RuntimeError(
                        f"Linked clone не запустился: одна из нод ({nodes_label}) не видит "
                        f"базовый или overlay-диск VMID {template_vmid} на {storage_label}. "
                        f"Проверьте mount/storage на целевой ноде. Proxmox: {detail}"
                    ) from exc
                raise
            progress(55, f"Запущено VM: {vm_count}")

            completed = 0
            progress_lock = threading.Lock()

            def prepare_guest(plan: dict[str, Any]) -> dict[str, Any]:
                target_node = str(plan["node"])
                new_vmid = int(plan["vmid"])
                index = int(plan["index"])
                vm_ip = str(plan["ip"])
                deploy_script = str(blueprint.get("deploy_script", ""))
                network_target = self._linux_network_target(
                    deploy_script,
                    vm_ip,
                    str(blueprint.get("subnet", "")),
                )
                deferred_network_error = ""
                if deploy_script.strip():
                    environment = {
                        "STAND_NAME": str(stand["name"]),
                        "STAND_POOL": str(pool_id),
                        "VM_INDEX": str(index),
                        "VMID": str(new_vmid),
                        "VM_IP": vm_ip,
                        "STAND_SUBNET": str(blueprint.get("subnet", "")),
                        "VM_BRIDGE": str(blueprint.get("bridge", "")),
                    }
                    if network_target:
                        environment["GUEST_INTERFACE"] = network_target[0]
                    result = self._guest_script(
                        target_node,
                        new_vmid,
                        deploy_script,
                        "deploy",
                        environment,
                        timeout=900,
                    )
                    if result["exit_code"] != 0:
                        detail = result["stderr"] or result["stdout"] or f"exit code {result['exit_code']}"
                        # Exit 75 is the default bootstrap's explicit signal
                        # that its live network apply needs backend recovery.
                        if network_target and int(result["exit_code"]) == 75:
                            deferred_network_error = str(detail)[-500:]
                        else:
                            raise RuntimeError(f"Скрипт развёртывания VM {new_vmid}: {str(detail)[-500:]}")

                if network_target:
                    try:
                        self._ensure_linux_guest_network(
                            target_node,
                            new_vmid,
                            network_target[0],
                            network_target[1],
                        )
                    except Exception as exc:
                        suffix = f" Bootstrap: {deferred_network_error}" if deferred_network_error else ""
                        raise RuntimeError(f"{exc}{suffix}") from exc

                # Even when a blueprint has no bootstrap script, wait for QGA
                # before applying the generated login password.
                self._wait_guest_agent(target_node, new_vmid)
                credential = dict(plan["credential"])
                guest_username = str(credential.get("guest_username") or "root")
                password = str(credential["password"])
                self.client.nodes(target_node).qemu(new_vmid).agent("set-user-password").post(
                    username=guest_username,
                    password=password,
                )
                changed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                return {
                    "index": index, "vmid": new_vmid, "name": str(plan["name"]), "node": target_node,
                    "ip": vm_ip, "status": "running", "guest_username": guest_username,
                    "web_username": str(credential.get("web_username") or "root@pam"),
                    "password": password, "password_updated_at": changed_at,
                }

            deployed: list[dict[str, Any]] = []
            try:
                configured_workers = int(os.environ.get("PROXMOX_DEPLOY_WORKERS", "6"))
            except ValueError:
                configured_workers = 6
            worker_limit = max(1, min(configured_workers, vm_count, 12))
            with ThreadPoolExecutor(max_workers=worker_limit, thread_name_prefix="pve-guest") as executor:
                future_plans = {executor.submit(prepare_guest, plan): plan for plan in plans}
                for future in as_completed(future_plans):
                    deployed.append(future.result())
                    with progress_lock:
                        completed += 1
                        progress(
                            55 + round(completed / vm_count * 35),
                            f"VM {completed} из {vm_count}: настройка и сеть готовы",
                        )

            deployed.sort(key=lambda item: int(item["vmid"]))
            snapshot_tasks: list[tuple[str, str]] = []
            snapshot_batch = self._batch_limit("PROXMOX_SNAPSHOT_BATCH")
            for item in deployed:
                node = str(item["node"])
                upid = self.client.nodes(node).qemu(int(item["vmid"])).snapshot.post(
                    snapname="start",
                    description="Начальное состояние после развёртывания",
                )
                if upid:
                    snapshot_tasks.append((node, str(upid)))
                if len(snapshot_tasks) >= snapshot_batch:
                    self._wait_tasks(snapshot_tasks, timeout=1800)
                    snapshot_tasks.clear()
            self._wait_tasks(snapshot_tasks, timeout=1800)
            for item in deployed:
                item["last_snapshot"] = "start"
            progress(97, "Начальные снимки start созданы")
            progress(100, "Стенд готов")
            return deployed
        except Exception as exc:
            remaining, cleanup_errors = self._cleanup_failed_deploy(
                created, pool_id, expected_marker,
                preserve_pool=use_existing_pool,
                expected_names=expected_names,
            )
            if remaining or cleanup_errors:
                raise RuntimeError(
                    f"{exc}; автоматический откат не завершён: "
                    + "; ".join(cleanup_errors)
                ) from exc
            raise

    def _locate_vm(self, vmid: int) -> str:
        return str(self._vm_inventory([vmid])[int(vmid)]["node"])

    def power_action(self, vmids: list[int], action: str) -> None:
        # The dashboard's Stop button is an operator action for an entire lab.
        # Use Proxmox hard-stop (as the legacy pool tool did) so one guest with
        # a broken ACPI/QGA shutdown cannot hold a 25-VM request for minutes.
        endpoint = {"start": "start", "stop": "stop", "restart": "reboot"}.get(action)
        if endpoint is None:
            raise ValueError("Неизвестное действие питания")
        inventory = self._vm_inventory(vmids)
        tasks: list[tuple[str, str]] = []
        for vmid in vmids:
            item = inventory[int(vmid)]
            node = str(item["node"])
            current = str(item["status"])
            if action == "stop" and current == "stopped":
                continue
            if action == "start" and current == "running":
                continue
            if action == "restart" and current != "running":
                raise RuntimeError(f"VM {vmid} остановлена; сначала запустите её")
            status = self.client.nodes(node).qemu(vmid).status
            upid = getattr(status, endpoint).post()
            if upid:
                tasks.append((node, str(upid)))
        self._wait_tasks(tasks, timeout=600)

    def rotate_password(self, vmids: list[int], username: str, password: str) -> None:
        inventory = self._vm_inventory(vmids)
        for vmid in vmids:
            node = str(inventory[int(vmid)]["node"])
            self.client.nodes(node).qemu(vmid).agent("set-user-password").post(username=username, password=password)

    def create_snapshot(self, vmids: list[int], name: str, description: str = "") -> None:
        inventory = self._vm_inventory(vmids)
        tasks: list[tuple[str, str]] = []
        batch_size = self._batch_limit("PROXMOX_SNAPSHOT_BATCH")
        for vmid in vmids:
            node = str(inventory[int(vmid)]["node"])
            upid = self.client.nodes(node).qemu(vmid).snapshot.post(
                snapname=name,
                description=description,
            )
            if upid:
                tasks.append((node, str(upid)))
            if len(tasks) >= batch_size:
                self._wait_tasks(tasks, timeout=1800)
                tasks.clear()
        self._wait_tasks(tasks, timeout=1800)

    def rollback_snapshot(
        self,
        vmids: list[int],
        name: str = "start",
        *,
        start: bool = True,
        progress: ProgressCallback | None = None,
    ) -> None:
        """Rollback every VM to one exact snapshot, then optionally start it."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(name)):
            raise ValueError("Некорректное имя snapshot для отката")
        inventory = self._vm_inventory(vmids)
        missing: list[int] = []
        unavailable: list[str] = []

        # Complete preflight for the whole stand before changing the first VM.
        for vmid in vmids:
            node = str(inventory[int(vmid)]["node"])
            vm_api = self.client.nodes(node).qemu(int(vmid))
            config = vm_api.config.get()
            if int(config.get("template") or 0) == 1:
                unavailable.append(f"VMID {vmid}: это шаблон")
                continue
            lock = str(config.get("lock") or "").strip()
            if lock:
                unavailable.append(f"VMID {vmid}: активна блокировка {lock}")
                continue
            snapshots = vm_api.snapshot.get()
            selected = next(
                (
                    item for item in snapshots
                    if str(item.get("name") or item.get("snapname") or "") == name
                ),
                None,
            )
            if not selected:
                missing.append(int(vmid))
            elif str(selected.get("snapstate") or "").strip():
                unavailable.append(f"VMID {vmid}: snapshot {name} ещё не завершён")
        if missing or unavailable:
            details = []
            if missing:
                details.append(f"нет snapshot {name} на VMID {', '.join(map(str, missing))}")
            details.extend(unavailable)
            raise RuntimeError("Откат не запущен: " + "; ".join(details))
        if progress:
            progress(20, f"Snapshot {name} проверен на всех VM")

        batch_size = self._batch_limit("PROXMOX_ROLLBACK_BATCH", default=4)
        total = max(len(vmids), 1)
        completed = 0
        changed_vmids: list[int] = []
        for offset in range(0, len(vmids), batch_size):
            batch = [int(vmid) for vmid in vmids[offset:offset + batch_size]]
            tasks: list[tuple[str, str]] = []
            submitted: list[int] = []
            try:
                for vmid in batch:
                    node = str(inventory[vmid]["node"])
                    upid = (
                        self.client.nodes(node)
                        .qemu(vmid)
                        .snapshot(name)
                        .rollback.post(start=0)
                    )
                    if upid:
                        tasks.append((node, str(upid)))
                    submitted.append(vmid)
                self._wait_tasks(tasks, timeout=1800)
            except Exception as exc:
                if tasks:
                    try:
                        self._wait_tasks(tasks, timeout=1800)
                    except Exception:
                        pass
                raise RollbackSnapshotError(
                    f"Откат snapshot {name} завершился ошибкой на группе VMID "
                    f"{', '.join(map(str, submitted or batch))}; часть стенда могла уже измениться: {exc}",
                    changed_vmids + (submitted or batch),
                ) from exc
            changed_vmids.extend(batch)
            completed += len(batch)
            if progress:
                progress(20 + round(completed / total * 50), f"Откат выполнен: {completed} из {len(vmids)} VM")

        if not start:
            return

        start_tasks: list[tuple[str, str]] = []
        try:
            for vmid in vmids:
                node = str(inventory[int(vmid)]["node"])
                vm_api = self.client.nodes(node).qemu(int(vmid))
                current = vm_api.status.current.get()
                if str(current.get("status") or "") == "running":
                    continue
                upid = vm_api.status.start.post()
                if upid:
                    start_tasks.append((node, str(upid)))
                if len(start_tasks) >= batch_size:
                    self._wait_tasks(start_tasks, timeout=600)
                    start_tasks.clear()
            self._wait_tasks(start_tasks, timeout=600)
        except Exception as exc:
            if start_tasks:
                try:
                    self._wait_tasks(start_tasks, timeout=600)
                except Exception:
                    pass
            raise RollbackSnapshotError(
                f"VM откатились к snapshot {name}, но запуск завершился ошибкой: {exc}",
                [int(vmid) for vmid in vmids],
            ) from exc
        if progress:
            progress(80, "VM запущены после отката")

        workers = max(1, min(self._batch_limit("PROXMOX_DEPLOY_WORKERS"), len(vmids)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pve-rollback-ready") as executor:
            futures = {
                executor.submit(
                    self._wait_guest_agent,
                    str(inventory[int(vmid)]["node"]),
                    int(vmid),
                    240,
                ): int(vmid)
                for vmid in vmids
            }
            for future in as_completed(futures):
                vmid = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    raise RollbackSnapshotError(
                        f"VM {vmid} запущена, но QEMU Guest Agent не готов: {exc}",
                        [int(item) for item in vmids],
                    ) from exc
        if progress:
            progress(90, "Все VM отвечают после отката")

    def restore_credentials(self, credentials: list[dict[str, Any]]) -> list[int]:
        usable = [
            dict(item) for item in credentials
            if item.get("vmid") is not None and str(item.get("password") or "")
        ]
        if not usable:
            return []
        vmids = [int(item["vmid"]) for item in usable]
        inventory = self._vm_inventory(vmids)

        def apply(item: dict[str, Any]) -> None:
            vmid = int(item["vmid"])
            node = str(inventory[vmid]["node"])
            last_error: Exception | None = None
            for attempt in range(1, 4):
                try:
                    self._wait_guest_agent(node, vmid, timeout=60)
                    self.client.nodes(node).qemu(vmid).agent("set-user-password").post(
                        username=str(item.get("username") or "root"),
                        password=str(item["password"]),
                    )
                    return
                except Exception as exc:
                    last_error = exc
                    if attempt < 3:
                        time.sleep(attempt)
            raise RuntimeError(str(last_error or "неизвестная ошибка QEMU Guest Agent"))

        workers = max(1, min(self._batch_limit("PROXMOX_DEPLOY_WORKERS"), len(usable)))
        restored: list[int] = []
        failures: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pve-rollback-password") as executor:
            futures = {executor.submit(apply, item): int(item["vmid"]) for item in usable}
            for future in as_completed(futures):
                vmid = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    failures[vmid] = str(exc)
                else:
                    restored.append(vmid)
        if failures:
            raise CredentialRestoreError(failures, restored)
        return sorted(restored)

    def run_autocheck(self, vmids: list[int], script: str) -> list[dict[str, Any]]:
        """Run the editable check inside guests, never on the dashboard host."""
        if not script.strip():
            raise RuntimeError("Скрипт автопроверки пуст")
        results: list[dict[str, Any]] = []
        for vmid in vmids:
            node = self._locate_vm(vmid)
            execution = self._guest_script(node, vmid, script, "autocheck", {"VMID": str(vmid)}, timeout=180)
            parsed = self._parse_check_output(execution["stdout"])
            for item in parsed:
                results.append({
                    "name": f"VM {vmid} · {item['name']}",
                    "ok": bool(item["ok"]),
                    "duration": execution["duration"],
                    "message": str(item.get("message", ""))[-500:],
                })
            if not parsed or execution["exit_code"] != 0:
                output = execution["stderr"] or execution["stdout"]
                results.append({
                    "name": f"VM {vmid} · выполнение скрипта",
                    "ok": execution["exit_code"] == 0,
                    "duration": execution["duration"],
                    "message": output[-500:] or f"exit code {execution['exit_code']}",
                })
        return results

    @staticmethod
    def _pve_access_time(value: str) -> datetime | None:
        match = re.fullmatch(
            r"(\d{1,2})/(\d{1,2}|[A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})",
            value.strip(),
        )
        if not match:
            return None
        months = {
            "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
            "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
        }
        month_value = match.group(2)
        month = int(month_value) if month_value.isdigit() else months.get(month_value.title())
        if month is None:
            return None
        offset = timedelta(hours=int(match.group(8)), minutes=int(match.group(9)))
        if match.group(7) == "-":
            offset = -offset
        try:
            return datetime(
                int(match.group(3)), month, int(match.group(1)),
                int(match.group(4)), int(match.group(5)), int(match.group(6)),
                tzinfo=timezone(offset),
            ).astimezone(timezone.utc)
        except ValueError:
            return None

    def web_activity(self, vmids: list[int], window_seconds: int = 180) -> dict[str, Any]:
        """Derive recent PVE UI presence from nested pveproxy access logs."""
        requested = sorted({int(vmid) for vmid in vmids})
        observed = datetime.now(timezone.utc)
        safe_window = max(30, min(int(window_seconds), 900))
        if not requested:
            return {
                "activity": [], "errors": [], "scanned_vms": 0, "requested_vms": 0,
                "window_seconds": safe_window,
                "observed_at": observed.replace(microsecond=0).isoformat(),
            }
        inventory = self._vm_inventory(requested, require_all=False)
        available = [vmid for vmid in requested if vmid in inventory]
        errors: list[dict[str, Any]] = [
            {"vmid": vmid, "error": "VM не найдена в Proxmox"}
            for vmid in requested
            if vmid not in inventory
        ]
        lines_by_vm: dict[int, tuple[str, str]] = {}

        def read_log(vmid: int) -> tuple[int, str, str]:
            node = str(inventory[vmid]["node"])
            execution = self._guest_command(
                node, vmid,
                ["/usr/bin/tail", "-n", "500", "/var/log/pveproxy/access.log"],
                timeout=8,
            )
            if execution["exit_code"] != 0:
                raise RuntimeError(execution["stderr"] or f"tail завершился с кодом {execution['exit_code']}")
            return vmid, node, execution["stdout"]

        if available:
            workers = max(
                1,
                min(self._batch_limit("PROXMOX_ACTIVITY_WORKERS", default=4), len(available)),
            )
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pve-web-activity") as executor:
                futures = {executor.submit(read_log, vmid): vmid for vmid in available}
                for future in as_completed(futures):
                    vmid = futures[future]
                    try:
                        _, node, output = future.result()
                        lines_by_vm[vmid] = (node, output)
                    except Exception as exc:
                        errors.append({"vmid": vmid, "error": str(exc)[-500:]})

        pattern = re.compile(
            r'^(?P<ip>\S+)\s+-\s+(?P<user>\S+)\s+\[(?P<stamp>[^\]]+)\]\s+'
            r'"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+HTTP/[0-9.]+"\s+'
            r'(?P<status>\d{3})\s+(?P<bytes>\S+)'
        )
        grouped: dict[tuple[int, str, str], dict[str, Any]] = {}
        cutoff = observed - timedelta(seconds=safe_window)
        for vmid, (node, output) in lines_by_vm.items():
            for line in output.splitlines():
                match = pattern.match(line.strip())
                if not match or match.group("user") == "-":
                    continue
                status_code = int(match.group("status"))
                if not 200 <= status_code < 400:
                    continue
                resource_path = match.group("path").split("?", 1)[0]
                if resource_path not in {
                    "/api2/extjs/cluster/resources",
                    "/api2/json/cluster/resources",
                }:
                    continue
                seen = self._pve_access_time(match.group("stamp"))
                if seen is None or seen < cutoff or seen > observed + timedelta(seconds=30):
                    continue
                key = (vmid, match.group("ip"), match.group("user"))
                item = grouped.setdefault(key, {
                    "vmid": vmid, "node": node,
                    "source_ip": match.group("ip"), "user": match.group("user"),
                    "last_seen": seen.replace(microsecond=0).isoformat(), "request_count": 0,
                })
                item["request_count"] += 1
                if seen > datetime.fromisoformat(str(item["last_seen"])):
                    item["last_seen"] = seen.replace(microsecond=0).isoformat()
        activity = list(grouped.values())
        for item in activity:
            last_seen = datetime.fromisoformat(str(item["last_seen"]))
            age = max(0, int((observed - last_seen).total_seconds()))
            item["age_seconds"] = age
            item["state"] = "active" if age <= 60 else "recent"
        activity.sort(key=lambda item: (item["state"] != "active", item["age_seconds"], item["vmid"]))
        return {
            "activity": activity,
            "errors": sorted(errors, key=lambda item: item["vmid"]),
            "scanned_vms": len(lines_by_vm), "requested_vms": len(requested),
            "window_seconds": safe_window,
            "observed_at": observed.replace(microsecond=0).isoformat(),
        }

    @staticmethod
    def _parse_check_output(output: str) -> list[dict[str, Any]]:
        def normalized(value: Any) -> list[dict[str, Any]]:
            items = value if isinstance(value, list) else [value]
            parsed_items: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict) or "name" not in item or "ok" not in item:
                    continue
                ok_value = item["ok"]
                if isinstance(ok_value, str):
                    ok = ok_value.strip().lower() in {"1", "true", "yes", "ok", "passed"}
                else:
                    ok = bool(ok_value)
                parsed_items.append({"name": str(item["name"]), "ok": ok, "message": item.get("message", "")})
            return parsed_items

        text = output.strip()
        if not text:
            return []
        try:
            parsed = normalized(json.loads(text))
            if parsed:
                return parsed
        except json.JSONDecodeError:
            pass
        result: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                result.extend(normalized(json.loads(line)))
                continue
            except json.JSONDecodeError:
                pass
            token_checks = re.findall(r"(?:^|\s)([^:\s]+):(ok|pass|passed|fail|failed|error)(?=\s|$)", line, re.IGNORECASE)
            if token_checks:
                for name, state in token_checks:
                    result.append({
                        "name": name,
                        "ok": state.lower() in {"ok", "pass", "passed"},
                        "message": f"{name}:{state}",
                    })
                continue
            if ":" in line:
                name, state = line.rsplit(":", 1)
                if state.strip().lower() in {"ok", "pass", "passed", "fail", "failed", "error"}:
                    result.append({
                        "name": name.strip(),
                        "ok": state.strip().lower() in {"ok", "pass", "passed"},
                        "message": line,
                    })
        return result

    def delete_stand(self, stand: dict[str, Any], vmids: list[int]) -> None:
        pool_id = str(stand["pool_id"])
        preserve_pool = str(stand.get("origin") or "deployed") == "existing"
        try:
            pool = self.client.pools(pool_id).get()
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            response = getattr(exc, "response", None)
            if status_code is None and response is not None:
                status_code = getattr(response, "status_code", None)
            try:
                missing_pool = int(status_code) == 404
            except (TypeError, ValueError):
                missing_pool = False
            if not missing_pool:
                # PVE's pool API historically reports this specific not-found
                # condition as HTTP 500 instead of 404.
                missing_pool = bool(
                    re.search(r"\bpool\s+['\"][^'\"]+['\"]\s+does not exist\b", str(exc), re.IGNORECASE)
                )
            if (
                str(stand.get("status") or "") == "error"
                and not vmids
                and missing_pool
            ):
                # The automatic deployment rollback already removed the pool.
                return
            raise RuntimeError(f"Управляемый пул {pool_id} не найден; удаление отменено") from exc
        expected_marker = f"DEMOEXAM dashboard stand_id={stand['id']}"
        if not preserve_pool and str(pool.get("comment", "")).strip() != expected_marker:
            raise RuntimeError("Пул не имеет метки владельца Deployer; удаление отменено")
        pool_members = [
            member
            for member in pool.get("members", [])
            if member.get("type") in {"qemu", "lxc"} and member.get("vmid") is not None
        ]
        pool_vmids = {
            int(member["vmid"])
            for member in pool_members
        }
        if preserve_pool:
            expected_vm_names = {
                int(vm["vmid"]): str(vm.get("name") or "")
                for vm in stand.get("vms", [])
                if vm.get("vmid") is not None
            }
            pool_names = {
                int(member["vmid"]): str(member.get("name") or "")
                for member in pool_members
            }
            mismatched_vmids = sorted(
                vmid for vmid in set(vmids) & pool_vmids
                if pool_names.get(vmid) != expected_vm_names.get(vmid)
            )
            if mismatched_vmids:
                raise RuntimeError(
                    "VM из существующего пула больше не совпадают с объектами Deployer; "
                    "удаление отменено: " + ", ".join(str(vmid) for vmid in mismatched_vmids)
                )
            existing_vmids = sorted(set(vmids) & pool_vmids)
        elif str(stand.get("status") or "") == "error":
            # A failed deploy can leave clones in the pool before their VMIDs
            # are committed to SQLite.  Recover only the names generated by
            # this deploy; an unrelated/manual member still blocks deletion.
            generated_name = re.compile(rf"^{re.escape(pool_id)}-\d+$")
            recoverable_vmids = {
                int(member["vmid"])
                for member in pool_members
                if member.get("type") == "qemu"
                and generated_name.fullmatch(str(member.get("name") or ""))
            }
            unexpected_vmids = sorted(pool_vmids - recoverable_vmids - set(vmids))
            if unexpected_vmids:
                raise RuntimeError(
                    "В ошибочном пуле есть ресурсы, не созданные этим стендом; "
                    "автоудаление отменено: "
                    + ", ".join(str(vmid) for vmid in unexpected_vmids)
                )
            existing_vmids = sorted((recoverable_vmids | set(vmids)) & pool_vmids)
        else:
            # A tracked VM may already have been deleted manually.  That is
            # safe to ignore; an unknown VM in a healthy pool must still abort.
            untracked_vmids = sorted(pool_vmids - set(vmids))
            if untracked_vmids:
                raise RuntimeError(
                    "В пуле обнаружены VM, отсутствующие в учёте Deployer; удаление отменено: "
                    + ", ".join(str(vmid) for vmid in untracked_vmids)
                )
            existing_vmids = sorted(pool_vmids & set(vmids))
        inventory = self._vm_inventory(existing_vmids) if existing_vmids else {}
        stop_tasks: list[tuple[str, str]] = []
        for vmid in existing_vmids:
            node = str(inventory[vmid]["node"])
            api = self.client.nodes(node).qemu(vmid)
            if inventory[vmid]["status"] == "running":
                # Deletion is already explicitly confirmed by the operator, so
                # use a parallel hard stop instead of waiting for 25 sequential
                # guest shutdown timeouts.
                upid = api.status.stop.post()
                if upid:
                    stop_tasks.append((node, str(upid)))
        self._wait_tasks(stop_tasks, timeout=300)

        delete_tasks: list[tuple[str, str]] = []
        delete_batch = self._batch_limit("PROXMOX_DELETE_BATCH")
        for vmid in existing_vmids:
            node = str(inventory[vmid]["node"])
            api = self.client.nodes(node).qemu(vmid)
            try:
                api.config.put(**{"delete": "lock"})
            except Exception:
                pass
            # PVE 7 and some early PVE 8 builds reject
            # destroy-unreferenced-disks as an unknown schema property.
            upid = api.delete(purge=1)
            if upid:
                delete_tasks.append((node, str(upid)))
            if len(delete_tasks) >= delete_batch:
                self._wait_tasks(delete_tasks, timeout=1800)
                delete_tasks.clear()
        self._wait_tasks(delete_tasks, timeout=1800)
        if preserve_pool:
            return
        # Close the window where an ambiguous clone request could attach a VM
        # after the first pool read but before pool deletion.
        latest_pool = self.client.pools(pool_id).get()
        if str(latest_pool.get("comment", "")).strip() != expected_marker:
            raise RuntimeError("Метка владельца пула изменилась; удаление отменено")
        late_vmids = sorted(
            int(member["vmid"])
            for member in latest_pool.get("members", [])
            if member.get("type") in {"qemu", "lxc"} and member.get("vmid") is not None
        )
        if late_vmids:
            raise RuntimeError(
                "В пуле появились новые VM во время удаления; пул сохранён: "
                + ", ".join(str(vmid) for vmid in late_vmids)
            )
        self.client.pools(pool_id).delete()

    def cluster_metrics(self, tracked_vmids: list[int] | None = None) -> dict[str, Any]:
        tracked_key = frozenset(int(vmid) for vmid in (tracked_vmids or []))
        now = time.monotonic()
        with self._metrics_lock:
            if (
                self._metrics_cache_value is not None
                and self._metrics_cache_key == tracked_key
                and now - self._metrics_cache_at < 5
            ):
                return self._metrics_cache_value
            result = self._collect_cluster_metrics(set(tracked_key))
            self._metrics_cache_key = tracked_key
            self._metrics_cache_at = now
            self._metrics_cache_value = result
            return result

    def _collect_cluster_metrics(self, tracked: set[int]) -> dict[str, Any]:
        resources = self.client.cluster.resources.get()
        node_resources = {item["node"]: item for item in resources if item.get("type") == "node"}
        vm_resources = [item for item in resources if item.get("type") in {"qemu", "lxc"}]
        nodes = []
        for name, resource in node_resources.items():
            max_cpu = max(float(resource.get("maxcpu", 1)), 1)
            max_mem = max(float(resource.get("maxmem", 1)), 1)
            node_vms = [vm for vm in vm_resources if vm.get("node") == name and vm.get("status") == "running"]
            exam_vms = [vm for vm in node_vms if int(vm.get("vmid", -1)) in tracked]
            exam_cores = sum(float(vm.get("cpu", 0)) * max(float(vm.get("maxcpu", 1)), 1) for vm in exam_vms)
            exam_mem = sum(float(vm.get("mem", 0)) for vm in exam_vms)
            exam_disk = sum(float(vm.get("disk", 0)) for vm in exam_vms)
            nodes.append({
                "name": name, "status": resource.get("status", "unknown"),
                "cpu": round(float(resource.get("cpu", 0)) * 100, 1),
                "ram": round(float(resource.get("mem", 0)) / max_mem * 100, 1),
                "disk": round(float(resource.get("disk", 0)) / max(float(resource.get("maxdisk", 1)), 1) * 100, 1),
                "exam_cpu": round(_clamp(exam_cores / max_cpu * 100, 0, 100), 1),
                "exam_ram": round(_clamp(exam_mem / max_mem * 100, 0, 100), 1),
                "exam_disk": round(_clamp(exam_disk / max(float(resource.get("maxdisk", 1)), 1) * 100, 0, 100), 1),
                "running_vms": len(node_vms), "exam_vms": len(exam_vms),
                "uptime_days": round(float(resource.get("uptime", 0)) / 86400),
            })
        total_cpu_capacity = sum(max(float(item.get("maxcpu", 1)), 1) for item in node_resources.values())
        used_cpu = sum(float(item.get("cpu", 0)) * max(float(item.get("maxcpu", 1)), 1) for item in node_resources.values())
        exam_cpu_cores = sum(
            float(vm.get("cpu", 0)) * max(float(vm.get("maxcpu", 1)), 1)
            for vm in vm_resources if vm.get("status") == "running" and int(vm.get("vmid", -1)) in tracked
        )
        total_mem_capacity = sum(max(float(item.get("maxmem", 1)), 1) for item in node_resources.values())
        used_mem = sum(float(item.get("mem", 0)) for item in node_resources.values())
        exam_mem = sum(
            float(vm.get("mem", 0)) for vm in vm_resources
            if vm.get("status") == "running" and int(vm.get("vmid", -1)) in tracked
        )
        total_disk_capacity = sum(max(float(item.get("maxdisk", 1)), 1) for item in node_resources.values())
        used_disk = sum(float(item.get("disk", 0)) for item in node_resources.values())
        exam_disk = sum(
            float(vm.get("disk", 0)) for vm in vm_resources
            if vm.get("status") == "running" and int(vm.get("vmid", -1)) in tracked
        )
        cpu = round(_clamp(used_cpu / max(total_cpu_capacity, 1) * 100, 0, 100), 1)
        ram = round(_clamp(used_mem / max(total_mem_capacity, 1) * 100, 0, 100), 1)
        disk = round(_clamp(used_disk / max(total_disk_capacity, 1) * 100, 0, 100), 1)
        exam_cpu = round(_clamp(exam_cpu_cores / max(total_cpu_capacity, 1) * 100, 0, 100), 1)
        exam_ram = round(_clamp(exam_mem / max(total_mem_capacity, 1) * 100, 0, 100), 1)
        exam_disk_percent = round(_clamp(exam_disk / max(total_disk_capacity, 1) * 100, 0, 100), 1)
        cluster = {
            "cpu": cpu, "ram": ram, "disk": disk,
            "exam_cpu": exam_cpu, "exam_ram": exam_ram, "exam_disk": exam_disk_percent,
            "exam_cpu_share": round(_clamp(exam_cpu_cores / max(used_cpu, 0.001) * 100, 0, 100), 1),
            "exam_ram_share": round(_clamp(exam_mem / max(used_mem, 1) * 100, 0, 100), 1),
            "exam_disk_share": round(_clamp(exam_disk / max(used_disk, 1) * 100, 0, 100), 1),
            "online_nodes": sum(1 for node in nodes if node["status"] == "online"), "total_nodes": len(nodes),
        }
        sample = {
            "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "total": cpu,
            "exam": exam_cpu,
        }
        with self._metrics_lock:
            self._metrics_history.append(sample)
            history = list(self._metrics_history)
        return {
            "cluster": cluster, "nodes": nodes, "history": history,
            "attribution_method": "Сумма CPU, RAM и диска VM, принадлежащих стендам, относительно ёмкости кластера",
            "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }


def create_gateway() -> DemoProxmoxGateway | LiveProxmoxGateway:
    if os.environ.get("PROXMOX_MODE", "demo").lower() == "live":
        return LiveProxmoxGateway()
    return DemoProxmoxGateway()
