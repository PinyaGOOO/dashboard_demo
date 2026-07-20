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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


ProgressCallback = Callable[[int, str], None]


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
            time.sleep(0.55)
            progress(value, message)
        nodes = ["pve-01", "pve-02", "pve-03", "pve-04"]
        vm_count = int(blueprint.get("vm_count") or 1)
        return [
            {
                "vmid": 2000 + int(stand["id"]) * 10 + index,
                "name": f"{stand['pool_id']}-{index}",
                "node": nodes[(int(stand["id"]) + index) % len(nodes)],
                "ip": self._ip_for(blueprint.get("subnet", ""), index),
                "status": "running",
            }
            for index in range(1, vm_count + 1)
        ]

    @staticmethod
    def _ip_for(subnet: str, index: int) -> str:
        try:
            network = ipaddress.ip_network(str(subnet), strict=False)
        except ValueError:
            return ""
        if network.version != 4:
            return ""
        if network.prefixlen >= 31:
            offset = index - 1
        else:
            preferred = 39 + index
            offset = preferred if preferred < network.num_addresses - 1 else index
        candidate = int(network.network_address) + offset
        last_usable = int(network.broadcast_address) if network.prefixlen >= 31 else int(network.broadcast_address) - 1
        return str(ipaddress.ip_address(candidate)) if candidate <= last_usable else ""

    def power_action(self, vmids: list[int], action: str) -> None:
        time.sleep(0.25)

    def rotate_password(self, vmids: list[int], username: str, password: str) -> None:
        time.sleep(0.35)

    def create_snapshot(self, vmids: list[int], name: str, description: str = "") -> None:
        time.sleep(0.25)

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
        self._wait_guest_agent(node, vmid)
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

    def _cleanup_failed_deploy(self, created: list[tuple[str, int]], pool_id: str) -> None:
        for node, vmid in reversed(created):
            api = self.client.nodes(node).qemu(vmid)
            try:
                current = api.status.current.get()
                if current.get("status") == "running":
                    upid = api.status.stop.post()
                    self._wait_task(node, upid)
            except Exception:
                pass
            try:
                api.config.put(**{"delete": "lock"})
            except Exception:
                pass
            try:
                upid = api.delete(purge=1, destroy_unreferenced_disks=1)
                self._wait_task(node, upid)
            except Exception:
                pass
        try:
            self.client.pools(pool_id).delete()
        except Exception:
            pass

    @staticmethod
    def _network_with_bridge(network: str, bridge: str) -> str:
        parts = [part for part in str(network).split(",") if part]
        if not parts:
            parts = ["virtio"]
        parts = [part for part in parts if not part.startswith("bridge=")]
        parts.append(f"bridge={bridge}")
        return ",".join(parts)

    def deploy(self, stand: dict[str, Any], blueprint: dict[str, Any], progress: ProgressCallback) -> list[dict[str, Any]]:
        pool_id = stand["pool_id"]
        pools = {pool["poolid"] for pool in self.client.pools.get()}
        if pool_id in pools:
            raise RuntimeError(f"Пул Proxmox {pool_id} уже существует")
        self.client.pools.post(poolid=pool_id, comment=f"DEMOEXAM dashboard stand_id={stand['id']}")
        deployed: list[dict[str, Any]] = []
        created: list[tuple[str, int]] = []
        try:
            progress(12, "Пул создан")
            template_vmid = int(blueprint["template_vmid"])
            if template_vmid <= 0:
                raise RuntimeError("Не указан VMID шаблона")
            template_node = self._find_template_node(template_vmid)
            target_nodes = self._rank_nodes()
            requested_node = str(stand.get("node", "")).strip()
            if requested_node and requested_node != "auto":
                if requested_node not in target_nodes:
                    raise RuntimeError(f"Нода {requested_node} недоступна")
                target_nodes = [requested_node]
            vm_count = int(blueprint.get("vm_count") or 1)
            for index in range(1, vm_count + 1):
                target_node = target_nodes[(index - 1) % len(target_nodes)]
                new_vmid = int(self.client.cluster.nextid.get())
                created.append((target_node, new_vmid))
                name = f"{pool_id}-{index}"
                params: dict[str, Any] = {
                    "newid": new_vmid, "name": name, "full": 0,
                    "target": target_node, "pool": pool_id,
                }
                upid = self.client.nodes(template_node).qemu(template_vmid).clone.post(**params)
                self._wait_task(template_node, upid)
                config: dict[str, Any] = {"agent": "1"}
                if blueprint.get("bridge"):
                    vm_api = self.client.nodes(target_node).qemu(new_vmid)
                    current_config = vm_api.config.get()
                    config["net0"] = self._network_with_bridge(
                        str(current_config.get("net0", "")), str(blueprint["bridge"]),
                    )
                self.client.nodes(target_node).qemu(new_vmid).config.put(**config)
                upid = self.client.nodes(target_node).qemu(new_vmid).status.start.post()
                self._wait_task(target_node, upid)
                vm_ip = DemoProxmoxGateway._ip_for(str(blueprint.get("subnet", "")), index)
                deploy_script = str(blueprint.get("deploy_script", ""))
                if deploy_script.strip():
                    result = self._guest_script(
                        target_node,
                        new_vmid,
                        deploy_script,
                        "deploy",
                        {
                            "STAND_NAME": str(stand["name"]),
                            "STAND_POOL": str(pool_id),
                            "VM_INDEX": str(index),
                            "VMID": str(new_vmid),
                            "VM_IP": vm_ip,
                            "STAND_SUBNET": str(blueprint.get("subnet", "")),
                            "VM_BRIDGE": str(blueprint.get("bridge", "")),
                        },
                        timeout=900,
                    )
                    if result["exit_code"] != 0:
                        detail = result["stderr"] or result["stdout"] or f"exit code {result['exit_code']}"
                        raise RuntimeError(f"Скрипт развёртывания VM {new_vmid}: {detail[-500:]}")
                deployed.append({
                    "vmid": new_vmid, "name": name, "node": target_node,
                    "ip": vm_ip, "status": "running",
                })
                progress(12 + round(index / vm_count * 78), f"VM {index} из {vm_count} настроена")
            progress(100, "Стенд готов")
            return deployed
        except Exception:
            self._cleanup_failed_deploy(created, pool_id)
            raise

    def _locate_vm(self, vmid: int) -> str:
        for node in self.client.nodes.get():
            name = node["node"]
            try:
                if any(int(vm.get("vmid", -1)) == int(vmid) for vm in self.client.nodes(name).qemu.get()):
                    return name
            except Exception:
                continue
        raise RuntimeError(f"VM {vmid} не найдена")

    def power_action(self, vmids: list[int], action: str) -> None:
        endpoint = {"start": "start", "stop": "shutdown", "restart": "reboot"}.get(action)
        if endpoint is None:
            raise ValueError("Неизвестное действие питания")
        for vmid in vmids:
            node = self._locate_vm(vmid)
            status = self.client.nodes(node).qemu(vmid).status
            upid = getattr(status, endpoint).post()
            self._wait_task(node, upid)

    def rotate_password(self, vmids: list[int], username: str, password: str) -> None:
        for vmid in vmids:
            node = self._locate_vm(vmid)
            self.client.nodes(node).qemu(vmid).agent("set-user-password").post(username=username, password=password)

    def create_snapshot(self, vmids: list[int], name: str, description: str = "") -> None:
        for vmid in vmids:
            node = self._locate_vm(vmid)
            upid = self.client.nodes(node).qemu(vmid).snapshot.post(
                snapname=name,
                description=description,
            )
            if upid:
                self._wait_task(node, str(upid))

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
        try:
            pool = self.client.pools(pool_id).get()
        except Exception as exc:
            raise RuntimeError(f"Управляемый пул {pool_id} не найден; удаление отменено") from exc
        expected_marker = f"DEMOEXAM dashboard stand_id={stand['id']}"
        if str(pool.get("comment", "")).strip() != expected_marker:
            raise RuntimeError("Пул не имеет метки владельца DemoOps; удаление отменено")
        pool_vmids = {
            int(member["vmid"])
            for member in pool.get("members", [])
            if member.get("type") in {"qemu", "lxc"} and member.get("vmid") is not None
        }
        foreign_vmids = sorted(set(vmids) - pool_vmids)
        if foreign_vmids:
            raise RuntimeError(
                "VM не принадлежат управляемому пулу; удаление отменено: "
                + ", ".join(str(vmid) for vmid in foreign_vmids)
            )
        untracked_vmids = sorted(pool_vmids - set(vmids))
        if untracked_vmids:
            raise RuntimeError(
                "В пуле обнаружены VM, отсутствующие в учёте DemoOps; удаление отменено: "
                + ", ".join(str(vmid) for vmid in untracked_vmids)
            )
        for vmid in vmids:
            node = self._locate_vm(vmid)
            api = self.client.nodes(node).qemu(vmid)
            try:
                current = api.status.current.get()
                if current.get("status") == "running":
                    upid = api.status.stop.post()
                    self._wait_task(node, upid)
            finally:
                upid = api.delete(purge=1, destroy_unreferenced_disks=1)
                self._wait_task(node, upid)
        try:
            self.client.pools(stand["pool_id"]).delete()
        except Exception:
            pass

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
