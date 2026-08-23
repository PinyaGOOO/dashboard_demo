from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock

from dashboard_backend.operation_queue import ProxmoxOperationQueue
from server import DashboardHandler


class ProxmoxOperationQueueTests(unittest.TestCase):
    def test_free_capacity_starts_without_queueing(self) -> None:
        queue = ProxmoxOperationQueue(10)
        queued_positions: list[int] = []
        with queue.reserve("deploy", "stand-a", 7, vm_count=20, on_queued=queued_positions.append):
            state = queue.snapshot()
            self.assertEqual(state["used"], 7)
            self.assertEqual(state["active_count"], 1)
            self.assertEqual(state["queued_count"], 0)
        self.assertEqual(queued_positions, [])
        self.assertEqual(queue.snapshot()["used"], 0)

    def test_heavy_operations_are_serialized_and_capacity_is_released(self) -> None:
        queue = ProxmoxOperationQueue(10)
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        second_queued = threading.Event()

        def first() -> None:
            with queue.reserve("deploy", "stand-a", 7):
                first_entered.set()
                release_first.wait(2)

        def second() -> None:
            with queue.reserve("deploy", "stand-b", 7, on_queued=lambda _position: second_queued.set()):
                second_entered.set()

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        first_thread.start()
        self.assertTrue(first_entered.wait(1))
        second_thread.start()
        self.assertTrue(second_queued.wait(1))
        self.assertFalse(second_entered.wait(0.05))
        self.assertEqual(queue.snapshot()["queued_count"], 1)
        release_first.set()
        self.assertTrue(second_entered.wait(1))
        first_thread.join(1)
        second_thread.join(1)
        self.assertEqual(queue.snapshot()["used"], 0)

    def test_light_power_action_can_run_beside_deployment(self) -> None:
        queue = ProxmoxOperationQueue(10)
        deploy_entered = threading.Event()
        release_deploy = threading.Event()
        power_entered = threading.Event()

        def deploy() -> None:
            with queue.reserve("deploy", "stand-a", 8):
                deploy_entered.set()
                release_deploy.wait(2)

        def power() -> None:
            with queue.reserve("power", "stand-b start", 1):
                power_entered.set()

        deploy_thread = threading.Thread(target=deploy)
        power_thread = threading.Thread(target=power)
        deploy_thread.start()
        self.assertTrue(deploy_entered.wait(1))
        power_thread.start()
        self.assertTrue(power_entered.wait(1))
        release_deploy.set()
        deploy_thread.join(1)
        power_thread.join(1)

    def test_exception_releases_capacity(self) -> None:
        queue = ProxmoxOperationQueue(4)
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with queue.reserve("delete", "stand-a", 4):
                raise RuntimeError("boom")
        started = time.monotonic()
        with queue.reserve("delete", "stand-b", 4):
            pass
        self.assertLess(time.monotonic() - started, 0.1)

    def test_broken_queue_notification_does_not_leave_ghost_ticket(self) -> None:
        queue = ProxmoxOperationQueue(2)
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        def first() -> None:
            with queue.reserve("deploy", "stand-a", 2):
                first_entered.set()
                release_first.wait(2)

        def second() -> None:
            def broken_callback(_position: int) -> None:
                raise RuntimeError("audit unavailable")

            with queue.reserve("deploy", "stand-b", 2, on_queued=broken_callback):
                second_entered.set()

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        first_thread.start()
        self.assertTrue(first_entered.wait(1))
        second_thread.start()
        time.sleep(0.05)
        self.assertEqual(queue.snapshot()["queued_count"], 1)
        release_first.set()
        self.assertTrue(second_entered.wait(1))
        first_thread.join(1)
        second_thread.join(1)
        self.assertEqual(queue.snapshot()["queued_count"], 0)

    def test_different_nodes_run_concurrently_while_same_node_waits(self) -> None:
        queue = ProxmoxOperationQueue(
            10, node_capacity=2, control_reserve=0, node_control_reserve=0,
        )
        same_node_entered = threading.Event()

        def same_node() -> None:
            with queue.reserve(
                "deploy", "same-node", 2, node_weights={"node-a": 1},
            ):
                same_node_entered.set()

        with queue.reserve("deploy", "first", 2, node_weights={"node-a": 2}):
            with queue.reserve("deploy", "other-node", 2, node_weights={"node-b": 2}):
                worker = threading.Thread(target=same_node)
                worker.start()
                time.sleep(0.05)
                self.assertFalse(same_node_entered.is_set())
                node_state = {
                    item["name"]: item for item in queue.snapshot()["nodes"]
                }
                self.assertEqual(node_state["node-a"]["used"], 2)
                self.assertEqual(node_state["node-b"]["used"], 2)
            self.assertFalse(same_node_entered.wait(0.05))
        self.assertTrue(same_node_entered.wait(1))
        worker.join(1)

    def test_multiresource_waiter_does_not_partially_hold_free_node(self) -> None:
        queue = ProxmoxOperationQueue(
            10, node_capacity=2, storage_capacity=2,
            control_reserve=0, node_control_reserve=0,
        )
        waiter_entered = threading.Event()

        def waiter() -> None:
            with queue.reserve(
                "snapshot", "atomic", 2,
                node_weights={"node-b": 1}, storage_weights={"nas": 1},
            ):
                waiter_entered.set()

        with queue.reserve(
            "snapshot", "storage-owner", 2,
            node_weights={"node-a": 1}, storage_weights={"nas": 2},
        ):
            worker = threading.Thread(target=waiter)
            worker.start()
            time.sleep(0.05)
            self.assertFalse(waiter_entered.is_set())
            node_state = {
                item["name"]: item for item in queue.snapshot()["nodes"]
            }
            self.assertEqual(node_state["node-b"]["used"], 0)
            # The waiting request has not captured node-b, so unrelated work
            # can use it while NAS remains the bottleneck.
            with queue.reserve(
                "deploy", "node-b-free", 1, node_weights={"node-b": 2},
            ):
                self.assertEqual(queue.snapshot()["queued_count"], 1)
        self.assertTrue(waiter_entered.wait(1))
        worker.join(1)

    def test_control_lane_bypasses_aged_heavy_waiter(self) -> None:
        queue = ProxmoxOperationQueue(
            10, control_reserve=2, max_bypass_seconds=0,
        )
        heavy_entered = threading.Event()

        def heavy() -> None:
            with queue.reserve("deploy", "waiting-heavy", 3):
                heavy_entered.set()

        with queue.reserve("deploy", "active-heavy", 6):
            worker = threading.Thread(target=heavy)
            worker.start()
            time.sleep(0.05)
            self.assertFalse(heavy_entered.is_set())
            with queue.reserve("power", "emergency-stop", 1):
                self.assertEqual(queue.snapshot()["lanes"]["control"]["used"], 1)
        self.assertTrue(heavy_entered.wait(1))
        worker.join(1)

    def test_aged_node_waiter_does_not_block_independent_node(self) -> None:
        queue = ProxmoxOperationQueue(
            10, node_capacity=2, control_reserve=0,
            node_control_reserve=0, max_bypass_seconds=0,
        )
        waiter_entered = threading.Event()

        def waiter() -> None:
            with queue.reserve("deploy", "node-a-waiter", 2, node_weights={"node-a": 1}):
                waiter_entered.set()

        with queue.reserve("deploy", "node-a-active", 2, node_weights={"node-a": 2}):
            worker = threading.Thread(target=waiter)
            worker.start()
            time.sleep(0.05)
            with queue.reserve("deploy", "node-b", 2, node_weights={"node-b": 2}):
                self.assertFalse(waiter_entered.is_set())
        self.assertTrue(waiter_entered.wait(1))
        worker.join(1)

    def test_external_overload_is_accounted_but_control_reserve_still_works(self) -> None:
        queue = ProxmoxOperationQueue(
            6, node_capacity=3, control_reserve=1,
            node_control_reserve=1, telemetry_stale_seconds=0.1,
        )
        queue.update_pressure({
            "sources": {"resources": True, "tasks": True},
            "collected_at": "2026-08-23T00:00:00+00:00",
            "nodes": [{
                "node": "node-a", "status": "online",
                "cpu_ratio": 0.2, "memory_ratio": 0.3,
            }],
            "storages": [
                {"node": "node-a", "storage": "nas", "status": "available", "shared": True, "used_ratio": 0.4},
                {"node": "node-b", "storage": "nas", "status": "available", "shared": True, "used_ratio": 0.5},
            ],
            "tasks": [{
                "upid": "UPID:external-clone", "node": "node-a",
                "type": "qmclone", "status": "running", "running": True,
                "weight": 6, "node_weight": 3,
            }],
        })
        state = queue.snapshot()
        self.assertEqual(state["used"], 0)
        self.assertEqual(state["total_used"], 6)
        self.assertEqual(state["external_tasks_count"], 1)
        self.assertEqual(len(state["storages"]), 1)
        node = state["nodes"][0]
        self.assertEqual(node["used"], 0)
        self.assertEqual(node["total_used"], 3)

        # The external task did not honour our heavy reserve, but an emergency
        # power action can still use the dedicated global and node control slot.
        with queue.reserve(
            "power", "stop", 1, node_weights={"node-a": 1}, timeout=0.2,
        ):
            self.assertEqual(queue.snapshot()["used"], 1)

        time.sleep(0.12)
        with queue.reserve(
            "deploy", "after-stale", 2, node_weights={"node-a": 2}, timeout=0.2,
        ):
            self.assertEqual(queue.snapshot()["pressure"]["state"], "stale")

    def test_pressure_hysteresis_avoids_capacity_flapping(self) -> None:
        queue = ProxmoxOperationQueue(10, node_capacity=4)

        def update(cpu_ratio: float) -> int:
            queue.update_pressure({
                "nodes": [{
                    "node": "node-a", "status": "online",
                    "cpu_ratio": cpu_ratio, "memory_ratio": 0.2,
                }],
                "storages": [], "tasks": [],
            })
            return queue.snapshot()["nodes"][0]["effective_capacity"]

        self.assertEqual(update(0.81), 3)
        self.assertEqual(update(0.79), 3)
        self.assertEqual(update(0.74), 4)

    def test_timeout_removes_waiter_and_callback_runs_without_queue_lock(self) -> None:
        queue = ProxmoxOperationQueue(2, control_reserve=0)
        callback_completed = threading.Event()
        timed_out: list[Exception] = []

        def callback(_position: int) -> None:
            # snapshot takes the same condition lock; this would deadlock if
            # callbacks were invoked inside the critical section.
            queue.snapshot()
            callback_completed.set()

        def waiter() -> None:
            try:
                with queue.reserve(
                    "deploy", "timeout", 2, timeout=0.08,
                    on_queued=callback,
                ):
                    pass
            except Exception as exc:  # test captures the worker exception
                timed_out.append(exc)

        with queue.reserve("deploy", "owner", 2):
            worker = threading.Thread(target=waiter)
            worker.start()
            self.assertTrue(callback_completed.wait(0.5))
            worker.join(1)
            self.assertEqual(queue.snapshot()["queued_count"], 0)
        self.assertEqual(len(timed_out), 1)
        self.assertIsInstance(timed_out[0], TimeoutError)


class OperationQueueApiTests(unittest.TestCase):
    def test_queue_snapshot_endpoint(self) -> None:
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.path = "/api/operation-queue"
        handler.service = MagicMock()
        handler._authorized = MagicMock(return_value=True)
        handler._json = MagicMock()
        snapshot = {"capacity": 10, "used": 7, "active": [], "queued": []}
        handler.service.operation_queue.return_value = snapshot

        handler._handle_api("GET")

        handler.service.operation_queue.assert_called_once_with()
        handler._json.assert_called_once_with(snapshot)


if __name__ == "__main__":
    unittest.main()
