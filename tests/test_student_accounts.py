import concurrent.futures
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard_backend.student_portal import StudentError, StudentPortal


class StudentAccountTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name, "student.sqlite")
        self.catalog = [{"id": f"lab-{i}-{i}", "title": f"Лабораторная {i}", "description": "Пример",
                         "template_name": f"vm-{i}", "template_vmid": i, "steps": []} for i in (400, 401, 402)]
        self.portal = StudentPortal(self.path, lambda: self.catalog)

    def test_group_of_25_unique_personal_logins_no_plaintext_or_vm(self):
        result = self.portal.create_accounts({"count":25, "group":"  ДЭ-24  "})
        self.assertEqual(result["count"], 25)
        people = result["accounts"]
        self.assertEqual([p["login"] for p in people], [f"student{i:02d}" for i in range(3,28)])
        self.assertEqual({p["group"] for p in people}, {"ДЭ-24"})
        self.assertEqual(len({p["password"] for p in people}), 25)
        for person in people:
            self.assertEqual(len(person["password"]), 16)
            self.assertFalse(any(c in person["password"] for c in "0Oo1Il|"))
        reopened = StudentPortal(self.path, lambda: self.catalog)
        self.assertEqual(reopened.demo_profiles(), ["student01", "student02"])
        token = reopened.login(people[0]["login"], people[0]["password"])
        dashboard = reopened.get_dashboard(token)
        self.assertTrue(dashboard["student"]["managed"])
        self.assertEqual(dashboard["student"]["group"], "ДЭ-24")
        self.assertEqual([s["id"] for s in dashboard["assignments"]], [s["id"] for s in self.catalog])
        self.assertTrue(all(s["stand"] is None for s in dashboard["assignments"]))
        self.assertNotIn(people[0]["password"], json.dumps(dashboard))
        with self.portal._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM student_stands").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(distinct salt) FROM student_accounts").fetchone()[0], 25)
            self.assertTrue(all(size == 32 for size, in db.execute("SELECT length(verifier) FROM student_accounts")))
        self.assertTrue(all(p["password"].encode() not in self.path.read_bytes() for p in people))
        after = reopened.create_accounts({"count":1, "group":"Другая"})["accounts"][0]
        self.assertEqual(after["login"], "student28")
        with self.assertRaises(StudentError) as error:
            reopened.login(people[0]["login"], after["password"])
        self.assertEqual(error.exception.status, 401)

    def test_invalid_batches_have_no_side_effects(self):
        for payload in (None, {}, {"group":"g", "count":True}, {"group":"g", "count":101}, {"group":"g", "count":0},
                        {"group":"g", "count":"20"}, {"group":" ", "count":1}, {"group":"a\nb", "count":1},
                        {"group":"g", "count":1, "login":"student01"}, {"group":"g", "count":1, "prefix":"../"}):
            with self.subTest(payload=payload), self.assertRaises(StudentError) as error:
                self.portal.create_accounts(payload)
            self.assertEqual(error.exception.status, 400)
        with self.portal._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM student_accounts").fetchone()[0], 0)

    def test_concurrent_batches_are_disjoint_and_atomic(self):
        barrier = threading.Barrier(2)
        def issue():
            barrier.wait(timeout=5)
            return [p["login"] for p in self.portal.create_accounts({"count":2, "group":"МДК", "prefix":"mdk"})["accounts"]]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            batches = [f.result() for f in [pool.submit(issue),pool.submit(issue)]]
        self.assertEqual(sorted(batches), [["mdk01","mdk02"], ["mdk03","mdk04"]])
        with self.portal._db() as db:
            db.execute("""CREATE TRIGGER test_failure BEFORE INSERT ON student_accounts WHEN new.login='mdk06'
                          BEGIN SELECT RAISE(ABORT,'fixture failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.portal.create_accounts({"count":3,"group":"МДК","prefix":"mdk"})
        with self.portal._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM student_accounts").fetchone()[0],4)
            self.assertEqual(db.execute("SELECT last_number FROM student_account_sequences WHERE prefix='mdk'").fetchone()[0],4)

    def test_orphan_or_reissued_account_does_not_authorize_old_session(self):
        person = self.portal.create_accounts({"count":1,"group":"g"})["accounts"][0]
        login, password = person["login"], person["password"]
        token = self.portal.login(login,password)
        with self.portal._db() as db:
            db.execute("UPDATE student_accounts SET salt=randomblob(16) WHERE login=?",(login,))
        with self.assertRaises(StudentError) as error:
            self.portal.get_dashboard(token)
        self.assertEqual(error.exception.status,401)
        with self.portal._db() as db:
            db.execute("DELETE FROM student_accounts WHERE login=?",(login,))
        with self.assertRaises(StudentError) as error:
            self.portal.get_dashboard(token)
        self.assertEqual(error.exception.status,401)
        self.assertFalse(self.portal._managed(login))

    def test_20_simultaneous_personal_signins_wait_for_bounded_hash_slots(self):
        # Deterministic delay exercises contention without making this test depend
        # on the runner's memory bandwidth. Other tests use the production scrypt.
        active = peak = 0
        lock = threading.Lock()
        def delayed_hash(password,salt):
            nonlocal active,peak
            with lock:
                active += 1
                peak = max(peak,active)
            try:
                time.sleep(0.01)
                return hashlib.sha256(salt+password.encode()).digest()
            finally:
                with lock:
                    active -= 1
        barrier = threading.Barrier(20, timeout=5)
        def sign_in(person):
            barrier.wait()
            token = self.portal.login(person["login"],person["password"])
            return self.portal.get_dashboard(token)["student"]["login"]
        with patch("dashboard_backend.student_accounts._hash",side_effect=delayed_hash):
            people = self.portal.create_accounts({"count":20,"group":"g"})["accounts"]
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
                actual = list(pool.map(sign_in,people))
        self.assertEqual(actual,[p["login"] for p in people])
        self.assertGreater(peak,1)
        self.assertLessEqual(peak,4)
