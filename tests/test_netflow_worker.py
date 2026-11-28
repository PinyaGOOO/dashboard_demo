import json
import ssl
import unittest
from unittest.mock import Mock, patch

from dashboard_backend.netflow_worker import (
    NetflowClient, NetflowExchangeError, NetflowWorker, _origin, start_netflow_worker,
)
from dashboard_backend.student_labs import StudentLabs


JOB = dict(job_id="a"*32, subject="b"*32, assignment_id="lab-12-276", group="ДЭ-24", lease_token="c"*43)
TOKEN = "worker-test-token-"*3
READY = {"status":"ready", "progress":100, "web_url":"https://10.39.5.42:8006/"}


def response(payload, status=200, media="application/json", *, missing_length=False):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    r = Mock(status=status, length=None if missing_length else len(raw))
    r.getheader.side_effect = lambda k, default=None: {"Content-Type":media}.get(k, default)
    r.read.side_effect = lambda n: raw[:n]
    return r


class WorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = NetflowClient("https://netflow.example.test", TOKEN)

    def test_default_off_even_with_demo_and_partial_configuration_rejected(self):
        demo, live = Mock(), Mock()
        demo.gateway.mode, live.gateway.mode = "demo", "live"
        config = {"NETFLOW_INTEGRATION_URL":"https://site.test", "NETFLOW_WORKER_TOKEN":TOKEN}
        self.assertIsNone(start_netflow_worker(demo, None, {}))
        self.assertIsNone(start_netflow_worker(live, None, {"NETFLOW_ALLOW_LAB_LINKS":"0"}))
        for svc, labs, values in [(demo, Mock(), config), (live, None, config), (live,Mock(),{"NETFLOW_WORKER_TOKEN":TOKEN}),
                (live,Mock(),{"NETFLOW_ALLOW_LAB_LINKS":"1"}), (live,Mock(),{"NETFLOW_INTEGRATION_URL":"https://site.test"}),
                (live,Mock(),dict(config,NETFLOW_ALLOW_LAB_LINKS="yes")),
                (live,StudentLabs(live),config),(live,StudentLabs(live,"10.39.1.1","10.40.1.1"),config)]:
            with self.assertRaises(ValueError):
                start_netflow_worker(svc, labs, values)

    def test_https_origin_and_secret_not_logged_in_errors(self):
        for origin in ("http://netflow.test", "https://u@netflow.test", "https://nf.test/%2f",
                       "https://nf..test", "https://nf.test?", "https://nf.test/#",
                       "https://nf.test:0", "https://nf.test:99999", "https://nf.test:",
                       "https://nf.test\\@other.test", "https://nf.test/\n"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                _origin(origin)
        self.assertEqual(_origin("https://netflow.test:9443/"),("netflow.test",9443))
        self.assertEqual(_origin("https://[::1]"),("::1",443))
        for token in ("short", TOKEN+"\n", TOKEN+"\rX: injected", TOKEN+"\t", TOKEN+" "):
            with self.assertRaises(ValueError) as exc:
                NetflowClient("https://nf.test",token)
            self.assertNotIn(token,str(exc.exception))

    @patch("dashboard_backend.netflow_worker.NetflowWorker.start", autospec=True)
    def test_worker_refuses_operator_or_proxmox_secret_before_starting(self, start):
        svc = Mock()
        svc.gateway.mode = "live"
        env = {"NETFLOW_INTEGRATION_URL":"https://netflow.test", "NETFLOW_WORKER_TOKEN":TOKEN}
        for name in ("DASHBOARD_ADMIN_TOKEN","PROXMOX_TOKEN_VALUE"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "must be separate") as exc:
                start_netflow_worker(svc,Mock(),dict(env, **{name:TOKEN}))
            self.assertNotIn(TOKEN,str(exc.exception))
        start.assert_not_called()

    @patch("dashboard_backend.netflow_worker.NetflowWorker.start", autospec=True)
    @patch("dashboard_backend.netflow_labs.NetflowLabs", autospec=True)
    def test_live_start_reuses_real_lab_service_without_enabling_links(self, adapter, start):
        svc,labs = Mock(),Mock()
        svc.gateway.mode="live"
        worker=start_netflow_worker(svc,labs,{"NETFLOW_INTEGRATION_URL":"https://netflow.test", "NETFLOW_WORKER_TOKEN":TOKEN})
        adapter.assert_called_once_with(svc,labs)
        start.assert_called_once_with(worker)
        self.assertFalse(worker._allow_links)

    @patch("dashboard_backend.netflow_worker.http.client.HTTPSConnection")
    def test_one_real_protocol_exchange_without_external_network(self, https):
        conn = https.return_value
        conn.getresponse.side_effect = [response({"job":JOB}),response({"ok":True}),response({"job":None})]
        adapter = Mock()
        adapter.handle.return_value = dict(READY, guest_password="MUST NOT LEAK", login="private", phase="private")
        worker = NetflowWorker(self.client, adapter, allow_links=True)
        self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())
        self.assertEqual(https.call_count,3)
        self.assertEqual(https.call_args.args,("netflow.example.test",443))
        ctx = https.call_args.kwargs["context"]
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode,ssl.CERT_REQUIRED)
        calls=conn.request.call_args_list
        self.assertEqual([c.args for c in calls],[("POST","/api/integration/jobs/claim"),
                            ("POST","/api/integration/jobs/status"),("POST","/api/integration/jobs/claim")])
        self.assertEqual(calls[0].kwargs["headers"]["Authorization"],"Bearer "+TOKEN)
        self.assertEqual(json.loads(calls[1].kwargs["body"]),dict(READY,job_id=JOB["job_id"],lease_token=JOB["lease_token"]))
        self.assertEqual(conn.close.call_count,3)
        adapter.handle.assert_called_once_with(JOB)

    @patch("dashboard_backend.netflow_worker.http.client.HTTPSConnection")
    def test_unknown_format_oversized_empty_bad_lease_and_redirect(self, https):
        conn=https.return_value
        for r in [
            response({"job":JOB},status=302),response({},status=401),response({"job":JOB},media="text/html"),
            response({"job":dict(JOB,guest_password="private")}),response({"job":dict(JOB,subject=1)}),
            response({"job":dict(JOB,group="")}),response({"job":dict(JOB,group="class\n")}),
            response({"job":dict(JOB,lease_token="x")}),response({"job":dict(JOB,assignment_id="../../admin")}),
            response({"job":dict(JOB,action=None)}),response({"job":dict(JOB,action="deploy")}),
            response(b'{"job":null,"job":null}'),response(b"[0]"),response(b"{}"),response(b"not-json"),
            response(b'x'*17000),response(b'x'*17000,missing_length=True),
        ]:
            https.reset_mock()
            conn.getresponse.return_value=r
            with self.assertRaises(NetflowExchangeError) as err:
                self.client.claim()
            self.assertNotIn(TOKEN,str(err.exception))
            self.assertNotIn("private",str(err.exception))
            https.assert_called_once()  # no authentication retry or redirect with stale token
            conn.close.assert_called_once()

    @patch("dashboard_backend.netflow_worker.http.client.HTTPSConnection")
    def test_reconcile_accepts_only_non_access_results(self,https):
        conn=https.return_value
        job=dict(JOB, action="reconcile")
        conn.getresponse.return_value=response({"job":job})
        self.assertEqual(self.client.claim(),job)
        conn.reset_mock()
        for state in ("ready","building","queued","expired","stopped", "awaiting_confirmation"):
            with self.subTest(state=state), self.assertRaises(NetflowExchangeError):
                self.client.publish(job,dict(READY,status=state),allow_links=True)
        conn.request.assert_not_called()
        conn.getresponse.return_value=response({"ok":True})
        for state in ("error","removed"):
            self.client.publish(job,dict(READY,status=state),allow_links=True)
            msg=json.loads(conn.request.call_args.kwargs["body"])
            self.assertEqual(msg,{"job_id":JOB["job_id"],"lease_token":JOB["lease_token"],"status":state,"progress":100})

    @patch("dashboard_backend.netflow_worker.http.client.HTTPSConnection")
    def test_public_projection_and_link_gate(self,https):
        conn=https.return_value
        conn.getresponse.return_value=response(b"",status=204)
        for allow,state,url in [
            (False,"ready",READY["web_url"]), (True,"building",READY["web_url"]),
            (True,"ready","https://elsewhere.test:8006/"), (True,"ready","https://10.39.1.2:8006/?password=secret"),
            (True,"ready","https://10.39.1.2:8006/#data"),(True,"ready","https://test@10.39.1.2:8006/"),
            (True,"ready","https://10.39.0.0:8006/"),(True,"ready","https://10.39.255.255:8006/"),
            (True,"ready","https://10.39.01.4:8006/"), (True,"ready","https://10.39.1.256:8006/")]:
            self.client.publish(JOB,{"status":state,"progress":34,"web_url":url,"phase":"secret"},allow_links=allow)
            self.assertEqual(set(json.loads(conn.request.call_args.kwargs["body"])),{"job_id","lease_token","status","progress"})
        for raw in (None, {"progress":20},{"status":"ready","progress":True},{"status":"ready","progress":101}):
            with self.assertRaises(NetflowExchangeError):
                self.client.publish(JOB,raw,allow_links=True)

    def test_uncertain_local_commit_or_lost_ack_does_not_report_fake_terminal_failure(self):
        client,adapter=Mock(),Mock()
        client.claim.return_value=JOB
        adapter.handle.side_effect=TimeoutError
        worker=NetflowWorker(client,adapter)
        with self.assertRaises(TimeoutError):worker.run_once()
        client.publish.assert_not_called()
        adapter.handle.side_effect=None
        adapter.handle.return_value=READY
        client.publish.side_effect=NetflowExchangeError("Netflow HTTP 409")
        with self.assertRaises(NetflowExchangeError):worker.run_once()
        self.assertEqual(client.publish.call_count,1)
        self.assertEqual(adapter.handle.call_args.args,(JOB,))

    @patch("dashboard_backend.netflow_worker.http.client.HTTPSConnection")
    def test_negative_application_ack_is_not_success(self,https):
        https.return_value.getresponse.return_value=response({"ok":False})
        with self.assertRaises(NetflowExchangeError):
            self.client.publish(JOB,READY,allow_links=False)

    def test_shutdown_and_backoff_do_not_log_raw_data(self):
        client,adapter=Mock(),Mock()
        client.claim.side_effect=Exception(TOKEN)
        worker=NetflowWorker(client,adapter)
        with self.assertLogs("dashboard_backend.netflow_worker","WARNING") as logs:
            worker.start()
            # Log should already have been emitted when the worker goes to sleep.
            import time
            for _ in range(100):
                if logs.output:break
                time.sleep(.01)
            worker.stop()
        self.assertFalse(worker._thread.is_alive())
        self.assertEqual(len(logs.output),1)
        self.assertNotIn(TOKEN,logs.output[0])
        adapter.handle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
