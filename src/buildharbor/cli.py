"""BuildHarbor's small command-line interface."""

import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__
from .config import load_config
from .errors import BuildHarborError
from .paths import validate_destination
from .planner import make_plan
from .reporting import render_plan, plan_report
from .volume import inspect_volume
from .xcode import inspect_xcode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guard selected Xcode outputs on a configured external APFS volume.")
    parser.add_argument("--version", action="version", version=f"BuildHarbor {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "plan", "run"):
        command = sub.add_parser(name)
        command.add_argument("--project-dir", type=Path, default=Path.cwd(), help="Directory containing both configuration files (default: current directory).")
        if name != "run":
            command.add_argument("--json", action="store_true", help="Print a schema-versioned JSON report.")
        if name != "doctor":
            command.add_argument("xcode_arguments", nargs=argparse.REMAINDER, help="Use -- followed by xcodebuild arguments.")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.project_dir)
        if args.command == "doctor":
            volume = inspect_volume(config)
            validate_destination(config.storage_root, config, volume)
            if config.storage_root.exists() and not config.storage_root.is_dir():
                raise BuildHarborError("storage_root is occupied by a file.")
            xcode = inspect_xcode()
            nearest = config.storage_root
            while not nearest.exists():
                nearest = nearest.parent
            report = {
                "schema_version": 1, "kind": "doctor", "tool_version": __version__, "status": "ready_for_plan",
                "mount": str(config.mount), "volume_uuid_matches": volume.uuid == config.volume_uuid,
                "filesystem": volume.filesystem, "external": True, "available_bytes": volume.available_bytes,
                "minimum_free_bytes": config.minimum_free_bytes,
                "permission_metadata": {"write_and_traverse": os.access(nearest, os.W_OK | os.X_OK), "path": str(nearest)},
                "write_access": "not_probed_read_only_doctor",
                "xcode": {"version": xcode.version, "build": xcode.build, "developer_dir": str(xcode.developer_dir)},
            }
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print(f"BuildHarbor {__version__}: configuration and volume checks passed.")
                print(f"External APFS volume: {config.mount}; UUID matches.")
                print(f"Available capacity: {volume.available_bytes / 1024**3:.2f} GiB (filesystem available bytes).")
                print(f"Xcode: {xcode.version} ({xcode.build}).")
                print("Permission metadata permits access. Actual write access is unverified; run probes it before launching Xcode.")
            return 0
        if not args.xcode_arguments or args.xcode_arguments[0] != "--":
            raise BuildHarborError("Use -- before the xcodebuild arguments.")
        plan = make_plan(config, args.xcode_arguments[1:])
        if args.command == "plan":
            print(json.dumps(plan_report(plan), indent=2) if args.json else render_plan(plan))
            return 0
        from .executor import execute
        return execute(plan)
    except (BuildHarborError, OSError) as exc:
        # Unexpected OS messages can contain sensitive paths; report only our own messages.
        message = str(exc) if isinstance(exc, BuildHarborError) else "A filesystem operation failed. Check the configured paths, permissions, and mounted volume."
        if getattr(args, "json", False):
            print(json.dumps({"schema_version": 1, "kind": "error", "tool_version": __version__, "error": message}))
        else:
            print(f"BuildHarbor: {message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
