import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from dashboard_backend.student_mount import StudentMount


class StudentMountTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "student.sqlite"
        self.catalog = Path(temp.name) / "catalog.json"
        self.mount = StudentMount(self.db, self.catalog, secure_cookie=False, example_if_missing=False)
        case = self

        class Handler(BaseHTTPRequestHandler):
            def serve(self):
                if not case.mount.handle(self, self.command):
                    self.send_error(418, "Outside student demo")

            do_GET = do_POST = do_HEAD = serve

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = server.server_address
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

    def request(self, path, method="GET", *, data=None, headers=None):
        conn = http.client.HTTPConnection(*self.address, timeout=5)
        try:
            hdr = dict(headers or {})
            if data is not None:
                hdr["Content-Type"] = "application/json"
            conn.request(method, path, body=None if data is None else json.dumps(data), headers=hdr)
            resp = conn.getresponse()
            return resp.status, resp.headers, resp.read()
        finally:
            conn.close()

    def test_redirect_index_and_whitelisted_assets_get_and_head(self):
        code, hdr, _ = self.request("/student-demo?something")
        self.assertEqual((code, hdr["Location"]), (308, "/student-demo/"))
        for suffix, mime in (("", "text/html"), ("index.html", "text/html"),
                             ("student.css", "text/css"), ("student.js?v=1", "text/javascript")):
            with self.subTest(suffix=suffix):
                path = "/student-demo/" + suffix
                code, hdr, body = self.request(path)
                self.assertEqual((code, hdr.get_content_type()), (200, mime))
                self.assertGreater(len(body), 0)
                self.assertEqual(hdr["X-Content-Type-Options"], "nosniff")
                self.assertEqual(hdr["Cache-Control"], "no-cache")
                code, hdr, head = self.request(path, "HEAD")
                self.assertEqual((code, hdr["Content-Length"], head), (200, str(len(body)), b""))
        self.assertFalse(self.db.exists())

    def test_unknown_routes_never_reach_outer_site(self):
        for path in ("/student-demo/unknown", "/student-demo/../app.js", "/student-demo/%2e%2e/app.js",
                     "/student-demo%2Fapp.js", "/student-demo/api", "/student-demo/api/unknown"):
            with self.subTest(path=path):
                code, hdr, _ = self.request(path)
                self.assertEqual((code, hdr["Cache-Control"]), (404, "no-store"))
        self.assertEqual(self.request("/student-demonstration")[0], 418)
        self.assertEqual(self.request("/api/operator")[0], 418)
        self.assertEqual(self.request("/student-demo/api/me")[0], 401)
        code, hdr, _ = self.request("/student-demo/index.html", "POST", data={})
        self.assertEqual((code, hdr["Allow"]), (405, "GET, HEAD"))
        self.assertFalse(self.db.exists())

    def test_login_creates_separate_database_then_dashboard_uses_catalog_mode(self):
        fields = {"login": "student01", "password": "DemoLab01!"}
        code, hdr, _ = self.request("/student-demo/api/login", "POST", data=fields)
        self.assertEqual(code, 200)
        self.assertTrue(self.db.is_file())
        with patch("dashboard_backend.student_mount.load_catalog", return_value=[]) as load:
            cookie = hdr["Set-Cookie"].split(";", 1)[0]
            code, _, body = self.request("/student-demo/api/me", headers={"Cookie": cookie})
        self.assertEqual((code, json.loads(body)["assignments"]), (200, []))
        load.assert_called_once_with(self.catalog, example_if_missing=False)
        self.assertFalse(self.catalog.exists())

    def test_refuses_alias_to_catalog_without_writing(self):
        self.mount = StudentMount(self.catalog, self.catalog, secure_cookie=False)
        self.assertEqual(self.request("/student-demo/")[0], 200)
        code, hdr, body = self.request("/student-demo/api/login", "POST",
                                       data={"login": "student01", "password": "DemoLab01!"})
        self.assertEqual((code, hdr["Cache-Control"]), (503, "no-store"))
        self.assertNotIn(str(self.catalog), body.decode())
        self.assertFalse(self.catalog.exists())
