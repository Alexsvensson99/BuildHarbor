from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from buildharbor.config import Config
from buildharbor.settings import (
    FILE_PATH_SETTINGS,
    PATH_SETTINGS,
    SIGNING_ENV_SETTINGS,
    STATIC_ONLY_SETTINGS,
    SettingsError,
    capture_settings,
    inspect_effective_settings,
    validate_settings,
)
from buildharbor.volume import Volume


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="buildharbor-settings-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.mount = self.root / "External"
        self.mount.mkdir()
        self.storage = self.mount / "BuildHarbor"
        self.storage.mkdir()
        self.project = self.root / "Demo.xcodeproj"
        self.project.mkdir()
        self.config = Config(
            project_root=self.root,
            project_id="settings-fixture",
            mount=self.mount,
            storage_root=self.storage,
            volume_uuid="00000000-0000-0000-0000-000000000001",
            minimum_free_bytes=1,
        )
        self.volume = Volume(
            device_id=self.mount.stat().st_dev,
            available_bytes=1024**3,
            uuid=self.config.volume_uuid,
        )
        self.plan = SimpleNamespace(
            action="build",
            config=self.config,
            volume=self.volume,
            graph=SimpleNamespace(
                projects=(self.project,),
                targets={self.project: ("Demo",)},
            ),
            settings_command=(),
            member_settings_commands=(),
        )
        self.lock_fd = os.open(self.root / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, self.lock_fd)

    def record(self, *, project: Path | None = None, target: str = "Demo") -> dict[str, object]:
        base = self.storage / "projects" / "fixture"
        return {
            "target": target,
            "buildSettings": {
                "PROJECT_FILE_PATH": str(self.project if project is None else project),
                "SYMROOT": str(base / "Build" / "Products"),
                "OBJROOT": str(base / "Build" / "Intermediates.noindex"),
                "BUILD_DIR": str(base / "Build" / "Products"),
                "TARGET_BUILD_DIR": str(base / "Build" / "Products" / "Debug"),
                "TARGET_TEMP_DIR": str(base / "Build" / "Intermediates.noindex" / "Demo.build"),
            },
        }

    def archive_record(self) -> dict[str, object]:
        record = self.record()
        record["buildSettings"].update(
            {
                "PLATFORM_NAME": "macosx",
                "CODE_SIGNING_ALLOWED": "YES",
                "CODE_SIGN_IDENTITY": "-",
                "CODE_SIGN_STYLE": "Manual",
            }
        )
        return record

    def capture(self, code: str, *arguments: str, timeout: float = 2.0, max_bytes: int = 4096) -> bytes:
        command = (sys.executable, "-c", code, *arguments)
        return capture_settings(
            command,
            self.root,
            dict(os.environ),
            self.lock_fd,
            timeout=timeout,
            max_bytes=max_bytes,
        )

    def assert_reaped(self, pid_path: Path) -> None:
        pid = int(pid_path.read_text(encoding="utf-8"))
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_capture_returns_bounded_output(self) -> None:
        payload = self.capture("import sys; sys.stdout.buffer.write(b'harbor')", max_bytes=6)
        self.assertEqual(payload, b"harbor")

    def test_capture_rejects_oversized_output_and_reaps_child(self) -> None:
        pid_path = self.root / "oversized.pid"
        code = (
            "import os, pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
            "sys.stdout.buffer.write(b'x' * 4096); sys.stdout.flush(); time.sleep(30)"
        )

        with self.assertRaisesRegex(SettingsError, "exceeded the inspection size limit"):
            self.capture(code, str(pid_path), max_bytes=64)

        self.assert_reaped(pid_path)

    def test_capture_times_out_and_reaps_child(self) -> None:
        pid_path = self.root / "timeout.pid"
        code = (
            "import os, pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(30)"
        )
        started = time.monotonic()

        with self.assertRaisesRegex(SettingsError, "inspection timed out"):
            self.capture(code, str(pid_path), timeout=0.05)

        self.assertLess(time.monotonic() - started, 2.0)
        self.assert_reaped(pid_path)

    @unittest.skipUnless(
        threading.current_thread() is threading.main_thread(),
        "real signal forwarding requires the main thread",
    )
    def test_capture_forwards_signal_to_process_group_and_reaps_child(self) -> None:
        pid_path = self.root / "signal.pid"
        sender_error: list[str] = []
        code = (
            "import os, pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(30)"
        )

        def interrupt_when_ready() -> None:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if pid_path.exists():
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                time.sleep(0.005)
            sender_error.append("child did not become ready")

        sender = threading.Thread(target=interrupt_when_ready, daemon=True)
        original_handler = signal.getsignal(signal.SIGTERM)
        sender.start()
        with mock.patch("buildharbor.settings.os.killpg", wraps=os.killpg) as killpg:
            with self.assertRaises(SettingsError) as raised:
                self.capture(code, str(pid_path), timeout=3.0)
        sender.join(timeout=2.0)

        self.assertEqual(sender_error, [])
        self.assertFalse(sender.is_alive())
        self.assertEqual(raised.exception.exit_code, 128 + signal.SIGTERM)
        pid = int(pid_path.read_text(encoding="utf-8"))
        self.assertTrue(any(call.args == (pid, signal.SIGTERM) for call in killpg.call_args_list))
        self.assertEqual(signal.getsignal(signal.SIGTERM), original_handler)
        self.assert_reaped(pid_path)

    def test_validate_rejects_missing_invalid_or_empty_json(self) -> None:
        for payload, expected in (
            (b"", "invalid settings JSON"),
            (b"not-json", "invalid settings JSON"),
            (b"[]", "no inspectable targets"),
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(SettingsError, expected):
                    validate_settings(self.plan, payload)

    def test_validate_rejects_unknown_project_and_duplicate_target(self) -> None:
        outside = self.root / "Unknown.xcodeproj"
        unknown = json.dumps([self.record(project=outside)]).encode()
        duplicate = json.dumps([self.record(), self.record()]).encode()

        for payload in (unknown, duplicate):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(SettingsError, "unknown project or duplicate target"):
                    validate_settings(self.plan, payload)

    def test_validate_rejects_unresolved_and_escaped_outputs_with_source_diagnostic(self) -> None:
        unresolved = self.record()
        unresolved["buildSettings"]["SYMROOT"] = "$(SRCROOT)/Build"
        with self.assertRaises(SettingsError) as unresolved_error:
            validate_settings(self.plan, json.dumps([unresolved]).encode())
        self.assertIn("Effective SYMROOT", str(unresolved_error.exception))
        self.assertIn("source: Xcode target settings", str(unresolved_error.exception))

        escaped = self.record()
        escaped_path = self.root / "internal-output"
        escaped["buildSettings"]["OBJROOT"] = str(escaped_path)
        escaped["buildSettings"]["SECRET_TOKEN"] = "unrelated-secret-value"
        with self.assertRaises(SettingsError) as escaped_error:
            validate_settings(self.plan, json.dumps([escaped]).encode())
        diagnostic = str(escaped_error.exception)
        self.assertIn(f"Effective OBJROOT={escaped_path}", diagnostic)
        self.assertIn("source: Xcode target settings", diagnostic)
        self.assertIn("definition source unavailable", diagnostic)
        self.assertNotIn("SECRET_TOKEN", diagnostic)
        self.assertNotIn("unrelated-secret-value", diagnostic)

    def test_validate_rejects_secondary_directory_file_and_variant_output_escapes(self) -> None:
        for setting in (
            "OBJECT_FILE_DIR",
            "OBJECT_FILE_DIR_normal",
            "DERIVED_SOURCES_DIR",
            "SHARED_DERIVED_FILE_DIR",
            "TEMP_FILE_DIR",
            "UNINSTALLED_PRODUCTS_DIR",
            "GENERATED_MODULEMAP_DIR",
            "REZ_COLLECTOR_DIR",
            "FILE_LIST",
            "LINK_FILE_LIST_normal",
            "LD_MAP_FILE_PATH",
            "SWIFT_DEPENDENCY_INFO_FILE",
            "PROCESSED_INFOPLIST_PATH",
        ):
            with self.subTest(setting=setting):
                record = self.record()
                record["buildSettings"][setting] = str(self.root / "internal-output" / setting)
                with self.assertRaisesRegex(SettingsError, f"Effective {setting}="):
                    validate_settings(self.plan, json.dumps([record]).encode())

    def test_validate_never_reflects_terminal_controls_or_long_escaped_paths(self) -> None:
        controls = "\x7f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        for control in controls:
            with self.subTest(control=ord(control)):
                record = self.record()
                unsafe_value = str(self.root / "internal-output") + control + "SECRET"
                record["buildSettings"]["OBJECT_FILE_DIR"] = unsafe_value
                with self.assertRaises(SettingsError) as raised:
                    validate_settings(self.plan, json.dumps([record]).encode())
                diagnostic = str(raised.exception)
                self.assertNotIn(unsafe_value, diagnostic)
                self.assertNotIn("SECRET", diagnostic)

        record = self.record()
        long_value = "/" + "sensitive" * 50
        record["buildSettings"]["LD_MAP_FILE_PATH"] = long_value
        with self.assertRaises(SettingsError) as raised:
            validate_settings(self.plan, json.dumps([record]).encode())
        self.assertIn("Effective LD_MAP_FILE_PATH=[redacted]", str(raised.exception))
        self.assertNotIn(long_value, str(raised.exception))

    def test_validate_accepts_managed_paths_without_serializing_unrelated_secrets(self) -> None:
        record = self.record()
        record["buildSettings"].update(
            {
                "DSTROOT": str(self.storage / "projects" / "fixture" / "Install"),
                "SECRET_TOKEN": "never-serialize-this-value",
            }
        )

        result = validate_settings(self.plan, json.dumps([record]).encode())
        serialized = json.dumps(result, sort_keys=True)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["targets"], 1)
        self.assertGreaterEqual(result["path_settings_checked"], 6)
        self.assertNotIn("SECRET_TOKEN", serialized)
        self.assertNotIn("never-serialize-this-value", serialized)

    def test_archive_accepts_only_unsigned_or_ad_hoc_effective_signing(self) -> None:
        self.plan.action = "archive"
        result = validate_settings(self.plan, json.dumps([self.archive_record()]).encode())
        self.assertEqual(result["status"], "passed")

        unsafe = (
            ("EXPANDED_CODE_SIGN_IDENTITY", "A" * 40),
            ("EXPANDED_CODE_SIGN_IDENTITY_NAME", "Apple Development: Private"),
            ("CODE_SIGN_KEYCHAIN", "/Users/example/Library/Keychains/private.keychain-db"),
            ("OTHER_CODE_SIGN_FLAGS", "--keychain private.keychain-db"),
            ("SWIFT_STDLIB_TOOL_CODE_SIGN_IDENTITY", "B" * 40),
            ("SWIFT_STDLIB_TOOL_KEYCHAIN", "private.keychain-db"),
            ("SWIFT_STDLIB_TOOL_OTHER_CODE_SIGN_FLAGS", "--keychain private.keychain-db"),
        )
        for setting, value in unsafe:
            with self.subTest(setting=setting):
                record = self.archive_record()
                record["buildSettings"][setting] = value
                with self.assertRaises(SettingsError) as raised:
                    validate_settings(self.plan, json.dumps([record]).encode())
                self.assertNotIn(value, str(raised.exception))

        disabled = self.archive_record()
        disabled["buildSettings"].update(
            {"CODE_SIGNING_ALLOWED": "NO", "EXPANDED_CODE_SIGN_IDENTITY": "C" * 40}
        )
        with self.assertRaisesRegex(SettingsError, "effective archive signing identity"):
            validate_settings(self.plan, json.dumps([disabled]).encode())

        for setting in ("EXPANDED_CODE_SIGN_IDENTITY", "EXPANDED_CODE_SIGN_IDENTITY_NAME", "SWIFT_STDLIB_TOOL_CODE_SIGN_IDENTITY"):
            malformed = self.archive_record()
            malformed["buildSettings"][setting] = ["not a scalar"]
            with self.subTest(malformed=setting), self.assertRaises(SettingsError):
                validate_settings(self.plan, json.dumps([malformed]).encode())

    def test_exported_static_and_environment_setting_policies_cover_signing_bypasses(self) -> None:
        self.assertNotIn("CODE_SIGN_IDENTITY", STATIC_ONLY_SETTINGS)
        for setting in (
            "CODE_SIGN_KEYCHAIN",
            "OTHER_CODE_SIGN_FLAGS",
            "EXPANDED_CODE_SIGN_IDENTITY",
            "EXPANDED_CODE_SIGN_IDENTITY_NAME",
            "SWIFT_STDLIB_TOOL_CODE_SIGN_IDENTITY",
            "SWIFT_STDLIB_TOOL_KEYCHAIN",
            "SWIFT_STDLIB_TOOL_OTHER_CODE_SIGN_FLAGS",
        ):
            self.assertIn(setting, STATIC_ONLY_SETTINGS)
            self.assertIn(setting, SIGNING_ENV_SETTINGS)
        for setting in (
            "CODE_SIGN_IDENTITY",
            "CODE_SIGN_STYLE",
            "CODE_SIGNING_ALLOWED",
            "DEVELOPMENT_TEAM",
            "PROVISIONING_PROFILE_SPECIFIER",
        ):
            self.assertIn(setting, SIGNING_ENV_SETTINGS)
        self.assertIn("OBJECT_FILE_DIR", PATH_SETTINGS)
        self.assertIn("LD_MAP_FILE_PATH", FILE_PATH_SETTINGS)

    def test_validate_member_requires_exact_static_target_set(self) -> None:
        self.plan.graph.targets = {self.project: ("Demo", "Other")}

        with self.assertRaisesRegex(SettingsError, "exactly every statically inspected member target"):
            validate_settings(
                self.plan,
                json.dumps([self.record()]).encode(),
                expected_project=self.project,
            )

    def test_validate_member_ignores_only_byte_equivalent_duplicate_records(self) -> None:
        record = self.record()
        payload = json.dumps([record, record]).encode()

        result = validate_settings(self.plan, payload, expected_project=self.project)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["targets"], 1)
        self.assertEqual(result["identical_member_records_ignored"], 1)
        self.assertEqual(result["path_settings_checked"], 5)

    def test_validate_member_rejects_conflicting_duplicate_records(self) -> None:
        first = self.record()
        second = json.loads(json.dumps(first))
        second["buildSettings"]["SYMROOT"] = str(self.storage / "different-products")

        with self.assertRaisesRegex(SettingsError, "unknown project or duplicate target"):
            validate_settings(
                self.plan,
                json.dumps([first, second]).encode(),
                expected_project=self.project,
            )

    def test_inspect_effective_settings_checks_selected_action_and_member_queries(self) -> None:
        selected = ("selected",)
        member = ("member",)
        self.plan.settings_command = selected
        self.plan.member_settings_commands = (member,)
        payload = json.dumps([self.record()]).encode()

        with mock.patch("buildharbor.settings.capture_settings", side_effect=(payload, payload)) as capture:
            result = inspect_effective_settings(self.plan, self.lock_fd, dict(os.environ))

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["scope"], "all_static_projects_and_selected_scheme_action")
        self.assertEqual(result["member_projects"], 1)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(capture.call_args_list[0].args[0], selected)
        self.assertEqual(capture.call_args_list[1].args[0], member)

    def test_inspect_effective_settings_skips_absent_query(self) -> None:
        with mock.patch("buildharbor.settings.capture_settings") as capture:
            result = inspect_effective_settings(self.plan, self.lock_fd, dict(os.environ))

        self.assertEqual(result, {"status": "not_applicable"})
        capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
