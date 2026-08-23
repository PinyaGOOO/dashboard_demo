from __future__ import annotations

import threading
import time
import unittest

from dashboard_backend.operation_queue import ProxmoxOperationQueue


class AdaptiveOperationQueueTests(unittest.TestCase):
    def test_blocked_old_ticket_does_not_hold_unrelated_node(self) -> None:
        queue = ProxmoxOperationQueue(10, max_bypass_seconds=0.01)
        active_entered = threading.Event()
        release_active = threading.Event()
        same_node_queued = threading.Event()
        other_node_entered = threading.Event()

        def active() -> None:
            with queue.reserve("deploy", "active-a", 4, node_weights={"depo": 3}):
                active_entered.set()
                release_active.wait(2)

        def same_node() -> None:
            with queue.reserve(
                "deploy", "waiting-a", 1, node_weights={"depo": 1},
                on_queued=lambda _position: same_node_queued.set(),
            ):
                pass

        def other_node() -> None:
            with queue.reserve("deploy", "ready-b", 1, node_weights={"fuji1": 1}):
                other_node_entered.set()

        threads = [threading.Thread(target=active), threading.Thread(target=same_node)]
        threads[0].start()
        self.assertTrue(active_entered.wait(1))
        threads[1].start()
        self.assertTrue(same_node_queued.wait(1))
        time.sleep(0.03)  # the blocked ticket is now old enough for fairness protection
        third = threading.Thread(target=other_node)
        third.start()

        self.assertTrue(other_node_entered.wait(1), queue.snapshot())
        release_active.set()
        for thread in [*threads, third]:
            thread.join(1)

    def test_multinode_reservation_is_atomic_while_waiting(self) -> None:
        queue = ProxmoxOperationQueue(10)
        active_entered = threading.Event()
        release_active = threading.Event()
        multi_queued = threading.Event()

        def active() -> None:
            with queue.reserve("snapshot", "busy-b", 2, node_weights={"fuji1": 3}):
                active_entered.set()
                release_active.wait(2)

        def multinode() -> None:
            with queue.reserve(
                "rollback", "a-and-b", 2,
                node_weights={"depo": 2, "fuji1": 1},
                on_queued=lambda _position: multi_queued.set(),
            ):
                pass

        first = threading.Thread(target=active)
        second = threading.Thread(target=multinode)
        first.start()
        self.assertTrue(active_entered.wait(1))
        second.start()
        self.assertTrue(multi_queued.wait(1))

        snapshot = queue.snapshot()
        depo = next(node for node in snapshot["nodes"] if node["name"] == "depo")
        self.assertEqual(depo["used"], 0)
        self.assertEqual(snapshot["used"], 2)
        self.assertEqual(snapshot["queued_count"], 1)

        release_active.set()
        first.join(1)
        second.join(1)

    def test_external_heavy_overload_keeps_control_reserve_available(self) -> None:
        queue = ProxmoxOperationQueue(10, control_reserve=2)
        queue.update_pressure({
            "sources": {"resources": True, "tasks": True},
            "nodes": [{
                "node": "depo", "status": "online", "cpu_percent": 30,
                "memory_percent": 40,
            }],
            "tasks": [
                {"upid": f"UPID:depo:{index}", "node": "depo", "type": "qmclone"}
                for index in range(6)
            ],
        })

        before = queue.snapshot()
        self.assertGreater(before["external_used"], before["effective_capacity"])
        started = time.monotonic()
        with queue.reserve(
            "power", "emergency stop", 1,
            node_weights={"depo": 1}, lane="control", timeout=0.25,
        ):
            self.assertEqual(queue.snapshot()["used"], 1)
        self.assertLess(time.monotonic() - started, 0.2)

    def test_control_reserve_does_not_bypass_hard_zero_capacity(self) -> None:
        global_queue = ProxmoxOperationQueue(
            10, control_reserve=2, node_control_reserve=1,
        )
        global_queue.update_pressure({
            "sources": {"resources": True, "tasks": True},
            "cluster": {"status": "offline", "effective_capacity": 0},
            "nodes": [{"node": "depo", "status": "online"}],
            "tasks": [],
        })
        self.assertEqual(global_queue.snapshot()["effective_capacity"], 0)
        with self.assertRaises(TimeoutError):
            with global_queue.reserve(
                "power", "offline cluster", 1, timeout=0.03,
            ):
                pass
        self.assertEqual(global_queue.snapshot()["queued_count"], 0)

        node_queue = ProxmoxOperationQueue(
            10, node_capacity=4, control_reserve=2,
            node_control_reserve=1,
        )
        node_queue.update_pressure({
            "sources": {"resources": True, "tasks": True},
            "nodes": [{"node": "depo", "status": "offline", "online": False}],
            "tasks": [],
        })
        node = next(
            item for item in node_queue.snapshot()["nodes"]
            if item["name"] == "depo"
        )
        self.assertEqual(node["effective_capacity"], 0)
        with self.assertRaises(TimeoutError):
            with node_queue.reserve(
                "power", "offline node", 1,
                node_weights={"depo": 1}, timeout=0.03,
            ):
                pass
        self.assertEqual(node_queue.snapshot()["queued_count"], 0)

    def test_node_pressure_reduces_global_and_node_autolimit(self) -> None:
        queue = ProxmoxOperationQueue(10, node_capacity=4)
        queue.update_pressure({
            "collected_at": "2026-08-23T08:00:00+00:00",
            "sources": {"resources": True, "tasks": True},
            "nodes": [
                {"node": "depo", "status": "online", "cpu_ratio": 0.91, "memory_ratio": 0.5},
                {"node": "fuji1", "status": "online", "cpu_ratio": 0.2, "memory_ratio": 0.4},
            ],
            "tasks": [],
        })

        snapshot = queue.snapshot()
        depo = next(node for node in snapshot["nodes"] if node["name"] == "depo")
        self.assertEqual(depo["cpu_percent"], 91.0)
        self.assertLess(depo["effective_capacity"], depo["base_capacity"])
        self.assertLess(snapshot["effective_capacity"], snapshot["configured_capacity"])
        self.assertEqual(snapshot["pressure"]["state"], "throttled")

    def test_unrelated_hot_shared_storage_does_not_reduce_global_capacity(self) -> None:
        queue = ProxmoxOperationQueue(
            10, node_capacity=4, storage_capacity=6,
            control_reserve=2, node_control_reserve=1,
        )
        queue.update_pressure({
            "sources": {"resources": True, "tasks": True},
            "nodes": [{
                "node": "depo", "status": "online",
                "cpu_percent": 20, "memory_percent": 30,
            }],
            "storages": [
                {
                    "node": "depo", "storage": "busy-nas",
                    "status": "available", "shared": True,
                    "used_percent": 98,
                },
                {
                    "node": "fuji1", "storage": "busy-nas",
                    "status": "available", "shared": True,
                    "used_percent": 98,
                },
            ],
            "tasks": [],
        })

        snapshot = queue.snapshot()
        self.assertEqual(snapshot["effective_capacity"], 10)
        storage = next(
            item for item in snapshot["storages"]
            if item["name"] == "busy-nas"
        )
        self.assertLess(
            storage["effective_capacity"], storage["base_capacity"],
        )
        # A deployment that does not use this NAS starts immediately.
        with queue.reserve(
            "deploy", "other-storage", 8,
            node_weights={"depo": 1}, timeout=0.1,
        ):
            self.assertEqual(queue.snapshot()["used"], 8)
        # The NAS constraint still applies when a ticket explicitly names it.
        with self.assertRaises(TimeoutError):
            with queue.reserve(
                "snapshot", "busy-storage", 1,
                node_weights={"depo": 1},
                storage_weights={"busy-nas": 2}, timeout=0.03,
            ):
                pass

    def test_same_local_storage_id_on_different_nodes_is_independent(self) -> None:
        queue = ProxmoxOperationQueue(
            10, node_capacity=4, storage_capacity=2,
            control_reserve=2, node_control_reserve=1,
        )
        queue.update_pressure({
            "sources": {"resources": True, "tasks": True},
            "nodes": [
                {"node": "depo", "status": "online"},
                {"node": "fuji1", "status": "online"},
            ],
            "storages": [
                {
                    "node": "depo", "storage": "local-lvm",
                    "status": "available", "shared": False, "used_percent": 20,
                },
                {
                    "node": "fuji1", "storage": "local-lvm",
                    "status": "available", "shared": False, "used_percent": 30,
                },
            ],
            "tasks": [],
        })

        state = queue.snapshot()
        self.assertEqual(
            {item["name"] for item in state["storages"]},
            {"depo/local-lvm", "fuji1/local-lvm"},
        )
        with queue.reserve(
            "snapshot", "depo-local", 1,
            storage_weights={"depo/local-lvm": 2}, timeout=0.1,
        ):
            with queue.reserve(
                "snapshot", "fuji-local", 1,
                storage_weights={"fuji1/local-lvm": 2}, timeout=0.1,
            ):
                active = queue.snapshot()
                self.assertEqual(active["active_count"], 2)
                by_name = {item["name"]: item for item in active["storages"]}
                self.assertEqual(by_name["depo/local-lvm"]["used"], 2)
                self.assertEqual(by_name["fuji1/local-lvm"]["used"], 2)

    def test_partial_task_failure_keeps_then_expires_last_external_tasks(self) -> None:
        queue = ProxmoxOperationQueue(10, telemetry_stale_seconds=0.05)
        queue.update_pressure({
            "sources": {"resources": True, "tasks": True},
            "nodes": [{"node": "depo", "status": "online", "cpu_ratio": 0.2}],
            "tasks": [{"upid": "UPID:depo:1", "node": "depo", "type": "qmclone"}],
        })
        self.assertEqual(queue.snapshot()["external_tasks_count"], 1)

        queue.update_pressure({
            "partial": True,
            "sources": {"resources": True, "tasks": False},
            "nodes": [{"node": "depo", "status": "online", "cpu_ratio": 0.2}],
            "tasks": [],
            "warnings": ["cluster.tasks: forbidden"],
        })
        self.assertEqual(queue.snapshot()["external_tasks_count"], 1)
        time.sleep(0.12)
        self.assertEqual(queue.snapshot()["external_tasks_count"], 0)

    def test_stale_task_source_uses_safe_degraded_cap_with_fresh_resources(self) -> None:
        queue = ProxmoxOperationQueue(
            10, control_reserve=2, telemetry_stale_seconds=0.05,
        )
        queue.update_pressure({
            "sources": {"resources": True, "tasks": True},
            "nodes": [{"node": "depo", "status": "online", "cpu_ratio": 0.2}],
            "tasks": [],
        })
        self.assertEqual(queue.snapshot()["effective_capacity"], 10)

        # The task sample expires, while a successful resources poll remains
        # current. Admission keeps a conservative uncertainty margin instead
        # of assuming that no external Proxmox jobs exist.
        time.sleep(0.12)
        queue.update_pressure({
            "partial": True,
            "sources": {"resources": True, "tasks": False},
            "nodes": [{"node": "depo", "status": "online", "cpu_ratio": 0.2}],
            "tasks": [],
            "warnings": ["cluster.tasks: timeout"],
        })
        snapshot = queue.snapshot()
        self.assertEqual(snapshot["effective_capacity"], 8)
        self.assertEqual(snapshot["pressure"]["state"], "degraded")
        self.assertTrue(snapshot["pressure"]["tasks_stale"])
        self.assertEqual(snapshot["pressure"]["degraded_capacity"], 8)
        # The degraded cap already leaves two configured slots for control
        # work. Do not subtract that same reserve a second time: the largest
        # normally legal heavy ticket must still make progress.
        self.assertEqual(snapshot["lanes"]["heavy"]["capacity"], 8)
        with queue.reserve("deploy", "large stand", 8, timeout=0.1):
            with queue.reserve("power", "emergency stop", 2, timeout=0.1):
                active = queue.snapshot()
                self.assertEqual(active["used"], 10)
                self.assertEqual(active["lanes"]["heavy"]["used"], 8)
                self.assertEqual(active["lanes"]["control"]["used"], 2)

    def test_stale_telemetry_fails_open_to_static_limits(self) -> None:
        queue = ProxmoxOperationQueue(10, telemetry_stale_seconds=0.05)
        queue.update_pressure({
            "sources": {"resources": True, "tasks": True},
            "nodes": [{"node": "depo", "status": "online", "cpu_percent": 98}],
            "tasks": [],
        })
        self.assertLess(queue.snapshot()["effective_capacity"], 10)
        time.sleep(0.12)

        snapshot = queue.snapshot()
        self.assertEqual(snapshot["effective_capacity"], 10)
        self.assertTrue(snapshot["pressure"]["stale"])

    def test_timeout_removes_waiting_ticket(self) -> None:
        queue = ProxmoxOperationQueue(4, control_reserve=0)
        entered = threading.Event()
        release = threading.Event()

        def active() -> None:
            with queue.reserve("deploy", "active", 4):
                entered.set()
                release.wait(2)

        thread = threading.Thread(target=active)
        thread.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(TimeoutError):
            with queue.reserve("deploy", "timeout", 4, timeout=0.03):
                pass
        self.assertEqual(queue.snapshot()["queued_count"], 0)
        release.set()
        thread.join(1)


if __name__ == "__main__":
    unittest.main()
