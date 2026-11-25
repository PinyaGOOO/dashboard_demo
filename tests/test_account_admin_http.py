"""End-to-end account administration on loopback, with an isolated student database."""
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from dashboard_backend.student_http import _LoginWindow
from dashboard_backend.student_mount import StudentMount
from server import DashboardHandler


ACCOUNTS = "/api/student-accounts"
STUDENT = "/student-demo/api"
TOKEN = ("X-Admin-Token", "test-operator-only")


class AccountAdminHTTP(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        limiter = patch("dashboard_backend.student_http._logins", _LoginWindow())
        limiter.start()
        self.addCleanup(limiter.stop)

        class Handler(DashboardHandler):
            admin_token = TOKEN[1]
            student_mount = StudentMount(root / "students.sqlite", root / "catalog.json", secure_cookie=True)

            def log_message(self, *args):
                pass

        self.handler = Handler
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = server.server_address
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

    def req(self, path, data=None, headers=()):
        """Keep separate physical headers so duplicate-token rejection is actually tested."""
        conn = http.client.HTTPConnection(*self.address, timeout=10)
        try:
            conn.putrequest("GET" if data is None else "POST", path)
            for name, value in headers:
                conn.putheader(name, value)
            body = None
            if data is not None:
                body = json.dumps(data).encode()
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            resp = conn.getresponse()
            self.assertEqual(resp.getheader("Cache-Control"), "no-store")
            return resp.status, json.loads(resp.read()), resp.headers
        finally:
            conn.close()

    def issue(self, count=1, group="Учебная группа"):
        status, result, _ = self.req(ACCOUNTS + "/batch", {"count": count, "group": group}, [TOKEN])
        self.assertEqual(status, 201)
        return result["accounts"]

    def listed(self, **params):
        path = ACCOUNTS + ("?" + urlencode(params) if params else "")
        status, result, _ = self.req(path, headers=[TOKEN])
        self.assertEqual(status, 200)
        return result

    def version(self, login):
        (account,) = self.listed(search=login)["accounts"]
        return account["version"]

    def action(self, login, action, version=None):
        if version is None:
            version = self.version(login)
        return self.req(f"{ACCOUNTS}/{login}/{action}", {"version": version}, [TOKEN])

    def login(self, login, password, *, expected=200):
        status, _, headers = self.req(STUDENT + "/login", {"login": login, "password": password})
        self.assertEqual(status, expected)
        if status == 200:
            return "Cookie", headers["Set-Cookie"].split(";", 1)[0]

    def me(self, cookie):
        return self.req(STUDENT + "/me", headers=[cookie])[0]

    def test_student_cookie_query_and_duplicate_or_unconfigured_token_do_not_authorize(self):
        (person,) = self.issue()
        login = person["login"]
        student_cookie = self.login(login, person["password"])
        for path, data in [(ACCOUNTS, None)] + [
            (f"{ACCOUNTS}/{login}/{action}", {"version": 1})
            for action in ("reset-password", "block", "unblock", "revoke-sessions")
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.req(path, data)[0], 401)
                self.assertEqual(self.req(path, data, [student_cookie])[0], 401)
                self.assertEqual(self.req(path + "?token=" + TOKEN[1], data)[0], 401)
                self.assertEqual(self.req(path, data, [("X-Admin-Token", "wrong")])[0], 401)
                self.assertEqual(self.req(path, data, [TOKEN, TOKEN])[0], 401)
                with patch.object(self.handler, "admin_token", ""):
                    self.assertEqual(self.req(path, data, [TOKEN])[0], 401)
                self.assertEqual(self.req("/student-demo" + path, data, [student_cookie, TOKEN])[0], 404)
        self.assertEqual(self.me(student_cookie), 200)

    def test_rejects_cross_origin_change_without_mutating_account(self):
        (person,) = self.issue()
        login = person["login"]
        path = f"{ACCOUNTS}/{login}/block"
        initial = self.version(login)
        for extra in ([("Origin", "https://another.example")], [("Sec-Fetch-Site", "cross-site")]):
            with self.subTest(extra=extra):
                self.assertEqual(self.req(path, {"version": initial}, [TOKEN, *extra])[0], 403)
        self.assertEqual(self.version(login), initial)
        self.login(login, person["password"])
        # A normal HTTPS browser behind the operator reverse proxy sends Origin on POST.
        host, port = self.address
        same_site = [TOKEN, ("Origin", f"https://{host}:{port}"), ("Sec-Fetch-Site", "same-origin")]
        self.assertEqual(self.req(path, {"version": initial}, same_site)[0], 200)

    def test_directory_paginates_filters_and_does_not_disclose_credentials(self):
        first, second = self.issue(2, "Группа А")
        (third,) = self.issue(1, "Группа Б")
        full = self.listed()
        self.assertEqual({u["login"] for u in full["accounts"]},
                         {p["login"] for p in (first, second, third)})
        for p in (first, second, third):
            self.assertNotIn(p["password"], json.dumps(full))
        for u in full["accounts"]:
            self.assertEqual(set(u), {"login", "group", "blocked", "version", "created", "last_login"})
            self.assertIs(u["blocked"], False)
        pages = [self.listed(search="группа а", limit=1, offset=o) for o in range(3)]
        self.assertTrue(all(p["total"] == 2 for p in pages))
        self.assertEqual({p["accounts"][0]["login"] for p in pages[:2]}, {first["login"], second["login"]})
        self.assertEqual(pages[2]["accounts"], [])
        self.assertEqual(self.listed(search="no-such-login")["total"], 0)

    def test_invalid_pagination_and_unguarded_changes_rejected(self):
        (person,) = self.issue()
        login = person["login"]
        initial = self.version(login)
        for query in ("limit=0", "limit=101", "offset=-1", "offset=NaN"):
            with self.subTest(query=query):
                self.assertEqual(self.req(ACCOUNTS + "?" + query, headers=[TOKEN])[0], 400)
        path = f"{ACCOUNTS}/{login}/reset-password"
        for body in ({}, {"version": True}, {"version": initial, "password": "not-accepted"}):
            with self.subTest(body=body):
                status, result, _ = self.req(path, body, [TOKEN])
                self.assertEqual(status, 400)
                self.assertNotIn("password", result)
        self.assertEqual(self.version(login), initial)
        self.login(login, person["password"])

    def test_reset_block_unblock_revoke_and_stale_actions(self):
        person, other = self.issue(2)
        login = person["login"]
        other_cookie = self.login(other["login"], other["password"])
        old = self.login(login, person["password"])
        initial = self.version(login)
        status, reset, _ = self.action(login, "reset-password", initial)
        self.assertEqual(status, 200)
        new = reset["password"]
        self.assertNotEqual(new, person["password"])
        self.assertNotIn(new, json.dumps(self.listed(search=login)))
        self.assertEqual(self.me(old), 401)
        self.login(login, person["password"], expected=401)
        first = self.login(login, new)
        second = self.login(login, new)
        self.assertEqual(self.action(login, "block", initial)[0], 409)
        self.assertEqual(self.me(first), 200)
        self.assertEqual(self.action(login, "block")[0], 200)
        self.assertEqual([self.me(c) for c in (first, second)], [401, 401])
        self.login(login, new, expected=401)
        status, reset, _ = self.action(login, "reset-password")
        self.assertEqual(status, 200)
        while_blocked = reset["password"]
        self.assertIs(self.listed(search=login)["accounts"][0]["blocked"], True)
        self.login(login, while_blocked, expected=401)
        self.assertEqual(self.action(login, "unblock")[0], 200)
        self.assertEqual(self.me(second), 401)
        active = self.login(login, while_blocked)
        self.assertEqual(self.action(login, "revoke-sessions")[0], 200)
        self.assertEqual(self.me(active), 401)
        self.assertEqual(self.me(self.login(login, while_blocked)), 200)
        self.assertEqual(self.me(other_cookie), 200)

    def test_builtin_personas_are_not_manageable(self):
        for login in ("student01", "student02"):
            self.assertEqual(self.listed(search=login)["total"], 0)
            for action in ("reset-password", "block", "unblock", "revoke-sessions"):
                with self.subTest(login=login, action=action):
                    status, result, _ = self.action(login, action, version=1)
                    self.assertIn(status, (400, 403, 404))
                    self.assertNotIn("password", result)
        self.login("student02", "DemoLab02!")

    def test_group_order_is_global_before_pagination_and_search(self):
        first, = self.issue(group="Язык")
        middle, = self.issue(group="Алгебра")
        last, = self.issue(group="алгебра")
        for order, expected in (("group", [middle, last, first]), ("group-desc", [first, middle, last]),
                                ("login", [first, middle, last])):
            pages = [self.listed(search="", limit=1, offset=i, sort=order) for i in range(3)]
            self.assertEqual([p["accounts"][0]["login"] for p in pages], [r["login"] for r in expected])
            self.assertTrue(all(p["total"] == 3 for p in pages))
        self.assertEqual(self.listed(search="АЛГЕБ", sort="group")["total"], 2)
        for query in ("sort=unknown", "sort=group&sort=login"):
            self.assertEqual(self.req(ACCOUNTS + "?" + query, headers=[TOKEN])[0], 400)

    def test_history_and_confirmed_delete_are_private_and_scoped(self):
        person, other = self.issue(2)
        login = person["login"]
        prefix = f"{ACCOUNTS}/{login}"
        history, delete = prefix + "/history", prefix + "/delete"
        self.assertEqual(self.req(history, headers=[TOKEN])[1]["entries"], [])
        cookie = self.login(login, person["password"])
        self.login(login, "not-the-password", expected=401)
        other_cookie = self.login(other["login"], other["password"])
        status, data, _ = self.req(history, headers=[TOKEN])
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"login", "entries", "limit"})
        self.assertEqual((data["login"], data["limit"]), (login, 50))
        event, = data["entries"]
        self.assertEqual(set(event), {"at"})
        self.assertGreater(event["at"], 0)
        body = {"version": self.version(login), "confirm_login":login}
        for path, payload in ((history, None), (delete, body)):
            for headers in ([], [cookie], [TOKEN, TOKEN]):
                self.assertEqual(self.req(path, payload, headers)[0], 401)
            self.assertEqual(self.req(path, payload, [TOKEN, ("Origin", "https://foreign.example")])[0], 403)
            self.assertEqual(self.req("/student-demo" + path, payload, [TOKEN, cookie])[0], 404)
        self.assertEqual(self.req(delete, headers=[TOKEN])[0], 404)  # Following a link must never delete.
        self.assertEqual(self.req(delete, {"version":body["version"]}, [TOKEN])[0], 400)
        self.assertEqual(self.req(delete, dict(body, confirm_login=other["login"]), [TOKEN])[0], 400)
        self.assertEqual(self.req(delete, dict(body, version=body["version"]+1), [TOKEN])[0], 409)
        self.assertEqual(self.me(cookie), 200)
        status, result, _ = self.req(delete, body, [TOKEN])
        self.assertEqual((status, result), (200, {"login":login, "deleted":True}))
        self.assertEqual(self.me(cookie), 401)
        self.login(login, person["password"], expected=401)
        self.assertEqual(self.req(history, headers=[TOKEN])[0], 404)
        self.assertEqual(self.req(delete, body, [TOKEN])[0], 404)
        self.assertEqual(self.listed(search=login)["total"], 0)
        self.assertEqual(self.me(other_cookie), 200)
        for reserved in ("student01", "student02"):
            self.assertEqual(self.req(f"{ACCOUNTS}/{reserved}/history", headers=[TOKEN])[0], 404)
            self.assertEqual(self.req(f"{ACCOUNTS}/{reserved}/delete",
                                      {"version":1, "confirm_login":reserved}, [TOKEN])[0], 404)
