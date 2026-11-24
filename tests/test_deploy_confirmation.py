from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dashboard_backend.database import DashboardStore
from dashboard_backend.operation_queue import DeploymentCapacityBlocked
from dashboard_backend.proxmox_gateway import LiveProxmoxGateway
from dashboard_backend.service import ConflictError, DashboardService


class DeploymentConfirmationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = DashboardStore(Path(temp.name) / "dashboard.db", seed_demo=False)
        self.gateway = MagicMock(mode="live")
        self.gateway.list_pools.return_value = [{"pool_id": "Shared"}]
        self.gateway.deployment_scheduler_scope.return_value = {"node_weights": {"depo": 1}}
        self.service = DashboardService(self.store, self.gateway)
        self.blueprint = self.service.save_blueprint({
            "name": "Lab", "code": "LAB", "status": "active", "template_vmid": 123,
        })

    def finish_worker(self, stand_id):
        with self.service._job_lock:
            worker = self.service._jobs.get(stand_id)
        if worker:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        return self.service.get_stand(stand_id)

    def create_paused(self):
        self.service._operation_queue.update_pressure({"nodes": [{"node": "depo", "memory_percent": 99}]})
        stand = self.service.create_stand({
            "name": "My lab", "blueprint_id": self.blueprint["id"], "vm_count": 1,
            "pool_id": "Shared", "use_existing_pool": True, "subnet": "10.39.5.0/16",
            "bridge": "exam42",
        })
        stand = self.finish_worker(stand["id"])
        self.assertEqual(stand["status"], "awaiting_confirmation", stand["last_error"])
        self.assertEqual(stand["progress"], 0)
        self.assertIn("depo", stand["last_error"])
        self.gateway.deploy.assert_not_called()
        self.assertEqual(self.service.operation_queue()["queued_count"], 0)
        return stand["id"]

    def test_admission_considers_only_actual_destinations_and_clone_source(self):
        self.gateway.deployment_scheduler_scope.return_value = {
            "template_node": "source", "node_weights": {"destination": 1, "unused": 0},
        }
        scope = self.service._deployment_operation_scope({"node": "auto"}, {}, 1)
        self.assertEqual(scope["node_weights"], {"source": 1, "destination": 1})

    def test_pause_survives_restart_cancel_only_removes_local_reservations(self):
        stand_id = self.create_paused()
        self.service = DashboardService(self.store, self.gateway)
        node = self.service.get_stand(stand_id)
        self.assertEqual(node["status"], "awaiting_confirmation")
        self.assertEqual(self.service.list_ipam()["summary"]["reserved"], 1)
        self.service.stand_action(stand_id, "cancel_deploy")
        self.assertEqual(self.service.list_stands(), [])
        self.assertEqual(self.service.list_ipam()["summary"]["reserved"], 0)
        self.assertEqual(self.store.query_all("SELECT * FROM pending_deployments"), [])
        self.gateway.delete_stand.assert_not_called()
        self.gateway.power_action.assert_not_called()

    def test_retry_uses_original_plan_and_credentials_and_claims_once(self):
        stand_id = self.create_paused()
        secret = self.service.stand_credentials(stand_id)["credentials"][0]["password"]
        self.service = DashboardService(self.store, self.gateway)
        self.service.save_blueprint({"template_vmid": 999}, self.blueprint["id"])
        entered = threading.Event()
        release = threading.Event()

        def clone(stand, plan, _progress):
            entered.set()
            self.assertTrue(release.wait(3))
            self.assertEqual(plan["template_vmid"], 123)
            self.assertEqual(plan["bridge"], "exam42")
            self.assertEqual(plan["credentials"][0]["password"], secret)
            self.assertEqual(plan["allocated_ips"], ["10.39.5.0"])
            self.assertTrue(plan["use_existing_pool"])
            return [{"name": stand["vms"][0]["name"], "vmid": 500, "index": 1, "password": secret}]

        self.gateway.deploy.side_effect = clone
        with patch("dashboard_backend.service.deploy_relief.stop_selected") as relief:
            self.service.stand_action(stand_id, "retry_deploy", {"stop_stand_ids": [52]})
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(ConflictError):
                    self.service.stand_action(stand_id, "retry_deploy")
                with self.assertRaises(ConflictError):
                    self.service.stand_action(stand_id, "cancel_deploy")
            finally:
                release.set()
            self.assertEqual(self.finish_worker(stand_id)["status"], "running")
            relief.assert_called_once_with(self.service, stand_id, [52])
        self.gateway.deploy.assert_called_once()
        self.assertEqual(self.service.list_ipam()["summary"]["assigned"], 1)
        self.assertNotIn(secret, json.dumps(self.service.get_stand(stand_id)))

    def test_relief_failure_is_actionable_and_gateway_failure_cannot_be_replayed(self):
        stand_id = self.create_paused()
        self.service._operation_queue.update_pressure({"nodes": [{"node": "depo", "memory_percent": 62}]})
        with patch("dashboard_backend.service.deploy_relief.stop_selected", side_effect=ConflictError("стенд занят")):
            self.service.stand_action(stand_id, "retry_deploy", {"stop_stand_ids": [5]})
            self.assertIn("стенд занят", self.finish_worker(stand_id)["last_error"])
        self.gateway.deploy.assert_not_called()
        self.gateway.deploy.side_effect = DeploymentCapacityBlocked("после клонирования")
        self.service.stand_action(stand_id, "retry_deploy")
        self.assertEqual(self.finish_worker(stand_id)["status"], "error")
        with self.assertRaises(ConflictError):
            self.service.stand_action(stand_id, "retry_deploy")

    def test_unavailable_live_resource_telemetry_pauses_initial_attempt_and_retry(self):
        self.gateway = MagicMock(spec=LiveProxmoxGateway)
        self.gateway.mode = "live"
        self.gateway.scheduler_snapshot.side_effect = [
            TimeoutError("timeout"), {"stale": True, "sources": {"resources": False}},
        ]
        with patch.object(DashboardService, "_start_scheduler_monitor"):
            self.service = DashboardService(self.store, self.gateway)
        stand_id = self.service.create_stand({
            "name": "No metrics", "blueprint_id": self.blueprint["id"],
        })["id"]
        self.assertEqual(self.finish_worker(stand_id)["status"], "awaiting_confirmation")
        self.service.stand_action(stand_id, "retry_deploy")
        stand = self.finish_worker(stand_id)
        self.assertEqual(stand["status"], "awaiting_confirmation")
        self.assertIn("неизвестна", stand["last_error"])
        self.assertEqual([call.kwargs["force"] for call in self.gateway.scheduler_snapshot.call_args_list], [False, True])
        self.gateway.deploy.assert_not_called()
