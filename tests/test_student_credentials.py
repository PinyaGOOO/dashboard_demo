import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard_backend.database import DashboardStore
from dashboard_backend.student_credentials import PilotCredentials
from dashboard_backend.student_portal import StudentError


class PilotCredentialsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "operator.db"
        self.store = DashboardStore(self.db, seed_demo=False)
        self.sql = self.store.execute
        self.sql("INSERT INTO blueprints(id,code,name,template_vmid,updated_at,created_at) VALUES(2,'PR1','pr1',276,'','')")
        self.sql("""INSERT INTO stands(id,name,owner,blueprint_id,status,pool_id,origin,created_at,updated_at)
                    VALUES(42,'pilot','student01',2,'running','pr1-pilot','deployed','','')""")
        self.sql("""INSERT INTO stand_vms(id,stand_id,vmid,name,ip,status,credential_password)
                    VALUES(54,42,331,'pilot-1','10.39.5.41','running','fixture-only')""")
        self.sql("""INSERT INTO ipam_reservations(stand_id,stand_vm_id,address,requested_cidr,prefix_length,
                    vm_index,status,created_at,updated_at) VALUES(42,54,'10.39.5.41','10.39.5.41/16',16,1,'assigned','','')""")
        self.binding = dict(student_login="student01", assignment_id="lab-2-276", stand_id="7a"*12,
                            deployer_stand_id=42, pool_id="pr1-pilot", vmid=331, guest_ipv4="10.39.5.41")
        self.file = self.db.with_name("student-pilot.json")
        self.publish(self.binding)
        self.issuer = PilotCredentials(self.db, "test-operator-key-not-for-real-use")

    def publish(self, binding):
        self.file.write_text(json.dumps(binding), encoding="utf-8")
        self.file.chmod(0o600)

    def fails(self, code, status, binding=None):
        with self.assertRaises(StudentError) as caught:
            self.issuer.for_student(self.binding if binding is None else binding, code)
        self.assertEqual(caught.exception.status, status)
        self.assertNotIn("fixture-only", str(caught.exception))

    def test_code_scoped_stable_and_reads_exact_guest(self):
        code = self.issuer.operator_code(42,331)
        self.assertRegex(code, r"^LAB-[A-Z2-7]{32}$")
        self.assertIsNone(self.issuer.operator_code(43,331))
        self.assertIsNone(self.issuer.operator_code(42,332))
        self.assertEqual(self.issuer.operator_code(42,331), code)
        self.assertEqual(self.issuer.for_student(self.binding,code), {"username":"root@pam","password":"fixture-only"})
        self.fails("DemoLab01!",403)
        self.fails("пароль",403)
        self.fails(None,403)
        self.fails(code,404,{**self.binding,"student_login":"student02"})
        self.fails(code,404,{**self.binding,"vmid":332})
        rotated = PilotCredentials(self.db,"second-test-operator-key")
        self.assertNotEqual(rotated.operator_code(42,331),code)
        with self.assertRaises(StudentError) as caught:
            rotated.for_student(self.binding,code)
        self.assertEqual(caught.exception.status,403)
        moved = {**self.binding,"stand_id":"ab"*12}
        self.publish(moved)
        self.fails(code,404)
        self.fails(code,403,moved)
        self.assertNotEqual(self.issuer.operator_code(42,331),code)

    def test_password_column_not_read_for_invalid_or_operator_code(self):
        connect = sqlite3.connect
        def no_password(*args, **kwargs):
            self.assertIn("mode=ro",args[0])
            db = connect(*args, **kwargs)
            def allow(op,table,column,*_):
                if op == sqlite3.SQLITE_READ and column=="credential_password":
                    raise AssertionError("password read without access code")
                return sqlite3.SQLITE_OK
            db.set_authorizer(allow)
            return db
        with patch("sqlite3.connect",side_effect=no_password):
            self.assertIsNone(self.issuer.authorize(self.binding, self.issuer.operator_code(42,331)))
            self.assertEqual(self.issuer.lease(self.binding)["status"], "active")
            self.fails("",403)

    def test_lease_status_and_actual_owner(self):
        code = self.issuer.operator_code(42,331)
        self.sql("UPDATE stands SET expires_at='2001-01-01T00:00:00Z' WHERE id=42")
        self.assertEqual(self.issuer.operator_code(42,331),code)  # inactive legacy timestamp
        self.assertEqual(self.issuer.lease(self.binding),{"duration_minutes":None,"expires_at":None,"status":"active"})
        self.sql("UPDATE stands SET duration_minutes=20 WHERE id=42")
        self.assertEqual(self.issuer.lease(self.binding),
                         {"duration_minutes":20,"expires_at":"2001-01-01T00:00:00Z","status":"expired"})
        self.assertIsNone(self.issuer.operator_code(42,331))
        self.fails(code,404)
        self.fails("DemoLab01!",404)
        for bad in (None,"broken"):
            self.sql("UPDATE stands SET expires_at=? WHERE id=42",(bad,))
            self.fails(code,404)
        self.sql("UPDATE stands SET expires_at=datetime('now','+1 hour') WHERE id=42")
        self.assertEqual(self.issuer.for_student(self.binding,code)["username"],"root@pam")
        self.sql("UPDATE stands SET owner='other' WHERE id=42")
        self.fails(code,404)
        self.assertIsNone(self.issuer.lease(self.binding))
        self.assertIsNone(self.issuer.operator_code(42,331))

    def test_stopped_lease_never_reenables_password_or_reshaped_network(self):
        code=self.issuer.operator_code(42,331)
        self.sql("UPDATE stands SET status='stopped' WHERE id=42")
        self.sql("UPDATE stand_vms SET status='stopped' WHERE id=54")
        self.assertEqual(self.issuer.lease(self.binding)["status"],"stopped")
        self.fails(code,404)
        self.sql("UPDATE stands SET duration_minutes=20,expires_at=datetime('now','-1 hour') WHERE id=42")
        self.assertEqual(self.issuer.lease(self.binding)["status"],"expired")
        self.assertIsNone(self.issuer.lease({**self.binding,"stand_id":"00"*12}))
        self.sql("UPDATE ipam_reservations SET prefix_length=24 WHERE stand_id=42")
        self.assertIsNone(self.issuer.lease(self.binding))

    def test_revocation_rechecked_by_secret_select(self):
        code = self.issuer.operator_code(42,331)
        def invalidate_lease_after_identity_check(_):
            self.sql("UPDATE ipam_reservations SET prefix_length=24 WHERE stand_id=42")
            return True
        with patch.object(self.issuer,"_valid",side_effect=invalidate_lease_after_identity_check):
            self.fails(code,404)

    def test_fails_closed_no_key_missing_or_replaced_binding(self):
        self.assertIsNone(PilotCredentials(self.db," ").operator_code(42,331))
        self.file.unlink()
        self.fails("",404)
        self.file.write_text("{}",encoding="utf-8")
        self.fails("",404)
        self.publish({**self.binding,"vmid":276})
        self.assertIsNone(self.issuer.operator_code(42,276))
        self.publish(self.binding)
        self.sql("UPDATE stand_vms SET credential_password=''")
        self.fails(self.issuer.operator_code(42,331),404)
        if os.name=="posix":
            self.file.chmod(0o666)
            self.assertIsNone(self.issuer.operator_code(42,331))


if __name__ == "__main__":
    unittest.main()
