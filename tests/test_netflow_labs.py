import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard_backend.database import DashboardStore
from dashboard_backend.netflow_labs import NetflowLabs, NetflowRetry
from dashboard_backend.service import DashboardService
from dashboard_backend.student_errors import StudentError
from dashboard_backend.student_labs import StudentLabs


class NetflowLabsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = DashboardStore(Path(tmp.name) / "dashboard.db")
        self.gateway = Mock(mode="live")
        self.service = DashboardService(self.store, self.gateway)
        self.addCleanup(self.service.stop_monitors)
        self.bp = next(b for b in self.service.list_blueprints() if b["status"] == "active")
        self.vmid = self.bp["template_vmid"]
        self.gateway.list_templates.return_value = [{"vmid": self.vmid, "name": "nested-test"}]
        self.service.save_blueprint({"assignment_markdown": "# Lab", "student_visible": True,
                                     "student_groups": ["Group A"]}, self.bp["id"])
        self.job = {"job_id": "a"*32, "subject": "1"*32, "group": "group a",
                    "assignment_id": f"lab-{self.bp['id']}-{self.vmid}", "lease_token": "unused-in-adapter"}
        self.labs = StudentLabs(self.service, "10.39.171.10", "10.39.171.19")
        self.adapter = NetflowLabs(self.service, self.labs)
        p = patch.object(self.service, "_launch_deployment")
        self.launch = p.start()
        self.addCleanup(p.stop)

    def binding(self):
        return self.store.query_one("SELECT * FROM netflow_lab_jobs WHERE job_id=?", (self.job["job_id"],))

    def test_concurrent_duplicate_and_restarts_only_create_one_real_submission(self):
        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(lambda _: self.adapter.handle(self.job), range(10)))
        self.assertEqual({r["status"] for r in results}, {"building"})
        self.launch.assert_called_once()
        fresh = NetflowLabs(self.service, self.labs)
        self.assertEqual(fresh.handle(dict(self.job, lease_token="replacement"))["status"], "building")
        self.assertEqual(self.binding()["stand_id"], self.launch.call_args.args[0])
        r = self.store.query_one("SELECT * FROM student_lab_receipts WHERE card_id=?", (self.binding()["card_id"],))
        self.assertEqual(r["login"], "nf-" + self.job["subject"] + "-external")
        self.assertEqual(r["student_group"], "Group A")
        self.assertEqual(self.adapter.handle(dict(self.job, subject="9"*32)), {"status":"error","progress":0})
        self.assertEqual(self.adapter.handle(dict(self.job, job_id="b"*32))["status"], "error")
        self.launch.assert_called_once()

    def test_receipt_trigger_and_tombstone_close_even_if_group_or_publication_changed(self):
        self.adapter.handle(self.job)
        r = self.binding()
        # Simulate admin deletion. Inventory and a new stand must never restore the old job.
        self.store.execute("DELETE FROM stands WHERE id=?", (r["stand_id"],))
        self.store.execute("UPDATE blueprints SET student_visible=0 WHERE id=?", (self.bp["id"],))
        self.assertEqual(self.adapter.handle(dict(self.job, group="Different"))["status"], "removed")
        self.store.execute("UPDATE blueprints SET student_visible=1 WHERE id=?", (self.bp["id"],))
        self.assertEqual(self.adapter.handle(dict(self.job, job_id="b"*32))["status"], "building")
        self.assertEqual(self.adapter.handle(self.job)["status"], "removed")
        self.assertEqual(self.launch.call_count, 2)

    def test_failed_and_withdrawn_never_emit_link(self):
        denied = dict(self.job, group="Group B", job_id="d"*32)
        self.assertEqual(self.adapter.handle(denied)["status"], "removed")
        self.assertIsNone(self.binding())
        self.adapter.handle(self.job)
        with patch.object(self.labs, "inspect") as inspect:
            self.store.execute("UPDATE blueprints SET student_visible=0 WHERE id=?", (self.bp["id"],))
            self.assertEqual(self.adapter.handle(self.job), {"status":"error","progress":0})
            inspect.assert_not_called()
        self.store.execute("UPDATE blueprints SET student_visible=1 WHERE id=?", (self.bp["id"],))
        with patch.object(self.labs, "inspect") as inspect:
            def revoke(*args, **kwargs):
                self.assertTrue(kwargs["detail"])
                self.store.execute("UPDATE blueprints SET student_groups='[]' WHERE id=?", (self.bp["id"],))
                return {"status":"ready","access":{"kind":"proxmox","url":"https://10.39.171.10:8006/"}}
            inspect.side_effect = revoke
            self.assertEqual(self.adapter.handle(self.job)["status"], "error")
        self.launch.assert_called_once()

    def test_missing_receipt_cannot_allocate_again(self):
        self.adapter.handle(self.job)
        self.store.execute("DELETE FROM student_lab_receipts WHERE card_id=?", (self.binding()["card_id"],))
        self.assertEqual(self.adapter.handle(self.job)["status"], "error")
        self.launch.assert_called_once()

    def test_safe_states_urls_and_retry(self):
        with patch.object(self.labs, "submit", side_effect=StudentError(503, "operator detail")):
            with self.assertRaises(NetflowRetry):
                self.adapter.handle(self.job)
        self.assertIsNone(self.binding()["stand_id"])
        self.assertEqual(self.adapter.handle(self.job)["status"], "building")
        for state in ("awaiting_confirmation","stopped","expired","error"):
            with patch.object(self.labs, "inspect", return_value={"status":state,"password":"not-exportable"}):
                self.assertEqual(self.adapter.handle(self.job), {"status":state,"progress":0})
        for u in ("https://10.39.171.10:8006/", "http://10.39.171.10:8006/",
                  "https://10.39.171.10:8006/?token=exfil", "https://external.test:8006/"):
            with patch.object(self.labs, "inspect", return_value={"status":"ready","password":"never",
                "access":{"kind":"proxmox","url":u,"login":"root@pam","password":"never"}}) as inspect:
                result = self.adapter.handle(self.job)
                if u == "https://10.39.171.10:8006/":
                    self.assertEqual(result, {"status":"ready", "progress":100, "web_url":u})
                else:
                    self.assertEqual(result, {"status":"error", "progress":0})
                self.assertTrue(inspect.call_args.kwargs["detail"])
        self.launch.assert_called_once()

    def test_changed_claim_cannot_rebind(self):
        self.adapter.handle(self.job)
        for bad in ({"group":"Group B"}, {"subject":"f"*32}, {"assignment_id":"lab-999-100"},
                    {"subject":"../../evil"}, {"group":""}):
            self.assertEqual(self.adapter.handle(dict(self.job, **bad))["status"], "error")
        self.launch.assert_called_once()

    def test_reconcile_unknown_is_a_tombstone_not_a_slot_or_a_proxmox_action(self):
        with patch.object(self.labs, "submit") as submit, patch.object(self.labs, "inspect") as inspect:
            self.assertEqual(self.adapter.handle(dict(self.job, action="bogus"))["status"], "error")
            self.assertIsNone(self.binding())
            self.assertEqual(self.adapter.handle(dict(self.job, action="reconcile", group=None))["status"], "removed")
            self.assertEqual(self.adapter.handle(self.job)["status"], "removed")
            submit.assert_not_called()
            inspect.assert_not_called()
        self.assertEqual(self.adapter.handle(dict(self.job, job_id="c"*32))["status"], "building")
        self.launch.assert_called_once()

    def test_reconcile_blocks_old_claim_even_after_access_is_back_until_admin_removes(self):
        self.adapter.handle(self.job)
        with patch.object(self.labs, "submit") as submit, patch.object(self.labs, "inspect") as inspect:
            action = dict(self.job, action="reconcile")
            self.assertEqual(self.adapter.handle(dict(action, subject="f"*32))["status"], "error")
            self.assertEqual(self.binding()["revoked"], 0)
            self.assertEqual(self.adapter.handle(action), {"status":"error","progress":0})
            self.assertEqual(self.binding()["revoked"], 1)
            self.assertEqual(self.adapter.handle(self.job)["status"], "error")
            self.assertEqual(self.adapter.handle(dict(self.job, job_id="c"*32))["status"], "error")
            self.store.execute("DELETE FROM stands WHERE id=?", (self.binding()["stand_id"],))
            self.assertEqual(self.adapter.handle(action)["status"], "removed")
            submit.assert_not_called()
            inspect.assert_not_called()
        self.assertEqual(self.adapter.handle(dict(self.job, job_id="c"*32))["status"], "building")

    def test_reconcile_fences_a_clone_still_between_reservation_and_receipt(self):
        reserved, resume = threading.Event(), threading.Event()
        initial = self.labs.submit
        def delayed(*args, **kwargs):
            reserved.set()
            self.assertTrue(resume.wait(5))
            return initial(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as worker, patch.object(self.labs, "submit", side_effect=delayed):
            first = worker.submit(self.adapter.handle, self.job)
            try:
                self.assertTrue(reserved.wait(5))
                self.assertEqual(self.adapter.handle(dict(self.job, action="reconcile"))["status"], "removed")
            finally:
                resume.set()
            # The in-flight submit got fenced. Its HTTP acknowledgement may
            # be lost; a new claim always sees the tombstone.
            try:
                self.assertIn(first.result()["status"], {"error","removed"})
            except NetflowRetry:
                pass
        self.assertEqual(self.adapter.handle(self.job)["status"], "removed")
        self.launch.assert_not_called()
        self.assertEqual(self.store.query_one("SELECT count(*) AS n FROM student_lab_receipts")["n"], 0)
        self.assertIsNone(self.binding()["stand_id"])

    def test_unpublished_before_first_submission_releases_slot_without_replay(self):
        with patch.object(self.labs, "submit", side_effect=NetflowRetry):
            with self.assertRaises(NetflowRetry):
                self.adapter.handle(self.job)
        self.store.execute("UPDATE blueprints SET student_visible=0 WHERE id=?", (self.bp["id"],))
        self.assertEqual(self.adapter.handle(self.job)["status"], "removed")
        self.store.execute("UPDATE blueprints SET student_visible=1 WHERE id=?", (self.bp["id"],))
        self.assertEqual(self.adapter.handle(self.job)["status"], "removed")
        self.assertEqual(self.adapter.handle(dict(self.job, job_id="b"*32))["status"], "building")
        self.launch.assert_called_once()

    def test_deterministic_refusal_without_receipt_is_durable_rejected_and_frees_slot(self):
        for n, code in enumerate((400, 403, 404, 409), 1):
            test_job = dict(self.job, job_id=f"{n:032x}")
            with patch.object(self.labs, "submit", side_effect=StudentError(code, "private error")) as submit:
                self.assertEqual(self.adapter.handle(test_job), {"status":"rejected","progress":0})
                restarted = NetflowLabs(self.service, self.labs)
                self.assertEqual(restarted.handle(test_job)["status"], "rejected")
                submit.assert_called_once()
                self.assertEqual(restarted.handle(dict(test_job, action="reconcile"))["status"], "removed")
                self.assertEqual(restarted.handle(test_job)["status"], "removed")
        # Explicit new request, same student, no cleanup of a phantom stand.
        self.assertEqual(self.adapter.handle(self.job)["status"], "building")
        self.launch.assert_called_once()

    def test_deterministic_error_after_receipt_commit_never_rejects_real_stand(self):
        original = self.labs.submit
        def committed(*args, **kwargs):
            original(*args, **kwargs)
            raise StudentError(409, "Simulate a lost acknowledgement observed as conflict")
        with patch.object(self.labs, "submit", side_effect=committed):
            self.assertEqual(self.adapter.handle(self.job)["status"], "building")
        self.assertEqual(self.binding()["released"], 0)
        self.assertEqual(self.adapter.handle(dict(self.job, job_id="c"*32))["status"], "error")
        self.launch.assert_called_once()

    def test_rejection_fences_inflight_attempt_even_across_worker_instances(self):
        reserved, resume = threading.Event(), threading.Event()
        initial = self.labs.submit
        other = NetflowLabs(self.service, Mock())
        other.labs.submit.side_effect = StudentError(409, "exhausted")
        def delayed(*args, **kwargs):
            reserved.set()
            self.assertTrue(resume.wait(5))
            return initial(*args, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as worker, patch.object(self.labs, "submit", side_effect=delayed):
            first = worker.submit(self.adapter.handle, self.job)
            try:
                self.assertTrue(reserved.wait(5))
                self.assertEqual(other.handle(self.job)["status"], "rejected")
            finally:
                resume.set()
            try:
                self.assertEqual(first.result()["status"], "rejected")
            except NetflowRetry:
                pass  # A lease timeout or transient DB error must not create another VM, either.
        self.assertEqual(self.adapter.handle(self.job)["status"], "rejected")
        self.assertIsNone(self.binding()["stand_id"])
        self.assertEqual(self.store.query_one("SELECT count(*) AS n FROM student_lab_receipts")["n"], 0)
        self.launch.assert_not_called()
