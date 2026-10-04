"""Focused read-only storage accounting tests."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from buildharbor import __version__
from buildharbor.config import Config
from buildharbor.errors import BuildHarborError
from buildharbor.storage import render_storage, scan_storage
from buildharbor.volume import Volume


UUID = "11111111-2222-3333-4444-555555555555"
KEY_A = "0123456789abcdef"
KEY_B = "fedcba9876543210"


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="buildharbor-storage-test-")
        self.addCleanup(self.temporary.cleanup)
        self.mount = Path(self.temporary.name).resolve()
        self.storage = self.mount / "Storage"
        self.project_a = self.mount / "Sources/A"
        self.project_b = self.mount / "Sources/B"
        self.project_a.mkdir(parents=True)
        self.project_b.mkdir(parents=True)
        self.config_a = self.config(self.project_a, "alpha")
        self.config_b = self.config(self.project_b, "beta")
        self.volume = Volume(self.mount.stat().st_dev, 10**12, UUID)

    def config(self, root: Path, project_id: str, *, storage: Path | None = None) -> Config:
        return Config(
            project_root=root,
            project_id=project_id,
            mount=self.mount,
            storage_root=self.storage if storage is None else storage,
            volume_uuid=UUID,
            minimum_free_bytes=1,
        )

    @contextmanager
    def runtime(self, side_effect: object | None = None):
        if side_effect is None:
            patcher = mock.patch("buildharbor.storage.inspect_volume", return_value=self.volume)
        else:
            patcher = mock.patch("buildharbor.storage.inspect_volume", side_effect=side_effect)
        with patcher as inspect:
            yield inspect

    def project_directory(self, project_id: str, key: str = KEY_A) -> Path:
        path = self.storage / "projects" / f"{project_id}-{key}" / "xcode-27A266a"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def write(path: Path, data: bytes) -> os.stat_result:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path.stat()

    def assert_reconciles(self, report: dict[str, object]) -> None:
        categories = [*report["projects"].values(), report["shared"], report["unattributed"]]
        for field in ("logical_bytes", "allocated_bytes", "regular_files"):
            self.assertEqual(report["totals"][field], sum(item[field] for item in categories))

    def test_regular_files_are_attributed_and_totals_reconcile(self) -> None:
        known_info = self.write(self.project_directory("alpha") / "known.bin", b"known")
        unknown_info = self.write(self.storage / "Evidence/unknown.bin", b"unknown-data")

        with self.runtime() as inspect:
            report = scan_storage([self.config_a, self.config_b])

        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["tool_version"], __version__)
        self.assertFalse(report["is_lower_bound"])
        self.assertEqual(report["projects"]["alpha"]["logical_bytes"], known_info.st_size)
        self.assertEqual(report["projects"]["alpha"]["allocated_bytes"], known_info.st_blocks * 512)
        self.assertEqual(report["projects"]["beta"], {"logical_bytes": 0, "allocated_bytes": 0, "regular_files": 0})
        self.assertEqual(report["unattributed"]["logical_bytes"], unknown_info.st_size)
        self.assertEqual(report["unattributed"]["allocated_bytes"], unknown_info.st_blocks * 512)
        self.assert_reconciles(report)
        self.assertEqual(inspect.call_count, 2)
        self.assertTrue(all(call.kwargs == {"read_only": True} for call in inspect.call_args_list))

    def test_hardlinks_are_counted_once_as_project_shared_or_unattributed(self) -> None:
        alpha = self.project_directory("alpha")
        beta = self.project_directory("beta", KEY_B)

        same = alpha / "same-one"
        same.write_bytes(b"same-project")
        os.link(same, alpha / "same-two")

        shared = alpha / "shared"
        shared.write_bytes(b"shared-projects")
        os.link(shared, beta / "shared")

        unknown = alpha / "unknown-link"
        unknown.write_bytes(b"known-and-unknown")
        unknown_parent = self.storage / "Other"
        unknown_parent.mkdir()
        os.link(unknown, unknown_parent / "unknown-link")

        outside = alpha / "outside-link"
        outside.write_bytes(b"outside")
        os.link(outside, self.mount / "outside-storage-link")

        with self.runtime():
            report = scan_storage([self.config_a, self.config_b])

        self.assertEqual(report["projects"]["alpha"]["regular_files"], 1)
        self.assertEqual(report["shared"]["regular_files"], 1)
        self.assertEqual(report["unattributed"]["regular_files"], 2)
        self.assertEqual(report["totals"]["regular_files"], 4)
        self.assert_reconciles(report)

    def test_duplicate_project_identifier_from_different_roots_is_ambiguous(self) -> None:
        duplicate = self.config(self.project_b, "alpha")
        self.write(self.project_directory("alpha") / "ambiguous", b"ambiguous")
        with self.runtime():
            report = scan_storage([self.config_a, duplicate])
        self.assertEqual(report["ambiguous_project_ids"], ["alpha"])
        self.assertEqual(report["projects"], {})
        self.assertEqual(report["unattributed"]["regular_files"], 1)

    def test_repeated_same_project_root_is_deduplicated(self) -> None:
        self.write(self.project_directory("alpha") / "known", b"known")
        with self.runtime():
            report = scan_storage([self.config_a, self.config_a])
        self.assertEqual(report["ambiguous_project_ids"], [])
        self.assertEqual(report["projects"]["alpha"]["regular_files"], 1)

    def test_malformed_and_unknown_project_directories_are_unattributed(self) -> None:
        names = (
            f"unknown-{KEY_A}",
            "alpha-0123456789abcde",
            "alpha-0123456789abcdef0",
            "alpha-0123456789ABCDEf",
            f"alpha-{KEY_A}-extra",
        )
        for index, name in enumerate(names):
            self.write(self.storage / "projects" / name / f"file-{index}", b"x")
        with self.runtime():
            report = scan_storage([self.config_a])
        self.assertEqual(report["projects"]["alpha"]["regular_files"], 0)
        self.assertEqual(report["unattributed"]["regular_files"], len(names))

    def test_symlinks_are_never_followed_and_special_entries_are_only_counted(self) -> None:
        target = self.mount / "large-outside-target"
        target.write_bytes(b"outside" * 100)
        self.storage.mkdir()
        (self.storage / "file-link").symlink_to(target)
        (self.storage / "directory-link").symlink_to(self.project_a, target_is_directory=True)
        fifo = self.storage / "named-pipe"
        os.mkfifo(fifo)

        with self.runtime():
            report = scan_storage([self.config_a])

        self.assertEqual(report["ignored"], {"symlinks": 2, "special": 1})
        self.assertEqual(report["totals"]["regular_files"], 0)
        self.assertNotIn(target.name, json.dumps(report, sort_keys=True))

    def test_missing_storage_root_is_complete_absent_and_is_not_created(self) -> None:
        self.assertFalse(self.storage.exists())
        with self.runtime():
            report = scan_storage([self.config_a])
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["storage_root_state"], "absent")
        self.assertEqual(report["totals"]["regular_files"], 0)
        self.assertFalse(self.storage.exists())

    def test_preflight_and_end_volume_failures_are_incomplete(self) -> None:
        with self.subTest(stage="preflight"), self.runtime(BuildHarborError("private detail")):
            report = scan_storage([self.config_a])
        self.assertEqual(report["storage_root_state"], "unavailable")
        self.assertEqual(report["issues"], [{"code": "volume_preflight_failed", "count": 1}])
        self.assertTrue(report["is_lower_bound"])
        self.assertNotIn("private detail", json.dumps(report))

        self.write(self.project_directory("alpha") / "observed", b"observed")
        with self.subTest(stage="revalidation"), self.runtime([self.volume, BuildHarborError("gone")]):
            report = scan_storage([self.config_a])
        self.assertEqual(report["totals"]["regular_files"], 1)
        self.assertIn("volume_revalidation_failed", {item["code"] for item in report["issues"]})
        self.assertTrue(report["is_lower_bound"])

    def test_initial_root_fstat_failure_is_structured_and_closes_both_descriptors(self) -> None:
        self.storage.mkdir()
        real_fstat = os.fstat
        real_close = os.close
        calls = 0
        opened: dict[str, int] = {}
        closed: list[int] = []

        def failing_fstat(fd: int):
            nonlocal calls
            calls += 1
            if calls == 1:
                opened["mount"] = fd
            elif calls == 2:
                opened["root"] = fd
            elif calls == 3:
                self.assertEqual(fd, opened["root"])
                raise OSError("initial root inspection failed")
            return real_fstat(fd)

        def tracking_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        with (
            self.runtime(),
            mock.patch("buildharbor.storage.os.fstat", side_effect=failing_fstat),
            mock.patch("buildharbor.storage.os.close", side_effect=tracking_close),
        ):
            report = scan_storage([self.config_a])

        self.assertEqual(report["status"], "incomplete")
        self.assertTrue(report["is_lower_bound"])
        self.assertEqual(report["storage_root_state"], "unavailable")
        self.assertEqual(report["totals"], {"logical_bytes": 0, "allocated_bytes": 0, "regular_files": 0})
        self.assertEqual(report["issues"], [{"code": "storage_root_unreadable", "count": 1}])
        self.assertIn(opened["root"], closed)
        self.assertIn(opened["mount"], closed)

    def test_permission_failure_preserves_other_observed_lower_bounds(self) -> None:
        allowed = self.project_directory("alpha") / "allowed"
        self.write(allowed, b"allowed")
        denied = self.storage / "Denied"
        denied.mkdir()
        self.write(denied / "secret-name", b"secret")
        denied_inode = denied.stat().st_ino
        real_scandir = os.scandir

        def guarded_scandir(fd):
            if os.fstat(fd).st_ino == denied_inode:
                raise PermissionError("secret-name")
            return real_scandir(fd)

        with self.runtime(), mock.patch("buildharbor.storage.os.scandir", side_effect=guarded_scandir):
            report = scan_storage([self.config_a])

        self.assertEqual(report["projects"]["alpha"]["regular_files"], 1)
        self.assertTrue(report["is_lower_bound"])
        self.assertIn("directory_unreadable", {item["code"] for item in report["issues"]})
        self.assertNotIn("secret-name", json.dumps(report))

    def test_entry_inode_depth_and_time_bounds_return_incomplete_reports(self) -> None:
        for index in range(4):
            self.write(self.storage / f"top-{index}", b"x")
        self.write(self.storage / "nested/file", b"nested")
        cases = (
            ("entry", {"max_entries": 1}, "entry_limit_reached"),
            ("inode", {"max_inodes": 1}, "inode_limit_reached"),
            ("depth", {"max_depth": 0}, "depth_limit_reached"),
        )
        for label, arguments, issue in cases:
            with self.subTest(label=label), self.runtime():
                report = scan_storage([self.config_a], **arguments)
            self.assertTrue(report["is_lower_bound"])
            self.assertIn(issue, {item["code"] for item in report["issues"]})
            self.assert_reconciles(report)

        with (
            self.subTest(label="time"),
            self.runtime(),
            mock.patch("buildharbor.storage.time.monotonic", side_effect=[0.0, 0.0, 31.0]),
        ):
            report = scan_storage([self.config_a], timeout=30.0)
        self.assertEqual(report["totals"]["regular_files"], 0)
        self.assertIn("time_limit_reached", {item["code"] for item in report["issues"]})

    def test_scan_does_not_mutate_contents_mode_mtime_or_ctime(self) -> None:
        managed = self.project_directory("alpha") / "artifact"
        self.write(managed, b"artifact")
        before = {
            path.relative_to(self.storage): (
                path.lstat().st_mode,
                path.lstat().st_size,
                path.lstat().st_mtime_ns,
                path.lstat().st_ctime_ns,
            )
            for path in [self.storage, *self.storage.rglob("*")]
        }
        real_open = os.open

        def read_only_open(path, flags, *args, **kwargs):
            forbidden = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC
            self.assertEqual(flags & forbidden, 0)
            return real_open(path, flags, *args, **kwargs)

        with (
            self.runtime(),
            mock.patch("buildharbor.storage.os.open", side_effect=read_only_open),
            mock.patch("buildharbor.storage.os.mkdir", side_effect=AssertionError("write attempted")),
            mock.patch("buildharbor.storage.os.unlink", side_effect=AssertionError("write attempted")),
            mock.patch("buildharbor.storage.os.rename", side_effect=AssertionError("write attempted")),
        ):
            report = scan_storage([self.config_a])

        after = {
            path.relative_to(self.storage): (
                path.lstat().st_mode,
                path.lstat().st_size,
                path.lstat().st_mtime_ns,
                path.lstat().st_ctime_ns,
            )
            for path in [self.storage, *self.storage.rglob("*")]
        }
        self.assertEqual(report["status"], "complete")
        self.assertEqual(after, before)

    def test_configuration_conflicts_and_invalid_bounds_are_rejected(self) -> None:
        other_storage = self.mount / "OtherStorage"
        conflict = self.config(self.project_b, "beta", storage=other_storage)
        with self.assertRaisesRegex(BuildHarborError, "share one exact storage root"):
            scan_storage([self.config_a, conflict])
        with self.assertRaises(BuildHarborError):
            scan_storage([])
        for arguments in (
            {"max_entries": 0},
            {"max_entries": 200_001},
            {"max_inodes": 0},
            {"max_inodes": 100_001},
            {"max_depth": -1},
            {"max_depth": 65},
            {"timeout": 0},
            {"timeout": 30.000_001},
            {"timeout": float("nan")},
            {"timeout": float("inf")},
            {"timeout": float("-inf")},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(BuildHarborError):
                scan_storage([self.config_a], **arguments)

    def test_renderer_labels_lower_bounds_and_avoids_physical_usage_claims(self) -> None:
        with self.runtime(BuildHarborError("offline")):
            report = scan_storage([self.config_a])
        rendered = render_storage(report)
        self.assertIn("incomplete", rendered)
        self.assertIn("observed lower bounds", rendered)
        self.assertIn("not APFS physical usage or reclaimable capacity", rendered)
        self.assertNotIn(str(self.storage), rendered)


if __name__ == "__main__":
    unittest.main()
