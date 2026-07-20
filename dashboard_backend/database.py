from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iso_ago(**kwargs: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).replace(microsecond=0).isoformat()


class DashboardStore:
    """Small SQLite repository used by both demo and live Proxmox modes."""

    def __init__(self, db_path: Path, *, seed_demo: bool = True):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._create_schema()
        if seed_demo:
            self._seed_if_empty()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open a process-safe write transaction for multi-statement operations."""
        with self._lock, self.connect() as connection:
            # A deferred transaction permits two request threads to both read the
            # same state before either writes.  Reserving the write lock up front
            # keeps capacity checks and job creation atomic.
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def _create_schema(self) -> None:
        schema = """
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS blueprints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'Общий',
            version TEXT NOT NULL DEFAULT '1.0',
            status TEXT NOT NULL DEFAULT 'active',
            vm_count INTEGER NOT NULL DEFAULT 1,
            template_vmid INTEGER NOT NULL DEFAULT 0,
            clone_type TEXT NOT NULL DEFAULT 'linked',
            storage TEXT NOT NULL DEFAULT '',
            bridge TEXT NOT NULL DEFAULT 'vmbr0',
            subnet TEXT NOT NULL DEFAULT '',
            estimated_minutes INTEGER NOT NULL DEFAULT 8,
            tags TEXT NOT NULL DEFAULT '[]',
            deploy_script TEXT NOT NULL DEFAULT '',
            autocheck_script TEXT NOT NULL DEFAULT '',
            checks_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            blueprint_id INTEGER,
            status TEXT NOT NULL DEFAULT 'stopped',
            progress INTEGER NOT NULL DEFAULT 0,
            node TEXT NOT NULL DEFAULT '',
            pool_id TEXT NOT NULL DEFAULT '',
            owner TEXT NOT NULL DEFAULT '',
            participants INTEGER NOT NULL DEFAULT 0,
            max_participants INTEGER NOT NULL DEFAULT 12,
            vm_count INTEGER NOT NULL DEFAULT 1,
            cpu REAL NOT NULL DEFAULT 0,
            ram REAL NOT NULL DEFAULT 0,
            disk REAL NOT NULL DEFAULT 0,
            ip_range TEXT NOT NULL DEFAULT '',
            check_score INTEGER,
            check_status TEXT NOT NULL DEFAULT 'idle',
            last_check TEXT,
            expires_at TEXT,
            password_updated_at TEXT,
            origin TEXT NOT NULL DEFAULT 'deployed',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (blueprint_id) REFERENCES blueprints(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS stand_vms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stand_id INTEGER NOT NULL,
            vmid INTEGER,
            name TEXT NOT NULL,
            node TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'stopped',
            cpu REAL NOT NULL DEFAULT 0,
            ram REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (stand_id) REFERENCES stands(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stand_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            login TEXT NOT NULL,
            ip TEXT NOT NULL DEFAULT '',
            device TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'Участник',
            status TEXT NOT NULL DEFAULT 'active',
            started_at TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            ended_at TEXT,
            FOREIGN KEY (stand_id) REFERENCES stands(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS check_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stand_id INTEGER NOT NULL,
            blueprint_id INTEGER,
            status TEXT NOT NULL,
            score INTEGER,
            passed INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            details TEXT NOT NULL DEFAULT '[]',
            output TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT,
            FOREIGN KEY (stand_id) REFERENCES stands(id) ON DELETE CASCADE,
            FOREIGN KEY (blueprint_id) REFERENCES blueprints(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT 'Система',
            status TEXT NOT NULL DEFAULT 'info',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_stand_status ON sessions(stand_id, status);
        CREATE INDEX IF NOT EXISTS idx_checks_stand_started ON check_runs(stand_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_activity_created ON activity(created_at DESC);
        """
        with self.connect() as connection:
            connection.executescript(schema)
            stand_columns = {row[1] for row in connection.execute("PRAGMA table_info(stands)")}
            if "origin" not in stand_columns:
                connection.execute("ALTER TABLE stands ADD COLUMN origin TEXT NOT NULL DEFAULT 'deployed'")

    def _seed_if_empty(self) -> None:
        with self.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM blueprints").fetchone()[0]
            if count:
                return
            now = utc_now()
            blueprints = [
                (
                    "NET-01", "Сетевое администрирование", "Маршрутизация, VLAN, DNS и отказоустойчивая сеть для модуля А.",
                    "Сети", "2.4", "active", 4, 9001, "full", "ceph-fast", "vmbr100", "10.39.10.0/24", 9,
                    json.dumps(["Debian 12", "FRR", "DNS"], ensure_ascii=False),
                    """#!/usr/bin/env bash
set -euo pipefail

# Базовая подготовка сетевого стенда
hostnamectl set-hostname \"${STAND_NAME}-${VM_INDEX}\"
apt-get update -qq
apt-get install -y frr bind9 dnsutils
systemctl enable --now frr

echo \"Стенд ${STAND_NAME}: узел ${VM_INDEX} готов\"""",
                    """#!/usr/bin/env bash
set -euo pipefail

check() { printf '{\"name\":\"%s\",\"ok\":%s}\n' \"$1\" \"$2\"; }

systemctl is-active --quiet frr && check \"Служба FRR\" true || check \"Служба FRR\" false
ip route | grep -q \"10.39.\" && check \"Маршруты модуля\" true || check \"Маршруты модуля\" false
dig +short exam.demo.local | grep -q . && check \"DNS-зона\" true || check \"DNS-зона\" false""",
                    3, now, now,
                ),
                (
                    "SYS-02", "Системное администрирование", "Доменные службы, файловое хранилище и резервное копирование.",
                    "Системы", "3.1", "active", 3, 9010, "linked", "ceph-fast", "vmbr120", "10.39.20.0/24", 6,
                    json.dumps(["Windows Server", "AD DS", "GPO"], ensure_ascii=False),
                    """# PowerShell bootstrap
$ErrorActionPreference = 'Stop'
Rename-Computer -NewName \"$env:STAND_NAME-$env:VM_INDEX\" -Force
Install-WindowsFeature AD-Domain-Services, GPMC -IncludeManagementTools
Write-Output \"Bootstrap complete\"""",
                    """# PowerShell autocheck
$checks = @()
$adds = Get-Service NTDS -ErrorAction SilentlyContinue
$checks += @{ name = 'Служба AD DS'; ok = ($adds.Status -eq 'Running') }
$dns = Resolve-DnsName 'exam.demo.local' -ErrorAction SilentlyContinue
$checks += @{ name = 'Внутренняя DNS-зона'; ok = ($null -ne $dns) }
$checks | ConvertTo-Json -Compress""",
                    4, now, now,
                ),
                (
                    "SEC-01", "Информационная безопасность", "Сегмент с SIEM, межсетевым экраном и защищённым Linux-хостом.",
                    "Безопасность", "1.8", "active", 5, 9020, "full", "local-lvm", "vmbr140", "10.39.30.0/24", 13,
                    json.dumps(["OPNsense", "Wazuh", "Linux"], ensure_ascii=False),
                    """#!/usr/bin/env bash
set -euo pipefail
apt-get update -qq
apt-get install -y auditd nftables
systemctl enable --now auditd nftables
install -m 600 /dev/null /var/log/demoexam-bootstrap.log
date -Is >> /var/log/demoexam-bootstrap.log""",
                    """#!/usr/bin/env bash
set -euo pipefail
results=()
systemctl is-active --quiet auditd && results+=(\"auditd:ok\") || results+=(\"auditd:fail\")
nft list ruleset | grep -q 'table inet filter' && results+=(\"firewall:ok\") || results+=(\"firewall:fail\")
ss -lnt | grep -q ':1514' && results+=(\"siem:ok\") || results+=(\"siem:fail\")
printf '%s\\n' \"${results[@]}\"""",
                    3, now, now,
                ),
                (
                    "DB-01", "Администрирование баз данных", "Кластер PostgreSQL с репликацией и резервным копированием.",
                    "Базы данных", "0.9", "draft", 3, 9030, "linked", "ceph-fast", "vmbr160", "10.39.40.0/24", 7,
                    json.dumps(["PostgreSQL", "Patroni", "Backup"], ensure_ascii=False),
                    "#!/usr/bin/env bash\nset -euo pipefail\necho 'Черновик сценария развёртывания'",
                    "#!/usr/bin/env bash\nset -euo pipefail\npg_isready",
                    1, now, now,
                ),
            ]
            connection.executemany(
                """INSERT INTO blueprints
                (code, name, description, category, version, status, vm_count, template_vmid, clone_type,
                 storage, bridge, subnet, estimated_minutes, tags, deploy_script, autocheck_script,
                 checks_count, updated_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                blueprints,
            )

            expires = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=20)).replace(microsecond=0).isoformat()
            stands = [
                ("ДЭ-24 · Группа 2-ИС", 1, "running", 100, "pve-02", "de24-g2is", "А. Орлова", 8, 12, 4, 36.2, 42.5, 28.0, "10.39.10.40–59", 94, "passed", iso_ago(minutes=18), expires, iso_ago(days=8), iso_ago(hours=3), now),
                ("Тренировка · 3-СА", 2, "running", 100, "pve-01", "practice-3sa", "М. Соколов", 6, 10, 3, 24.8, 31.2, 19.0, "10.39.20.60–79", 86, "warning", iso_ago(minutes=42), expires, iso_ago(days=2), iso_ago(hours=2), now),
                ("ДЭ-24 · Резерв", 3, "stopped", 100, "pve-03", "de24-reserve", "А. Орлова", 0, 8, 5, 0.0, 3.4, 22.0, "10.39.30.80–99", 100, "passed", iso_ago(days=1), expires, iso_ago(days=12), iso_ago(days=1), now),
                ("Подготовка · 1-КБ", 3, "provisioning", 72, "pve-04", "prep-1kb", "И. Волков", 0, 12, 5, 12.4, 18.7, 9.0, "10.39.30.100–119", None, "idle", None, expires, None, iso_ago(minutes=6), now),
            ]
            connection.executemany(
                """INSERT INTO stands
                (name, blueprint_id, status, progress, node, pool_id, owner, participants, max_participants,
                 vm_count, cpu, ram, disk, ip_range, check_score, check_status, last_check, expires_at,
                 password_updated_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                stands,
            )

            vm_rows: list[tuple[Any, ...]] = []
            for stand_id, count, node, base_ip, state in [
                (1, 4, "pve-02", 40, "running"), (2, 3, "pve-01", 60, "running"),
                (3, 5, "pve-03", 80, "stopped"), (4, 4, "pve-04", 100, "running"),
            ]:
                for index in range(1, count + 1):
                    vm_rows.append((stand_id, 1100 + stand_id * 10 + index, f"stand-{stand_id}-{index}", node, f"10.39.{10 * stand_id}.{base_ip + index - 1}", state, 4.1 + index, 2.0 + index / 3))
            connection.executemany(
                "INSERT INTO stand_vms (stand_id, vmid, name, node, ip, status, cpu, ram) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                vm_rows,
            )

            people = [
                (1, "Анна Лукина", "a.lukina", "10.50.1.24", "Chrome · Windows", "Участник", "active", iso_ago(hours=2, minutes=14), iso_ago(seconds=18), None),
                (1, "Максим Чернов", "m.chernov", "10.50.1.31", "Edge · Windows", "Участник", "active", iso_ago(hours=1, minutes=48), iso_ago(seconds=42), None),
                (1, "Олег Нестеров", "o.nesterov", "10.50.1.44", "Firefox · Linux", "Участник", "idle", iso_ago(hours=1, minutes=36), iso_ago(minutes=7), None),
                (1, "Елена Барышева", "e.barysheva", "10.50.1.52", "Chrome · Windows", "Участник", "active", iso_ago(hours=1, minutes=22), iso_ago(seconds=12), None),
                (1, "Влад Ким", "v.kim", "10.50.1.58", "Chrome · macOS", "Участник", "active", iso_ago(minutes=59), iso_ago(seconds=29), None),
                (2, "Иван Борисов", "i.borisov", "10.50.2.11", "Edge · Windows", "Участник", "active", iso_ago(hours=2, minutes=3), iso_ago(seconds=9), None),
                (2, "Софья Мельник", "s.melnik", "10.50.2.19", "Chrome · Windows", "Участник", "active", iso_ago(hours=1, minutes=45), iso_ago(seconds=56), None),
                (2, "Артём Жуков", "a.zhukov", "10.50.2.27", "Firefox · Linux", "Эксперт", "idle", iso_ago(minutes=48), iso_ago(minutes=5), None),
                (2, "Дарья Морозова", "d.morozova", "10.50.2.34", "Chrome · Windows", "Участник", "active", iso_ago(minutes=37), iso_ago(seconds=21), None),
                (2, "Никита Орлов", "n.orlov", "10.50.2.41", "Edge · Windows", "Участник", "active", iso_ago(minutes=26), iso_ago(seconds=33), None),
            ]
            connection.executemany(
                """INSERT INTO sessions
                (stand_id, user_name, login, ip, device, role, status, started_at, last_seen, ended_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", people,
            )

            check_details = json.dumps([
                {"name": "Доступность узлов", "ok": True, "duration": 320},
                {"name": "Маршрутизация и VLAN", "ok": True, "duration": 680},
                {"name": "DNS-зона exam.demo.local", "ok": True, "duration": 410},
                {"name": "Резервный маршрут", "ok": False, "duration": 190},
            ], ensure_ascii=False)
            connection.executemany(
                """INSERT INTO check_runs
                (stand_id, blueprint_id, status, score, passed, total, duration_ms, details, output, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (1, 1, "passed", 94, 7, 8, 3140, check_details, "7/8 проверок пройдено", iso_ago(minutes=18), iso_ago(minutes=18)),
                    (2, 2, "warning", 86, 6, 7, 4260, check_details, "6/7 проверок пройдено", iso_ago(minutes=42), iso_ago(minutes=42)),
                    (3, 3, "passed", 100, 9, 9, 5210, check_details, "9/9 проверок пройдено", iso_ago(days=1), iso_ago(days=1)),
                ],
            )

            activity = [
                ("deploy", "Развёртывание стенда", "Подготовка · 1-КБ: клонирование VM 4 из 5", "Система", "progress", iso_ago(minutes=2)),
                ("check", "Автопроверка завершена", "ДЭ-24 · Группа 2-ИС — результат 94%", "А. Орлова", "success", iso_ago(minutes=18)),
                ("session", "Новая сессия", "Никита Орлов вошёл в Тренировка · 3-СА", "Система", "info", iso_ago(minutes=26)),
                ("password", "Пароль стенда обновлён", "Ротация учётных данных ДЭ-24 · Группа 2-ИС", "А. Орлова", "success", iso_ago(days=1)),
                ("snapshot", "Создан снимок", "Контрольная точка before-exam для резервного стенда", "И. Волков", "info", iso_ago(days=1, hours=2)),
            ]
            connection.executemany(
                "INSERT INTO activity (kind, title, detail, actor, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                activity,
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for key in ("tags", "details"):
            if key in item and isinstance(item[key], str):
                try:
                    item[key] = json.loads(item[key])
                except json.JSONDecodeError:
                    item[key] = []
        return item

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [self._row(row) for row in connection.execute(sql, params).fetchall()]  # type: ignore[misc]

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._row(connection.execute(sql, params).fetchone())

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock, self.connect() as connection:
            cursor = connection.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def add_activity(self, kind: str, title: str, detail: str, status: str = "info", actor: str = "Администратор") -> int:
        return self.execute(
            "INSERT INTO activity (kind, title, detail, actor, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (kind, title, detail, actor, status, utc_now()),
        )
