#!/usr/bin/env python3
"""Create private, opt-in storage-report evidence on the configured real volume.

The script creates one new UUID-named Evidence directory, never reuses or removes
evidence, and does not invoke Xcode. Run from the repository root with the package
installed or with PYTHONPATH=src.
"""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from buildharbor.config import load_config
from buildharbor.paths import open_directory
from buildharbor.storage import scan_storage
from buildharbor.volume import inspect_volume


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    """Capture content and stable metadata; access time is deliberately excluded."""
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


def inode_bytes(path: Path) -> tuple[int, int]:
    metadata = path.stat()
    return metadata.st_size, metadata.st_blocks * 512


def reconcile(report: dict[str, object]) -> None:
    categories = [*report["projects"].values(), report["shared"], report["unattributed"]]
    for field in ("logical_bytes", "allocated_bytes", "regular_files"):
        require(
            report["totals"][field] == sum(category[field] for category in categories),
            f"Storage report did not reconcile {field}.",
        )


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    base = load_config(repo)
    volume = inspect_volume(base)
    evidence = base.storage_root / "Evidence" / ("report-" + uuid.uuid4().hex)
    require(not evidence.exists(), "The unique reporting evidence path already exists.")
    evidence_fd = open_directory(evidence, base, volume, create=True)
    os.close(evidence_fd)

    started = datetime.now(timezone.utc).isoformat()
    alpha_source = evidence / "ConfiguredProjects/Alpha"
    beta_source = evidence / "ConfiguredProjects/Beta"
    alpha_source.mkdir(parents=True)
    beta_source.mkdir(parents=True)

    storage = evidence / "Storage"
    absent_storage = evidence / "AbsentStorage"
    alpha = replace(base, project_root=alpha_source, project_id="alpha", storage_root=storage)
    beta = replace(base, project_root=beta_source, project_id="beta", storage_root=storage)
    absent_alpha = replace(alpha, storage_root=absent_storage)
    absent_beta = replace(beta, storage_root=absent_storage)

    absent_report = scan_storage([absent_alpha, absent_beta])
    require(absent_report["status"] == "complete", "An absent controlled storage root was not reported completely.")
    require(absent_report["storage_root_state"] == "absent", "The absent controlled storage root was not identified.")
    require(absent_report["totals"]["regular_files"] == 0, "The absent controlled storage root reported files.")
    require(not absent_storage.exists(), "Reporting created the absent controlled storage root.")

    alpha_storage = storage / "projects/alpha-0000000000000001/xcode-fixture"
    beta_storage = storage / "projects/beta-0000000000000002/xcode-fixture"
    unknown_storage = storage / "Unknown"
    alpha_storage.mkdir(parents=True)
    beta_storage.mkdir(parents=True)
    unknown_storage.mkdir(parents=True)

    same = alpha_storage / "same-project-a"
    same.write_bytes(b"same-project-hardlink\n")
    os.link(same, alpha_storage / "same-project-b")

    shared = alpha_storage / "cross-project-alpha"
    shared.write_bytes(b"cross-project-hardlink\n")
    os.link(shared, beta_storage / "cross-project-beta")

    unknown = alpha_storage / "known-to-unknown-alpha"
    unknown.write_bytes(b"known-and-unknown-hardlink\n")
    os.link(unknown, unknown_storage / "known-to-unknown-other")

    outside = alpha_storage / "outside-storage-alpha"
    outside.write_bytes(b"outside-storage-hardlink\n")
    os.link(outside, evidence / "outside-storage-hardlink")

    symlink_target = evidence / "outside-symlink-target"
    symlink_target.write_bytes(b"This content must never be followed by the storage scanner.\n")
    (storage / "outside-symlink").symlink_to(symlink_target)

    before = snapshot(storage)
    report = scan_storage([alpha, beta])
    after = snapshot(storage)
    require(before == after, "Storage reporting changed controlled contents, mode, mtime, or ctime.")
    require(report["status"] == "complete", "The controlled full storage report was incomplete.")
    require(report["is_lower_bound"] is False, "The controlled full report was unexpectedly a lower bound.")
    require(report["issues"] == [], "The controlled full storage report had issues.")

    same_bytes = inode_bytes(same)
    shared_bytes = inode_bytes(shared)
    unknown_bytes = inode_bytes(unknown)
    outside_bytes = inode_bytes(outside)
    expected_total = tuple(sum(values) for values in zip(same_bytes, shared_bytes, unknown_bytes, outside_bytes))
    require(report["projects"]["alpha"]["regular_files"] == 1, "Same-project hardlinks were not counted once for alpha.")
    require(report["projects"]["beta"]["regular_files"] == 0, "Beta received an unexpected private inode.")
    require(report["shared"]["regular_files"] == 1, "Cross-project hardlinks were not counted once as shared.")
    require(report["unattributed"]["regular_files"] == 2, "Unknown or outside links were not unattributed.")
    require(report["totals"]["regular_files"] == 4, "Unique regular inodes were not counted exactly once.")
    require(report["totals"]["logical_bytes"] == expected_total[0], "Logical byte accounting differed from st_size.")
    require(report["totals"]["allocated_bytes"] == expected_total[1], "Allocated byte accounting differed from st_blocks * 512.")
    require(report["ignored"] == {"symlinks": 1, "special": 0}, "The outside symlink was not skipped exactly once.")
    reconcile(report)

    partial_report = scan_storage([alpha, beta], max_entries=1)
    require(partial_report["status"] == "incomplete", "The bounded partial scan was not marked incomplete.")
    require(partial_report["is_lower_bound"] is True, "The bounded partial scan was not marked as a lower bound.")
    require(
        "entry_limit_reached" in {issue["code"] for issue in partial_report["issues"]},
        "The bounded partial scan did not report its entry limit.",
    )
    reconcile(partial_report)

    summary = {
        "schema_version": 1,
        "kind": "reporting_integration_evidence",
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "storage_contents_and_stable_metadata_unchanged": True,
        "atime_excluded": True,
        "full_report": report,
        "absent_report": absent_report,
        "partial_report": partial_report,
        "note": "Private local evidence. The report is best-effort and does not claim APFS physical or reclaimable bytes.",
    }
    (evidence / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(evidence)


if __name__ == "__main__":
    main()
