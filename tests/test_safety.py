"""Simulated system tests. These do not establish real Xcode compatibility."""

import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from buildharbor.cli import main
from buildharbor.config import Config, load_config
from buildharbor.errors import BuildHarborError
from buildharbor.paths import open_directory, validate_destination
from buildharbor.planner import make_plan, parse_arguments
from buildharbor.reporting import plan_report
from buildharbor.volume import Volume, inspect_volume
from buildharbor.xcode import Xcode, inspect_xcode

UUID = "11111111-2222-3333-4444-555555555555"


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="buildharbor-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.mount = self.root / "External SSD"
        self.mount.mkdir()
        self.project = self.root / "Source with spaces"
        self.project.mkdir()
        self.identity = self.project / "Demo.xcodeproj"
        self.identity.mkdir()
        (self.identity / "project.pbxproj").write_text("{ objects = {}; }")
        self.config = Config(self.project, "demo", self.mount, self.mount / "Harbor", UUID, 1024)
        self.volume = Volume(self.mount.stat().st_dev, 10**12, UUID)
        self.xcode = Xcode(Path("/Example/Xcode.app/Contents/Developer"), "27.0", "27A266a", Path("/example/xcodebuild"))
        self.arguments = ["-project", "Demo.xcodeproj", "-scheme", "Demo", "build"]

    def plan(self, args=None, env=None):
        with patch("buildharbor.planner.inspect_volume", return_value=self.volume), patch("buildharbor.planner.inspect_xcode", return_value=self.xcode):
            return make_plan(self.config, self.arguments if args is None else args, {} if env is None else env)

    def snapshot(self):
        """Capture names, types, modes, links, and file bytes below the test root."""
        result = {}
        for path in sorted(self.root.rglob("*")):
            relative = str(path.relative_to(self.root))
            metadata = path.lstat()
            if path.is_symlink():
                content = ("symlink", os.readlink(path))
            elif path.is_file():
                content = ("file", path.read_bytes())
            else:
                content = ("directory", None)
            result[relative] = (metadata.st_mode, content)
        return result

    def test_plan_success_and_failure_preserve_tree_contents(self):
        before = self.snapshot()
        with patch("subprocess.Popen", side_effect=AssertionError("No process during a mocked plan")):
            report = plan_report(self.plan())
        self.assertEqual(before, self.snapshot())
        self.assertEqual(report["write_access"], "not_probed_read_only_plan")
        self.assertEqual(report["schema_version"], 1)

        (self.identity / "project.pbxproj").write_text("{ SYMROOT = /tmp/escape; }")
        before_failure = self.snapshot()
        with self.assertRaises(BuildHarborError):
            self.plan()
        self.assertEqual(before_failure, self.snapshot())

    def test_doctor_success_and_failure_preserve_tree_contents(self):
        common = (
            patch("buildharbor.cli.load_config", return_value=self.config),
            patch("buildharbor.cli.inspect_xcode", return_value=self.xcode),
            patch("builtins.print"),
        )
        before = self.snapshot()
        with common[0], common[1], common[2], patch("buildharbor.cli.inspect_volume", return_value=self.volume):
            self.assertEqual(main(["doctor", "--project-dir", str(self.project)]), 0)
        self.assertEqual(before, self.snapshot())

        before_failure = self.snapshot()
        with patch("buildharbor.cli.load_config", return_value=self.config), patch(
            "buildharbor.cli.inspect_volume", side_effect=BuildHarborError("missing volume")
        ), patch("builtins.print"):
            self.assertEqual(main(["doctor", "--project-dir", str(self.project)]), 2)
        self.assertEqual(before_failure, self.snapshot())

    def test_paths_with_spaces_remain_single_arguments(self):
        plan = self.plan()
        at = plan.command.index("-derivedDataPath")
        self.assertEqual(plan.command[at + 1], str(plan.outputs["derived_data"]))
        self.assertIn("External SSD", plan.command[at + 1])

    def test_caches_reused_results_unique(self):
        args = self.arguments[:-1] + ["test"]
        first, second = self.plan(args), self.plan(args)
        for key in ("derived_data", "package_cache", "module_cache"):
            self.assertEqual(first.outputs[key], second.outputs[key])
        self.assertNotEqual(first.outputs["result_bundle"], second.outputs["result_bundle"])
        self.assertNotIn(first.outputs["result_bundle"], first.directories)

    def test_build_routes_error_result_bundle_too(self):
        plan = self.plan()
        self.assertIn("-resultBundlePath", plan.command)
        self.assertEqual(plan.command[plan.command.index("-resultBundlePath") + 1], str(plan.outputs["result_bundle"]))
        self.assertNotIn(plan.outputs["result_bundle"], plan.directories)

    def test_duplicate_flags_and_settings_rejected(self):
        for extra in (["-scheme", "Other"], ["-jobs", "1", "-jobs", "2"], ["CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_ALLOWED=NO"]):
            with self.subTest(extra=extra), self.assertRaises(BuildHarborError):
                self.plan(self.arguments + extra)

    def test_workspace_and_nested_projects_are_rejected(self):
        workspace = self.project / "Demo.xcworkspace"
        workspace.mkdir()
        (workspace / "contents.xcworkspacedata").write_text(
            '<Workspace version="1.0"><FileRef location="group:Demo.xcodeproj"/></Workspace>'
        )
        (self.identity / "project.pbxproj").write_text("{ CONFIGURATION_BUILD_DIR = /tmp/escape; }")
        workspace_args = ["-workspace", "Demo.xcworkspace", "-scheme", "Demo", "build"]
        with self.assertRaises(BuildHarborError):
            self.plan(workspace_args)

        (self.identity / "project.pbxproj").write_text(
            '{ isa = PBXFileReference; lastKnownFileType = wrapper.pb-project; path = Nested.xcodeproj; }'
        )
        with self.assertRaises(BuildHarborError):
            self.plan()

    def test_managed_setting_cannot_hide_between_comment_markers_in_strings(self):
        (self.identity / "project.pbxproj").write_text(
            '{ shellScript = "printf /*"; CONFIGURATION_BUILD_DIR = /tmp/escape; marker = "*/"; }'
        )
        with self.assertRaises(BuildHarborError):
            self.plan()

    def test_test_only_options_require_test_and_selectors_are_nonempty(self):
        extras = (
            ["-testPlan", "Unit"],
            ["-enableCodeCoverage", "YES"],
            ["-parallel-testing-worker-count", "2"],
            ["-only-testing:DemoTests/testExample"],
            ["-skip-testing:DemoTests/testExample"],
            ["-only-testing:"],
            ["-skip-testing:"],
        )
        for extra in extras:
            with self.subTest(extra=extra), self.assertRaises(BuildHarborError):
                self.plan(self.arguments + extra)

    def test_terminal_control_and_bidi_characters_are_rejected(self):
        for value in ("Demo\x1b[2J", "Demo\tHidden", "Demo\x7f", "Demo\u202eHidden"):
            arguments = ["-project", "Demo.xcodeproj", "-scheme", value, "build"]
            with self.subTest(value=repr(value)), self.assertRaises(BuildHarborError):
                self.plan(arguments)

    def test_path_resolution_failures_are_reported_without_raw_runtime_errors(self):
        with patch.object(Path, "resolve", side_effect=RuntimeError("private symlink-loop details")):
            with self.assertRaises(BuildHarborError) as selected:
                parse_arguments(self.arguments, self.project)
        self.assertNotIn("private symlink-loop details", str(selected.exception))

        with patch.object(Path, "resolve", side_effect=RuntimeError("private config path")):
            with self.assertRaises(BuildHarborError) as configured:
                load_config(self.project)
        self.assertNotIn("private config path", str(configured.exception))

        with patch.object(Path, "resolve", side_effect=RuntimeError("private Xcode path")):
            with self.assertRaises(BuildHarborError) as xcode:
                inspect_xcode({"DEVELOPER_DIR": "/private/Xcode.app/Contents/Developer"})
        self.assertNotIn("private Xcode path", str(xcode.exception))

    def test_unsupported_actions_and_output_options_rejected(self):
        for arg in ("archive", "clean", "build-for-testing", "test-without-building", "-exportArchive", "-runFirstLaunch", "-resolvePackageDependencies", "-derivedDataPath", "-xcconfig", "@args.txt", "SYMROOT=/tmp/output", "-showBuildSettings", "-authenticationKeyPath"):
            with self.subTest(arg=arg), self.assertRaises(BuildHarborError):
                self.plan(self.arguments + [arg])

    def test_unknown_secret_arguments_are_not_reflected(self):
        with self.assertRaises(BuildHarborError) as caught:
            self.plan(self.arguments + ["API_TOKEN=private-value"])
        self.assertNotIn("private-value", str(caught.exception))

    def test_rejected_cli_run_never_spawns_a_child(self):
        argv = [
            "run", "--project-dir", str(self.project), "--",
            "-project", "Demo.xcodeproj", "-scheme", "Demo", "archive",
        ]
        with patch("buildharbor.cli.load_config", return_value=self.config), patch(
            "buildharbor.planner.inspect_xcode", side_effect=AssertionError("Rejected arguments must not inspect Xcode")
        ), patch(
            "buildharbor.planner.inspect_volume", side_effect=AssertionError("Rejected arguments must not inspect the volume")
        ), patch("subprocess.Popen") as process, patch("builtins.print"):
            self.assertEqual(main(argv), 2)
        process.assert_not_called()

    def test_inherited_conflicts_rejected(self):
        for name in ("SYMROOT", "XCODE_XCCONFIG_FILE", "CLANG_MODULE_CACHE_PATH", "TOOLCHAINS"):
            with self.subTest(name=name), self.assertRaises(BuildHarborError):
                self.plan(env={name: "conflict"})

    def test_plain_project_override_and_xcconfig_rejected(self):
        for text in ('{ SYMROOT = elsewhere; }', '{ "OBJROOT[sdk=macosx*]" = elsewhere; }', '{ baseConfigurationReference = ABC; }'):
            (self.identity / "project.pbxproj").write_text(text)
            with self.subTest(text=text), self.assertRaises(BuildHarborError):
                self.plan()

    def test_escaping_and_dangling_symlinks_rejected(self):
        for destination in (self.root, self.root / "does-not-exist"):
            self.config.storage_root.symlink_to(destination, target_is_directory=True)
            try:
                with self.assertRaises(BuildHarborError):
                    self.plan()
            finally:
                self.config.storage_root.unlink()

    def test_symlink_inside_storage_also_rejected(self):
        self.config.storage_root.mkdir()
        (self.config.storage_root / "actual").mkdir()
        (self.config.storage_root / "projects").symlink_to(self.config.storage_root / "actual", target_is_directory=True)
        with self.assertRaises(BuildHarborError):
            self.plan()

    def test_missing_components_allowed_without_creating(self):
        target = self.config.storage_root / "one/two/three"
        self.assertEqual(validate_destination(target, self.config, self.volume), target)
        self.assertFalse(self.config.storage_root.exists())

    def test_non_directory_parent_rejected(self):
        self.config.storage_root.write_text("file")
        with self.assertRaises(BuildHarborError):
            self.plan()

    def test_permission_metadata_failure(self):
        self.config.storage_root.mkdir()
        with patch("os.access", return_value=False), self.assertRaises(BuildHarborError):
            self.plan()

    def test_secure_creation_never_creates_missing_mount(self):
        missing = Config(self.project, "demo", self.root / "Missing", self.root / "Missing/Harbor", UUID)
        with self.assertRaises(BuildHarborError):
            open_directory(missing.storage_root, missing, self.volume, create=True)
        self.assertFalse(missing.mount.exists())

    def test_secure_creation_and_symlink_race(self):
        with patch.object(Path, "is_mount", return_value=True):
            fd = open_directory(self.config.storage_root / "new", self.config, self.volume, create=True)
            os.close(fd)
            (self.config.storage_root / "link").symlink_to(self.root, target_is_directory=True)
            with patch("buildharbor.paths.validate_destination"), self.assertRaises(BuildHarborError):
                open_directory(self.config.storage_root / "link/escaped", self.config, self.volume, create=True)
        self.assertFalse((self.root / "escaped").exists())

    def disk(self, changes=None):
        data = {"MountPoint": str(self.mount), "VolumeUUID": UUID, "FilesystemType": "apfs", "Internal": False, "WritableVolume": True, "Locked": False}
        data.update(changes or {})
        return subprocess.CompletedProcess([], 0, plistlib.dumps(data), b"")

    def test_wrong_uuid_readonly_internal_and_non_apfs_rejected(self):
        for changes in ({"VolumeUUID": "other"}, {"Locked": True}, {"WritableVolume": False}, {"Internal": True}, {"FilesystemType": "hfs"}, {"MountPoint": "/wrong"}):
            with self.subTest(changes=changes), patch("sys.platform", "darwin"), patch.object(Path, "is_mount", return_value=True), patch("subprocess.run", return_value=self.disk(changes)), self.assertRaises(BuildHarborError):
                inspect_volume(self.config)

    def test_missing_volume_never_calls_diskutil(self):
        with patch("sys.platform", "darwin"), patch.object(Path, "is_mount", return_value=False), patch("subprocess.run") as process, self.assertRaises(BuildHarborError):
            inspect_volume(self.config)
        process.assert_not_called()

    def test_diskutil_failure_is_explicit(self):
        with patch("sys.platform", "darwin"), patch.object(Path, "is_mount", return_value=True), patch("subprocess.run", side_effect=subprocess.TimeoutExpired("diskutil", 15)), self.assertRaises(BuildHarborError):
            inspect_volume(self.config)

    def test_verified_volume_and_capacity(self):
        with patch("sys.platform", "darwin"), patch.object(Path, "is_mount", return_value=True), patch("subprocess.run", return_value=self.disk()):
            actual = inspect_volume(self.config)
        self.assertEqual(actual.uuid, UUID)
        self.assertGreater(actual.available_bytes, 0)

    def test_configuration_separates_private_and_portable_fields(self):
        (self.project / "buildharbor.toml").write_text('schema_version = 1\nproject_id = "demo"\n')
        (self.project / ".buildharbor.local.toml").write_text(f'schema_version = 1\nmount = "/Volumes/Developer SSD"\nvolume_uuid = "{UUID}"\nstorage_root = "/Volumes/Developer SSD/Harbor"\n')
        self.assertEqual(load_config(self.project).project_id, "demo")
        with (self.project / "buildharbor.toml").open("a") as stream:
            stream.write('volume_uuid = "private"\n')
        with self.assertRaises(BuildHarborError):
            load_config(self.project)

    def test_xcode_selection_is_static_and_rejects_unverified_distribution(self):
        developer = self.root / "Xcode.app/Contents/Developer"
        (developer / "usr/bin").mkdir(parents=True)
        binary = developer / "usr/bin/xcodebuild"
        binary.write_text("never executed")
        binary.chmod(0o700)
        metadata = developer.parent / "version.plist"
        metadata.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "27.0", "ProductBuildVersion": "27A266a"}))
        with patch("subprocess.Popen", side_effect=AssertionError("Static inspection")):
            self.assertEqual(inspect_xcode({"DEVELOPER_DIR": str(developer)}).build, "27A266a")
        metadata.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "99.0", "ProductBuildVersion": "unknown"}))
        with self.assertRaises(BuildHarborError):
            inspect_xcode({"DEVELOPER_DIR": str(developer)})

        for malformed in (
            {"CFBundleShortVersionString": ["27.0"], "ProductBuildVersion": "27A266a"},
            {"CFBundleShortVersionString": "27.0", "ProductBuildVersion": {"value": "27A266a"}},
            {"CFBundleShortVersionString": "", "ProductBuildVersion": "27A266a"},
        ):
            metadata.write_bytes(plistlib.dumps(malformed))
            with self.subTest(malformed=malformed), self.assertRaises(BuildHarborError):
                inspect_xcode({"DEVELOPER_DIR": str(developer)})


if __name__ == "__main__":
    unittest.main()
