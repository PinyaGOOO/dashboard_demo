import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from dashboard_backend.database import DashboardStore
from dashboard_backend.service import ConflictError, DashboardService, ValidationError
from dashboard_backend.student_catalog import public_templates, published_assignments


class AssignmentPublicationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name)/"db"
        source = DashboardStore(self.db, seed_demo=False)
        self.gw = Mock(mode="live")
        self.gw.list_templates.return_value = [{"vmid":401,"name":"First"}, {"vmid":402,"name":"Second"}]
        self.service = DashboardService(source, self.gw)
        self.addCleanup(self.service.stop_monitors)
        self.data = {"name":"Практическая работа","code":"LAB","status":"active","template_vmid":401,
                     "assignment_markdown":"# Сеть\r\n- [ ] Пункт 1\n\n**Готово**",
                     "deploy_script":"teacher-only-script", "description":"teacher-only-notes"}

    def test_create_edit_publish_unpublish_duplicate_archive_and_delete(self):
        bp = self.service.save_blueprint(self.data)
        ident = bp["id"]
        self.assertIs(bp["student_visible"],False)
        self.assertEqual(published_assignments(self.db),[])
        self.gw.list_templates.assert_not_called()
        self.service.save_blueprint({"student_visible":True},ident)
        self.gw.list_templates.assert_called_once()
        task, = published_assignments(self.db)
        self.assertEqual(task,{"id":f"lab-{ident}-401","title":self.data["name"],"description":"","steps":[],
                               "template_vmid":401,"template_name":"First","assignment_markdown":self.data["assignment_markdown"]})
        edited = self.service.save_blueprint({"assignment_markdown":"# Другое условие"},ident)
        self.assertIs(edited["student_visible"],True)
        self.gw.list_templates.assert_called_once()
        copied = self.service.duplicate_blueprint(ident)
        self.assertIs(copied["student_visible"],False)
        self.assertEqual(copied["assignment_markdown"],edited["assignment_markdown"])
        self.assertEqual(len(published_assignments(self.db)),1)
        self.service.save_blueprint({"status":"archived"},ident)
        self.assertEqual(published_assignments(self.db),[])
        self.assertFalse(self.service.save_blueprint({"status":"active"},ident)["student_visible"])
        self.service.save_blueprint({"student_visible":True,"template_vmid":402},ident)
        task2, = published_assignments(self.db)
        self.assertEqual((task2["assignment_markdown"],task2["template_name"]),("# Другое условие","Second"))
        self.service.save_blueprint({"student_visible":False},ident)
        self.assertEqual(published_assignments(self.db),[])
        self.service.delete_blueprint(ident)
        self.assertEqual(published_assignments(self.db),[])

    def test_invalid_publication_cannot_replace_draft_or_last_published_text(self):
        ident = self.service.save_blueprint(self.data)["id"]
        for fields in ({"student_visible":1},{"student_visible":"false"},{"assignment_markdown":[]},
                       {"assignment_markdown":"x"*32001},{"assignment_markdown":"nul\0byte"},
                       {"student_visible":True,"status":"draft"},{"student_visible":True,"template_vmid":0},
                       {"student_visible":True,"template_vmid":403},{"student_visible":True,"assignment_markdown":" \n "}):
            with self.subTest(fields=list(fields)), self.assertRaises(ValidationError):
                self.service.save_blueprint(fields,ident)
        self.gw.list_templates.side_effect = ConnectionError("private-detail")
        with self.assertRaises(ConflictError):
            self.service.save_blueprint({"student_visible":True},ident)
        self.assertEqual(published_assignments(self.db),[])
        self.gw.list_templates.side_effect = None
        self.service.save_blueprint({"student_visible":True},ident)
        with self.assertRaises(ValidationError):
            self.service.save_blueprint({"template_vmid":403},ident)
        self.assertEqual(published_assignments(self.db)[0]["assignment_markdown"],self.data["assignment_markdown"])

    def test_concurrent_unpublish_wins_over_slow_template_change(self):
        ident = self.service.save_blueprint(dict(self.data,student_visible=True))["id"]
        def templates():
            self.service.save_blueprint({"student_visible":False},ident)
            return [{"vmid":402,"name":"Second"}]
        self.gw.list_templates.side_effect = templates
        with self.assertRaises(ConflictError):
            self.service.save_blueprint({"template_vmid":402},ident)
        self.assertEqual(published_assignments(self.db),[])

    def test_migration_and_legacy_export_do_not_publish_existing_records(self):
        ident = self.service.save_blueprint(dict(self.data,student_visible=True))["id"]
        # Recreate the table without the three new columns, as on the previous release.
        with closing(sqlite3.connect(self.db)) as db:
            for col in ("assignment_markdown","student_visible","assignment_template_name"):
                db.execute("ALTER TABLE blueprints DROP COLUMN "+col)
            db.commit()
        self.assertEqual(public_templates(self.db,self.gw.list_templates.return_value),[])
        DashboardStore(self.db, seed_demo=False)
        item = self.service.get_blueprint(ident)
        self.assertEqual((item["name"],item["assignment_markdown"],item["student_visible"]),(self.data["name"],"",False))
        self.assertEqual(published_assignments(self.db),[])
