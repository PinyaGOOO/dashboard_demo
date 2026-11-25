"""Operator -> student HTTP boundary; no Proxmox connection, real admin or guest secrets."""
import http.client
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from dashboard_backend.student_mount import StudentMount
from server import DashboardHandler


class Labs:
    def __init__(self):
        self.receipts = {}
        self.lock = threading.Lock()
        self.offline = False

    def submit(self, login, task, card_id, duration_minutes, *, student_group=None):
        from dashboard_backend.student_portal import StudentError
        if self.offline:
            raise StudentError(503, "Нет связи")
        with self.lock:
            self.receipts.setdefault((login, task["id"], card_id), {"status":"provisioning", "progress":15,
                "phase":"В очереди", "duration_minutes":duration_minutes})
        return self.inspect(login, task, card_id)

    def inspect(self, login, task, card_id, detail=False):
        return dict(self.receipts.get((login, task["id"], card_id), {"status":"pending", "progress":0}))

    def credentials(self, login, task, card_id):
        assert (login, task["id"], card_id) in self.receipts
        return {"username":"root@pam","password":"fake-guest"}


class ClassroomFlow(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.labs = Labs()
        self.mount = StudentMount(self.root/"students.sqlite", self.root/"test-catalog.json",
                                  secure_cookie=True, labs=self.labs)
        class Handler(DashboardHandler):
            admin_token = "fixture-only-token"
            student_mount = self.mount
            def log_message(self, *args):
                pass
        self.handler = Handler
        server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
        self.address = server.server_address
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

    def req(self, url, data=None, *, cookie=None, token=None):
        conn = http.client.HTTPConnection(*self.address, timeout=12)
        headers = {"Content-Type":"application/json", "Sec-Fetch-Site":"same-origin"}
        if token is not None:
            headers["X-Admin-Token"] = token
        if cookie:
            headers["Cookie"] = cookie
        try:
            conn.request("GET" if data is None else "POST",url,
                         None if data is None else json.dumps(data),headers)
            resp = conn.getresponse()
            self.assertEqual(resp.headers["Cache-Control"],"no-store")
            return resp.status,json.loads(resp.read()),resp.headers
        finally:
            conn.close()

    def login(self, login, password):
        s,_,h = self.req("/student-demo/api/login",{"login":login,"password":password})
        self.assertEqual(s,200)
        return h["Set-Cookie"].split(";",1)[0]

    def test_batch_has_no_public_student_or_tokenless_alias(self):
        path = "/api/student-accounts/batch"
        data = {"count":20,"group":"fixture"}
        self.assertEqual(self.req(path,data)[0],401)
        cookie = self.login("student02","DemoLab02!")
        self.assertEqual(self.req(path,data,cookie=cookie)[0],401)
        self.assertEqual(self.req("/student-demo/api/student-accounts/batch",data,cookie=cookie)[0],404)
        with patch.object(self.handler,"admin_token",""):
            self.assertEqual(self.req(path,data,token="anything")[0],401)

    def test_issued_accounts_own_a_real_request_even_across_retries(self):
        s,b,_ = self.req("/api/student-accounts/batch",{"count":2,"group":"class"},token=self.handler.admin_token)
        self.assertEqual((s,b["count"],self.labs.receipts),(201,2,{}))
        a,z = [self.login(row["login"],row["password"]) for row in b["accounts"]]
        api = "/student-demo/api"
        s,home,_ = self.req(api+"/me",cookie=a)
        self.assertTrue(home["student"]["managed"])
        first,second = [api+"/assignments/"+row["id"]+"/deploy" for row in home["assignments"][:2]]
        self.assertEqual(self.req(first,{"vmid":276},cookie=a)[0],400)
        self.labs.offline = True
        self.assertEqual(self.req(first,{"duration_minutes":45},cookie=a)[0],503)
        s,home,_ = self.req(api+"/me",cookie=a)
        pending = next(row["stand"] for row in home["assignments"] if row["stand"])
        self.assertEqual((pending["real"],pending["status"],self.labs.receipts),(True,"pending",{}))
        self.assertEqual(self.req(second,{},cookie=a)[0],409)
        self.labs.offline = False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.req(first,{},cookie=a), range(2)))
        self.assertEqual(len(self.labs.receipts),1)
        for code,result,_ in results:
            self.assertEqual((code,result["stand"]["id"],result["stand"]["duration_minutes"]), (200,pending["id"],45))
        card = api+"/stands/"+pending["id"]
        self.assertEqual(self.req(card,cookie=z)[0],404)
        self.assertEqual(self.req(card+"/credentials",{},cookie=z)[0],404)
        self.assertEqual(self.req(card+"/credentials",{},cookie=a)[1]["password"],"fake-guest")
        self.assertNotIn("fake-guest",json.dumps(self.req(card,cookie=a)[1]))
        self.assertEqual(self.req(second,{},cookie=z)[0],200)
        self.assertEqual(len(self.labs.receipts),2)

    def test_public_persona_cannot_use_real_driver(self):
        cookie = self.login("student02","DemoLab02!")
        _,home,_ = self.req("/student-demo/api/me",cookie=cookie)
        self.assertFalse(home["student"]["managed"])
        task = home["assignments"][0]["id"]
        s,b,_ = self.req("/student-demo/api/assignments/"+task+"/deploy",{},cookie=cookie)
        self.assertEqual(s,200)
        self.assertFalse(b["stand"].get("real"))
        self.assertFalse(self.labs.receipts)
