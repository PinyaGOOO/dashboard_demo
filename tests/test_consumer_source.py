import unittest
from unittest.mock import MagicMock, call, patch

from dashboard_backend.proxmox_gateway import DemoProxmoxGateway, LiveProxmoxGateway


class ConsumerResourceSourceTests(unittest.TestCase):
    def setUp(self):
        self.gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        self.gateway.client = MagicMock()

    def test_one_get_for_entire_visible_inventory_and_preserves_missing_counters(self):
        rows = [
            {"type": "node", "node": "pve-a"},
            {"type": "qemu", "vmid": 123, "pool": "other-team"},
            {"type": "lxc", "vmid": 234, "diskread": 100, "diskwrite": 200},
            {"type": "storage", "storage": "nas", "node": "pve-b"},
            {"type": "pool", "pool": "other-team"},
        ]
        self.gateway.client.cluster.resources.get.return_value = rows
        self.assertEqual(self.gateway.consumer_resources(), rows)
        self.assertEqual(self.gateway.client.mock_calls, [call.cluster.resources.get()])

    def test_failed_or_invalid_collection_is_not_a_successful_empty_inventory(self):
        self.gateway.client.cluster.resources.get.side_effect = PermissionError("denied")
        with self.assertRaises(PermissionError):
            self.gateway.consumer_resources()
        self.gateway.client.cluster.resources.get.side_effect = None
        for bad in ({}, None, ["qemu/123"]):
            with self.subTest(bad=bad):
                self.gateway.client.cluster.resources.get.return_value = bad
                with self.assertRaises(RuntimeError):
                    self.gateway.consumer_resources()
        self.gateway.client.cluster.resources.get.return_value = []
        self.assertEqual(self.gateway.consumer_resources(), [])

    @patch("dashboard_backend.proxmox_gateway.time.monotonic", side_effect=[10., 40.])
    def test_demo_uses_running_guest_byte_counters_and_contains_non_stand_guests(self, _clock):
        demo = DemoProxmoxGateway()
        before = demo.consumer_resources()
        after = demo.consumer_resources()
        self.assertTrue({"qemu", "lxc", "node", "storage"} <= {r["type"] for r in before})
        first = {r["vmid"]: r for r in before if "vmid" in r}
        self.assertIn("data-platform", {r.get("pool") for r in before})
        self.assertIn("de24-g2is", {r.get("pool") for r in before})
        for row in after:
            if row.get("type") == "node":
                self.assertIsInstance(row["mem"], int)
                self.assertIsInstance(row["disk"], int)
            if row.get("status") == "running":
                for field in ("diskread", "diskwrite", "netin", "netout"):
                    self.assertIsInstance(row[field], int)
                    self.assertGreater(row[field], first[row["vmid"]][field])
                self.assertEqual(row["uptime"] - first[row["vmid"]]["uptime"], 30)
        collector = next(row for row in after if row.get("vmid") == 2303)
        self.assertEqual(collector["netin"] - first[2303]["netin"], 14 * 1024**2 * 30)
        self.assertEqual(collector["netout"] - first[2303]["netout"], 1024**2 * 30 // 2)
