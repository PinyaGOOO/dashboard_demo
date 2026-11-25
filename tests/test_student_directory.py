import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard_backend.student_portal import StudentError, StudentPortal


class StudentDirectoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name, "students.sqlite")
        self.now = 1000
        self.portal = StudentPortal(self.path, list, clock=lambda: self.now)
        # Retention tests make 50+ real login transactions; hashing itself is
        # covered by the account tests and is not what is being measured here.
        fast = patch("dashboard_backend.student_accounts._hash",
                     side_effect=lambda password, salt: hashlib.sha256(salt + password.encode()).digest())
        fast.start()
        self.addCleanup(fast.stop)

    def issue(self, group, prefix):
        return self.portal.create_accounts({"count":1, "group":group, "prefix":prefix})["accounts"][0]

    def assert_status(self, status, function, *args, **kwargs):
        with self.assertRaises(StudentError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.status, status)

    def test_groups_sort_before_pagination_with_stable_login_ties(self):
        a = self.issue("яРкая", "a")["login"]
        b = self.issue("Алая", "b")["login"]
        c = self.issue("аЛАя", "c")["login"]
        for sort, order in (("group", [b,c,a]), ("group-desc", [a,b,c]), ("login", [a,b,c])):
            pages = [self.portal.list_accounts(limit=1,offset=i,sort=sort) for i in range(3)]
            self.assertEqual([p["accounts"][0]["login"] for p in pages], order)
            self.assertTrue(all(p["total"] == 3 for p in pages))
        self.assertEqual(self.portal.list_accounts(search="ал",sort="group",offset=1)["accounts"][0]["login"],c)
        for invalid in (None, [], "descending", "login limit 2"):
            self.assert_status(400, self.portal.list_accounts, sort=invalid)

    def test_history_is_success_only_private_and_bounded_across_restarts(self):
        a, b = self.issue("group","a"), self.issue("group","b")
        def history(person):
            return self.portal.account_login_history(person["login"])
        self.assertEqual(history(a), {"login":a["login"], "limit":50, "entries":[]})
        self.portal.login(b["login"], b["password"])
        for i in range(55):
            self.now += 1
            session = self.portal.login(a["login"], a["password"])
        self.assert_status(401, self.portal.login, a["login"], "wrong")
        self.portal.logout(session)
        self.portal.manage_account(a["login"],"block",{"version":1})
        self.assert_status(401, self.portal.login, a["login"], a["password"])
        reopened = StudentPortal(self.path, list)
        self.assertEqual(reopened.account_login_history(a["login"])["entries"],
                         [{"at": t} for t in range(1055,1005,-1)])
        self.assertEqual(history(b)["entries"], [{"at":1000}])
        for login in ("student01","student02","unknown"):
            self.assert_status(404, self.portal.account_login_history, login)
        with self.portal._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM student_login_history WHERE login=?", (a["login"],)).fetchone()[0],50)

    def test_upgrade_imports_one_known_login_without_fabricating_session_events(self):
        a, b = self.issue("group","a"), self.issue("group","b")
        self.portal.login(a["login"],a["password"])
        # Reconstruct just the pre-directory schema; real student sessions remain.
        with self.portal._db() as db:
            db.execute("DROP TRIGGER student_record_login")
            db.execute("DROP TABLE student_login_history")
        for _ in range(2):
            reopened = StudentPortal(self.path,list,clock=lambda: self.now)
            self.assertEqual(reopened.account_login_history(a["login"])["entries"],[{"at":1000}])
            self.assertEqual(reopened.account_login_history(b["login"])["entries"],[])
        # Two genuine logins with the same timestamp are separate events.
        reopened.login(a["login"],a["password"])
        self.assertEqual(reopened.account_login_history(a["login"])["entries"],[{"at":1000},{"at":1000}])

    def test_failed_session_commit_writes_neither_history_nor_last_login(self):
        a = self.issue("group","a")
        with self.portal._db() as db:
            db.execute("""CREATE TRIGGER unavailable BEFORE INSERT ON student_sessions
                          BEGIN SELECT RAISE(ABORT,'session fixture'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.portal.login(a["login"],a["password"])
        self.assertEqual(self.portal.account_login_history(a["login"])["entries"], [])
        self.assertIsNone(self.portal.list_accounts()["accounts"][0]["last_login"])

    def test_delete_retires_even_overlapping_prefix_without_reassigning_or_removing_vm(self):
        a = self.issue("A","s1")  # s101 can also be generated by prefix 's' with sequence 101
        self.assertEqual(a["login"], "s101")
        session = self.portal.login(a["login"],a["password"])
        other = self.issue("B","z")
        other_session = self.portal.login(other["login"],other["password"])
        with self.portal._db() as db:
            db.execute("INSERT INTO student_stands VALUES ('owned',?,'task','{}',1000)",(a["login"],))
            db.execute("INSERT INTO student_lab_requests VALUES ('owned')")
            db.execute("INSERT INTO student_account_sequences VALUES ('s',100)")
        for bad in ("", " s101", "S101", "z01"):
            self.assert_status(400,self.portal.manage_account,"s101","delete",{"version":1,"confirm_login":bad})
        self.assert_status(409,self.portal.manage_account,"s101","delete",{"version":2,"confirm_login":"s101"})
        self.assertEqual(self.portal.get_dashboard(other_session)["student"]["login"],other["login"])
        self.assertEqual(self.portal.manage_account("s101","delete",{"version":1,"confirm_login":"s101"}),
                         {"deleted":True,"login":"s101"})
        self.assert_status(404,self.portal.manage_account,"s101","delete",{"version":1,"confirm_login":"s101"})
        self.assert_status(401,self.portal.login,"s101",a["password"])
        self.assert_status(401,self.portal.get_stand,session,"owned")
        self.assert_status(404,self.portal.account_login_history,"s101")
        self.assertEqual(self.portal.list_accounts(search="s101")["total"],0)
        with self.portal._db() as db:
            for table in ("student_login_history","student_sessions","student_accounts"):
                self.assertIsNone(db.execute(f"SELECT 1 FROM {table} WHERE login='s101'").fetchone())
            self.assertEqual(db.execute("SELECT * FROM student_retired_logins").fetchone()[:],("s101",))
            self.assertEqual(db.execute("SELECT id,login FROM student_stands").fetchone()[:],("owned","s101"))
            self.assertEqual(db.execute("SELECT stand_id FROM student_lab_requests").fetchone()[0],"owned")
            # A previous release without awareness of the retired table cannot recycle it.
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("""INSERT INTO student_accounts(login,group_name,salt,verifier,format,created)
                              VALUES ('s101','other',zeroblob(16),zeroblob(32),'scrypt-v1',2000)""")
        self.assertEqual(self.issue("C","s")["login"], "s102")
        self.assertEqual(self.portal.get_dashboard(other_session)["student"]["login"],other["login"])
