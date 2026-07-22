from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from dashboard_backend.database import DashboardStore
from dashboard_backend.proxmox_gateway import LiveProxmoxGateway
from dashboard_backend.service import DashboardService


FROZEN_NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
TAIL_COMMAND = [
    "/usr/bin/tail",
    "-n",
    "500",
    "/var/log/pveproxy/access.log",
]


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz: timezone | None = None) -> datetime:
        if tz is None:
            return FROZEN_NOW.replace(tzinfo=None)
        return FROZEN_NOW.astimezone(tz)


def _live_gateway(
    inventory: dict[int, dict[str, str]],
    output_by_vmid: dict[int, str],
) -> LiveProxmoxGateway:
    gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
    gateway._vm_inventory = MagicMock(return_value=inventory)
    gateway._batch_limit = MagicMock(return_value=4)

    def guest_command(node: str, vmid: int, command: list[str], *, timeout: int):
        return {
            "exit_code": 0,
            "stdout": output_by_vmid.get(vmid, ""),
            "stderr": "",
            "duration": 0.01,
        }

    gateway._guest_command = MagicMock(side_effect=guest_command)
    return gateway


class PveAccessTimeTests(unittest.TestCase):
    def test_parses_numeric_pveproxy_month_and_timezone_offsets(self) -> None:
        self.assertEqual(
            LiveProxmoxGateway._pve_access_time("22/07/2026:16:59:55 +0500"),
            datetime(2026, 7, 22, 11, 59, 55, tzinfo=timezone.utc),
        )
        self.assertEqual(
            LiveProxmoxGateway._pve_access_time("22/07/2026:04:59:40 -0700"),
            datetime(2026, 7, 22, 11, 59, 40, tzinfo=timezone.utc),
        )
        self.assertIsNone(
            LiveProxmoxGateway._pve_access_time("31/02/2026:12:00:00 +0000")
        )
        self.assertIsNone(LiveProxmoxGateway._pve_access_time("not-a-pve-time"))


class GatewayWebActivityTests(unittest.TestCase):
    def test_filters_access_log_and_classifies_active_and_recent(self) -> None:
        log = "\n".join(
            [
                # Two valid requests from the same browser identity are grouped.
                '10.39.2.10 - alice@pve [22/07/2026:16:59:30 +0500] "GET /api2/extjs/cluster/resources HTTP/1.1" 200 1200',
                '10.39.2.10 - alice@pve [22/07/2026:16:59:55 +0500] "GET /api2/extjs/cluster/resources?type=vm HTTP/1.1" 200 1201',
                # A valid request older than 60 seconds remains visible as recent.
                '10.39.2.11 - bob@pve [22/07/2026:16:58:30 +0500] "GET /api2/json/cluster/resources HTTP/2" 304 0',
                # Negative timezone offsets and IPv6 source addresses are supported.
                '2001:db8::42 - carol@pam [22/07/2026:04:59:40 -0700] "GET /api2/json/cluster/resources HTTP/1.1" 200 900',
                # Unauthenticated, unrelated, failed and stale requests are ignored.
                '10.39.2.12 - - [22/07/2026:16:59:59 +0500] "GET /api2/json/cluster/resources HTTP/1.1" 200 500',
                '10.39.2.13 - root@pam [22/07/2026:16:59:58 +0500] "GET /api2/json/nodes/pve/status HTTP/1.1" 200 500',
                '10.39.2.14 - root@pam [22/07/2026:16:59:57 +0500] "GET /api2/json/cluster/resources HTTP/1.1" 500 500',
                '10.39.2.15 - root@pam [22/07/2026:16:56:59 +0500] "GET /api2/json/cluster/resources HTTP/1.1" 200 500',
                # More than 30 seconds in the future must not be treated as presence.
                '10.39.2.16 - root@pam [22/07/2026:17:00:31 +0500] "GET /api2/json/cluster/resources HTTP/1.1" 200 500',
                'malformed access line',
            ]
        )
        gateway = _live_gateway({274: {"node": "outer-pve"}}, {274: log})

        with patch("dashboard_backend.proxmox_gateway.datetime", FrozenDateTime):
            result = gateway.web_activity([274], window_seconds=180)

        by_user = {item["user"]: item for item in result["activity"]}
        self.assertEqual(set(by_user), {"alice@pve", "bob@pve", "carol@pam"})
        self.assertEqual(
            (by_user["alice@pve"]["state"], by_user["alice@pve"]["age_seconds"]),
            ("active", 5),
        )
        self.assertEqual(by_user["alice@pve"]["request_count"], 2)
        self.assertEqual(
            by_user["alice@pve"]["last_seen"], "2026-07-22T11:59:55+00:00"
        )
        self.assertEqual(
            (by_user["bob@pve"]["state"], by_user["bob@pve"]["age_seconds"]),
            ("recent", 90),
        )
        self.assertEqual(by_user["carol@pam"]["source_ip"], "2001:db8::42")
        self.assertEqual(by_user["carol@pam"]["age_seconds"], 20)
        self.assertEqual(result["errors"], [])
        self.assertEqual(
            (result["scanned_vms"], result["requested_vms"], result["window_seconds"]),
            (1, 1, 180),
        )

    def test_uses_fixed_tail_argv_for_sorted_unique_inventory(self) -> None:
        inventory = {
            274: {"node": "outer-a"},
            275: {"node": "outer-b"},
        }
        gateway = _live_gateway(inventory, {})

        with patch("dashboard_backend.proxmox_gateway.datetime", FrozenDateTime):
            result = gateway.web_activity([275, 274, 275], window_seconds=180)

        gateway._vm_inventory.assert_called_once_with([274, 275], require_all=False)
        gateway._guest_command.assert_has_calls(
            [
                call("outer-a", 274, TAIL_COMMAND, timeout=8),
                call("outer-b", 275, TAIL_COMMAND, timeout=8),
            ],
            any_order=True,
        )
        self.assertEqual(gateway._guest_command.call_count, 2)
        self.assertEqual(result["requested_vms"], 2)
        self.assertEqual(result["scanned_vms"], 2)

    def test_missing_inventory_vm_is_reported_without_hiding_healthy_vm(self) -> None:
        gateway = _live_gateway(
            {274: {"node": "outer-a"}},
            {
                274: '10.39.2.10 - root@pam [22/07/2026:16:59:55 +0500] '
                '"GET /api2/extjs/cluster/resources HTTP/1.1" 200 1200',
            },
        )

        with patch("dashboard_backend.proxmox_gateway.datetime", FrozenDateTime):
            result = gateway.web_activity([274, 999], window_seconds=180)

        self.assertEqual([item["vmid"] for item in result["activity"]], [274])
        self.assertEqual(result["scanned_vms"], 1)
        self.assertEqual(result["requested_vms"], 2)
        self.assertEqual(result["errors"], [{"vmid": 999, "error": "VM не найдена в Proxmox"}])


class ServiceWebActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = DashboardStore(
            Path(self.tempdir.name) / "dashboard.sqlite3",
            seed_demo=False,
        )
        self.gateway = MagicMock()
        self.gateway.mode = "live"
        self.service = DashboardService(self.store, self.gateway)

        now = "2026-07-22T12:00:00+00:00"
        self.stand_id = self.store.execute(
            "INSERT INTO stands (name, status, pool_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("Экзамен 1", "running", "exam-1", now, now),
        )
        self.store.execute(
            "INSERT INTO stand_vms (stand_id, vmid, name, node, ip, status) VALUES (?, ?, ?, ?, ?, ?)",
            (self.stand_id, 274, "exam-1-1", "outer-a", "10.39.2.10", "running"),
        )
        self.store.execute(
            "INSERT INTO stand_vms (stand_id, vmid, name, node, ip, status) VALUES (?, ?, ?, ?, ?, ?)",
            (self.stand_id, 275, "exam-1-2", "outer-a", "10.39.2.11", "stopped"),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_enriches_gateway_results_and_reuses_short_cache(self) -> None:
        self.gateway.web_activity.return_value = {
            "activity": [
                {
                    "vmid": 274,
                    "source_ip": "192.0.2.25",
                    "user": "root@pam",
                    "last_seen": "2026-07-22T11:59:55+00:00",
                    "request_count": 3,
                    "age_seconds": 5,
                    "state": "active",
                }
            ],
            "errors": [{"vmid": 274, "error": "guest agent timeout"}],
            "scanned_vms": 0,
            "requested_vms": 1,
            "window_seconds": 180,
            "observed_at": "2026-07-22T12:00:00+00:00",
        }

        first = self.service.web_activity()
        second = self.service.web_activity()

        self.assertIs(first, second)
        self.gateway.web_activity.assert_called_once_with([274], window_seconds=180)
        item = first["activity"][0]
        self.assertEqual(
            (
                item["stand_id"], item["stand_name"], item["pool_id"],
                item["vm_name"], item["vm_ip"], item["node"],
            ),
            (self.stand_id, "Экзамен 1", "exam-1", "exam-1-1", "10.39.2.10", "outer-a"),
        )
        self.assertEqual(first["errors"][0]["stand_name"], "Экзамен 1")
        self.assertFalse(first["exact_sessions"])
        self.assertIn("недавняя активность", first["notice"].lower())


if __name__ == "__main__":
    unittest.main()
