"""Read macOS volume identity. Never mount a disk or create a mount point."""

from dataclasses import dataclass
import os
import plistlib
import subprocess
import sys

from .config import Config
from .errors import BuildHarborError


@dataclass(frozen=True)
class Volume:
    device_id: int
    available_bytes: int
    uuid: str
    filesystem: str = "apfs"
    permission_metadata: bool = True


def inspect_volume(config: Config) -> Volume:
    if sys.platform != "darwin":
        raise BuildHarborError("BuildHarbor requires macOS and a mounted external APFS volume.")
    mount = config.mount
    if mount.is_symlink() or not mount.is_mount() or mount.resolve() != mount:
        raise BuildHarborError("The configured volume is absent or its mount path changed. Connect and unlock it; no fallback is used.")
    try:
        before = mount.stat().st_dev
        result = subprocess.run(
            ["/usr/sbin/diskutil", "info", "-plist", str(mount)],
            capture_output=True, check=True, timeout=15,
        )
        disk = plistlib.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, plistlib.InvalidFileException) as exc:
        raise BuildHarborError("Cannot inspect the mounted volume with diskutil. Check disk access permissions.") from exc
    if not isinstance(disk, dict):
        raise BuildHarborError("diskutil returned an invalid volume record.")
    if disk.get("MountPoint") != str(mount) or str(disk.get("VolumeUUID", "")).upper() != config.volume_uuid:
        raise BuildHarborError("The mounted volume UUID or mount point does not match local configuration.")
    if disk.get("FilesystemType") != "apfs" or disk.get("Internal") is not False:
        raise BuildHarborError("The destination must be an external APFS volume.")
    if disk.get("Locked", False) or disk.get("ReadOnlyVolume", False) or disk.get("WritableVolume") is not True:
        raise BuildHarborError("The configured APFS volume is locked or read-only.")
    try:
        info = os.statvfs(mount)
        available = info.f_bavail * info.f_frsize
        if mount.stat().st_dev != before or not mount.is_mount():
            raise BuildHarborError("The destination changed during volume inspection.")
    except OSError as exc:
        raise BuildHarborError("The destination became unavailable during volume inspection.") from exc
    if available < config.minimum_free_bytes:
        raise BuildHarborError("The destination has less available capacity than minimum_free_gib requires.")
    if not os.access(mount, os.W_OK | os.X_OK):
        raise BuildHarborError("Permission metadata does not allow writing and traversal on the volume.")
    return Volume(before, available, config.volume_uuid)
