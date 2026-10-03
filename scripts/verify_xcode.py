#!/usr/bin/env python3
"""Run opt-in real Xcode integration using only a verified external destination.

This creates a source fixture copy and a local Git package remote on the configured
volume. It does not disconnect disks, use the network, or delete its evidence.
Run from the repository root with the package installed or PYTHONPATH=src.
"""

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import functools
import http.server
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid

from buildharbor.config import load_config
from buildharbor.paths import open_directory
from buildharbor.planner import make_plan
from buildharbor.volume import inspect_volume


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=2)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    config = load_config(repo)
    volume = inspect_volume(config)
    evidence = config.storage_root / "Evidence" / ("integration-" + uuid.uuid4().hex[:12])
    fd = open_directory(evidence, config, volume, create=True)
    os.close(fd)
    started = datetime.now(timezone.utc).isoformat()
    source = evidence / "Source with spaces"
    shutil.copytree(repo / "fixtures/HarborFixture", source)
    remote = evidence / "remote" / "HarborSupport"
    shutil.copytree(repo / "fixtures/HarborFixture/Packages/HarborSupport", remote)
    for command in (["init", "-q", "-b", "main"], ["add", "."], ["-c", "user.name=BuildHarbor Fixture", "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Create local package fixture"], ["tag", "1.0.0"]):
        subprocess.run(["git", "-C", str(remote), *command], check=True)
    bare = remote.parent / "HarborSupport.git"
    subprocess.run(["git", "clone", "--bare", "--quiet", str(remote), str(bare)], check=True)
    subprocess.run(["git", "-C", str(bare), "update-server-info"], check=True)
    # A loopback-only HTTP Git remote exercises repository caching; file:// remotes
    # can deliberately bypass SwiftPM's shared repository cache.
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(remote.parent))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    project = source / "HarborFixture.xcodeproj"
    pbx = project / "project.pbxproj"
    text = pbx.read_text()
    text = text.replace("XCLocalSwiftPackageReference", "XCRemoteSwiftPackageReference")
    remote_url = f"http://127.0.0.1:{server.server_port}/HarborSupport.git"
    text = text.replace("relativePath = Packages/HarborSupport;", f'repositoryURL = "{remote_url}";\n            requirement = {{ kind = exactVersion; version = 1.0.0; }};')
    pbx.write_text(text)
    config = replace(config, project_root=source, project_id="remote-fixture")
    # The fixture copy deliberately includes a space in its source path.
    common = ["-project", str(project), "-scheme", "HarborFixture", "-destination", "platform=macOS", "-jobs", str(args.jobs)]
    from buildharbor.executor import execute

    try:
        verify_runs(config, common, evidence, started)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def verify_runs(config, common, evidence, started):
    from buildharbor.executor import execute
    runs = []
    object_snapshot = None
    cache_snapshot = None
    for number, action in enumerate(("build", "build", "test"), 1):
        inspect_volume(config)
        plan = make_plan(config, common + (["-parallel-testing-enabled", "NO"] if action == "test" else []) + [action])
        log = evidence / f"{number}-{action}.log"
        # Redirect file descriptors so inherited Xcode streams stay in the local log.
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
        print(f"{number}: {action} exited {code}; log: {log}", flush=True)
        require(code == 0, f"Integration {action} failed; inspect {log}.")
        objects = sorted(plan.outputs["derived_data"].rglob("HarborFixtureClang.o"))
        objects += sorted(plan.outputs["derived_data"].rglob("HarborFixture.o"))
        require(len(objects) >= 2, "Expected compiled C and Swift objects were not observed.")
        snapshot = {str(p): p.stat().st_mtime_ns for p in objects}
        modules = list(plan.outputs["module_cache"].rglob("*.pcm"))
        modules += list(plan.outputs["derived_data"].rglob("*.pcm"))
        require(modules, "No compiled modules were observed.")
        module_times = {str(p): p.stat().st_mtime_ns for p in modules}
        if number == 1:
            object_snapshot, cache_snapshot = snapshot, module_times
        if number == 2:
            require(snapshot == object_snapshot, "The repeated build recompiled the fixture objects.")
            require(all(Path(p).stat().st_mtime_ns == stamp for p, stamp in cache_snapshot.items()), "The repeated build replaced existing compiled modules.")
        clones = list(plan.outputs["package_clones"].glob("checkouts/*/Package.swift"))
        require(clones, "The local Git package was not checked out beneath the routed clone directory.")
        package_repos = list(plan.outputs["package_cache"].glob("repositories/*"))
        require(package_repos, "Package repository cache content was not observed at its planned location.")
        require(any(p.is_file() for p in plan.outputs["compilation_cache"].rglob("*")), "Compilation cache files were not observed.")
        if action == "test":
            require((plan.outputs["result_bundle"] / "Info.plist").is_file(), "The test result bundle was not observed.")
            require("Executed 1 test" in log.read_text(), "Expected XCTest execution was not found in the log.")
        runs.append({"action": action, "exit_code": code, "receipt": str(plan.outputs["receipt"]), "compiled_objects": len(objects), "compiled_modules": len(modules), "package_checkouts": len(clones), "package_cache_repositories": len(package_repos)})
    summary = {"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(), "xcode": {"version": plan.xcode.version, "build": plan.xcode.build}, "runs": runs, "reused_objects_and_modules": True, "note": "Local private evidence. No claim of complete filesystem isolation."}
    (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"Integration passed; evidence retained at {evidence}")


if __name__ == "__main__":
    main()
