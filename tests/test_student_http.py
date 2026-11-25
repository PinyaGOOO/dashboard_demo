import io
import json
import unittest
from email.message import Message
from http.cookies import SimpleCookie
from unittest.mock import MagicMock, patch

from dashboard_backend.student_http import COOKIE, PREFIX, _LoginWindow, dispatch_student
from dashboard_backend.student_portal import StudentError


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.portal = MagicMock()
        self.portal.login.return_value = "test-session-abc"
        self.portal.deploy.return_value = {"id": "stand-1"}
        self.portal.get_stand.return_value = {"stand": {"id": "stand-1"}, "assignment": {"id": "lab:1"}}
        self.portal.get_dashboard.return_value = {"assignments": []}

    def request(self, path, method="GET", *, data=None, raw=None, token=None, headers=None, secure=False):
        h = MagicMock()
        h.path = path
        h.client_address = ("127.0.0.1", 55000)
        h.headers = Message()
        h.headers["Host"] = "demo.test"
        if token is not None:
            h.headers["Cookie"] = f"{COOKIE}={token}"
        if data is not None:
            raw = json.dumps(data).encode("utf-8")
        if raw is not None:
            h.headers["Content-Length"] = str(len(raw))
            h.headers["Content-Type"] = "application/json"
        for key, value in (headers or {}).items():
            if key in h.headers:
                del h.headers[key]
            h.headers[key] = value
        h.rfile = io.BytesIO(raw or b"")
        h.wfile = io.BytesIO()
        self.assertTrue(dispatch_student(h, method, self.portal, secure_cookie=secure))
        headers = {c.args[0]: c.args[1] for c in h.send_header.call_args_list}
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        return h.send_response.call_args.args[0], headers, h.wfile.getvalue()

    def test_scoped_secure_cookie_and_no_secret_in_login_body(self):
        data = {"login": "student01", "password": "fake"}
        status, hdr, body = self.request(PREFIX + "/login", "POST", data=data, secure=True,
                                         headers={"Origin": "https://demo.test:443", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        cookie = SimpleCookie(hdr["Set-Cookie"])[COOKIE]
        self.assertEqual(cookie.value, self.portal.login.return_value)
        self.assertEqual(cookie["path"], "/student-demo/")
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertTrue(cookie["httponly"] and cookie["secure"])
        self.assertFalse(cookie["domain"])

    def test_local_http_cookie_and_logout_and_replacement(self):
        _, hdr, _ = self.request(PREFIX + "/login", "POST", data={"login": "s", "password": "x"},
                                 headers={"Origin": "http://demo.test"}, token="old-session")
        self.assertFalse(SimpleCookie(hdr["Set-Cookie"])[COOKIE]["secure"])
        self.portal.logout.assert_called_once_with("old-session")
        code, hdr, body = self.request(PREFIX + "/logout", "POST", data={}, token="test-session-abc")
        self.assertEqual(code, 200)
        self.assertEqual(SimpleCookie(hdr["Set-Cookie"])[COOKIE]["max-age"], "0")
        self.portal.logout.assert_called_with("test-session-abc")

    def test_rejects_untrusted_post_origins_and_does_not_trust_forwarded(self):
        scenarios = [
            {"Origin": "http://other.test", "X-Forwarded-Host": "other.test"},
            {"Origin": "https://demo.test", "X-Forwarded-Proto": "https"},
            {"Origin": "http://demo.test:8001"}, {"Origin": "http://demo.test:0"},
            {"Origin": "null"}, {"Origin": "http://user@demo.test"}, {"Origin": "http://[bad"},
            {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
        ]
        for header in scenarios:
            with self.subTest(header=header):
                self.assertEqual(self.request(PREFIX + "/assignments/lab-1/deploy", "POST", data={},
                                              token="t", headers=header)[0], 403)
        self.portal.deploy.assert_not_called()

    def test_public_login_options_only_expose_demo_profiles(self):
        self.portal.demo_profiles.return_value = ["student02"]
        code, _, body = self.request(PREFIX + "/login-options")
        self.assertEqual((code, json.loads(body)), (200, {"demo_profiles": ["student02"]}))

    def test_guest_copy_needs_only_session_and_scoped_empty_post(self):
        path = PREFIX + "/stands/abc/credentials"
        self.portal.guest_action.return_value = {"username":"root@pam", "password":"fixture"}
        self.assertEqual(self.request(path, "GET", token="s")[0],405)
        self.assertEqual(self.request(path, "POST", data={})[0],401)
        for extra in ({"vmid":123}, {"access_code":"X"}, {"login":"student01"}):
            self.assertEqual(self.request(path, "POST", token="s", data=extra)[0],400)
        self.assertEqual(self.request(path, "POST", token="s", data={}, headers={"Origin":"https://wrong.example"})[0],403)
        self.portal.guest_action.assert_not_called()
        code, hdr, body = self.request(path,"POST", token="s", data={})
        self.assertEqual((code,hdr["Cache-Control"],json.loads(body)["password"]), (200,"no-store","fixture"))
        self.portal.guest_action.assert_called_once_with("s","abc","credentials",{})

    def test_only_scoped_session_cookie_authenticates(self):
        for hdr in [{"Authorization": "Bearer admin"}, {"X-Admin-Token": "admin"},
                    {"Cookie": f"{COOKIE}=a; {COOKIE}=b"}, {"Cookie": f"{COOKIE}=no+good"}]:
            with self.subTest(hdr=hdr):
                self.assertEqual(self.request(PREFIX + "/me?token=a", headers=hdr)[0], 401)
        self.portal.get_dashboard.assert_not_called()
        status, _, body = self.request(PREFIX + "/me", token="t")
        self.assertEqual((status, json.loads(body)), (200, {"assignments": []}))
        self.portal.get_dashboard.assert_called_once_with("t")

    def test_guarded_deploy_and_detail(self):
        status, _, body = self.request(PREFIX + "/assignments/lab:1/deploy", "POST", data={}, token="my-session")
        self.assertEqual((status, json.loads(body)), (200, {"stand": {"id": "stand-1"}}))
        self.portal.deploy.assert_called_once_with("my-session", "lab:1")
        self.request(PREFIX + "/stands/stand-1", token="t")
        self.portal.get_stand.assert_called_with("t", "stand-1")
        self.portal.get_stand.side_effect = StudentError(404, "Стенд не найден.")
        self.assertEqual(self.request(PREFIX + "/stands/foreign", token="t")[0], 404)
        self.assertEqual(self.request(PREFIX + "/assignments/lab:1/deploy", "POST",
                                      data={"student_id": 2}, token="t")[0], 400)

    def test_invalid_bodies_and_payload_limits(self):
        for raw, headers, expected in [
            (b"[]", {}, 400), (b"{", {}, 400), (b"\xff", {}, 400),
            (b"{}", {"Content-Length": "-2"}, 400), (b"{}", {"Content-Length": "5000"}, 413),
            (b"{}", {"Content-Type": "text/plain"}, 415), (b"{}", {"Transfer-Encoding": "chunked"}, 400),
            (b"{}", {"Content-Length": "3"}, 400), (b'{"login":[],"password":"hi"}', {}, 400),
        ]:
            with self.subTest(raw=raw, headers=headers):
                self.assertEqual(self.request(PREFIX + "/login", "POST", raw=raw, headers=headers)[0], expected)
        self.portal.login.assert_not_called()

    def test_namespace_and_methods(self):
        h = MagicMock()
        for path in ("/api/stands", "/student-demo/api-other/me", "/student-demo/"):
            h.path = path
            self.assertFalse(dispatch_student(h, "GET", self.portal))
        for suffix in ("", "/absent", "/assignments/../deploy", "/stands/a%2Fb", "/stands/../a"):
            self.assertEqual(self.request(PREFIX + suffix)[0], 404)
        code, hdr, body = self.request(PREFIX + "/login", "HEAD")
        self.assertEqual((code, hdr["Allow"], body), (405, "POST", b""))

    def test_bounded_failed_login_budget_good_logins_do_not_consume_it(self):
        with patch("dashboard_backend.student_http._logins", _LoginWindow(limit=2)):
            for _ in range(4):
                self.assertEqual(self.request(PREFIX + "/login", "POST", data={"login": "s", "password": "x"})[0], 200)
            self.portal.login.side_effect = StudentError(401, "Неверный логин или пароль.")
            for _ in range(2):
                self.assertEqual(self.request(PREFIX + "/login", "POST", data={"login": "s", "password": "x"})[0], 401)
            status, hdr, _ = self.request(PREFIX + "/login", "POST", data={"login": "s", "password": "x"})
            self.assertEqual((status, hdr["Retry-After"]), (429, "60"))

    def test_unexpected_errors_are_sanitized(self):
        self.portal.get_dashboard.side_effect = RuntimeError("secret internal data")
        code, _, body = self.request(PREFIX + "/me", token="s")
        self.assertEqual(code, 500)
        self.assertNotIn(b"secret", body)
