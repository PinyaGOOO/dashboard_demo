import unittest
from unittest.mock import MagicMock, call

from dashboard_backend.proxmox_gateway import DemoProxmoxGateway, LiveProxmoxGateway


class GatewayPoolInventoryTests(unittest.TestCase):
    def setUp(self):
        self.gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        self.gateway.client = MagicMock()

    def test_reads_each_requested_pool_once_and_identifies_only_qemu(self):
        pools = {
            "Shared": {
                "poolid": "Shared",
                "members": [
                    {"type": "lxc", "vmid": 777},
                    {"type": "qemu", "vmid": 50, "template": 1},
                    {"type": "qemu", "vmid": 102, "name": "lab", "node": "depo",
                     "status": "running", "cpu": 0.25, "mem": 4000, "maxmem": 10000},
                    {"type": "storage", "storage": "NAS"},
                    {"type": "qemu", "vmid": 101, "name": "other", "node": "fuji1", "status": "stopped"},
                ],
            },
            "shared": {"poolid": "shared", "members": []},
        }
        self.gateway.client.pools.side_effect = lambda name: MagicMock(get=lambda: pools[name])
        result = self.gateway.pool_inventory(["Shared", "shared", "Shared"])
        self.assertEqual(result["shared"], [])
        self.assertEqual([item["vmid"] for item in result["Shared"]], [101, 102])
        self.assertEqual(result["Shared"][1], {
            "vmid": 102, "name": "lab", "node": "depo", "status": "running",
            "ip": "", "cpu": 25.0, "ram": 40.0,
        })
        self.gateway.client.pools.assert_has_calls([call("Shared"), call("shared")], any_order=True)
        self.assertEqual(self.gateway.client.pools.call_count, 2)
        self.gateway.client.cluster.resources.get.assert_not_called()

    def test_incomplete_and_unreadable_pools_are_not_reported_as_empty(self):
        pools = {
            "complete": {"members": []},
            "no-members-key": {"comment": "legacy"},
            "invalid-entry": {"members": [{"type": "qemu"}]},
            "duplicate": {"members": [{"type": "qemu", "vmid": 100}] * 2},
            "wrong-id": {"poolid": "another", "members": []},
        }
        self.gateway.client.pools.side_effect = lambda name: MagicMock(get=lambda: pools[name])
        result = self.gateway.pool_inventory([*pools, "missing"])
        self.assertEqual(result, {"complete": []})

    def test_no_requested_pools_avoids_proxmox_reads(self):
        self.assertEqual(self.gateway.pool_inventory([]), {})
        self.gateway.client.pools.assert_not_called()

    def test_demo_inventory_omits_unmodelled_pools(self):
        gateway = DemoProxmoxGateway()
        result = gateway.pool_inventory(["existing-network-lab", "EXISTING-network-lab", "other"])
        self.assertEqual(result, {"existing-network-lab": gateway.pool_members("existing-network-lab")})


if __name__ == "__main__":
    unittest.main()
