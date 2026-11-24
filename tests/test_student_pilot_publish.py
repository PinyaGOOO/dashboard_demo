import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dashboard_backend import student_pilot_publish as pilot
from dashboard_backend.database import DashboardStore
from dashboard_backend.student_portal import StudentPortal


class PilotPublishTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        self.db = directory / "operator.db"
        self.intent = directory / "student-pilot-request.json"
        self.final = directory / "student-pilot.json"
        self.sql = DashboardStore(self.db, seed_demo=False).execute
        self.sql("INSERT INTO blueprints(id,code,name,template_vmid,updated_at,created_at) VALUES(2,'pr1','pr1',276,'','')")
        self.sql("INSERT INTO stands(id,name,blueprint_id,status,pool_id,created_at,updated_at) VALUES(10,'pilot',2,'running','pilot01','','')")
        self.sql("INSERT INTO stand_vms(id,stand_id,vmid,name,node,ip,status) VALUES(11,10,701,'clone','node1','10.39.13.40','running')")
        self.sql("""INSERT INTO ipam_reservations(stand_id,stand_vm_id,address,requested_cidr,prefix_length,vm_index,
                 status,created_at,updated_at) VALUES(10,11,'10.39.13.40','10.39.13.40/16',16,1,'assigned','','')""")
        portal = StudentPortal(directory / "student-demo-state.db", lambda: [{
            "id":"lab-2-276", "title":"t", "description":"d", "template_name":"t", "template_vmid":276, "steps":["s"]}])
        token = portal.login("student01", "DemoLab01!")
        sid = portal.deploy(token, "lab-2-276")["id"]
        self.record = {"student_login":"student01", "stand_id":sid, "assignment_id":"lab-2-276",
                       "deployer_stand_id":10, "guest_ipv4":"10.39.13.40", "pool_id":"pilot01"}
        self.intent.write_text(json.dumps(self.record), encoding="utf-8")
        self.gateway = MagicMock(mode="live")
        self.gateway.pool_members.return_value = [{"vmid":701,"status":"running","node":"node1"}]
        self.iprow = {"ifname":"vmbr0","ifindex":3,"flags":["UP"],"operstate":"UP",
                      "address":"02:03:04:05:06:07",
                      "addr_info":[{"family":"inet","scope":"global","local":"10.39.13.40","prefixlen":16}]}
        self.digest = hashlib.sha256(b"expected-cert").hexdigest()
        self.socket_output = "0 0 [::ffff:10.39.13.40]:8006 [::ffff:10.10.0.5]:43123"
        self.gateway._guest_command.side_effect = self.guest
        net = patch.object(pilot.socket, "create_connection")
        self.net = net.start()
        self.addCleanup(net.stop)
        tls = patch.object(pilot.ssl, "SSLContext")
        self.ctx = tls.start()
        self.addCleanup(tls.stop)
        self.tls = self.ctx.return_value.wrap_socket.return_value.__enter__.return_value
        self.tls.getpeercert.return_value = b"expected-cert"
        self.tls.getsockname.return_value = ("10.10.0.5",43123)
        self.http = b'HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<html>Proxmox<script src="/pve2/pvemanagerlib.js"></script></html>'
        self.tls.makefile.side_effect = lambda *_: io.BytesIO(self.http)

    def guest(self, node, vmid, command, timeout):
        self.assertEqual((node,vmid), ("node1",701))
        self.assertLessEqual(timeout, 10)
        if command[:3] == ["ip","-j","-4"]:
            data = json.dumps([self.iprow])
        elif command[:3] == ["ip","-j","link"]:
            data = json.dumps([{**self.iprow,"address":"02:03:04:05:06:07"}])
        elif command == pilot._TLS_CHECK:
            data = "sha256 Fingerprint=" + ":".join(self.digest[i:i+2] for i in range(0,64,2))
        elif command == ["ss","-Hnt","state","established","sport","=",":8006"]:
            data = self.socket_output
        else:
            raise AssertionError("unexpected command")
        return {"exit_code":0,"stdout":data}

    def publish(self, stand=10):
        return pilot.publish_pilot_if_ready(self.db,self.gateway,stand)

    def test_verified_guest_published_once_and_only_known_fields(self):
        self.iprow.pop("address")  # Test iproute2 without link fields in -4 output.
        self.assertTrue(self.publish())
        self.assertEqual(json.loads(self.final.read_text()), {**self.record,"vmid":701})
        if os.name == "posix":
            self.assertEqual(self.final.stat().st_mode & 0o077, 0)
        self.net.assert_called_once_with(("10.39.13.40",8006),timeout=4)
        request = self.tls.sendall.call_args.args[0]
        self.assertTrue(request.startswith(b"GET / HTTP/1.1\r\nHost: 10.39.13.40:8006\r\n"))
        self.assertNotIn(b"Authorization:",request)
        self.assertNotIn(b"Cookie:",request)
        self.assertFalse(self.publish())
        self.gateway.pool_members.assert_called_once_with("pilot01")

    def test_no_or_unrelated_intent_never_contacts_cluster(self):
        self.assertFalse(self.publish(9))
        for key, value in [("student_login","student02"), ("stand_id","a"*24), ("pool_id","not-mine"), ("guest_ipv4","10.11.2.4")]:
            self.intent.write_text(json.dumps({**self.record,key:value}), encoding="utf-8")
            self.assertFalse(self.publish())
        self.intent.unlink()
        self.assertFalse(self.publish())
        self.gateway.pool_members.assert_not_called()
        self.net.assert_not_called()

    def test_no_overwrite_and_bad_local_db(self):
        self.final.write_text("operator decision")
        self.assertFalse(self.publish())
        self.assertEqual(self.final.read_text(), "operator decision")
        self.final.unlink()
        self.sql("UPDATE stand_vms SET credential_valid=0")
        self.assertFalse(self.publish())
        self.gateway.pool_members.assert_not_called()

    def test_guest_disagrees_with_operator_inventory(self):
        for members in ([{"vmid":701,"node":"node2","status":"running"}], [],
                        [{"vmid":701,"node":"node1","status":"stopped"}], [{"vmid":702,"node":"node1","status":"running"}]):
            self.gateway.pool_members.return_value = members
            self.assertFalse(self.publish())
        self.gateway._guest_command.assert_not_called()
        self.gateway.pool_members.return_value = [{"vmid":701,"status":"running","node":"node1"}]
        self.iprow["address"] = "00:00:00:00:00:00"
        self.assertFalse(self.publish())
        self.iprow["address"] = "02:03:04:05:06:07"
        self.iprow["addr_info"][0]["local"] = "10.13.13.88"
        self.assertFalse(self.publish())
        self.net.assert_not_called()

    def test_tls_pin_or_connected_peer_mismatch_prevents_publication(self):
        self.tls.getpeercert.return_value = b"other-cert"
        self.assertFalse(self.publish())
        self.tls.getpeercert.return_value = b"expected-cert"
        self.socket_output = "0 0 10.39.13.40:8006 10.10.0.5:43124"
        self.assertFalse(self.publish())
        self.assertFalse(self.final.exists())

    def test_unresponsive_gateway_and_retracted_intent_are_safe(self):
        self.gateway.pool_members.side_effect = RuntimeError("sensitive details must not escape")
        self.assertFalse(self.publish())
        self.gateway.pool_members.side_effect = None
        self.tls.getsockname.side_effect = lambda: (self.intent.unlink() or ("10.10.0.5",43123))
        self.assertFalse(self.publish())
        self.assertFalse(self.final.exists())

    def test_wrong_http_page_maintenance_or_redirect_prevent_publication(self):
        for response in (self.http.replace(b"200 OK", b"503 Service Unavailable"), self.http[:self.http.index(b"<html>")] + b"<html>unrelated server</html>",
                         self.http.replace(b"200 OK", b"302 Found\r\nLocation: https://other/"),
                         self.http.replace(b"Content-Type: ", b"Content-Length: 900000\r\nContent-Type: ")):
            with self.subTest(status=response.splitlines()[0]):
                self.http = response
                self.assertFalse(self.publish())
                self.assertFalse(self.final.exists())

    def test_cli_verifies_existing_vm_without_creating_or_overwriting(self):
        with patch("dashboard_backend.proxmox_gateway.create_gateway", return_value=self.gateway) as make, patch("builtins.print"):
            args = ["--source-db",str(self.db),"--stand-id"]
            self.assertEqual(pilot.main(args+["99"]), 1)
            make.assert_not_called()
            self.assertEqual(pilot.main(args+["10"]), 0)
            self.assertEqual(pilot.main(args+["10"]), 1)
            make.assert_called_once_with()
        self.gateway.deploy_stand.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Unix operator file mode")
    def test_writable_intent_denied(self):
        self.intent.chmod(0o666)
        self.assertFalse(self.publish())
        self.gateway.pool_members.assert_not_called()
