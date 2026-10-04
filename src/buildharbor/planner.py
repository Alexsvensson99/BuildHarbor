"""Construct a reviewable build command without creating files or launching Xcode."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import uuid

from .config import Config
from .errors import BuildHarborError
from .paths import validate_destination
from .volume import Volume, inspect_volume
from .xcode import Xcode, inspect_xcode

VALUE_OPTIONS = {
    "-project", "-scheme", "-configuration", "-destination", "-sdk",
    "-arch", "-jobs", "-destination-timeout", "-parallel-testing-enabled",
    "-parallel-testing-worker-count", "-maximum-parallel-testing-workers",
    "-maximum-concurrent-test-device-destinations", "-maximum-concurrent-test-simulator-destinations",
    "-testPlan", "-enableCodeCoverage", "-testLanguage", "-testRegion",
}
SWITCH_OPTIONS = {"-quiet", "-showBuildTimingSummary", "-disableAutomaticPackageResolution", "-onlyUsePackageVersionsFromResolvedFile", "-skipPackageUpdates"}
BOOLEAN_SETTINGS = {"CODE_SIGNING_ALLOWED", "CODE_SIGNING_REQUIRED", "ONLY_ACTIVE_ARCH", "ENABLE_TESTABILITY"}
MANAGED_SETTINGS = {
    "SYMROOT", "OBJROOT", "DSTROOT", "BUILD_DIR", "BUILD_ROOT", "PROJECT_TEMP_DIR",
    "CONFIGURATION_BUILD_DIR", "CONFIGURATION_TEMP_DIR", "TARGET_BUILD_DIR", "TARGET_TEMP_DIR",
    "DERIVED_DATA_DIR", "DERIVED_FILE_DIR", "DERIVED_FILES_DIR", "CACHE_ROOT", "CCHROOT",
    "SHARED_PRECOMPS_DIR", "MODULE_CACHE_DIR", "CLANG_MODULE_CACHE_PATH", "SWIFT_MODULE_CACHE_PATH",
    "SWIFT_MODULECACHE_PATH", "COMPILATION_CACHE_CAS_PATH", "COMPILATION_CACHE_ENABLE_CACHING",
    "SDK_STAT_CACHE_DIR", "INDEX_DATA_STORE_DIR", "INDEX_PRECOMPS_DIR", "TEMP_DIR", "TMPDIR",
    "OTHER_CFLAGS", "OTHER_CPLUSPLUSFLAGS", "OTHER_SWIFT_FLAGS", "OTHER_LDFLAGS",
}
FORBIDDEN_ENV = MANAGED_SETTINGS - {"TMPDIR"} | {"XCODE_XCCONFIG_FILE", "TOOLCHAINS"}
TEST_ONLY_OPTIONS = {
    "-testPlan", "-enableCodeCoverage", "-testLanguage", "-testRegion",
    "-parallel-testing-enabled", "-parallel-testing-worker-count", "-maximum-parallel-testing-workers",
    "-maximum-concurrent-test-device-destinations", "-maximum-concurrent-test-simulator-destinations",
}


def has_control_characters(value: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069" for c in value)


@dataclass(frozen=True)
class Plan:
    config: Config
    volume: Volume
    xcode: Xcode
    action: str
    command: tuple[str, ...]
    environment: dict[str, str]
    outputs: dict[str, Path]
    directories: tuple[Path, ...]
    run_id: str
    project_storage: Path


def parse_arguments(arguments: list[str], project_root: Path) -> tuple[str, list[str], Path]:
    seen: set[str] = set()
    action = None
    normalized: list[str] = []
    selections: dict[str, str] = {}
    at = 0
    while at < len(arguments):
        arg = arguments[at]
        if not arg or has_control_characters(arg):
            raise BuildHarborError("Empty arguments and control characters are not supported.")
        if arg in {"build", "test"}:
            if action is not None:
                raise BuildHarborError("Choose exactly one action: build or test.")
            action = arg
            normalized.append(arg)
        elif arg in VALUE_OPTIONS:
            if arg in seen:
                raise BuildHarborError(f"Duplicate option: {arg}.")
            if at + 1 >= len(arguments) or not arguments[at + 1] or arguments[at + 1].startswith("-"):
                raise BuildHarborError(f"Missing value for {arg}.")
            at += 1
            value = arguments[at]
            if has_control_characters(value):
                raise BuildHarborError("Option values cannot contain control characters.")
            if arg in {"-project", "-workspace"}:
                path = Path(value)
                try:
                    path = (project_root / path).resolve() if not path.is_absolute() else path.resolve()
                except (OSError, RuntimeError) as exc:
                    raise BuildHarborError("Cannot resolve the selected project path. Check permissions and symlinks.") from exc
                if not path.is_relative_to(project_root) or not path.is_dir():
                    raise BuildHarborError("The selected project or workspace must exist inside the policy directory.")
                if path.suffix != (".xcodeproj" if arg == "-project" else ".xcworkspace"):
                    raise BuildHarborError("The selected input has the wrong project or workspace extension.")
                value = str(path)
            if arg in {"-jobs", "-destination-timeout", "-parallel-testing-worker-count", "-maximum-parallel-testing-workers", "-maximum-concurrent-test-device-destinations", "-maximum-concurrent-test-simulator-destinations"}:
                if not value.isdecimal() or not 1 <= int(value) <= 1024:
                    raise BuildHarborError(f"{arg} needs an integer between 1 and 1024.")
            if arg in {"-parallel-testing-enabled", "-enableCodeCoverage"} and value not in {"YES", "NO"}:
                raise BuildHarborError(f"{arg} needs YES or NO.")
            seen.add(arg)
            selections[arg] = value
            normalized.extend((arg, value))
        elif arg in SWITCH_OPTIONS or arg.startswith(("-only-testing:", "-skip-testing:")):
            if arg.startswith(("-only-testing:", "-skip-testing:")) and not arg.split(":", 1)[1]:
                raise BuildHarborError("Test selectors require a nonempty inline identifier.")
            if arg in seen:
                raise BuildHarborError("Duplicate option.")
            seen.add(arg)
            normalized.append(arg)
        elif "=" in arg and not arg.startswith("-"):
            key, value = arg.split("=", 1)
            if key in seen:
                raise BuildHarborError("Duplicate build setting.")
            if key not in BOOLEAN_SETTINGS or value not in {"YES", "NO"}:
                raise BuildHarborError("Unsupported or conflicting build setting. Version 0.1 accepts only documented boolean settings.")
            seen.add(key)
            normalized.append(arg)
        else:
            # Do not reflect arbitrary arguments: they may contain credentials.
            raise BuildHarborError("Unsupported action or option. Only build/test and the documented argument list are supported; output paths are managed by BuildHarbor.")
        at += 1
    if action is None:
        raise BuildHarborError("Specify exactly one action: build or test.")
    if "-project" not in selections:
        raise BuildHarborError("An explicit -project is required. Workspaces are not supported in version 0.1.")
    if "-scheme" not in selections:
        raise BuildHarborError("An explicit -scheme is required.")
    if action != "test" and (seen & TEST_ONLY_OPTIONS or any(a.startswith(("-only-testing:", "-skip-testing:")) for a in seen)):
        raise BuildHarborError("Test-only options require the test action.")
    identity = Path(selections["-project"])
    return action, normalized, identity


def _inspect_project(identity: Path) -> None:
    """Catch plain project output overrides; this is not an effective-settings parser."""
    try:
        text = (identity / "project.pbxproj").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BuildHarborError("Cannot read the selected project.pbxproj.") from exc
    # Keep comments: comment markers inside quoted strings must never hide settings.
    # Conservative false positives are preferable to an incomplete OpenStep parser.
    for key in MANAGED_SETTINGS:
        if re.search(r'(?:^|[;{\s])"?' + re.escape(key) + r'(?:\[[^\]]*\])?"?\s*=', text):
            raise BuildHarborError(f"The project explicitly sets {key}. Review and remove this output/compiler override before using version 0.1.")
    if re.search(r"\bbaseConfigurationReference\s*=", text):
        raise BuildHarborError("Project xcconfig overrides require an effective-settings review and are not supported in version 0.1.")
    if "wrapper.pb-project" in text or re.search(r'\bpath\s*=\s*[^;]*\.xcodeproj', text):
        raise BuildHarborError("Nested project references require a settings review and are not supported in version 0.1.")


def make_plan(config: Config, arguments: list[str], environment: dict[str, str] | None = None) -> Plan:
    env = os.environ if environment is None else environment
    action, normalized, identity = parse_arguments(arguments, config.project_root)
    if any(env.get(key) for key in FORBIDDEN_ENV):
        raise BuildHarborError("The environment contains output/compiler overrides or XCODE_XCCONFIG_FILE/TOOLCHAINS. Unset them before planning.")
    _inspect_project(identity)
    xcode = inspect_xcode(env)
    volume = inspect_volume(config)
    # Each checkout and Xcode distribution gets its own reusable build directories.
    key = hashlib.sha256(str(identity).encode()).hexdigest()[:16]
    project = config.storage_root / "projects" / f"{config.project_id}-{key}"
    storage = project / f"xcode-{xcode.build}"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
    outputs = {
        "derived_data": storage / "DerivedData",
        "package_clones": storage / "SourcePackages",
        "package_cache": storage / "PackageCache",
        "module_cache": storage / "ModuleCache.noindex",
        "compilation_cache": storage / "CompilationCache.noindex",
        "precompiled_headers": storage / "PrecompiledHeaders",
        "temporary": storage / "Temporary",
        "receipt": storage / "Receipts" / f"{run_id}.json",
        "result_bundle": storage / "Results" / f"{run_id}.xcresult",
    }
    for path in outputs.values():
        validate_destination(path, config, volume)
    directories = tuple(path.parent if name in {"receipt", "result_bundle"} else path for name, path in outputs.items())
    for directory in directories:
        if directory.exists() and not directory.is_dir():
            raise BuildHarborError("A planned output directory is occupied by a file.")
    for name in ("receipt", "result_bundle"):
        if name in outputs and outputs[name].exists():
            raise BuildHarborError("A supposedly unique run output already exists.")
    command = [str(xcode.executable), *normalized, "-hideShellScriptEnvironment"]
    for flag, name in (("-derivedDataPath", "derived_data"), ("-clonedSourcePackagesDirPath", "package_clones"), ("-packageCachePath", "package_cache"), ("-resultBundlePath", "result_bundle")):
        if name in outputs:
            command.extend((flag, str(outputs[name])))
    settings = {
        "SYMROOT": outputs["derived_data"] / "Build/Products",
        "OBJROOT": outputs["derived_data"] / "Build/Intermediates.noindex",
        "DSTROOT": storage / "Install",
        "SHARED_PRECOMPS_DIR": outputs["precompiled_headers"],
        "MODULE_CACHE_DIR": outputs["module_cache"],
        "CLANG_MODULE_CACHE_PATH": outputs["module_cache"],
        "COMPILATION_CACHE_CAS_PATH": outputs["compilation_cache"],
    }
    command.extend(f"{key}={value}" for key, value in settings.items())
    changes = {"DEVELOPER_DIR": str(xcode.developer_dir), "TMPDIR": str(outputs["temporary"]) + "/"}
    return Plan(config, volume, xcode, action, tuple(command), changes, outputs, directories, run_id, storage)
