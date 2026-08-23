from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dashboard_backend.database import DashboardStore
from dashboard_backend.proxmox_gateway import LiveProxmoxGateway
from dashboard_backend.service import ConflictError, DashboardService, ValidationError


class ExistingPoolServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = DashboardStore(Path(self.tempdir.name) / "dashboard.db")
        self.gateway = MagicMock()
        self.gateway.mode = "live"
        self.gateway.list_pools.return_value = [
            {"pool_id": "shared-lab", "comment": "Общий pool", "vm_count": 3},
            {"pool_id": "Templates-MDK-02-01", "comment": "Шаблоны", "vm_count": 1},
        ]
        self.service = DashboardService(self.store, self.gateway)
        self.blueprint = next(
            item for item in self.service.list_blueprints() if item["status"] == "active"
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_create_in_existing_pool_marks_origin_and_uses_unique_names(self) -> None:
        with patch("dashboard_backend.service.threading.Thread") as thread:
            stand = self.service.create_stand({
                "blueprint_id": self.blueprint["id"],
                "name": "Новая группа",
                "pool_id": "shared-lab",
                "use_existing_pool": True,
                "vm_count": 2,
            })

        self.assertEqual(stand["origin"], "existing")
        self.assertEqual(stand["pool_id"], "shared-lab")
        self.assertEqual(
            [vm["name"] for vm in stand["vms"]],
            [
                f"shared-lab-deployer-{stand['id']}-1",
                f"shared-lab-deployer-{stand['id']}-2",
            ],
        )
        thread.return_value.start.assert_called_once_with()

    def test_existing_pool_id_preserves_proxmox_letter_case(self) -> None:
        with patch("dashboard_backend.service.threading.Thread") as thread:
            stand = self.service.create_stand({
                "blueprint_id": self.blueprint["id"],
                "name": "Стенд со смешанным регистром",
                "pool_id": "Templates-MDK-02-01",
                "use_existing_pool": True,
                "vm_count": 1,
            })

        self.assertEqual(stand["pool_id"], "Templates-MDK-02-01")
        self.assertEqual(
            stand["vms"][0]["name"],
            f"Templates-MDK-02-01-deployer-{stand['id']}-1",
        )
        thread.return_value.start.assert_called_once_with()

    def test_multiple_stands_can_share_explicit_existing_pool(self) -> None:
        with patch("dashboard_backend.service.threading.Thread"):
            first = self.service.create_stand({
                "blueprint_id": self.blueprint["id"],
                "name": "Первая группа",
                "pool_id": "shared-lab",
                "use_existing_pool": True,
                "vm_count": 1,
            })
            second = self.service.create_stand({
                "blueprint_id": self.blueprint["id"],
                "name": "Вторая группа",
                "pool_id": "shared-lab",
                "use_existing_pool": True,
                "vm_count": 1,
            })

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["pool_id"], second["pool_id"])
        self.assertNotEqual(first["vms"][0]["name"], second["vms"][0]["name"])
        pool = next(item for item in self.service.list_pools() if item["pool_id"] == "shared-lab")
        self.assertTrue(pool["available_for_deploy"])
        self.assertEqual(pool["stand_count"], 2)
        self.assertEqual(pool["stand_ids"], [first["id"], second["id"]])

    def test_dashboard_owned_pool_cannot_be_reused_as_shared_destination(self) -> None:
        self.store.execute(
            """INSERT INTO stands
            (name, status, pool_id, origin, created_at, updated_at)
            VALUES ('Managed', 'running', 'shared-lab', 'deployed', '', '')"""
        )

        with patch("dashboard_backend.service.threading.Thread"), self.assertRaisesRegex(
            ConflictError, "управляется Deployer",
        ):
            self.service.create_stand({
                "blueprint_id": self.blueprint["id"],
                "name": "Нельзя добавить",
                "pool_id": "shared-lab",
                "use_existing_pool": True,
                "vm_count": 1,
            })

    def test_unknown_existing_pool_is_rejected_before_job_creation(self) -> None:
        with self.assertRaisesRegex(ValidationError, "не найден"):
            self.service.create_stand({
                "blueprint_id": self.blueprint["id"],
                "name": "Новая группа",
                "pool_id": "missing-pool",
                "use_existing_pool": True,
            })

    def test_imported_pool_can_delete_all_current_vms_without_removing_pool_card(self) -> None:
        self.gateway.pool_members.return_value = [
            {"vmid": 410, "name": "router-1", "node": "pve-1", "status": "running"},
            {"vmid": 411, "name": "router-2", "node": "pve-2", "status": "stopped"},
        ]
        stand = self.service.import_pool({
            "pool_id": "shared-lab",
            "name": "Подключённый pool",
            "blueprint_id": self.blueprint["id"],
        })

        result = self.service.stand_action(stand["id"], "delete_pool_stands")

        self.assertEqual(result["deleted_count"], 2)
        self.assertEqual(result["stand"]["status"], "stopped")
        self.assertEqual(result["stand"]["vms"], [])
        deletion_scope, vmids = self.gateway.delete_stand.call_args.args
        self.assertEqual(deletion_scope["origin"], "existing")
        self.assertEqual(vmids, [410, 411])
        self.assertEqual(self.service.get_stand(stand["id"])["pool_id"], "shared-lab")


class ExistingPoolGatewaySafetyTests(unittest.TestCase):
    def test_failed_deploy_cleanup_removes_only_matching_new_vm(self) -> None:
        gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        gateway.client = MagicMock()
        gateway._wait_tasks = MagicMock()
        pool_endpoint = MagicMock()
        pool_endpoint.get.return_value = {
            "members": [
                {"type": "qemu", "vmid": 100, "name": "shared-lab-deployer-7-1", "node": "pve-1"},
                {"type": "qemu", "vmid": 200, "name": "legacy-vm", "node": "pve-2"},
            ],
        }
        gateway.client.pools.return_value = pool_endpoint
        managed_api = MagicMock()
        gateway.client.nodes.return_value.qemu.return_value = managed_api
        gateway.client.cluster.resources.get.return_value = [
            {"type": "qemu", "vmid": 200, "name": "legacy-vm", "node": "pve-2"},
        ]

        remaining, errors = gateway._cleanup_failed_deploy(
            [("pve-1", 100)],
            "shared-lab",
            "unused-marker",
            preserve_pool=True,
            expected_names={100: "shared-lab-deployer-7-1"},
        )

        self.assertEqual(remaining, [])
        self.assertEqual(errors, [])
        managed_api.delete.assert_called_once_with(purge=1)
        pool_endpoint.delete.assert_not_called()

    def test_delete_removes_only_tracked_vm_and_preserves_pool(self) -> None:
        gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        gateway.client = MagicMock()
        gateway._wait_tasks = MagicMock()
        pool_endpoint = MagicMock()
        pool_endpoint.get.return_value = {
            "comment": "Чужой существующий pool",
            "members": [
                {"type": "qemu", "vmid": 100, "name": "shared-lab-deployer-7-1", "node": "pve-1"},
                {"type": "qemu", "vmid": 200, "name": "legacy-vm", "node": "pve-2"},
            ],
        }
        gateway.client.pools.return_value = pool_endpoint
        managed_api = MagicMock()
        gateway.client.nodes.return_value.qemu.return_value = managed_api
        gateway._vm_inventory = MagicMock(return_value={
            100: {"node": "pve-1", "status": "running"},
        })

        gateway.delete_stand({
            "id": 7,
            "pool_id": "shared-lab",
            "origin": "existing",
            "status": "running",
            "vms": [{"vmid": 100, "name": "shared-lab-deployer-7-1"}],
        }, [100])

        managed_api.status.stop.post.assert_called_once_with()
        managed_api.delete.assert_called_once_with(purge=1)
        pool_endpoint.delete.assert_not_called()
        gateway._vm_inventory.assert_called_once_with([100])

    def test_delete_refuses_reused_vmid_with_different_name(self) -> None:
        gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        gateway.client = MagicMock()
        pool_endpoint = MagicMock()
        pool_endpoint.get.return_value = {
            "members": [
                {"type": "qemu", "vmid": 100, "name": "someone-elses-vm", "node": "pve-1"},
            ],
        }
        gateway.client.pools.return_value = pool_endpoint

        with self.assertRaisesRegex(RuntimeError, "удаление отменено"):
            gateway.delete_stand({
                "id": 7,
                "pool_id": "shared-lab",
                "origin": "existing",
                "status": "running",
                "vms": [{"vmid": 100, "name": "shared-lab-deployer-7-1"}],
            }, [100])

        gateway.client.nodes.assert_not_called()
        pool_endpoint.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
