from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard_backend.database import DashboardStore
from dashboard_backend.proxmox_gateway import LiveProxmoxGateway
from dashboard_backend.service import DashboardService, ValidationError


class TimerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "admin.db"
        self.db = DashboardStore(self.path)
        self.gw = Mock(mode="live")
        self.gw.timer_vm_uuid.return_value = "ce2f61c3-d922-4965-8934-e5bc91de21cc"
        self.service = DashboardService(self.db, self.gw)
        self.bp = next(b["id"] for b in self.service.list_blueprints() if b["status"] == "active")
        spawner = patch.object(self.service, "_launch_deployment")
        self.spawn = spawner.start()
        self.addCleanup(spawner.stop)
        timer = patch("dashboard_backend.stand_timer.StandTimer.start")
        timer.start()
        self.addCleanup(timer.stop)
        self.member = dict(vmid=9999, node="test-node", name="test-vm", status="running")
        self.gw.deploy.return_value = [self.member]
        self.gw.pool_members.return_value = [self.member]
        self.payload = dict(name="Test", pool_id="test-pool", blueprint_id=self.bp, owner="student01",
                            subnet="10.39.7.8/16", vm_count=1)
        self.future = int(time.time()) + 3 * 3600

    def deploy(self, minutes):
        initial = self.service.create_stand(dict(self.payload, duration_minutes=minutes))
        self.assertIsNone(initial["expires_at"])
        sid, plan = self.spawn.call_args.args
        self.service._deploy_job(sid, plan)
        result = self.service.get_stand(sid)
        self.assertEqual(result["status"], "running", result["last_error"])
        return result

    def test_validate_and_default(self):
        for value in (0, False, 10081, -1, 0.5, "1"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.service.create_stand(dict(self.payload, duration_minutes=value))
        with self.assertRaises(ValidationError):
            self.service.create_stand(dict(self.payload, duration_minutes=1, use_existing_pool=True))
        stand = self.deploy(None)
        self.assertIsNone(stand["duration_minutes"])
        self.assertIsNone(stand["expires_at"])
        self.assertFalse(self.db.query_all("SELECT * FROM stand_timer_targets"))
        self.service.stand_timer.tick(self.future)
        self.gw.shutdown_idle_vm.assert_not_called()
        self.gw.timer_vm_uuid.assert_not_called()

    def test_ready_deadline_survives_restart_with_old_ttl_ignored(self):
        stand = self.deploy(60)
        due = datetime.fromisoformat(stand["expires_at"])
        self.assertLess(abs((due - datetime.now(timezone.utc) - timedelta(hours=1)).total_seconds()), 10)
        self.db.execute("UPDATE stands SET expires_at='2000-01-01' WHERE id!=?", (stand["id"],))
        reopened = DashboardStore(self.path, seed_demo=False)
        self.assertEqual(reopened.query_one("SELECT expires_at FROM stands WHERE id=?", (stand["id"],))["expires_at"],
                         stand["expires_at"])
        self.assertFalse(reopened.query_all("SELECT id FROM stands WHERE duration_minutes IS NULL AND expires_at IS NOT NULL"))
        service2 = DashboardService(reopened, self.gw)
        service2.stand_timer.tick()
        self.gw.shutdown_idle_vm.assert_not_called()
        service2.stand_timer.tick(self.future)
        service2.stand_timer.tick(self.future + 200)
        self.gw.shutdown_idle_vm.assert_called_once_with({**self.member, "timer_uuid": self.gw.timer_vm_uuid.return_value, "timer_pool":"test-pool"})
        self.gw.power_action.assert_not_called()
        self.assertEqual(service2.get_stand(stand["id"])["status"], "stopped")

    def test_additional_pool_members_are_never_targeted(self):
        stand = self.deploy(1)
        self.gw.pool_members.return_value = [self.member, dict(vmid=10000, name="manual", status="running", node="test-node")]
        self.service.stand_timer.tick(self.future)
        self.assertEqual(self.gw.shutdown_idle_vm.call_count, 1)
        self.assertEqual(self.gw.shutdown_idle_vm.call_args.args[0]["vmid"], self.member["vmid"])
        self.assertEqual(self.service.get_stand(stand["id"])["status"], "running")

    def test_vm_generation_replaced_is_blocked(self):
        self.deploy(1)
        self.gw.timer_vm_uuid.return_value = "another-guest"
        self.service.stand_timer.tick(self.future)
        self.gw.shutdown_idle_vm.assert_not_called()
        self.assertEqual(self.db.query_one("SELECT state FROM stand_timer_targets")["state"], "blocked")

    def test_removed_from_pool_is_blocked(self):
        self.deploy(1)
        self.gw.pool_members.return_value = []
        self.service.stand_timer.tick(self.future)
        self.gw.shutdown_idle_vm.assert_not_called()

    def test_ownership_transfer_retires_enrollment(self):
        stand = self.deploy(1)
        self.db.execute("UPDATE stands SET owner='another student' WHERE id=?", (stand["id"],))
        self.service.stand_timer.tick(self.future)
        self.gw.shutdown_idle_vm.assert_not_called()
        self.assertEqual(self.db.query_one("SELECT state FROM stand_timer_targets")["state"], "blocked")

    def test_operator_restart_after_expiry_not_stopped_by_old_timer(self):
        stand = self.deploy(1)
        self.db.execute("UPDATE stands SET expires_at='2000-01-01' WHERE id=?", (stand["id"],))
        self.service.stand_action(stand["id"], "restart", {})
        self.service.stand_timer.tick(self.future)
        self.gw.shutdown_idle_vm.assert_not_called()

    def test_graceful_failure_retries_without_forcing(self):
        stand = self.deploy(1)
        self.gw.shutdown_idle_vm.side_effect = TimeoutError()
        self.service.stand_timer.tick(self.future)
        self.service.stand_timer.tick(self.future + 2)
        self.gw.shutdown_idle_vm.assert_called_once()
        self.gw.power_action.assert_not_called()
        self.assertEqual(self.service.get_stand(stand["id"])["status"], "running")
        self.gw.shutdown_idle_vm.side_effect = None
        self.service.stand_timer.tick(self.future + 301)
        self.assertEqual(self.service.get_stand(stand["id"])["status"], "stopped")

    def test_set_existing_scoped_lease_once(self):
        stand = self.deploy(None)
        args = dict(expected_vmid=9999, expected_pool="test-pool", expected_ip="10.39.7.8")
        with self.assertRaises(ValueError):
            self.service.set_stand_duration(stand["id"], 120, **{**args, "expected_ip":"10.39.7.9"})
        result = self.service.set_stand_duration(stand["id"], 120, **args)
        self.assertEqual(result, self.service.set_stand_duration(stand["id"], 120, **args))
        with self.assertRaises(ValueError):
            self.service.set_stand_duration(stand["id"], 240, **args)
        self.service.stand_timer.tick(self.future)
        with self.assertRaises(ValueError):
            self.service.set_stand_duration(stand["id"], 120, **args)

    def test_gateway_last_moment_uuid_check(self):
        gateway = object.__new__(LiveProxmoxGateway)
        gateway.client = Mock()
        api = gateway.client.nodes.return_value.qemu.return_value
        api.config.get.return_value = {"name":"test-vm","smbios1":"uuid=00000000-0000-4000-8000-000000000001"}
        with self.assertRaises(ValueError):
            gateway.shutdown_idle_vm({**self.member, "timer_uuid":"00000000-0000-4000-8000-000000000002"})
        api.status.shutdown.post.assert_not_called()
        gateway.pool_members = Mock(return_value=[])
        with self.assertRaises(ValueError):
            gateway.shutdown_idle_vm({**self.member, "timer_uuid":"00000000-0000-4000-8000-000000000001",
                                      "timer_pool":"test-pool"})
        api.status.shutdown.post.assert_not_called()
