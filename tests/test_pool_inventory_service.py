import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from dashboard_backend.database import DashboardStore, utc_now
from dashboard_backend.service import DashboardService


def member(vmid, name, status="running"):
    return {"vmid": vmid, "name": name, "node": "test-node", "status": status}


class PoolObservationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = DashboardStore(Path(temp.name) / "test.db", seed_demo=False)
        self.gateway = MagicMock(mode="live")
        self.service = DashboardService(self.store, self.gateway)
        self.gateway.pool_members.return_value = [member(201, "initial")]
        self.sid = self.service.import_pool({"pool_id": "Lab"})["id"]

    def refresh(self, snapshot):
        self.gateway.pool_inventory.return_value = snapshot
        self.service.pool_inventory.refresh()
        return self.service.get_stand(self.sid, include_pool_inventory=True)

    def test_manual_add_move_out_and_legacy_managed_scope(self):
        self.refresh({"Lab": [member(201, "initial")]})
        new = member(202, "new")
        view = self.refresh({"Lab": [member(201, "initial"), new]})
        self.assertEqual(view["pool_inventory_vmids"], [201, 202])
        self.assertEqual(view["pool_extra_vms"], [new])
        self.assertEqual([vm["vmid"] for vm in view["vms"]], [201])
        self.assertEqual(self.service.list_stands(include_pool_inventory=True)[0]["pool_extra_vm_count"], 1)
        self.service.stand_action(self.sid, "stop")
        self.gateway.power_action.assert_called_once_with([201], "stop")
        self.assertEqual(self.refresh({"Lab": []})["pool_extra_vms"], [])

    def test_failed_read_preserves_last_observation_and_disables_delete_confirmation(self):
        self.assertEqual(self.refresh({})["pool_inventory_state"], "unavailable")
        self.refresh({"Lab": [member(202, "new")]})
        view = self.refresh({})
        self.assertEqual(view["pool_inventory_state"], "stale")
        self.assertEqual(view["pool_extra_vm_count"], 1)
        self.assertNotIn("pool_inventory_vmids", view)
        changed = self.refresh({"Lab": [member(202, "renamed", "stopped")]})
        self.assertEqual(changed["pool_extra_vms"], [member(202, "renamed", "stopped")])
        self.assertEqual(changed["pool_inventory_state"], "fresh")

    def test_shared_pool_excludes_sibling_and_in_flight_clones(self):
        self.store.execute("UPDATE stands SET origin='existing' WHERE id=?", (self.sid,))
        sibling = self.store.execute(
            """INSERT INTO stands(name,origin,pool_id,created_at,updated_at)
               VALUES('second','existing','Lab',?,?)""", (utc_now(), utc_now()),
        )
        for vmid, name in [(204, "sibling"), (None, "planned")]:
            self.store.execute("INSERT INTO stand_vms(stand_id,vmid,name) VALUES(?,?,?)", (sibling, vmid, name))
        view = self.refresh({"Lab": [member(201, "initial"), member(204, "sibling"),
                                     member(205, "planned"), member(206, "manual")]})
        self.assertEqual(view["pool_extra_vms"], [member(206, "manual")])
        self.assertEqual(self.service.get_stand(sibling, include_pool_inventory=True)["pool_extra_vms"],
                         [member(206, "manual")])
        self.gateway.pool_inventory.assert_called_once_with(["Lab"])
        self.assertNotIn("pool_inventory_vmids", view)
