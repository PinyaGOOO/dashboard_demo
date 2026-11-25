import concurrent.futures
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard_backend import student_accounts
from dashboard_backend.student_portal import StudentError, StudentPortal


class AccountManagementTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name, "students.sqlite")
        self.portal = StudentPortal(self.path, list, clock=lambda: 5000)

    def issue(self, count=1, group="ДЭ-24"):
        return self.portal.create_accounts({"count": count, "group": group})["accounts"]

    def assert_status(self, status, method, *args):
        with self.assertRaises(StudentError) as ex:
            method(*args)
        self.assertEqual(ex.exception.status, status)
        return str(ex.exception)

    def test_migration_keeps_preupgrade_session_and_stored_credentials(self):
        path = self.path.with_name("legacy.sqlite")
        salt = bytes(range(16))
        verifier = student_accounts._hash("old password", salt)
        token = "x" * 43
        token_hash = hashlib.sha256(token.encode()).digest()
        binding = "account:" + hashlib.sha256(b"student/session/v1\0" + salt + verifier).hexdigest()
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA application_id=0x53545544")
            db.execute("""CREATE TABLE student_accounts (login TEXT PRIMARY KEY, group_name TEXT NOT NULL,
                        salt BLOB NOT NULL, verifier BLOB NOT NULL, format TEXT NOT NULL, created REAL NOT NULL)""")
            db.execute("INSERT INTO student_accounts VALUES (?,?,?,?,?,1234)", ("old03","Учебная",salt,verifier,"scrypt-v1"))
            db.execute("CREATE TABLE student_sessions (token_hash BLOB PRIMARY KEY, login TEXT NOT NULL, expires REAL NOT NULL)")
            db.execute("""CREATE TABLE student_private_sessions (token_hash BLOB PRIMARY KEY REFERENCES
                        student_sessions(token_hash) ON DELETE CASCADE, binding_stamp TEXT NOT NULL)""")
            db.execute("INSERT INTO student_sessions VALUES (?,?,9000)", (token_hash, "old03"))
            db.execute("INSERT INTO student_private_sessions VALUES (?,?)", (token_hash, binding))
        db.close()
        for _ in range(2):
            migrated = StudentPortal(path, list, clock=lambda: 5000)
            self.assertEqual(migrated.get_dashboard(token)["student"]["login"], "old03")
            self.assertEqual(migrated.list_accounts()["accounts"], [{"id":0,"login":"old03","group":"Учебная","blocked":False,
                             "version":1,"created":1234,"last_login":None}])
        migrated.login("old03", "old password")
        migrated.manage_account("old03", "revoke-sessions", {"version":1})
        self.assert_status(401, migrated.get_dashboard, token)

    def test_roster_is_paginated_searchable_and_has_no_credentials(self):
        people = self.issue(3, "ДЭ-24_100%")
        last = self.issue(1, "Иная")[0]
        logins = [p["login"] for p in people]
        page = self.portal.list_accounts(" дЭ-24_100% ", 1, 1)
        self.assertEqual((page["total"],page["offset"],page["limit"]), (3,1,1))
        self.assertEqual([p["login"] for p in page["accounts"]], logins[1:2])
        self.assertEqual(self.portal.list_accounts("%")["total"], 3)
        self.assertEqual(self.portal.list_accounts(last["login"].upper())["accounts"][0]["login"], last["login"])
        self.assertEqual(self.portal.list_accounts("", 50)["accounts"], [])
        for p in self.portal.list_accounts()["accounts"]:
            self.assertEqual(set(p), {"id","login","group","version","blocked","last_login","created"})
            self.assertEqual((p["version"],p["blocked"],p["last_login"],p["created"]),(1,False,None,5000))
        for args in (([],),("x\n",),("x"*129,),("",False),("",-1),("",0,101),("",0,True)):
            self.assert_status(400, self.portal.list_accounts, *args)
        for login in ("student01","student02","missing"):
            for action in ("reset-password","block","unblock","revoke-sessions"):
                self.assert_status(404, self.portal.manage_account, login, action, {"version":1})
        self.assert_status(400, self.portal.manage_account, last["login"], "delete", {"version":1})
        for bad in ({},{"version":1,"password":"x"},{"version":True},{"version":0}):
            self.assert_status(400, self.portal.manage_account, last["login"], "block", bad)

    def test_block_reset_unblock_revoke_preserve_stands_and_other_students(self):
        a, b = self.issue(2)
        user, old_password = a["login"], a["password"]
        tokens = [self.portal.login(user, old_password) for _ in range(2)]
        other = self.portal.login(b["login"], b["password"])
        with self.portal._db() as db:
            db.execute("INSERT INTO student_stands VALUES ('saved',?,'task','{}',5000)", (user,))
        blocked = self.portal.manage_account(user, "block", {"version":1})
        self.assertEqual(set(blocked), {"account"})
        self.assertEqual((blocked["account"]["last_login"],blocked["account"]["blocked"]), (5000,True))
        with self.portal._db() as db:
            # The unmodified, older application checks this format before issuing a session.
            self.assertNotEqual(db.execute("SELECT format FROM student_accounts WHERE login=?", (user,)).fetchone()[0], "scrypt-v1")
        for token in tokens:
            self.assert_status(401, self.portal.get_dashboard, token)
        message = self.assert_status(401, self.portal.login, "unknown", old_password)
        self.assertEqual(self.assert_status(401, self.portal.login, user, old_password), message)
        reset = self.portal.manage_account(user, "reset-password", {"version":2})
        self.assertTrue(reset["account"]["blocked"])
        password = reset["password"]
        self.assertEqual(len(password),16)
        self.assertFalse(set(password) & set("0Oo1Il|"))
        self.assertNotEqual(password,old_password)
        self.assertEqual(self.assert_status(401, self.portal.login, user, password), message)
        self.assert_status(409, self.portal.manage_account, user, "unblock", {"version":2})
        self.assertFalse(self.portal.manage_account(user, "unblock", {"version":3})["account"]["blocked"])
        self.assert_status(401, self.portal.login, user, old_password)
        self.assert_status(401, self.portal.get_dashboard, tokens[0])
        token = self.portal.login(user, password)
        self.portal.manage_account(user, "revoke-sessions", {"version":4})
        self.assert_status(401, self.portal.get_dashboard, token)
        newer = self.portal.login(user, password)
        changed = self.portal.manage_account(user, "reset-password", {"version":5})
        self.assert_status(401, self.portal.get_dashboard, newer)
        self.assert_status(401, self.portal.login, user, password)
        self.portal.login(user, changed["password"])
        self.assertEqual(self.portal.get_dashboard(other)["student"]["login"], b["login"])
        with self.portal._db() as db:
            self.assertEqual(db.execute("SELECT id,login FROM student_stands").fetchone()[:], ("saved",user))
        for candidate in (old_password, password, changed["password"]):
            self.assertNotIn(candidate.encode(), self.path.read_bytes())
            self.assertNotIn(candidate, json.dumps(self.portal.list_accounts()))

    def test_inflight_verified_password_cannot_issue_session_after_admin_changes(self):
        real_auth = student_accounts.authenticate
        for actions in (("block",), ("block","unblock"), ("reset-password",), ("revoke-sessions",)):
            with self.subTest(actions=actions):
                person = self.issue()[0]
                login, password = person["login"], person["password"]
                ready, finish = threading.Event(), threading.Event()
                def pause(row, candidate):
                    verified = real_auth(row, candidate)
                    ready.set()
                    if not finish.wait(10):
                        raise AssertionError("admin operation did not complete")
                    return verified
                with patch.object(student_accounts, "authenticate", side_effect=pause), concurrent.futures.ThreadPoolExecutor(1) as pool:
                    sign_in = pool.submit(self.portal.login, login, password)
                    try:
                        self.assertTrue(ready.wait(10))
                        for v, action in enumerate(actions, 1):
                            self.portal.manage_account(login, action, {"version":v})
                    finally:
                        finish.set()
                    self.assert_status(401, sign_in.result)
                with self.portal._db() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM student_sessions WHERE login=?", (login,)).fetchone()[0], 0)
                    self.assertIsNone(db.execute("SELECT last_login FROM student_accounts WHERE login=?", (login,)).fetchone()[0])

    def test_concurrent_admins_only_one_reset_commits(self):
        login = self.issue()[0]["login"]
        barrier = threading.Barrier(2)
        def reset():
            barrier.wait(timeout=10)
            try:
                return self.portal.manage_account(login, "reset-password", {"version":1})
            except StudentError as ex:
                return ex.status
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = [f.result() for f in [pool.submit(reset), pool.submit(reset)]]
        self.assertEqual(results.count(409), 1)
        winner = next(r for r in results if isinstance(r, dict))
        self.assertEqual(winner["account"]["version"], 2)
        self.portal.login(login, winner["password"])
