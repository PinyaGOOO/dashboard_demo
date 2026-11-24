import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard_backend.database import DashboardStore
from dashboard_backend.student_pilot_validation import make_pilot_validator


class PilotIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "operator.db"
        self.store = DashboardStore(self.path, seed_demo=False)
        self.sql = self.store.execute
        self.sql("INSERT INTO blueprints(id,code,name,template_vmid,updated_at,created_at) VALUES(2,'PR1','test',276,'','')")
        self.sql("""INSERT INTO stands(id,name,blueprint_id,status,pool_id,origin,created_at,updated_at)
                    VALUES(42,'pilot',2,'running','pilot','deployed','',''),
                          (99,'other',2,'running','other','deployed','','')""")
        self.sql("""INSERT INTO stand_vms(id,stand_id,vmid,name,ip,status)
                    VALUES(54,42,331,'mine','10.39.24.87','running')""")
        self.sql("""INSERT INTO ipam_reservations(stand_id,stand_vm_id,address,requested_cidr,prefix_length,
                    vm_index,status,created_at,updated_at) VALUES(42,54,'10.39.24.87','10.39.24.87/16',16,1,'assigned','','')""")
        self.binding = dict(student_login="student01", assignment_id="lab-2-276", stand_id="7a"*12,
                            deployer_stand_id=42, pool_id="pilot", vmid=331, guest_ipv4="10.39.24.87")
        self.valid = make_pilot_validator(self.path)

    def test_valid_and_never_accesses_secret_columns(self):
        original = sqlite3.connect
        def restricted(filename, **kw):
            self.assertIn("mode=ro", filename)
            db = original(filename, **kw)
            def allow(op, table, column, *_):
                if op == sqlite3.SQLITE_READ and column in {"credential_password", "deploy_script", "autocheck_script"}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(allow)
            return db
        with patch("dashboard_backend.student_pilot_validation.sqlite3.connect", side_effect=restricted):
            self.assertTrue(self.valid(self.binding))

    def test_binding_identity_all_fields_fail_closed(self):
        for field, wrong in (("student_login","student02"), ("assignment_id","lab-3-278"),
                             ("stand_id","bad"), ("vmid", True), ("vmid",276), ("vmid",332),
                             ("deployer_stand_id",99), ("deployer_stand_id",2**64), ("pool_id","pILOT"),
                             ("guest_ipv4","10.39.24.88"), ("guest_ipv4","127.0.0.1"),
                             ("guest_ipv4","10.39.24.87:8006"), ("guest_ipv4","::1")):
            with self.subTest(field=field,value=wrong):
                self.assertFalse(self.valid({**self.binding, field: wrong}))

    def test_current_identity_and_lease_changes_revoke_access(self):
        for sql, restore in (
            ("UPDATE stands SET origin='imported' WHERE id=42", "UPDATE stands SET origin='deployed' WHERE id=42"),
            ("UPDATE stands SET status='stopped' WHERE id=42", "UPDATE stands SET status='running' WHERE id=42"),
            ("UPDATE blueprints SET template_vmid=277", "UPDATE blueprints SET template_vmid=276"),
            ("UPDATE stand_vms SET status='resetting'", "UPDATE stand_vms SET status='running'"),
            ("UPDATE stand_vms SET credential_valid=0", "UPDATE stand_vms SET credential_valid=1"),
            ("UPDATE stand_vms SET web_username='different@pam'", "UPDATE stand_vms SET web_username='root@pam'"),
            ("UPDATE ipam_reservations SET status='reserved'", "UPDATE ipam_reservations SET status='assigned'"),
            ("UPDATE ipam_reservations SET stand_id=99", "UPDATE ipam_reservations SET stand_id=42"),
            ("UPDATE ipam_reservations SET stand_vm_id=NULL", "UPDATE ipam_reservations SET stand_vm_id=54"),
            ("UPDATE ipam_reservations SET address='10.39.24.88'", "UPDATE ipam_reservations SET address='10.39.24.87'"),
            ("UPDATE stand_vms SET ip='10.39.24.88'", "UPDATE stand_vms SET ip='10.39.24.87'"),
            ("UPDATE stands SET pool_id='pilot' WHERE id=99", "UPDATE stands SET pool_id='other' WHERE id=99"),
        ):
            with self.subTest(sql=sql):
                self.sql(sql)
                self.assertFalse(self.valid(self.binding))
                self.sql(restore)
                self.assertTrue(self.valid(self.binding))
        self.sql("DELETE FROM stands WHERE id=42")
        self.assertFalse(self.valid(self.binding))

    def test_duplicate_id_or_ip_in_inventory_including_cidr_is_ambiguous(self):
        for stand, vm, ip in ((99,331,"10.39.24.200"), (99,775,"10.39.24.87"),
                              (99,775,"10.39.24.87/24"), (42,775,"10.39.24.200")):
            with self.subTest(stand=stand, vm=vm, ip=ip):
                row = self.sql("INSERT INTO stand_vms(stand_id,vmid,name,ip) VALUES(?,?,'other',?)", (stand,vm,ip))
                self.assertFalse(self.valid(self.binding))
                self.sql("DELETE FROM stand_vms WHERE id=?", (row,))
                self.assertTrue(self.valid(self.binding))

    def test_no_database_creation_or_schema_repair(self):
        missing = self.path.with_name("missing.db")
        self.assertFalse(make_pilot_validator(missing)(self.binding))
        self.assertFalse(missing.exists())
        self.sql("DROP TABLE ipam_reservations")
        self.assertFalse(self.valid(self.binding))


if __name__ == "__main__":
    unittest.main()
