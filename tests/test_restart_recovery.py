from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from dashboard_backend.database import DashboardStore, utc_now
from dashboard_backend.proxmox_gateway import (
    CloneVmidCollisionError,
    LiveProxmoxGateway,
)
from dashboard_backend.service import DashboardService


class RestartRecoveryTests(unittest.TestCase):
    def test_live_interrupted_deployment_becomes_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = DashboardStore(Path(tempdir) / "dashboard.db", seed_demo=False)
            now = utc_now()
            stand_id = store.execute(
                """INSERT INTO stands (name, status, progress, vm_count, created_at, updated_at)
                VALUES (?, 'provisioning', 4, 1, ?, ?)""",
                ("Прерванный стенд", now, now),
            )
            store.execute(
                """INSERT INTO stand_vms (stand_id, name, status)
                VALUES (?, ?, 'provisioning')""",
                (stand_id, "Прерванный стенд-1"),
            )
            gateway = MagicMock()
            gateway.mode = "live"

            with patch.object(DashboardService, "_start_scheduler_monitor"):
                DashboardService(store, gateway)

            stand = store.query_one("SELECT * FROM stands WHERE id = ?", (stand_id,))
            vm = store.query_one("SELECT * FROM stand_vms WHERE stand_id = ?", (stand_id,))
            self.assertEqual(stand["status"], "error")
            self.assertIn("перезапуском dashboard", stand["last_error"])
            self.assertEqual(vm["status"], "error")
            self.assertEqual(vm["credential_valid"], 0)

    def test_interrupted_check_is_finished_instead_of_polling_forever(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = DashboardStore(Path(tempdir) / "dashboard.db", seed_demo=False)
            now = utc_now()
            stand_id = store.execute(
                """INSERT INTO stands
                (name, status, check_status, vm_count, created_at, updated_at)
                VALUES (?, 'running', 'running', 1, ?, ?)""",
                ("Проверяемый стенд", now, now),
            )
            run_id = store.execute(
                """INSERT INTO check_runs
                (stand_id, status, details, output, started_at)
                VALUES (?, 'running', '[]', 'Ожидание', ?)""",
                (stand_id, now),
            )
            gateway = MagicMock()
            gateway.mode = "live"

            with patch.object(DashboardService, "_start_scheduler_monitor"):
                DashboardService(store, gateway)

            stand = store.query_one("SELECT * FROM stands WHERE id = ?", (stand_id,))
            run = store.query_one("SELECT * FROM check_runs WHERE id = ?", (run_id,))
            self.assertEqual(stand["check_status"], "failed")
            self.assertEqual(run["status"], "failed")
            self.assertIn("перезапуском dashboard", run["output"])
            self.assertIsNotNone(run["finished_at"])


class LiveGatewayResilienceTests(unittest.TestCase):
    def test_api_timeout_defaults_to_twenty_seconds(self) -> None:
        proxmox_api = MagicMock()
        module = SimpleNamespace(ProxmoxAPI=proxmox_api)
        environment = {
            "PROXMOX_HOST": "pve.example.test",
            "PROXMOX_USER": "dashboard@pve",
            "PROXMOX_TOKEN_NAME": "dashboard",
            "PROXMOX_TOKEN_VALUE": "secret",
        }
        with patch.dict(os.environ, environment, clear=True), patch.dict(
            sys.modules, {"proxmoxer": module},
        ):
            gateway = LiveProxmoxGateway()

        self.assertEqual(gateway.api_timeout, 20.0)
        self.assertEqual(proxmox_api.call_args.kwargs["timeout"], 20.0)

    def test_external_vmid_race_retries_without_claiming_foreign_vm(self) -> None:
        gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        gateway.client = MagicMock()
        gateway.client.cluster.nextid.get.side_effect = [290, 291]
        gateway._clone_submit_lock = __import__("threading").Lock()
        gateway._submit_clone_task = MagicMock(side_effect=[
            CloneVmidCollisionError("VMID 290 already belongs to another VM"),
            "UPID:pve-1:clone-291",
        ])
        created: list[tuple[str, int]] = []
        expected_names: dict[int, str] = {}

        with patch("dashboard_backend.proxmox_gateway.time.sleep") as sleep:
            vmid, upid = gateway._allocate_clone_task(
                "pve-1", 278, "exam-pool", "pve-2", "Exam-1",
                created, expected_names,
            )

        self.assertEqual((vmid, upid), (291, "UPID:pve-1:clone-291"))
        self.assertEqual(created, [("pve-2", 291)])
        self.assertEqual(expected_names, {291: "Exam-1"})
        self.assertEqual(gateway._submit_clone_task.call_count, 2)
        sleep.assert_called_once_with(0.2)


if __name__ == "__main__":
    unittest.main()
