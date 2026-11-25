"""Publish/unpublish from the real HTTP editor through to a personal student session."""
import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from http.server import ThreadingHTTPServer
from unittest import TestCase
from unittest.mock import Mock

from server import DashboardHandler, student_image_reader
from tests.test_assignment_images import TINY
from dashboard_backend.database import DashboardStore
from dashboard_backend.service import DashboardService
from dashboard_backend.student_catalog import published_assignments
from dashboard_backend.student_mount import StudentMount
from tests.test_classroom_flow import Labs


class AssignmentFlow(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        db = directory/"dashboard.db"
        gw = Mock(mode="live")
        gw.list_templates.return_value = [{"vmid":237,"name":"test-template"}]
        service = DashboardService(DashboardStore(db,seed_demo=False),gw)
        self.addCleanup(service.stop_monitors)
        self.labs = Labs()
        students = StudentMount(directory/"students.db",directory/"unused.json",secure_cookie=True,
                                labs=self.labs,published_catalog=lambda: published_assignments(db),
                                image_reader=student_image_reader(service))
        class Handler(DashboardHandler):
            admin_token = "test-operator"
            student_mount = students
            def log_message(self, *args):
                pass
        Handler.service = service
        server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
        self.address = server.server_address
        thread = Thread(target=lambda: server.serve_forever(poll_interval=.02),daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        credential = students.create_accounts({"count":1,"group":"Fixture"})["accounts"][0]
        self.cookie = self.login(credential["login"],credential["password"])

    def req(self,path,method="GET",payload=None,*,operator=False,cookie=None):
        h = {"Content-Type":"application/json"}
        if cookie: h["Cookie"] = cookie
        if operator: h["X-Admin-Token"] = "test-operator"
        conn = http.client.HTTPConnection(*self.address,timeout=10)
        try:
            conn.request(method,path,None if payload is None else json.dumps(payload),h)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read()), resp.headers
        finally:
            conn.close()

    def login(self,login,password):
        code,_,hdr = self.req("/student-demo/api/login","POST",{"login":login,"password":password})
        self.assertEqual(code,200)
        return hdr["Set-Cookie"].split(";",1)[0]

    def test_image_from_editor_to_student_and_withdrawal(self):
        def img(path, data=None, *, token=False, cookie=None):
            h = {"Content-Type":"image/png"}
            if token: h["X-Admin-Token"] = "test-operator"
            if cookie: h["Cookie"] = cookie
            conn = http.client.HTTPConnection(*self.address,timeout=10)
            try:
                conn.request("GET" if data is None else "POST",path,data,h)
                res = conn.getresponse()
                self.assertEqual(res.headers["Cache-Control"],"no-store")
                return res.status,res.read(),res.headers.get_content_type()
            finally:
                conn.close()
        upload = "/api/assignment-images"
        self.assertEqual(img(upload,TINY,cookie=self.cookie)[0],401)
        self.assertEqual(img(upload,b"bad raster",token=True)[0],400)
        status,data,_ = img(upload,TINY,token=True)
        self.assertEqual(status,201)
        ident = json.loads(data)["id"]
        self.assertEqual(img(upload+"/"+ident,cookie=self.cookie)[0],401)
        self.assertEqual(img(upload+"/"+ident,token=True),(200,TINY,"image/png"))
        code,b,_ = self.req("/api/blueprints","POST",{"name":"Illustrated","code":"PIC","template_vmid":237,
            "status":"active","assignment_markdown":f"![Схема](attachment:{ident})"},operator=True)
        self.assertEqual(code,201)
        bp = "/api/blueprints/"+str(b["id"])
        url = f"/student-demo/api/assignments/lab-{b['id']}-237/images/{ident}"
        self.assertEqual(img(url)[0],401)
        self.assertEqual(img(url,cookie=self.cookie)[0],404)
        self.assertEqual(self.req(bp,"PATCH",{"student_visible":True},operator=True)[0],200)
        self.assertTrue(self.req("/student-demo/api/me",cookie=self.cookie)[1]["assignments"][0]["images_available"])
        self.assertEqual(img(url,cookie=self.cookie),(200,TINY,"image/png"))
        self.assertEqual(img(url,cookie=self.login("student02","DemoLab02!"))[0],404)
        self.assertEqual(self.req(bp,"PATCH",{"student_visible":False},operator=True)[0],200)
        self.assertEqual(img(url,cookie=self.cookie)[0],404)

    def test_publish_change_withdraw_and_republish(self):
        def edit(**kw):
            self.assertEqual(self.req(bp,"PATCH",kw,operator=True)[0],200)
        api = "/student-demo/api"
        def tasks():
            return self.req(api+"/me",cookie=self.cookie)[1]["assignments"]
        code,b,_ = self.req("/api/blueprints","POST",{"name":"Practical","code":"EX","template_vmid":237,
            "status":"active","assignment_markdown":"# not-yet-public","deploy_script":"private-bootstrap"},operator=True)
        self.assertEqual(code,201)
        bp = "/api/blueprints/"+str(b["id"])
        self.assertEqual(self.req(bp+"/assignment",cookie=self.cookie)[0],401)
        self.assertEqual(self.req(bp+"/assignment",operator=True)[1]["assignment_markdown"],"# not-yet-public")
        self.assertNotIn("assignment_markdown",self.req(bp)[1])
        self.assertNotIn("not-yet-public",json.dumps(self.req("/api/blueprints")[1]))
        self.assertEqual(tasks(),[])
        deploy = api+"/assignments/lab-"+str(b["id"])+"-237/deploy"
        self.assertEqual(self.req(deploy,"POST",{},cookie=self.cookie)[0],404)
        edit(student_visible=True,assignment_markdown="# Week 1\n\n**Routing**")
        self.assertEqual(tasks()[0]["assignment_markdown"],"# Week 1\n\n**Routing**")
        self.assertNotIn("private-bootstrap",json.dumps(tasks()))
        public = self.login("student02","DemoLab02!")
        self.assertNotIn("Routing",json.dumps(self.req(api+"/me",cookie=public)[1]))
        self.assertEqual(self.req(deploy,"POST",{},cookie=public)[0],404)
        code,b,_ = self.req(deploy,"POST",{},cookie=self.cookie)
        self.assertEqual(code,200)
        card = api+"/stands/"+b["stand"]["id"]
        edit(assignment_markdown="# Updated")
        self.assertEqual(self.req(card,cookie=self.cookie)[1]["assignment"]["assignment_markdown"],"# Updated")
        edit(student_visible=False,assignment_markdown="Hidden newer draft")
        self.assertEqual(self.req(deploy,"POST",{},cookie=self.cookie)[0],404)
        self.assertEqual(len(self.labs.receipts),1)
        self.assertTrue(tasks()[0]["withdrawn"])
        code,b,_ = self.req(card,cookie=self.cookie)
        self.assertEqual(code,200)
        self.assertTrue(b["assignment"]["withdrawn"])
        self.assertNotIn("assignment_markdown",b["assignment"])
        self.assertEqual(self.req(card+"/credentials","POST",{},cookie=self.cookie)[0],200)
        self.assertEqual(self.req(card,cookie=public)[0],404)
        edit(student_visible=True)
        self.assertEqual(tasks()[0]["assignment_markdown"],"Hidden newer draft")
        self.assertFalse(tasks()[0].get("withdrawn"))
        self.assertEqual(len(self.labs.receipts),1)
