"""Optional offline compatibility smoke test: python check_host.py /path/to/reviewed/app.py

Pass only a *trusted and reviewed* copy of Netflow's production source. The
script patches a temporary copy, then exercises the host's actual WSGI, session
and transaction handlers with a fresh synthetic database. It never imports or
initializes the host app, uses a real account, or needs an SSH/API credential.
"""
import argparse
import ast
from contextlib import contextmanager
from hashlib import sha256
import hashlib
import hmac
from http.cookies import SimpleCookie
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

try:
    from . import stand_routes
except ImportError:  # directly invoked beside the site-side modules
    import stand_routes


def check(source):
    with tempfile.TemporaryDirectory(prefix="netflow-contract-") as scratch:
        root = Path(scratch)
        original = Path(source).read_bytes()
        host = root / "app.py"
        host.write_bytes(original)
        change = str(Path(__file__).with_name("app.py.patch").resolve())
        for options in (["--check"], []):
            subprocess.run(["git", "apply", "--no-index", *options, change], cwd=root, check=True)
        syntax = ast.parse(host.read_text(encoding="utf-8"))
        # Compile just the five source definitions; do not run the host's imports,
        # application init, catalogue loader, password setup or other module code.
        names = {"APIError", "digest", "connect", "endpoint", "application"}
        selected = [n for n in syntax.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
        if len(selected) != len(names):
            raise ValueError("Netflow WSGI/auth interface has changed; review the integration first")
        db = str(root / "mock.sqlite3")
        origin, cookie = "https://school.invalid", "netflow-test"
        namespace = dict(DB=db, ORIGIN=origin, COOKIE=cookie, stand_routes=stand_routes,
                         hashlib=hashlib, json=json, hmac=hmac, time=time, sqlite3=sqlite3,
                         contextmanager=contextmanager, SimpleCookie=SimpleCookie,
                         sysadmin_save=SimpleNamespace(PATH="/never-used", MAX_REQUEST=16384))
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(host), "exec"), namespace)
        c = sqlite3.connect(db)
        try:
            c.executescript("""
                CREATE TABLE users(id INTEGER PRIMARY KEY, grp TEXT, active INTEGER, admin INTEGER, must_change INTEGER);
                CREATE TABLE sessions(token TEXT PRIMARY KEY, user_id INTEGER, csrf TEXT, expires INTEGER, last_seen INTEGER);
                CREATE TABLE rounds(expires INTEGER);
                CREATE TABLE limits(until INTEGER);
                INSERT INTO users VALUES (1,'Lab-A',1,0,0),(2,'Other',1,0,0);
            """)
            now = int(time.time())
            c.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)",
                          [(sha256(token.encode()).hexdigest(), uid, "mock-csrf", now+600, now)
                           for uid, token in enumerate(("one", "two"), 1)])
            c.commit()
            run_requests(namespace["application"], c, origin, cookie)
        finally:
            c.close()


def run_requests(application, db, origin, cookie):
    assignment = "lab-2-276"
    worker_key = "local-synthetic-worker-token-do-not-use-in-production"
    cfg = {"NETFLOW_STANDS_ENABLED": "1", "NETFLOW_STAND_SCENARIOS": json.dumps({assignment: ["Lab-A"]}),
           "NETFLOW_WORKER_TOKEN": worker_key}
    stand, claim, status = stand_routes.STAND, stand_routes.CLAIM, stand_routes.STATUS

    def call(expected, route, method="GET", data=None, **headers):
        body = json.dumps(data).encode() if data is not None else b""
        env = {"REQUEST_METHOD": method, "PATH_INFO": route, "CONTENT_TYPE": "application/json",
               "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), **headers}
        seen = []
        out = json.loads(b"".join(application(env, lambda line, hdr: seen.append(int(line.split()[0])))))
        if seen != [expected]:
            raise AssertionError(f"{method} {route}: expected {expected}; got {seen}")
        return out

    first = {"HTTP_COOKIE": f"{cookie}=one"}
    browser = dict(first, HTTP_ORIGIN=origin, HTTP_X_CSRF_TOKEN="mock-csrf")
    worker = {"HTTP_AUTHORIZATION": "Bearer "+worker_key}
    data = {"assignment_id": assignment}
    with patch.dict("os.environ", cfg):
        stand_routes._configured.cache_clear()
        try:
            call(401, stand)
            call(403, stand, "POST", data, **first, HTTP_ORIGIN=origin)  # no CSRF
            call(403, stand, "POST", data, **dict(browser, HTTP_ORIGIN="https://other.invalid"))
            call(401, stand, "POST", data, **browser, HTTP_X_ACCOUNT_ID="2")
            call(403, stand, "POST", data, **dict(browser, HTTP_COOKIE=f"{cookie}=two"))
            call(400, stand, "POST", dict(data, user_id=2), **browser)
            got = call(200, stand, "POST", data, **browser)["stand"]
            if call(200, stand, "POST", data, **browser)["stand"]["job_id"] != got["job_id"]:
                raise AssertionError("Duplicate browser POST allocated a second job")
            call(401, claim, "POST", {})
            call(403, claim, "POST", {}, **worker, **first)
            job = call(200, claim, "POST", {}, **worker)["job"]
            if not job or job["job_id"] != got["job_id"] or job["group"] != "Lab-A":
                raise AssertionError("Site/worker contract mismatch")
            link = "https://10.39.1.240:8006/"
            call(200, status, "POST", {"job_id": job["job_id"], "lease_token": job["lease_token"],
                 "status": "ready", "progress": 100, "web_url": link}, **worker)
            if (call(200, stand, **first)["stand"]["web_url"] != link
                    or call(200, stand, HTTP_COOKIE=f"{cookie}=two")["stand"] is not None):
                raise AssertionError("Student isolation or ready link mismatch")
            db.execute("UPDATE users SET must_change=1 WHERE id=1"); db.commit()
            call(403, stand, **first)
            db.execute("UPDATE users SET active=0 WHERE id=1"); db.commit()
            call(401, stand, **first)
        finally:
            stand_routes._configured.cache_clear()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Reviewed, unmodified copy of Netflow app.py")
    args = parser.parse_args()
    check(args.source)
    print("OK: patch, WSGI, session/CSRF, allowlist, deduplication, lease/status, isolation and account revocation")
