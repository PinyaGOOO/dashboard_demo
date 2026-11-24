import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import call, patch

from dashboard_backend.student_catalog import load_catalog, make_assignments, refresh_catalog


GATEWAY = "dashboard_backend.proxmox_gateway.create_gateway"


class StudentCatalogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.db = root / "deployer.sqlite"
        self.dest = root / "public" / "catalog.json"

    def seed(self):
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("CREATE TABLE blueprints (id INTEGER, name TEXT, template_vmid INTEGER, status TEXT, bootstrap TEXT)")
            db.executemany("INSERT INTO blueprints VALUES (?,?,?,?,?)", [
                (1, "1 module", 283, "active", "private-provision-script"),
                (2, "old draft", 10, "draft", "private-key"),
                (3, "removed template", 11, "active", "private-script"),
                (6, "1 module 2027", 144, "active", "private-autocheck"),
            ])
            db.commit()
        return [
            {"vmid": 144, "name": "DEMOFULL2027", "node": "private-node"},
            {"vmid": 283, "name": "de2026module-1-ach", "token": "private-token"},
            {"vmid": 10, "name": "unused draft"},
            {"vmid": 57, "name": "not in scenarios"},
        ]

    def test_verified_snapshot_contains_only_active_existing_templates_and_public_fields(self):
        templates = self.seed()
        before = self.db.read_bytes()
        with patch(GATEWAY) as factory:
            factory.return_value.list_templates.return_value = templates
            self.assertEqual(refresh_catalog(self.db, self.dest), 2)
            self.assertEqual(factory.mock_calls, [call(), call().list_templates()])
        self.assertEqual(self.db.read_bytes(), before)
        rows = json.loads(self.dest.read_text(encoding="utf-8"))
        self.assertEqual([r["vmid"] for r in rows], [283, 144])
        self.assertTrue(all(set(r) == {"blueprint_id", "scenario", "vmid", "template"} for r in rows))
        self.assertNotIn("private", str(rows))
        with patch(GATEWAY, side_effect=AssertionError("student reads must be offline")):
            tasks = load_catalog(self.dest)
        self.assertEqual([t["template_name"] for t in tasks], ["de2026module-1-ach", "DEMOFULL2027"])
        self.assertIn("2026", tasks[0]["title"])
        self.assertIn("2027", tasks[1]["title"])
        self.assertNotEqual(tasks[0]["steps"], tasks[1]["steps"])
        self.assertEqual(set(tasks[0]), {"id", "title", "description", "steps", "template_name", "template_vmid"})

    def test_task_ids_stable_when_titles_or_catalog_order_change(self):
        rows = [
            {"blueprint_id": 2, "scenario": "CP-PR1-EMPTY", "vmid": 276, "template": "CP-PR1-EMPTY"},
            {"blueprint_id": 3, "scenario": "CP-PR2-INFRA-READY", "vmid": 278, "template": "CP-PR2-INFRA-READY"},
        ]
        first = {t["template_vmid"]: t["id"] for t in make_assignments(rows)}
        rows = [{**r, "template": "new alias", "scenario": "modified"} for r in reversed(rows)]
        self.assertEqual(first, {t["template_vmid"]: t["id"] for t in make_assignments(rows)})
        self.assertEqual(len(set(first.values())), 2)

    def test_offline_examples_distinct_and_used_only_for_missing_snapshot(self):
        with patch(GATEWAY, side_effect=AssertionError("student reads must be offline")):
            self.assertEqual(load_catalog(self.dest, example_if_missing=False), [])
            examples = load_catalog(self.dest)
            self.assertEqual([t["template_vmid"] for t in examples], [9001, 9002, 9003])
            self.assertEqual(len({t["title"] for t in examples}), 3)
            self.assertEqual(len({tuple(t["steps"]) for t in examples}), 3)
            self.dest.parent.mkdir()
            self.dest.write_text("[]", encoding="utf-8")
            self.assertEqual(load_catalog(self.dest), [])
            self.dest.write_text("broken", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                load_catalog(self.dest)

    def test_invalid_paths_never_contact_gateway_or_overwrite_db(self):
        with patch(GATEWAY, side_effect=AssertionError("invalid paths must be rejected before refresh")):
            with self.assertRaises(FileNotFoundError):
                refresh_catalog(self.db, self.dest)
            self.assertFalse(self.db.exists())
            self.seed()
            before = self.db.read_bytes()
            with self.assertRaises(ValueError):
                refresh_catalog(self.db, self.db)
            alias = self.db.with_suffix(".json")
            self.db.rename(alias)
            with self.assertRaises(ValueError):
                refresh_catalog(alias, alias)
            other = self.db.with_name("other.json")
            other.write_bytes(before)
            with self.assertRaises(ValueError):
                refresh_catalog(alias, other)
            self.assertEqual(alias.read_bytes(), before)
            self.assertEqual(other.read_bytes(), before)

    def test_refresh_failure_keeps_previous_snapshot_and_cleans_only_own_tempfile(self):
        templates = self.seed()
        self.dest.parent.mkdir()
        self.dest.write_text("[]", encoding="utf-8")
        with patch(GATEWAY) as factory:
            factory.return_value.list_templates.side_effect = ConnectionError("offline")
            with self.assertRaises(ConnectionError):
                refresh_catalog(self.db, self.dest)
            factory.return_value.list_templates.side_effect = None
            factory.return_value.list_templates.return_value = templates
            with patch("dashboard_backend.student_catalog.os.replace", side_effect=PermissionError("busy")):
                with self.assertRaises(PermissionError):
                    refresh_catalog(self.db, self.dest)
        self.assertEqual(self.dest.read_text(encoding="utf-8"), "[]")
        self.assertEqual(list(self.dest.parent.iterdir()), [self.dest])
