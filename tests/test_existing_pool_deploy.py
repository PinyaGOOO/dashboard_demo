from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
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

    def test_deploy_repeats_preflight_and_reservation_after_long_queue_wait(self) -> None:
        first_scope = {
            "node_weights": {"pve-1": 1},
            "storage_weights": {"shared-lab": 1},
            "template_node": "pve-1",
            "template_vmid": int(self.blueprint["template_vmid"]),
            "target_nodes": ["pve-1"],
        }
        second_scope = {
            "node_weights": {"pve-2": 1},
            "storage_weights": {"shared-lab": 1},
            "template_node": "pve-2",
            "template_vmid": int(self.blueprint["template_vmid"]),
            "target_nodes": ["pve-2"],
        }
        self.gateway.deployment_scheduler_scope.side_effect = [first_scope, second_scope]
        with patch("dashboard_backend.service.threading.Thread") as thread:
            stand = self.service.create_stand({
                "blueprint_id": self.blueprint["id"],
                "name": "Повторный preflight",
                "pool_id": "shared-lab",
                "use_existing_pool": True,
                "vm_count": 1,
                "subnet": "10.39.11.1/16",
                "start_ip": "10.39.11.1",
            })
        deployment = thread.call_args.kwargs["args"][1]
        reserve_scopes: list[dict[str, object]] = []

        @contextmanager
        def reserve(_kind, _label, _weight, **kwargs):
            reserve_scopes.append(dict(kwargs))
            yield {"wait_seconds": 6.0 if len(reserve_scopes) == 1 else 0.0}

        def deploy(_stand, selected_blueprint, _progress):
            self.assertEqual(selected_blueprint["_scheduler_scope"]["target_nodes"], ["pve-2"])
            return [{
                "index": 1,
                "vmid": 517,
                "name": f"shared-lab-deployer-{stand['id']}-1",
                "node": "pve-2",
                "status": "running",
                "ip": selected_blueprint["allocated_ips"][0],
                "username": "root",
                "web_username": "root@pam",
                "password": "SafePass2",
                "snapshot": "start",
            }]

        self.gateway.deploy.side_effect = deploy
        with patch.object(self.service._operation_queue, "reserve", side_effect=reserve):
            self.service._deploy_job(stand["id"], deployment)

        self.assertEqual(self.gateway.deployment_scheduler_scope.call_count, 2)
        self.assertEqual(reserve_scopes[0]["node_weights"], {"pve-1": 1})
        self.assertEqual(reserve_scopes[1]["node_weights"], {"pve-2": 1})
        self.gateway.deploy.assert_called_once()
        refreshed = self.service.get_stand(stand["id"])
        self.assertEqual(refreshed["status"], "running", refreshed.get("last_error"))

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

    def test_existing_pool_stand_is_assigned_to_selected_workspace(self) -> None:
        with patch("dashboard_backend.service.threading.Thread"):
            stand = self.service.create_stand({
                "blueprint_id": self.blueprint["id"],
                "name": "МДК 02.01",
                "pool_id": "shared-lab",
                "use_existing_pool": True,
                "workspace": "mdk02.01",
                "vm_count": 1,
            })

        self.assertEqual(stand["workspace"], "mdk02.01")
        pool = next(item for item in self.service.list_pools() if item["pool_id"] == "shared-lab")
        self.assertEqual(pool["workspaces"], ["mdk02.01"])

    def test_unknown_workspace_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "рабочая область"):
            self.service.create_stand({
                "blueprint_id": self.blueprint["id"],
                "name": "Неизвестный раздел",
                "pool_id": "shared-lab",
                "use_existing_pool": True,
                "workspace": "other",
            })

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

    def test_pool_delete_rejects_migration_after_scheduler_preflight(self) -> None:
        original = [
            {"vmid": 415, "name": "router", "node": "pve-1", "status": "running"},
        ]
        migrated = [
            {"vmid": 415, "name": "router", "node": "pve-2", "status": "running"},
        ]
        self.gateway.pool_members.side_effect = [original, original, migrated]
        stand = self.service.import_pool({"pool_id": "shared-lab"})

        with self.assertRaisesRegex(ConflictError, "Состав или размещение VM"):
            self.service.stand_action(stand["id"], "delete_pool_stands")

        self.gateway.delete_stand.assert_not_called()
        queue = self.service.operation_queue()
        self.assertEqual(queue["used"], 0)
        self.assertEqual(queue["queued_count"], 0)

    def test_pool_delete_rejects_reused_vmid_after_scheduler_preflight(self) -> None:
        original = [
            {"vmid": 416, "name": "owned-router", "node": "pve-1", "status": "running"},
        ]
        replacement = [
            {"vmid": 416, "name": "unrelated-router", "node": "pve-1", "status": "running"},
        ]
        self.gateway.pool_members.side_effect = [original, original, replacement]
        stand = self.service.import_pool({"pool_id": "shared-lab"})

        with self.assertRaisesRegex(ConflictError, "Состав или размещение VM"):
            self.service.stand_action(stand["id"], "delete_pool_stands")

        self.gateway.delete_stand.assert_not_called()
        self.assertEqual(self.service.operation_queue()["used"], 0)

    def test_import_pool_does_not_require_display_metadata_or_blueprint(self) -> None:
        self.gateway.pool_members.return_value = [
            {"vmid": 412, "name": "existing-vm", "node": "pve-1", "status": "running"},
        ]

        stand = self.service.import_pool({"pool_id": "monitor-only", "workspace": "mdk02.01"})

        self.assertEqual(stand["name"], "monitor-only")
        self.assertEqual(stand["owner"], "Администратор")
        self.assertEqual(stand["workspace"], "mdk02.01")
        self.assertIsNone(stand["blueprint_id"])

    def test_deployer_only_vm_ip_override_updates_metadata_and_ipam(self) -> None:
        self.gateway.pool_members.return_value = [
            {"vmid": 420, "name": "existing-vm", "node": "pve-1", "status": "running", "ip": "172.31.250.10"},
        ]
        stand = self.service.import_pool({"pool_id": "metadata-ip"})
        gateway_calls_before_update = list(self.gateway.method_calls)

        updated = self.service.update_stand(stand["id"], {
            "name": "Новый адрес Deployer",
            "vm_ips": {"420": "172.31.250.11"},
        })

        self.assertEqual(updated["vms"][0]["ip"], "172.31.250.11")
        reservation = self.store.query_one(
            "SELECT address FROM ipam_reservations WHERE stand_id = ?", (stand["id"],),
        )
        self.assertEqual(reservation["address"], "172.31.250.11")
        self.assertEqual(self.gateway.method_calls, gateway_calls_before_update)

    def test_deployer_only_vm_ip_override_rejects_address_from_another_stand(self) -> None:
        self.gateway.pool_members.return_value = [
            {"vmid": 421, "name": "first", "node": "pve-1", "status": "running", "ip": "172.31.251.10"},
        ]
        first = self.service.import_pool({"pool_id": "first-ip-pool"})
        self.gateway.pool_members.return_value = [
            {"vmid": 422, "name": "second", "node": "pve-2", "status": "running", "ip": "172.31.251.11"},
        ]
        second = self.service.import_pool({"pool_id": "second-ip-pool"})

        with self.assertRaisesRegex(ConflictError, "уже используется"):
            self.service.update_stand(first["id"], {
                "vm_ips": {"421": second["vms"][0]["ip"]},
            })

        self.assertEqual(self.service.get_stand(first["id"])["vms"][0]["ip"], "172.31.251.10")

    def test_deployer_only_vm_ip_override_validates_ipv4(self) -> None:
        self.gateway.pool_members.return_value = [
            {"vmid": 423, "name": "invalid-ip-test", "node": "pve-1", "status": "running", "ip": "172.31.252.10"},
        ]
        stand = self.service.import_pool({"pool_id": "invalid-ip-pool"})

        with self.assertRaisesRegex(ValidationError, "некорректный IPv4"):
            self.service.update_stand(stand["id"], {"vm_ips": {"423": "999.1.1.1"}})


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
