"""Execute a reviewed plan while retaining its volume and Xcode guards."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import uuid

from . import __version__
from .errors import BuildHarborError
from .paths import open_directory, validate_destination
from .planner import Plan, FORBIDDEN_ENV
from .reporting import LIMITATIONS
from .volume import Volume, inspect_volume
from .xcode import inspect_xcode
from .artifacts import archive_digest, inspect_archived_app, read_receipt, EXPORT_OPTIONS_BYTES
from .settings import SettingsError, inspect_effective_settings


_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@dataclass(frozen=True)
class _ChildResult:
    return_code: int
    exit_code: int
    termination: dict[str, object]


class _LaunchError(BuildHarborError):
    """The guarded launch failed before a child process existed."""


def _revalidate(plan: Plan) -> Volume:
    """Refresh volume state and validate every path against the planned identity."""
    current = inspect_volume(plan.config)
    if current.uuid != plan.volume.uuid or current.device_id != plan.volume.device_id:
        raise BuildHarborError("The destination volume identity changed after planning; Xcode was not launched.")
    validate_destination(plan.project_storage, plan.config, current)
    for path in plan.outputs.values():
        validate_destination(path, plan.config, current)
    for path in plan.directories:
        validate_destination(path, plan.config, current)
    return current


def _revalidate_inputs(plan: Plan, volume: Volume) -> None:
    if plan.graph is not None:
        from .projects import revalidate_graph
        revalidate_graph(plan.graph, plan.config.project_root)
    if plan.action == "export":
        if archive_digest(plan.inputs["archive"], plan.config, volume) != plan.input_hashes["archive"]:
            raise BuildHarborError("The archive changed after planning; export was not started.")
        _, digest = read_receipt(plan.inputs["archive_receipt"], plan.config, volume)
        if digest != plan.input_hashes["archive_receipt"]:
            raise BuildHarborError("The archive receipt changed after planning; export was not started.")


def _require_absent_outputs(plan: Plan, *, after_inspection: bool = False) -> None:
    for name in plan.unique_outputs:
        if name not in plan.outputs:
            continue
        if after_inspection and ("settings_result" in name or name == "export_options"):
            continue
        try:
            plan.outputs[name].lstat()
        except FileNotFoundError:
            continue
        raise BuildHarborError("A unique output appeared after planning; existing data will not be overwritten.")


def _write_export_options(plan: Plan, volume: Volume) -> None:
    path = plan.outputs["export_options"]
    directory = open_directory(path.parent, plan.config, volume)
    fd = None
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        if os.fstat(fd).st_dev != volume.device_id:
            raise BuildHarborError("Export options crossed onto another filesystem.")
        payload = EXPORT_OPTIONS_BYTES
        while payload:
            count = os.write(fd, payload)
            if count == 0:
                raise OSError("zero-byte write")
            payload = payload[count:]
        os.fsync(fd)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def _acquire_lock(directory_fd: int, volume: Volume) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    lock_fd: int | None = None
    try:
        lock_fd = os.open(".buildharbor.lock", flags, 0o600, dir_fd=directory_fd)
        metadata = os.fstat(lock_fd)
        if metadata.st_dev != volume.device_id or not stat.S_ISREG(metadata.st_mode):
            raise BuildHarborError("The persistent BuildHarbor lock is not a regular file on the destination volume.")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BuildHarborError("Another BuildHarbor run already holds this project storage lock.") from exc
        return lock_fd
    except BuildHarborError:
        if lock_fd is not None:
            os.close(lock_fd)
        raise
    except OSError as exc:
        if lock_fd is not None:
            os.close(lock_fd)
        raise BuildHarborError("Cannot open or lock the persistent project storage lock.") from exc


def _probe_write(directory_fd: int, volume: Volume) -> None:
    """Create, sync, and remove only a random probe owned by this invocation."""
    name = f".buildharbor-probe-{uuid.uuid4().hex}"
    probe_fd: int | None = None
    created = False
    error: OSError | None = None
    try:
        probe_fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        created = True
        if os.fstat(probe_fd).st_dev != volume.device_id:
            raise OSError("probe crossed onto another filesystem")
        payload = b"BuildHarbor write probe\n"
        written = 0
        while written < len(payload):
            count = os.write(probe_fd, payload[written:])
            if count == 0:
                raise OSError("zero-byte probe write")
            written += count
        os.fsync(probe_fd)
    except OSError as exc:
        error = exc
    finally:
        if probe_fd is not None:
            try:
                os.close(probe_fd)
            except OSError as exc:
                error = error or exc
        if created:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError as exc:
                error = error or exc
    if error is not None:
        raise BuildHarborError("Cannot write, sync, and remove a probe on the destination volume.") from error


def _create_directories(plan: Plan, volume: Volume) -> int:
    storage_fd = open_directory(plan.project_storage, plan.config, volume, create=True)
    lock_fd: int | None = None
    try:
        lock_fd = _acquire_lock(storage_fd, volume)
        _probe_write(storage_fd, volume)
        for directory in plan.directories:
            fd = open_directory(directory, plan.config, volume, create=True)
            os.close(fd)
        result, lock_fd = lock_fd, None
        return result
    finally:
        os.close(storage_fd)
        if lock_fd is not None:
            os.close(lock_fd)


def _run_child(plan: Plan, lock_fd: int, environment: dict[str, str]) -> _ChildResult:
    child: subprocess.Popen[bytes] | None = None
    pending: list[int] = []
    previous: dict[int, signal.Handlers] = {}

    def forward(signum: int, _frame: object) -> None:
        pending.append(signum)
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    try:
        for signum in _SIGNALS:
            try:
                old_handler = signal.getsignal(signum)
                signal.signal(signum, forward)
                previous[signum] = old_handler
            except ValueError:
                # execute() is expected on the CLI's main thread. Tests and embedded
                # callers can still execute, but Python only permits signal handlers
                # to be installed from the main thread.
                break
        try:
            child = subprocess.Popen(
                list(plan.command),
                cwd=plan.config.project_root,
                env=environment,
                start_new_session=True,
                pass_fds=(lock_fd,),
            )
        except OSError as exc:
            raise _LaunchError("Cannot launch the planned xcodebuild executable.") from exc
        for signum in pending:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                break
        return_code = child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if return_code < 0:
        signum = -return_code
        return _ChildResult(
            return_code=return_code,
            exit_code=128 + signum,
            termination={"kind": "signal", "signal": signum},
        )
    return _ChildResult(
        return_code=return_code,
        exit_code=return_code,
        termination={"kind": "exit", "code": return_code},
    )


def _directory_state(path: Path, plan: Plan, volume: Volume) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise BuildHarborError("Cannot observe a planned output after the build.") from exc
    if stat.S_ISDIR(metadata.st_mode):
        fd = open_directory(path, plan.config, volume)
        try:
            with os.scandir(fd) as entries:
                return "directory_with_contents" if next(entries, None) is not None else "empty_directory"
        except OSError as exc:
            raise BuildHarborError("Cannot inspect a planned output directory after the build.") from exc
        finally:
            os.close(fd)
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    return "other"


def _make_receipt(
    plan: Plan,
    volume: Volume,
    *,
    result: str,
    exit_code: int,
    termination: dict[str, object],
    validation: dict | None = None,
) -> dict[str, object]:
    observed: dict[str, dict[str, str]] = {}
    for name, path in plan.outputs.items():
        state = "file" if name == "receipt" else _directory_state(path, plan, volume)
        observed[name] = {"path": str(path), "state": state}
    receipt = {
        "schema_version": 1,
        "kind": "run_receipt",
        "tool_version": __version__,
        "xcode": {"version": plan.xcode.version, "build": plan.xcode.build},
        "action": plan.action,
        "result": result,
        "exit_code": exit_code,
        "termination": termination,
        "run_id": plan.run_id,
        "project_id": plan.config.project_id,
        "source_identity": str(plan.source_identity) if plan.source_identity is not None else None,
        "settings_validation": validation or {"status": "not_completed"},
        "inputs": {name: str(path) for name, path in plan.inputs.items()},
        "planned_paths": {name: str(path) for name, path in plan.outputs.items()},
        "observed_paths": observed,
        "observation_note": "Directory contents may predate this run. Presence is not proof of new writes, cache hits, or complete routing.",
        "unverified_behavior": list(LIMITATIONS),
    }
    if plan.action == "archive" and result == "succeeded":
        if not (plan.outputs["archive"] / "Info.plist").is_file():
            raise BuildHarborError("Xcode returned success without the expected archive metadata.")
        receipt["archive_sha256"] = archive_digest(plan.outputs["archive"], plan.config, volume)
        receipt["archive_application"] = inspect_archived_app(plan.outputs["archive"], plan.config, volume)
    if plan.action == "export" and result == "succeeded":
        if not any(plan.outputs["export"].glob("*.app/Contents/Info.plist")):
            raise BuildHarborError("Xcode returned success without an exported macOS application.")
    return receipt


def _write_receipt(plan: Plan, volume: Volume, receipt: dict[str, object]) -> None:
    path = plan.outputs["receipt"]
    directory_fd = open_directory(path.parent, plan.config, volume)
    receipt_fd: int | None = None
    try:
        receipt_fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        if os.fstat(receipt_fd).st_dev != volume.device_id:
            raise OSError("receipt crossed onto another filesystem")
        payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
        written = 0
        while written < len(payload):
            count = os.write(receipt_fd, payload[written:])
            if count == 0:
                raise OSError("zero-byte receipt write")
            written += count
        os.fsync(receipt_fd)
        os.close(receipt_fd)
        receipt_fd = None
        os.fsync(directory_fd)
    except OSError as exc:
        raise BuildHarborError("Cannot create and sync the unique run receipt.") from exc
    finally:
        if receipt_fd is not None:
            os.close(receipt_fd)
        os.close(directory_fd)


def _record_prelaunch_failure(plan: Plan, result: str, message: str, exit_code: int = 2) -> int:
    print(f"BuildHarbor did not start the requested action: {message}", file=sys.stderr)
    try:
        volume = _revalidate(plan)
        receipt = _make_receipt(
            plan,
            volume,
            result=result,
            exit_code=exit_code,
            termination={"kind": "not_started"},
        )
        _write_receipt(plan, volume, receipt)
        print(f"BuildHarbor saved run receipt: {plan.outputs['receipt']}", file=sys.stderr)
    except (BuildHarborError, OSError):
        print("BuildHarbor could not safely record the prelaunch failure receipt.", file=sys.stderr)
    return exit_code


def execute(plan: Plan) -> int:
    """Execute *plan* and return xcodebuild's shell-style exit status."""
    lock_fd: int | None = None
    try:
        try:
            volume = _revalidate(plan)
            _revalidate_inputs(plan, volume)
            _require_absent_outputs(plan)
            lock_fd = _create_directories(plan, volume)
        except BuildHarborError as exc:
            print(f"BuildHarbor could not prepare protected storage: {exc}", file=sys.stderr)
            return 2
        except OSError:
            print("BuildHarbor could not prepare protected storage due to a filesystem error.", file=sys.stderr)
            return 2

        environment = os.environ | plan.environment
        try:
            current_xcode = inspect_xcode(environment)
            if current_xcode != plan.xcode:
                raise BuildHarborError("The selected Xcode changed after planning.")
            if any(environment.get(key) for key in FORBIDDEN_ENV):
                raise BuildHarborError("Output/compiler environment overrides appeared after planning.")
            volume = _revalidate(plan)
            _revalidate_inputs(plan, volume)
            _require_absent_outputs(plan)
        except BuildHarborError as exc:
            return _record_prelaunch_failure(plan, "guard_failed", str(exc))

        try:
            validation = inspect_effective_settings(plan, lock_fd, environment)
            packages = None
            if plan.graph is not None and plan.graph.remote_packages:
                from .projects import inspect_resolved_packages
                packages = inspect_resolved_packages(plan.outputs["package_clones"])
                if not packages:
                    raise BuildHarborError("Resolved package manifests are missing from the managed checkout directory.")
                validation["resolved_package_manifests"] = len(packages)
            # Metadata queries can take time and may mutate project metadata.
            # Recheck original inputs, storage and unique action leaves afterward.
            volume = _revalidate(plan)
            _revalidate_inputs(plan, volume)
            _require_absent_outputs(plan, after_inspection=True)
            if packages is not None and inspect_resolved_packages(plan.outputs["package_clones"]) != packages:
                raise BuildHarborError("Resolved package manifests changed before the requested action.")
            if plan.action == "export":
                _write_export_options(plan, volume)
                volume = _revalidate(plan)
                _revalidate_inputs(plan, volume)
                _require_absent_outputs(plan, after_inspection=True)
        except SettingsError as exc:
            return _record_prelaunch_failure(plan, "settings_failed", str(exc), exc.exit_code)
        except BuildHarborError as exc:
            return _record_prelaunch_failure(plan, "guard_failed", str(exc))
        except OSError:
            return _record_prelaunch_failure(plan, "guard_failed", "A filesystem operation failed during guarded inspection.")

        try:
            child_result = _run_child(plan, lock_fd, environment)
        except _LaunchError as exc:
            return _record_prelaunch_failure(plan, "launch_failed", str(exc))
        try:
            volume = _revalidate(plan)
            receipt = _make_receipt(
                plan,
                volume,
                result="succeeded" if child_result.exit_code == 0 else "failed",
                exit_code=child_result.exit_code,
                termination=child_result.termination,
                validation=validation,
            )
            _write_receipt(plan, volume, receipt)
            print(f"BuildHarbor saved run receipt: {plan.outputs['receipt']}", file=sys.stderr)
        except (BuildHarborError, OSError) as exc:
            print(f"BuildHarbor could not record the run receipt: {exc}", file=sys.stderr)
            return child_result.exit_code if child_result.exit_code != 0 else 2
        return child_result.exit_code
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
