"""Classroom authorization is based on the issued login, not a URL or a saved card."""
import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from dashboard_backend.database import DashboardStore
from dashboard_backend.service import DashboardService
from dashboard_backend.student_catalog import (assignment_group_access, load_catalog, public_templates,
                                              published_assignments)
from dashboard_backend.student_portal import StudentError, StudentPortal
from server import student_image_reader
from tests.test_assignment_images import TINY
from tests.test_classroom_flow import Labs


class StudentAssignmentAudience(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "dashboard.db"
        store = DashboardStore(self.db, seed_demo=False)
        gateway = Mock(mode="live")
        self.templates = [{"vmid":312, "name":"fixture-template"}]
        gateway.list_templates.return_value = self.templates
        self.svc = DashboardService(store,gateway)
        self.addCleanup(self.svc.stop_monitors)
        self.labs = Labs()
        self.submit = self.labs.submit = Mock(wraps=self.labs.submit)
        self.reader = student_image_reader(self.svc)
        pilot = Mock()
        pilot.private_login_required.side_effect = lambda login: login == "student01"
        pilot.authenticate_login.return_value = pilot.session_stamp.return_value = "fixture-private"
        self.state = self.root / "students.db"
        self.portal = StudentPortal(self.state, list, labs=self.labs, pilot_grant=pilot,
                published_catalog=lambda: published_assignments(self.db), image_reader=self.reader,
                assignment_access=lambda ident,group: assignment_group_access(self.db,ident,group))
        def issue(group):
            user, = self.portal.create_accounts({"count":1,"group":group})["accounts"]
            return self.portal.login(user["login"], user["password"])
        self.a = issue("ДЭ-24")
        self.b = issue("ДЭ-240")
        self.pilot = self.portal.login("student01", "private")

    def publish(self, **extra):
        data = dict(name="Restricted lesson",code="TEST",template_vmid=312,status="active",
                    assignment_markdown="Private exercise text",student_visible=True,student_groups=["ДЭ-24"])
        bp = self.svc.save_blueprint(dict(data, **extra))
        return bp["id"],f"lab-{bp['id']}-312"

    def denied(self, fn, *args):
        with self.assertRaises(StudentError) as err:
            fn(*args)
        self.assertEqual(err.exception.status,404)

    def test_exact_group_session_pilot_and_withdrawal_of_existing_card(self):
        bp, ident = self.publish()
        task, = self.portal.get_dashboard(self.a)["assignments"]
        self.assertEqual(task["id"],ident)
        self.assertNotIn("student_groups", task)
        for token in (self.b, self.pilot, self.portal.login("student02","DemoLab02!")):
            self.assertEqual(self.portal.get_dashboard(token)["assignments"],[])
            self.denied(self.portal.deploy,token,ident)
        self.submit.assert_not_called()
        sid = self.portal.deploy(self.a,ident)["id"]
        self.assertEqual(self.submit.call_args.kwargs["student_group"], "ДЭ-24")
        self.assertNotIn("student_groups",self.submit.call_args.args[1])
        self.svc.save_blueprint({"student_groups":["ДЭ-240"]},bp)
        self.assertEqual(self.portal.get_dashboard(self.a)["assignments"],[])
        self.denied(self.portal.get_stand,self.a,sid)
        self.denied(self.portal.deploy,self.a,ident)
        self.denied(self.portal.guest_action,self.a,sid,"credentials",{})
        self.assertEqual(self.portal.get_dashboard(self.b)["assignments"][0]["id"],ident)
        self.denied(self.portal.get_stand,self.b,sid)
        # Hiding the publication as well must not expose a previous group's VM credentials.
        self.svc.save_blueprint({"student_visible":False},bp)
        self.denied(self.portal.get_stand,self.a,sid)
        self.denied(self.portal.guest_action,self.a,sid,"credentials",{})
        self.assertEqual(len(self.labs.receipts),1)
        self.submit.assert_called_once()
        # The issued VM is still there when the teacher restores the group.
        self.svc.save_blueprint({"student_visible":True,"student_groups":["ДЭ-24"]},bp)
        self.assertEqual(self.portal.get_stand(self.a,sid)["stand"]["id"],sid)
        # Opening the same file without a classroom provider must not disclose old task_json.
        standalone = StudentPortal(self.state, list)
        self.assertEqual(standalone.get_dashboard(self.a)["assignments"],[])
        self.denied(standalone.get_stand,self.a,sid)

    def test_standalone_export_and_malformed_policies(self):
        bp,ident = self.publish()
        self.assertEqual(public_templates(self.db,self.templates),[])
        path = self.root / "snapshot.json"
        path.write_text(json.dumps([{"blueprint_id":bp,"vmid":312,"template":"fixture-template",
                                    "scenario":"Restricted lesson","assignment_markdown":"secret",
                                    "student_groups":["ДЭ-24"]}]), encoding="utf-8")
        self.assertEqual(load_catalog(path),[])
        for malformed in ("null", "{", '["ДЭ-24", 5]'):
            self.svc.store.execute("UPDATE blueprints SET student_groups=? WHERE id=?",(malformed,bp))
            self.assertEqual(public_templates(self.db,self.templates),[])
            self.assertEqual(self.portal.get_dashboard(self.a)["assignments"],[])
            self.denied(self.portal.deploy,self.a,ident)
        self.svc.store.execute("UPDATE blueprints SET student_groups=NULL WHERE id=?",(bp,))
        self.assertEqual(self.portal.get_dashboard(self.pilot)["assignments"][0]["id"],ident)
        self.assertEqual(len(public_templates(self.db,self.templates)),1)

    def test_image_policy_checked_again_when_reading_bytes(self):
        image = self.svc.upload_assignment_image(TINY)["id"]
        bp,ident = self.publish(assignment_markdown=f"![plan](attachment:{image})")
        self.assertEqual(self.portal.assignment_image(self.a,ident,image),TINY)
        self.denied(self.portal.assignment_image,self.b,ident,image)
        def revoke_before_bytes(assignment, ident, group):
            self.svc.save_blueprint({"student_groups":["ДЭ-240"]},bp)
            return self.reader(assignment,ident,group)
        self.portal._image_reader = revoke_before_bytes
        self.denied(self.portal.assignment_image,self.a,ident,image)
