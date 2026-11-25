"""Teacher authoring and real queue admission, using temporary DBs and a mock PVE."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dashboard_backend.assignment_audience import allows_student_group, parse_stored_groups
from dashboard_backend.database import DashboardStore
from dashboard_backend.service import ConflictError, DashboardService, ValidationError


class AssignmentAudienceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = DashboardStore(Path(temp.name)/"admin.db", seed_demo=False)
        self.gw = Mock(mode="live")
        self.gw.list_templates.return_value = [{"vmid":101, "name":"nested"}]
        self.svc = DashboardService(self.store, self.gw)
        self.addCleanup(self.svc.stop_monitors)
        self.bp = self.svc.save_blueprint(dict(code="PR", name="Practical", template_vmid=101,
            assignment_markdown="Only a task", student_visible=True, status="active"))["id"]
        launch = patch.object(self.svc, "_launch_deployment")
        self.launch = launch.start()
        self.addCleanup(launch.stop)

    def edit(self, **fields):
        return self.svc.save_blueprint(fields, self.bp)

    def submit(self, group=None, **extra):
        payload = dict(name="student-lab", owner="class01", pool_id="student-lab", blueprint_id=self.bp,
                       vm_count=1, subnet="10.39.4.60/16", start_ip="10.39.4.60")
        receipt = dict(card_id="1"*24, assignment_id=f"lab-{self.bp}-101", template_vmid=101,
                       template_name="nested", ip_end="10.39.4.69", student_group=group)
        return self.svc.create_stand(dict(payload, **extra), student_receipt=receipt)

    def test_legacy_all_exact_groups_draft_duplicate_and_validation(self):
        self.assertIsNone(self.svc.get_blueprint(self.bp)["student_groups"])
        groups = ["А-1", "А-2"]
        self.assertEqual(self.edit(student_groups=groups[::-1])["student_groups"], groups)
        self.assertEqual(self.edit(description="new")["student_groups"], groups)
        clone = self.svc.duplicate_blueprint(self.bp)
        self.assertEqual(clone["student_groups"], groups)
        self.assertFalse(clone["student_visible"])
        for bad in ([], ["А-1","А-1"], "А-1", [None], [" "], ["X"*81], ["a\nb"], ["A "],
                    [str(i) for i in range(257)]):
            with self.subTest(bad=str(bad)[:24]), self.assertRaises(ValidationError):
                self.edit(student_groups=bad)
            self.assertEqual(self.svc.get_blueprint(self.bp)["student_groups"], groups)
        self.assertEqual(self.edit(student_visible=False, student_groups=[])["student_groups"], [])
        with self.assertRaises(ValidationError):
            self.edit(student_visible=True)
        self.assertTrue(self.edit(student_visible=True, student_groups=None)["student_visible"])

    def test_malformed_storage_never_turns_into_all_and_can_be_repaired(self):
        for raw in ('null', 'false', '{}', 'broken', '["А",null]', '["А","А"]', b'[]'):
            with self.subTest(raw=raw):
                self.assertFalse(allows_student_group(parse_stored_groups(raw), "А"))
                self.store.execute("UPDATE blueprints SET student_groups=? WHERE id=?", (raw,self.bp))
                self.assertEqual(self.svc.get_blueprint(self.bp)["student_groups"], [])
                with self.assertRaises(ConflictError):
                    self.submit("А")
        self.assertIsNone(self.edit(student_groups=None)["student_groups"])
        self.assertIsNone(self.store.query_one("SELECT id FROM stands LIMIT 1"))

    def test_publish_cannot_overwrite_audience_changed_during_template_lookup(self):
        self.edit(student_visible=False, student_groups=["A"])
        def teacher_edits_audience():
            self.edit(student_groups=["B"])
            return [{"vmid":101, "name":"nested"}]
        self.gw.list_templates.side_effect = teacher_edits_audience
        with self.assertRaises(ConflictError):
            self.edit(student_visible=True, student_groups=["A"])
        saved = self.svc.get_blueprint(self.bp)
        self.assertFalse(saved["student_visible"])
        self.assertEqual(saved["student_groups"], ["B"])

    def test_admission_uses_server_group_not_body_and_revocation_before_clone(self):
        self.edit(student_groups=["Группа А"])
        for actual in (None, "Группа", "Группа а", "Группа Б"):
            with self.assertRaises(ConflictError):
                self.submit(actual, student_group="Группа А")
        self.launch.assert_not_called()
        self.submit("Группа А")
        sid, plan = self.launch.call_args.args
        self.assertNotIn("student_groups", plan)
        self.assertEqual(self.store.query_one("SELECT student_group FROM student_lab_receipts WHERE stand_id=?", (sid,))["student_group"], "Группа А")
        def revoke_during_preflight():
            self.edit(student_groups=["Группа Б"])
            return [{"vmid":101, "name":"nested"}]
        self.gw.list_templates.side_effect = revoke_during_preflight
        self.svc._deploy_job(sid, plan)
        self.gw.deploy.assert_not_called()
        self.assertEqual(self.svc.get_stand(sid)["status"], "error")

    def test_retry_after_revocation_does_not_stop_relief_vms(self):
        self.submit("A")
        sid, plan = self.launch.call_args.args
        self.edit(student_groups=["B"])
        with patch("dashboard_backend.service.deploy_relief.stop_selected") as stop:
            self.svc._deploy_job(sid, plan, stop_stand_ids=[123])
            stop.assert_not_called()
        self.gw.deploy.assert_not_called()
        # A teacher's ordinary deployment is independent of classroom audience.
        self.svc.create_stand(dict(name="operator-test", blueprint_id=self.bp, vm_count=1))
        self.assertEqual(self.launch.call_count, 2)
