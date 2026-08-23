from __future__ import annotations

import tempfile
import threading
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from dashboard_backend.database import DashboardStore, utc_now
from dashboard_backend.proxmox_gateway import (
    CredentialRestoreError,
    DemoProxmoxGateway,
    LiveProxmoxGateway,
    RollbackSnapshotError,
)
from dashboard_backend.service import ConflictError, DashboardService
from server import DashboardHandler


def _live_gateway(
    snapshots: dict[int, list[dict[str, object]]],
) -> tuple[LiveProxmoxGateway, dict[int, MagicMock], dict[int, MagicMock]]:
    gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
    gateway.client = MagicMock()
    gateway._wait_tasks = MagicMock()
    gateway._wait_guest_agent = MagicMock()

    nodes: dict[str, MagicMock] = {}
    vms: dict[int, MagicMock] = {}
    rollback_endpoints: dict[int, MagicMock] = {}
    inventory: dict[int, dict[str, str]] = {}
    for index, (vmid, available) in enumerate(snapshots.items(), 1):
        node_name = f"pve-{index}"
        node = MagicMock(name=node_name)
        vm = MagicMock(name=f"vm-{vmid}")
        rollback = MagicMock(name=f"rollback-{vmid}")
        vm.config.get.return_value = {}
        vm.snapshot.get.return_value = available
        vm.snapshot.side_effect = lambda name, endpoint=rollback: endpoint
        rollback.rollback.post.return_value = f"UPID:rollback:{vmid}"
        vm.status.current.get.return_value = {"status": "stopped"}
        vm.status.start.post.return_value = f"UPID:start:{vmid}"
        node.qemu.side_effect = lambda requested, item=vm, expected=vmid: (
            item if int(requested) == expected else None
        )
        nodes[node_name] = node
        vms[vmid] = vm
        rollback_endpoints[vmid] = rollback
        inventory[vmid] = {"node": node_name, "status": "stopped"}

    gateway.client.nodes.side_effect = lambda name: nodes[str(name)]
    gateway._vm_inventory = MagicMock(return_value=inventory)
    return gateway, vms, rollback_endpoints


class GatewayRollbackTests(unittest.TestCase):
    def test_demo_rollback_reports_progress_without_sleeping(self) -> None:
        progress = MagicMock()
        with patch("dashboard_backend.proxmox_gateway.time.sleep") as sleep:
            DemoProxmoxGateway().rollback_snapshot(
                [274, 275], "start", start=True, progress=progress,
            )

        self.assertEqual(sleep.call_count, 3)
        self.assertEqual([item.args[0] for item in progress.call_args_list], [20, 65, 90])

    def test_live_preflight_missing_snapshot_submits_zero_tasks(self) -> None:
        gateway, vms, endpoints = _live_gateway({
            274: [{"name": "start"}],
            275: [{"name": "manual-1"}],
        })

        with self.assertRaisesRegex(RuntimeError, r"275"):
            gateway.rollback_snapshot([274, 275], "start", start=True)

        for vmid in (274, 275):
            endpoints[vmid].rollback.post.assert_not_called()
            vms[vmid].status.start.post.assert_not_called()
        gateway._wait_tasks.assert_not_called()
        gateway._wait_guest_agent.assert_not_called()

    def test_live_uses_rollback_start_zero_then_explicit_start_and_waits(self) -> None:
        gateway, vms, endpoints = _live_gateway({
            274: [{"name": "start"}],
            275: [{"snapname": "start"}],
        })

        with patch.dict(
            "os.environ",
            {"PROXMOX_ROLLBACK_BATCH": "4", "PROXMOX_DEPLOY_WORKERS": "1"},
        ):
            gateway.rollback_snapshot([274, 275], "start", start=True)

        endpoints[274].rollback.post.assert_called_once_with(start=0)
        endpoints[275].rollback.post.assert_called_once_with(start=0)
        vms[274].status.start.post.assert_called_once_with()
        vms[275].status.start.post.assert_called_once_with()
        self.assertEqual(
            gateway._wait_tasks.call_args_list,
            [
                call(
                    [("pve-1", "UPID:rollback:274"), ("pve-2", "UPID:rollback:275")],
                    timeout=1800,
                ),
                call(
                    [("pve-1", "UPID:start:274"), ("pve-2", "UPID:start:275")],
                    timeout=600,
                ),
            ],
        )
        gateway._wait_guest_agent.assert_has_calls(
            [call("pve-1", 274, 240), call("pve-2", 275, 240)],
        )

    def test_live_start_submission_error_drains_already_submitted_task(self) -> None:
        gateway, vms, _ = _live_gateway({
            274: [{"name": "start"}],
            275: [{"name": "start"}],
        })
        vms[275].status.start.post.side_effect = RuntimeError("start rejected")

        with (
            patch.dict("os.environ", {"PROXMOX_ROLLBACK_BATCH": "4"}),
            self.assertRaises(RollbackSnapshotError) as raised,
        ):
            gateway.rollback_snapshot([274, 275], "start", start=True)

        self.assertEqual(raised.exception.affected_vmids, [274, 275])
        self.assertEqual(
            gateway._wait_tasks.call_args_list,
            [
                call(
                    [("pve-1", "UPID:rollback:274"), ("pve-2", "UPID:rollback:275")],
                    timeout=1800,
                ),
                call([("pve-1", "UPID:start:274")], timeout=600),
            ],
        )
        vms[274].status.start.post.assert_called_once_with()
        vms[275].status.start.post.assert_called_once_with()
        gateway._wait_guest_agent.assert_not_called()


class ServiceRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = DashboardStore(
            Path(self.temporary.name) / "dashboard.db", seed_demo=False,
        )
        self.gateway = MagicMock()
        self.gateway.mode = "live"
        self.service = DashboardService(self.store, self.gateway)
        self.stand_id = self._seed_stand()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seed_stand(self) -> int:
        now = utc_now()
        stand_id = self.store.execute(
            """INSERT INTO stands
            (name, status, progress, node, pool_id, owner, vm_count, cpu, ram,
             check_score, check_status, last_check, origin, last_error,
             created_at, updated_at)
            VALUES (?, 'running', 100, 'pve-1', 'exam-test', 'Admin', 2, 17, 23,
                    61, 'warning', ?, 'deployed', 'old error', ?, ?)""",
            ("Rollback test", now, now, now),
        )
        rows = (
            (274, "vm-274", "pve-1", "root", "Current-Password-274"),
            (275, "vm-275", "pve-2", "exam", "Current-Password-275"),
        )
        for vmid, name, node, username, password in rows:
            self.store.execute(
                """INSERT INTO stand_vms
                (stand_id, vmid, name, node, status, cpu, ram,
                 credential_username, credential_password, last_snapshot,
                 has_start_snapshot, check_score, check_status, last_check)
                VALUES (?, ?, ?, ?, 'running', 8, 12, ?, ?, 'manual-1',
                        1, 55, 'warning', ?)""",
                (stand_id, vmid, name, node, username, password, now),
            )
        return stand_id

    def test_queued_snapshot_does_not_block_control_action_for_same_stand(self) -> None:
        """A heavy waiter must not own the per-stand lock before admission."""
        blocker = self.service._operation_queue.reserve(
            "deploy", "busy cluster", 8, lane="heavy",
        )
        blocker.__enter__()
        snapshot_errors: list[BaseException] = []
        stop_errors: list[BaseException] = []
        power_called = threading.Event()

        def snapshot() -> None:
            try:
                self.service.stand_action(
                    self.stand_id, "snapshot", {"name": "queued-test"},
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                snapshot_errors.append(exc)

        def stop() -> None:
            try:
                self.service.stand_action(self.stand_id, "stop")
            except BaseException as exc:  # pragma: no cover - asserted below
                stop_errors.append(exc)

        self.gateway.power_action.side_effect = lambda *_args, **_kwargs: power_called.set()
        snapshot_thread = threading.Thread(target=snapshot, daemon=True)
        stop_thread = threading.Thread(target=stop, daemon=True)
        try:
            snapshot_thread.start()
            for _ in range(100):
                queued = self.service._operation_queue.snapshot().get("queued", [])
                if any(item.get("kind") == "snapshot" for item in queued):
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("snapshot request did not enter the scheduler queue")

            stop_thread.start()
            self.assertTrue(
                power_called.wait(0.75),
                "control-lane stop was blocked by the queued snapshot lock",
            )
        finally:
            blocker.__exit__(None, None, None)
            snapshot_thread.join(timeout=2)
            if stop_thread.ident is not None:
                stop_thread.join(timeout=2)

        self.assertFalse(snapshot_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(snapshot_errors, [])
        self.assertEqual(stop_errors, [])

    def test_second_same_stand_operation_waits_locally_without_capacity(self) -> None:
        first_entered = threading.Event()
        release_first = threading.Event()
        errors: list[BaseException] = []

        def create_snapshot(_vmids, label, _description) -> None:
            if label == "first-local-gate":
                first_entered.set()
                release_first.wait(2)

        self.gateway.create_snapshot.side_effect = create_snapshot

        def snapshot(label: str) -> None:
            try:
                self.service.stand_action(
                    self.stand_id, "snapshot", {"name": label},
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        first = threading.Thread(target=snapshot, args=("first-local-gate",), daemon=True)
        second = threading.Thread(target=snapshot, args=("second-local-gate",), daemon=True)
        first.start()
        self.assertTrue(first_entered.wait(1))
        second.start()
        try:
            for _ in range(100):
                with self.service._stand_gate_condition:
                    local_waiters = len(
                        self.service._stand_gate_waiting.get(self.stand_id, []),
                    )
                if local_waiters == 2:
                    break
                threading.Event().wait(0.01)
            snapshot_state = self.service._operation_queue.snapshot()
            self.assertEqual(snapshot_state["active_count"], 1)
            self.assertEqual(snapshot_state["queued_count"], 0)
            self.assertEqual(snapshot_state["used"], 3)
        finally:
            release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.service._stand_gate_waiting, {})
        self.assertEqual(self.service._stand_gate_owner, {})

    def _seed_additional_stand(
        self,
        *,
        name: str,
        pool_id: str,
        vmid: int,
        origin: str = "deployed",
    ) -> int:
        now = utc_now()
        stand_id = self.store.execute(
            """INSERT INTO stands
            (name, status, progress, node, pool_id, owner, vm_count, cpu, ram,
             check_status, origin, last_error, created_at, updated_at)
            VALUES (?, 'running', 100, 'pve-3', ?, 'Admin', 1, 4, 8,
                    'idle', ?, '', ?, ?)""",
            (name, pool_id, origin, now, now),
        )
        self.store.execute(
            """INSERT INTO stand_vms
            (stand_id, vmid, name, node, status, cpu, ram,
             credential_username, credential_password, last_snapshot,
             has_start_snapshot, check_score, check_status, last_check)
            VALUES (?, ?, ?, 'pve-3', 'running', 4, 8,
                    'root', 'Additional-Password-123', 'manual-2',
                    1, 91, 'success', ?)""",
            (stand_id, vmid, f"vm-{vmid}", now),
        )
        return stand_id

    @staticmethod
    def _capturing_thread_factory(target_list: list[threading.Thread]):
        real_thread = threading.Thread

        def create(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            target_list.append(thread)
            return thread

        return create

    def test_async_success_resets_state_and_restores_current_credentials(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        order: list[str] = []

        def rollback(vmids, name, *, start, progress):
            self.assertEqual((vmids, name, start), ([274, 275], "start", True))
            order.append("rollback")
            progress(20, "preflight complete")
            entered.set()
            self.assertTrue(release.wait(2), "test did not release rollback worker")
            progress(70, "rollback complete")
            progress(90, "guests ready")

        def restore(credentials):
            order.append("credentials")
            self.assertEqual(
                credentials,
                [
                    {"vmid": 274, "username": "root", "password": "Current-Password-274"},
                    {"vmid": 275, "username": "exam", "password": "Current-Password-275"},
                ],
            )

        self.gateway.rollback_snapshot.side_effect = rollback
        self.gateway.restore_credentials.side_effect = restore
        threads: list[threading.Thread] = []
        with patch(
            "dashboard_backend.service.threading.Thread",
            side_effect=self._capturing_thread_factory(threads),
        ):
            response = self.service.stand_action(
                self.stand_id, "rollback_start", {"action": "rollback_start"},
            )

        self.assertTrue(entered.wait(2), "rollback worker did not start")
        self.assertEqual(response["stand"]["status"], "resetting")
        release.set()
        threads[0].join(2)
        self.assertFalse(threads[0].is_alive())
        self.assertEqual(order, ["rollback", "credentials"])

        stand = self.store.query_one("SELECT * FROM stands WHERE id = ?", (self.stand_id,))
        self.assertIsNotNone(stand)
        self.assertEqual(stand["status"], "running")
        self.assertEqual(stand["progress"], 100)
        self.assertEqual((stand["cpu"], stand["ram"]), (0, 0))
        self.assertIsNone(stand["check_score"])
        self.assertEqual(stand["check_status"], "idle")
        self.assertIsNone(stand["last_check"])
        self.assertEqual(stand["last_error"], "")

        vms = self.store.query_all(
            "SELECT * FROM stand_vms WHERE stand_id = ? ORDER BY vmid", (self.stand_id,),
        )
        self.assertEqual(len(vms), 2)
        for vm in vms:
            self.assertEqual(vm["status"], "running")
            self.assertEqual((vm["cpu"], vm["ram"]), (0, 0))
            self.assertEqual(vm["last_snapshot"], "start")
            self.assertEqual(vm["has_start_snapshot"], 1)
            self.assertIsNone(vm["check_score"])
            self.assertEqual(vm["check_status"], "idle")
            self.assertIsNone(vm["last_check"])
        activity = self.store.query_one("SELECT * FROM activity ORDER BY id DESC LIMIT 1")
        self.assertEqual((activity["kind"], activity["status"]), ("rollback", "success"))
        self.assertNotIn(self.stand_id, self.service._jobs)

    def test_progress_activity_failure_does_not_interrupt_rollback(self) -> None:
        raw_vms = self.store.query_all(
            "SELECT * FROM stand_vms WHERE stand_id = ? ORDER BY id", (self.stand_id,),
        )
        self.store.execute(
            "UPDATE stands SET status = 'resetting', progress = 5 WHERE id = ?",
            (self.stand_id,),
        )
        self.store.execute(
            """UPDATE stand_vms SET status = 'resetting', credential_valid = 0
            WHERE stand_id = ?""",
            (self.stand_id,),
        )

        def rollback(vmids, name, *, start, progress):
            progress(20, "snapshot preflight complete")

        self.gateway.rollback_snapshot.side_effect = rollback
        self.gateway.restore_credentials.return_value = [274, 275]
        with patch.object(
            self.store,
            "add_activity",
            side_effect=RuntimeError("activity storage unavailable"),
        ) as add_activity:
            self.service._rollback_start_job(self.stand_id, "Rollback test", raw_vms)

        self.assertEqual(add_activity.call_count, 2)  # milestone and final success audit
        stand = self.store.query_one("SELECT * FROM stands WHERE id = ?", (self.stand_id,))
        self.assertEqual(stand["status"], "running")
        self.assertEqual(stand["progress"], 100)
        self.assertEqual(stand["last_error"], "")
        self.assertEqual(
            self.store.query_all(
                """SELECT credential_valid FROM stand_vms
                WHERE stand_id = ? ORDER BY vmid""",
                (self.stand_id,),
            ),
            [{"credential_valid": 1}, {"credential_valid": 1}],
        )
        self.gateway.restore_credentials.assert_called_once()

    def test_rollback_waits_for_password_rotation_and_uses_new_credentials(self) -> None:
        rotation_entered = threading.Event()
        release_rotation = threading.Event()
        rollback_lock_attempted = threading.Event()
        rollback_entered = threading.Event()
        release_rollback = threading.Event()
        rollback_returned = threading.Event()
        errors: list[BaseException] = []
        restored: list[dict[str, object]] = []

        def rotate_password(vmids, username, password):
            self.assertEqual(vmids, [274, 275])
            rotation_entered.set()
            self.assertTrue(release_rotation.wait(2), "test did not release password rotation")

        def rollback_snapshot(vmids, name, *, start, progress):
            rollback_entered.set()
            self.assertTrue(release_rollback.wait(2), "test did not release rollback")

        self.gateway.rotate_password.side_effect = rotate_password
        self.gateway.rollback_snapshot.side_effect = rollback_snapshot
        self.gateway.restore_credentials.side_effect = lambda credentials: restored.extend(credentials)

        operation_lock = self.service._stand_operation_lock(self.stand_id)

        def observed_operation_lock(stand_id):
            self.assertEqual(stand_id, self.stand_id)
            if threading.current_thread().name == "rollback-request":
                rollback_lock_attempted.set()
            return operation_lock

        self.service._stand_operation_lock = observed_operation_lock

        def run_rotation() -> None:
            try:
                self.service.stand_action(
                    self.stand_id,
                    "rotate_password",
                    {
                        "username": "newadmin",
                        "web_username": "newadmin@pam",
                        "password": "Newest-Password-999",
                    },
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def run_rollback_request() -> None:
            try:
                self.service.stand_action(self.stand_id, "rollback_start")
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                rollback_returned.set()

        real_thread = threading.Thread
        rotation_request = real_thread(target=run_rotation, name="password-request")
        rollback_request = real_thread(target=run_rollback_request, name="rollback-request")
        workers: list[threading.Thread] = []
        with patch(
            "dashboard_backend.service.threading.Thread",
            side_effect=self._capturing_thread_factory(workers),
        ):
            rotation_request.start()
            self.assertTrue(rotation_entered.wait(2), "password rotation did not enter gateway")
            rollback_request.start()
            self.assertTrue(rollback_lock_attempted.wait(2), "rollback did not contend for stand lock")
            self.assertFalse(rollback_returned.is_set())
            self.assertFalse(rollback_entered.is_set())

            release_rotation.set()
            rotation_request.join(2)
            self.assertFalse(rotation_request.is_alive())
            self.assertTrue(rollback_entered.wait(2), "rollback did not start after password rotation")
            rollback_request.join(2)
            self.assertFalse(rollback_request.is_alive())
            self.assertEqual(len(workers), 1)

            release_rollback.set()
            workers[0].join(2)
            self.assertFalse(workers[0].is_alive())

        self.assertFalse(errors)
        self.assertEqual(
            restored,
            [
                {"vmid": 274, "username": "newadmin", "password": "Newest-Password-999"},
                {"vmid": 275, "username": "newadmin", "password": "Newest-Password-999"},
            ],
        )
        current = self.store.query_all(
            """SELECT credential_username, credential_password
            FROM stand_vms WHERE stand_id = ? ORDER BY vmid""",
            (self.stand_id,),
        )
        self.assertEqual(
            [(vm["credential_username"], vm["credential_password"]) for vm in current],
            [("newadmin", "Newest-Password-999"), ("newadmin", "Newest-Password-999")],
        )

    def test_gateway_error_marks_stand_error(self) -> None:
        self.gateway.rollback_snapshot.side_effect = RuntimeError("snapshot task failed")
        threads: list[threading.Thread] = []
        with patch(
            "dashboard_backend.service.threading.Thread",
            side_effect=self._capturing_thread_factory(threads),
        ):
            self.service.stand_action(self.stand_id, "rollback_start")
        threads[0].join(2)
        self.assertFalse(threads[0].is_alive())

        stand = self.store.query_one("SELECT * FROM stands WHERE id = ?", (self.stand_id,))
        self.assertEqual(stand["status"], "error")
        self.assertIn("snapshot task failed", stand["last_error"])
        self.gateway.restore_credentials.assert_not_called()
        activity = self.store.query_one("SELECT * FROM activity ORDER BY id DESC LIMIT 1")
        self.assertEqual((activity["kind"], activity["status"]), ("rollback", "error"))
        self.assertNotIn(self.stand_id, self.service._jobs)

    def test_partial_credential_restore_keeps_secret_but_hides_failed_password(self) -> None:
        self.gateway.restore_credentials.side_effect = CredentialRestoreError(
            {275: "guest agent rejected password"},
            [274],
        )
        threads: list[threading.Thread] = []
        with patch(
            "dashboard_backend.service.threading.Thread",
            side_effect=self._capturing_thread_factory(threads),
        ):
            self.service.stand_action(self.stand_id, "rollback_start")
        threads[0].join(2)
        self.assertFalse(threads[0].is_alive())

        rows = self.store.query_all(
            """SELECT vmid, credential_password, credential_valid
            FROM stand_vms WHERE stand_id = ? ORDER BY vmid""",
            (self.stand_id,),
        )
        self.assertEqual(
            [(row["vmid"], row["credential_password"], row["credential_valid"]) for row in rows],
            [
                (274, "Current-Password-274", 1),
                (275, "Current-Password-275", 0),
            ],
        )

        credentials = {
            int(item["vmid"]): item
            for item in self.service.stand_credentials(self.stand_id)["credentials"]
        }
        self.assertEqual(credentials[274]["password"], "Current-Password-274")
        self.assertTrue(credentials[274]["credential_available"])
        self.assertEqual(credentials[275]["password"], "")
        self.assertFalse(credentials[275]["credential_available"])
        self.assertTrue(credentials[275]["credential_recoverable"])

        failed = self.service.vm_credentials(self.stand_id, 275)
        self.assertEqual(failed["password"], "")
        self.assertFalse(failed["credential_available"])
        self.assertTrue(failed["credential_recoverable"])
        self.assertEqual(
            self.store.query_one("SELECT status FROM stands WHERE id = ?", (self.stand_id,))["status"],
            "error",
        )
        self.assertNotIn(self.stand_id, self.service._jobs)

    def test_missing_start_snapshot_is_rejected_before_background_job(self) -> None:
        self.store.execute(
            "UPDATE stand_vms SET has_start_snapshot = 0 WHERE vmid = 275",
        )

        with self.assertRaises(ConflictError):
            self.service.stand_action(self.stand_id, "rollback_start")

        self.gateway.rollback_snapshot.assert_not_called()
        self.assertEqual(
            self.store.query_one("SELECT status FROM stands WHERE id = ?", (self.stand_id,))["status"],
            "running",
        )
        self.assertFalse(self.service._jobs)

    def test_vm_rollback_targets_only_selected_vm_and_preserves_sibling(self) -> None:
        self.store.execute(
            "UPDATE stand_vms SET status = 'error' WHERE stand_id = ? AND vmid = 275",
            (self.stand_id,),
        )
        self.store.execute(
            """UPDATE stands SET status = 'error', last_error = 'Ошибка соседней VM',
            cpu = 17.5, ram = 23.0 WHERE id = ?""",
            (self.stand_id,),
        )
        sibling_before = self.store.query_one(
            "SELECT * FROM stand_vms WHERE stand_id = ? AND vmid = 275",
            (self.stand_id,),
        )
        self.assertIsNotNone(sibling_before)
        self.gateway.restore_credentials.return_value = [274]
        threads: list[threading.Thread] = []

        with patch(
            "dashboard_backend.service.threading.Thread",
            side_effect=self._capturing_thread_factory(threads),
        ):
            response = self.service.vm_action(
                self.stand_id,
                274,
                "rollback_start",
                {"action": "rollback_start"},
            )

        self.assertEqual(response["vmids"], [274])
        self.assertEqual(len(threads), 1)
        threads[0].join(2)
        self.assertFalse(threads[0].is_alive())

        rollback_call = self.gateway.rollback_snapshot.call_args
        self.assertEqual(rollback_call.args, ([274], "start"))
        self.assertTrue(rollback_call.kwargs["start"])
        self.assertTrue(callable(rollback_call.kwargs["progress"]))
        self.gateway.restore_credentials.assert_called_once_with([
            {
                "vmid": 274,
                "username": "root",
                "password": "Current-Password-274",
            },
        ])

        target = self.store.query_one(
            "SELECT * FROM stand_vms WHERE stand_id = ? AND vmid = 274",
            (self.stand_id,),
        )
        sibling_after = self.store.query_one(
            "SELECT * FROM stand_vms WHERE stand_id = ? AND vmid = 275",
            (self.stand_id,),
        )
        self.assertEqual(target["status"], "running")
        self.assertEqual(target["last_snapshot"], "start")
        self.assertEqual(target["check_status"], "idle")
        self.assertIsNone(target["check_score"])

        sibling_fields = (
            "status", "cpu", "ram", "last_snapshot", "has_start_snapshot",
            "credential_username", "credential_password", "credential_valid",
            "check_score", "check_status", "last_check",
        )
        self.assertEqual(
            tuple(sibling_after[field] for field in sibling_fields),
            tuple(sibling_before[field] for field in sibling_fields),
        )
        stand_after = self.store.query_one(
            "SELECT status, last_error, cpu, ram FROM stands WHERE id = ?",
            (self.stand_id,),
        )
        self.assertEqual(stand_after["status"], "error")
        self.assertEqual(stand_after["last_error"], "Ошибка соседней VM")
        self.assertEqual((stand_after["cpu"], stand_after["ram"]), (17.5, 23.0))

    def test_vm_rollback_does_not_clear_unrelated_stand_error(self) -> None:
        self.store.execute(
            """UPDATE stands SET status = 'error', last_error = 'Ошибка pool',
            cpu = 9.5, ram = 14.0 WHERE id = ?""",
            (self.stand_id,),
        )
        self.gateway.restore_credentials.return_value = [274]
        threads: list[threading.Thread] = []

        with patch(
            "dashboard_backend.service.threading.Thread",
            side_effect=self._capturing_thread_factory(threads),
        ):
            self.service.vm_action(self.stand_id, 274, "rollback_start")
        threads[0].join(2)

        stand = self.store.query_one(
            "SELECT status, last_error, cpu, ram FROM stands WHERE id = ?",
            (self.stand_id,),
        )
        self.assertEqual(stand["status"], "error")
        self.assertEqual(stand["last_error"], "Ошибка pool")
        self.assertEqual((stand["cpu"], stand["ram"]), (9.5, 14.0))

    def test_bulk_rollback_rejects_when_no_stand_is_eligible(self) -> None:
        self.store.execute(
            "UPDATE stands SET origin = 'imported' WHERE id = ?",
            (self.stand_id,),
        )

        with self.assertRaisesRegex(ConflictError, "Нет стендов"):
            self.service.rollback_all_stands()

        self.assertIsNone(self.service._bulk_rollback_job)
        self.gateway.rollback_snapshot.assert_not_called()

    def test_bulk_rollback_rejects_duplicate_running_coordinator(self) -> None:
        running = MagicMock()
        running.is_alive.return_value = True
        self.service._bulk_rollback_job = running

        with (
            patch("dashboard_backend.service.threading.Thread") as thread,
            self.assertRaisesRegex(ConflictError, "уже выполняется"),
        ):
            self.service.rollback_all_stands()

        thread.assert_not_called()
        self.gateway.rollback_snapshot.assert_not_called()

    def test_bulk_rollback_schedules_eligible_and_reports_imported_as_skipped(self) -> None:
        imported_id = self._seed_additional_stand(
            name="Imported pool",
            pool_id="imported-pool",
            vmid=376,
            origin="imported",
        )
        coordinator = MagicMock()
        coordinator.is_alive.return_value = False

        with patch("dashboard_backend.service.threading.Thread", return_value=coordinator):
            response = self.service.rollback_all_stands()

        coordinator.start.assert_called_once_with()
        self.assertEqual(response["scheduled"], [self.stand_id])
        self.assertEqual(response["scheduled_count"], 1)
        self.assertEqual(response["skipped_count"], 1)
        self.assertEqual(self.service._bulk_rollback_pending, {self.stand_id})
        self.assertTrue(self.service.get_stand(self.stand_id)["bulk_rollback_pending"])
        with self.assertRaisesRegex(ConflictError, "очередь массового возврата"):
            self.service.stand_action(self.stand_id, "stop")
        self.assertEqual(response["skipped"], [{
            "stand_id": imported_id,
            "name": "Imported pool",
            "reason": "подключённый существующий pool",
        }])

    def test_bulk_coordinator_is_serial_by_default_and_continues_after_failure(self) -> None:
        second_id = self._seed_additional_stand(
            name="Second stand",
            pool_id="exam-second",
            vmid=376,
        )
        calls: list[int] = []
        active = 0
        peak_active = 0
        counter_lock = threading.Lock()
        self.service._bulk_rollback_pending = {self.stand_id, second_id}

        def stand_action(stand_id, action, payload, *, _bulk_reserved=False):
            nonlocal active, peak_active
            self.assertEqual((action, payload), ("rollback_start", {"action": "rollback_start"}))
            self.assertTrue(_bulk_reserved)
            if stand_id == second_id:
                self.assertNotIn(self.stand_id, self.service._bulk_rollback_pending)
                self.assertIn(second_id, self.service._bulk_rollback_pending)
            with counter_lock:
                active += 1
                peak_active = max(peak_active, active)
                calls.append(stand_id)
            try:
                if stand_id == self.stand_id:
                    raise RuntimeError("first stand failed")
                return {"message": "started"}
            finally:
                with counter_lock:
                    active -= 1

        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(self.service, "stand_action", side_effect=stand_action),
        ):
            self.service._bulk_rollback_start_job([self.stand_id, second_id])

        self.assertEqual(calls, [self.stand_id, second_id])
        self.assertEqual(peak_active, 1)
        self.assertFalse(self.service._bulk_rollback_pending)
        self.assertIn(
            "first stand failed",
            self.store.query_one(
                "SELECT last_error FROM stands WHERE id = ?", (self.stand_id,),
            )["last_error"],
        )
        activity = self.store.query_one("SELECT * FROM activity ORDER BY id DESC LIMIT 1")
        self.assertEqual((activity["kind"], activity["status"]), ("rollback", "warning"))
        self.assertIn("Успешно 1 из 2", activity["detail"])
        self.assertIsNone(self.service._bulk_rollback_job)


class RollbackApiTests(unittest.TestCase):
    def test_bulk_rollback_action_returns_accepted(self) -> None:
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.path = "/api/stands/actions"
        handler.service = MagicMock()
        handler._authorized = MagicMock(return_value=True)
        body = {"action": "rollback_start_all"}
        result = {"message": "started", "scheduled_count": 2}
        handler._body = MagicMock(return_value=body)
        handler._json = MagicMock()
        handler.service.rollback_all_stands.return_value = result

        handler._handle_api("POST")

        handler.service.rollback_all_stands.assert_called_once_with()
        handler._json.assert_called_once_with(result, HTTPStatus.ACCEPTED)

    def test_stand_rollback_action_returns_accepted(self) -> None:
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.path = "/api/stands/7/actions"
        handler.service = MagicMock()
        handler._authorized = MagicMock(return_value=True)
        body = {"action": "rollback_start"}
        result = {"message": "started"}
        handler._body = MagicMock(return_value=body)
        handler._json = MagicMock()
        handler.service.stand_action.return_value = result

        handler._handle_api("POST")

        handler.service.stand_action.assert_called_once_with(7, "rollback_start", body)
        handler._json.assert_called_once_with(result, HTTPStatus.ACCEPTED)

    def test_vm_rollback_action_returns_accepted(self) -> None:
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.path = "/api/stands/7/vms/274/actions"
        handler.service = MagicMock()
        handler._authorized = MagicMock(return_value=True)
        body = {"action": "rollback_start"}
        result = {"message": "started"}
        handler._body = MagicMock(return_value=body)
        handler._json = MagicMock()
        handler.service.vm_action.return_value = result

        handler._handle_api("POST")

        handler.service.vm_action.assert_called_once_with(7, 274, "rollback_start", body)
        handler._json.assert_called_once_with(result, HTTPStatus.ACCEPTED)


if __name__ == "__main__":
    unittest.main()
