"""Report command integration and read-only volume policy."""

from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from buildharbor.cli import main
from buildharbor.config import Config
from buildharbor.errors import BuildHarborError
from buildharbor.volume import Volume, inspect_volume


UUID = "11111111-2222-3333-4444-555555555555"


class ReportCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="buildharbor-report-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = Config(self.root, "demo", self.root, self.root / "Storage", UUID)
        self.volume = Volume(self.root.stat().st_dev, 0, UUID)

    def test_report_needs_no_xcode_and_creates_no_missing_storage(self):
        output = io.StringIO()
        with patch("buildharbor.cli.load_config", return_value=self.config), patch(
            "buildharbor.storage.inspect_volume", return_value=self.volume
        ), patch("buildharbor.cli.inspect_xcode", side_effect=AssertionError("No Xcode")), patch(
            "subprocess.Popen", side_effect=AssertionError("No process")
        ), redirect_stdout(output):
            status = main(["report", "--json"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(report["kind"], "storage_report")
        self.assertEqual(report["storage_root_state"], "absent")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_repeated_project_directories_are_loaded_and_reconciled(self):
        other_root = self.root / "Other"
        other_root.mkdir()
        other = replace(self.config, project_root=other_root, project_id="other")
        directory = self.config.storage_root / "projects/demo-0123456789abcdef"
        directory.mkdir(parents=True)
        (directory / "data").write_bytes(b"count once")
        output = io.StringIO()
        with patch("buildharbor.cli.load_config", side_effect=[self.config, other]) as load, patch(
            "buildharbor.storage.inspect_volume", return_value=self.volume
        ), redirect_stdout(output):
            status = main(["report", "--project-dir", str(self.root), "--project-dir", str(other_root), "--json"])
        self.assertEqual([call.args[0] for call in load.call_args_list], [self.root, other_root])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(report["projects"]["demo"]["logical_bytes"], 10)
        self.assertEqual(report["projects"]["other"]["logical_bytes"], 0)
        self.assertEqual(report["totals"]["regular_files"], 1)

    def test_unavailable_volume_emits_incomplete_report_and_nonzero_exit(self):
        output = io.StringIO()
        with patch("buildharbor.cli.load_config", return_value=self.config), patch(
            "buildharbor.storage.inspect_volume", side_effect=BuildHarborError("private diagnostic")
        ), redirect_stdout(output):
            status = main(["report", "--json"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(report["kind"], "storage_report")
        self.assertEqual(report["status"], "incomplete")
        self.assertTrue(report["is_lower_bound"])
        self.assertNotIn("private diagnostic", output.getvalue())

    def disk(self, **changes):
        data = {"MountPoint": str(self.root), "VolumeUUID": UUID, "FilesystemType": "apfs", "Internal": False, "WritableVolume": True, "Locked": False}
        data.update(changes)
        return subprocess.CompletedProcess([], 0, plistlib.dumps(data), b"")

    def test_read_only_inspection_allows_low_capacity_and_read_only_disk(self):
        with patch("sys.platform", "darwin"), patch.object(Path, "is_mount", return_value=True), patch(
            "subprocess.run", return_value=self.disk(ReadOnlyVolume=True, WritableVolume=False)
        ), patch("os.statvfs", return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)), patch(
            "os.access", return_value=True
        ) as access:
            self.assertEqual(inspect_volume(self.config, read_only=True).available_bytes, 0)
            access.assert_called_once_with(self.root, os.R_OK | os.X_OK)
            with self.assertRaises(BuildHarborError):
                inspect_volume(self.config)

    def test_report_volume_still_requires_identity_external_apfs_and_unlocked(self):
        for changes in ({"VolumeUUID": "wrong"}, {"Internal": True}, {"FilesystemType": "hfs"}, {"Locked": True}):
            with self.subTest(changes=changes), patch("sys.platform", "darwin"), patch.object(
                Path, "is_mount", return_value=True
            ), patch("subprocess.run", return_value=self.disk(**changes)), self.assertRaises(BuildHarborError):
                inspect_volume(self.config, read_only=True)


if __name__ == "__main__":
    unittest.main()
