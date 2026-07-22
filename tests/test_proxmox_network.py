from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, call, patch

from dashboard_backend.proxmox_gateway import LiveProxmoxGateway


class ProxmoxNetworkConfigurationTests(unittest.TestCase):
    def test_network_with_bridge_enables_nic_and_preserves_other_options(self) -> None:
        original = (
            "virtio=52:54:00:12:34:56,bridge=vmbr9,firewall=1,"
            "link_down=1,tag=20,rate=25"
        )

        overridden = LiveProxmoxGateway._network_with_bridge(original, "vmbr0")
        self.assertEqual(
            overridden,
            "virtio=52:54:00:12:34:56,firewall=1,tag=20,rate=25,bridge=vmbr0",
        )
        self.assertNotIn("link_down=", overridden)

        inherited = LiveProxmoxGateway._network_with_bridge(
            original.replace("link_down=1", "link_down=0"),
            "",
        )
        self.assertTrue(inherited.endswith("bridge=vmbr9"))
        self.assertIn("virtio=52:54:00:12:34:56", inherited)
        self.assertIn("firewall=1", inherited)
        self.assertIn("tag=20", inherited)
        self.assertIn("rate=25", inherited)
        self.assertNotIn("link_down=", inherited)

    def test_linux_network_target_recognizes_only_relevant_linux_bootstrap(self) -> None:
        gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        legacy_script = """#!/usr/bin/env bash
# Сетевой bootstrap из исходного vklvikl.py.
GUEST_INTERFACE="${GUEST_INTERFACE:-vmbr7}"
sed -i 's/old/new/' /etc/network/interfaces
ifup "$GUEST_INTERFACE"
"""

        with patch.dict(os.environ, {"PROXMOX_GUEST_INTERFACE": ""}):
            self.assertEqual(
                gateway._linux_network_target(
                    legacy_script,
                    "10.39.4.23",
                    "10.39.4.0/16",
                ),
                ("vmbr7", "10.39.4.23/16"),
            )
            self.assertIsNone(
                gateway._linux_network_target(
                    "# PowerShell bootstrap\n# Сетевой bootstrap из исходного vklvikl.py.\n"
                    "$ErrorActionPreference = 'Stop'",
                    "10.39.4.23",
                    "10.39.4.0/16",
                )
            )
            self.assertIsNone(
                gateway._linux_network_target(
                    "#!/usr/bin/env bash\necho 'обычная подготовка VM'",
                    "10.39.4.23",
                    "10.39.4.0/16",
                )
            )

        fixed_interface_script = """#!/usr/bin/env bash
# Сетевой bootstrap из исходного vklvikl.py.
GUEST_INTERFACE="ens18"
sed -i 's/old/new/' /etc/network/interfaces
ifup "$GUEST_INTERFACE"
"""
        with patch.dict(os.environ, {"PROXMOX_GUEST_INTERFACE": "vmbr0"}):
            self.assertEqual(
                gateway._linux_network_target(
                    fixed_interface_script,
                    "10.39.4.23",
                    "10.39.4.0/16",
                ),
                ("ens18", "10.39.4.23/16"),
            )


class ProxmoxNetworkReadinessTests(unittest.TestCase):
    @staticmethod
    def gateway() -> LiveProxmoxGateway:
        gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        gateway.client = MagicMock()
        gateway._wait_task = MagicMock()
        gateway._run_linux_network_readiness = MagicMock()
        return gateway

    def test_ready_guest_returns_without_reboot(self) -> None:
        gateway = self.gateway()
        gateway._run_linux_network_readiness.return_value = {
            "exit_code": 0,
            "stdout": "BOOT_ID=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n"
            "NETWORK_READY vmbr0 10.39.4.23/16\n",
            "stderr": "",
        }

        gateway._ensure_linux_guest_network(
            "pve-a", 501, "vmbr0", "10.39.4.23/16"
        )

        gateway._run_linux_network_readiness.assert_called_once_with(
            "pve-a",
            501,
            "vmbr0",
            "10.39.4.23/16",
            repair=True,
            timeout=75,
        )
        gateway.client.nodes.assert_not_called()
        gateway._wait_task.assert_not_called()

    def test_soft_failure_reboots_once_and_waits_for_new_boot_id(self) -> None:
        gateway = self.gateway()
        old_boot_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        new_boot_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        gateway._run_linux_network_readiness.side_effect = [
            {
                "exit_code": 1,
                "stdout": f"BOOT_ID={old_boot_id}\n",
                "stderr": "vmbr0 is down",
            },
            {
                # A successful probe from the old boot must not be accepted.
                "exit_code": 0,
                "stdout": f"BOOT_ID={old_boot_id}\n"
                "NETWORK_READY vmbr0 10.39.4.23/16\n",
                "stderr": "",
            },
            {
                "exit_code": 0,
                "stdout": f"BOOT_ID={new_boot_id}\n"
                "NETWORK_READY vmbr0 10.39.4.23/16\n",
                "stderr": "",
            },
        ]
        reboot = gateway.client.nodes.return_value.qemu.return_value.status.reboot.post
        reboot.return_value = "UPID:pve-a:reboot"

        monotonic_values = iter([0, 1, 2, 3, 4, 5])
        with (
            patch.dict(os.environ, {"PROXMOX_NETWORK_READY_TIMEOUT": "30"}),
            patch(
                "dashboard_backend.proxmox_gateway.time.monotonic",
                side_effect=lambda: next(monotonic_values),
            ),
            patch("dashboard_backend.proxmox_gateway.time.sleep") as sleep,
        ):
            gateway._ensure_linux_guest_network(
                "pve-a", 501, "vmbr0", "10.39.4.23/16"
            )

        reboot.assert_called_once_with()
        gateway._wait_task.assert_called_once_with(
            "pve-a", "UPID:pve-a:reboot", timeout=180
        )
        gateway._run_linux_network_readiness.assert_has_calls(
            [
                call(
                    "pve-a",
                    501,
                    "vmbr0",
                    "10.39.4.23/16",
                    repair=True,
                    timeout=75,
                ),
                call(
                    "pve-a",
                    501,
                    "vmbr0",
                    "10.39.4.23/16",
                    repair=False,
                    timeout=20,
                ),
                call(
                    "pve-a",
                    501,
                    "vmbr0",
                    "10.39.4.23/16",
                    repair=False,
                    timeout=20,
                ),
            ]
        )
        self.assertEqual(gateway._run_linux_network_readiness.call_count, 3)
        sleep.assert_called_once_with(2)

    def test_configuration_error_does_not_reboot(self) -> None:
        gateway = self.gateway()
        gateway._run_linux_network_readiness.return_value = {
            "exit_code": 2,
            "stdout": "BOOT_ID=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n",
            "stderr": "bad /etc/network/interfaces syntax",
        }

        with self.assertRaises(RuntimeError) as raised:
            gateway._ensure_linux_guest_network(
                "pve-a", 501, "vmbr0", "10.39.4.23/16"
            )

        self.assertIn("ошибка конфигурации", str(raised.exception))
        self.assertIn("bad /etc/network/interfaces syntax", str(raised.exception))
        gateway.client.nodes.assert_not_called()
        gateway._wait_task.assert_not_called()
        self.assertEqual(gateway._run_linux_network_readiness.call_count, 1)


if __name__ == "__main__":
    unittest.main()
