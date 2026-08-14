import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.safe_file_cleanup import (
    UnsafeCleanupPath,
    quarantine_export_package,
    restore_quarantined_paths,
)


class SafeFileCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.state = Path(self.temp.name) / "state"
        (self.root / "data").mkdir(parents=True)
        (self.root / "keep.txt").write_text("keep", encoding="utf-8")
        self.env = patch.dict(os.environ, {"XIANYU_DATA_DIR": "data"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_empty_paths_are_noop_and_never_mean_working_directory(self):
        moved = quarantine_export_package(
            {"package_dir": "", "package_zip": None},
            kind="ip_content_studio", record_id=5, user_id=1,
            project_root=self.root, state_root=self.state,
        )
        self.assertEqual(moved, [])
        self.assertEqual((self.root / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_dot_and_project_root_are_rejected(self):
        for dangerous in (".", str(self.root), str(Path.cwd())):
            with self.subTest(dangerous=dangerous):
                with self.assertRaises(UnsafeCleanupPath):
                    quarantine_export_package(
                        {"package_dir": dangerous},
                        kind="ip_content_studio", record_id=5, user_id=1,
                        project_root=self.root, state_root=self.state,
                    )
        self.assertTrue((self.root / "keep.txt").is_file())

    def test_ip_package_is_moved_to_external_trash_and_can_be_restored(self):
        package_dir = self.root / "data/ip_content_studio/1/8"
        package_zip = self.root / "data/ip_content_studio/1/ip-content-8.zip"
        package_dir.mkdir(parents=True)
        (package_dir / "master.md").write_text("article", encoding="utf-8")
        package_zip.write_bytes(b"zip")
        moved = quarantine_export_package(
            {
                "package_dir": "data/ip_content_studio/1/8",
                "package_zip": "data/ip_content_studio/1/ip-content-8.zip",
            },
            kind="ip_content_studio", record_id=8, user_id=1,
            project_root=self.root, state_root=self.state,
        )
        self.assertEqual(len(moved), 2)
        self.assertFalse(package_dir.exists())
        self.assertFalse(package_zip.exists())
        self.assertTrue(all(item.destination.exists() for item in moved))
        self.assertTrue((moved[0].destination.parent / "record.json").is_file())
        restore_quarantined_paths(moved)
        self.assertTrue((package_dir / "master.md").is_file())
        self.assertTrue(package_zip.is_file())

    def test_other_record_or_outside_path_is_rejected(self):
        for dangerous in (
            "data/ip_content_studio/1/9",
            "data/content_products/1/8",
            str(Path(self.temp.name) / "outside"),
        ):
            with self.subTest(dangerous=dangerous):
                with self.assertRaises(UnsafeCleanupPath):
                    quarantine_export_package(
                        {"package_dir": dangerous},
                        kind="ip_content_studio", record_id=8, user_id=1,
                        project_root=self.root, state_root=self.state,
                    )

    def test_content_product_uses_its_own_exact_layout(self):
        package_dir = self.root / "data/content_products/3/12"
        package_zip = self.root / "data/content_products/3/12.zip"
        package_dir.mkdir(parents=True)
        package_zip.write_bytes(b"zip")
        moved = quarantine_export_package(
            {"package_dir": str(package_dir), "package_zip": str(package_zip)},
            kind="content_products", record_id=12, user_id=3,
            project_root=self.root, state_root=self.state,
        )
        self.assertEqual(len(moved), 2)
        self.assertTrue((self.root / "keep.txt").is_file())

    def test_trash_must_be_outside_project_root(self):
        package_dir = self.root / "data/ip_content_studio/1/8"
        package_dir.mkdir(parents=True)
        with self.assertRaises(UnsafeCleanupPath):
            quarantine_export_package(
                {"package_dir": "data/ip_content_studio/1/8"},
                kind="ip_content_studio", record_id=8, user_id=1,
                project_root=self.root, state_root=self.root / "trash",
            )
        self.assertTrue(package_dir.is_dir())


if __name__ == "__main__":
    unittest.main()
