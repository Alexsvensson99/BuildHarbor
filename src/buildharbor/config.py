"""Parse portable policy separately from private machine configuration."""

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
import uuid

from .errors import BuildHarborError


@dataclass(frozen=True)
class Config:
    project_root: Path
    project_id: str
    mount: Path
    storage_root: Path
    volume_uuid: str
    minimum_free_bytes: int = 2 * 1024**3


def _read(path: Path, keys: set[str]) -> dict:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildHarborError(f"Cannot read {path.name}; check its TOML syntax and permissions.") from exc
    if set(data) - keys:
        raise BuildHarborError(f"Unknown keys in {path.name}. Check the configuration example.")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise BuildHarborError(f"{path.name} must declare schema_version = 1.")
    return data


def _absolute(value: object, key: str) -> Path:
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 or c in "$~\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069" for c in value):
        raise BuildHarborError(f"{key} must be a literal absolute path, without shell variables.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise BuildHarborError(f"{key} must be absolute and cannot contain '..'.")
    return path


def load_config(project_root: Path) -> Config:
    try:
        project_root = project_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BuildHarborError("Cannot resolve the configuration directory. Check permissions and symlinks.") from exc
    policy = _read(project_root / "buildharbor.toml", {"schema_version", "project_id", "minimum_free_gib"})
    local = _read(project_root / ".buildharbor.local.toml", {"schema_version", "mount", "volume_uuid", "storage_root"})
    project_id = policy.get("project_id")
    if not isinstance(project_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", project_id):
        raise BuildHarborError("project_id must contain 1-64 ASCII letters, digits, underscores, or hyphens.")
    minimum = policy.get("minimum_free_gib", 2)
    if type(minimum) is not int or not 1 <= minimum <= 65536:
        raise BuildHarborError("minimum_free_gib must be an integer between 1 and 65536.")
    try:
        volume_uuid = str(uuid.UUID(local["volume_uuid"])).upper()
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise BuildHarborError("volume_uuid must be the APFS volume UUID from diskutil info.") from exc
    mount = _absolute(local.get("mount"), "mount")
    storage = _absolute(local.get("storage_root"), "storage_root")
    if mount.parent != Path("/Volumes"):
        raise BuildHarborError("mount must be a volume directly under /Volumes.")
    if storage == mount or not storage.is_relative_to(mount):
        raise BuildHarborError("storage_root must be a directory below the configured mount.")
    return Config(project_root, project_id, mount, storage, volume_uuid, minimum * 1024**3)
