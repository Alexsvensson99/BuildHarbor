#!/usr/bin/env python3
"""Check publishable files and Git history without printing matched private data."""

from pathlib import Path
import re
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def main():
    names = set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")) - {""}
    private = [str(Path.home()).encode()]
    local = ROOT / ".buildharbor.local.toml"
    if local.exists():
        settings = tomllib.loads(local.read_text())
        private.extend(str(settings[key]).encode() for key in ("mount", "volume_uuid", "storage_root"))
    patterns = [
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(rb"gh[pousr]_" + rb"[A-Za-z0-9]{30,}"),
        re.compile(rb"github_pat_" + rb"[A-Za-z0-9_]{40,}"),
        re.compile(rb"AKIA" + rb"[A-Z0-9]{16}"),
    ]
    problems = []
    banned = {".buildharbor.local.toml", ".DS_Store", ".env", "__pycache__", ".venv", "DerivedData", "xcuserdata", "work"}
    for name in sorted(names):
        path = ROOT / name
        if any(part in banned or part.endswith((".xcresult", ".egg-info")) for part in path.relative_to(ROOT).parts):
            problems.append(f"Unexpected publishable path: {name}")
            continue
        if path.is_symlink():
            problems.append(f"Review publishable symlink: {name}")
            continue
        content = path.read_bytes()
        if any(value in content for value in private) or any(pattern.search(content) for pattern in patterns):
            problems.append(f"Private data or credential pattern in: {name}")
    refs = git("for-each-ref", "--format=%(refname)").decode().splitlines()
    if refs:
        history = git("log", "--all", "--format=fuller", "-p", "--no-ext-diff")
        if any(value in history for value in private) or any(pattern.search(history) for pattern in patterns):
            problems.append("Private data or credential pattern in Git history.")
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print(f"Publication checks passed for {len(names)} files and reachable Git history. Manual review is still required.")
    return bool(problems)


if __name__ == "__main__":
    raise SystemExit(main())
