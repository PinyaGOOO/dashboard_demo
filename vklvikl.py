from proxmoxer import ProxmoxAPI
import warnings
import time
import os
import requests
import webbrowser
import urllib.parse
import string
import secrets

from math import ceil
warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────
#  ПУТЬ К ФАЙЛУ С ПАРОЛЯМИ НОД
# ─────────────────────────────────────────────
PASSWORDS_FILE = "password-ssa-24-06.txt"
NODE_WEB_USER = "root@pam"

DEF_CONNECTION = {
    "IP": os.environ.get("PROXMOX_HOST", "").strip(),
    "USER": os.environ.get("PROXMOX_USER", "").strip(),
    "TOKEN_NAME": os.environ.get("PROXMOX_TOKEN_NAME", "").strip(),
    "TOKEN_KEY": os.environ.get("PROXMOX_TOKEN_VALUE", "").strip(),
    "HOSTS": os.environ.get("PROXMOX_HOSTS", "").strip(),
}

USERS = ["popovs@pve", "fedorin@pve"]


# ─────────────────────────────────────────────
#  КЛАСС-ОБЁРТКА НАД PROXMOX API
# ─────────────────────────────────────────────

class ProxmoxManager(ProxmoxAPI):

    def get_node(self, vmid):
        """Найти ноду по VMID (qemu или lxc)"""
        for node in self.nodes.get():
            if node["status"] != "online":
                continue
            nodename = node["node"]
            try:
                for vm in self.nodes(nodename).qemu.get():
                    if str(vm["vmid"]) == str(vmid):
                        return node, "qemu"
            except Exception:
                pass
            try:
                for ct in self.nodes(nodename).lxc.get():
                    if str(ct["vmid"]) == str(vmid):
                        return node, "lxc"
            except Exception:
                pass
        return None, None

    def get_vm_api(self, vmid):
        """Вернуть API-объект VM/CT по VMID"""
        node, vm_type = self.get_node(vmid)
        if not node:
            return None, None, None
        nodename = node["node"]
        if vm_type == "qemu":
            return self.nodes(nodename).qemu(str(vmid)), nodename, "qemu"
        else:
            return self.nodes(nodename).lxc(str(vmid)), nodename, "lxc"

    def get_nextid(self):
        """Следующий свободный VMID"""
        return self.cluster.nextid.get()

    def get_vms_in_pool(self, poolid):
        """Список всех VM/CT в пуле"""
        pool = self.pools(poolid).get()
        return pool.get("members", [])

    def check_pool_exists(self, poolid):
        return any(p["poolid"] == poolid for p in self.pools.get())

    def wait_task(self, upid, nodename):
        """Блокирующее ожидание завершения задачи Proxmox"""
        while True:
            data = self.nodes(nodename).tasks(upid).status.get()
            if data["status"] == "stopped":
                return data
            time.sleep(1)

    def wait_agent(self, vmid, timeout=180):
        """Ждать пока guest agent ответит на ping"""
        node, vm_type = self.get_node(vmid)
        if not node:
            return False
        nodename = node["node"]
        for _ in range(timeout):
            try:
                self.nodes(nodename).qemu(str(vmid)).agent("ping").post()
                return True
            except KeyboardInterrupt:
                raise
            except Exception:
                time.sleep(1)
        return False

    def guest_run(self, vmid, cmd, wait=True):
        """Выполнить команду внутри VM через guest agent"""
        node, vm_type = self.get_node(vmid)
        if not node:
            return None
        nodename = node["node"]
        task = self.nodes(nodename).qemu(str(vmid)).agent("exec").post(command=cmd)
        if not wait:
            return None
        pid = task["pid"]
        while True:
            result = self.nodes(nodename).qemu(str(vmid)).agent("exec-status").get(pid=pid)
            if result.get("exited"):
                return result
            time.sleep(1)

    def vm_change_password(self, vmid, username, password):
        """Сменить пароль пользователя внутри VM через guest agent"""
        node, vm_type = self.get_node(vmid)
        if not node:
            return False
        nodename = node["node"]
        try:
            self.nodes(nodename).qemu(str(vmid)).agent("set-user-password").post(
                username=username, password=password
            )
            return True
        except Exception as e:
            print(f"  [ERR] Смена пароля: {e}")
            return False

    def unlock_vm(self, vmid):
        """Снять лок с VM"""
        node, vm_type = self.get_node(vmid)
        if not node:
            return False
        nodename = node["node"]
        try:
            self.nodes(nodename).qemu(str(vmid)).config.put(**{"delete": "lock"})
            return True
        except Exception:
            return False


def env_bool(name, default=True):
    """Прочитать булеву переменную окружения без небезопасных неявных значений."""
    value = os.environ.get(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{name} должна быть одним из значений: true/false, yes/no, on/off, 1/0"
    )


def connect():
    required = {
        "PROXMOX_HOST": DEF_CONNECTION["IP"],
        "PROXMOX_USER": DEF_CONNECTION["USER"],
        "PROXMOX_TOKEN_NAME": DEF_CONNECTION["TOKEN_NAME"],
        "PROXMOX_TOKEN_VALUE": DEF_CONNECTION["TOKEN_KEY"],
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("Не заданы переменные окружения: " + ", ".join(missing))

    return ProxmoxManager(
        DEF_CONNECTION["IP"],
        user=DEF_CONNECTION["USER"],
        token_name=DEF_CONNECTION["TOKEN_NAME"],
        token_value=DEF_CONNECTION["TOKEN_KEY"],
        verify_ssl=env_bool("PROXMOX_VERIFY_SSL", default=True)
    )


def get_pools(proxmox):
    return proxmox.pools.get()


def show_pools(pools):
    print("\n=== Пулы на Proxmox ===")
    for i, pool in enumerate(pools, 1):
        poolid = pool.get("poolid", "?")
        comment = pool.get("comment", "")
        print(f"  [{i}] {poolid}" + (f" — {comment}" if comment else ""))
    print()


def get_pool_members(proxmox, poolid):
    pool = proxmox.pools(poolid).get()
    return pool.get("members", [])


def show_pool_members(proxmox, poolid):
    members = get_pool_members(proxmox, poolid)
    vms = [m for m in members if m.get("type") == "qemu"]
    cts = [m for m in members if m.get("type") == "lxc"]

    print(f"\n=== Виртуальные машины в пуле {poolid} ===")
    if vms:
        for vm in vms:
            vmid = vm["vmid"]
            name = vm.get("name", "без имени")
            status = vm.get("status", "?")
            node = vm["node"]
            status_icon = "🟢" if status == "running" else "🔴"
            print(f"  {status_icon} [{vmid}] {name} | {node} | {status}")
    else:
        print("  Нет VM")

    print(f"\n=== Контейнеры в пуле {poolid} ===")
    if cts:
        for ct in cts:
            vmid = ct["vmid"]
            name = ct.get("name", "без имени")
            status = ct.get("status", "?")
            node = ct["node"]
            status_icon = "🟢" if status == "running" else "🔴"
            print(f"  {status_icon} [{vmid}] {name} | {node} | {status}")
    else:
        print("  Нет контейнеров")
    print()


def start_pool(proxmox, poolid):
    members = get_pool_members(proxmox, poolid)
    vms = [m for m in members if m.get("type") == "qemu"]
    cts = [m for m in members if m.get("type") == "lxc"]

    if not vms and not cts:
        print(f"  [!] В пуле {poolid} нет виртуальных машин или контейнеров")
        return

    for vm in vms:
        vmid = vm["vmid"]
        node = vm["node"]
        status = vm.get("status", "")
        if status == "running":
            print(f"  [~] VM {vmid} уже запущена")
        else:
            try:
                proxmox.nodes(node).qemu(vmid).status.start.post()
                print(f"  [OK] VM {vmid} на {node} — запущена")
            except Exception as e:
                print(f"  [ERR] VM {vmid}: {e}")

    for ct in cts:
        vmid = ct["vmid"]
        node = ct["node"]
        status = ct.get("status", "")
        if status == "running":
            print(f"  [~] CT {vmid} уже запущен")
        else:
            try:
                proxmox.nodes(node).lxc(vmid).status.start.post()
                print(f"  [OK] CT {vmid} на {node} — запущен")
            except Exception as e:
                print(f"  [ERR] CT {vmid}: {e}")


def stop_pool(proxmox, poolid):
    members = get_pool_members(proxmox, poolid)
    vms = [m for m in members if m.get("type") == "qemu"]
    cts = [m for m in members if m.get("type") == "lxc"]

    if not vms and not cts:
        print(f"  [!] В пуле {poolid} нет виртуальных машин или контейнеров")
        return

    for vm in vms:
        vmid = vm["vmid"]
        node = vm["node"]
        status = vm.get("status", "")
        if status == "stopped":
            print(f"  [~] VM {vmid} уже остановлена")
        else:
            try:
                proxmox.nodes(node).qemu(vmid).status.stop.post()
                print(f"  [OK] VM {vmid} на {node} — остановлена")
            except Exception as e:
                print(f"  [ERR] VM {vmid}: {e}")

    for ct in cts:
        vmid = ct["vmid"]
        node = ct["node"]
        status = ct.get("status", "")
        if status == "stopped":
            print(f"  [~] CT {vmid} уже остановлен")
        else:
            try:
                proxmox.nodes(node).lxc(vmid).status.stop.post()
                print(f"  [OK] CT {vmid} на {node} — остановлен")
            except Exception as e:
                print(f"  [ERR] CT {vmid}: {e}")


def grant_pool_access(proxmox, poolid):
    for user in USERS:
        try:
            proxmox.access.acl.put(
                path=f"/pool/{poolid}",
                users=user,
                roles="PVEAdmin",
                propagate=1
            )
            print(f"  [OK] {user} -> PVEAdmin на пул {poolid}")
        except Exception as e:
            print(f"  [ERR] {user}: {e}")


def revoke_pool_access(proxmox, poolid):
    for user in USERS:
        try:
            proxmox.access.acl.put(
                path=f"/pool/{poolid}",
                users=user,
                roles="PVEAdmin",
                propagate=1,
                delete=1
            )
            print(f"  [OK] {user} -> права на пул {poolid} забраны")
        except Exception as e:
            print(f"  [ERR] {user}: {e}")


def find_vm_node(proxmox, vmid):
    """Обёртка для совместимости — использует метод класса"""
    node, vm_type = proxmox.get_node(vmid)
    if node:
        return node["node"], vm_type
    return None, None


# ─────────────────────────────────────────────
#  ВКЛЮЧЕНИЕ / ВЫКЛЮЧЕНИЕ ОТДЕЛЬНОЙ VM
# ─────────────────────────────────────────────

def vm_power_menu(proxmox):
    print("\n=== Управление отдельной VM/CT ===")
    print("Введите ID виртуальной машины или контейнера: ", end="")
    vmid_input = input().strip()

    if not vmid_input.isdigit():
        print("  [!] ID должен быть числом")
        return

    vmid = int(vmid_input)
    print(f"  Ищу VM/CT с ID {vmid}...")

    node, vm_type = find_vm_node(proxmox, vmid)
    if not node:
        print(f"  [!] VM/CT с ID {vmid} не найдена")
        return

    type_label = "VM" if vm_type == "qemu" else "CT"

    try:
        if vm_type == "qemu":
            info = proxmox.nodes(node).qemu(vmid).status.current.get()
        else:
            info = proxmox.nodes(node).lxc(vmid).status.current.get()
        status = info.get("status", "?")
        name = info.get("name", "без имени")
    except Exception as e:
        print(f"  [ERR] Не удалось получить статус: {e}")
        return

    status_icon = "🟢" if status == "running" else "🔴"
    print(f"  {status_icon} {type_label} {vmid} ({name}) | {node} | {status}")
    print()
    print("  [1] Запустить")
    print("  [2] Остановить (graceful shutdown)")
    print("  [3] Выключить принудительно (stop)")
    print("  [4] Перезагрузить")
    print("  [0] Назад")
    print("Выбор: ", end="")
    action = input().strip()

    try:
        if action == "1":
            if status == "running":
                print(f"  [~] {type_label} {vmid} уже запущена")
            else:
                if vm_type == "qemu":
                    proxmox.nodes(node).qemu(vmid).status.start.post()
                else:
                    proxmox.nodes(node).lxc(vmid).status.start.post()
                print(f"  [OK] {type_label} {vmid} — запущена")

        elif action == "2":
            if status == "stopped":
                print(f"  [~] {type_label} {vmid} уже остановлена")
            else:
                if vm_type == "qemu":
                    proxmox.nodes(node).qemu(vmid).status.shutdown.post()
                else:
                    proxmox.nodes(node).lxc(vmid).status.shutdown.post()
                print(f"  [OK] {type_label} {vmid} — отправлен сигнал shutdown")

        elif action == "3":
            if status == "stopped":
                print(f"  [~] {type_label} {vmid} уже остановлена")
            else:
                if vm_type == "qemu":
                    proxmox.nodes(node).qemu(vmid).status.stop.post()
                else:
                    proxmox.nodes(node).lxc(vmid).status.stop.post()
                print(f"  [OK] {type_label} {vmid} — принудительно остановлена")

        elif action == "4":
            if status != "running":
                print(f"  [!] {type_label} {vmid} не запущена, перезагрузка невозможна")
            else:
                if vm_type == "qemu":
                    proxmox.nodes(node).qemu(vmid).status.reboot.post()
                else:
                    proxmox.nodes(node).lxc(vmid).status.reboot.post()
                print(f"  [OK] {type_label} {vmid} — перезагружается")

        elif action == "0":
            return
        else:
            print("  [!] Неверный выбор")

    except Exception as e:
        print(f"  [ERR] Ошибка: {e}")


# ─────────────────────────────────────────────
#  МОНИТОРИНГ РЕСУРСОВ В РЕАЛЬНОМ ВРЕМЕНИ
# ─────────────────────────────────────────────

def bytes_to_gb(b):
    return round(b / (1024 ** 3), 2)


def bar(used, total, width=20):
    if total == 0:
        return "[" + "-" * width + "]  n/a"
    pct = min(used / total, 1.0)
    filled = int(pct * width)
    return "[" + "#" * filled + "." * (width - filled) + f"]  {pct*100:.1f}%"


def resources_monitor(proxmox):
    print("\n  Мониторинг запущен. Нажмите Ctrl+C для выхода.\n")
    time.sleep(1)

    try:
        while True:
            os.system("cls" if os.name == "nt" else "clear")

            print("=" * 65)
            print("  МОНИТОРИНГ PROXMOX  |  обновление 5 сек  |  Ctrl+C — выход")
            print("=" * 65)

            nodes = proxmox.nodes.get()

            total_mem_used = 0
            total_mem_total = 0
            total_disk_used = 0
            total_disk_total = 0
            cpu_list = []
            node_count = 0

            for node_info in nodes:
                nodename = node_info["node"]
                node_status = node_info.get("status", "unknown")

                if node_status != "online":
                    print(f"\n  [OFFLINE] {nodename}")
                    continue

                try:
                    stats = proxmox.nodes(nodename).status.get()
                except Exception as e:
                    print(f"\n  [ERR] {nodename}: {e}")
                    continue

                cpu_pct = round(stats.get("cpu", 0) * 100, 1)
                cpus = stats.get("cpuinfo", {}).get("cpus", 1)

                mem = stats.get("memory", {})
                mem_used = mem.get("used", 0)
                mem_total = mem.get("total", 1)

                disk = stats.get("rootfs", {})
                disk_used = disk.get("used", 0)
                disk_total = disk.get("total", 1)

                uptime_sec = stats.get("uptime", 0)
                uptime_h = uptime_sec // 3600
                uptime_m = (uptime_sec % 3600) // 60

                total_mem_used += mem_used
                total_mem_total += mem_total
                total_disk_used += disk_used
                total_disk_total += disk_total
                cpu_list.append(cpu_pct)
                node_count += 1

                # VM/CT счётчики
                vm_run = vm_total = ct_run = ct_total = 0
                try:
                    vms = proxmox.nodes(nodename).qemu.get()
                    vm_total = len(vms)
                    vm_run = sum(1 for v in vms if v.get("status") == "running")
                    cts = proxmox.nodes(nodename).lxc.get()
                    ct_total = len(cts)
                    ct_run = sum(1 for c in cts if c.get("status") == "running")
                except Exception:
                    pass

                print(f"\n  NODE: {nodename}  |  uptime: {uptime_h}ч {uptime_m}м  |  ядер: {cpus}")
                print(f"  CPU   {bar(cpu_pct, 100)}")
                print(f"  RAM   {bar(mem_used, mem_total)}  {bytes_to_gb(mem_used)}/{bytes_to_gb(mem_total)} GB")
                print(f"  DISK  {bar(disk_used, disk_total)}  {bytes_to_gb(disk_used)}/{bytes_to_gb(disk_total)} GB")
                print(f"  VM: {vm_run}/{vm_total} запущено    CT: {ct_run}/{ct_total} запущено")

            # Итого по кластеру
            if node_count > 1:
                avg_cpu = round(sum(cpu_list) / len(cpu_list), 1)
                print("\n" + "-" * 65)
                print(f"  КЛАСТЕР ИТОГО  ({node_count} ноды)")
                print(f"  CPU   {bar(avg_cpu, 100)}  (среднее)")
                print(f"  RAM   {bar(total_mem_used, total_mem_total)}  {bytes_to_gb(total_mem_used)}/{bytes_to_gb(total_mem_total)} GB")
                print(f"  DISK  {bar(total_disk_used, total_disk_total)}  {bytes_to_gb(total_disk_used)}/{bytes_to_gb(total_disk_total)} GB")

            # Хранилища
            print("\n" + "-" * 65)
            print("  ХРАНИЛИЩА:")
            try:
                first_node = nodes[0]["node"]
                storages = proxmox.nodes(first_node).storage.get()
                for st in storages:
                    st_name = st.get("storage", "?")
                    st_type = st.get("type", "?")
                    st_used = st.get("used", 0)
                    st_total = st.get("total", 0)
                    st_active = st.get("active", 0)
                    icon = "[ON] " if st_active else "[OFF]"
                    if st_total > 0:
                        print(f"  {icon} {st_name:<20} ({st_type:<8})  {bar(st_used, st_total)}  {bytes_to_gb(st_used)}/{bytes_to_gb(st_total)} GB")
                    else:
                        print(f"  {icon} {st_name:<20} ({st_type:<8})  размер неизвестен")
            except Exception as e:
                print(f"  [ERR] {e}")

            print("\n" + "=" * 65)
            print(f"  Обновлено: {time.strftime('%H:%M:%S')}")
            print("=" * 65)

            time.sleep(5)

    except KeyboardInterrupt:
        print("\n\n  Мониторинг остановлен.")


# ─────────────────────────────────────────────
#  СНАПШОТЫ
# ─────────────────────────────────────────────

def snapshot_menu(proxmox):
    print("\n=== Снапшоты ===")
    print("Введите ID виртуальной машины или контейнера: ", end="")
    vmid_input = input().strip()

    if not vmid_input.isdigit():
        print("  [!] ID должен быть числом")
        return

    vmid = int(vmid_input)
    print(f"  Ищу VM/CT с ID {vmid}...")

    node, vm_type = find_vm_node(proxmox, vmid)
    if not node:
        print(f"  [!] VM/CT с ID {vmid} не найдена")
        return

    type_label = "VM" if vm_type == "qemu" else "CT"
    print(f"  Найдено: {type_label} {vmid} на {node}")

    while True:
        try:
            if vm_type == "qemu":
                snapshots = proxmox.nodes(node).qemu(vmid).snapshot.get()
            else:
                snapshots = proxmox.nodes(node).lxc(vmid).snapshot.get()
            real_snaps = [s for s in snapshots if s.get("name") != "current"]
        except Exception as e:
            print(f"  [ERR] Не удалось получить список снапшотов: {e}")
            return

        print(f"\n  Снапшоты {type_label} {vmid}:")
        if real_snaps:
            for i, s in enumerate(real_snaps, 1):
                name = s.get("name", "?")
                desc = s.get("description", "")
                print(f"    [{i}] {name}" + (f" | {desc}" if desc else ""))
        else:
            print("    Снапшотов пока нет")

        print()
        print("  [c] Создать новый снапшот")
        print("  [r] Откатиться к снапшоту")
        print("  [d] Удалить снапшот")
        print("  [0] Назад")
        print("Выбор: ", end="")
        action = input().strip().lower()

        if action == "0":
            return

        elif action == "c":
            print("Введите имя снапшота (без пробелов): ", end="")
            snap_name = input().strip().replace(" ", "_")
            print("Введите описание (или Enter чтобы пропустить): ", end="")
            snap_desc = input().strip()

            if not snap_name:
                print("  [!] Имя не может быть пустым")
                continue

            try:
                if vm_type == "qemu":
                    proxmox.nodes(node).qemu(vmid).snapshot.post(
                        snapname=snap_name,
                        description=snap_desc
                    )
                else:
                    proxmox.nodes(node).lxc(vmid).snapshot.post(
                        snapname=snap_name,
                        description=snap_desc
                    )
                print(f"  [OK] Снапшот '{snap_name}' создан!")

                time.sleep(2)
                if vm_type == "qemu":
                    updated = proxmox.nodes(node).qemu(vmid).snapshot.get()
                else:
                    updated = proxmox.nodes(node).lxc(vmid).snapshot.get()
                found = any(s.get("name") == snap_name for s in updated)
                if found:
                    print(f"  [OK] Снапшот подтверждён в Proxmox")
                else:
                    print(f"  [!] Снапшот не найден после создания — возможна ошибка")

            except Exception as e:
                print(f"  [ERR] Не удалось создать снапшот: {e}")

        elif action == "r":
            if not real_snaps:
                print("  [!] Нет снапшотов для отката")
                continue

            print("Введите номер снапшота для отката: ", end="")
            num = input().strip()
            if not num.isdigit() or not (1 <= int(num) <= len(real_snaps)):
                print("  [!] Неверный номер")
                continue

            snap = real_snaps[int(num) - 1]
            snap_name = snap["name"]

            print(f"  Откатываюсь к снапшоту '{snap_name}'...")
            try:
                if vm_type == "qemu":
                    proxmox.nodes(node).qemu(vmid).snapshot(snap_name).rollback.post()
                else:
                    proxmox.nodes(node).lxc(vmid).snapshot(snap_name).rollback.post()
                print(f"  [OK] Откат к '{snap_name}' выполнен!")

                print(f"  Запустить {type_label} {vmid} после отката? [y/n]: ", end="")
                start_choice = input().strip().lower()
                if start_choice == "y":
                    time.sleep(3)
                    try:
                        if vm_type == "qemu":
                            proxmox.nodes(node).qemu(vmid).status.start.post()
                        else:
                            proxmox.nodes(node).lxc(vmid).status.start.post()
                        print(f"  [OK] {type_label} {vmid} запущена!")
                    except Exception as e:
                        print(f"  [ERR] Не удалось запустить: {e}")

            except Exception as e:
                print(f"  [ERR] Не удалось откатиться: {e}")

        elif action == "d":
            if not real_snaps:
                print("  [!] Нет снапшотов для удаления")
                continue

            print("Введите номер снапшота для удаления: ", end="")
            num = input().strip()
            if not num.isdigit() or not (1 <= int(num) <= len(real_snaps)):
                print("  [!] Неверный номер")
                continue

            snap = real_snaps[int(num) - 1]
            snap_name = snap["name"]

            print(f"  Удалить снапшот '{snap_name}'? [y/n]: ", end="")
            confirm = input().strip().lower()
            if confirm != "y":
                print("  Отменено")
                continue

            try:
                if vm_type == "qemu":
                    proxmox.nodes(node).qemu(vmid).snapshot(snap_name).delete()
                else:
                    proxmox.nodes(node).lxc(vmid).snapshot(snap_name).delete()
                print(f"  [OK] Снапшот '{snap_name}' удалён!")
            except Exception as e:
                print(f"  [ERR] Не удалось удалить: {e}")

        else:
            print("  [!] Неверный выбор")


# ─────────────────────────────────────────────
#  СНАПШОТ ВСЕГО ПУЛА
# ─────────────────────────────────────────────

def snapshot_pool(proxmox, poolid):
    print(f"\n=== Снапшот пула {poolid} ===")

    members = get_pool_members(proxmox, poolid)
    vms = [m for m in members if m.get("type") == "qemu"]
    cts = [m for m in members if m.get("type") == "lxc"]

    if not vms and not cts:
        print(f"  [!] В пуле {poolid} нет VM или контейнеров")
        return

    # Показываем что будет снапшотиться
    total = len(vms) + len(cts)
    print(f"  Будет создан снапшот для {total} машин: {len(vms)} VM, {len(cts)} CT")
    print()

    print("Введите имя снапшота (без пробелов): ", end="")
    snap_name = input().strip().replace(" ", "_")
    if not snap_name:
        print("  [!] Имя не может быть пустым")
        return

    print("Введите описание (или Enter чтобы пропустить): ", end="")
    snap_desc = input().strip()

    print(f"\n  Создаю снапшот '{snap_name}' для всего пула {poolid}...")
    print()

    ok_count = 0
    err_count = 0

    for vm in vms:
        vmid = vm["vmid"]
        node = vm["node"]
        name = vm.get("name", "без имени")
        try:
            proxmox.nodes(node).qemu(vmid).snapshot.post(
                snapname=snap_name,
                description=snap_desc
            )
            print(f"  [OK] VM  {vmid} ({name}) — снапшот создан")
            ok_count += 1
        except Exception as e:
            print(f"  [ERR] VM  {vmid} ({name}): {e}")
            err_count += 1

    for ct in cts:
        vmid = ct["vmid"]
        node = ct["node"]
        name = ct.get("name", "без имени")
        try:
            proxmox.nodes(node).lxc(vmid).snapshot.post(
                snapname=snap_name,
                description=snap_desc
            )
            print(f"  [OK] CT  {vmid} ({name}) — снапшот создан")
            ok_count += 1
        except Exception as e:
            print(f"  [ERR] CT  {vmid} ({name}): {e}")
            err_count += 1

    print()
    print(f"  Готово: ✅ {ok_count} успешно, ❌ {err_count} ошибок")

    # Подтверждение — проверяем что снапшот появился хотя бы у первой машины
    if ok_count > 0:
        time.sleep(2)
        first = vms[0] if vms else cts[0]
        vmid = first["vmid"]
        node = first["node"]
        vm_type = first.get("type")
        try:
            if vm_type == "qemu":
                snaps = proxmox.nodes(node).qemu(vmid).snapshot.get()
            else:
                snaps = proxmox.nodes(node).lxc(vmid).snapshot.get()
            found = any(s.get("name") == snap_name for s in snaps)
            if found:
                print(f"  [OK] Снапшот '{snap_name}' подтверждён в Proxmox")
            else:
                print(f"  [!] Снапшот не найден при проверке — возможна задержка")
        except Exception:
            pass



def rollback_pool(proxmox, poolid):
    print(f"\n=== Откат пула {poolid} к снапшоту ===")

    members = get_pool_members(proxmox, poolid)
    vms = [m for m in members if m.get("type") == "qemu"]
    cts = [m for m in members if m.get("type") == "lxc"]

    if not vms and not cts:
        print(f"  [!] В пуле {poolid} нет VM или контейнеров")
        return

    # Собираем общие снапшоты — те что есть у ВСЕХ машин пула
    print("  Собираю список снапшотов пула...")
    snap_sets = []
    all_members = vms + cts

    for m in all_members:
        vmid = m["vmid"]
        node = m["node"]
        vm_type = m.get("type")
        try:
            if vm_type == "qemu":
                snaps = proxmox.nodes(node).qemu(vmid).snapshot.get()
            else:
                snaps = proxmox.nodes(node).lxc(vmid).snapshot.get()
            names = {s["name"] for s in snaps if s.get("name") != "current"}
            snap_sets.append(names)
        except Exception as e:
            print(f"  [ERR] Не удалось получить снапшоты VM {vmid}: {e}")
            return

    if not snap_sets:
        print("  [!] Не удалось получить снапшоты")
        return

    # Пересечение — только снапшоты присутствующие у всех машин
    common_snaps = sorted(snap_sets[0].intersection(*snap_sets[1:]))

    if not common_snaps:
        print("  [!] Нет общих снапшотов для всех машин пула")
        return

    print(f"\n  Общие снапшоты пула ({len(common_snaps)}):")
    for i, name in enumerate(common_snaps, 1):
        print(f"    [{i}] {name}")
    print()
    print("Введите номер снапшота для отката: ", end="")
    num = input().strip()

    if not num.isdigit() or not (1 <= int(num) <= len(common_snaps)):
        print("  [!] Неверный номер")
        return

    snap_name = common_snaps[int(num) - 1]
    total = len(all_members)

    print(f"\n  ⚠️  Откат {total} машин к снапшоту '{snap_name}'.")
    print(f"  Все изменения после снапшота будут потеряны!")
    print(f"  Подтвердить? [y/n]: ", end="")
    confirm = input().strip().lower()
    if confirm != "y":
        print("  Отменено")
        return

    # Останавливаем все запущенные машины перед откатом
    print(f"\n  Останавливаю запущенные машины...")
    for m in all_members:
        vmid = m["vmid"]
        node = m["node"]
        vm_type = m.get("type")
        name = m.get("name", "без имени")
        status = m.get("status", "")
        if status == "running":
            try:
                if vm_type == "qemu":
                    proxmox.nodes(node).qemu(vmid).status.stop.post()
                else:
                    proxmox.nodes(node).lxc(vmid).status.stop.post()
                print(f"  [OK] {vmid} ({name}) — остановлена")
            except Exception as e:
                print(f"  [ERR] {vmid} ({name}): {e}")

    # Небольшая пауза чтобы машины успели остановиться
    print("  Жду остановки машин (5 сек)...")
    time.sleep(5)

    # Откат
    print(f"\n  Откатываю к '{snap_name}'...")
    ok_count = 0
    err_count = 0

    for m in all_members:
        vmid = m["vmid"]
        node = m["node"]
        vm_type = m.get("type")
        name = m.get("name", "без имени")
        try:
            if vm_type == "qemu":
                proxmox.nodes(node).qemu(vmid).snapshot(snap_name).rollback.post()
            else:
                proxmox.nodes(node).lxc(vmid).snapshot(snap_name).rollback.post()
            print(f"  [OK] {'VM' if vm_type == 'qemu' else 'CT'}  {vmid} ({name}) — откат выполнен")
            ok_count += 1
        except Exception as e:
            print(f"  [ERR] {vmid} ({name}): {e}")
            err_count += 1

    print()
    print(f"  Готово: ✅ {ok_count} успешно, ❌ {err_count} ошибок")

    # Предложение запустить всё после отката
    if ok_count > 0:
        print(f"\n  Запустить все машины после отката? [y/n]: ", end="")
        start_choice = input().strip().lower()
        if start_choice == "y":
            print("  Жду завершения отката (5 сек)...")
            time.sleep(5)
            print("  Запускаю...")
            for m in all_members:
                vmid = m["vmid"]
                node = m["node"]
                vm_type = m.get("type")
                name = m.get("name", "без имени")
                try:
                    if vm_type == "qemu":
                        proxmox.nodes(node).qemu(vmid).status.start.post()
                    else:
                        proxmox.nodes(node).lxc(vmid).status.start.post()
                    print(f"  [OK] {'VM' if vm_type == 'qemu' else 'CT'}  {vmid} ({name}) — запущена")
                except Exception as e:
                    print(f"  [ERR] {vmid} ({name}): {e}")


# ─────────────────────────────────────────────
#  ОТКРЫТЬ НОДУ В БРАУЗЕРЕ (АВТОЛОГИН)
# ─────────────────────────────────────────────

def load_node_passwords(filepath):
    """Читает файл паролей нод. Формат: ID | hostname | IP/mask | password"""
    nodes = []
    if not os.path.exists(filepath):
        return nodes
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 4:
                continue
            node_id, hostname, ip_mask, password = parts[0], parts[1], parts[2], parts[3]
            ip = ip_mask.split("/")[0]  # убираем маску подсети
            nodes.append({
                "id": node_id,
                "hostname": hostname,
                "ip": ip,
                "password": password,
            })
    return nodes


def open_node_browser():
    print("\n=== Открыть ноду в браузере ===")

    nodes = load_node_passwords(PASSWORDS_FILE)
    if not nodes:
        print(f"  [!] Файл паролей не найден или пуст: {PASSWORDS_FILE}")
        return

    print(f"\n  {'ID':<6} {'Hostname':<22} {'IP':<18}")
    print("  " + "-" * 48)
    for i, n in enumerate(nodes, 1):
        print(f"  [{i:<3}] {n['id']:<6} {n['hostname']:<22} {n['ip']:<18}")
    print()
    print("Введите номер ноды (или 0 для выхода): ", end="")
    choice = input().strip()

    if choice == "0":
        return
    if not choice.isdigit() or not (1 <= int(choice) <= len(nodes)):
        print("  [!] Неверный номер")
        return

    node = nodes[int(choice) - 1]
    ip = node["ip"]
    hostname = node["hostname"]
    base_url = f"https://{ip}:8006"

    print(f"\n  Подключаюсь к {hostname} ({ip})...")

    # Получаем ticket через логин/пароль
    try:
        resp = requests.post(
            f"{base_url}/api2/json/access/ticket",
            data={
                "username": NODE_WEB_USER,
                "password": node["password"],
            },
            verify=env_bool("PROXMOX_VERIFY_SSL", default=True),
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        ticket = data.get("ticket", "")

        if not ticket:
            print("  [ERR] Не удалось получить ticket — проверьте пароль или доступность ноды")
            return

    except requests.exceptions.ConnectionError:
        print(f"  [ERR] Нода {ip} недоступна")
        return
    except Exception as e:
        print(f"  [ERR] Ошибка авторизации: {e}")
        return

    # Пробуем подключиться к уже открытому Chrome через remote debugging (порт 9222)
    # Для этого Chrome должен быть запущен с флагом --remote-debugging-port=9222
    # Если не получается — запускаем новый Chrome с этим флагом
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        options.add_argument("--ignore-certificate-errors")
        options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")

        try:
            # Пробуем подключиться к уже открытому Chrome
            driver = webdriver.Chrome(options=options)
        except Exception:
            # Chrome не запущен с remote debugging — запускаем сами
            import subprocess
            chrome_paths = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]
            chrome_exe = next((p for p in chrome_paths if os.path.exists(p)), None)
            if not chrome_exe:
                print("  [!] Chrome не найден по стандартному пути")
                print(f"      Укажи путь в переменной CHROME_PATH в начале скрипта")
                webbrowser.open(base_url)
                return

            # Запускаем Chrome с remote debugging в фоне
            subprocess.Popen([
                chrome_exe,
                "--remote-debugging-port=9222",
                "--ignore-certificate-errors",
            ])
            time.sleep(2)

            # Подключаемся к нему
            options2 = Options()
            options2.add_argument("--ignore-certificate-errors")
            options2.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
            driver = webdriver.Chrome(options=options2)

        # Открываем новую вкладку
        driver.execute_script("window.open('about:blank', '_blank');")
        driver.switch_to.window(driver.window_handles[-1])

        # Сначала заходим на страницу чтобы установить cookie для нужного домена
        driver.get(base_url)
        time.sleep(1)

        # Устанавливаем PVEAuthCookie — это и есть сессия Proxmox
        driver.add_cookie({
            "name": "PVEAuthCookie",
            "value": ticket,
            "domain": ip,
            "path": "/",
            "secure": True,
            "sameSite": "Lax",
        })

        # Перезагружаем страницу — теперь Proxmox видит cookie и пускает без логина
        driver.get(base_url)

        print(f"  [OK] Открыта новая вкладка — {hostname} ({ip})")

    except ImportError:
        print("  [!] Selenium не установлен (pip install selenium).")
        print("      Открываю без автологина — войдите вручную:")
        print(f"      Логин: {NODE_WEB_USER}  |  Пароль: {node['password']}")
        webbrowser.open(base_url)

    except Exception as e:
        print(f"  [ERR] Ошибка открытия браузера: {e}")
        print(f"      Попробуйте открыть вручную: {base_url}")


# ─────────────────────────────────────────────
#  РАЗВЁРТЫВАНИЕ VM ИЗ ШАБЛОНА
# ─────────────────────────────────────────────

def gen_password(length=8):
    """Генерирует пароль без визуально похожих символов: o O 0 i I l L"""
    excluded = set("oO0iIlL")
    chars = [c for c in (string.ascii_letters + string.digits) if c not in excluded]
    # Гарантируем хотя бы 1 цифру и 1 букву в пароле
    while True:
        pwd = [secrets.choice(chars) for _ in range(length)]
        s = "".join(pwd)
        if any(c.isdigit() for c in s) and any(c.isalpha() for c in s):
            return s


def get_templates(proxmox):
    """Возвращает все VM с флагом template=1 со всех нод"""
    templates = []
    try:
        nodes = proxmox.nodes.get()
        for node_info in nodes:
            nodename = node_info["node"]
            if node_info.get("status") != "online":
                continue
            try:
                vms = proxmox.nodes(nodename).qemu.get()
                for vm in vms:
                    if vm.get("template") == 1:
                        templates.append({
                            "vmid": vm["vmid"],
                            "name": vm.get("name", "без имени"),
                            "node": nodename,
                        })
            except Exception:
                pass
    except Exception as e:
        print(f"  [ERR] Не удалось получить список шаблонов: {e}")
    return templates


def get_next_vmid(proxmox):
    """Возвращает следующий свободный VMID через API Proxmox"""
    try:
        return proxmox.cluster.nextid.get()
    except Exception:
        return None


def get_nodes_list(proxmox):
    """Возвращает список онлайн-нод, отсортированных по нагрузке RAM (меньше = лучше)"""
    try:
        nodes = [n for n in proxmox.nodes.get() if n.get("status") == "online"]
        for n in nodes:
            mem_used = n.get("mem", 0)
            mem_total = n.get("maxmem", 1)
            n["mem_pct"] = round(mem_used / mem_total * 100, 1)
            n["cpu_pct"] = round(n.get("cpu", 0) * 100, 1)
        # Сортируем по RAM%
        return sorted(nodes, key=lambda x: x["mem_pct"])
    except Exception:
        return []


def deploy_from_template(proxmox):
    print("\n=== Развёртывание VM из шаблона ===")

    # ── 1. Название пула ──────────────────────────────────────────
    print("\nШаг 1/6: Название пула")
    existing_pools = [p["poolid"] for p in proxmox.pools.get()]
    print("  Существующие пулы: " + ", ".join(existing_pools))
    print("  Введите название нового пула (или выберите существующий): ", end="")
    poolid = input().strip()
    if not poolid:
        print("  [!] Название не может быть пустым")
        return

    # Создаём пул если не существует
    if poolid not in existing_pools:
        try:
            proxmox.pools.post(poolid=poolid)
            print(f"  [OK] Пул '{poolid}' создан")
        except Exception as e:
            print(f"  [ERR] Не удалось создать пул: {e}")
            return
    else:
        print(f"  [~] Пул '{poolid}' уже существует — будем добавлять в него")

    # ── 2. Шаблон ────────────────────────────────────────────────
    print("\nШаг 2/6: Выбор шаблона")
    templates = get_templates(proxmox)

    if templates:
        print(f"  {'#':<5} {'VMID':<8} {'Имя':<30} {'Нода'}")
        print("  " + "-" * 55)
        for i, t in enumerate(templates, 1):
            print(f"  [{i:<3}] {t['vmid']:<8} {t['name']:<30} {t['node']}")
        print(f"  [0  ] Ввести VMID шаблона вручную")
        print()
        print("Выберите шаблон: ", end="")
        t_choice = input().strip()

        if t_choice == "0":
            print("Введите VMID шаблона: ", end="")
            t_input = input().strip()
            if not t_input.isdigit():
                print("  [!] VMID должен быть числом")
                return
            tmpl_vmid = int(t_input)
            # Ищем ноду шаблона
            tmpl_node = None
            for t in templates:
                if t["vmid"] == tmpl_vmid:
                    tmpl_node = t["node"]
                    break
            if not tmpl_node:
                # Ищем вручную по всем нодам
                for node_info in proxmox.nodes.get():
                    nn = node_info["node"]
                    try:
                        vms = proxmox.nodes(nn).qemu.get()
                        if any(v["vmid"] == tmpl_vmid for v in vms):
                            tmpl_node = nn
                            break
                    except Exception:
                        pass
            if not tmpl_node:
                print(f"  [!] Шаблон с VMID {tmpl_vmid} не найден")
                return
        elif t_choice.isdigit() and 1 <= int(t_choice) <= len(templates):
            tmpl = templates[int(t_choice) - 1]
            tmpl_vmid = tmpl["vmid"]
            tmpl_node = tmpl["node"]
        else:
            print("  [!] Неверный выбор")
            return
    else:
        print("  Шаблонов не найдено. Введите VMID шаблона вручную: ", end="")
        t_input = input().strip()
        if not t_input.isdigit():
            print("  [!] VMID должен быть числом")
            return
        tmpl_vmid = int(t_input)
        tmpl_node = None
        for node_info in proxmox.nodes.get():
            nn = node_info["node"]
            try:
                vms = proxmox.nodes(nn).qemu.get()
                if any(v["vmid"] == tmpl_vmid for v in vms):
                    tmpl_node = nn
                    break
            except Exception:
                pass
        if not tmpl_node:
            print(f"  [!] Шаблон с VMID {tmpl_vmid} не найден")
            return

    print(f"  [OK] Шаблон: VMID {tmpl_vmid} на ноде {tmpl_node}")

    # ── 3. Количество VM ────────────────────────────────────────
    print("\nШаг 3/6: Количество VM")
    print("Сколько виртуальных машин создать? ", end="")
    count_input = input().strip()
    if not count_input.isdigit() or int(count_input) < 1:
        print("  [!] Введите число больше 0")
        return
    vm_count = int(count_input)

    # ── 4. Тип клонирования ─────────────────────────────────────
    print("\nШаг 4/6: Тип клонирования")
    print("  [1] Full clone (полная копия, независимая)")
    print("  [2] Linked clone (быстрее, зависит от шаблона)")
    print("Выбор [1/2]: ", end="")
    clone_type = input().strip()
    if clone_type == "2":
        full_clone = 0
        print("  [OK] Linked clone")
    else:
        full_clone = 1
        print("  [OK] Full clone")

    # ── 5. Нода для деплоя — автораспределение по нагрузке ──────
    print("\nШаг 5/6: Распределение по нодам")
    nodes_list = get_nodes_list(proxmox)
    if not nodes_list:
        print("  [!] Нет доступных нод")
        return

    print(f"  {'Нода':<20} {'CPU%':<8} {'RAM%':<8} {'Приоритет'}")
    print("  " + "-" * 48)
    for i, n in enumerate(nodes_list):
        priority = "⭐ наименее загружена" if i == 0 else ""
        print(f"  {n['node']:<20} {n['cpu_pct']:<8} {n['mem_pct']:<8} {priority}")

    print()
    print("  VM будут распределены по нодам по очереди (от наименее загруженной).")
    print("  Нажмите Enter для продолжения или введите имя конкретной ноды: ", end="")
    node_override = input().strip()

    if node_override:
        # Проверяем что такая нода существует
        node_names = [n["node"] for n in nodes_list]
        if node_override not in node_names:
            print(f"  [!] Нода '{node_override}' не найдена или офлайн")
            return
        # Деплоим всё на одну ноду
        nodes_cycle = [node_override] * vm_count
        print(f"  [OK] Все VM → {node_override}")
    else:
        # Round-robin по нодам, начиная с наименее загруженной
        node_names = [n["node"] for n in nodes_list]
        nodes_cycle = [node_names[i % len(node_names)] for i in range(vm_count)]
        print(f"  [OK] Автораспределение по {len(node_names)} нод(ам)")

    # Предупреждение: linked clone на другую ноду работает только при общем хранилище
    if full_clone == 0:
        unique_nodes = set(nodes_cycle)
        if len(unique_nodes) > 1 or (len(unique_nodes) == 1 and list(unique_nodes)[0] != tmpl_node):
            print(f"\n  ⚠️  Linked clone на другую ноду работает ТОЛЬКО если хранилище")
            print(f"     шаблона доступно со всех целевых нод (NFS, Ceph и т.п.).")
            print(f"     Если хранилище локальное — используйте Full clone.")

    # ── 6. Хранилище ─────────────────────────────────────────────
    # Для linked clone storage НЕ передаётся — Proxmox запрещает этот параметр.
    # Для full clone можно указать явно.
    target_storage = None
    if full_clone == 1:
        print("\nШаг 6/7: Хранилище для дисков VM")
        print("  Введите имя хранилища (например NAS1, local-lvm, local).")
        print("  Введите имя хранилища (или Enter — оставить из шаблона): ", end="")
        storage_input = input().strip()
        if storage_input:
            target_storage = storage_input
            print(f"  [OK] Хранилище: {target_storage}")
        else:
            print("  [OK] Хранилище из шаблона")

    # ── 7. Сеть ─────────────────────────────────────────────────
    print("\nШаг 7/7: Сетевой мост (bridge)")
    print("  Стандартные варианты: vmbr0, vmbr1, vmbr100 ...")
    print("  Введите имя bridge (или Enter — оставить из шаблона): ", end="")
    bridge_input = input().strip()
    if bridge_input:
        custom_bridge = bridge_input
        print(f"  [OK] Bridge: {custom_bridge}")
    else:
        custom_bridge = None
        print("  [OK] Сеть из шаблона")

    # ── Начальный IP ─────────────────────────────────────────────
    print("\nНачальный IP-адрес для первой VM (например 10.39.1.50)")
    print("  Введите IP (или Enter — пропустить назначение IP): ", end="")
    start_ip_input = input().strip()

    start_ip = None
    if start_ip_input:
        try:
            parts = start_ip_input.split(".")
            if len(parts) == 4:
                start_ip = [int(p) for p in parts]
            else:
                print("  [!] Неверный IP, пропускаю")
        except ValueError:
            print("  [!] Неверный IP, пропускаю")

    # ── Имя для VM ───────────────────────────────────────────────
    print(f"\nПрефикс имени VM (например '{poolid}'): ", end="")
    name_prefix = input().strip() or poolid

    # ── Итоговая сводка ──────────────────────────────────────────
    print()
    print("=" * 55)
    print(f"  Пул:        {poolid}")
    print(f"  Шаблон:     VMID {tmpl_vmid} ({tmpl_node})")
    print(f"  Количество: {vm_count} VM")
    print(f"  Тип:        {'Full' if full_clone else 'Linked'} clone")
    if full_clone == 1:
        print(f"  Хранилище: {target_storage or 'из шаблона'}")
    print(f"  Ноды:       {' → '.join(dict.fromkeys(nodes_cycle))}")
    print(f"  Bridge:     {custom_bridge or 'из шаблона'}")
    print(f"  IP с:       {start_ip_input or 'не назначать'}")
    print(f"  Имена:      {name_prefix}-1 ... {name_prefix}-{vm_count}")
    print("=" * 55)
    print("Начать развёртывание? [y/n]: ", end="")
    if input().strip().lower() != "y":
        print("  Отменено")
        return

    # ── Развёртывание ─────────────────────────────────────────────
    print()
    deployed = []
    ok_count = 0
    err_count = 0

    for idx in range(1, vm_count + 1):
        vm_name = f"{name_prefix}-{idx}"
        password = gen_password()
        target_node = nodes_cycle[idx - 1]

        # Вычисляем IP
        if start_ip:
            ip_parts = start_ip[:]
            ip_parts[3] += (idx - 1)
            for octet in range(3, 0, -1):
                if ip_parts[octet] > 254:
                    ip_parts[octet] -= 254
                    ip_parts[octet - 1] += 1
            vm_ip = ".".join(str(p) for p in ip_parts)
        else:
            vm_ip = None

        new_vmid = proxmox.get_nextid()
        if not new_vmid:
            print(f"  [ERR] {vm_name}: не удалось получить VMID")
            err_count += 1
            continue

        print(f"  [{idx}/{vm_count}] {vm_name} (VMID {new_vmid}) → {target_node}...", end=" ", flush=True)

        try:
            clone_params = {
                "newid": new_vmid,
                "name": vm_name,
                "full": full_clone,
                "target": target_node,
                "pool": poolid,
            }
            # storage передаём только для full clone — для linked clone Proxmox запрещает этот параметр
            if target_storage and full_clone == 1:
                clone_params["storage"] = target_storage
            # clone.post возвращает UPID задачи — ждём реального завершения
            upid = proxmox.nodes(tmpl_node).qemu(tmpl_vmid).clone.post(**clone_params)
            task_result = proxmox.wait_task(upid, tmpl_node)
            if task_result.get("exitstatus") != "OK":
                raise Exception(f"Клонирование завершилось с ошибкой: {task_result.get('exitstatus')}")
            print("клонировано...", end=" ", flush=True)

            # Настраиваем сеть если указан bridge
            if custom_bridge:
                try:
                    proxmox.nodes(target_node).qemu(new_vmid).config.put(
                        net0=f"virtio,bridge={custom_bridge}"
                    )
                except Exception as e:
                    print(f"\n  [!] Сеть не назначена: {e}", end=" ")

            # Включаем guest agent в конфиге VM
            try:
                proxmox.nodes(target_node).qemu(new_vmid).config.put(agent="1")
            except Exception:
                pass

            # Запускаем VM
            upid = proxmox.nodes(target_node).qemu(new_vmid).status.start.post()
            proxmox.wait_task(upid, target_node)
            print("запущена...", end=" ", flush=True)

            # Ждём guest agent (до 3 минут) с отображением прогресса
            print("жду агента", end="", flush=True)
            agent_ready = False
            node_info, _ = proxmox.get_node(new_vmid)
            nodename = node_info["node"] if node_info else target_node
            for tick in range(180):
                try:
                    proxmox.nodes(nodename).qemu(str(new_vmid)).agent("ping").post()
                    agent_ready = True
                    break
                except KeyboardInterrupt:
                    raise
                except Exception:
                    if tick % 10 == 9:
                        print(f"({tick+1}с)", end="", flush=True)
                    else:
                        print(".", end="", flush=True)
                    time.sleep(1)
            print(" ", end="", flush=True)

            if agent_ready:
                print("агент готов...", end=" ", flush=True)

                # Меняем IP если указан
                if vm_ip:
                    try:
                        changeip_script = (
                            "#!/bin/bash\n"
                            "set -e\n"
                            f"sed -i '/^iface vmbr0 inet static/,/^[^ \\t]/ s|^\\([ \\t]*address[ \\t]\\+\\).*|\\1{vm_ip}/16|' /etc/network/interfaces\n"
                            f"sed -i '/^iface vmbr0 inet static/,/^[^ \\t]/ s|^\\([ \\t]*gateway[ \\t]\\+\\).*|\\110.39.1.1|' /etc/network/interfaces\n"
                            "HOSTNAME=$(cat /etc/hostname | tr -d '\\n')\n"
                            "sed -i '/^127\\.0\\.1\\.1/d' /etc/hosts\n"
                            "echo \"127.0.1.1 $HOSTNAME\" >> /etc/hosts\n"
                            # Удаляем старую строку с реальным IP (любой не-127 и не-::1 адрес с hostname)
                            f"sed -i '/^[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+.*'\"$HOSTNAME\"'/d' /etc/hosts\n"
                            f"echo \"{vm_ip} $HOSTNAME.example.local $HOSTNAME\" >> /etc/hosts\n"
                            "ifdown vmbr0 2>/dev/null || true\n"
                            "ifup vmbr0 2>/dev/null || true\n"
                        )
                        proxmox.nodes(target_node).qemu(str(new_vmid)).agent("file-write").post(
                            file="/tmp/changeip.sh",
                            content=changeip_script
                        )
                        task = proxmox.nodes(target_node).qemu(str(new_vmid)).agent("exec").post(
                            command=["bash", "/tmp/changeip.sh"]
                        )
                        pid = task["pid"]
                        for _ in range(30):
                            time.sleep(1)
                            try:
                                result = proxmox.nodes(target_node).qemu(str(new_vmid)).agent("exec-status").get(pid=pid)
                                if result.get("exited"):
                                    break
                            except Exception:
                                break
                        print("IP...", end=" ", flush=True)
                    except Exception as e:
                        print(f"[!IP: {e}]...", end=" ", flush=True)

                # Меняем пароль
                try:
                    proxmox.nodes(target_node).qemu(str(new_vmid)).agent("set-user-password").post(
                        username="root",
                        password=password
                    )
                    print("пароль установлен...", end=" ", flush=True)
                    password_set = True
                except Exception as e:
                    print(f"\n  [!] Пароль не установлен: {e}", end=" ")
                    password_set = False
            else:
                print("\n  [!] Агент не ответил за 120 сек — пароль и IP не установлены...", end=" ")
                password_set = False

            # Останавливаем VM
            try:
                proxmox.nodes(target_node).qemu(new_vmid).status.shutdown.post()
                # Ждём остановки до 30 сек
                for _ in range(15):
                    time.sleep(2)
                    info = proxmox.nodes(target_node).qemu(new_vmid).status.current.get()
                    if info.get("status") == "stopped":
                        break
            except Exception:
                pass

            # Создаём снапшот 'start' ПОСЛЕ установки пароля (уже с правильным паролем)
            try:
                snaps = proxmox.nodes(target_node).qemu(new_vmid).snapshot.get()
                snap_names = {s["name"] for s in snaps}
                if "start" not in snap_names:
                    proxmox.nodes(target_node).qemu(new_vmid).snapshot.post(
                        snapname="start",
                        description="Начальное состояние после развёртывания"
                    )
                    print("снапшот 'start' создан...", end=" ", flush=True)
            except Exception as e:
                print(f"\n  [!] Снапшот не создан: {e}", end=" ")

            print("✅")
            ok_count += 1
            deployed.append({
                "vmid": new_vmid,
                "name": vm_name,
                "ip": vm_ip or "—",
                "password": password if password_set else "⚠ не установлен — см. шаблон",
            })

        except Exception as e:
            print(f"❌\n  [ERR] {vm_name}: {e}")
            err_count += 1

    # ── Сохраняем файл с лого/пасами ────────────────────────────
    print()
    print(f"  Готово: ✅ {ok_count} развёрнуто, ❌ {err_count} ошибок")

    if deployed:
        logopass_dir = r"C:\Users\Serega\Desktop\PRC\scripts\logopass"
        os.makedirs(logopass_dir, exist_ok=True)
        out_filename = os.path.join(logopass_dir, f"password-{poolid}.txt")
        try:
            with open(out_filename, "w", encoding="utf-8") as f:
                for entry in deployed:
                    line = f"{entry['vmid']} | {entry['name']} | {entry['ip']}:8006 | {entry['password']}\n"
                    f.write(line)
            print(f"\n  [OK] Файл с логинами/паролями сохранён: {out_filename}")
            print(f"       Логин для всех: root")
        except Exception as e:
            print(f"\n  [ERR] Не удалось сохранить файл: {e}")
            print("\n  Данные для входа:")
            for entry in deployed:
                print(f"    {entry['vmid']} | {entry['name']} | {entry['ip']}:8006 | {entry['password']}")



def unlock_vm(proxmox, vmid, node=None):
    """Снять лок с VM — использует метод класса"""
    return proxmox.unlock_vm(vmid)


# ─────────────────────────────────────────────
#  УДАЛЕНИЕ ВСЕГО ПУЛА
# ─────────────────────────────────────────────

def delete_pool(proxmox, poolid):
    print(f"\n=== Удаление пула {poolid} ===")

    members = get_pool_members(proxmox, poolid)
    vms = [m for m in members if m.get("type") == "qemu"]
    cts = [m for m in members if m.get("type") == "lxc"]
    all_members = vms + cts
    total = len(all_members)

    if total == 0:
        print(f"  Пул пуст — будет удалён только сам пул.")
    else:
        print(f"  Будет удалено: {total} машин ({len(vms)} VM, {len(cts)} CT) + сам пул.")
        print()
        print(f"  {'Тип':<5} {'VMID':<8} {'Имя':<25} {'Нода':<15} {'Статус'}")
        print("  " + "-" * 60)
        for m in all_members:
            mtype = "VM" if m.get("type") == "qemu" else "CT"
            status_icon = "🟢" if m.get("status") == "running" else "🔴"
            print(f"  {mtype:<5} {m['vmid']:<8} {m.get('name','—'):<25} {m['node']:<15} {status_icon} {m.get('status','?')}")

    print()
    print(f"  ⚠️  ВНИМАНИЕ: все данные будут уничтожены безвозвратно!")
    print(f"  Введите название пула для подтверждения ({poolid}): ", end="")
    confirm = input().strip()
    if confirm != poolid:
        print("  Отменено — название не совпадает")
        return

    if total == 0:
        try:
            proxmox.pools(poolid).delete()
            print(f"  [OK] Пул '{poolid}' удалён")
        except Exception as e:
            print(f"  [ERR] Пул не удалён: {e}")
        return

    # ── Шаг 1: fire-and-forget stop на все запущенные машины ──────────────────
    running = [m for m in all_members if m.get("status") == "running"]
    last_stop_upid = None
    last_stop_node = None
    if running:
        print(f"\n  Останавливаю {len(running)} запущенных машин...")
        for m in running:
            vmid = m["vmid"]
            node = m["node"]
            vm_type = m.get("type")
            name = m.get("name", "—")
            try:
                if vm_type == "qemu":
                    upid = proxmox.nodes(node).qemu(str(vmid)).status.stop.post()
                else:
                    upid = proxmox.nodes(node).lxc(str(vmid)).status.stop.post()
                last_stop_upid = upid
                last_stop_node = node
                print(f"  [>>] VM/CT {vmid} ({name}) — стоп отправлен")
            except Exception as e:
                print(f"  [!]  VM/CT {vmid} ({name}) — {e}")

        # Ждём только последнюю задачу остановки (как в рабочем скрипте)
        if last_stop_upid:
            print(f"  Жду завершения остановки...")
            try:
                data = {"status": ""}
                while data["status"] != "stopped":
                    data = proxmox.nodes(last_stop_node).tasks(last_stop_upid).status.get()
                    time.sleep(1)
            except Exception as e:
                print(f"  [!] Не дождались: {e}")
        time.sleep(5)  # буфер чтобы все VM точно остановились

    # ── Шаг 2: снять локи со всех VM ─────────────────────────────────────────
    for m in vms:
        vmid = m["vmid"]
        node = m["node"]
        try:
            proxmox.nodes(node).qemu(str(vmid)).config.put(**{"delete": "lock"})
        except Exception:
            pass  # лока не было — норма
    for m in cts:
        vmid = m["vmid"]
        node = m["node"]
        try:
            proxmox.nodes(node).lxc(str(vmid)).config.put(**{"delete": "lock"})
        except Exception:
            pass

    # ── Шаг 3: fire-and-forget delete на все машины ───────────────────────────
    print(f"\n  Удаляю {total} машин...")
    err_count = 0
    for m in all_members:
        vmid = m["vmid"]
        node = m["node"]
        vm_type = m.get("type")
        name = m.get("name", "—")
        try:
            if vm_type == "qemu":
                proxmox.nodes(node).qemu(str(vmid)).delete(
                    **{"destroy-unreferenced-disks": 1}
                )
            else:
                proxmox.nodes(node).lxc(str(vmid)).delete(
                    **{"destroy-unreferenced-disks": 1}
                )
            print(f"  [>>] VM/CT {vmid} ({name}) — задача удаления запущена")
        except Exception as e:
            print(f"  [ERR] VM/CT {vmid} ({name}): {e}")
            err_count += 1

    # ── Шаг 4: ждём пока пул опустеет (polling членов пула) ──────────────────
    print(f"\n  Жду завершения удаления (слежу за пулом)...")
    deadline = time.time() + 300  # максимум 5 минут
    while time.time() < deadline:
        try:
            remaining = get_pool_members(proxmox, poolid)
            count = len(remaining)
            if count == 0:
                break
            print(f"  ... осталось {count} машин")
            time.sleep(5)
        except Exception:
            time.sleep(3)

    # ── Шаг 5: удалить сам пул ───────────────────────────────────────────────
    print(f"\n  Удаляю пул {poolid}...")
    try:
        proxmox.pools(poolid).delete()
        print(f"  [OK] Пул '{poolid}' удалён")
    except Exception as e:
        print(f"  [ERR] Пул не удалён: {e}")
        if err_count > 0:
            print(f"        {err_count} машин не запустили удаление — пул не пуст")
        else:
            print(f"        Возможно не все VM успели удалиться, попробуйте ещё раз")

    ok_count = total - err_count
    print()
    print(f"  Итого: ✅ {ok_count} запущено удаление, ❌ {err_count} ошибок запуска")


# ─────────────────────────────────────────────
#  ПРЕДНАСТРОЙКА ПУЛА (IP + ПАРОЛЬ + ПЕРЕЗАГРУЗКА)
# ─────────────────────────────────────────────

def preset_pool(proxmox, poolid):
    print(f"\n=== Преднастройка пула {poolid} ===")
    print("  Функция меняет IP и пароль root внутри каждой VM через guest agent,")
    print("  затем перезагружает машины. Все VM должны быть запущены и иметь агента.\n")

    members = proxmox.get_vms_in_pool(poolid)
    vms = [m for m in members if m.get("type") == "qemu"]

    if not vms:
        print("  [!] В пуле нет VM (только CT не поддерживаются в preset)")
        return

    print(f"  VM в пуле: {len(vms)}")
    print()

    # Подсеть
    print("Введите подсеть (например 10.39.1.): ", end="")
    subnet = input().strip()
    if not subnet:
        print("  [!] Подсеть не может быть пустой")
        return

    print("Введите маску (например 16): ", end="")
    mask = input().strip()
    if not mask.isdigit():
        print("  [!] Маска должна быть числом")
        return

    print("Введите шлюз (например 10.39.0.1): ", end="")
    gateway = input().strip()
    if not gateway:
        print("  [!] Шлюз не может быть пустым")
        return

    print("Введите начальный IP (последний октет, например 50): ", end="")
    start_oct = input().strip()
    if not start_oct.isdigit():
        print("  [!] Должно быть число")
        return

    print("Генерировать новые пароли? [y/n]: ", end="")
    gen_pass = input().strip().lower() == "y"

    print()
    print("=" * 55)
    print(f"  Подсеть:  {subnet}X/{mask}")
    print(f"  Шлюз:     {gateway}")
    print(f"  IP с:     {subnet}{start_oct}")
    print(f"  Пароли:   {'новые (генерация)' if gen_pass else 'не менять'}")
    print("=" * 55)
    print("Начать преднастройку? [y/n]: ", end="")
    if input().strip().lower() != "y":
        print("  Отменено")
        return

    print()
    results = []
    ip_counter = int(start_oct)

    # Сортируем VM по имени чтобы IP назначались детерминированно
    vms_sorted = sorted(vms, key=lambda m: m.get("name", ""))

    for vm in vms_sorted:
        vmid = vm["vmid"]
        name = vm.get("name", "—")
        vmip = f"{subnet}{ip_counter}/{mask}"
        password = gen_password() if gen_pass else "—"

        print(f"  [{ip_counter - int(start_oct) + 1}/{len(vms_sorted)}] {name} (VMID {vmid}) → {vmip}...", end=" ", flush=True)

        # Проверяем что агент доступен
        node, vm_type = proxmox.get_node(vmid)
        if not node:
            print("❌ не найдена")
            ip_counter += 1
            continue

        nodename = node["node"]

        # Проверяем агента
        agent_ok = False
        try:
            proxmox.nodes(nodename).qemu(str(vmid)).agent("ping").post()
            agent_ok = True
        except Exception:
            pass

        if not agent_ok:
            print("❌ агент недоступен")
            ip_counter += 1
            continue

        # Меняем IP напрямую через guest agent — без внешнего changeip скрипта
        changeip_script = (
            "#!/bin/bash\n"
            "set -e\n"
            # Меняем address в блоке iface vmbr0
            f"sed -i '/^iface vmbr0 inet static/,/^[^ \t]/ s|^\\([ \\t]*address[ \\t]\\+\\).*|\\1{vmip}|' /etc/network/interfaces\n"
            # Меняем gateway в блоке iface vmbr0
            f"sed -i '/^iface vmbr0 inet static/,/^[^ \t]/ s|^\\([ \\t]*gateway[ \\t]\\+\\).*|\\1{gateway}|' /etc/network/interfaces\n"
            # Обновляем /etc/hosts
            "HOSTNAME=$(cat /etc/hostname | tr -d '\\n')\n"
            "sed -i '/^127\\.0\\.1\\.1/d' /etc/hosts\n"
            "echo \"127.0.1.1 $HOSTNAME\" >> /etc/hosts\n"
            # Удаляем старую строку с реальным IP и добавляем новую
            f"sed -i '/^[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+.*'\"$HOSTNAME\"'/d' /etc/hosts\n"
            f"NEWIP=$(echo '{vmip}' | cut -d'/' -f1)\n"
            "echo \"$NEWIP $HOSTNAME.example.local $HOSTNAME\" >> /etc/hosts\n"
            # Применяем сетевые настройки
            "ifdown vmbr0 2>/dev/null || true\n"
            "ifup vmbr0 2>/dev/null || true\n"
        )
        try:
            proxmox.nodes(nodename).qemu(str(vmid)).agent("file-write").post(
                file="/tmp/changeip.sh",
                content=changeip_script
            )
            task = proxmox.nodes(nodename).qemu(str(vmid)).agent("exec").post(
                command=["bash", "/tmp/changeip.sh"]
            )
            pid = task["pid"]
            for _ in range(30):
                time.sleep(1)
                try:
                    result = proxmox.nodes(nodename).qemu(str(vmid)).agent("exec-status").get(pid=pid)
                    if result.get("exited"):
                        break
                except Exception:
                    break
            print("IP...", end=" ", flush=True)
        except Exception as e:
            print(f"[!IP: {e}]...", end=" ", flush=True)

        # Меняем пароль
        if gen_pass:
            try:
                proxmox.nodes(nodename).qemu(str(vmid)).agent("set-user-password").post(
                    username="root", password=password
                )
                print("пароль...", end=" ", flush=True)
            except Exception as e:
                print(f"[!pass: {e}]...", end=" ", flush=True)
                password = "⚠ не установлен"

        # Перезагружаем через Proxmox status API (надёжнее чем через exec)
        try:
            proxmox.nodes(nodename).qemu(str(vmid)).status.reboot.post()
            print("✅")
        except Exception as e:
            print(f"[!reboot: {e}] ✅")

        results.append({
            "vmid": vmid,
            "name": name,
            "ip": vmip,
            "password": password,
        })
        ip_counter += 1

    # Сохраняем файл
    if results:
        logopass_dir = r"C:\Users\Serega\Desktop\PRC\scripts\logopass"
        os.makedirs(logopass_dir, exist_ok=True)
        out_filename = os.path.join(logopass_dir, f"password-{poolid}.txt")
        try:
            with open(out_filename, "w", encoding="utf-8") as f:
                for r in results:
                    ip_clean = r["ip"].replace(f"/{mask}", f":{8006}")
                    f.write(f"{r['vmid']} | {r['name']} | {ip_clean} | {r['password']}\n")
            print(f"\n  [OK] Файл обновлён: {out_filename}")
        except Exception as e:
            print(f"\n  [ERR] Файл не сохранён: {e}")
        print()
        print(f"  {'VMID':<8} {'Имя':<25} {'IP':<22} {'Пароль'}")
        print("  " + "-" * 65)
        for r in results:
            print(f"  {r['vmid']:<8} {r['name']:<25} {r['ip']:<22} {r['password']}")


# ─────────────────────────────────────────────
#  УДАЛЕНИЕ ВСЕХ СНАПШОТОВ ПУЛА
# ─────────────────────────────────────────────

def cleanup_snapshots_pool(proxmox, poolid):
    print(f"\n=== Удаление всех снапшотов пула {poolid} ===")

    members = proxmox.get_vms_in_pool(poolid)
    vms = [m for m in members if m.get("type") == "qemu"]
    cts = [m for m in members if m.get("type") == "lxc"]

    if not vms and not cts:
        print("  [!] Пул пуст")
        return

    # Собираем сколько снапшотов будет удалено
    total_snaps = 0
    for m in vms + cts:
        vmid = m["vmid"]
        node, vm_type = proxmox.get_node(vmid)
        if not node:
            continue
        nodename = node["node"]
        try:
            if vm_type == "qemu":
                snaps = proxmox.nodes(nodename).qemu(str(vmid)).snapshot.get()
            else:
                snaps = proxmox.nodes(nodename).lxc(str(vmid)).snapshot.get()
            total_snaps += sum(1 for s in snaps if s.get("name") != "current")
        except Exception:
            pass

    if total_snaps == 0:
        print("  Снапшотов нет — нечего удалять")
        return

    print(f"  Будет удалено: {total_snaps} снапшотов из {len(vms + cts)} машин")
    print(f"  ⚠️  Это действие необратимо!")
    print("  Подтвердить? [y/n]: ", end="")
    if input().strip().lower() != "y":
        print("  Отменено")
        return

    print()
    ok_count = 0
    err_count = 0

    for m in vms + cts:
        vmid = m["vmid"]
        name = m.get("name", "—")
        node, vm_type = proxmox.get_node(vmid)
        if not node:
            continue
        nodename = node["node"]
        try:
            if vm_type == "qemu":
                snaps = proxmox.nodes(nodename).qemu(str(vmid)).snapshot.get()
            else:
                snaps = proxmox.nodes(nodename).lxc(str(vmid)).snapshot.get()

            snap_names = [s["name"] for s in snaps if s.get("name") != "current"]
            for snap_name in snap_names:
                try:
                    if vm_type == "qemu":
                        proxmox.nodes(nodename).qemu(str(vmid)).snapshot(snap_name).delete()
                    else:
                        proxmox.nodes(nodename).lxc(str(vmid)).snapshot(snap_name).delete()
                    print(f"  [OK] {name} ({vmid}) — снапшот '{snap_name}' удалён")
                    ok_count += 1
                except Exception as e:
                    print(f"  [ERR] {name} ({vmid}) — '{snap_name}': {e}")
                    err_count += 1
        except Exception as e:
            print(f"  [ERR] {name} ({vmid}): {e}")
            err_count += 1

    print()
    print(f"  Итого: ✅ {ok_count} удалено, ❌ {err_count} ошибок")


# ─────────────────────────────────────────────
#  ГЛАВНОЕ МЕНЮ
# ─────────────────────────────────────────────

def main():
    print("Подключение к Proxmox...")
    try:
        proxmox = connect()
        print("Подключено!\n")
    except Exception as e:
        print(f"Ошибка подключения: {e}")
        input("Нажмите Enter для выхода...")
        return

    while True:
        pools = get_pools(proxmox)
        show_pools(pools)

        print("  [s]  Снапшоты VM/CT")
        print("  [v]  Включить / выключить отдельную VM/CT")
        print("  [m]  Мониторинг ресурсов в реальном времени")
        print("  [o]  Открыть ноду в браузере (автологин)")
        print("  [d]  Развернуть VM из шаблона")
        print("  [q]  Выход")
        print()
        print("Введите номер пула или команду: ", end="")
        choice = input().strip()

        if choice.lower() == "q":
            print("Выход.")
            break

        elif choice.lower() == "s":
            snapshot_menu(proxmox)
            continue

        elif choice.lower() == "v":
            vm_power_menu(proxmox)
            continue

        elif choice.lower() == "m":
            resources_monitor(proxmox)
            continue

        elif choice.lower() == "o":
            open_node_browser()
            continue

        elif choice.lower() == "d":
            deploy_from_template(proxmox)
            continue

        if not choice.isdigit() or not (1 <= int(choice) <= len(pools)):
            print("  [!] Неверный номер, попробуй ещё раз")
            continue

        pool = pools[int(choice) - 1]
        poolid = pool["poolid"]

        print(f"\nПул: {poolid}")
        print("  [1] Показать VM и контейнеры в пуле")
        print("  [2] Запустить все VM/CT в пуле")
        print("  [3] Остановить все VM/CT в пуле")
        print("  [4] Выдать доступ пользователям к пулу")
        print("  [5] Забрать доступ пользователей к пулу")
        print("  [6] Создать снапшот всего пула")
        print("  [7] Откатить весь пул к снапшоту")
        print("  [8] ⚠️  Удалить весь пул")
        print("  [9] Преднастройка пула (IP + пароль + перезагрузка)")
        print("  [10] Удалить все снапшоты пула")
        print("  [0] Назад")
        print("Выбор: ", end="")

        action = input().strip()

        if action == "1":
            show_pool_members(proxmox, poolid)
        elif action == "2":
            print(f"\nЗапускаю всё в пуле {poolid}...")
            start_pool(proxmox, poolid)
        elif action == "3":
            print(f"\nОстанавливаю всё в пуле {poolid}...")
            stop_pool(proxmox, poolid)
        elif action == "4":
            print(f"\nВыдаю доступ к пулу {poolid}...")
            grant_pool_access(proxmox, poolid)
        elif action == "5":
            print(f"\nЗабираю доступ к пулу {poolid}...")
            revoke_pool_access(proxmox, poolid)
        elif action == "6":
            snapshot_pool(proxmox, poolid)
        elif action == "7":
            rollback_pool(proxmox, poolid)
        elif action == "8":
            delete_pool(proxmox, poolid)
        elif action == "9":
            preset_pool(proxmox, poolid)
        elif action == "10":
            cleanup_snapshots_pool(proxmox, poolid)
        elif action == "0":
            continue
        else:
            print("  [!] Неверный выбор")

        print()


if __name__ == "__main__":
    main()
