import io
import ipaddress
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from dashboard_backend import student_range as sr


class RangeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name, "inventory.db")
        self.sql("CREATE TABLE ipam_reservations(address)", "CREATE TABLE stand_vms(ip)")

    def sql(self, *statements):
        with closing(sqlite3.connect(self.db)) as db, db:
            for sql in statements:
                db.execute(sql)

    def seed(self):
        self.sql("INSERT INTO ipam_reservations VALUES ('10.39.5.45/16')",
                 "INSERT INTO stand_vms VALUES (' 10.39.5.45 '), ('10.39.4.99'), (''), ('192.0.2.10')")

    def test_read_only_suggestion_uses_canonical_inventory_and_partial_neighbours(self):
        self.seed()
        peers = [{"dst":"10.39.128.25","lladdr":"00:11:22:33:44:55","state":["STALE"]},
                 {"dst":"10.39.19.10","state":["FAILED"]}]
        local = [{"flags":["UP"],"addr_info":[{"family":"inet","local":"10.39.1.9","prefixlen":24}]}]
        before = self.db.read_bytes()
        with patch.object(sr,"_echo",side_effect=AssertionError("passive run sent traffic")):
            r = sr.recommend(self.db,neighbours=peers,interfaces=local)
        self.assertEqual(before,self.db.read_bytes())
        self.assertFalse(r["verified_free"])
        self.assertFalse(r["icmp"]["enabled"])
        self.assertNotIn(r["subnet"],{"10.39.0.0/24","10.39.255.0/24","10.39.128.0/24"})
        self.assertEqual((r["capacity"],r["prefix_length"],r["window_known"]),(200,16,0))
        self.assertTrue(r["start"].endswith(".20") and r["end"].endswith(".219"))
        i = r["inventory"]
        self.assertEqual((i["ipam"],i["stand_vms"],i["local_neighbours"],i["known_total"]),(1,2,1,4))
        self.assertFalse(i["local_l2_covers_candidate"])
        local[0]["addr_info"][0]["prefixlen"] = 16
        self.assertTrue(sr.recommend(self.db,interfaces=local)["inventory"]["local_l2_covers_candidate"])

    def test_empty_malformed_missing_schemas_and_fully_occupied_fail_closed(self):
        for value in ("", "192.0.2.44", "10.39.4.005"):
            self.sql("DELETE FROM stand_vms",f"INSERT INTO stand_vms VALUES ('{value}')")
            with self.assertRaises(sr.InventoryError):
                sr.recommend(self.db)
        self.sql("DELETE FROM stand_vms")
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executemany("INSERT INTO stand_vms VALUES (?)",((f"10.39.{n}.20",) for n in range(1,255)))
        with self.assertRaises(sr.InventoryError):
            sr.recommend(self.db)
        self.sql("DROP TABLE ipam_reservations")
        with self.assertRaises(sr.InventoryError):
            sr.recommend(self.db)
        path = self.db.with_name("does-not-exist.db")
        with self.assertRaises(sr.InventoryError):
            sr.recommend(path)
        self.assertFalse(path.exists())

    def test_probe_rejects_responding_sector_and_keeps_coverage_honest(self):
        self.seed()
        first = ipaddress.IPv4Address(sr.recommend(self.db)["start"])
        with patch.object(sr,"_echo",side_effect=lambda ip: ip == first) as echo, patch.object(sr,"_has_route",return_value=True):
            r = sr.recommend(self.db,probe=True)
        self.assertNotEqual(r["start"], str(first))
        self.assertEqual(r["icmp"],{"enabled":True,"candidates_checked":2,"addresses_probed":208,"selected_addresses_without_echo":200})
        self.assertEqual(echo.call_count,208)
        self.assertTrue(all(call.args[0] in sr.STUDENT_NETWORK and 20 <= int(call.args[0])%256 <= 219 for call in echo.call_args_list))
        self.assertFalse(r["verified_free"])

    def test_probe_fails_closed_on_no_route_too_many_responses_or_new_reservation(self):
        self.seed()
        with patch.object(sr,"_has_route",return_value=False), patch.object(sr,"_echo") as echo, self.assertRaises(sr.InventoryError):
            sr.recommend(self.db,probe=True)
        echo.assert_not_called()
        with patch.object(sr,"_has_route",return_value=True), patch.object(sr,"_echo",return_value=True) as echo, self.assertRaises(sr.InventoryError):
            sr.recommend(self.db,probe=True)
        self.assertEqual(echo.call_count,3*8)
        def reserve_during_probe(block):
            self.sql(f"INSERT INTO ipam_reservations VALUES ('{block.network_address+21}')")
            return True,200
        with patch.object(sr,"_probe_window",side_effect=reserve_during_probe), self.assertRaises(sr.InventoryError):
            sr.recommend(self.db,probe=True)

    def test_linux_ping_timeout_vs_tool_or_route_failure(self):
        target = ipaddress.IPv4Address("10.39.42.100")
        for status,stdout,stderr,expected in [
            (0,b"1 received",b"",True),
            (1,b"1 packets transmitted, 0 received, 100% packet loss, time 0ms",b"",False),
            (1,b"From 10.39.42.1 Destination Host Unreachable\n0 received",b"",None),
            (2,b"",b"ping: socket: Operation not permitted",None),
            (1,b"",b"",None),
        ]:
            with self.subTest(status=status,stdout=stdout), patch.object(subprocess,"run",return_value=subprocess.CompletedProcess([],status,stdout,stderr)) as run:
                if expected is None:
                    with self.assertRaises(sr.InventoryError):
                        sr._echo(target)
                else:
                    self.assertIs(sr._echo(target),expected)
                self.assertEqual(run.call_args.args[0],["ping","-n","-c","1","-W","1","-w","2",str(target)])

    def test_cli_outputs_only_json_with_no_suggestion_on_error(self):
        with patch.object(sr,"_ip_table",return_value=None), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sr.main(["--source-db",str(self.db)]),1)
        body=json.loads(out.getvalue())
        self.assertEqual(set(body),{"error","verified_free"})
