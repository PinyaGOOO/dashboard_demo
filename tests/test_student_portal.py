import concurrent.futures
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from dashboard_backend.student_portal import StudentError, StudentPortal


def task(key, vmid):
    return {"id": key, "title": f"Задание {key}", "description": "Учебная практика",
            "template_name": f"template-{vmid}", "template_vmid": vmid,
            "steps": ["Изучите топологию", "Проверьте результат"], "bootstrap_script": "private"}


class StudentPortalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name, "student.sqlite")
        self.tasks = [task("a", 250), task("b", 252), task("c", 260)]
        self.clock = 1000.0
        self.portal = self.reopen()

    def reopen(self):
        return StudentPortal(self.path, lambda: self.tasks, clock=lambda: self.clock)

    def user(self, number):
        return self.portal.login("student0" + number, "DemoLab0" + number + "!")

    def check_status(self, status, fn, *args):
        with self.assertRaises(StudentError) as err:
            fn(*args)
        self.assertEqual(err.exception.status, status)

    def test_wrong_password_random_session_logout_and_expiry(self):
        self.check_status(401, self.portal.login, "unknown", "DemoLab01!")
        self.check_status(401, self.portal.login, "student01", "wrong")
        self.check_status(401, self.portal.get_dashboard, None)
        self.check_status(401, self.portal.get_dashboard, "x" * 43)
        token, other = self.user("1"), self.user("1")
        self.assertNotEqual(token, other)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertNotIn(token.encode(), [x[0] for x in db.execute("SELECT token_hash FROM student_sessions")])
        self.portal.logout(token)
        self.portal.logout(token)
        self.check_status(401, self.portal.get_dashboard, token)
        self.assertEqual(self.portal.get_dashboard(other)["student"]["login"], "student01")
        self.clock += self.portal.SESSION_TTL
        self.check_status(401, self.portal.deploy, other, "a")

    def test_assignment_isolation_including_shared_assignment(self):
        a, b = self.user("1"), self.user("2")
        dashboard = self.portal.get_dashboard(a)
        self.assertEqual([t["id"] for t in dashboard["assignments"]], ["a", "b"])
        self.assertEqual([t["id"] for t in self.portal.get_dashboard(b)["assignments"]], ["b", "c"])
        self.assertNotIn("bootstrap_script", dashboard["assignments"][0])
        self.check_status(404, self.portal.deploy, a, "c")
        self.check_status(404, self.portal.deploy, b, "a")
        one = self.portal.deploy(a, "b")
        self.check_status(404, self.portal.get_stand, b, one["id"])
        two = self.portal.deploy(b, "b")
        self.assertNotEqual(one["id"], two["id"])
        self.assertNotIn(one["id"], str(self.portal.get_dashboard(b)))
        self.assertNotIn(two["id"], str(self.portal.get_dashboard(a)))
        self.assertEqual(self.portal.get_stand(a, one["id"])["assignment"]["template_vmid"], 252)

    def test_one_and_zero_templates_do_not_assign_unknown_tasks(self):
        self.tasks.extend([task("d", 261), task("e", 262)])
        a, b = self.user("1"), self.user("2")
        self.assertEqual([x["id"] for x in self.portal.get_dashboard(a)["assignments"]], ["a", "b", "d"])
        self.assertEqual([x["id"] for x in self.portal.get_dashboard(b)["assignments"]], ["b", "c", "e"])
        self.tasks[:] = self.tasks[:2]
        self.assertEqual([x["id"] for x in self.portal.get_dashboard(b)["assignments"]], ["b"])
        self.tasks[:] = self.tasks[:1]
        self.assertEqual([x["id"] for x in self.portal.get_dashboard(b)["assignments"]], ["a"])
        self.tasks.clear()
        self.assertEqual(self.portal.get_dashboard(a)["assignments"], [])
        self.check_status(404, self.portal.deploy, b, "a")

    def test_deploy_survives_restart_and_pins_assignment(self):
        token = self.user("1")
        original = self.portal.deploy(token, "a")
        self.assertEqual((original["status"], original["progress"]), ("provisioning", 0))
        self.clock += 4
        self.tasks[0] = task("a", 999)
        reopened = self.reopen()
        self.assertEqual(reopened.get_dashboard(token)["assignments"][0]["template_vmid"], 250)
        self.assertEqual(reopened.deploy(token, "a")["progress"], 50)
        self.clock += 4
        self.tasks.clear()
        again = reopened.deploy(token, "a")
        self.assertEqual((again["status"], again["progress"], again["id"]), ("ready", 100, original["id"]))
        self.assertEqual(reopened.get_dashboard(token)["assignments"][0]["stand"], again)
        self.assertEqual(reopened.get_stand(token, again["id"])["assignment"]["title"], "Задание a")

    def test_concurrent_deployment_requests_share_one_persisted_stand(self):
        barrier = threading.Barrier(6)
        token = self.user("1")
        instances = [self.reopen() for _ in range(6)]
        def submit(client):
            barrier.wait(timeout=8)
            return client.deploy(token, "a")["id"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(submit, instances))
        self.assertEqual(len(set(ids)), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM student_stands").fetchone()[0], 1)

    def test_refuses_to_change_unrelated_database(self):
        foreign = self.path.with_name("foreign.db")
        with closing(sqlite3.connect(foreign)) as db:
            db.execute("CREATE TABLE blueprints (name TEXT)")
        with self.assertRaises(ValueError):
            StudentPortal(foreign, lambda: [])
        with closing(sqlite3.connect(foreign)) as db:
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master").fetchall(), [("blueprints",)])
