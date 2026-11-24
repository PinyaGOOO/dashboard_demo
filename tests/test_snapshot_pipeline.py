import threading
import unittest
from unittest.mock import MagicMock

from dashboard_backend.proxmox_gateway import LiveProxmoxGateway, SnapshotOutcomeUnknown


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.gateway = LiveProxmoxGateway.__new__(LiveProxmoxGateway)
        self.gateway._snapshot_limit = 2
        self.gateway._snapshot_slots = threading.BoundedSemaphore(2)
        self.gateway.client = MagicMock()
        self.gateway._wait_tasks = MagicMock()
        self.placements = {101: "node-a", 102: "node-a", 201: "node-b"}
        self.gateway._vm_inventory = MagicMock(return_value={
            vmid: {"node": node} for vmid, node in self.placements.items()
        })
        self.nodes = {node: MagicMock() for node in set(self.placements.values())}
        self.gateway.client.nodes.side_effect = self.nodes.__getitem__
        self.apis = {vmid: MagicMock() for vmid in self.placements}
        for node in self.nodes.values():
            node.qemu.side_effect = self.apis.__getitem__
        for vmid, api in self.apis.items():
            api.snapshot.post.return_value = f"UPID:{vmid}"

    def test_slow_first_task_does_not_hold_free_slot(self):
        third_entered = threading.Event()
        second_finished = threading.Event()
        caller = threading.get_ident()
        confirmed = []

        def wait_tasks(tasks, timeout):
            self.assertEqual(timeout, 1800)
            [(node, upid)] = tasks
            vmid = int(upid.split(":")[1])
            self.assertEqual(node, self.placements[vmid])
            if vmid == 101:
                self.assertTrue(third_entered.wait(3), "pool still waits at a batch barrier")
            elif vmid == 201:
                second_finished.set()
            else:
                self.assertTrue(second_finished.is_set(), "second node was not scheduled in first window")
                third_entered.set()

        def callback(vmid):
            self.assertEqual(threading.get_ident(), caller)
            confirmed.append(vmid)

        self.gateway._wait_tasks.side_effect = wait_tasks
        self.gateway.create_snapshot(list(self.placements), "lab", "desc", on_completed=callback)
        self.assertCountEqual(confirmed, list(self.placements))
        self.gateway._vm_inventory.assert_called_once()
        for api in self.apis.values():
            api.snapshot.post.assert_called_once_with(snapname="lab", description="desc", vmstate=0)

    def test_missing_upid_drains_admitted_peer_reporting_only_confirmed_vm(self):
        first_started = threading.Event()
        released = threading.Event()
        confirmed = []

        def wait_tasks(tasks, timeout):
            self.assertEqual(tasks, [("node-a", "UPID:101")])
            first_started.set()
            self.assertTrue(released.wait(2))

        def lost_response(**kwargs):
            self.assertTrue(first_started.wait(2))
            return None

        self.apis[201].snapshot.post.side_effect = lost_response
        self.gateway._wait_tasks.side_effect = wait_tasks
        # Release the admitted peer only after the failing worker released its slot.
        def release_peer():
            if first_started.wait(2):
                while not self.apis[201].snapshot.post.called:
                    if released.wait(.01):
                        return
                with self.gateway._snapshot_slots:
                    released.set()

        peer = threading.Thread(target=release_peer, daemon=True)
        peer.start()
        with self.assertRaisesRegex(SnapshotOutcomeUnknown, r"VM 201 \(node-b\).*UPID"):
            self.gateway.create_snapshot(list(self.placements), "lab", on_completed=confirmed.append)
        peer.join(3)
        self.assertEqual(confirmed, [101])
        self.apis[102].snapshot.post.assert_not_called()

    def test_manual_and_baseline_requests_share_capacity(self):
        first_started = threading.Event()
        second_approached = threading.Event()
        active = []
        peak = []
        lock = threading.Lock()
        self.gateway._snapshot_limit = 1
        self.gateway._snapshot_slots = threading.BoundedSemaphore(1)

        def wait_tasks(tasks, timeout):
            with lock:
                active.append(tasks[0])
                peak.append(len(active))
            if tasks[0][1] == "UPID:101":
                first_started.set()
                self.assertTrue(second_approached.wait(3))
            with lock:
                active.remove(tasks[0])

        self.gateway._wait_tasks.side_effect = wait_tasks
        errors = []

        def manual_snapshot():
            try:
                self.assertTrue(first_started.wait(3))
                second_approached.set()
                self.gateway._snapshot_group({201: "node-b"}, "midterm", "", None)
            except Exception as exc:
                errors.append(str(exc))

        manual = threading.Thread(target=manual_snapshot, daemon=True)
        manual.start()
        self.gateway._snapshot_group({101: "node-a"}, "start", "", None)
        manual.join(3)
        self.assertFalse(manual.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(peak, [1, 1])

    def test_unconfirmed_start_snapshot_preserves_pool_and_vm(self):
        gateway = self.gateway
        gateway._allocate_clone_task = MagicMock(return_value=(101, None))
        gateway._submit_start_task = MagicMock(return_value=None)
        gateway._wait_linked_clone_visible = MagicMock()
        gateway._wait_guest_agent = MagicMock()
        gateway._cleanup_failed_deploy = MagicMock()
        gateway.client.pools.get.return_value = []
        self.apis[101].config.get.return_value = {}

        def wait_tasks(tasks, timeout=1800):
            if tasks:
                self.assertEqual(tasks, [("node-a", "UPID:101")])
                raise TimeoutError("worker still running")

        gateway._wait_tasks.side_effect = wait_tasks
        with self.assertRaisesRegex(RuntimeError, "UPID:101.*VM сохранены"):
            gateway.deploy(
                {"id": 1, "name": "class", "pool_id": "class"},
                {"template_vmid": 8, "_scheduler_scope": {
                    "template_vmid": 8, "template_node": "node-a", "target_nodes": ["node-a"],
                }},
                MagicMock(),
            )
        gateway._cleanup_failed_deploy.assert_not_called()
