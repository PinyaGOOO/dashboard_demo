import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from unittest.mock import MagicMock, patch

from dashboard_backend.consumer_monitor import ConsumerMonitor, ConsumerReportUnavailable
from server import DashboardHandler


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.source = MagicMock(return_value=[])
        self.monitor = ConsumerMonitor(self.source, "live")
        self.clock = self.enter_clock()

    def enter_clock(self):
        patcher = patch("dashboard_backend.consumer_monitor.time.monotonic", return_value=100.0)
        clock = patcher.start()
        self.addCleanup(patcher.stop)
        return clock

    def test_stale_retains_sample_time_then_expires_and_recovers(self):
        report = self.monitor.report()
        self.assertFalse(report["stale"])
        self.assertEqual(report["mode"], "live")
        self.clock.return_value = 116.0
        self.source.side_effect = RuntimeError("credentialed upstream diagnostic")
        stale = self.monitor.report()
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["observed_at"], report["observed_at"])
        self.assertNotIn("credentialed", stale["warning"])
        self.monitor.report()
        self.assertEqual(self.source.call_count, 2)
        self.clock.return_value = 401.0
        with self.assertRaises(ConsumerReportUnavailable):
            self.monitor.report()
        self.clock.return_value = 417.0
        self.source.side_effect = None
        self.assertFalse(self.monitor.report()["stale"])

    def test_failure_without_sample_is_not_successful_empty_cluster(self):
        self.source.side_effect = OSError("offline")
        for _ in range(2):
            with self.assertRaises(ConsumerReportUnavailable):
                self.monitor.report()
        self.source.assert_called_once_with()

    def test_skips_old_baseline_and_reuses_recent_one(self):
        with patch("dashboard_backend.consumer_monitor.build_report", return_value={}) as build:
            self.monitor.report()
            self.clock.return_value = 120
            self.monitor.report()
            self.assertEqual(build.call_args.args, ([], [], 20))
            self.clock.return_value = 260
            self.monitor.report()
            self.assertEqual(build.call_args.args, ([], None, None))

    def test_simultaneous_viewers_share_the_read(self):
        gate = threading.Barrier(5)
        def read():
            gate.wait(timeout=5)
            return self.monitor.report()["stale"]
        with ThreadPoolExecutor(max_workers=5) as workers:
            reads = [workers.submit(read) for _ in range(5)]
            self.assertFalse(any(job.result() for job in reads))
        self.source.assert_called_once_with()


class ConsumerEndpointTests(unittest.TestCase):
    def handler(self):
        h = DashboardHandler.__new__(DashboardHandler)
        h.path = "/api/resource-consumers"
        h.admin_token = "demo-unit-test"
        h.headers = {}
        h.service = MagicMock()
        h._json = MagicMock()
        return h

    def test_inventory_requires_configured_admin_token(self):
        h = self.handler()
        h._handle_api("GET")
        self.assertEqual(h._json.call_args.args[1], HTTPStatus.UNAUTHORIZED)
        h.service.resource_consumers.assert_not_called()
        h.headers["X-Admin-Token"] = h.admin_token
        h._handle_api("GET")
        h._json.assert_called_with(h.service.resource_consumers.return_value)

    def test_upstream_unavailable_is_503(self):
        h = self.handler()
        h.headers["X-Admin-Token"] = h.admin_token
        h.service.resource_consumers.side_effect = ConsumerReportUnavailable("Нет данных")
        h._handle_api("GET")
        self.assertEqual(h._json.call_args.args[1], HTTPStatus.SERVICE_UNAVAILABLE)
