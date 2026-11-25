import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard_backend import assignment_images as images
from dashboard_backend.database import DashboardStore
from dashboard_backend.service import ConflictError, DashboardService, NotFoundError, ValidationError


def chunk(name, data=b""):
    return struct.pack(">I",len(data)) + name + data + struct.pack(">I",zlib.crc32(name+data))


PNG = b"\x89PNG\r\n\x1a\n"
HEADER = chunk(b"IHDR", struct.pack(">IIBBBBB",1,1,8,6,0,0,0))
DATA = chunk(b"IDAT",zlib.compress(b"\x00\xff\x00\xff\xff"))
END = chunk(b"IEND")
TINY = PNG + HEADER + DATA + END


class AssignmentImagesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = DashboardStore(Path(temp.name)/"db", seed_demo=False)
        gw = Mock(mode="live")
        gw.list_templates.return_value = [{"vmid":276,"name":"class"}]
        self.svc = DashboardService(self.store,gw)
        self.addCleanup(self.svc.stop_monitors)
        self.bp = {"name":"class","code":"LAB","status":"active","template_vmid":276}

    def upload(self):
        return self.svc.upload_assignment_image(TINY)["id"]

    def test_png_raster_only_bounded_and_metadata_removed(self):
        secret = chunk(b"tEXt",b"note\x00private text in metadata")
        ident = self.svc.upload_assignment_image(PNG+HEADER+secret+DATA+END)["id"]
        self.assertEqual(self.svc.assignment_image(ident),TINY)
        for invalid in (b"<svg/>",TINY[:-4],TINY+b"x",PNG+HEADER+DATA+chunk(b"IEND",b"x"),
                        PNG+HEADER+chunk(b"IDAT",zlib.compress(b"\x00"*500000))+END,
                        PNG+HEADER+chunk(b"IDAT",zlib.compress(bytes([5])*5))+END,
                        PNG+HEADER+chunk(b"IDAT",zlib.compress(bytes(5))+b"junk")+END,
                        PNG+HEADER+chunk(b"IDAT",zlib.compress(bytes(5))[:-2])+END,
                        PNG+HEADER+chunk(b"acTL",bytes(8))+DATA+END,
                        PNG+HEADER+DATA+chunk(b"pHYs",bytes(9))+DATA+END,
                        PNG+chunk(b"IHDR",struct.pack(">IIBBBBB",50000,6000,8,6,0,0,0))+DATA+END,
                        TINY[:30]+bytes([TINY[30]^1])+TINY[31:],
                        bytes(images.MAX_IMAGE_BYTES+1)):
            with self.subTest(length=len(invalid)), self.assertRaises(ValidationError):
                self.svc.upload_assignment_image(invalid)
        self.assertEqual(self.store.query_one("SELECT count(*) AS n FROM assignment_images")["n"],1)

    def test_blueprint_reference_is_exact_revocable_and_shared_with_duplicate(self):
        img = self.upload()
        art = f"![Screenshot](attachment:{img})"
        bp = self.svc.save_blueprint(dict(self.bp,assignment_markdown=art))
        sid = bp["id"]
        task = f"lab-{sid}-276"
        with self.assertRaises(NotFoundError):
            self.svc.published_assignment_image(task,img)
        self.svc.save_blueprint({"student_visible":True},sid)
        self.assertEqual(self.svc.published_assignment_image(task,img),TINY)
        clone = self.svc.duplicate_blueprint(sid)
        copy_task = f"lab-{clone['id']}-276"
        for wrong,ident in [(copy_task,img),(f"lab-{sid}-278",img),(task,self.upload()),("other",img),(task,"xxx")]:
            with self.assertRaises(NotFoundError):
                self.svc.published_assignment_image(wrong,ident)
        self.svc.save_blueprint({"status":"active","student_visible":True},clone["id"])
        self.svc.save_blueprint({"assignment_markdown":"withdrawn illustration"},sid)
        with self.assertRaises(NotFoundError):
            self.svc.published_assignment_image(task,img)
        self.assertEqual(self.svc.published_assignment_image(copy_task,img),TINY)
        self.svc.save_blueprint({"student_visible":False},clone["id"])
        with self.assertRaises(NotFoundError):
            self.svc.published_assignment_image(copy_task,img)
        self.svc.delete_blueprint(clone["id"])
        self.assertIsNotNone(self.store.query_one("SELECT unused_since FROM assignment_images WHERE id=?",(img,))["unused_since"])

    def test_missing_reference_rolls_back_create_update_and_flag(self):
        unknown = "![x](attachment:"+"a"*32+")"
        with self.assertRaises(ValidationError):
            self.svc.save_blueprint(dict(self.bp,student_visible=True,assignment_markdown=unknown))
        self.assertEqual(self.svc.list_blueprints(),[])
        sid = self.svc.save_blueprint(dict(self.bp,assignment_markdown="ok"))["id"]
        with self.assertRaises(ValidationError):
            self.svc.save_blueprint({"student_visible":True,"assignment_markdown":unknown},sid)
        self.assertFalse(self.svc.get_blueprint(sid)["student_visible"])
        self.assertEqual(self.svc.get_blueprint(sid)["assignment_markdown"],"ok")

    def test_raw_code_is_not_an_attachment_and_abuse_is_bounded(self):
        asset = "e"*32
        art = f"![a](attachment:{asset})"
        for text in (f"`{art}`", "\\"+art, "~~~markdown\n"+art+"\n~~~",f"[a](attachment:{asset})",
                     f"![a](https://example.com/{asset})",f"(attachment:{asset})"):
            self.assertEqual(images.references(text),set(),text)
        self.assertEqual(images.references(art+" "+art),{asset})
        many = "\n".join(f"![a](attachment:{i:032x})" for i in range(13))
        with self.assertRaises(images.ImageInputError):
            images.references(many)
        with patch.object(images,"STORAGE_BYTES",len(TINY)):
            old = self.upload()
            with self.assertRaises(ConflictError):
                self.upload()
            self.store.execute("UPDATE assignment_images SET unused_since=1 WHERE id=?",(old,))
            self.upload()
            with self.assertRaises(NotFoundError):
                self.svc.assignment_image(old)
