import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from dashboard_backend import deploy_relief
from dashboard_backend.database import DashboardStore, utc_now
from dashboard_backend.proxmox_gateway import LiveProxmoxGateway
from dashboard_backend.service import ConflictError, DashboardService


class ReliefTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = DashboardStore(Path(tmp.name) / "db", seed_demo=False)
        self.gateway = MagicMock()
        self.gateway.mode = "live"
        self.service = DashboardService(self.db, self.gateway)
        self.target = self.stand("new", status="awaiting_confirmation")
        self.source = self.stand("free")
        self.gateway.web_activity.side_effect = lambda ids, window_seconds: {
            "activity": [], "errors": [], "scanned_vms": len(ids),
        }
        self.gateway.pool_members.return_value = self.service.get_stand(self.source)["vms"]

    def stand(self, name, status="running", workspace="demoexam", origin="deployed"):
        stand_id = self.db.execute(
            """INSERT INTO stands (name,status,workspace,origin,pool_id,vm_count,created_at,updated_at)
               VALUES (?,?,?,?,?,2,?,?)""", (name, status, workspace, origin, name, utc_now(), utc_now()),
        )
        for offset in (1, 2):
            self.db.execute(
                """INSERT INTO stand_vms (stand_id,vmid,name,status,node) VALUES (?,?,?,'running','node')""",
                (stand_id, stand_id * 100 + offset, f"{name}-{offset}"),
            )
        return stand_id

    def test_candidates_and_selected_scope(self):
        imported = self.stand("external", origin="imported")
        other = self.stand("other", workspace="mdk02.01")
        candidates = deploy_relief.candidates(self.service, self.target)
        self.assertEqual(candidates["stands"], [{"id": self.source, "name": "free", "vm_count": 2}])
        for unsafe in (imported, other, self.target):
            with self.subTest(unsafe=unsafe), self.assertRaises(ConflictError):
                deploy_relief.stop_selected(self.service, self.target, [self.source, unsafe])
        self.gateway.shutdown_idle_vm.assert_not_called()

    def test_busy_unreadable_or_incomplete_probe_vetoes_shutdown(self):
        probes = [
            {"scanned_vms": 2, "activity": [{"vmid": self.source * 100 + 2}], "errors": []},
            {"scanned_vms": 1, "activity": [], "errors": [{"vmid": self.source * 100 + 1}]},
            {"scanned_vms": 0, "activity": [], "errors": []},
        ]
        for probe in probes:
            self.gateway.web_activity.side_effect = None
            self.gateway.web_activity.return_value = probe
            with self.subTest(probe=probe), self.assertRaises(ConflictError):
                deploy_relief.stop_selected(self.service, self.target, [self.source])
        self.gateway.shutdown_idle_vm.assert_not_called()

    def test_reused_vmid_vetoes_whole_stand(self):
        self.gateway.pool_members.return_value[1]["name"] = "someone-else"
        with self.assertRaises(ConflictError):
            deploy_relief.stop_selected(self.service, self.target, [self.source])
        self.gateway.shutdown_idle_vm.assert_not_called()

    def test_confirmed_vm_states_survive_partial_failure(self):
        self.gateway.shutdown_idle_vm.side_effect = [None, TimeoutError("guest timed out")]
        with self.assertRaises(TimeoutError):
            deploy_relief.stop_selected(self.service, self.target, [self.source])
        stand = self.service.get_stand(self.source)
        self.assertEqual([vm["status"] for vm in stand["vms"]], ["stopped", "running"])
        self.assertEqual(stand["status"], "running")

    def test_all_confirmed_marks_stand_stopped(self):
        deploy_relief.stop_selected(self.service, self.target, [self.source])
        self.assertEqual(self.service.get_stand(self.source)["status"], "stopped")
        self.gateway.power_action.assert_not_called()

    def test_shutdown_uses_graceful_endpoint_and_verifies_result(self):
        gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        gateway.client = MagicMock()
        gateway._wait_task = MagicMock()
        vm = {"vmid": 42, "node": "node", "name": "free-1"}
        api = gateway.client.nodes("node").qemu(42)
        api.config.get.return_value = {"name": "free-1"}
        api.status.current.get.return_value = {"status": "running"}
        api.status.shutdown.post.return_value = "UPID"
        with self.assertRaisesRegex(RuntimeError, "остановку"):
            gateway.shutdown_idle_vm(vm)
        api.status.shutdown.post.assert_called_once_with(timeout=60, forceStop=0)
        api.status.stop.post.assert_not_called()
