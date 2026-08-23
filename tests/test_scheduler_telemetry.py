from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock

from dashboard_backend.proxmox_gateway import (
    DemoProxmoxGateway,
    LiveProxmoxGateway,
    _normalize_scheduler_resources,
    _normalize_scheduler_tasks,
)


def _live_gateway(resources: object, tasks: object) -> LiveProxmoxGateway:
    gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
    gateway.client = MagicMock()
    gateway.client.cluster.resources.get.return_value = resources
    gateway.client.cluster.tasks.get.return_value = tasks
    gateway._scheduler_snapshot_lock = threading.RLock()
    gateway._scheduler_snapshot_cache_at = 0.0
    gateway._scheduler_snapshot_cache = None
    return gateway


class SchedulerNormalizationTests(unittest.TestCase):
    def test_resources_are_normalized_and_storage_is_not_node_rootfs(self) -> None:
        nodes, storages = _normalize_scheduler_resources([
            {
                "type": "node", "node": "fuji1", "status": "online",
                "cpu": "0.25", "maxcpu": "16", "mem": "75", "maxmem": "100",
                "disk": "90", "maxdisk": "100", "uptime": "86400",
            },
            {
                "type": "storage", "node": "fuji1", "storage": "NAS1",
                "status": "available", "shared": "1", "disk": "600",
                "maxdisk": "1000", "avail": "400", "content": "images",
                "plugintype": "nfs",
            },
            {"type": "qemu", "node": "fuji1", "vmid": 100},
            None,
        ])

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["node"], "fuji1")
        self.assertEqual(nodes[0]["cpu_percent"], 25.0)
        self.assertEqual(nodes[0]["memory_percent"], 75.0)
        self.assertEqual(nodes[0]["root_used_ratio"], 0.9)
        self.assertNotIn("storage", nodes[0])
        self.assertEqual(storages, [{
            "node": "fuji1", "storage": "NAS1", "storage_id": "NAS1",
            "scheduler_key": "NAS1", "status": "available",
            "available": True, "shared": True, "content": "images",
            "storage_type": "nfs", "used": 600.0, "total": 1000.0,
            "free": 400.0, "used_ratio": 0.6, "used_percent": 60.0,
        }])

    def test_local_storage_scheduler_keys_are_qualified_by_node(self) -> None:
        _, storages = _normalize_scheduler_resources([
            {
                "type": "storage", "node": "depo", "storage": "local-lvm",
                "status": "available", "shared": 0, "disk": 10, "maxdisk": 100,
            },
            {
                "type": "storage", "node": "fuji1", "storage": "local-lvm",
                "status": "available", "shared": "0", "disk": 20, "maxdisk": 100,
            },
        ])

        self.assertEqual(
            [item["scheduler_key"] for item in storages],
            ["depo/local-lvm", "fuji1/local-lvm"],
        )

    def test_active_tasks_are_sanitized_deduplicated_and_sorted(self) -> None:
        tasks = _normalize_scheduler_tasks([
            {
                "upid": "UPID:fuji2:2", "node": "fuji2", "type": "QMCLONE",
                "id": "278", "user": "deployer@pve", "tokenid": "dashboard",
                "starttime": "20",
            },
            {
                "upid": "UPID:fuji1:1", "node": "fuji1", "type": "qmstart",
                "id": "not-a-vmid", "starttime": 10, "status": "RUNNING",
            },
            {"node": "fuji3", "type": "vzdump"},
        ])

        self.assertEqual([task["type"] for task in tasks], ["qmstart", "qmclone"])
        self.assertIsNone(tasks[0]["vmid"])
        self.assertEqual(tasks[1]["vmid"], 278)
        self.assertEqual(tasks[1]["token_id"], "dashboard")
        self.assertEqual(tasks[0]["node"], "fuji1")
        self.assertEqual(tasks[0]["worker_node"], "fuji1")
        self.assertEqual(tasks[1]["node"], "")
        self.assertEqual(tasks[1]["worker_node"], "fuji2")
        self.assertTrue(all(task["running"] for task in tasks))

    def test_clone_and_migrate_worker_nodes_are_not_treated_as_targets(self) -> None:
        tasks = _normalize_scheduler_tasks([
            {
                "upid": "UPID:depo:1", "node": "depo", "type": "qmclone",
                "id": "278", "starttime": 1,
            },
            {
                "upid": "UPID:fuji1:2", "node": "fuji1", "type": "qmigrate",
                "id": "279", "starttime": 2,
            },
        ])

        self.assertEqual([task["node"] for task in tasks], ["", ""])
        self.assertEqual(
            [task["worker_node"] for task in tasks],
            ["depo", "fuji1"],
        )


class SchedulerSnapshotTests(unittest.TestCase):
    def test_live_snapshot_is_cached_for_five_seconds_and_defensively_copied(self) -> None:
        gateway = _live_gateway(
            [{
                "type": "node", "node": "depo", "status": "online",
                "cpu": 0.1, "maxcpu": 8, "mem": 5, "maxmem": 10,
            }],
            [{
                "upid": "UPID:depo:1", "node": "depo", "type": "qmclone",
                "id": "100", "starttime": 1,
            }],
        )

        first = gateway.scheduler_snapshot()
        first["nodes"][0]["node"] = "mutated"
        second = gateway.scheduler_snapshot()

        self.assertEqual(second["nodes"][0]["node"], "depo")
        self.assertEqual(second["tasks"][0]["type"], "qmclone")
        self.assertEqual(gateway.client.cluster.resources.get.call_count, 1)
        self.assertEqual(gateway.client.cluster.tasks.get.call_count, 1)
        self.assertFalse(second["partial"])

    def test_one_failed_endpoint_returns_the_successful_half(self) -> None:
        gateway = _live_gateway([], [{
            "upid": "UPID:depo:1", "node": "depo", "type": "qmstop",
            "id": "100", "starttime": 1,
        }])
        gateway.client.cluster.resources.get.side_effect = RuntimeError("forbidden")

        snapshot = gateway.scheduler_snapshot()

        self.assertTrue(snapshot["available"])
        self.assertTrue(snapshot["partial"])
        self.assertEqual(snapshot["sources"], {"resources": False, "tasks": True})
        self.assertEqual(snapshot["nodes"], [])
        self.assertEqual(snapshot["tasks"][0]["type"], "qmstop")
        self.assertIn("cluster.resources", snapshot["warnings"][0])

    def test_total_transient_failure_returns_explicitly_stale_previous_sample(self) -> None:
        gateway = _live_gateway(
            [{
                "type": "node", "node": "depo", "status": "online",
                "cpu": 0.2, "maxcpu": 8, "mem": 5, "maxmem": 10,
            }],
            [],
        )
        original = gateway.scheduler_snapshot()
        gateway._scheduler_snapshot_cache_at = 0.0
        gateway.client.cluster.resources.get.side_effect = RuntimeError("offline")
        gateway.client.cluster.tasks.get.side_effect = RuntimeError("offline")

        stale = gateway.scheduler_snapshot()

        self.assertTrue(stale["available"])
        self.assertTrue(stale["stale"])
        self.assertTrue(stale["partial"])
        self.assertEqual(stale["sources"], {"resources": False, "tasks": False})
        self.assertEqual(stale["nodes"], original["nodes"])
        self.assertEqual(len(stale["warnings"]), 2)

    def test_demo_gateway_exposes_compatible_snapshot_and_scope(self) -> None:
        gateway = DemoProxmoxGateway()

        snapshot = gateway.scheduler_snapshot()
        scope = gateway.deployment_scheduler_scope(
            {"node": "pve-02"},
            {"vm_count": 5},
        )

        self.assertTrue(snapshot["available"])
        self.assertEqual(len(snapshot["nodes"]), 4)
        self.assertEqual(scope["target_nodes"], ["pve-02"])
        self.assertEqual(scope["node_weights"], {"pve-02": 5})
        self.assertEqual(scope["storage_weights"], {"demo-shared": 5})


class DeploymentSchedulerScopeTests(unittest.TestCase):
    def test_live_scope_fails_before_storage_discovery_when_template_node_is_offline(self) -> None:
        gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        gateway._find_template_node = MagicMock(return_value="depo")
        gateway._rank_nodes = MagicMock(return_value=["fuji1", "fuji2"])
        gateway._linked_clone_nodes = MagicMock()

        with self.assertRaisesRegex(RuntimeError, "VMID 278.*depo.*недоступна"):
            gateway.deployment_scheduler_scope(
                {"node": "auto"},
                {"template_vmid": 278, "vm_count": 5},
            )

        gateway._linked_clone_nodes.assert_not_called()

    def test_live_scope_reuses_verified_linked_clone_placement(self) -> None:
        gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        gateway.client = MagicMock()
        gateway.client.storage.get.return_value = [
            {"storage": "NAS1", "shared": 1},
            {"storage": "iso", "shared": 1},
        ]
        gateway._find_template_node = MagicMock(return_value="depo")
        gateway._rank_nodes = MagicMock(return_value=["fuji1", "depo", "fuji2"])
        gateway._linked_clone_nodes = MagicMock(
            return_value=(["fuji1", "depo"], ["NAS1", "iso"], ["NAS1"]),
        )

        scope = gateway.deployment_scheduler_scope(
            {"node": "auto"},
            {"template_vmid": 278, "vm_count": 5},
        )

        self.assertEqual(scope["target_nodes"], ["fuji1", "depo"])
        self.assertEqual(scope["node_weights"], {"fuji1": 3, "depo": 2})
        self.assertEqual(scope["storages"], ["NAS1"])
        self.assertEqual(scope["storage_weights"], {"NAS1": 5})
        self.assertEqual(scope["template_node"], "depo")

    def test_live_scope_honors_explicit_verified_node(self) -> None:
        gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        gateway.client = MagicMock()
        gateway.client.storage.get.return_value = [{"storage": "NAS1", "shared": 1}]
        gateway._find_template_node = MagicMock(return_value="depo")
        gateway._rank_nodes = MagicMock(return_value=["fuji1", "depo"])
        gateway._linked_clone_nodes = MagicMock(
            return_value=(["fuji1", "depo"], ["NAS1"], ["NAS1"]),
        )

        scope = gateway.deployment_scheduler_scope(
            {"node": "depo"},
            {"template_vmid": 278, "vm_count": 3},
        )

        self.assertEqual(scope["target_nodes"], ["depo"])
        self.assertEqual(scope["node_weights"], {"depo": 3})
        self.assertEqual(scope["storage_weights"], {"NAS1": 3})

    def test_live_scope_qualifies_local_storage_for_actual_target_node(self) -> None:
        gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        gateway.client = MagicMock()
        gateway.client.storage.get.return_value = [
            {"storage": "local-lvm", "shared": 0},
        ]
        gateway._find_template_node = MagicMock(return_value="depo")
        gateway._rank_nodes = MagicMock(return_value=["fuji1", "depo"])
        gateway._linked_clone_nodes = MagicMock(
            return_value=(["depo"], ["local-lvm"], ["local-lvm"]),
        )

        scope = gateway.deployment_scheduler_scope(
            {"node": "auto"},
            {"template_vmid": 278, "vm_count": 5},
        )

        self.assertEqual(scope["target_nodes"], ["depo"])
        self.assertEqual(scope["node_weights"], {"depo": 5})
        self.assertEqual(scope["storages"], ["depo/local-lvm"])
        self.assertEqual(scope["storage_weights"], {"depo/local-lvm": 5})


if __name__ == "__main__":
    unittest.main()
