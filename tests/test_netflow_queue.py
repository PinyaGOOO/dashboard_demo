import concurrent.futures
import sqlite3
import tempfile
import unittest
from pathlib import Path

from integrations.netflow.stand_queue import QueueError, StandQueue


FIRST = "lab-2-276"
OTHER = "lab-3-278"
URL = "https://10.39.5.99:8006/"


class NetflowQueueTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "classroom db.sqlite3"
        self.staffdb = sqlite3.connect(self.path, timeout=1)
        self.addCleanup(self.staffdb.close)
        self.staffdb.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY, login TEXT NOT NULL UNIQUE, grp TEXT,
                active INTEGER, admin INTEGER, must_change INTEGER);
            INSERT INTO users VALUES(1,'demo-one',' сСа-24 ',1,0,0),(2,'other','ССА-24',1,0,0),
                (3,'staff','ССА-24',1,1,0),(4,'third','Последняя группа',1,0,1),
                (5,'inactive','ССА-24',0,0,0);
        """)
        self.cfg = {FIRST: ["ССА-24"], OTHER: ["ССА-24", "Последняя группа"]}
        self.now = 1000000
        self.queue = StandQueue(self.path, self.cfg, clock=lambda: self.now, lease_seconds=30,
                                poll_seconds=3, freshness_seconds=10)

    def change(self, sql, *args):
        with self.staffdb as c:
            c.execute(sql, args)

    def report(self, lease, state="ready", progress=100, url=URL):
        return self.queue.report(lease["job_id"], lease["lease_token"], state, progress, url)

    def assert_status(self, status, fn, *args):
        with self.assertRaises(QueueError) as caught:
            fn(*args)
        self.assertEqual(caught.exception.status, status)

    def test_identity_and_group_come_from_private_database(self):
        q = self.queue
        self.assertEqual(q.scenarios(1)["scenarios"], [{"assignment_id": FIRST}, {"assignment_id": OTHER}])
        self.assertEqual(q.get(1), {"stand": None})
        job = q.request(1, FIRST)["stand"]
        self.assertEqual(q.get(2), {"stand": None})
        self.assertEqual(q.request(1, FIRST)["stand"], job)
        self.assert_status(409, q.request, 1, OTHER)
        lease = q.claim()["job"]
        self.assertEqual(lease["group"], "ССА-24")
        self.assertRegex(lease["subject"], "^[a-f0-9]{32}$")
        self.assertNotIn("login", lease)
        self.assertEqual(self.report(lease), {"ok": True})
        self.change("UPDATE users SET login='new-name' WHERE id=1")
        stand = q.get(1)["stand"]
        self.assertEqual(stand["job_id"], job["job_id"])
        self.assertEqual(stand["web_url"], URL)
        self.assertNotIn("lease_token", stand)

    def test_safe_no_group_inactive_admin_and_password_reset(self):
        q = self.queue
        for who in (None, True, 3, 4, 5, 22):
            self.assert_status(403, q.request, who, FIRST)
        self.change("UPDATE users SET grp='' WHERE id=1")
        self.assertEqual(q.scenarios(1)["scenarios"], [])
        self.assert_status(403, q.request, 1, FIRST)
        self.assert_status(400, q.request, 2, "DROP")
        self.assert_status(403, q.request, 2, "lab-1-1")
        with self.assertRaises(ValueError):
            StandQueue(self.path, {FIRST: ["a", " A"]})

    def test_no_overclaim_concurrent_and_repeated_posts(self):
        q = self.queue
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            jobs = list(executor.map(lambda _: q.request(1, FIRST)["stand"]["job_id"], range(12)))
            claims = list(executor.map(lambda _: q.claim()["job"], range(12)))
        self.assertEqual(len(set(jobs)), 1)
        self.assertEqual(sum(j is not None for j in claims), 1)
        self.assertIsNone(q.claim()["job"])
        self.now += 30
        again = q.claim()["job"]
        initial = next(j for j in claims if j)
        self.assertEqual(initial["job_id"], again["job_id"])
        self.assertNotEqual(initial["lease_token"], again["lease_token"])
        self.assert_status(409, self.report, initial)
        self.assertEqual(self.report(again), {"ok": True})
        self.assert_status(409, self.report, again)

    def test_reference_and_refresh_lifecycle(self):
        q = self.queue
        original = q.request(1, FIRST)["stand"]["job_id"]
        lease = q.claim()["job"]
        strict_bad = ["https://10.39.5.99:8006/?pw=x", "https://10.39.5.99:8006/#token",
                      "https://user@10.39.5.99:8006/", "https://192.168.1.6:8006/",
                      "https://10.39.1.999:8006/", "https://10.39.255.255:8006/",
                      "http://10.39.5.99:8006/", "https://10.39.5.99:22/"]
        for invalid in strict_bad:
            self.assert_status(400, self.report, lease, "ready", 100, invalid)
        self.assert_status(400, self.report, lease, "building", 33, URL)
        self.report(lease)
        self.now += 10
        self.assertEqual(q.get(1)["stand"]["status"], "verifying")
        self.assertIsNone(q.get(1)["stand"]["web_url"])
        self.report(q.claim()["job"], "ready", 100, None)
        self.assertEqual(q.get(1)["stand"]["status"], "ready")
        self.assertIsNone(q.get(1)["stand"]["web_url"])
        for state in ("awaiting_confirmation", "stopped", "expired", "error"):
            self.now += 3
            self.report(q.claim()["job"], state, 0, None)
            self.assertEqual(q.get(1)["stand"]["status"], state)
            self.assert_status(409, q.request, 1, OTHER)
        self.now += 3
        self.report(q.claim()["job"], "removed", 0, None)
        self.assertIsNone(q.get(1)["stand"])
        self.assertNotEqual(q.request(1, OTHER)["stand"]["job_id"], original)

    def test_revocation_invalidates_pending_lease_and_cached_ready(self):
        q = self.queue
        q.request(1, FIRST)
        lease = q.claim()["job"]
        self.report(lease)
        self.now += 3
        lease = q.claim()["job"]
        self.change("UPDATE users SET active=0 WHERE id=1")
        self.assert_status(403, q.get, 1)
        self.assert_status(409, self.report, lease)
        blocked = q.claim()["job"]
        self.assertEqual(blocked["action"], "reconcile")
        self.assert_status(409, self.report, blocked)
        self.change("UPDATE users SET active=1,grp='Неподходящая' WHERE id=1")
        self.assert_status(403, q.get, 1)
        check = q.claim()["job"]
        self.assertEqual(check["action"], "reconcile")
        self.change("UPDATE users SET grp='ССА-24' WHERE id=1")
        self.assert_status(409, self.report, check, "removed", 0, None)
        self.assertEqual(q.get(1)["stand"]["status"], "verifying")
        self.assertIsNone(q.get(1)["stand"]["web_url"])
        self.assertNotIn("action", q.claim()["job"])

    def test_rowid_reuse_and_replace_do_not_grant_old_identity(self):
        q = self.queue
        self.assertEqual(self.staffdb.execute("PRAGMA foreign_keys").fetchone()[0], 0)
        self.assertEqual(self.staffdb.execute("PRAGMA recursive_triggers").fetchone()[0], 0)
        q.request(1, FIRST)
        lease = q.claim()["job"]
        self.change("DELETE FROM users WHERE id=1")
        self.change("INSERT INTO users VALUES(1,'replacement','ССА-24',1,0,0)")
        self.assertIsNone(q.get(1)["stand"])
        check = q.claim()["job"]
        self.assertEqual(check["action"], "reconcile")
        self.assertEqual(check["subject"], lease["subject"])
        self.report(check, "removed", 0, None)
        q.request(1, FIRST)
        next_lease = q.claim()["job"]
        self.assertNotEqual(next_lease["subject"], lease["subject"])
        self.assert_status(409, self.report, lease)
        self.change("INSERT OR REPLACE INTO users VALUES(1,'another','ССА-24',1,0,0)")
        self.assertIsNone(q.get(1)["stand"])
        self.assert_status(409, self.report, next_lease)

    def test_bound_transaction_rolls_back_without_committing_caller(self):
        q = self.queue
        self.staffdb.execute("BEGIN IMMEDIATE")
        bound = q.using(self.staffdb)
        bound.request(1, FIRST)
        self.assertIsNone(self.staffdb.row_factory)
        self.assertTrue(self.staffdb.in_transaction)
        self.assertIsNotNone(bound.claim()["job"])
        self.staffdb.rollback()
        self.assertIsNone(q.get(1)["stand"])

    def test_restart_and_catalog_change_do_not_create_or_expose_another_job(self):
        q = self.queue
        first = q.request(1, FIRST)["stand"]
        lease = q.claim()["job"]
        self.report(lease)
        restarted = StandQueue(self.path, self.cfg, clock=lambda: self.now)
        self.assertEqual(restarted.request(1, FIRST)["stand"]["job_id"], first["job_id"])
        self.assertEqual(restarted.get(1)["stand"]["web_url"], URL)
        restricted = StandQueue(self.path, {OTHER: ["ССА-24"]}, clock=lambda: self.now)
        self.assert_status(403, restricted.get, 1)
        self.assert_status(403, restricted.request, 1, FIRST)
        self.assert_status(409, restricted.request, 1, OTHER)
        self.now += 10
        check = restricted.claim()["job"]
        self.assertEqual(check["action"], "reconcile")
        self.assert_status(409, self.report, check)
        self.assert_status(409, self.report, check, "building", 45, None)
        restricted.report(check["job_id"], check["lease_token"], "error", 0)
        self.assert_status(409, restricted.request, 1, OTHER)
        self.now += 10
        final = restricted.claim()["job"]
        self.assertEqual(final["action"], "reconcile")
        restricted.report(final["job_id"], final["lease_token"], "removed", 0)
        self.assertIsNone(restricted.get(1)["stand"])
        self.assertNotEqual(restricted.request(1, OTHER)["stand"]["job_id"], first["job_id"])

    def test_rejection_releases_slot_requires_new_post_and_keeps_only_latest_result(self):
        q = self.queue
        j = q.request(1, FIRST)["stand"]
        old = q.claim()["job"]
        self.now += 30
        new = q.claim()["job"]
        self.assert_status(409, self.report, old, "rejected", 0, None)
        self.assert_status(400, self.report, new, "rejected", 0, URL)
        self.report(new, "rejected", 0, None)
        self.assert_status(409, self.report, new, "ready", 100, URL)
        self.assertEqual(q.get(1)["stand"], dict(j, status="rejected"))
        self.assertIsNone(q.get(2)["stand"])
        self.now += 10
        self.assertIsNone(q.claim()["job"])
        self.change("UPDATE users SET grp='Неизвестная' WHERE id=1")
        self.assertIsNone(q.get(1)["stand"])
        self.change("UPDATE users SET grp='ССА-24' WHERE id=1")
        self.assertEqual(q.get(1)["stand"]["job_id"], j["job_id"])
        second = q.request(1, FIRST)["stand"]
        self.assertNotEqual(j["job_id"], second["job_id"])
        self.assertEqual(q.request(1, FIRST)["stand"], second)
        self.report(q.claim()["job"], "removed", 0, None)
        self.assertIsNone(q.get(1)["stand"])  # previous rejection isn't redisplayed
        self.assertIsNone(q.claim()["job"])

    def test_reconcile_cannot_release_slot_by_rejection(self):
        q = self.queue
        q.request(1, FIRST)
        self.change("UPDATE users SET grp='Другая' WHERE id=1")
        restricted = q.claim()["job"]
        self.assertEqual(restricted["action"], "reconcile")
        self.assert_status(409, self.report, restricted, "rejected", 0, None)
        self.change("UPDATE users SET grp='ССА-24' WHERE id=1")
        self.report(q.claim()["job"], "rejected", 0, None)
        self.now += 86400
        self.assertIsNone(q.get(1)["stand"])

    def test_rejection_migrates_old_partial_unique_index_without_losing_job(self):
        j = self.queue.request(1, FIRST)["stand"]
        self.staffdb.executescript("""DROP INDEX nf_deployer_one_stand_v2;
            CREATE UNIQUE INDEX nf_deployer_one_stand ON nf_deployer_jobs(subject) WHERE status <> 'removed';
            DROP INDEX nf_deployer_next_v2;
            CREATE INDEX nf_deployer_next ON nf_deployer_jobs(due_at,created_at) WHERE status <> 'removed';""")
        q = StandQueue(self.path, self.cfg, clock=lambda: self.now)
        self.assertEqual(q.get(1)["stand"], j)
        lease = q.claim()["job"]
        q.report(j["job_id"], lease["lease_token"], "rejected", 0)
        self.assertNotEqual(q.request(1, FIRST)["stand"]["job_id"], j["job_id"])


if __name__ == "__main__":
    unittest.main()
