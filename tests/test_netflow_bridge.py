"""Two real temporary DBs and the Netflow wire protocol, with no real network or VM."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard_backend.database import DashboardStore
from dashboard_backend.netflow_labs import NetflowLabs
from dashboard_backend.netflow_worker import NetflowClient, NetflowWorker
from dashboard_backend.service import DashboardService
from dashboard_backend.student_labs import StudentLabs
from integrations.netflow import stand_routes as api


class BridgeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.db = root / "netflow.sqlite3"
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY, grp TEXT, admin INTEGER, active INTEGER, must_change INTEGER);
            INSERT INTO users VALUES(19, 'DE-24', 0, 1, 0),(20, 'DE-24', 0, 1, 0),(21, 'OTHER', 0, 1, 0);
        """)
        self.store = DashboardStore(root / "dashboard.db")
        self.gateway = Mock(mode="live")
        self.svc = DashboardService(self.store, self.gateway)
        self.addCleanup(self.svc.stop_monitors)
        self.bp = next(x for x in self.svc.list_blueprints() if x["status"] == "active")
        self.gateway.list_templates.return_value = [{"vmid":self.bp["template_vmid"], "name":"nested-test"}]
        self.svc.save_blueprint({"student_visible":True, "assignment_markdown":"# Test", "student_groups":["DE-24"]},
                                self.bp["id"])
        self.task = f"lab-{self.bp['id']}-{self.bp['template_vmid']}"
        token = "t" * 40
        p = patch.dict(os.environ, {"NETFLOW_STANDS_ENABLED":"1", "NETFLOW_WORKER_TOKEN":token,
                                    "NETFLOW_STAND_SCENARIOS":json.dumps({self.task:["DE-24"]})})
        p.start()
        self.addCleanup(p.stop)
        api._configured.cache_clear()
        self.addCleanup(api._configured.cache_clear)
        queue = api.prepare(self.db)
        self.now = 100.
        queue._clock = lambda: self.now
        self.env = {"REQUEST_METHOD":"POST", "HTTP_AUTHORIZATION":"Bearer " + token,
                    "CONTENT_TYPE":"application/json"}
        self.client = NetflowClient("https://fixture.invalid", token)
        self.labs = StudentLabs(self.svc, "10.39.171.10", "10.39.171.20")
        self.runner = NetflowWorker(self.client, NetflowLabs(self.svc, self.labs), allow_links=True)
        p = patch.object(self.svc, "_launch_deployment")
        self.launch = p.start()
        self.addCleanup(p.stop)

    def browser(self, uid, method, path, data=None):
        # The Netflow hook passes its own authenticated uid and existing SQLite transaction.
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            return api.student(self.db, self.conn, uid, {"REQUEST_METHOD":method}, path, data or {})

    def exchange(self, path, data):
        return api.worker(self.db, self.env, path, data)

    def test_lost_ack_repeat_ready_isolation_and_block(self):
        self.assertEqual(self.browser(19, "GET", api.SCENARIOS), {"scenarios":[{"assignment_id":self.task}]})
        first = self.browser(19, "POST", api.STAND, {"assignment_id":self.task})
        self.assertEqual(first, self.browser(19, "POST", api.STAND, {"assignment_id":self.task}))
        self.assertEqual(self.browser(20, "GET", api.STAND), {"stand":None})
        def fail_ack(path, data):
            if path == api.STATUS:
                raise TimeoutError
            return self.exchange(path, data)
        with patch.object(self.client, "_post", side_effect=fail_ack), self.assertRaises(TimeoutError):
            self.runner.run_once()
        self.launch.assert_called_once()
        self.now += 91
        with patch.object(self.client, "_post", side_effect=self.exchange):
            self.assertTrue(self.runner.run_once())
            self.launch.assert_called_once()
            self.assertEqual(self.browser(19, "GET", api.STAND)["stand"]["status"], "building")
            sid, plan = self.launch.call_args.args
            r = self.store.query_one("SELECT vm_name FROM student_lab_receipts WHERE stand_id=?", (sid,))
            vm = {"vmid":7890, "node":"node1", "name":r["vm_name"], "status":"running"}
            self.gateway.deploy.return_value = [dict(vm, password="never-leave-deployer", last_snapshot="start")]
            self.gateway.timer_vm_uuid.return_value = "fake-uuid"
            self.gateway.pool_members.return_value = [vm]
            self.svc._deploy_job(sid,plan)
            self.now += 11
            with patch.object(self.labs, "_live_endpoint", return_value=True):
                self.assertTrue(self.runner.run_once())
            stand = self.browser(19, "GET", api.STAND)["stand"]
            self.assertEqual((stand["status"], stand["web_url"]), ("ready","https://10.39.171.10:8006/"))
            self.assertNotIn("never-leave-deployer", str(stand))
            self.assertEqual(self.browser(20, "GET", api.STAND), {"stand":None})
            self.conn.execute("UPDATE users SET active=0 WHERE id=19")
            self.conn.commit()
            with self.assertRaises(api.QueueError):
                self.browser(19, "GET", api.STAND)
            self.assertTrue(self.runner.run_once())
            self.launch.assert_called_once()

    def test_rejected_before_vm_is_not_retried_until_another_explicit_request(self):
        first = self.browser(19, "POST", api.STAND, {"assignment_id":self.task})["stand"]
        templates = self.gateway.list_templates.return_value
        self.gateway.list_templates.return_value = []
        with patch.object(self.client, "_post", side_effect=self.exchange):
            self.assertTrue(self.runner.run_once())
            rejected = self.browser(19, "GET", api.STAND)["stand"]
            self.assertEqual((rejected["job_id"], rejected["status"], rejected["web_url"]),
                             (first["job_id"], "rejected", None))
            self.now += 91
            self.assertFalse(self.runner.run_once())
            self.assertEqual(self.browser(20, "GET", api.STAND), {"stand":None})
            self.launch.assert_not_called()
            self.gateway.list_templates.return_value = templates
            again = self.browser(19, "POST", api.STAND, {"assignment_id":self.task})["stand"]
            self.assertNotEqual(again["job_id"], first["job_id"])
            self.assertTrue(self.runner.run_once())
            self.launch.assert_called_once()
            self.assertEqual(self.browser(19, "GET", api.STAND)["stand"]["status"], "building")

    def test_http_permissions_and_strict_fields(self):
        for change in ({"HTTP_AUTHORIZATION":""}, {"HTTP_ORIGIN":"https://fixture.invalid"},
                       {"HTTP_COOKIE":"student=anything"}, {"REQUEST_METHOD":"GET"},
                       {"CONTENT_TYPE":"text/plain"}):
            with self.subTest(fields=list(change)), self.assertRaises(api.RouteError):
                api.worker(self.db, dict(self.env, **change), api.CLAIM, {})
        with self.assertRaises(api.RouteError):
            self.browser(19, "POST", api.STAND, {"assignment_id":self.task, "user_id":20})
        with self.assertRaises(api.QueueError):
            self.browser(21, "POST", api.STAND, {"assignment_id":self.task})
        self.assertEqual(self.browser(21, "GET", api.SCENARIOS), {"scenarios":[]})
        with patch.dict(os.environ, {"NETFLOW_STANDS_ENABLED":"0"}), self.assertRaises(api.RouteError) as ex:
            self.exchange(api.CLAIM, {})
        self.assertEqual(ex.exception.status, 404)
        with patch.dict(os.environ, {"NETFLOW_STAND_SCENARIOS":"{}"}), self.assertRaises(api.RouteError):
            api._catalog()
