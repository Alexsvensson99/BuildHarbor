"""Validate destinations and create directories relative to a verified mount fd."""

import os
from pathlib import Path
import stat

from .config import Config
from .errors import BuildHarborError
from .volume import Volume


def validate_destination(path: Path, config: Config, volume: Volume) -> Path:
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(config.storage_root):
        raise BuildHarborError("An output path is outside the configured storage root.")
    try:
        if path.resolve() != path or not path.resolve().is_relative_to(config.mount):
            raise BuildHarborError("An output path contains a symlink. Managed paths must use real directories.")
        current = config.mount
        for part in path.relative_to(config.mount).parts:
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                break
            if stat.S_ISLNK(metadata.st_mode):
                raise BuildHarborError("An existing output path component is a symlink.")
            if metadata.st_dev != volume.device_id:
                raise BuildHarborError("An output path crosses onto another filesystem.")
            if current != path and not stat.S_ISDIR(metadata.st_mode):
                raise BuildHarborError("An output path has a non-directory parent.")
            if stat.S_ISDIR(metadata.st_mode) and not os.access(current, os.W_OK | os.X_OK):
                raise BuildHarborError("Permission metadata denies access to an output directory.")
    except (OSError, RuntimeError) as exc:
        raise BuildHarborError("Cannot resolve an output path. Check permissions and symlinks.") from exc
    return path


def open_directory(path: Path, config: Config, volume: Volume, create: bool = False) -> int:
    """Return a caller-owned fd; never create the mount itself or follow links."""
    validate_destination(path, config, volume)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = None
    try:
        fd = os.open(config.mount, flags)
        if os.fstat(fd).st_dev != volume.device_id or not config.mount.is_mount():
            raise BuildHarborError("The volume changed before directory creation.")
        for part in path.relative_to(config.mount).parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            if os.fstat(fd).st_dev != volume.device_id:
                raise BuildHarborError("An output directory crosses onto another filesystem.")
        result, fd = fd, None
        return result
    except OSError as exc:
        raise BuildHarborError("Cannot open or create an output directory on the verified volume.") from exc
    finally:
        if fd is not None:
            os.close(fd)
