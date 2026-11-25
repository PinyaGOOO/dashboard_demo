import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard_backend.database import DashboardStore
from dashboard_backend.service import DashboardService
from dashboard_backend.student_labs import StudentLabs
from dashboard_backend.student_portal import StudentError


class PersonalLabTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = DashboardStore(Path(temp.name)/"admin.db")
        self.gw = Mock(mode="live")
        self.svc = DashboardService(self.store,self.gw)
        self.addCleanup(self.svc.stop_monitors)
        bp = next(b for b in self.svc.list_blueprints() if b["status"] == "active")
        vmid = bp["template_vmid"]
        self.gw.list_templates.return_value = [{"vmid":vmid,"name":"nested"}]
        self.task = {"id":f"lab-{bp['id']}-{vmid}","template_vmid":vmid,"template_name":"nested"}
        self.card, self.login = "1"*24, "practicum-03"
        self.adapter = StudentLabs(self.svc,"10.39.170.10","10.39.170.11")
        p = patch.object(self.svc,"_launch_deployment")
        self.launch = p.start()
        self.addCleanup(p.stop)

    def submit(self,card=None):
        return self.adapter.submit(self.login,self.task,card or self.card)

    def receipt(self):
        return self.store.query_one("SELECT * FROM student_lab_receipts WHERE card_id=?", (self.card,))

    def complete(self):
        self.submit()
        sid, plan = self.launch.call_args.args
        r = self.receipt()
        self.gw.deploy.return_value = [dict(vmid=123456,name=r["vm_name"],node="n1",status="running",
                                            password="fake-guest-secret",last_snapshot="start")]
        self.gw.timer_vm_uuid.return_value = "original-clone-uuid"
        self.gw.pool_members.return_value = [dict(vmid=123456,name=r["vm_name"],node="n1",status="running")]
        self.svc._deploy_job(sid,plan)
        return self.receipt()

    def test_real_submission_is_atomic_bounded_idempotent_and_survives_restart(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(lambda _: self.submit(), range(10)))
        self.assertTrue(all(r["status"]=="provisioning" for r in rows))
        self.launch.assert_called_once()
        copied = StudentLabs(self.svc,"10.39.170.10","10.39.170.11")
        self.assertEqual(copied.submit(self.login,self.task,self.card)["status"],"provisioning")
        self.assertEqual(self.receipt()["guest_ip"],"10.39.170.10")
        self.submit("2"*24)
        with self.assertRaises(StudentError):
            self.submit("3"*24)
        self.assertEqual(self.launch.call_count,2)
        self.assertEqual(self.store.query_one("SELECT count(*) AS n FROM student_lab_receipts")["n"],2)
        self.assertIsNone(self.store.query_one("SELECT id FROM stands WHERE pool_id=?",("lab-"+"3"*24,)))

    def test_bad_range_public_payload_or_stale_template_never_allocates(self):
        for lo,hi in [(None,None),("10.39.170.10","10.40.0.1"),("10.39.0.0","10.39.0.99"),
                      ("10.39.170.11","10.39.170.10"),("10.39.170.11","10.39.255.255")]:
            with self.assertRaises(StudentError):
                StudentLabs(self.svc,lo,hi).submit(self.login,self.task,self.card)
        self.gw.list_templates.return_value=[]
        with self.assertRaises(StudentError):
            self.submit()
        self.gw.list_templates.return_value=[dict(vmid=self.task["template_vmid"],name="different-template")]
        with self.assertRaises(StudentError):
            self.submit()
        self.assertIsNone(self.receipt())
        self.launch.assert_not_called()
        data=dict(name="normal-admin-stand", blueprint_id=int(self.task["id"].split("-")[1]),student_receipt={"card_id":self.card})
        self.svc.create_stand(data)
        self.assertIsNone(self.receipt())

    def test_credentials_require_original_live_clone_and_current_enrollment(self):
        r=self.complete()
        self.assertEqual(r["vmid"],123456)
        def creds(): return self.adapter.credentials(self.login,self.task,self.card)
        with patch("dashboard_backend.student_labs._guest_mac",return_value="a-valid-mac"), patch(
                "dashboard_backend.student_labs._verify_endpoint",return_value=True):
            data=self.adapter.inspect(self.login,self.task,self.card,detail=True)
            self.assertEqual(data["access"]["url"],"https://10.39.170.10:8006/")
            self.assertNotIn("secret",str(data))
            self.assertEqual(creds(),{"username":"root@pam","password":"fake-guest-secret"})
            with self.assertRaises(StudentError):
                self.adapter.credentials("practicum-04",self.task,self.card)
            self.gw.timer_vm_uuid.return_value="recycled-vmid"
            with self.assertRaises(StudentError): creds()
            self.gw.timer_vm_uuid.return_value=r["guest_uuid"]
            self.store.execute("UPDATE ipam_reservations SET status='reserved' WHERE stand_id=?",(r["stand_id"],))
            with self.assertRaises(StudentError): creds()
            self.store.execute("UPDATE ipam_reservations SET status='assigned' WHERE stand_id=?",(r["stand_id"],))
            self.store.execute("UPDATE stands SET duration_minutes=1, expires_at='2001-01-01' WHERE id=?",(r["stand_id"],))
            self.assertEqual(self.adapter.inspect(self.login,self.task,self.card)["status"],"expired")
            with self.assertRaises(StudentError): creds()

    def test_queued_plan_never_clones_a_replaced_template(self):
        self.submit()
        sid, plan = self.launch.call_args.args
        self.gw.list_templates.return_value=[]
        self.svc._deploy_job(sid,plan)
        self.gw.deploy.assert_not_called()
        self.assertEqual(self.adapter.inspect(self.login,self.task,self.card)["status"],"error")

    def test_twenty_simultaneous_cards_bound_proofs_and_password_joins_inflight(self):
        full, release = threading.Event(), threading.Event()
        started = []
        def probe(r):
            started.append(r["vmid"])
            if len(started)==4: full.set()
            return release.wait(8)
        def row(vmid):
            return dict(stand_id=vmid,vm_row_id=vmid,vmid=vmid,node="n",guest_uuid=str(vmid),guest_ip=f"10.39.1.{vmid}")
        with patch.object(self.adapter,"_live_endpoint",side_effect=probe), ThreadPoolExecutor(max_workers=6) as threads:
            try:
                checking=[threads.submit(self.adapter._endpoint,row(i)) for i in range(4)]
                self.assertTrue(full.wait(3))
                for i in range(4,20):
                    self.assertFalse(self.adapter._endpoint(row(i)))
                # Ensure a password request is already waiting on the in-flight
                # result when we release the proof, not a second gateway probe.
                with self.adapter._lock:
                    flight = next(f for key,f in self.adapter._inflight.items() if key[2]==started[0])
                waiting = threading.Event()
                original = flight.result
                def joined(*args, **kwargs):
                    waiting.set()
                    return original(*args,**kwargs)
                with patch.object(flight, "result", side_effect=joined):
                    password = threads.submit(self.adapter._endpoint,row(started[0]),fresh=True)
                    self.assertTrue(waiting.wait(3))
                    release.set()
                    self.assertTrue(password.result(timeout=3))
                self.assertTrue(all(f.result() for f in checking))
                self.assertEqual(len(started),4)
            finally:
                release.set()

    def test_removed_receipt_never_adopts_another_stand_by_owner_and_pool(self):
        self.submit()
        r=self.receipt()
        self.store.execute("UPDATE stands SET status='awaiting_confirmation' WHERE id=?",(r["stand_id"],))
        self.assertEqual(self.submit()["status"],"awaiting_confirmation")
        self.store.execute("DELETE FROM stands WHERE id=?",(r["stand_id"],))
        self.svc.create_stand(dict(name="imported", owner=self.login, pool_id=r["pool_id"],
                                   blueprint_id=r["blueprint_id"], subnet="10.39.170.10/16"))
        self.assertEqual(self.submit()["status"],"removed")
        self.assertEqual(self.launch.call_count,2)
        with self.assertRaises(StudentError):
            self.adapter.credentials(self.login,self.task,self.card)
