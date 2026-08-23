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
