import concurrent.futures
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from dashboard_backend.student_portal import StudentError, StudentPortal, read_pilot_request


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

    def test_timed_demo_starts_at_readiness_and_renews_only_after_expiry(self):
        token = self.user("1")
        for bad in (0, True, -1, 10081, "1", 0.5):
            self.check_status(400, self.portal.deploy, token, "b", bad)
        first = self.portal.deploy(token, "b", 1)
        self.assertEqual(first["duration_minutes"], 1)
        self.clock += self.portal.DEPLOY_SECONDS + 59
        reopened = self.reopen()
        self.assertEqual(reopened.deploy(token, "b", 45)["id"], first["id"])
        self.assertEqual(reopened.get_stand(token, first["id"])["stand"]["status"], "ready")
        self.clock += 1
        self.assertEqual(reopened.get_stand(token, first["id"])["stand"]["status"], "expired")
        new = reopened.deploy(token, "b", 45)
        self.assertNotEqual(new["id"], first["id"])
        self.assertEqual(new["duration_minutes"], 45)
        self.check_status(404, reopened.get_stand, token, first["id"])

    def private_portal(self, validator):
        grant, setter = Mock(), Mock()
        grant.private_login_required.side_effect = lambda login: login == "student01"
        grant.session_stamp.return_value = "opaque-binding-key-stamp"
        grant.authenticate_login.side_effect = lambda login, password: (
            grant.session_stamp(login) if password == "test-personal-pass" else None)
        grant.lease.return_value = None
        self.portal = StudentPortal(self.path, lambda: self.tasks, clock=lambda: self.clock,
                                    pilot_validator=validator, pilot_grant=grant, set_duration=setter)
        return grant, setter

    def test_private_login_revokes_legacy_public_session_without_changing_peer(self):
        legacy, stand_id, validator, _, _ = self.pilot_fixture()
        peer = self.user("2")
        self.assertEqual(self.portal.demo_profiles(), ["student01", "student02"])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TABLE student_private_sessions")  # previous release's schema
        grant, setter = self.private_portal(validator)
        self.assertEqual(self.portal.demo_profiles(), ["student02"])
        self.check_status(401, self.portal.login, "student01", "DemoLab01!")
        self.assertEqual(self.portal.get_dashboard(peer)["student"]["login"], "student02")
        self.assertEqual(self.portal.get_dashboard(self.user("2"))["student"]["login"], "student02")
        self.check_status(401, self.portal.get_dashboard, legacy)
        self.check_status(401, self.portal.deploy, legacy, "b")
        self.check_status(401, self.portal.get_stand, legacy, stand_id)
        self.check_status(401, self.portal.guest_action, legacy, stand_id, "credentials", {})
        self.check_status(401, self.portal.guest_action, legacy, stand_id, "duration",
                          {"duration_minutes": 45, "access_code": "test-personal-pass"})
        grant.for_session.assert_not_called()
        grant.authorize.assert_not_called()
        setter.assert_not_called()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM student_sessions WHERE login='student01'").fetchone()[0], 0)
        # An older worker could still insert an unstamped row after migration.
        late_public = self.reopen().login("student01", "DemoLab01!")
        self.check_status(401, self.portal.get_dashboard, late_public)
        self.check_status(401, self.portal.guest_action, late_public, stand_id, "credentials", {})
        grant.for_session.assert_not_called()

    def test_guest_actions_accept_only_private_owner_of_current_real_stand(self):
        _, stand_id, validator, _, binding = self.pilot_fixture()
        grant, setter = self.private_portal(validator)
        token = self.portal.login("student01", "test-personal-pass")
        self.check_status(404, self.portal.guest_action, self.user("2"), stand_id, "credentials", {})
        ordinary = self.portal.deploy(token, "a")
        self.check_status(404, self.portal.guest_action, token, ordinary["id"], "credentials", {})
        self.check_status(400, self.portal.guest_action, token, stand_id, "credentials", {"vmid": 101})
        grant.for_session.assert_not_called()
        self.assertIs(self.portal.guest_action(token, stand_id, "credentials", {}), grant.for_session.return_value)
        grant.for_session.assert_called_once_with(binding, "opaque-binding-key-stamp")
        grant.authorize.assert_not_called()
        grant.authorize.side_effect = StudentError(403, "Неверный код")
        self.check_status(403, self.portal.guest_action, token, stand_id, "duration", {"access_code":"wrong", "duration_minutes": 40})
        setter.assert_not_called()
        grant.authorize.side_effect = None
        self.portal.guest_action(token, stand_id, "duration", {"access_code":"test", "duration_minutes":40})
        setter.assert_called_once_with(binding, 40)

    def test_private_session_stamp_is_revocable_and_never_stores_the_password(self):
        _, stand_id, validator, _, _ = self.pilot_fixture()
        grant, setter = self.private_portal(validator)
        token = self.portal.login("student01", "test-personal-pass")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT * FROM student_private_sessions").fetchall(),
                             [(self.portal._token_hash(token), "opaque-binding-key-stamp")])
            self.assertNotIn("test-personal-pass", "".join(db.iterdump()))
        self.assertEqual(self.portal.get_dashboard(token)["student"]["login"], "student01")
        for current in (None, "rotated-or-rebound-stamp"):
            grant.session_stamp.return_value = current
            self.check_status(401, self.portal.get_dashboard, token)
            self.check_status(401, self.portal.get_stand, token, stand_id)
            self.check_status(401, self.portal.guest_action, token, stand_id, "credentials", {})
            self.check_status(401, self.portal.guest_action, token, stand_id, "duration",
                              {"duration_minutes": 45, "access_code": "test-personal-pass"})
        grant.for_session.assert_not_called()
        grant.authorize.assert_not_called()
        setter.assert_not_called()
        # Neither standalone mode nor disabling the grant converts an old private session to demo.
        self.check_status(401, self.reopen().get_dashboard, token)
        fresh = self.portal.login("student01", "test-personal-pass")
        self.assertEqual(self.portal.get_stand(fresh, stand_id)["stand"]["id"], stand_id)
        self.portal.logout(fresh)
        self.check_status(401, self.portal.guest_action, fresh, stand_id, "credentials", {})
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM student_private_sessions WHERE token_hash=?",
                                        (self.portal._token_hash(fresh),)).fetchone()[0], 0)
        fresh = self.portal.login("student01", "test-personal-pass")
        self.clock += self.portal.SESSION_TTL
        self.check_status(401, self.portal.guest_action, fresh, stand_id, "credentials", {})
        grant.for_session.assert_not_called()

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

    def pilot_fixture(self):
        validator = Mock(return_value=True)
        self.portal = StudentPortal(self.path, lambda: self.tasks, clock=lambda: self.clock,
                                    pilot_validator=validator)
        token = self.user("1")
        stand_id = self.portal.deploy(token, "b")["id"]
        binding = dict(student_login="student01", assignment_id="b", stand_id=stand_id,
                       deployer_stand_id=17, pool_id="pilot-one", guest_ipv4="10.39.11.15", vmid=350)
        config = self.path.with_name("student-pilot.json")
        config.write_text(json.dumps(binding), encoding="utf-8")
        self.clock += self.portal.DEPLOY_SECONDS
        return token, stand_id, validator, config, binding

    def test_pilot_shows_only_verified_guest_url_to_owner_on_detail(self):
        token, stand_id, validator, config, binding = self.pilot_fixture()
        self.assertNotIn("access", self.portal.deploy(token, "b"))
        self.assertNotIn("access", self.portal.get_dashboard(token)["assignments"][1]["stand"])
        peer = self.user("2")
        self.check_status(404, self.portal.get_stand, peer, stand_id)
        peer_stand = self.portal.deploy(peer, "b")
        self.clock += self.portal.DEPLOY_SECONDS
        self.assertNotIn("access", self.portal.get_stand(peer, peer_stand["id"])["stand"])
        validator.assert_not_called()
        detail = self.portal.get_stand(token, stand_id)
        self.assertEqual(detail["stand"]["access"], {
            "kind": "proxmox", "url": "https://10.39.11.15:8006/", "vmid": 350, "login": "root@pam"})
        self.assertNotIn("access", detail["assignment"]["stand"])
        validator.assert_called_once_with(binding)
        config.unlink()
        self.assertNotIn("access", self.portal.get_stand(token, stand_id)["stand"])
        validator.assert_called_once()

    def test_pilot_requires_live_validator_and_exact_binding(self):
        token, stand_id, validator, config, binding = self.pilot_fixture()
        for field, invalid in [
                ("student_login", "student02"), ("assignment_id", "unknown"), ("stand_id", "unrelated"),
                ("vmid", True), ("vmid", 1), ("vmid", 10**10), ("deployer_stand_id", 0), ("pool_id", "../admin"),
                ("guest_ipv4", "localhost"), ("guest_ipv4", "127.0.0.1"), ("guest_ipv4", "169.254.169.254"),
                ("guest_ipv4", "https://10.1.1.1:8006/"), ("guest_ipv4", "8.8.8.8"), ("guest_ipv4", "::1"),
                ("guest_ipv4", "10.1.1.1@cluster.local"), ("password", "must-not-leak")]:
            with self.subTest(field=field, invalid=invalid):
                config.write_text(json.dumps({**binding, field: invalid}), encoding="utf-8")
                self.assertNotIn("access", self.portal.get_stand(token, stand_id)["stand"])
        for raw in ("{", "[]", " " * 4100, '{"guest_ipv4":"172.16.0.1"}'):
            config.write_text(raw, encoding="utf-8")
            self.assertNotIn("access", self.portal.get_stand(token, stand_id)["stand"])
        validator.assert_not_called()
        config.write_text(json.dumps(binding), encoding="utf-8")
        self.assertNotIn("access", self.reopen().get_stand(token, stand_id)["stand"])
        validator.return_value = False
        self.assertNotIn("access", self.portal.get_stand(token, stand_id)["stand"])
        validator.return_value = 1
        self.assertNotIn("access", self.portal.get_stand(token, stand_id)["stand"])
        validator.side_effect = RuntimeError("private internal issue")
        self.assertNotIn("access", self.portal.get_stand(token, stand_id)["stand"])

    def test_pilot_request_private_pending_then_verified_access(self):
        token, stand_id, validator, config, binding = self.pilot_fixture()
        config.unlink()
        request = {k: v for k, v in binding.items() if k != "vmid"}
        path = config.with_name("student-pilot-request.json")
        path.write_text(json.dumps(request), encoding="utf-8")
        self.assertEqual(read_pilot_request(path), request)
        before = self.reopen().get_stand(token, stand_id)["stand"]
        self.assertNotIn("pilot_pending", before)  # standalone never advertises a real pilot
        self.assertEqual(self.portal.get_stand(token, stand_id)["stand"], dict(before, pilot_pending=True))
        peer = self.user("2")
        self.check_status(404, self.portal.get_stand, peer, stand_id)
        own = self.portal.deploy(peer, "b")
        self.assertNotIn("pilot_pending", self.portal.get_stand(peer, own["id"])["stand"])
        self.assertNotIn("pilot_pending", self.portal.get_dashboard(token)["assignments"][1]["stand"])
        validator.assert_not_called()
        config.write_text(json.dumps(binding), encoding="utf-8")
        validator.return_value = False
        self.assertEqual(self.portal.get_stand(token, stand_id)["stand"], dict(before, pilot_pending=True))
        validator.return_value = True
        now = self.portal.get_stand(token, stand_id)["stand"]
        self.assertNotIn("pilot_pending", now)
        self.assertEqual(now["access"]["vmid"], binding["vmid"])
        config.unlink()
        for key, wrong in [("student_login", "student02"), ("stand_id", "5"*24), ("assignment_id", "other")]:
            path.write_text(json.dumps({**request, key: wrong}), encoding="utf-8")
            self.assertEqual(self.portal.get_stand(token, stand_id)["stand"], before)
        for wrong in [binding, {**request, "student_login": []}, {**request, "guest_ipv4": "8.8.8.8"},
                      {**request, "stand_id": "bad"}, {**request, "pool_id": ""}, {}]:
            path.write_text(json.dumps(wrong), encoding="utf-8")
            self.assertIsNone(read_pilot_request(path))
            self.assertEqual(self.portal.get_stand(token, stand_id)["stand"], before)
        path.unlink()
        self.assertIsNone(read_pilot_request(path))
