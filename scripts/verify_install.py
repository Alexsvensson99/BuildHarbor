#!/usr/bin/env python3
"""Verify a committed BuildHarbor candidate from a clean SSD installation.

This opt-in script retains one new private Evidence directory. It never reuses or
deletes evidence, changes global settings, or invokes xcodebuild. Run it from an
installed checkout (or with PYTHONPATH=src) after committing the candidate.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid

from buildharbor import __version__
from buildharbor.config import load_config
from buildharbor.paths import open_directory
from buildharbor.volume import inspect_volume


MAX_ARCHIVE_ENTRIES = 100_000
MAX_ARCHIVE_BYTES = 1024**3


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run_capture(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
    timeout: float,
) -> int:
    """Run one fixed argv with private retained streams and a finite timeout."""
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        try:
            result = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"The {stdout_path.stem} command exceeded its fixed timeout.") from exc
    return result.returncode


def require_success(code: int, label: str) -> None:
    require(code == 0, f"{label} failed with exit code {code}; inspect its private evidence logs.")


def safe_extract(archive: Path, destination: Path) -> None:
    """Extract only bounded regular files/directories from our controlled Git tar."""
    destination.mkdir(mode=0o700)
    seen: set[str] = set()
    directories: list[tuple[Path, int]] = []
    entries = 0
    byte_count = 0
    with tarfile.open(archive, mode="r:") as stream:
        for member in stream:
            entries += 1
            if entries > MAX_ARCHIVE_ENTRIES:
                raise RuntimeError("The committed source archive exceeds the safe entry bound.")
            path = PurePosixPath(member.name)
            canonical = path.as_posix()
            if (
                not member.name
                or path.is_absolute()
                or ".." in path.parts
                or member.name.rstrip("/") != canonical
                or canonical in seen
                or any(not part or part == "." for part in path.parts)
            ):
                raise RuntimeError("The committed source archive contains an unsafe path.")
            seen.add(canonical)
            target = destination.joinpath(*path.parts)
            if not target.is_relative_to(destination):
                raise RuntimeError("The committed source archive escapes its private destination.")
            if member.isdir():
                if target.exists() and not target.is_dir():
                    raise RuntimeError("The committed source archive has a conflicting directory.")
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
                directories.append((target, int(member.mtime)))
                continue
            if not member.isfile():
                raise RuntimeError("The committed source archive contains a link or special file.")
            if member.size < 0:
                raise RuntimeError("The committed source archive contains an invalid file size.")
            byte_count += member.size
            if byte_count > MAX_ARCHIVE_BYTES:
                raise RuntimeError("The committed source archive exceeds the safe byte bound.")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            source = stream.extractfile(member)
            if source is None:
                raise RuntimeError("A committed source file could not be read from the archive.")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            require(target.stat().st_size == member.size, "A committed source file extracted with the wrong size.")
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
            os.utime(target, (member.mtime, member.mtime), follow_symlinks=False)
    for directory, modified in sorted(directories, key=lambda item: len(item[0].parts), reverse=True):
        directory.chmod(0o755)
        os.utime(directory, (modified, modified), follow_symlinks=False)


def snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    """Capture contents and stable metadata; access times are deliberately absent."""
    result: dict[str, tuple[object, ...]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode):
            content = hashlib.sha256(path.read_bytes()).hexdigest()
            kind = "regular"
        elif stat.S_ISLNK(metadata.st_mode):
            content = os.readlink(path)
            kind = "symlink"
        elif stat.S_ISDIR(metadata.st_mode):
            content = None
            kind = "directory"
        else:
            content = None
            kind = "special"
        relative = "." if path == root else str(path.relative_to(root))
        result[relative] = (
            kind,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            content,
        )
    return result


def snapshot_digest(value: dict[str, tuple[object, ...]]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def reject_installed_bytecode(package: Path) -> None:
    unwanted = [path for path in package.rglob("*") if path.name == "__pycache__" or path.suffix == ".pyc"]
    require(not unwanted, "The installed BuildHarbor package contains bytecode cache files.")


def read_json(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"The installed {label} command did not produce one valid JSON report.") from exc
    require(isinstance(value, dict), f"The installed {label} report is not a JSON object.")
    require(value.get("schema_version") == 1, f"The installed {label} report has the wrong schema version.")
    require(value.get("tool_version") == __version__, f"The installed {label} report has the wrong tool version.")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="HEAD", help="Committed Git revision to install (default: HEAD).")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    config = load_config(repo)
    volume = inspect_volume(config)
    evidence = config.storage_root / "Evidence" / ("install-" + uuid.uuid4().hex)
    require(not evidence.exists(), "The unique install evidence path already exists.")
    evidence_fd = open_directory(evidence, config, volume, create=True)
    os.close(evidence_fd)

    logs = evidence / "Logs"
    reports = evidence / "Reports"
    temporary = evidence / "Temporary"
    pip_cache = evidence / "PipCache"
    for directory in (logs, reports, temporary, pip_cache):
        directory.mkdir(mode=0o700)

    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment.pop("VIRTUAL_ENV", None)
    environment.update(
        {
            "TMPDIR": str(temporary),
            "PIP_CACHE_DIR": str(pip_cache),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )

    started = datetime.now(timezone.utc).isoformat()
    revision_stdout = logs / "git-revision.txt"
    code = run_capture(
        ["git", "rev-parse", "--verify", f"{args.revision}^{{commit}}"],
        cwd=repo,
        env=environment,
        stdout_path=revision_stdout,
        stderr_path=logs / "git-revision.stderr.log",
        timeout=60,
    )
    require_success(code, "Git revision resolution")
    revision = revision_stdout.read_text(encoding="utf-8").strip()
    require(len(revision) in (40, 64) and all(character in "0123456789abcdef" for character in revision), "Git returned an invalid commit identity.")

    archive = evidence / "candidate-source.tar"
    with archive.open("xb") as output, (logs / "git-archive.stderr.log").open("xb") as stderr:
        try:
            result = subprocess.run(
                ["git", "archive", "--format=tar", revision],
                cwd=repo,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=stderr,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Git archive exceeded its fixed timeout.") from exc
    require_success(result.returncode, "Git archive")

    source = evidence / "CommittedSource"
    safe_extract(archive, source)
    require(not (source / ".git").exists(), "The private source copy unexpectedly contains Git metadata.")
    local_config = repo / ".buildharbor.local.toml"
    require(local_config.is_file() and not local_config.is_symlink(), "The ignored local BuildHarbor config is unavailable or unsafe.")
    target_config = source / ".buildharbor.local.toml"
    require(not target_config.exists(), "The committed archive unexpectedly contains the ignored local config.")
    shutil.copyfile(local_config, target_config)
    target_config.chmod(0o600)
    installed_config = load_config(source)
    require(
        (installed_config.mount, installed_config.storage_root, installed_config.volume_uuid)
        == (config.mount, config.storage_root, config.volume_uuid),
        "The private copied config does not preserve the verified volume identity.",
    )

    venv = evidence / "VirtualEnvironment"
    code = run_capture(
        [sys.executable, "-m", "venv", str(venv)],
        cwd=evidence,
        env=environment,
        stdout_path=logs / "venv.stdout.log",
        stderr_path=logs / "venv.stderr.log",
        timeout=60,
    )
    require_success(code, "Virtual environment creation")
    python = venv / "bin/python"
    cli = venv / "bin/buildharbor"
    require(python.is_file(), "The fresh virtual environment has no Python executable.")

    code = run_capture(
        [str(python), "-m", "pip", "install", "--no-compile", "--no-deps", str(source)],
        cwd=evidence,
        env=environment,
        stdout_path=logs / "pip-install.stdout.log",
        stderr_path=logs / "pip-install.stderr.log",
        timeout=180,
    )
    require_success(code, "Candidate pip installation")
    require(cli.is_file(), "The candidate installation did not create the BuildHarbor CLI.")

    site_stdout = logs / "site-packages.txt"
    code = run_capture(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        cwd=evidence,
        env=environment,
        stdout_path=site_stdout,
        stderr_path=logs / "site-packages.stderr.log",
        timeout=60,
    )
    require_success(code, "Installed package location inspection")
    package = Path(site_stdout.read_text(encoding="utf-8").strip()) / "buildharbor"
    require(package.is_dir() and package.is_relative_to(venv), "The installed package is outside the fresh virtual environment.")
    reject_installed_bytecode(package)

    source_before = snapshot(source)
    package_before = snapshot(package)
    results: list[dict[str, object]] = []

    version_stdout = reports / "version.txt"
    code = run_capture(
        [str(cli), "--version"],
        cwd=evidence,
        env=environment,
        stdout_path=version_stdout,
        stderr_path=logs / "version.stderr.log",
        timeout=60,
    )
    require_success(code, "Installed version check")
    version_text = version_stdout.read_text(encoding="utf-8").strip()
    require(version_text == f"BuildHarbor {__version__}", "The installed CLI version does not match the candidate version.")
    results.append({"command": "version", "exit_code": code, "version": version_text})

    doctor_path = reports / "doctor.json"
    code = run_capture(
        [str(cli), "doctor", "--project-dir", str(source), "--json"],
        cwd=evidence,
        env=environment,
        stdout_path=doctor_path,
        stderr_path=logs / "doctor.stderr.log",
        timeout=60,
    )
    require_success(code, "Installed doctor check")
    doctor = read_json(doctor_path, "doctor")
    require(doctor.get("kind") == "doctor" and doctor.get("status") == "ready_for_plan", "The installed doctor report is not ready for planning.")
    results.append({"command": "doctor", "exit_code": code, "kind": doctor["kind"], "status": doctor["status"]})

    plan_path = reports / "plan.json"
    code = run_capture(
        [
            str(cli),
            "plan",
            "--project-dir",
            str(source),
            "--json",
            "--",
            "-project",
            "fixtures/HarborFixture/HarborFixture.xcodeproj",
            "-scheme",
            "HarborFixture",
            "-destination",
            "platform=macOS",
            "build",
        ],
        cwd=evidence,
        env=environment,
        stdout_path=plan_path,
        stderr_path=logs / "plan.stderr.log",
        timeout=60,
    )
    require_success(code, "Installed plan check")
    plan = read_json(plan_path, "plan")
    require(plan.get("kind") == "plan" and plan.get("action") == "build", "The installed plan report has the wrong action.")
    require(plan.get("write_access") == "not_probed_read_only_plan", "The installed plan unexpectedly claimed write access.")
    require(plan.get("settings_inspection", {}).get("status") == "pending_run", "The installed plan has the wrong settings status.")
    results.append({"command": "plan", "exit_code": code, "kind": plan["kind"], "action": plan["action"], "status": plan["settings_inspection"]["status"]})

    report_path = reports / "report.json"
    code = run_capture(
        [str(cli), "report", "--project-dir", str(source), "--json"],
        cwd=evidence,
        env=environment,
        stdout_path=report_path,
        stderr_path=logs / "report.stderr.log",
        timeout=60,
    )
    storage_report = read_json(report_path, "report")
    report_complete = code == 0 and storage_report.get("kind") == "storage_report" and storage_report.get("status") == "complete"
    results.append(
        {
            "command": "report",
            "exit_code": code,
            "kind": storage_report.get("kind"),
            "status": storage_report.get("status"),
            "is_lower_bound": storage_report.get("is_lower_bound"),
        }
    )

    source_after = snapshot(source)
    package_after = snapshot(package)
    source_unchanged = source_before == source_after
    package_unchanged = package_before == package_after
    reject_installed_bytecode(package)
    summary = {
        "schema_version": 1,
        "kind": "clean_install_evidence",
        "status": "passed" if report_complete and source_unchanged and package_unchanged else "failed",
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "tool_version": __version__,
        "git_revision": revision,
        "results": results,
        "stable_metadata": {
            "fields": ["contents", "mode", "mtime_ns", "ctime_ns"],
            "atime_excluded": True,
            "source_entries": len(source_before),
            "source_before_sha256": snapshot_digest(source_before),
            "source_after_sha256": snapshot_digest(source_after),
            "source_unchanged": source_unchanged,
            "installed_package_entries": len(package_before),
            "installed_package_before_sha256": snapshot_digest(package_before),
            "installed_package_after_sha256": snapshot_digest(package_after),
            "installed_package_unchanged": package_unchanged,
            "installed_package_bytecode_absent": True,
        },
        "note": "Private local evidence. Planning ran, but xcodebuild and all build actions were not executed.",
    }
    (evidence / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    require(report_complete, "The installed report command was incomplete or failed; investigate the retained private report evidence.")
    require(source_unchanged, "Installed CLI checks changed the committed private source copy.")
    require(package_unchanged, "Installed CLI checks changed the installed BuildHarbor package.")
    print(evidence)


if __name__ == "__main__":
    main()
