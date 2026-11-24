from __future__ import annotations

import json
import unittest

from dashboard_backend.resource_consumers import build_report


def guest(vmid=101, **fields):
    return {"type": "qemu", "vmid": vmid, "name": f"guest-{vmid}", "node": "pve", "status": "running",
            "cpu": .5, "maxcpu": 8, "mem": 3 * 1024**3, "disk": 123, "maxdisk": 4096,
            "diskread": 90, "diskwrite": 10, "netin": 300, "netout": 400, "uptime": 600, **fields}


def storage(node, **fields):
    return {"type": "storage", "storage": "NAS", "node": node, "status": "available", "shared": "1",
            "disk": 91, "maxdisk": 100, **fields}


class ResourceConsumerTests(unittest.TestCase):
    def test_all_guests_no_pool_whitelist_templates_or_extra_fields(self):
        report = build_report([guest(202, type="lxc", cpu="0.8", maxcpu=2), guest(pool="outside", secret="PRIVATE"),
                               guest(999, template="1"), {"type": "pool", "pool": "outside"}, None])
        rows = report["guests"]
        self.assertEqual([(r["vmid"], r["kind"], r["cpu_cores"]) for r in rows],
                         [(101, "qemu", 4), (202, "lxc", 1.6)])
        self.assertEqual(rows[0]["ram_bytes"], 3 * 1024**3)
        self.assertEqual(rows[0]["disk_capacity_bytes"], 4096)  # maxdisk, not filesystem use
        self.assertIsNone(rows[0]["io_bps"])
        self.assertIsNone(rows[0]["net_bps"])
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_null_vs_real_zero_and_nonnumeric_api_values(self):
        rows = build_report([guest(cpu=0, mem=0, maxdisk=0),
                             guest(102, cpu=float("nan"), mem="-1", maxdisk=float("inf")),
                             guest(103, maxcpu=None, mem=False, maxdisk="not-a-number"),
                             guest(104, cpu=False, maxdisk=2**100),
                             guest(vmid="4.5"), guest(vmid=False)])["guests"]
        self.assertEqual((rows[0]["cpu_cores"], rows[0]["ram_bytes"], rows[0]["disk_capacity_bytes"]), (0, 0, 0))
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r["cpu_cores"] is None and r["disk_capacity_bytes"] is None for r in rows[1:]))
        self.assertIsNone(rows[1]["ram_bytes"])
        self.assertIsNone(rows[2]["ram_bytes"])
        json.dumps(rows, allow_nan=False)

    def test_inactive_or_offline_guests_do_not_show_live_usage(self):
        g = guest()
        rows = build_report([dict(g, status="stopped"), guest(102, node="lost"), guest(103, status="unknown"),
                             {"type": "node", "node": "lost", "status": "offline", "cpu": .9}], [g], 10)
        for v in rows["guests"]:
            self.assertTrue(all(v[k] is None for k in ["cpu_cores", "ram_bytes", "io_bps", "net_bps"]))
            self.assertEqual(v["disk_capacity_bytes"], 4096)
        self.assertIsNone(rows["nodes"][0]["cpu_pct"])
        self.assertIsNone(rows["nodes"][0]["ram_pct"])

    def test_disk_and_network_delta_are_independent_and_keep_integer_precision(self):
        base = 2**53 + 5
        old = guest(diskread=base, diskwrite=110)
        new = dict(old, uptime=610, diskread=base+1, diskwrite=159, netin=390, netout=420)
        v = build_report([new], [old], 10)["guests"][0]
        self.assertEqual((v["io_bps"], v["net_bps"]), (5, 11))
        v = build_report([dict(new, diskwrite=0)], [old], 10)["guests"][0]
        self.assertIsNone(v["io_bps"])
        self.assertEqual(v["net_bps"], 11)
        v = build_report([dict(new, netout=None)], [old], 10)["guests"][0]
        self.assertIsNone(v["net_bps"])
        self.assertEqual(v["io_bps"], 5)
        self.assertEqual(build_report([dict(old, uptime=610)], [old], 10)["guests"][0]["io_bps"], 0)
        self.assertIsNone(build_report([old], [old], 10)["guests"][0]["io_bps"])

    def test_never_compare_across_restart_migration_type_change_or_long_gap(self):
        old = guest()
        for fields, elapsed in [({"node": "pve2"}, 10), ({"type": "lxc"}, 10), ({"uptime": 500}, 10),
                                ({"uid": "new"}, 10), ({"status": "stopped"}, 10), ({}, None),
                                ({}, 0), ({}, 301), ({}, float("nan"))]:
            with self.subTest(fields=fields, elapsed=elapsed):
                v = build_report([dict(old, **fields)], [old], elapsed)["guests"][0]
                self.assertIsNone(v["io_bps"])
                self.assertIsNone(v["net_bps"])
        offline = {"type": "node", "node": "pve", "status": "offline"}
        self.assertIsNone(build_report([old], [old, offline], 10)["guests"][0]["io_bps"])

    def test_shared_storage_is_a_single_observation_not_sum_local_same_id_retained(self):
        rows = build_report([storage("a"), storage("b", disk=95), storage("c", disk=99, status="unknown"),
                             storage("a", storage="local", shared="0"), storage("b", storage="local", shared=0),
                             storage("c", storage="hidden", disk=None)])["storages"]
        self.assertEqual(len(rows), 4)
        nas = next(r for r in rows if r["name"] == "NAS")
        self.assertEqual((nas["node"], nas["used_bytes"], nas["total_bytes"], nas["used_pct"]),
                         ("a, b, c", 95, 100, 95))
        self.assertEqual([x["node"] for x in rows if x["name"] == "local"], ["a", "b"])
        self.assertIsNone(next(x for x in rows if x["name"] == "hidden")["used_pct"])

    def test_pressure_recommendations_use_measured_numbers_and_matching_node(self):
        rows = [guest(), guest(205, name="other", node="away", maxcpu=128, cpu=1),
                {"type": "node", "node": "pve", "status": "online", "cpu": .92, "mem": 95, "maxmem": 100},
                storage("a")]
        recs = build_report(rows)["recommendations"]
        text = " ".join(r["detail"] for r in recs)
        self.assertEqual(len(recs), 3)
        self.assertIn("92%", text)
        self.assertIn("95%", text)
        self.assertIn("91%", text)
        self.assertIn("guest-101", text)
        self.assertNotIn("other", text)
        self.assertEqual(build_report([guest(), storage("a", disk=20)])["recommendations"], [])

    def test_io_recommendation_only_for_significant_share_of_measured_guests(self):
        old = [guest(), guest(102)]
        current = [dict(old[0], diskread=old[0]["diskread"] + 60 * 1024**2 * 10, uptime=610),
                   dict(old[1], uptime=610), guest(103)]
        recs = build_report(current, old, 10)["recommendations"]
        self.assertEqual(len(recs), 1)
        self.assertIn("60 МиБ/с", recs[0]["detail"])
        self.assertIn("100%", recs[0]["detail"])
        self.assertFalse(build_report(current, [], 10)["recommendations"])
        self.assertFalse(build_report(current[:1], old, 10)["recommendations"])
        self.assertFalse(build_report([dict(current[0], diskread=1000), old[1]], old, 10)["recommendations"])
