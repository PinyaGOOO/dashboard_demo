"""PVE 8.1+ collection endpoints: a slash is data, never a /pools URL segment."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from dashboard_backend.proxmox_gateway import LiveProxmoxGateway


ROOT = "student-labs"
GROUP = ROOT + "/ssa-25-04"
LEAF = GROUP + "/lab-" + "0"*24
SIBLING = GROUP + "/lab-" + "a"*24
MARKER = "DEMOEXAM dashboard stand_id=7"


class Pools:
    def __init__(self):
        self.rows = {}
        self.created = []
        self.deleted = []
        self.lock = threading.Lock()

    def __call__(self, _):
        raise AssertionError("a nested pool ID cannot be interpolated into the URL")

    def get(self, *, poolid=None):
        with self.lock:
            if poolid is None:
                return [{"poolid":name} for name in self.rows]
            if poolid not in self.rows:
                raise RuntimeError(f"pool '{poolid}' does not exist")
            return [dict(self.rows[poolid])]

    def post(self, *, poolid, comment):
        with self.lock:
            if poolid in self.rows:
                raise RuntimeError("already exists")
            if "/" in poolid and poolid.rsplit("/", 1)[0] not in self.rows:
                raise RuntimeError("missing parent")
            self.rows[poolid] = dict(poolid=poolid, comment=comment, members=[])
            self.created.append(poolid)

    def delete(self, *, poolid):
        if self.rows[poolid]["members"]:
            raise AssertionError("cannot remove a nonempty pool")
        self.deleted.append(poolid)
        del self.rows[poolid]


class NestedPoolGatewayTests(unittest.TestCase):
    def setUp(self):
        self.gw = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        self.gw.client = MagicMock()
        self.gw._clone_submit_lock = threading.Lock()
        self.pools = self.gw.client.pools = Pools()
        self.stand = dict(id=7, pool_id=LEAF, origin="deployed", vm_count=1)
        self.plan = dict(_student_template_name="Fixture", _student_pool_path=LEAF, template_vmid=0)

    def test_distinct_sibling_deployments_share_parents_but_rollback_only_their_leaf(self):
        # A deliberate local preflight failure exercises the complete
        # preparation/cleanup path without submitting an actual clone.
        for leaf in (LEAF, SIBLING):
            with self.assertRaisesRegex(RuntimeError, "VMID шаблона"):
                self.gw.deploy(dict(self.stand,pool_id=leaf), dict(self.plan,_student_pool_path=leaf), lambda *_:None)
        self.assertEqual(self.pools.created, [ROOT, GROUP, LEAF, SIBLING])
        self.assertEqual(self.pools.deleted, [LEAF,SIBLING])
        self.assertEqual(set(self.pools.rows), {ROOT,GROUP})

    def test_concurrent_parent_creation_and_foreign_or_occupied_parent(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            # Both jobs started with an index that did not yet contain the parents.
            list(workers.map(lambda leaf:self.gw._ensure_student_parents(leaf,set()), (LEAF,SIBLING)))
        self.assertEqual(self.pools.created, [ROOT,GROUP])
        for key, value in (("members",[{"type":"qemu","vmid":111}]), ("comment","foreign")):
            with self.subTest(key=key):
                old = self.pools.rows[GROUP][key]
                self.pools.rows[GROUP][key] = value
                with self.assertRaisesRegex(RuntimeError, "создание отменено"):
                    self.gw.deploy(self.stand,self.plan,lambda *_:None)
                self.pools.rows[GROUP][key] = old
        self.assertFalse(self.pools.deleted)

    def test_cannot_mutate_parent_or_deploy_without_pinned_student_path(self):
        for target in (ROOT, GROUP, ROOT+"/../"+LEAF.split("/")[-1], "wrong/x/y"):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    self.gw.delete_stand(dict(self.stand,pool_id=target), [])
                with self.assertRaises(ValueError):
                    self.gw._cleanup_failed_deploy([],target,MARKER)
        for stand, plan in ((self.stand, {}), (self.stand,dict(self.plan,_student_pool_path=SIBLING)),
                            (dict(self.stand,origin="existing"),self.plan), (dict(self.stand,vm_count=2),self.plan)):
            with self.assertRaises(RuntimeError):
                self.gw.deploy(stand,plan,lambda *_:None)
        self.assertFalse(self.pools.created)
        self.assertFalse(self.pools.deleted)

    def test_clone_receives_full_leaf_and_membership_reads_only_its_exact_array(self):
        self.gw._ensure_student_parents(LEAF,set())
        self.pools.post(poolid=LEAF,comment=MARKER)
        self.pools.post(poolid=SIBLING,comment="DEMOEXAM dashboard stand_id=8")
        self.pools.rows[SIBLING]["members"] = [dict(type="qemu",vmid=123,name="other")]
        self.gw.client.cluster.nextid.get.return_value = 123
        api = self.gw.client.nodes("n").qemu(111).clone
        api.post.return_value = "UPID:n:test"
        self.gw._allocate_clone_task("n",111,LEAF,"n","personal",[],{})
        api.post.assert_called_once_with(newid=123, name="personal", full=0, target="n", pool=LEAF)
        self.assertEqual(self.gw.pool_members(LEAF),[])
        self.assertFalse(self.gw._clone_was_accepted(LEAF,123,"other"))
        # A stale VMID now belongs to another VM in the leaf. Do not stop it.
        self.pools.rows[LEAF]["members"] = [dict(type="qemu",vmid=123,name="changed")]
        with self.assertRaisesRegex(RuntimeError,"имя VM"):
            self.gw.delete_stand(dict(self.stand,vms=[dict(name="personal",vmid=123)]), [123])
        _, errors = self.gw._cleanup_failed_deploy([("n",123)],LEAF,MARKER,expected_names={123:"personal"})
        self.assertTrue(errors)
        self.pools.rows[LEAF]["members"] = []
        self.gw.delete_stand(self.stand, [])
        self.assertEqual(self.pools.deleted,[LEAF])
        self.assertIn(SIBLING,self.pools.rows)

    def test_ambiguous_or_wrong_pool_data_never_becomes_empty_membership_or_deletion(self):
        self.gw.client.pools = MagicMock()
        for rows in ([],{},[{"members":[],"poolid":SIBLING}],
                     [{"poolid":LEAF,"comment":MARKER}], [{"poolid":LEAF,"members":[]}]*2):
            self.gw.client.pools.get.return_value = rows
            with self.assertRaises(RuntimeError):
                self.gw.pool_members(LEAF)
            with self.assertRaises(RuntimeError):
                self.gw.delete_stand(self.stand,[])
        self.gw.client.pools.delete.assert_not_called()
        self.gw.client.nodes.assert_not_called()

    def test_unsupported_pool_api_does_not_create_flat_root(self):
        self.gw.client.pools = MagicMock()
        self.gw.client.pools.get.side_effect = [[], RuntimeError("poolid: property not in schema")]
        with self.assertRaisesRegex(RuntimeError, "вложенных пулов"):
            self.gw.deploy(self.stand, self.plan, lambda *_: None)
        self.gw.client.pools.post.assert_not_called()

    def test_personal_inventory_does_not_hide_unexpected_members(self):
        self.pools.rows[LEAF] = dict(poolid=LEAF, comment=MARKER, members=[
            dict(type="qemu", vmid=123, name="personal"), dict(type="storage", storage="nas"),
        ])
        with self.assertRaises(RuntimeError):
            self.gw.pool_members(LEAF)
        with self.assertRaises(RuntimeError):
            self.gw._clone_was_accepted(LEAF, 123, "personal")
