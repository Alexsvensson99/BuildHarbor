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
from .planner import Plan
from .reporting import LIMITATIONS
from .volume import Volume, inspect_volume
from .xcode import inspect_xcode


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
) -> dict[str, object]:
    observed: dict[str, dict[str, str]] = {}
    for name, path in plan.outputs.items():
        state = "file" if name == "receipt" else _directory_state(path, plan, volume)
        observed[name] = {"path": str(path), "state": state}
    return {
        "schema_version": 1,
        "kind": "run_receipt",
        "tool_version": __version__,
        "xcode": {"version": plan.xcode.version, "build": plan.xcode.build},
        "action": plan.action,
        "result": result,
        "exit_code": exit_code,
        "termination": termination,
        "run_id": plan.run_id,
        "planned_paths": {name: str(path) for name, path in plan.outputs.items()},
        "observed_paths": observed,
        "observation_note": "Directory contents may predate this run. Presence is not proof of new writes, cache hits, or complete routing.",
        "unverified_behavior": list(LIMITATIONS),
    }


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


def _record_prelaunch_failure(plan: Plan, result: str, message: str) -> int:
    print(f"BuildHarbor did not launch xcodebuild: {message}", file=sys.stderr)
    try:
        volume = _revalidate(plan)
        receipt = _make_receipt(
            plan,
            volume,
            result=result,
            exit_code=2,
            termination={"kind": "not_started"},
        )
        _write_receipt(plan, volume, receipt)
        print(f"BuildHarbor saved run receipt: {plan.outputs['receipt']}", file=sys.stderr)
    except (BuildHarborError, OSError):
        print("BuildHarbor could not safely record the prelaunch failure receipt.", file=sys.stderr)
    return 2


def execute(plan: Plan) -> int:
    """Execute *plan* and return xcodebuild's shell-style exit status."""
    lock_fd: int | None = None
    try:
        try:
            volume = _revalidate(plan)
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
            volume = _revalidate(plan)
        except BuildHarborError as exc:
            return _record_prelaunch_failure(plan, "guard_failed", str(exc))

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
