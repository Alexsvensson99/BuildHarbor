"""Construct a reviewable build command without creating files or launching Xcode."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import uuid

from .config import Config
from .errors import BuildHarborError
from .paths import validate_destination
from .volume import Volume, inspect_volume
from .xcode import Xcode, inspect_xcode
from .settings import PATH_SETTINGS, STATIC_ONLY_SETTINGS, SIGNING_ENV_SETTINGS

VALUE_OPTIONS = {
    "-project", "-workspace", "-scheme", "-configuration", "-destination", "-sdk",
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
MANAGED_SETTINGS.update(PATH_SETTINGS)
MANAGED_SETTINGS.update(STATIC_ONLY_SETTINGS)
FORBIDDEN_ENV = MANAGED_SETTINGS - {"TMPDIR"} | {"XCODE_XCCONFIG_FILE", "TOOLCHAINS"} | SIGNING_ENV_SETTINGS
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
    graph: object | None = None
    settings_command: tuple[str, ...] = ()
    unique_outputs: tuple[str, ...] = ("receipt", "result_bundle")
    source_identity: Path | None = None
    inputs: dict[str, Path] = field(default_factory=dict)
    input_hashes: dict[str, str] = field(default_factory=dict)
    member_settings_commands: tuple[tuple[str, ...], ...] = ()


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
        if arg in {"build", "test", "archive"}:
            if action is not None:
                raise BuildHarborError("Choose exactly one action: build, test, or archive.")
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
                raise BuildHarborError("Unsupported or conflicting build setting. Only documented boolean settings are accepted.")
            seen.add(key)
            normalized.append(arg)
        else:
            # Do not reflect arbitrary arguments: they may contain credentials.
            raise BuildHarborError("Unsupported action or option. Use a documented build/test/archive command; output paths are managed by BuildHarbor.")
        at += 1
    if action is None:
        raise BuildHarborError("Specify exactly one action: build, test, or archive.")
    if ("-project" in selections) == ("-workspace" in selections):
        raise BuildHarborError("Choose exactly one explicit -project or -workspace.")
    if "-scheme" not in selections:
        raise BuildHarborError("An explicit -scheme is required.")
    if action != "test" and (seen & TEST_ONLY_OPTIONS or any(a.startswith(("-only-testing:", "-skip-testing:")) for a in seen)):
        raise BuildHarborError("Test-only options require the test action.")
    identity = Path(selections.get("-project", selections.get("-workspace")))
    return action, normalized, identity


def make_plan(config: Config, arguments: list[str], environment: dict[str, str] | None = None) -> Plan:
    env = os.environ if environment is None else environment
    if arguments and arguments[0] == "-exportArchive":
        return make_export_plan(config, arguments, env)
    action, normalized, identity = parse_arguments(arguments, config.project_root)
    if any(env.get(key) for key in FORBIDDEN_ENV):
        raise BuildHarborError("The environment contains output/compiler overrides or XCODE_XCCONFIG_FILE/TOOLCHAINS. Unset them before planning.")
    from .projects import inspect_projects
    graph = inspect_projects(identity, config.project_root, MANAGED_SETTINGS)
    scheme = normalized[normalized.index("-scheme") + 1]
    if Path(scheme).name != scheme or scheme in {".", ".."}:
        raise BuildHarborError("The selected scheme must be a literal shared scheme name.")
    if any(graph.targets.values()) and not any(Path(path).name == scheme + ".xcscheme" for path in graph.fingerprints):
        raise BuildHarborError("Select a statically inspected shared scheme. User and autogenerated schemes are unsupported.")
    if len(graph.projects) > 1 and not {"-configuration", "-sdk"}.issubset(normalized):
        raise BuildHarborError("Workspace and nested-project runs require explicit -configuration and -sdk for every member's settings inspection.")
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
        "cache_root": storage / "Cache",
        "sdk_stat_cache": storage / "SDKStatCaches.noindex",
        "temporary": storage / "Temporary",
        "receipt": storage / "Receipts" / f"{run_id}.json",
        "result_bundle": storage / "Results" / f"{run_id}.xcresult",
        "settings_result_bundle": storage / "Results" / f"{run_id}-settings.xcresult",
    }
    if action == "archive":
        outputs["archive"] = storage / "Archives" / f"{run_id}.xcarchive"
        archive_work = outputs["derived_data"] / "Build/Intermediates.noindex/ArchiveIntermediates" / scheme
        outputs["archive_staging"] = archive_work / "InstallationBuildProductsLocation"
        outputs["archive_products"] = archive_work / "BuildProductsPath"
        outputs["archive_intermediates"] = archive_work / "IntermediateBuildFilesPath"
    if len(graph.projects) > 1:
        for index in range(len(graph.projects)):
            outputs[f"member_settings_result_{index}"] = storage / "Results" / f"{run_id}-member-{index}.xcresult"
    unique = tuple(name for name in outputs if name in {"receipt", "result_bundle", "settings_result_bundle", "archive"} or name.startswith("member_settings_result_"))
    for path in outputs.values():
        validate_destination(path, config, volume)
    directories = tuple(dict.fromkeys(path.parent if name in unique else path for name, path in outputs.items()))
    for directory in directories:
        if directory.exists() and not directory.is_dir():
            raise BuildHarborError("A planned output directory is occupied by a file.")
    for name in unique:
        if name in outputs and outputs[name].exists():
            raise BuildHarborError("A supposedly unique run output already exists.")
    command = [str(xcode.executable), *normalized, "-hideShellScriptEnvironment"]
    for flag, name in (("-derivedDataPath", "derived_data"), ("-clonedSourcePackagesDirPath", "package_clones"), ("-packageCachePath", "package_cache"), ("-resultBundlePath", "result_bundle"), ("-archivePath", "archive")):
        if name in outputs:
            command.extend((flag, str(outputs[name])))
    settings = {
        "SYMROOT": outputs["derived_data"] / "Build/Products",
        "OBJROOT": outputs["derived_data"] / "Build/Intermediates.noindex",
        "DSTROOT": storage / "Install",
        "SHARED_PRECOMPS_DIR": outputs["precompiled_headers"],
        "CACHE_ROOT": outputs["cache_root"],
        "CCHROOT": outputs["cache_root"],
        "SDK_STAT_CACHE_DIR": outputs["sdk_stat_cache"],
        "MODULE_CACHE_DIR": outputs["module_cache"],
        "CLANG_MODULE_CACHE_PATH": outputs["module_cache"],
        "COMPILATION_CACHE_CAS_PATH": outputs["compilation_cache"],
    }
    if action == "archive":
        # Archive packaging expects Xcode's action-specific directory structure.
        # Let -derivedDataPath establish it, then check every resolved root before
        # the archive action. Generic build roots break Xcode's packaging step.
        for key in ("SYMROOT", "OBJROOT", "DSTROOT"):
            settings.pop(key)
    command.extend(f"{key}={value}" for key, value in settings.items())
    changes = {"DEVELOPER_DIR": str(xcode.developer_dir), "TMPDIR": str(outputs["temporary"]) + "/"}
    query = command.copy()
    query[query.index("-resultBundlePath") + 1] = str(outputs["settings_result_bundle"])
    query.extend(("-showBuildSettings", "-json"))
    members = []
    if len(graph.projects) > 1:
        for index, member in enumerate(graph.projects):
            member_query = [str(xcode.executable), "-project", str(member), "-alltargets"]
            for flag in ("-configuration", "-sdk", "-arch", "-jobs"):
                if flag in normalized:
                    member_query.extend((flag, normalized[normalized.index(flag) + 1]))
            member_query.extend(a for a in normalized if a in SWITCH_OPTIONS or "=" in a and a.split("=", 1)[0] in BOOLEAN_SETTINGS)
            for flag, name in (("-clonedSourcePackagesDirPath", "package_clones"), ("-packageCachePath", "package_cache"), ("-resultBundlePath", f"member_settings_result_{index}")):
                member_query.extend((flag, str(outputs[name])))
            member_query.extend(f"{key}={value}" for key, value in settings.items())
            if action == "archive":
                member_query.extend(f"{key}={outputs[name]}" for key, name in (("SYMROOT", "archive_products"), ("OBJROOT", "archive_intermediates"), ("DSTROOT", "archive_staging")))
            member_query.extend((action, "-showBuildSettings", "-json", "-hideShellScriptEnvironment"))
            members.append(tuple(member_query))
    return Plan(config, volume, xcode, action, tuple(command), changes, outputs, directories, run_id, storage,
                graph=graph, settings_command=tuple(query), unique_outputs=unique, source_identity=identity,
                member_settings_commands=tuple(members))


def make_export_plan(config: Config, arguments: list[str], env: dict[str, str]) -> Plan:
    """Only local Copy App export of a successful, unchanged managed archive."""
    from .artifacts import inspect_archive_input
    if len(arguments) != 3 or arguments[:2] != ["-exportArchive", "-archivePath"]:
        raise BuildHarborError("Use -exportArchive -archivePath ARCHIVE. Local mac-application export options and its output path are managed by BuildHarbor.")
    if any(env.get(key) for key in FORBIDDEN_ENV):
        raise BuildHarborError("Unset inherited output/compiler overrides before exporting.")
    if has_control_characters(arguments[2]):
        raise BuildHarborError("Archive paths cannot contain control characters.")
    archive = Path(arguments[2])
    xcode, volume = inspect_xcode(env), inspect_volume(config)
    storage, identity, receipt, fingerprint, receipt_hash = inspect_archive_input(archive, config, volume, xcode)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
    outputs = {"export": storage / "Exports" / run_id,
               "export_options": storage / "ExportOptions" / f"{run_id}.plist",
               "receipt": storage / "Receipts" / f"{run_id}.json",
               "temporary": storage / "Temporary"}
    unique = ("export", "export_options", "receipt")
    for name, path in outputs.items():
        validate_destination(path, config, volume)
        if name in unique and path.exists():
            raise BuildHarborError("A unique export output already exists; no output will be overwritten.")
        if name not in unique and path.exists() and not path.is_dir():
            raise BuildHarborError("An export directory is occupied by a file.")
    directories = tuple(dict.fromkeys(path.parent if name in unique else path for name, path in outputs.items()))
    command = (str(xcode.executable), "-exportArchive", "-archivePath", str(archive),
               "-exportPath", str(outputs["export"]), "-exportOptionsPlist", str(outputs["export_options"]))
    environment = {"DEVELOPER_DIR": str(xcode.developer_dir), "TMPDIR": str(outputs["temporary"]) + "/"}
    return Plan(config, volume, xcode, "export", command, environment, outputs, directories, run_id, storage,
                unique_outputs=unique, source_identity=identity,
                inputs={"archive": archive, "archive_receipt": receipt},
                input_hashes={"archive": fingerprint, "archive_receipt": receipt_hash})
