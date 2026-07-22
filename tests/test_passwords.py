from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

from dashboard_backend.database import DashboardStore, utc_now
from dashboard_backend.passwords import (
    PASSWORD_DIGITS,
    PASSWORD_LOWER,
    PASSWORD_SYMBOLS,
    PASSWORD_UPPER,
    PROXMOX_PASSWORD_MAX_LENGTH,
    generate_password,
)
from dashboard_backend.service import DashboardService, ValidationError


class PasswordGenerationTests(unittest.TestCase):
    def test_generated_passwords_are_readable_and_cover_every_character_class(self) -> None:
        ambiguous = set("0O1IiloL")

        for _ in range(1_000):
            password = generate_password()

            self.assertEqual(len(password), 16)
            self.assertTrue(ambiguous.isdisjoint(password), password)
            self.assertTrue(any(character in PASSWORD_UPPER for character in password), password)
            self.assertTrue(any(character in PASSWORD_LOWER for character in password), password)
            self.assertTrue(any(character in PASSWORD_DIGITS for character in password), password)
            self.assertTrue(any(character in PASSWORD_SYMBOLS for character in password), password)


class LongCustomPasswordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = DashboardStore(
            Path(self.temporary.name) / "dashboard.db",
            seed_demo=False,
        )
        self.gateway = MagicMock()
        self.gateway.mode = "live"
        self.service = DashboardService(self.store, self.gateway)
        self.stand_id, self.vmid = self._seed_running_stand()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seed_running_stand(self) -> tuple[int, int]:
        now = utc_now()
        stand_id = self.store.execute(
            """INSERT INTO stands
            (name, status, progress, node, pool_id, owner, vm_count,
             created_at, updated_at)
            VALUES (?, 'running', 100, 'pve-1', 'password-test', 'Admin', 1, ?, ?)""",
            ("Password test", now, now),
        )
        vmid = 274
        self.store.execute(
            """INSERT INTO stand_vms
            (stand_id, vmid, name, node, status, credential_username,
             web_username, credential_password)
            VALUES (?, ?, ?, 'pve-1', 'running', 'root', 'root@pam', 'Initial-password')""",
            (stand_id, vmid, "password-test-1"),
        )
        return stand_id, vmid

    def test_full_proxmox_password_length_is_not_truncated_for_stand_or_vm_rotation(self) -> None:
        stand_password = "A2!b" * (PROXMOX_PASSWORD_MAX_LENGTH // 4)
        vm_password = "Z9@c" * (PROXMOX_PASSWORD_MAX_LENGTH // 4)

        stand_result = self.service.stand_action(
            self.stand_id,
            "rotate_password",
            {"username": "root", "web_username": "root@pam", "password": stand_password},
        )
        self.assertEqual(stand_result["credential"]["password"], stand_password)
        self.assertEqual(
            self.store.query_one(
                "SELECT credential_password FROM stand_vms WHERE stand_id = ? AND vmid = ?",
                (self.stand_id, self.vmid),
            )["credential_password"],
            stand_password,
        )

        vm_result = self.service.vm_action(
            self.stand_id,
            self.vmid,
            "rotate_password",
            {"username": "root", "web_username": "root@pam", "password": vm_password},
        )
        self.assertEqual(vm_result["credential"]["password"], vm_password)
        self.assertEqual(
            self.store.query_one(
                "SELECT credential_password FROM stand_vms WHERE stand_id = ? AND vmid = ?",
                (self.stand_id, self.vmid),
            )["credential_password"],
            vm_password,
        )
        self.gateway.rotate_password.assert_has_calls(
            [
                call([self.vmid], "root", stand_password),
                call([self.vmid], "root", vm_password),
            ]
        )

    def test_password_beyond_native_proxmox_limit_is_rejected_before_gateway(self) -> None:
        password = "A2!b" * (PROXMOX_PASSWORD_MAX_LENGTH // 4) + "x"

        with self.assertRaisesRegex(ValidationError, "Proxmox.*1024"):
            self.service.stand_action(
                self.stand_id,
                "rotate_password",
                {"username": "root", "web_username": "root@pam", "password": password},
            )
        with self.assertRaisesRegex(ValidationError, "Proxmox.*1024"):
            self.service.vm_action(
                self.stand_id,
                self.vmid,
                "rotate_password",
                {"username": "root", "web_username": "root@pam", "password": password},
            )

        self.gateway.rotate_password.assert_not_called()


if __name__ == "__main__":
    unittest.main()
