"""Versioned local reports; never serialize the inherited environment."""

import shlex

from . import __version__
from .planner import Plan

LIMITATIONS = [
    "Selected output routing is not a filesystem sandbox.",
    "Build scripts, package plugins, Xcode services, simulators, and macOS may write elsewhere.",
    "Effective settings are not exhaustively analyzed; workspaces, nested projects, and xcconfig overrides are unsupported in version 0.1.",
    "A successful preflight cannot guarantee protection against a later disk disconnection.",
]


def plan_report(plan: Plan) -> dict:
    return {
        "schema_version": 1, "kind": "plan", "tool_version": __version__,
        "action": plan.action, "run_id": plan.run_id,
        "xcode": {"version": plan.xcode.version, "build": plan.xcode.build},
        "command": list(plan.command), "environment_changes": plan.environment,
        "outputs": {key: str(value) for key, value in plan.outputs.items()},
        "available_bytes": plan.volume.available_bytes,
        "write_access": "not_probed_read_only_plan",
        "limitations": LIMITATIONS,
    }


def render_plan(plan: Plan) -> str:
    report = plan_report(plan)
    lines = [f"BuildHarbor {__version__}: {plan.action} plan", f"Xcode {plan.xcode.version} ({plan.xcode.build})", "", "Command:", shlex.join(plan.command), "", "Environment changes:"]
    lines.extend(f"  {key}={value}" for key, value in plan.environment.items())
    lines.append("\nPlanned locations (not observed outputs):")
    lines.extend(f"  {key}: {value}" for key, value in report["outputs"].items())
    lines.extend(("", "Write access has not been probed. Planning creates no files and runs no build.", *LIMITATIONS))
    return "\n".join(lines)
