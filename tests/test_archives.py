"""Archive/export provenance and managed-output tests without running Xcode."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from buildharbor.artifacts import (
    EXPORT_OPTIONS,
    EXPORT_OPTIONS_BYTES,
    archive_digest,
    inspect_archived_app,
    inspect_archive_input,
)
from buildharbor.config import Config
from buildharbor.errors import BuildHarborError
from buildharbor.executor import _make_receipt
from buildharbor.planner import make_plan
from buildharbor.volume import Volume
from buildharbor.xcode import Xcode


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="buildharbor-archive-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source_root = self.root / "Source"
        self.source_root.mkdir()
        self.identity = self.source_root / "Demo.xcodeproj"
        self.identity.mkdir()
        (self.identity / "project.pbxproj").write_text("{ objects = {}; }", encoding="utf-8")
        self.storage_root = self.root / "Storage"
        self.config = Config(
            project_root=self.source_root,
            project_id="demo",
            mount=self.root,
            storage_root=self.storage_root,
            volume_uuid="11111111-2222-3333-4444-555555555555",
            minimum_free_bytes=1,
        )
        self.volume = Volume(self.root.stat().st_dev, 10**12, self.config.volume_uuid)
        self.xcode = Xcode(
            developer_dir=self.root / "Xcode.app/Contents/Developer",
            version="27.0",
            build="27A266a",
            executable=self.root / "Xcode.app/Contents/Developer/usr/bin/xcodebuild",
        )

    @staticmethod
    def open_directory(path: Path, _config: Config, _volume: Volume, create: bool = False) -> int:
        if create:
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    @contextmanager
    def artifact_io(self):
        with (
            mock.patch("buildharbor.artifacts.validate_destination", side_effect=lambda path, *_: path),
            mock.patch("buildharbor.artifacts.open_directory", side_effect=self.open_directory),
        ):
            yield

    @contextmanager
    def planner_runtime(self):
        with (
            mock.patch("buildharbor.planner.inspect_volume", return_value=self.volume),
            mock.patch("buildharbor.planner.inspect_xcode", return_value=self.xcode),
            mock.patch("buildharbor.planner.validate_destination", side_effect=lambda path, *_: path),
            mock.patch(
                "buildharbor.projects.inspect_projects",
                return_value=SimpleNamespace(projects=(self.identity,), targets={}, fingerprints={}),
            ),
        ):
            yield

    def make_archive(self, run_id: str = "archive-run") -> tuple[Path, Path, dict[str, object]]:
        key = hashlib.sha256(str(self.identity).encode()).hexdigest()[:16]
        storage = self.storage_root / "projects" / f"demo-{key}" / f"xcode-{self.xcode.build}"
        archive = storage / "Archives" / f"{run_id}.xcarchive"
        archive.mkdir(parents=True)
        (archive / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "ApplicationProperties": {
                        "ApplicationPath": "Applications/HarborApp.app",
                        "CFBundleIdentifier": "org.example.HarborApp",
                    }
                }
            )
        )
        app = archive / "Products/Applications/HarborApp.app/Contents"
        app.mkdir(parents=True)
        (app / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleExecutable": "HarborApp",
                    "CFBundleIdentifier": "org.example.HarborApp",
                }
            )
        )
        (app / "MacOS").mkdir()
        (app / "MacOS/HarborApp").write_bytes(b"fixture executable\n")
        receipt_path = storage / "Receipts" / f"{run_id}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with self.artifact_io():
            fingerprint = archive_digest(archive, self.config, self.volume)
        receipt: dict[str, object] = {
            "schema_version": 1,
            "kind": "run_receipt",
            "action": "archive",
            "result": "succeeded",
            "exit_code": 0,
            "run_id": run_id,
            "project_id": self.config.project_id,
            "source_identity": str(self.identity),
            "xcode": {"version": self.xcode.version, "build": self.xcode.build},
            "planned_paths": {"archive": str(archive)},
            "archive_sha256": fingerprint,
            "archive_application": {
                "relative_path": "Applications/HarborApp.app",
                "bundle_identifier": "org.example.HarborApp",
                "executable": "HarborApp",
            },
        }
        receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
        return archive, receipt_path, receipt

    def write_receipt(self, path: Path, receipt: dict[str, object]) -> None:
        path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")

    def test_fixed_export_options_are_local_copy_app_only(self) -> None:
        self.assertEqual(EXPORT_OPTIONS, {"method": "mac-application", "destination": "export"})
        self.assertEqual(plistlib.loads(EXPORT_OPTIONS_BYTES), EXPORT_OPTIONS)
        text = EXPORT_OPTIONS_BYTES.decode("utf-8")
        for forbidden in (
            "teamID",
            "signingStyle",
            "signingCertificate",
            "installerSigningCertificate",
            "provisioningProfiles",
            "upload",
        ):
            self.assertNotIn(forbidden, text)

    def test_valid_managed_archive_and_receipt_are_bound_together(self) -> None:
        archive, receipt_path, receipt = self.make_archive()
        with self.artifact_io():
            storage, source, observed_receipt, fingerprint, receipt_hash = inspect_archive_input(
                archive, self.config, self.volume, self.xcode
            )
        self.assertEqual(storage, archive.parent.parent)
        self.assertEqual(source, self.identity)
        self.assertEqual(observed_receipt, receipt_path)
        self.assertEqual(fingerprint, receipt["archive_sha256"])
        self.assertEqual(receipt_hash, hashlib.sha256(receipt_path.read_bytes()).hexdigest())

    def test_archived_application_structure_is_inspected(self) -> None:
        archive, _receipt_path, receipt = self.make_archive()
        with self.artifact_io():
            application = inspect_archived_app(archive, self.config, self.volume)
        self.assertEqual(application, receipt["archive_application"])

    def test_empty_malformed_non_app_and_multiple_app_archives_are_rejected(self) -> None:
        cases = ("empty", "malformed", "non-app", "multiple-apps")
        for case in cases:
            with self.subTest(case=case):
                archive, _receipt_path, _receipt = self.make_archive(f"invalid-{case}")
                if case == "empty":
                    for path in sorted(archive.rglob("*"), reverse=True):
                        path.unlink() if path.is_file() else path.rmdir()
                elif case == "malformed":
                    (archive / "Info.plist").write_bytes(b"not a property list")
                elif case == "non-app":
                    (archive / "Info.plist").write_bytes(
                        plistlib.dumps(
                            {
                                "ApplicationProperties": {
                                    "ApplicationPath": "Libraries/HarborApp.app",
                                    "CFBundleIdentifier": "org.example.HarborApp",
                                }
                            }
                        )
                    )
                else:
                    second = archive / "Products/Applications/Other.app/Contents"
                    (second / "MacOS").mkdir(parents=True)
                    (second / "Info.plist").write_bytes(
                        plistlib.dumps(
                            {
                                "CFBundleExecutable": "Other",
                                "CFBundleIdentifier": "org.example.Other",
                            }
                        )
                    )
                    (second / "MacOS/Other").write_bytes(b"other executable\n")
                with self.artifact_io(), self.assertRaises(BuildHarborError):
                    inspect_archived_app(archive, self.config, self.volume)

    def test_success_receipt_rejects_an_empty_archive(self) -> None:
        archive = self.storage_root / "empty.xcarchive"
        archive.mkdir(parents=True)
        receipt = self.storage_root / "Receipts/empty.json"
        receipt.parent.mkdir(parents=True)
        plan = SimpleNamespace(
            action="archive",
            config=self.config,
            inputs={},
            outputs={"archive": archive, "receipt": receipt},
            run_id="empty",
            source_identity=self.identity,
            xcode=self.xcode,
        )
        with (
            mock.patch("buildharbor.executor.open_directory", side_effect=self.open_directory),
            self.assertRaises(BuildHarborError),
        ):
            _make_receipt(
                plan,
                self.volume,
                result="succeeded",
                exit_code=0,
                termination={"kind": "exit", "code": 0},
            )

    def test_foreign_failed_and_tampered_receipts_are_rejected(self) -> None:
        cases = {
            "foreign project": {"project_id": "other"},
            "foreign source": {"source_identity": str(self.root / "Elsewhere/Demo.xcodeproj")},
            "failed action": {"result": "failed", "exit_code": 65},
            "wrong action": {"action": "build"},
            "wrong Xcode": {"xcode": {"version": "27.0", "build": "other"}},
            "wrong archive": {"planned_paths": {"archive": "/managed/elsewhere.xcarchive"}},
            "tampered digest": {"archive_sha256": "0" * 64},
            "missing application metadata": {"archive_application": None},
            "wrong application metadata": {
                "archive_application": {
                    "relative_path": "Applications/Other.app",
                    "bundle_identifier": "org.example.Other",
                    "executable": "Other",
                }
            },
        }
        for label, changes in cases.items():
            with self.subTest(label=label):
                archive, receipt_path, receipt = self.make_archive(label.replace(" ", "-"))
                receipt.update(changes)
                self.write_receipt(receipt_path, receipt)
                with self.artifact_io(), self.assertRaises(BuildHarborError):
                    inspect_archive_input(archive, self.config, self.volume, self.xcode)

    def test_archive_mutation_after_receipt_is_rejected(self) -> None:
        archive, _receipt_path, _receipt = self.make_archive()
        executable = archive / "Products/Applications/HarborApp.app/Contents/MacOS/HarborApp"
        executable.write_bytes(b"tampered after receipt\n")
        with self.artifact_io(), self.assertRaisesRegex(BuildHarborError, "differs"):
            inspect_archive_input(archive, self.config, self.volume, self.xcode)

    def test_archive_digest_rejects_symlinks_and_hardlinks(self) -> None:
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                archive = self.root / f"{kind}.xcarchive"
                archive.mkdir()
                original = archive / "original"
                original.write_bytes(b"content")
                if kind == "symlink":
                    (archive / "alias").symlink_to(original)
                else:
                    os.link(original, archive / "alias")
                with self.artifact_io(), self.assertRaises(BuildHarborError):
                    archive_digest(archive, self.config, self.volume)

    def test_receipt_symlinks_and_hardlinks_are_rejected(self) -> None:
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                archive, receipt_path, _receipt = self.make_archive(f"receipt-{kind}")
                original = receipt_path.with_suffix(".original.json")
                receipt_path.replace(original)
                if kind == "symlink":
                    receipt_path.symlink_to(original)
                else:
                    os.link(original, receipt_path)
                with self.artifact_io(), self.assertRaises(BuildHarborError):
                    inspect_archive_input(archive, self.config, self.volume, self.xcode)

    def test_archive_plan_uses_managed_unique_absent_outputs(self) -> None:
        arguments = ["-project", "Demo.xcodeproj", "-scheme", "Demo", "archive"]
        with self.planner_runtime():
            first = make_plan(self.config, arguments, {})
            second = make_plan(self.config, arguments, {})
        self.assertEqual(first.action, "archive")
        self.assertIn("archive", first.outputs)
        self.assertEqual(first.outputs["archive"].suffix, ".xcarchive")
        self.assertEqual(first.outputs["archive"].parent.name, "Archives")
        self.assertFalse(first.outputs["archive"].exists())
        self.assertIn("archive", first.unique_outputs)
        archive_flag = first.command.index("-archivePath")
        self.assertEqual(first.command[archive_flag + 1], str(first.outputs["archive"]))
        for name in ("archive", "receipt", "result_bundle", "settings_result_bundle"):
            self.assertNotEqual(first.outputs[name], second.outputs[name])
        for name in ("derived_data", "package_cache", "module_cache"):
            self.assertEqual(first.outputs[name], second.outputs[name])

    def test_archive_plan_never_overwrites_a_unique_output(self) -> None:
        arguments = ["-project", "Demo.xcodeproj", "-scheme", "Demo", "archive"]
        fixed_time = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)
        fake_uuid = SimpleNamespace(hex="a" * 32)
        with (
            self.planner_runtime(),
            mock.patch("buildharbor.planner.datetime") as clock,
            mock.patch("buildharbor.planner.uuid.uuid4", return_value=fake_uuid),
        ):
            clock.now.return_value = fixed_time
            first = make_plan(self.config, arguments, {})
            first.outputs["archive"].mkdir(parents=True)
            with self.assertRaisesRegex(BuildHarborError, "unique run output"):
                make_plan(self.config, arguments, {})

    def test_export_plan_uses_only_managed_unique_outputs_and_bound_inputs(self) -> None:
        archive, receipt_path, _receipt = self.make_archive()
        storage = archive.parent.parent
        arguments = ["-exportArchive", "-archivePath", str(archive)]
        inspected = (storage, self.identity, receipt_path, "a" * 64, "b" * 64)
        with (
            self.planner_runtime(),
            mock.patch("buildharbor.artifacts.inspect_archive_input", return_value=inspected),
        ):
            first = make_plan(self.config, arguments, {})
            second = make_plan(self.config, arguments, {})

        self.assertEqual(first.action, "export")
        self.assertEqual(first.project_storage, storage)
        self.assertEqual(first.source_identity, self.identity)
        self.assertEqual(first.inputs, {"archive": archive, "archive_receipt": receipt_path})
        self.assertEqual(first.input_hashes, {"archive": "a" * 64, "archive_receipt": "b" * 64})
        self.assertEqual(first.unique_outputs, ("export", "export_options", "receipt"))
        for name in first.unique_outputs:
            self.assertFalse(first.outputs[name].exists())
            self.assertTrue(first.outputs[name].is_relative_to(storage))
            self.assertNotEqual(first.outputs[name], second.outputs[name])
        self.assertEqual(first.outputs["temporary"], second.outputs["temporary"])
        self.assertEqual(
            first.command,
            (
                str(self.xcode.executable),
                "-exportArchive",
                "-archivePath",
                str(archive),
                "-exportPath",
                str(first.outputs["export"]),
                "-exportOptionsPlist",
                str(first.outputs["export_options"]),
            ),
        )
        self.assertNotIn(str(EXPORT_OPTIONS_BYTES), first.command)

    def test_export_plan_rejects_caller_controlled_paths_and_upload_flags(self) -> None:
        archive, _receipt_path, _receipt = self.make_archive()
        extras = (
            ["-exportPath", str(self.root / "Elsewhere")],
            ["-exportOptionsPlist", str(self.root / "options.plist")],
            ["-allowProvisioningUpdates"],
            ["-authenticationKeyPath", str(self.root / "private-key.p8")],
            ["-uploadBitcode"],
        )
        for extra in extras:
            with self.subTest(extra=extra), self.planner_runtime(), self.assertRaises(BuildHarborError):
                make_plan(
                    self.config,
                    ["-exportArchive", "-archivePath", str(archive), *extra],
                    {},
                )

    def test_export_plan_never_overwrites_and_new_plans_get_new_run_ids(self) -> None:
        archive, receipt_path, _receipt = self.make_archive()
        storage = archive.parent.parent
        arguments = ["-exportArchive", "-archivePath", str(archive)]
        inspected = (storage, self.identity, receipt_path, "a" * 64, "b" * 64)
        with (
            self.planner_runtime(),
            mock.patch("buildharbor.artifacts.inspect_archive_input", return_value=inspected),
        ):
            first = make_plan(self.config, arguments, {})
            second = make_plan(self.config, arguments, {})
        self.assertNotEqual(first.run_id, second.run_id)

        fixed_time = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)
        fake_uuid = SimpleNamespace(hex="e" * 32)
        with (
            self.planner_runtime(),
            mock.patch("buildharbor.artifacts.inspect_archive_input", return_value=inspected),
            mock.patch("buildharbor.planner.datetime") as clock,
            mock.patch("buildharbor.planner.uuid.uuid4", return_value=fake_uuid),
        ):
            clock.now.return_value = fixed_time
            collision = make_plan(self.config, arguments, {})
            collision.outputs["export"].mkdir(parents=True)
            with self.assertRaisesRegex(BuildHarborError, "output already exists"):
                make_plan(self.config, arguments, {})


if __name__ == "__main__":
    unittest.main()
