#!/usr/bin/env python3
"""Opt-in, local macOS archive/export and workspace integration on verified storage."""

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from buildharbor.config import load_config
from buildharbor.errors import BuildHarborError
from buildharbor.executor import execute
from buildharbor.paths import open_directory
from buildharbor.planner import make_plan
from buildharbor.reporting import plan_report
from buildharbor.volume import inspect_volume


def snapshot(root):
    return {str(p.relative_to(root)): (p.lstat().st_size, p.lstat().st_mtime_ns, p.lstat().st_mode)
            for p in root.rglob("*")}


def run_logged(plan, log):
    saved_out, saved_err = os.dup(1), os.dup(2)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        with log.open("x") as stream:
            os.dup2(stream.fileno(), 1)
            os.dup2(stream.fileno(), 2)
            code = execute(plan)
            sys.stdout.flush()
            sys.stderr.flush()
    finally:
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)
    print(f"{plan.action}: exit {code}; log: {log}", flush=True)
    if code:
        raise RuntimeError(f"The guarded {plan.action} failed; inspect its retained log.")
    return json.loads(plan.outputs["receipt"].read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=2)
    parser.add_argument("--reuse-evidence", type=Path, help="Reuse a prior task-owned milestone source copy and caches; retain separate attempt logs.")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    config = load_config(repo)
    volume = inspect_volume(config)
    evidence = args.reuse_evidence or config.storage_root / "Evidence" / ("milestones-" + uuid.uuid4().hex[:12])
    if evidence.parent != config.storage_root / "Evidence" or not evidence.name.startswith("milestones-"):
        raise RuntimeError("Only an existing task-owned milestone evidence directory may be reused.")
    fd = open_directory(evidence, config, volume, create=args.reuse_evidence is None)
    os.close(fd)
    source = evidence / "Controlled sources with spaces"
    if args.reuse_evidence is None:
        shutil.copytree(repo / "fixtures", source)
    prefix = "attempt-" + uuid.uuid4().hex[:8] + "-"
    config = replace(config, project_root=source, project_id="milestone-fixtures")
    started = datetime.now(timezone.utc).isoformat()
    records = []
    common = ["-destination", "platform=macOS", "-jobs", str(args.jobs)]
    commands = [
        ["-project", "HarborFixture/HarborFixture.xcodeproj", "-scheme", "HarborFixture", *common, "build"],
        ["-project", "HarborFixture/HarborFixture.xcodeproj", "-scheme", "HarborFixture", *common, "-parallel-testing-enabled", "NO", "test"],
        ["-workspace", "HarborWorkspace/HarborWorkspace.xcworkspace", "-scheme", "HarborWorkspace", "-configuration", "Debug", "-sdk", "macosx", *common, "build"],
        ["-project", "HarborApp/HarborApp.xcodeproj", "-scheme", "HarborApp", "-configuration", "Release", *common, "archive"],
    ]
    before = snapshot(source)
    try:
        make_plan(config, ["-workspace", "HarborWorkspace/ConflictWorkspace.xcworkspace", "-scheme", "HarborConflictWorkspace", "-configuration", "Debug", "-sdk", "macosx", "build"])
    except BuildHarborError:
        pass
    else:
        raise RuntimeError("The non-selected workspace member conflict was not rejected.")
    if before != snapshot(source):
        raise RuntimeError("Rejected planning changed source metadata.")
    for number, command in enumerate(commands, 1):
        before = snapshot(source)
        plan = make_plan(config, command)
        if snapshot(source) != before:
            raise RuntimeError("Planning changed source metadata.")
        (evidence / f"{prefix}{number}-plan.json").write_text(json.dumps(plan_report(plan), indent=2) + "\n")
        receipt = run_logged(plan, evidence / f"{prefix}{number}-{plan.action}.log")
        if receipt["settings_validation"]["status"] != "passed":
            raise RuntimeError("The action has no passed effective-settings evidence.")
        if number == 3 and receipt["settings_validation"].get("member_projects") != 2:
            raise RuntimeError("Workspace member coverage is incomplete.")
        records.append({"action": plan.action, "receipt": str(plan.outputs["receipt"]), "settings": receipt["settings_validation"]})
    archive = plan.outputs["archive"]
    plan = make_plan(config, ["-exportArchive", "-archivePath", str(archive)])
    (evidence / f"{prefix}5-plan.json").write_text(json.dumps(plan_report(plan), indent=2) + "\n")
    receipt = run_logged(plan, evidence / f"{prefix}5-export.log")
    records.append({"action": "export", "receipt": str(plan.outputs["receipt"]), "export": str(plan.outputs["export"])})
    summary = {"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
               "xcode": receipt["xcode"], "non_selected_member_conflict_rejected": True,
               "plan_source_metadata_unchanged": True, "runs": records,
               "note": "Private local evidence. No simulator, GUI launch, account, provisioning, or distribution signing."}
    (evidence / f"{prefix}summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"Milestone integration passed; private evidence retained at {evidence}")


if __name__ == "__main__":
    main()
