"""Read and fingerprint local archive inputs without trusting arbitrary receipts."""

import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import time

from .errors import BuildHarborError
from .paths import open_directory, validate_destination

EXPORT_OPTIONS = {"method": "mac-application", "destination": "export"}
EXPORT_OPTIONS_BYTES = plistlib.dumps(EXPORT_OPTIONS, sort_keys=True)


def _read_plist(path, config, volume):
    validate_destination(path, config, volume)
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_dev != volume.device_id or info.st_size > 1024 * 1024:
            raise ValueError("Invalid archive plist")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            value = plistlib.loads(stream.read(1024 * 1024 + 1))
        if not isinstance(value, dict):
            raise ValueError("Invalid archive plist")
        return value
    except (OSError, ValueError, plistlib.InvalidFileException, RecursionError) as exc:
        raise BuildHarborError("The archive application metadata is unreadable or malformed.") from exc
    finally:
        if fd is not None:
            os.close(fd)


def inspect_archived_app(archive, config, volume):
    """Require one concrete macOS application, not merely an archive-shaped folder."""
    info = _read_plist(archive / "Info.plist", config, volume)
    properties = info.get("ApplicationProperties")
    if not isinstance(properties, dict):
        raise BuildHarborError("The archive has no macOS application properties.")
    relative = properties.get("ApplicationPath")
    if not isinstance(relative, str) or len(Path(relative).parts) != 2 or Path(relative).parts[0] != "Applications" or Path(relative).suffix != ".app":
        raise BuildHarborError("The archive does not identify a supported local macOS application.")
    application = archive / "Products" / relative
    validate_destination(application, config, volume)
    try:
        apps = list((archive / "Products/Applications").iterdir())
        if apps != [application] or not application.is_dir():
            raise BuildHarborError("Local archive/export support requires exactly one macOS application.")
        app_info = _read_plist(application / "Contents/Info.plist", config, volume)
        bundle_id, executable = app_info.get("CFBundleIdentifier"), app_info.get("CFBundleExecutable")
        if not isinstance(bundle_id, str) or not bundle_id or any(ord(c) < 32 for c in bundle_id):
            raise BuildHarborError("The archived application has no valid bundle identifier.")
        if not isinstance(executable, str) or not executable or Path(executable).name != executable or executable in {".", ".."}:
            raise BuildHarborError("The archived application has no valid executable name.")
        binary = application / "Contents/MacOS" / executable
        validate_destination(binary, config, volume)
        metadata = binary.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size == 0:
            raise BuildHarborError("The archived application executable is missing or empty.")
        if properties.get("CFBundleIdentifier") != bundle_id:
            raise BuildHarborError("The archive and application bundle identifiers disagree.")
    except OSError as exc:
        raise BuildHarborError("The archived application is incomplete or unreadable.") from exc
    return {"relative_path": relative, "bundle_identifier": bundle_id, "executable": executable}


def archive_digest(path, config, volume, *, max_entries=100000, max_bytes=4 * 1024**3, timeout=60):
    """Hash a bounded archive tree; reject symlinks rather than follow export inputs."""
    digest = hashlib.sha256()
    started = time.monotonic()
    count = 0
    byte_count = 0

    def visit(fd, relative):
        nonlocal count, byte_count
        before = os.fstat(fd)
        with os.scandir(fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            count += 1
            if count > max_entries or time.monotonic() - started > timeout:
                raise BuildHarborError("The archive exceeds the bounded input inspection limit.")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_dev != volume.device_id:
                raise BuildHarborError("The archive crosses onto another filesystem.")
            child_name = relative + "/" + name
            digest.update(child_name.encode("utf-8") + b"\0" + str(info.st_mode).encode() + b"\0")
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if (os.fstat(child).st_dev, os.fstat(child).st_ino) != (info.st_dev, info.st_ino):
                        raise BuildHarborError("An archive directory changed during inspection.")
                    visit(child, child_name)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                byte_count += info.st_size
                if byte_count > max_bytes or info.st_nlink != 1:
                    raise BuildHarborError("The archive is too large or contains multiply linked files.")
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino, opened.st_mode) != (info.st_dev, info.st_ino, info.st_mode):
                        raise BuildHarborError("An archive file changed during inspection.")
                    size = 0
                    while True:
                        if time.monotonic() - started > timeout:
                            raise BuildHarborError("Archive inspection timed out.")
                        chunk = os.read(child, 1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > info.st_size:
                            raise BuildHarborError("An archive file grew during inspection.")
                        digest.update(chunk)
                    after = os.fstat(child)
                    if size != info.st_size or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (info.st_size, info.st_mtime_ns, info.st_ctime_ns):
                        raise BuildHarborError("An archive file changed during inspection.")
                finally:
                    os.close(child)
            else:
                raise BuildHarborError("Local export currently requires an archive without symlinks or special files.")
        after = os.fstat(fd)
        if (after.st_mtime_ns, after.st_ctime_ns) != (before.st_mtime_ns, before.st_ctime_ns):
            raise BuildHarborError("The archive directory changed during inspection.")

    fd = open_directory(path, config, volume)
    try:
        visit(fd, "")
    except (OSError, UnicodeError, RecursionError) as exc:
        raise BuildHarborError("Cannot safely inspect the local archive input.") from exc
    finally:
        os.close(fd)
    return digest.hexdigest()


def read_receipt(path, config, volume):
    validate_destination(path, config, volume)
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024 or info.st_dev != volume.device_id:
            raise ValueError("Invalid receipt input")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            data = stream.read(1024 * 1024 + 1)
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("Invalid receipt structure")
        return result, hashlib.sha256(data).hexdigest()
    except (OSError, ValueError, RecursionError) as exc:
        raise BuildHarborError("Export requires a readable, bounded BuildHarbor archive receipt.") from exc
    finally:
        if fd is not None:
            os.close(fd)


def inspect_archive_input(archive: Path, config, volume, xcode):
    validate_destination(archive, config, volume)
    if archive.suffix != ".xcarchive" or archive.parent.name != "Archives" or not archive.is_dir():
        raise BuildHarborError("Export needs an existing BuildHarbor archive under its managed Archives directory.")
    storage = archive.parent.parent
    receipt_path = storage / "Receipts" / f"{archive.stem}.json"
    receipt, receipt_hash = read_receipt(receipt_path, config, volume)
    source = receipt.get("source_identity")
    if not isinstance(source, str) or not Path(source).is_absolute() or ".." in Path(source).parts or not Path(source).is_relative_to(config.project_root):
        raise BuildHarborError("The archive receipt does not belong to this configured source project.")
    key = hashlib.sha256(source.encode()).hexdigest()[:16]
    expected = config.storage_root / "projects" / f"{config.project_id}-{key}" / f"xcode-{xcode.build}"
    if storage != expected or receipt.get("project_id") != config.project_id:
        raise BuildHarborError("The archive storage identity does not match this project configuration.")
    if (receipt.get("schema_version"), receipt.get("kind"), receipt.get("action"), receipt.get("result"), receipt.get("exit_code")) != (1, "run_receipt", "archive", "succeeded", 0):
        raise BuildHarborError("Only a successfully recorded BuildHarbor archive can be exported.")
    paths = receipt.get("planned_paths")
    if not isinstance(paths, dict) or receipt.get("run_id") != archive.stem or paths.get("archive") != str(archive) or receipt.get("xcode") != {"version": xcode.version, "build": xcode.build}:
        raise BuildHarborError("The archive receipt does not match the selected archive and Xcode.")
    fingerprint = archive_digest(archive, config, volume)
    if receipt.get("archive_sha256") != fingerprint:
        raise BuildHarborError("The archive differs from its successful run receipt. Export was rejected.")
    if receipt.get("archive_application") != inspect_archived_app(archive, config, volume):
        raise BuildHarborError("The archived application does not match its successful receipt.")
    return storage, Path(source), receipt_path, fingerprint, receipt_hash
