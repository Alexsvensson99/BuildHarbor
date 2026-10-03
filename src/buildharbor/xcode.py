"""Identify the selected Xcode without running builds or resolving packages."""

from dataclasses import dataclass
import os
from pathlib import Path
import plistlib
import subprocess

from .errors import BuildHarborError

# Capability contract audited against this distribution's help and compiler specs.
# Add another distribution only after its real integration evidence is recorded.
VERIFIED_XCODE_BUILDS = {("27.0", "27A266a")}


@dataclass(frozen=True)
class Xcode:
    developer_dir: Path
    version: str
    build: str
    executable: Path


def inspect_xcode(environment: dict[str, str] | None = None) -> Xcode:
    env = os.environ if environment is None else environment
    selected = env.get("DEVELOPER_DIR")
    if not selected:
        try:
            selected = subprocess.check_output(["/usr/bin/xcode-select", "-p"], text=True, timeout=10).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise BuildHarborError("Cannot find the selected Xcode. Select a full Xcode installation first.") from exc
    try:
        developer = Path(selected).resolve(strict=True)
        if developer.suffix == ".app":
            developer /= "Contents/Developer"
        metadata = plistlib.loads((developer.parent / "version.plist").read_bytes())
        version, build = metadata["CFBundleShortVersionString"], metadata["ProductBuildVersion"]
        if not isinstance(version, str) or not isinstance(build, str) or not version or not build:
            raise ValueError("Invalid Xcode version metadata")
        executable = developer / "usr/bin/xcodebuild"
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("Missing xcodebuild")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, plistlib.InvalidFileException) as exc:
        raise BuildHarborError("The selected developer directory is not a readable full Xcode installation.") from exc
    if (version, build) not in VERIFIED_XCODE_BUILDS:
        raise BuildHarborError(f"Xcode {version} ({build}) has not been verified by this BuildHarbor release. See the compatibility record.")
    return Xcode(developer, version, build, executable)
