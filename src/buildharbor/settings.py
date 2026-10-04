"""Bounded effective-setting inspection during guarded execution, never planning."""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import TYPE_CHECKING

from .errors import BuildHarborError
from .paths import validate_destination

if TYPE_CHECKING:
    from .planner import Plan


# These are generated output locations, not input or search paths. Apple documents
# OBJECT_FILE_DIR_<VARIANT> as a dynamically named setting, so it is matched at
# runtime while its base setting remains available to static source inspection.
DIRECTORY_PATH_SETTINGS = frozenset({
    "SYMROOT", "OBJROOT", "DSTROOT", "BUILD_DIR", "BUILD_ROOT",
    "PROJECT_TEMP_DIR", "CONFIGURATION_BUILD_DIR", "CONFIGURATION_TEMP_DIR",
    "TARGET_BUILD_DIR", "TARGET_TEMP_DIR", "DERIVED_DATA_DIR", "DERIVED_FILE_DIR",
    "DERIVED_FILES_DIR", "CACHE_ROOT", "CCHROOT", "SHARED_PRECOMPS_DIR",
    "MODULE_CACHE_DIR", "CLANG_MODULE_CACHE_PATH", "SWIFT_MODULE_CACHE_PATH",
    "SWIFT_MODULECACHE_PATH", "COMPILATION_CACHE_CAS_PATH", "SDK_STAT_CACHE_DIR",
    "INDEX_DATA_STORE_DIR", "INDEX_PRECOMPS_DIR", "TEMP_DIR", "DWARF_DSYM_FOLDER_PATH",
    "BUILT_PRODUCTS_DIR", "INSTALL_DIR", "PROJECT_DERIVED_FILE_DIR", "PROJECT_TEMP_ROOT",
    "OBJECT_FILE_DIR", "CLASS_FILE_DIR", "DERIVED_SOURCES_DIR", "SHARED_DERIVED_FILE_DIR",
    "TEMP_FILE_DIR", "UNINSTALLED_PRODUCTS_DIR", "GENERATED_MODULEMAP_DIR", "INSTALL_ROOT",
    "REZ_COLLECTOR_DIR", "REZ_OBJECTS_DIR", "STRINGSDATA_DIR",
})
FILE_PATH_SETTINGS = frozenset({
    "FILE_LIST", "LD_MAP_FILE_PATH", "LD_DEPENDENCY_INFO_FILE",
    "SWIFT_DEPENDENCY_INFO_FILE", "PROCESSED_INFOPLIST_PATH",
})
PATH_SETTINGS = DIRECTORY_PATH_SETTINGS | FILE_PATH_SETTINGS
DYNAMIC_DIRECTORY_PREFIXES = ("OBJECT_FILE_DIR_",)
DYNAMIC_FILE_PREFIXES = ("LINK_FILE_LIST_",)

# These settings must not be defined by source metadata. CODE_SIGN_IDENTITY and
# CODE_SIGN_STYLE stay out of this set because an ordinary project may select
# explicit manual ad-hoc signing; the resolved values are checked below.
STATIC_ONLY_SETTINGS = frozenset({
    "CODE_SIGN_KEYCHAIN", "OTHER_CODE_SIGN_FLAGS",
    "EXPANDED_CODE_SIGN_IDENTITY", "EXPANDED_CODE_SIGN_IDENTITY_NAME",
    "SWIFT_STDLIB_TOOL_CODE_SIGN_IDENTITY", "SWIFT_STDLIB_TOOL_KEYCHAIN",
    "SWIFT_STDLIB_TOOL_OTHER_CODE_SIGN_FLAGS",
})

# Inherited build-setting selectors are not part of the reviewed command. The
# planner/executor union this set into their forbidden environment policy.
SIGNING_ENV_SETTINGS = frozenset({
    "CODE_SIGN_IDENTITY", "CODE_SIGN_STYLE", "CODE_SIGNING_ALLOWED",
    "CODE_SIGNING_REQUIRED", "CODE_SIGN_KEYCHAIN", "OTHER_CODE_SIGN_FLAGS",
    "EXPANDED_CODE_SIGN_IDENTITY", "EXPANDED_CODE_SIGN_IDENTITY_NAME",
    "DEVELOPMENT_TEAM", "PROVISIONING_PROFILE", "PROVISIONING_PROFILE_SPECIFIER",
    "SWIFT_STDLIB_TOOL_CODE_SIGN_IDENTITY", "SWIFT_STDLIB_TOOL_KEYCHAIN",
    "SWIFT_STDLIB_TOOL_OTHER_CODE_SIGN_FLAGS",
})
REQUIRED_SETTINGS = frozenset({"SYMROOT", "OBJROOT", "BUILD_DIR", "TARGET_BUILD_DIR", "TARGET_TEMP_DIR"})
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
QUERY_TIMEOUT = 120.0
_BIDI_CONTROLS = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"


class SettingsError(BuildHarborError):
    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


def _unsafe_text(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 or char in _BIDI_CONTROLS for char in value)


def _is_output_path_setting(name: str) -> bool:
    return (
        name in PATH_SETTINGS
        or name.startswith(DYNAMIC_DIRECTORY_PREFIXES)
        or name.startswith(DYNAMIC_FILE_PREFIXES)
    )


def _validate_archive_signing(settings: dict[str, object]) -> None:
    if settings.get("PLATFORM_NAME") != "macosx":
        raise SettingsError("Archive support is currently limited to verified local macOS workflows.")
    if any(settings.get(key) for key in (
        "CODE_SIGN_KEYCHAIN", "OTHER_CODE_SIGN_FLAGS", "DEVELOPMENT_TEAM",
        "PROVISIONING_PROFILE", "PROVISIONING_PROFILE_SPECIFIER",
        "SWIFT_STDLIB_TOOL_KEYCHAIN", "SWIFT_STDLIB_TOOL_OTHER_CODE_SIGN_FLAGS",
    )):
        raise SettingsError("Signing keychains, extra flags, teams, and provisioning profiles are outside local archive support.")
    expanded = settings.get("EXPANDED_CODE_SIGN_IDENTITY", "")
    if not isinstance(expanded, str) or expanded not in {"", "-"}:
        raise SettingsError("The effective archive signing identity is not unsigned or ad-hoc.")
    expanded_name = settings.get("EXPANDED_CODE_SIGN_IDENTITY_NAME", "")
    if not isinstance(expanded_name, str) or expanded_name not in {"", "-"}:
        raise SettingsError("The effective archive signing identity name is not unsigned or ad-hoc.")
    runtime_identity = settings.get("SWIFT_STDLIB_TOOL_CODE_SIGN_IDENTITY", "")
    if not isinstance(runtime_identity, str) or runtime_identity not in {"", "-"}:
        raise SettingsError("The Swift runtime signing identity is not unsigned or ad-hoc.")
    if settings.get("CODE_SIGNING_ALLOWED") == "NO":
        return
    if settings.get("CODE_SIGN_IDENTITY") != "-" or settings.get("CODE_SIGN_STYLE") != "Manual":
        raise SettingsError("Archives require disabled signing or manual ad-hoc signing with identity '-'.")


def capture_settings(command: tuple[str, ...], cwd: Path, environment: dict[str, str], lock_fd: int,
                     *, timeout: float = QUERY_TIMEOUT, max_bytes: int = MAX_OUTPUT_BYTES) -> bytes:
    """Keep potentially sensitive settings in bounded memory and reap every child."""
    child = None
    previous = {}
    interrupted = []
    output = bytearray()
    completed = False

    def forward(signum, _frame):
        interrupted.append(signum)
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            old = signal.getsignal(signum)
            try:
                signal.signal(signum, forward)
            except ValueError:
                break
            previous[signum] = old
        try:
            child = subprocess.Popen(list(command), cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     start_new_session=True, pass_fds=(lock_fd,))
        except OSError as exc:
            raise SettingsError("Cannot start the guarded Xcode settings inspection.") from exc
        started = time.monotonic()
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if interrupted:
                    raise SettingsError("Xcode settings inspection was interrupted; the build was not started.", 128 + interrupted[0])
                if time.monotonic() - started >= timeout:
                    raise SettingsError("Xcode settings inspection timed out; the build was not started.")
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > max_bytes:
                        raise SettingsError("Xcode settings exceeded the inspection size limit; the build was not started.")
            remaining = max(0.01, timeout - (time.monotonic() - started))
            try:
                code = child.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise SettingsError("Xcode settings inspection timed out; the build was not started.") from exc
        if interrupted:
            raise SettingsError("Xcode settings inspection was interrupted; the build was not started.", 128 + interrupted[0])
        if code != 0:
            raise SettingsError("Xcode could not inspect the selected action's build settings; the build was not started.", 128 - code if code < 0 else 2)
        completed = True
        return bytes(output)
    finally:
        if child is not None:
            if not completed or child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait()
            if child.stdout is not None:
                child.stdout.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def validate_settings(plan: Plan, payload: bytes, *, expected_project: Path | None = None) -> dict:
    try:
        records = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SettingsError("Xcode returned invalid settings JSON; the build was not started.") from exc
    if not isinstance(records, list) or not records or len(records) > 4096:
        raise SettingsError("Xcode returned no inspectable targets or too many settings records.")
    assert plan.graph is not None
    projects = set(plan.graph.projects)
    seen = set()
    previous_records = {}
    identical_repeats = 0
    inspected = 0
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("buildSettings"), dict):
            raise SettingsError("An Xcode settings record is incomplete.")
        settings = record["buildSettings"]
        project_value, target = settings.get("PROJECT_FILE_PATH"), record.get("target")
        if not isinstance(project_value, str) or not isinstance(target, str) or not target:
            raise SettingsError("An Xcode settings record has no project or target identity.")
        project = Path(project_value)
        if (project, target) in seen and expected_project is not None and previous_records[(project, target)] == record:
            # Xcode 27's -alltargets metadata query repeats identical records even
            # for a one-target project. Conflicting duplicates remain an error.
            identical_repeats += 1
            continue
        if project not in projects or (project, target) in seen:
            raise SettingsError("Xcode returned an unknown project or duplicate target settings record.")
        known_targets = getattr(plan.graph, "targets", {}).get(project)
        if known_targets is not None and target not in known_targets:
            raise SettingsError("Xcode returned a target outside the statically inspected project graph.")
        seen.add((project, target))
        previous_records[(project, target)] = record
        if expected_project is not None and project != expected_project:
            raise SettingsError("A member settings query returned a different project.")
        if plan.action == "archive":
            _validate_archive_signing(settings)
        if not REQUIRED_SETTINGS.issubset(settings):
            raise SettingsError("A target is missing required output settings; the build was not started.")
        for name in ("FULL_PRODUCT_NAME", "EXECUTABLE_PATH", "WRAPPER_NAME"):
            value = settings.get(name, "")
            if not isinstance(value, str) or (value and (Path(value).is_absolute() or ".." in Path(value).parts or "$" in value or "~" in value or _unsafe_text(value))):
                raise SettingsError(f"Effective {name} is an unsafe product-relative output path.")
        for name in sorted(name for name in settings if _is_output_path_setting(name)):
            value = settings[name]
            if value == "" and name not in REQUIRED_SETTINGS:
                continue
            if not isinstance(value, str) or not value or _unsafe_text(value) or "$" in value or "~" in value:
                raise SettingsError(f"Effective {name} is not a resolved output path (source: Xcode target settings).")
            path = Path(value)
            try:
                validate_destination(path, plan.config, plan.volume)
            except BuildHarborError as exc:
                # Only path-valued settings are reflected, never full settings/env.
                safe_value = value if len(value) <= 300 and not _unsafe_text(value) else "[redacted]"
                raise SettingsError(f"Effective {name}={safe_value} conflicts with managed storage (source: Xcode target settings; definition source unavailable).") from exc
            inspected += 1
    if expected_project is not None:
        targets = set(plan.graph.targets[expected_project])
        if {name for _, name in seen} != targets:
            raise SettingsError("Xcode did not return exactly every statically inspected member target.")
    return {"status": "passed", "scope": "selected_scheme_action", "targets": len(seen),
            "path_settings_checked": inspected, "identical_member_records_ignored": identical_repeats,
            "definition_sources": "not_exposed_by_xcode"}


def inspect_effective_settings(plan: Plan, lock_fd: int, environment: dict[str, str]) -> dict:
    if not plan.settings_command:
        return {"status": "not_applicable"}
    payload = capture_settings(plan.settings_command, plan.config.project_root, environment, lock_fd)
    result = validate_settings(plan, payload)
    members = getattr(plan, "member_settings_commands", ())
    for project, command in zip(plan.graph.projects, members, strict=bool(members)):
        payload = capture_settings(command, plan.config.project_root, environment, lock_fd)
        member = validate_settings(plan, payload, expected_project=project)
        result["path_settings_checked"] += member["path_settings_checked"]
        result["identical_member_records_ignored"] += member["identical_member_records_ignored"]
    if members:
        result["scope"] = "all_static_projects_and_selected_scheme_action"
        result["member_projects"] = len(members)
    return result
