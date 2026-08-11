import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet

from auto_updater import AutoUpdater, DeletedFile, FileUpdate
from db_manager import DBManager
from utils.project_guard import ProjectIntegrityError, ensure_project_root


class AutoUpdaterSafetyTests(unittest.TestCase):
    def test_source_mutation_is_locked_by_default(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=False):
            os.environ.pop(AutoUpdater.MUTATION_ENV, None)
            updater = AutoUpdater(app_dir=directory)
            item = FileUpdate("demo.txt", "", 0, "", "1", False)
            with self.assertRaisesRegex(RuntimeError, "Git"):
                asyncio.run(updater.apply_updates([item]))

    def test_deletion_requires_second_explicit_switch(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {AutoUpdater.MUTATION_ENV: "1"}, clear=False
        ):
            os.environ.pop(AutoUpdater.DELETION_ENV, None)
            updater = AutoUpdater(app_dir=directory)
            with self.assertRaisesRegex(RuntimeError, "Git"):
                asyncio.run(updater.apply_deletions([DeletedFile("demo.txt", False)]))

    def test_critical_paths_stay_protected_with_both_switches(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {AutoUpdater.MUTATION_ENV: "1", AutoUpdater.DELETION_ENV: "1"},
            clear=False,
        ):
            root = Path(directory)
            target = root / "reply_server.py"
            target.write_text("protected", encoding="utf-8")
            updater = AutoUpdater(app_dir=directory)
            success, deleted, _ = asyncio.run(
                updater.apply_deletions([DeletedFile("reply_server.py", True)])
            )
            self.assertFalse(success)
            self.assertEqual(deleted, [])
            self.assertTrue(target.is_file())


class ProjectRootGuardTests(unittest.TestCase):
    def test_missing_sentinel_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProjectIntegrityError):
                ensure_project_root(Path(directory))


class SecretRecoveryTests(unittest.TestCase):
    def test_secret_from_lost_key_is_treated_as_unconfigured(self):
        old_cipher = Fernet(Fernet.generate_key())
        manager = DBManager.__new__(DBManager)
        manager.secret_fernet = Fernet(Fernet.generate_key())
        ciphertext = "enc$" + old_cipher.encrypt(b"sensitive-cookie").decode("utf-8")
        self.assertEqual(manager._decrypt_secret(ciphertext), "")


if __name__ == "__main__":
    unittest.main()
