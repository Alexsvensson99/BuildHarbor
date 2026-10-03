from __future__ import annotations

from contextlib import contextmanager, redirect_stderr
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from buildharbor.cli import main
from buildharbor.config import Config
from buildharbor.errors import BuildHarborError
from buildharbor.executor import execute
from buildharbor.planner import Plan
from buildharbor.volume import Volume
from buildharbor.xcode import Xcode


class ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.storage = self.root / "storage"
        self.project_storage = self.storage / "project"
        self.config = Config(
            project_root=self.root,
            project_id="fixture",
            mount=self.root,
            storage_root=self.storage,
            volume_uuid="00000000-0000-0000-0000-000000000001",
            minimum_free_bytes=1,
        )
        self.volume = Volume(
            device_id=self.root.stat().st_dev,
            available_bytes=1024**3,
            uuid=self.config.volume_uuid,
        )
        self.xcode = Xcode(
            developer_dir=self.root / "Developer",
            version="27.0",
            build="27A266a",
            executable=Path(sys.executable),
        )

    def make_plan(self, code: str = "raise SystemExit(0)", run_id: str = "run-one") -> Plan:
        outputs = {
            "derived_data": self.project_storage / "DerivedData",
            "package_cache": self.project_storage / "PackageCache",
            "receipt": self.project_storage / "Receipts" / f"{run_id}.json",
        }
        directories = (
            outputs["derived_data"],
            outputs["package_cache"],
            outputs["receipt"].parent,
        )
        return Plan(
            config=self.config,
            volume=self.volume,
            xcode=self.xcode,
            action="build",
            command=(sys.executable, "-c", code, str(outputs["derived_data"])),
            environment={"BUILDHARBOR_EXECUTOR_TEST": "1"},
            outputs=outputs,
            directories=directories,
            run_id=run_id,
            project_storage=self.project_storage,
        )

    @staticmethod
    def fake_open_directory(path: Path, _config: Config, _volume: Volume, create: bool = False) -> int:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY)

    @contextmanager
    def runtime(self, *, volumes: object | None = None):
        volume_value = self.volume if volumes is None else volumes
        volume_patch = (
            mock.patch("buildharbor.executor.inspect_volume", side_effect=volume_value)
            if isinstance(volume_value, (list, BaseException))
            else mock.patch("buildharbor.executor.inspect_volume", return_value=volume_value)
        )
        with (
            volume_patch,
            mock.patch("buildharbor.executor.validate_destination", side_effect=lambda path, *_: path),
            mock.patch("buildharbor.executor.open_directory", side_effect=self.fake_open_directory),
            mock.patch("buildharbor.executor.inspect_xcode", return_value=self.xcode),
        ):
            yield

    def test_exit_code_and_receipt_report_only_bounded_observations(self) -> None:
        code = (
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1], 'artifact').write_text('built', encoding='utf-8'); "
            "raise SystemExit(7)"
        )
        plan = self.make_plan(code)

        with self.runtime():
            result = execute(plan)

        self.assertEqual(result, 7)
        receipt = json.loads(plan.outputs["receipt"].read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["kind"], "run_receipt")
        self.assertEqual(receipt["result"], "failed")
        self.assertEqual(receipt["exit_code"], 7)
        self.assertEqual(receipt["termination"], {"kind": "exit", "code": 7})
        self.assertEqual(receipt["run_id"], plan.run_id)
        self.assertEqual(receipt["xcode"], {"version": "27.0", "build": "27A266a"})
        self.assertEqual(receipt["observed_paths"]["derived_data"]["state"], "directory_with_contents")
        self.assertEqual(receipt["observed_paths"]["package_cache"]["state"], "empty_directory")
        self.assertNotIn("command", receipt)
        self.assertNotIn("environment", receipt)
        self.assertTrue((self.project_storage / ".buildharbor.lock").is_file())
        self.assertEqual(list(self.project_storage.glob(".buildharbor-probe-*")), [])

    def test_changed_volume_before_launch_never_spawns(self) -> None:
        plan = self.make_plan()
        changed = replace(self.volume, device_id=self.volume.device_id + 1)
        with (
            self.runtime(volumes=[self.volume, changed, changed]),
            mock.patch("buildharbor.executor.subprocess.Popen") as popen,
        ):
            self.assertEqual(execute(plan), 2)
        popen.assert_not_called()
        self.assertFalse(plan.outputs["receipt"].exists())

    def test_concurrent_run_is_rejected_by_persistent_lock(self) -> None:
        started = self.root / "child-started"
        code = (
            "from pathlib import Path; import sys, time; "
            f"Path({str(started)!r}).write_text('ready', encoding='utf-8'); "
            "time.sleep(0.7)"
        )
        first = self.make_plan(code, "first")
        second = self.make_plan("raise SystemExit(0)", "second")
        first_result: list[int] = []
        first_error: list[BaseException] = []

        def run_first() -> None:
            try:
                first_result.append(execute(first))
            except BaseException as exc:  # Preserve the worker failure for the assertion.
                first_error.append(exc)

        with self.runtime():
            worker = threading.Thread(target=run_first)
            worker.start()
            deadline = time.monotonic() + 5
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(started.exists(), "the first child did not start")
            self.assertEqual(execute(second), 2)
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(first_error, [])
        self.assertEqual(first_result, [0])

    def test_signals_are_forwarded_to_child_group_and_child_is_reaped(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=signum):
                pid_file = self.root / f"child-{signum}.pid"
                code = (
                    "from pathlib import Path; import os, signal, time; "
                    "signal.signal(signal.SIGINT, signal.SIG_DFL); "
                    f"Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
                    "time.sleep(30)"
                )
                plan = self.make_plan(code, f"signal-{signum}")

                def interrupt_when_ready() -> None:
                    deadline = time.monotonic() + 5
                    while not pid_file.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if pid_file.exists():
                        os.kill(os.getpid(), signum)

                interrupter = threading.Thread(target=interrupt_when_ready, daemon=True)
                interrupter.start()
                with self.runtime():
                    result = execute(plan)
                interrupter.join(timeout=2)

                self.assertEqual(result, 128 + signum)
                child_pid = int(pid_file.read_text(encoding="utf-8"))
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
                receipt = json.loads(plan.outputs["receipt"].read_text(encoding="utf-8"))
                self.assertEqual(receipt["termination"], {"kind": "signal", "signal": signum})

    def test_receipt_failure_does_not_mask_failed_child(self) -> None:
        for child_code, expected in (("raise SystemExit(0)", 2), ("raise SystemExit(9)", 9)):
            with self.subTest(expected=expected):
                plan = self.make_plan(child_code, f"receipt-failure-{expected}")
                diagnostics = io.StringIO()
                with (
                    self.runtime(),
                    mock.patch(
                        "buildharbor.executor._write_receipt",
                        side_effect=PermissionError("permission denied"),
                    ),
                    redirect_stderr(diagnostics),
                ):
                    result = execute(plan)
                self.assertEqual(result, expected)
                self.assertIn("could not record the run receipt", diagnostics.getvalue())

    def test_launch_uses_inherited_streams_session_and_lock_fd(self) -> None:
        plan = self.make_plan()
        child = mock.Mock(pid=12345)
        child.poll.return_value = 0
        child.wait.return_value = 0

        with (
            self.runtime(),
            mock.patch("buildharbor.executor.subprocess.Popen", return_value=child) as popen,
        ):
            self.assertEqual(execute(plan), 0)

        _args, kwargs = popen.call_args
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(len(kwargs["pass_fds"]), 1)
        self.assertEqual(kwargs["env"]["BUILDHARBOR_EXECUTOR_TEST"], "1")
        self.assertNotIn("stdout", kwargs)
        self.assertNotIn("stderr", kwargs)

    def test_ordinary_exit_143_is_not_reported_as_a_signal(self) -> None:
        plan = self.make_plan("raise SystemExit(143)")
        with self.runtime():
            self.assertEqual(execute(plan), 143)
        receipt = json.loads(plan.outputs["receipt"].read_text(encoding="utf-8"))
        self.assertEqual(receipt["termination"], {"kind": "exit", "code": 143})

    def test_probe_permission_failure_never_spawns(self) -> None:
        plan = self.make_plan()
        real_open = os.open

        def deny_probe(path: object, *args: object, **kwargs: object) -> int:
            if isinstance(path, str) and path.startswith(".buildharbor-probe-"):
                raise PermissionError("denied by test")
            return real_open(path, *args, **kwargs)

        with (
            self.runtime(),
            mock.patch("buildharbor.executor.os.open", side_effect=deny_probe),
            mock.patch("buildharbor.executor.subprocess.Popen") as popen,
        ):
            self.assertEqual(execute(plan), 2)
        popen.assert_not_called()
        self.assertFalse(plan.outputs["receipt"].exists())

    def test_pre_storage_guard_failure_only_reports_and_never_spawns(self) -> None:
        plan = self.make_plan()
        diagnostics = io.StringIO()
        with (
            self.runtime(volumes=BuildHarborError("volume unavailable")),
            mock.patch("buildharbor.executor.subprocess.Popen") as popen,
            redirect_stderr(diagnostics),
        ):
            self.assertEqual(execute(plan), 2)
        popen.assert_not_called()
        self.assertFalse(self.project_storage.exists())
        self.assertIn("could not prepare protected storage", diagnostics.getvalue())

    def test_disk_loss_after_success_returns_two_without_fallback_receipt(self) -> None:
        plan = self.make_plan()
        lost = BuildHarborError("destination unavailable")
        with self.runtime(volumes=[self.volume, self.volume, lost]):
            self.assertEqual(execute(plan), 2)
        self.assertFalse(plan.outputs["receipt"].exists())

    def test_prelaunch_failures_write_versioned_receipts_when_storage_is_safe(self) -> None:
        changed_xcode = replace(self.xcode, build="27A999z")
        guard_plan = self.make_plan(run_id="guard-failure")
        with (
            self.runtime(),
            mock.patch("buildharbor.executor.inspect_xcode", return_value=changed_xcode),
            mock.patch("buildharbor.executor.subprocess.Popen") as popen,
        ):
            self.assertEqual(execute(guard_plan), 2)
        popen.assert_not_called()
        guard_receipt = json.loads(guard_plan.outputs["receipt"].read_text(encoding="utf-8"))
        self.assertEqual(guard_receipt["result"], "guard_failed")
        self.assertEqual(guard_receipt["termination"], {"kind": "not_started"})

        launch_plan = self.make_plan(run_id="launch-failure")
        with (
            self.runtime(),
            mock.patch("buildharbor.executor.subprocess.Popen", side_effect=FileNotFoundError()),
        ):
            self.assertEqual(execute(launch_plan), 2)
        launch_receipt = json.loads(launch_plan.outputs["receipt"].read_text(encoding="utf-8"))
        self.assertEqual(launch_receipt["result"], "launch_failed")
        self.assertEqual(launch_receipt["termination"], {"kind": "not_started"})

    def test_cli_rejected_run_never_spawns(self) -> None:
        identity = self.root / "Demo.xcodeproj"
        identity.mkdir()
        (identity / "project.pbxproj").write_text("{ objects = {}; }", encoding="utf-8")
        diagnostics = io.StringIO()
        with (
            mock.patch("buildharbor.cli.load_config", return_value=self.config),
            mock.patch("subprocess.Popen") as popen,
            redirect_stderr(diagnostics),
        ):
            result = main(
                [
                    "run",
                    "--project-dir",
                    str(self.root),
                    "--",
                    "-project",
                    "Demo.xcodeproj",
                    "-scheme",
                    "Demo",
                    "archive",
                ]
            )
        self.assertEqual(result, 2)
        popen.assert_not_called()
        self.assertIn("Unsupported action or option", diagnostics.getvalue())


if __name__ == "__main__":
    unittest.main()
