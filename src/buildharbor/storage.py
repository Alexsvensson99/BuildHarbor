"""Read-only, bounded accounting for BuildHarbor-managed storage."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re
import stat
import time

from . import __version__
from .config import Config
from .errors import BuildHarborError
from .volume import Volume, inspect_volume


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_PROJECT_SUFFIX = re.compile(r"[0-9a-f]{16}")


@dataclass
class _Inode:
    logical_bytes: int
    allocated_bytes: int
    link_count: int
    observed_links: int = 0
    owners: set[str] = field(default_factory=set)
    unknown_owner: bool = False
    conflicting_metadata: bool = False


class _StopScan(Exception):
    def __init__(self, code: str):
        self.code = code


def _bucket() -> dict[str, int]:
    return {"logical_bytes": 0, "allocated_bytes": 0, "regular_files": 0}


def _add(bucket: dict[str, int], inode: _Inode) -> None:
    bucket["logical_bytes"] += inode.logical_bytes
    bucket["allocated_bytes"] += inode.allocated_bytes
    bucket["regular_files"] += 1


def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _configuration(configs: list[Config]) -> tuple[Config, list[str], set[str]]:
    if not configs:
        raise BuildHarborError("Storage reporting needs at least one project configuration.")
    first = configs[0]
    by_root: dict[Path, Config] = {}
    for config in configs:
        if (
            config.storage_root != first.storage_root
            or config.mount != first.mount
            or config.volume_uuid != first.volume_uuid
        ):
            raise BuildHarborError("Storage report configurations must share one exact storage root and volume identity.")
        try:
            root = config.project_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise BuildHarborError("A storage report project root cannot be resolved safely.") from exc
        previous = by_root.get(root)
        if previous is not None and previous.project_id != config.project_id:
            raise BuildHarborError("One project root has conflicting project identifiers.")
        by_root[root] = config

    counts = Counter(config.project_id for config in by_root.values())
    ambiguous = {project_id for project_id, count in counts.items() if count > 1}
    known = sorted(project_id for project_id in counts if project_id not in ambiguous)
    return first, known, ambiguous


def _base_report(known: list[str], ambiguous: set[str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "storage_report",
        "tool_version": __version__,
        "status": "complete",
        "is_lower_bound": False,
        "storage_root_state": "present",
        "consistency": "best_effort_non_atomic",
        "totals": _bucket(),
        "projects": {project_id: _bucket() for project_id in known},
        "shared": _bucket(),
        "unattributed": _bucket(),
        "ignored": {"symlinks": 0, "special": 0},
        "ambiguous_project_ids": sorted(ambiguous),
        "issues": [],
        "measurement": {
            "logical_bytes": "unique regular-file inode st_size",
            "allocated_bytes": "unique regular-file inode st_blocks multiplied by 512",
            "claim": "These are not APFS physical-usage or reclaimable-capacity estimates.",
        },
    }


def _issue(report: dict[str, object], issues: Counter[str], code: str, count: int = 1) -> None:
    issues[code] += count
    report["status"] = "incomplete"
    report["is_lower_bound"] = True


def _finish_issues(report: dict[str, object], issues: Counter[str]) -> None:
    report["issues"] = [
        {"code": code, "count": issues[code]}
        for code in sorted(issues)
    ]


def _owner(relative: tuple[str, ...], known: set[str], ambiguous: set[str]) -> str | None:
    if len(relative) < 3 or relative[0] != "projects":
        return None
    directory = relative[1]
    for project_id in known | ambiguous:
        prefix = project_id + "-"
        if directory.startswith(prefix) and _PROJECT_SUFFIX.fullmatch(directory[len(prefix) :]):
            return project_id if project_id in known else None
    return None


def _open_storage_root(config: Config, volume: Volume) -> tuple[int, int] | None:
    """Return mount and storage-root fds, or None when the root is absent."""
    mount_fd: int | None = None
    current_fd: int | None = None
    try:
        mount_fd = os.open(config.mount, _DIRECTORY_FLAGS)
        mount_info = os.fstat(mount_fd)
        if mount_info.st_dev != volume.device_id:
            raise BuildHarborError("The configured volume changed before the storage scan.")
        current_fd = os.dup(mount_fd)
        for part in config.storage_root.relative_to(config.mount).parts:
            try:
                before = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.close(current_fd)
                current_fd = None
                return None
            if not stat.S_ISDIR(before.st_mode) or before.st_dev != volume.device_id:
                raise BuildHarborError("The configured storage root is not a safe directory on the verified volume.")
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            opened = os.fstat(child)
            if not _same_stat(before, opened) or opened.st_dev != volume.device_id:
                os.close(child)
                raise BuildHarborError("The configured storage root changed while it was opened.")
            os.close(current_fd)
            current_fd = child
        result = (mount_fd, current_fd)
        mount_fd = current_fd = None
        return result
    except (OSError, ValueError) as exc:
        raise BuildHarborError("The configured storage root cannot be opened safely for reporting.") from exc
    finally:
        if current_fd is not None:
            os.close(current_fd)
        if mount_fd is not None:
            os.close(mount_fd)


def _reopen_storage_root(mount_fd: int, config: Config, volume: Volume) -> int:
    current_fd: int | None = None
    try:
        current_fd = os.dup(mount_fd)
        for part in config.storage_root.relative_to(config.mount).parts:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            opened = os.fstat(child)
            if opened.st_dev != volume.device_id:
                os.close(child)
                raise OSError("filesystem changed")
            os.close(current_fd)
            current_fd = child
        result, current_fd = current_fd, None
        return result
    finally:
        if current_fd is not None:
            os.close(current_fd)


def scan_storage(
    configs: list[Config],
    *,
    max_entries: int = 200_000,
    max_inodes: int = 100_000,
    max_depth: int = 64,
    timeout: float = 30.0,
) -> dict[str, object]:
    """Return bounded lower-bound accounting without writing to managed storage.

    The deadline is cooperative: it is checked between filesystem calls and cannot
    preempt a kernel syscall that stalls.
    """
    if (
        type(max_entries) is not int
        or type(max_inodes) is not int
        or type(max_depth) is not int
        or not 1 <= max_entries <= 200_000
        or not 1 <= max_inodes <= 100_000
        or not 0 <= max_depth <= 64
        or not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not 0 < timeout <= 30.0
        or not math.isfinite(timeout)
    ):
        raise BuildHarborError("Storage scan bounds exceed the fixed safe limits or are invalid.")

    config, known_list, ambiguous = _configuration(configs)
    known = set(known_list)
    report = _base_report(known_list, ambiguous)
    issues: Counter[str] = Counter()
    try:
        volume = inspect_volume(config, read_only=True)
    except (BuildHarborError, OSError):
        report["storage_root_state"] = "unavailable"
        _issue(report, issues, "volume_preflight_failed")
        _finish_issues(report, issues)
        return report

    try:
        opened = _open_storage_root(config, volume)
    except BuildHarborError:
        report["storage_root_state"] = "unavailable"
        _issue(report, issues, "storage_root_unreadable")
        _finish_issues(report, issues)
        return report
    if opened is None:
        report["storage_root_state"] = "absent"
        try:
            final_volume = inspect_volume(config, read_only=True)
            if (final_volume.device_id, final_volume.uuid) != (volume.device_id, volume.uuid):
                raise BuildHarborError("volume changed")
        except (BuildHarborError, OSError):
            report["storage_root_state"] = "unavailable"
            _issue(report, issues, "volume_revalidation_failed")
        _finish_issues(report, issues)
        return report

    mount_fd, root_fd = opened
    root_identity: os.stat_result | None = None
    try:
        root_identity = os.fstat(root_fd)
    except OSError:
        report["storage_root_state"] = "unavailable"
        _issue(report, issues, "storage_root_unreadable")
        _finish_issues(report, issues)
        return report
    finally:
        if root_identity is None:
            os.close(root_fd)
            os.close(mount_fd)
    started = time.monotonic()
    entries_seen = 0
    inode_keys: set[tuple[int, int]] = set()
    directory_keys: set[tuple[int, int]] = {(root_identity.st_dev, root_identity.st_ino)}
    inodes: dict[tuple[int, int], _Inode] = {}

    def check_time() -> None:
        if time.monotonic() - started > timeout:
            raise _StopScan("time_limit_reached")

    def remember_key(key: tuple[int, int]) -> None:
        if key in inode_keys:
            return
        if len(inode_keys) >= max_inodes:
            raise _StopScan("inode_limit_reached")
        inode_keys.add(key)

    def visit(directory_fd: int, relative: tuple[str, ...], depth: int) -> None:
        nonlocal entries_seen
        check_time()
        try:
            iterator = os.scandir(directory_fd)
        except OSError:
            _issue(report, issues, "directory_unreadable")
            return
        with iterator:
            while True:
                check_time()
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                except OSError:
                    _issue(report, issues, "directory_unreadable")
                    break
                if entries_seen >= max_entries:
                    raise _StopScan("entry_limit_reached")
                entries_seen += 1
                try:
                    metadata = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError:
                    _issue(report, issues, "entry_unreadable")
                    continue
                key = (metadata.st_dev, metadata.st_ino)
                remember_key(key)
                child_relative = relative + (entry.name,)
                if metadata.st_dev != volume.device_id:
                    _issue(report, issues, "filesystem_boundary")
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    report["ignored"]["symlinks"] += 1  # type: ignore[index]
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    if depth >= max_depth:
                        _issue(report, issues, "depth_limit_reached")
                        continue
                    if key in directory_keys:
                        _issue(report, issues, "directory_cycle")
                        continue
                    child_fd: int | None = None
                    try:
                        child_fd = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                        opened_metadata = os.fstat(child_fd)
                        if not _same_stat(metadata, opened_metadata) or opened_metadata.st_dev != volume.device_id:
                            _issue(report, issues, "entry_changed")
                            continue
                        directory_keys.add(key)
                        visit(child_fd, child_relative, depth + 1)
                    except OSError:
                        _issue(report, issues, "directory_unreadable")
                    finally:
                        if child_fd is not None:
                            os.close(child_fd)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    report["ignored"]["special"] += 1  # type: ignore[index]
                    continue
                file_fd: int | None = None
                try:
                    file_fd = os.open(entry.name, _FILE_FLAGS, dir_fd=directory_fd)
                    opened_metadata = os.fstat(file_fd)
                    if (
                        not _same_stat(metadata, opened_metadata)
                        or (
                            metadata.st_size,
                            metadata.st_blocks,
                            metadata.st_nlink,
                        )
                        != (
                            opened_metadata.st_size,
                            opened_metadata.st_blocks,
                            opened_metadata.st_nlink,
                        )
                    ):
                        _issue(report, issues, "entry_changed")
                        continue
                    if metadata.st_size < 0 or metadata.st_blocks < 0 or metadata.st_nlink < 1:
                        _issue(report, issues, "invalid_inode_metadata")
                        continue
                except OSError:
                    _issue(report, issues, "entry_unreadable")
                    continue
                finally:
                    if file_fd is not None:
                        os.close(file_fd)

                observed = inodes.get(key)
                values = (metadata.st_size, metadata.st_blocks * 512, metadata.st_nlink)
                if observed is None:
                    observed = _Inode(*values)
                    inodes[key] = observed
                elif values != (observed.logical_bytes, observed.allocated_bytes, observed.link_count):
                    if not observed.conflicting_metadata:
                        _issue(report, issues, "inode_metadata_conflict")
                    observed.conflicting_metadata = True
                observed.observed_links += 1
                owner = _owner(child_relative, known, ambiguous)
                if owner is None:
                    observed.unknown_owner = True
                else:
                    observed.owners.add(owner)

    try:
        try:
            visit(root_fd, (), 0)
        except _StopScan as exc:
            _issue(report, issues, exc.code)

        projects = report["projects"]
        shared = report["shared"]
        unattributed = report["unattributed"]
        for inode in inodes.values():
            if (
                inode.conflicting_metadata
                or inode.unknown_owner
                or not inode.owners
                or inode.link_count != inode.observed_links
            ):
                destination = unattributed
            elif len(inode.owners) == 1:
                destination = projects[next(iter(inode.owners))]  # type: ignore[index]
            else:
                destination = shared
            _add(destination, inode)  # type: ignore[arg-type]

        categories = [*projects.values(), shared, unattributed]  # type: ignore[union-attr]
        report["totals"] = {
            field: sum(category[field] for category in categories)
            for field in ("logical_bytes", "allocated_bytes", "regular_files")
        }

        try:
            root_after = os.fstat(root_fd)
            mount_after = os.fstat(mount_fd)
            reopened_fd = _reopen_storage_root(mount_fd, config, volume)
            try:
                reopened = os.fstat(reopened_fd)
            finally:
                os.close(reopened_fd)
            if (
                not _same_stat(root_identity, root_after)
                or not _same_stat(root_identity, reopened)
                or mount_after.st_dev != volume.device_id
            ):
                _issue(report, issues, "root_identity_changed")
        except OSError:
            _issue(report, issues, "root_identity_changed")
        try:
            final_volume = inspect_volume(config, read_only=True)
            if (final_volume.device_id, final_volume.uuid) != (volume.device_id, volume.uuid):
                raise BuildHarborError("volume changed")
        except (BuildHarborError, OSError):
            _issue(report, issues, "volume_revalidation_failed")
    finally:
        os.close(root_fd)
        os.close(mount_fd)

    _finish_issues(report, issues)
    return report


def render_storage(report: dict[str, object]) -> str:
    """Render a storage report without exposing scanned file names."""
    totals = report["totals"]
    lines = [
        f"BuildHarbor storage report: {report['status']} ({report['consistency'].replace('_', ' ')}).",
        f"Storage root: {report['storage_root_state']}.",
        (
            "Unique regular files: "
            f"{totals['regular_files']}; logical bytes: {totals['logical_bytes']}; "
            f"allocated bytes: {totals['allocated_bytes']}."
        ),
    ]
    for project_id, bucket in report["projects"].items():
        lines.append(
            f"Project {project_id}: {bucket['regular_files']} files, "
            f"{bucket['logical_bytes']} logical bytes, {bucket['allocated_bytes']} allocated bytes."
        )
    for label in ("shared", "unattributed"):
        bucket = report[label]
        lines.append(
            f"{label.capitalize()}: {bucket['regular_files']} files, "
            f"{bucket['logical_bytes']} logical bytes, {bucket['allocated_bytes']} allocated bytes."
        )
    ignored = report["ignored"]
    lines.append(f"Ignored entries: {ignored['symlinks']} symlinks, {ignored['special']} special files.")
    if report["ambiguous_project_ids"]:
        lines.append("Ambiguous configured project identifiers are included in unattributed totals.")
    if report["issues"]:
        details = ", ".join(f"{item['code']}={item['count']}" for item in report["issues"])
        lines.append(f"Issues: {details}.")
    if report["is_lower_bound"]:
        lines.append("Reported byte and file totals are observed lower bounds from an incomplete scan.")
    lines.append("Allocated bytes are inode-reported st_blocks values, not APFS physical usage or reclaimable capacity.")
    return "\n".join(lines)
