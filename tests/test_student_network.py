import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard_backend.database import DashboardStore
from dashboard_backend.service import DashboardService, ValidationError
from dashboard_backend.student_network import student_deployment_network


class StudentNetworkTests(unittest.TestCase):
    def test_range_and_boundaries(self):
        for subnet, start in (("10.38.4.0/16",""), ("10.39.2.5/24",""), ("10.39.255.255/16",""),
                              ("10.39.0.0/16",""), ("10.39.2.0/16","10.40.1.2"), ("","")):
            with self.subTest(subnet=subnet, start=start), self.assertRaises(ValueError):
                student_deployment_network(" Student01 ", subnet, start)
        self.assertEqual(student_deployment_network("student02","","10.39.4.0"), ("10.39.4.0/16","10.39.4.0"))
        self.assertEqual(student_deployment_network("Преподаватель","172.16.8.9/24",""), ("172.16.8.9/24",""))

    def test_real_provisioning_plan_preserves_host_and_rejects_wrong_network(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DashboardStore(Path(temp)/"db")
            service = DashboardService(store, Mock(mode="demo"))
            try:
                bp = next(b for b in service.list_blueprints() if b["status"] == "active")
                data = dict(name="student-lab", owner="student02", blueprint_id=bp["id"], vm_count=1)
                with patch.object(service, "_launch_deployment") as launch:
                    with self.assertRaises(ValidationError):
                        service.create_stand(dict(data, subnet="172.16.8.9/24"))
                    launch.assert_not_called()
                    result = service.create_stand(dict(data, subnet="10.39.0.0/16", start_ip="10.39.4.0"))
                    self.assertEqual(result["vms"][0]["ip"], "10.39.4.0")
                    self.assertEqual(launch.call_args.args[1]["allocated_ips"], ["10.39.4.0"])
            finally:
                service.stop_monitors()
