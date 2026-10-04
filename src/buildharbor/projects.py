"""Bounded, read-only inspection of Xcode project and workspace source graphs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

from .errors import BuildHarborError


_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_FILES = 2048
_MAX_TOKENS = 750_000
_MAX_DEPTH = 128
_MAX_XML_ELEMENTS = 20_000
_MAX_GRAPH_DEPTH = 64
_MAX_DIRECTORY_ENTRIES = 4096
_BIDI = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
_PROJECT_TYPES = {"wrapper.pb-project"}
_GROUP_TYPES = {"PBXGroup", "PBXVariantGroup", "XCVersionGroup"}
_TARGET_TYPES = {"PBXNativeTarget", "PBXAggregateTarget"}
_SCRIPT_TYPES = {"PBXShellScriptBuildPhase", "PBXAppleScriptBuildPhase", "PBXBuildRule"}
_STANDARD_PHASE_TYPES = {
    "PBXSourcesBuildPhase",
    "PBXFrameworksBuildPhase",
    "PBXResourcesBuildPhase",
    "PBXHeadersBuildPhase",
    "PBXCopyFilesBuildPhase",
}
_SAFE_COPY_SUBFOLDERS = {"1", "6", "7", "10", "13", "15", "16"}
_SAFE_OBJECT_TYPES = (
    _GROUP_TYPES
    | _TARGET_TYPES
    | _STANDARD_PHASE_TYPES
    | {
        "PBXBuildFile",
        "PBXContainerItemProxy",
        "PBXFileReference",
        "PBXProject",
        "PBXReferenceProxy",
        "PBXTargetDependency",
        "XCBuildConfiguration",
        "XCConfigurationList",
        "XCLocalSwiftPackageReference",
        "XCRemoteSwiftPackageReference",
        "XCSwiftPackageProductDependency",
    }
)


@dataclass(frozen=True)
class ProjectGraph:
    """Static source inputs that must remain unchanged through guarded launch."""

    identity: Path
    projects: tuple[Path, ...]
    fingerprints: dict[str, str]
    targets: dict[Path, tuple[str, ...]]
    managed_settings: tuple[str, ...]
    remote_packages: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    offset: int


class _OpenStepParser:
    def __init__(self, text: str):
        self.text = text
        self.at = 0
        self.tokens = 0
        self.lookahead: _Token | None = None

    def parse(self) -> object:
        value = self._value(0)
        token = self._take()
        if token.kind != "eof":
            raise BuildHarborError("The Xcode project contains trailing OpenStep data.")
        return value

    def _value(self, depth: int) -> object:
        if depth > _MAX_DEPTH:
            raise BuildHarborError("The Xcode project exceeds the supported nesting depth.")
        token = self._take()
        if token.kind == "{":
            result: dict[str, object] = {}
            while self._peek().kind != "}":
                key = self._take()
                if key.kind != "string":
                    raise BuildHarborError("The Xcode project has a malformed dictionary key.")
                self._expect("=")
                if key.value in result:
                    raise BuildHarborError("The Xcode project contains an ambiguous duplicate dictionary key.")
                result[key.value] = self._value(depth + 1)
                self._expect(";")
            self._take()
            return result
        if token.kind == "(":
            result: list[object] = []
            if self._peek().kind == ")":
                self._take()
                return result
            while True:
                result.append(self._value(depth + 1))
                token = self._take()
                if token.kind == ")":
                    return result
                if token.kind != ",":
                    raise BuildHarborError("The Xcode project has a malformed array.")
                if self._peek().kind == ")":
                    self._take()
                    return result
        if token.kind == "string":
            return token.value
        raise BuildHarborError("The Xcode project has a malformed OpenStep value.")

    def _expect(self, kind: str) -> None:
        if self._take().kind != kind:
            raise BuildHarborError("The Xcode project has malformed OpenStep punctuation.")

    def _peek(self) -> _Token:
        if self.lookahead is None:
            self.lookahead = self._next()
        return self.lookahead

    def _take(self) -> _Token:
        token = self._peek()
        self.lookahead = None
        return token

    def _next(self) -> _Token:
        self._skip_space_and_comments()
        self.tokens += 1
        if self.tokens > _MAX_TOKENS:
            raise BuildHarborError("The Xcode project exceeds the supported token count.")
        if self.at >= len(self.text):
            return _Token("eof", "", self.at)
        start = self.at
        char = self.text[self.at]
        if char in "{}()=;,":
            self.at += 1
            return _Token(char, char, start)
        if char == '"':
            return _Token("string", self._quoted(), start)
        value: list[str] = []
        while self.at < len(self.text):
            char = self.text[self.at]
            if char.isspace() or char in "{}()=;,\"":
                break
            if self.text.startswith("/*", self.at) or self.text.startswith("//", self.at):
                break
            value.append(char)
            self.at += 1
        if not value:
            raise BuildHarborError("The Xcode project contains an unsupported OpenStep token.")
        return _Token("string", "".join(value), start)

    def _quoted(self) -> str:
        self.at += 1
        value: list[str] = []
        while self.at < len(self.text):
            char = self.text[self.at]
            self.at += 1
            if char == '"':
                return "".join(value)
            if char != "\\":
                value.append(char)
                continue
            if self.at >= len(self.text):
                break
            escaped = self.text[self.at]
            self.at += 1
            replacements = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
            if escaped in replacements:
                value.append(replacements[escaped])
            elif escaped == "U" and self.at + 4 <= len(self.text):
                digits = self.text[self.at : self.at + 4]
                if not re.fullmatch(r"[0-9A-Fa-f]{4}", digits):
                    raise BuildHarborError("The Xcode project has an invalid Unicode escape.")
                value.append(chr(int(digits, 16)))
                self.at += 4
            elif escaped in "01234567":
                digits = escaped
                while len(digits) < 3 and self.at < len(self.text) and self.text[self.at] in "01234567":
                    digits += self.text[self.at]
                    self.at += 1
                value.append(chr(int(digits, 8)))
            else:
                value.append(escaped)
        raise BuildHarborError("The Xcode project has an unterminated quoted string.")

    def _skip_space_and_comments(self) -> None:
        while self.at < len(self.text):
            if self.text[self.at].isspace():
                self.at += 1
                continue
            if self.text.startswith("//", self.at):
                end = self.text.find("\n", self.at + 2)
                self.at = len(self.text) if end < 0 else end + 1
                continue
            if self.text.startswith("/*", self.at):
                end = self.text.find("*/", self.at + 2)
                if end < 0:
                    raise BuildHarborError("The Xcode project has an unterminated comment.")
                self.at = end + 2
                continue
            return


class _Inspector:
    def __init__(self, policy_root: Path, managed_settings: set[str]):
        try:
            self.root = policy_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise BuildHarborError("Cannot resolve the project policy directory.") from exc
        if not self.root.is_dir():
            raise BuildHarborError("The project policy path must be a directory.")
        self.managed = frozenset(managed_settings)
        if not self.managed or any(not isinstance(key, str) or not key for key in self.managed):
            raise BuildHarborError("The managed-setting policy is empty or invalid.")
        self.fingerprints: dict[str, str] = {}
        self.file_contents: dict[str, bytes] = {}
        self.projects: dict[tuple[int, int], Path] = {}
        self.workspaces: dict[tuple[int, int], Path] = {}
        self.targets: dict[Path, tuple[str, ...]] = {}
        self.target_ids: dict[Path, frozenset[str]] = {}
        self.group_parent_maps: dict[Path, dict[str, list[str]]] = {}
        self.active_projects: set[tuple[int, int]] = set()
        self.active_workspaces: set[tuple[int, int]] = set()
        self.completed_workspaces: set[tuple[int, int]] = set()
        self.total_bytes = 0
        self.directory_entries = 0
        self.scheme_references: list[tuple[Path, str]] = []
        self.remote_packages: list[str] = []

    def inspect(self, identity: Path) -> ProjectGraph:
        identity = self._secure(identity, directory=True)
        if identity.suffix == ".xcodeproj":
            self._project(identity, 0)
        elif identity.suffix == ".xcworkspace":
            self._workspace(identity, 0)
        else:
            raise BuildHarborError("The selected input must be an Xcode project or workspace.")
        owners = [*self.workspaces.values(), *self.projects.values()]
        for owner in sorted(set(owners), key=str):
            self._reject_private_schemes(owner)
            self._schemes(owner)
        project_set = set(self.projects.values())
        for project, target_id in self.scheme_references:
            if project not in project_set or target_id not in self.target_ids.get(project, frozenset()):
                raise BuildHarborError("A shared scheme references a project or target outside the inspected graph.")
        return ProjectGraph(
            identity=identity,
            projects=tuple(sorted(project_set, key=str)),
            fingerprints=dict(sorted(self.fingerprints.items())),
            targets={path: self.targets[path] for path in sorted(self.targets, key=str)},
            managed_settings=tuple(sorted(self.managed)),
            remote_packages=tuple(sorted(set(self.remote_packages))),
        )

    def _secure(self, path: Path, *, directory: bool | None = None) -> Path:
        raw = path if path.is_absolute() else self.root / path
        try:
            relative = raw.relative_to(self.root)
        except ValueError as exc:
            raise BuildHarborError("A project graph path escapes the policy directory.") from exc

        # Walk the spelling that Xcode will consume before normalizing it.  A
        # lexical ``link/../member`` can end at the expected path while still
        # traversing a symlink, so comparing only the final resolved path is not
        # sufficient.
        cursor = self.root
        for component in relative.parts:
            if component == "..":
                if cursor == self.root:
                    raise BuildHarborError("A project graph path escapes the policy directory.")
                cursor = cursor.parent
                continue
            if self._unsafe_text(component):
                raise BuildHarborError("A project graph path contains unsafe characters.")
            cursor /= component
            try:
                component_metadata = cursor.lstat()
            except OSError as exc:
                raise BuildHarborError("A project graph path is missing or inaccessible.") from exc
            if stat.S_ISLNK(component_metadata.st_mode):
                raise BuildHarborError("A symlink is not supported in the project graph.")

        candidate = Path(os.path.abspath(raw))
        try:
            resolved = raw.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise BuildHarborError("A project graph path is missing or inaccessible.") from exc
        if resolved != candidate or not resolved.is_relative_to(self.root):
            raise BuildHarborError("A project graph path is a symlink or escapes the policy directory.")
        try:
            metadata = resolved.lstat()
        except OSError as exc:
            raise BuildHarborError("Cannot inspect a project graph path.") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise BuildHarborError("Symlinks are not supported in the project graph.")
        if directory is True and not stat.S_ISDIR(metadata.st_mode):
            raise BuildHarborError("An expected project graph directory is not a directory.")
        if directory is False and not stat.S_ISREG(metadata.st_mode):
            raise BuildHarborError("An expected project graph file is not a regular file.")
        return resolved

    def _read(self, path: Path) -> bytes:
        path = self._secure(path, directory=False)
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise BuildHarborError("A project graph file exceeds the supported size.")
        if str(path) in self.fingerprints:
            return self.file_contents[str(path)]
        if len(self.fingerprints) >= _MAX_FILES:
            raise BuildHarborError("The project graph exceeds the supported file count.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            fd = os.open(path, flags)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_FILE_BYTES:
                raise BuildHarborError("A project graph input is not a bounded regular file.")
            chunks: list[bytes] = []
            remaining = before.st_size + 1
            while remaining > 0:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(fd)
            if len(data) != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise BuildHarborError("A project graph file changed during inspection.")
        except BuildHarborError:
            raise
        except OSError as exc:
            raise BuildHarborError("Cannot read a project graph file.") from exc
        finally:
            if fd is not None:
                os.close(fd)
        self.total_bytes += len(data)
        if self.total_bytes > _MAX_TOTAL_BYTES:
            raise BuildHarborError("The project graph exceeds the supported total byte count.")
        self.fingerprints[str(path)] = hashlib.sha256(data).hexdigest()
        self.file_contents[str(path)] = data
        return data

    def _xml(self, path: Path) -> ET.Element:
        data = self._read(path)
        upper = data.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise BuildHarborError("DTD and entity declarations are not supported in Xcode XML inputs.")
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            raise BuildHarborError("An Xcode XML input is malformed.") from exc
        if sum(1 for _ in root.iter()) > _MAX_XML_ELEMENTS:
            raise BuildHarborError("An Xcode XML input exceeds the supported element count.")
        return root

    def _workspace(self, workspace: Path, depth: int) -> None:
        if depth > _MAX_GRAPH_DEPTH:
            raise BuildHarborError("The workspace graph exceeds the supported depth.")
        workspace = self._secure(workspace, directory=True)
        metadata = workspace.stat()
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in self.active_workspaces:
            raise BuildHarborError("The workspace graph contains a cycle.")
        if identity in self.completed_workspaces:
            return
        self.active_workspaces.add(identity)
        try:
            root = self._xml(workspace / "contents.xcworkspacedata")
            if self._tag(root) != "Workspace":
                raise BuildHarborError("The workspace XML has an unsupported root element.")
            self._workspace_children(root, workspace.parent, workspace.parent, depth)
            self.workspaces[identity] = workspace
            self.completed_workspaces.add(identity)
        finally:
            self.active_workspaces.remove(identity)

    def _workspace_children(self, element: ET.Element, group_base: Path, container_base: Path, depth: int) -> None:
        if depth > _MAX_GRAPH_DEPTH:
            raise BuildHarborError("The workspace group graph exceeds the supported depth.")
        for child in element:
            tag = self._tag(child)
            if tag == "Group":
                location = child.attrib.get("location")
                base = group_base if not location else self._location(location, group_base, container_base)
                if base.suffix in {".xcodeproj", ".xcworkspace"}:
                    raise BuildHarborError("A workspace group location must resolve to a directory group.")
                base = self._secure(base, directory=True)
                self._workspace_children(child, base, container_base, depth + 1)
            elif tag == "FileRef":
                location = child.attrib.get("location")
                if not location:
                    raise BuildHarborError("A workspace member is missing its location.")
                member = self._location(location, group_base, container_base)
                if member.suffix == ".xcodeproj":
                    self._project(member, depth + 1)
                elif member.suffix == ".xcworkspace":
                    self._workspace(member, depth + 1)
                else:
                    raise BuildHarborError("A workspace contains an unsupported member type.")
            else:
                raise BuildHarborError("The workspace XML contains an unsupported element.")

    def _location(self, location: str, group_base: Path, container_base: Path) -> Path:
        if any(ord(char) < 32 or ord(char) == 127 or char in _BIDI for char in location):
            raise BuildHarborError("A workspace location contains unsafe characters.")
        if ":" not in location:
            raise BuildHarborError("A workspace location has an unsupported form.")
        kind, value = location.split(":", 1)
        if not value or Path(value).is_absolute() or "$" in value or "~" in value:
            raise BuildHarborError("A workspace location is unresolved or escapes its container.")
        if kind == "group":
            return self._secure(group_base / value, directory=True)
        if kind == "container":
            return self._secure(container_base / value, directory=True)
        raise BuildHarborError("A workspace location scheme is unsupported.")

    def _project(self, project: Path, depth: int) -> None:
        if depth > _MAX_GRAPH_DEPTH:
            raise BuildHarborError("The project graph exceeds the supported depth.")
        project = self._secure(project, directory=True)
        if project.suffix != ".xcodeproj":
            raise BuildHarborError("A nested project reference has the wrong extension.")
        metadata = project.stat()
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in self.active_projects:
            raise BuildHarborError("The nested project graph contains a cycle.")
        if identity in self.projects:
            return
        self.active_projects.add(identity)
        try:
            data = self._read(project / "project.pbxproj")
            try:
                text = data.decode("utf-8")
            except UnicodeError as exc:
                raise BuildHarborError("The Xcode project is not valid UTF-8.") from exc
            parsed = _OpenStepParser(text).parse()
            if not isinstance(parsed, dict):
                raise BuildHarborError("The Xcode project root must be a dictionary.")
            objects = parsed.get("objects", {})
            if not isinstance(objects, dict):
                raise BuildHarborError("The Xcode project objects table is malformed.")
            for key, value in objects.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    raise BuildHarborError("The Xcode project objects table contains a malformed object.")
            self._validate_structure(parsed, objects)
            target_names, target_ids = self._inspect_objects(project, objects)
            self.projects[identity] = project
            self.targets[project] = tuple(sorted(target_names))
            self.target_ids[project] = frozenset(target_ids)
            for nested in self._nested_projects(project, objects):
                self._project(nested, depth + 1)
        finally:
            self.active_projects.remove(identity)

    def _inspect_objects(self, project: Path, objects: dict[str, object]) -> tuple[set[str], set[str]]:
        target_names: set[str] = set()
        target_ids: set[str] = set()
        for object_id, raw in objects.items():
            assert isinstance(raw, dict)
            isa = raw.get("isa")
            if isa is not None and not isinstance(isa, str):
                raise BuildHarborError("An Xcode project object has an invalid type.")
            if isa in _SCRIPT_TYPES:
                raise BuildHarborError("Script phases and custom build rules are unsupported in the inspected graph.")
            if isa in {"PBXLegacyTarget", "PBXFileSystemSynchronizedRootGroup", "PBXFileSystemSynchronizedBuildFileExceptionSet"}:
                raise BuildHarborError("This Xcode project object type is unsupported by static graph inspection.")
            if isa in _TARGET_TYPES:
                name = raw.get("name")
                if not isinstance(name, str) or not name or self._unsafe_text(name) or name in target_names:
                    raise BuildHarborError("An Xcode project target name is missing, unsafe, or ambiguous.")
                build_rules = raw.get("buildRules", [])
                if not isinstance(build_rules, list) or build_rules:
                    raise BuildHarborError("Targets with custom build rules are unsupported.")
                target_names.add(name)
                target_ids.add(object_id)
            if isa == "PBXCopyFilesBuildPhase":
                self._copy_phase(raw)
            if isa == "XCBuildConfiguration":
                settings = raw.get("buildSettings", {})
                if not isinstance(settings, dict):
                    raise BuildHarborError("An Xcode build configuration has malformed settings.")
                for key, value in settings.items():
                    if not isinstance(key, str):
                        raise BuildHarborError("An Xcode build setting has a malformed key.")
                    base = key.split("[", 1)[0]
                    if base in self.managed:
                        detail = self._safe_value(value)
                        raise BuildHarborError(f"Managed setting {base} is explicitly set in {project.name}{detail}.")
                reference = raw.get("baseConfigurationReference")
                if reference is not None:
                    if not isinstance(reference, str):
                        raise BuildHarborError("An xcconfig reference is malformed.")
                    config = self._resolve_file_reference(project, objects, reference)
                    self._xcconfig(config, set())
            if isa == "PBXFileReference":
                self._validate_file_reference(project, objects, object_id, raw)
            if isa == "XCLocalSwiftPackageReference":
                self._local_package(project, raw)
            if isa == "XCRemoteSwiftPackageReference":
                self.remote_packages.append(self._remote_package(raw))
        return target_names, target_ids

    def _validate_structure(self, parsed: dict[str, object], objects: dict[str, object]) -> None:
        if not objects:
            # The repository's intentionally minimal synthetic fixture has exactly this
            # shape.  Do not let arbitrary dictionaries bypass structural validation.
            if set(parsed) != {"objects"}:
                raise BuildHarborError("An empty synthetic Xcode project has unsupported fields.")
            return
        root_id = parsed.get("rootObject")
        if not isinstance(root_id, str):
            raise BuildHarborError("A nonempty Xcode project is missing its root project reference.")
        root = objects.get(root_id)
        if not isinstance(root, dict) or root.get("isa") != "PBXProject":
            raise BuildHarborError("The Xcode project's root reference is unresolved.")
        for object_id, raw in objects.items():
            assert isinstance(raw, dict)
            isa = raw.get("isa")
            if not isinstance(isa, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", isa):
                raise BuildHarborError("An Xcode project object is missing its type.")
            if isa in _TARGET_TYPES:
                self._reference(objects, raw.get("buildConfigurationList"), {"XCConfigurationList"}, "target configuration")
                phases = self._string_list(raw.get("buildPhases", []), "target build phases")
                for phase in phases:
                    self._reference(objects, phase, _STANDARD_PHASE_TYPES | _SCRIPT_TYPES, "target build phase")
                for dependency in self._string_list(raw.get("dependencies", []), "target dependencies"):
                    self._reference(objects, dependency, {"PBXTargetDependency"}, "target dependency")
                for product in self._string_list(
                    raw.get("packageProductDependencies", []), "package product dependencies"
                ):
                    self._reference(objects, product, {"XCSwiftPackageProductDependency"}, "package product")
            elif isa == "PBXProject":
                self._reference(objects, raw.get("buildConfigurationList"), {"XCConfigurationList"}, "project configuration")
                self._reference(objects, raw.get("mainGroup"), _GROUP_TYPES, "main group")
                for target_id in self._string_list(raw.get("targets", []), "project targets"):
                    self._reference(objects, target_id, _TARGET_TYPES, "project target")
                for package in self._string_list(raw.get("packageReferences", []), "package references"):
                    self._reference(
                        objects,
                        package,
                        {"XCLocalSwiftPackageReference", "XCRemoteSwiftPackageReference"},
                        "package",
                    )
                references = raw.get("projectReferences", [])
                if not isinstance(references, list):
                    raise BuildHarborError("The nested project reference list is malformed.")
                for reference in references:
                    if not isinstance(reference, dict) or not isinstance(reference.get("ProjectRef"), str):
                        raise BuildHarborError("A nested project reference is malformed.")
                    self._reference(objects, reference["ProjectRef"], {"PBXFileReference"}, "nested project")
                    product_group = reference.get("ProductGroup")
                    if product_group is not None:
                        self._reference(objects, product_group, _GROUP_TYPES, "nested product group")
            elif isa == "XCConfigurationList":
                for configuration in self._string_list(raw.get("buildConfigurations", []), "build configurations"):
                    self._reference(objects, configuration, {"XCBuildConfiguration"}, "build configuration")
            elif isa in _STANDARD_PHASE_TYPES:
                for build_file in self._string_list(raw.get("files", []), "build phase files"):
                    self._reference(objects, build_file, {"PBXBuildFile"}, "build file")
            elif isa in _GROUP_TYPES:
                for child in self._string_list(raw.get("children", []), "group children"):
                    self._reference(
                        objects,
                        child,
                        _GROUP_TYPES | {"PBXFileReference", "PBXReferenceProxy"},
                        "group child",
                    )
            elif isa == "PBXBuildFile":
                file_ref = raw.get("fileRef")
                product_ref = raw.get("productRef")
                if (file_ref is None) == (product_ref is None):
                    raise BuildHarborError("An Xcode build file must have exactly one source reference.")
                if file_ref is not None:
                    self._reference(objects, file_ref, {"PBXFileReference", "PBXReferenceProxy"}, "build file")
                else:
                    self._reference(
                        objects, product_ref, {"XCSwiftPackageProductDependency"}, "package build file"
                    )
            elif isa == "PBXContainerItemProxy":
                self._reference(
                    objects, raw.get("containerPortal"), {"PBXProject", "PBXFileReference"}, "container portal"
                )
            elif isa == "PBXReferenceProxy":
                self._reference(objects, raw.get("remoteRef"), {"PBXContainerItemProxy"}, "remote product")
            elif isa == "PBXTargetDependency":
                target = raw.get("target")
                proxy = raw.get("targetProxy")
                if target is None and proxy is None:
                    raise BuildHarborError("An Xcode target dependency is unresolved.")
                if target is not None:
                    self._reference(objects, target, _TARGET_TYPES, "dependency target")
                if proxy is not None:
                    self._reference(objects, proxy, {"PBXContainerItemProxy"}, "dependency proxy")
            elif isa == "XCSwiftPackageProductDependency":
                self._reference(
                    objects,
                    raw.get("package"),
                    {"XCLocalSwiftPackageReference", "XCRemoteSwiftPackageReference"},
                    "package product",
                )

            if isa not in _SAFE_OBJECT_TYPES and isa not in _SCRIPT_TYPES:
                raise BuildHarborError("The Xcode project contains an unsupported object type.")

    @staticmethod
    def _string_list(value: object, description: str) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise BuildHarborError(f"The Xcode project's {description} list is malformed.")
        return value

    @staticmethod
    def _reference(objects: dict[str, object], reference: object, allowed: set[str], description: str) -> dict[str, object]:
        if not isinstance(reference, str):
            raise BuildHarborError(f"An Xcode {description} reference is missing.")
        raw = objects.get(reference)
        if not isinstance(raw, dict) or raw.get("isa") not in allowed:
            raise BuildHarborError(f"An Xcode {description} reference is unresolved or has the wrong type.")
        return raw

    def _copy_phase(self, raw: dict[str, object]) -> None:
        destination = raw.get("dstPath", "")
        subfolder = raw.get("dstSubfolderSpec")
        if not isinstance(destination, str) or (subfolder is not None and str(subfolder) not in _SAFE_COPY_SUBFOLDERS):
            raise BuildHarborError("A copy-files phase has an unsupported destination.")
        path = Path(destination) if destination else None
        if destination and (
            path is None
            or path.is_absolute()
            or ".." in path.parts
            or "$" in destination
            or "~" in destination
            or self._unsafe_text(destination)
        ):
            raise BuildHarborError("A copy-files phase has an unresolved or escaping destination.")

    def _nested_projects(self, project: Path, objects: dict[str, object]) -> list[Path]:
        result: list[Path] = []
        for object_id, raw in objects.items():
            assert isinstance(raw, dict)
            if raw.get("isa") != "PBXFileReference":
                continue
            kind = raw.get("lastKnownFileType", raw.get("explicitFileType"))
            path = raw.get("path")
            if (isinstance(kind, str) and kind in _PROJECT_TYPES) or (
                isinstance(path, str) and path.endswith(".xcodeproj")
            ):
                result.append(self._resolve_file_reference(project, objects, object_id, directory=True))
        return sorted(set(result), key=str)

    def _resolve_file_reference(
        self,
        project: Path,
        objects: dict[str, object],
        reference: str,
        *,
        directory: bool | None = False,
    ) -> Path:
        raw = objects.get(reference)
        if not isinstance(raw, dict) or raw.get("isa") != "PBXFileReference":
            raise BuildHarborError("A project file reference is unresolved.")
        path = raw.get("path")
        if not isinstance(path, str) or not path or self._unsafe_path(path):
            raise BuildHarborError("A project file reference path is unresolved or unsafe.")
        source_tree = raw.get("sourceTree", "<group>")
        if not isinstance(source_tree, str):
            raise BuildHarborError("A project file reference uses an unresolved source tree.")
        if source_tree in {"SOURCE_ROOT", "PROJECT_DIR"}:
            base = project.parent
        elif source_tree == "<group>":
            parents = self._group_parents(project, objects).get(reference, [])
            if len(parents) > 1:
                raise BuildHarborError("A project file reference has ambiguous group parents.")
            base = project.parent if not parents else self._group_base(project, objects, parents[0], set())
        elif source_tree == "<absolute>":
            if not Path(path).is_absolute():
                raise BuildHarborError("An absolute project file reference is malformed.")
            return self._secure(Path(path), directory=directory)
        else:
            raise BuildHarborError("A project file reference uses an unresolved source tree.")
        return self._secure(base / path, directory=directory)

    def _validate_file_reference(
        self,
        project: Path,
        objects: dict[str, object],
        reference: str,
        raw: dict[str, object],
    ) -> None:
        path = raw.get("path")
        if not isinstance(path, str) or self._unsafe_path(path):
            raise BuildHarborError("A project file reference path is unresolved or unsafe.")
        source_tree = raw.get("sourceTree", "<group>")
        if not isinstance(source_tree, str):
            raise BuildHarborError("A project file reference uses an unresolved source tree.")
        if source_tree in {"<group>", "SOURCE_ROOT", "PROJECT_DIR"}:
            self._resolve_file_reference(project, objects, reference, directory=None)
        elif source_tree not in {"BUILT_PRODUCTS_DIR", "DEVELOPER_DIR", "SDKROOT"}:
            raise BuildHarborError("A project file reference uses an unresolved source tree.")

    def _group_parents(self, project: Path, objects: dict[str, object]) -> dict[str, list[str]]:
        cached = self.group_parent_maps.get(project)
        if cached is not None:
            return cached
        result: dict[str, list[str]] = {}
        for object_id, raw in objects.items():
            if not isinstance(raw, dict) or raw.get("isa") not in _GROUP_TYPES:
                continue
            children = raw.get("children", [])
            if not isinstance(children, list) or any(not isinstance(child, str) for child in children):
                raise BuildHarborError("An Xcode group has malformed children.")
            for child in children:
                result.setdefault(child, []).append(object_id)
        self.group_parent_maps[project] = result
        return result

    def _group_base(self, project: Path, objects: dict[str, object], group_id: str, active: set[str]) -> Path:
        if group_id in active:
            raise BuildHarborError("The Xcode group graph contains a cycle.")
        if len(active) >= _MAX_GRAPH_DEPTH:
            raise BuildHarborError("The Xcode group graph exceeds the supported depth.")
        active.add(group_id)
        try:
            raw = objects.get(group_id)
            if not isinstance(raw, dict) or raw.get("isa") not in _GROUP_TYPES:
                raise BuildHarborError("A project group reference is unresolved.")
            source_tree = raw.get("sourceTree", "<group>")
            if not isinstance(source_tree, str):
                raise BuildHarborError("An Xcode group uses an unresolved source tree.")
            if source_tree in {"SOURCE_ROOT", "PROJECT_DIR"}:
                base = project.parent
            elif source_tree == "<group>":
                parents = self._group_parents(project, objects).get(group_id, [])
                if len(parents) > 1:
                    raise BuildHarborError("An Xcode group has ambiguous parents.")
                base = project.parent if not parents else self._group_base(project, objects, parents[0], active)
            else:
                raise BuildHarborError("An Xcode group uses an unresolved source tree.")
            path = raw.get("path")
            if path is not None:
                if not isinstance(path, str) or self._unsafe_path(path):
                    raise BuildHarborError("An Xcode group path is unresolved or unsafe.")
                base /= path
            return self._secure(base, directory=True)
        finally:
            active.remove(group_id)

    def _xcconfig(self, path: Path, active: set[Path]) -> None:
        path = self._secure(path, directory=False)
        if path in active:
            raise BuildHarborError("The xcconfig include graph contains a cycle.")
        active.add(path)
        try:
            data = self._read(path)
            try:
                text = data.decode("utf-8")
            except UnicodeError as exc:
                raise BuildHarborError("An xcconfig file is not valid UTF-8.") from exc
            text = self._strip_comments(text).replace("\\\r\n", " ").replace("\\\n", " ")
            for line in text.splitlines():
                include = re.fullmatch(r'\s*#include(\?)?\s+(?:"([^"]+)"|<([^>]+)>)\s*', line)
                if include:
                    included = include.group(2)
                    if include.group(3) is not None or not included or self._unsafe_path(included):
                        raise BuildHarborError("An xcconfig include path is unresolved or unsupported.")
                    self._xcconfig(path.parent / included, active)
                    continue
                if line.lstrip().startswith("#"):
                    raise BuildHarborError("An xcconfig directive is malformed or unsupported.")
                setting = re.match(
                    r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]+\]\s*)*(?:\+?=|\?=|:=)\s*(.*)$",
                    line,
                )
                if setting and setting.group(1) in self.managed:
                    detail = self._safe_value(setting.group(2).strip())
                    raise BuildHarborError(f"Managed setting {setting.group(1)} is explicitly set in {path.name}{detail}.")
        finally:
            active.remove(path)

    def _local_package(self, project: Path, raw: dict[str, object]) -> None:
        relative = raw.get("relativePath")
        if not isinstance(relative, str) or self._unsafe_path(relative):
            raise BuildHarborError("A local Swift package path is unresolved or unsafe.")
        package = self._secure(project.parent / relative, directory=True)
        manifest_path = package / "Package.swift"
        data = self._read(manifest_path)
        _screen_package_manifest(data)

    @classmethod
    def _remote_package(cls, raw: dict[str, object]) -> str:
        repository = raw.get("repositoryURL")
        if (
            not isinstance(repository, str)
            or not repository
            or len(repository) > 2048
            or cls._unsafe_text(repository)
            or any(char.isspace() for char in repository)
            or re.search(r"%(?![0-9A-Fa-f]{2})", repository)
        ):
            raise BuildHarborError("A remote Swift package repository URL is malformed.")
        try:
            decoded = unquote(repository, errors="strict")
            parsed = urlsplit(repository)
            decoded_parsed = urlsplit(decoded)
            port = parsed.port
        except (UnicodeError, ValueError) as exc:
            raise BuildHarborError("A remote Swift package repository URL is malformed.") from exc
        if (
            cls._unsafe_text(decoded)
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or decoded_parsed.username
            or decoded_parsed.password
        ):
            raise BuildHarborError("Remote Swift package repository URLs cannot contain credentials or unsafe data.")
        if parsed.scheme in {"http", "https"}:
            if not parsed.hostname or (port is not None and not 1 <= port <= 65535):
                raise BuildHarborError("A remote Swift package repository URL is malformed.")
        elif parsed.scheme == "file":
            if parsed.netloc not in {"", "localhost"} or not Path(unquote(parsed.path)).is_absolute():
                raise BuildHarborError("A file Swift package repository URL is malformed.")
        else:
            raise BuildHarborError("Remote Swift packages require an http, https, or file repository URL.")

        requirement = raw.get("requirement")
        if not isinstance(requirement, dict) or set(requirement) != {"kind", "version"}:
            raise BuildHarborError("A remote Swift package requires one exact version.")
        version = requirement.get("version")
        if requirement.get("kind") != "exactVersion" or not isinstance(version, str) or not re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
            r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
            version,
        ):
            raise BuildHarborError("A remote Swift package requires a literal exact semantic version.")
        return repository

    def _schemes(self, owner: Path) -> None:
        directory = owner / "xcshareddata/xcschemes"
        if not self._exists_without_following(directory):
            return
        for path in self._directory_contents(directory, "shared Xcode schemes"):
            if path.suffix != ".xcscheme":
                raise BuildHarborError("The shared scheme directory contains an unsupported entry.")
            root = self._xml(path)
            if self._tag(root) != "Scheme":
                raise BuildHarborError("A shared scheme has an unsupported root element.")
            for element in root.iter():
                tag = self._tag(element)
                if tag in {"PreActions", "PostActions", "ExecutionAction", "ActionContent"}:
                    raise BuildHarborError("Shared schemes with pre-actions or post-actions are unsupported.")
                if tag == "BuildableReference":
                    container = element.attrib.get("ReferencedContainer")
                    target_id = element.attrib.get("BlueprintIdentifier")
                    if not container or not target_id or not container.startswith("container:"):
                        raise BuildHarborError("A shared scheme has an unresolved buildable reference.")
                    value = container.split(":", 1)[1]
                    if self._unsafe_path(value):
                        raise BuildHarborError("A shared scheme references an unsafe project path.")
                    project = self._secure(owner.parent / value, directory=True)
                    self.scheme_references.append((project, target_id))

    def _reject_private_schemes(self, owner: Path) -> None:
        private_root = owner / "xcuserdata"
        if not self._exists_without_following(private_root):
            return
        for user_directory in self._directory_contents(private_root, "private Xcode data"):
            try:
                metadata = user_directory.lstat()
            except OSError as exc:
                raise BuildHarborError("Cannot inspect private Xcode data.") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise BuildHarborError("A symlink is not supported in private Xcode data.")
            if not stat.S_ISDIR(metadata.st_mode):
                continue
            scheme_directory = user_directory / "xcschemes"
            if not self._exists_without_following(scheme_directory):
                continue
            for entry in self._directory_contents(scheme_directory, "private Xcode schemes"):
                if entry.suffix == ".xcscheme":
                    raise BuildHarborError("Private Xcode schemes are unsupported; use a reviewed shared scheme.")

    @staticmethod
    def _exists_without_following(path: Path) -> bool:
        try:
            path.lstat()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise BuildHarborError("Cannot inspect an Xcode metadata path.") from exc

    def _directory_contents(self, path: Path, description: str) -> list[Path]:
        directory = self._secure(path, directory=True)
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise BuildHarborError(f"Cannot enumerate {description}.") from exc
        self.directory_entries += len(entries)
        if self.directory_entries > _MAX_DIRECTORY_ENTRIES:
            raise BuildHarborError("Xcode metadata exceeds the supported directory entry count.")
        return entries

    @staticmethod
    def _tag(element: ET.Element) -> str:
        return element.tag.rsplit("}", 1)[-1]

    @staticmethod
    def _unsafe_text(value: str) -> bool:
        return any(ord(char) < 32 or ord(char) == 127 or char in _BIDI for char in value)

    @classmethod
    def _unsafe_path(cls, value: str) -> bool:
        path = Path(value)
        return (
            not value
            or cls._unsafe_text(value)
            or "$" in value
            or "~" in value
            or path.is_absolute()
        )

    @classmethod
    def _safe_value(cls, value: object) -> str:
        if not isinstance(value, str) or not value or cls._unsafe_text(value):
            return ""
        redacted = value.upper()
        if any(word in redacted for word in ("TOKEN", "SECRET", "PASSWORD", "AUTHORIZATION")):
            return ""
        printable = "".join(char for char in value if 32 <= ord(char) < 127)
        return f" (value {printable[:80]!r})" if printable else ""

    @staticmethod
    def _strip_comments(text: str) -> str:
        result: list[str] = []
        at = 0
        quote: str | None = None
        while at < len(text):
            char = text[at]
            if quote is not None:
                result.append(char)
                if char == "\\" and at + 1 < len(text):
                    at += 1
                    result.append(text[at])
                elif char == quote:
                    quote = None
                at += 1
                continue
            if char in {'"', "'"}:
                quote = char
                result.append(char)
                at += 1
                continue
            if text.startswith("//", at):
                end = text.find("\n", at + 2)
                if end < 0:
                    break
                result.append("\n")
                at = end + 1
                continue
            if text.startswith("/*", at):
                end = text.find("*/", at + 2)
                if end < 0:
                    raise BuildHarborError("An xcconfig file has an unterminated comment.")
                result.extend("\n" for char in text[at : end + 2] if char == "\n")
                at = end + 2
                continue
            result.append(char)
            at += 1
        if quote is not None:
            raise BuildHarborError("An xcconfig file has an unterminated quoted string.")
        return "".join(result)


def _screen_package_manifest(data: bytes) -> None:
    try:
        manifest = data.decode("utf-8")
    except UnicodeError as exc:
        raise BuildHarborError("A Swift package manifest is not valid UTF-8.") from exc
    try:
        inspected = _Inspector._strip_comments(manifest)
    except BuildHarborError as exc:
        raise BuildHarborError("A Swift package manifest uses an unsupported lexical form.") from exc
    if re.search(r"\.\s*(?:package|plugin|macro|binaryTarget)\s*\(", inspected):
        raise BuildHarborError(
            "Swift packages with dependencies, plugins, macros, or binary targets are unsupported."
        )


def _secure_resolved_path(path: Path, root: Path, *, directory: bool) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise BuildHarborError("A resolved Swift package path is missing or inaccessible.") from exc
    if resolved != candidate or not resolved.is_relative_to(root) or stat.S_ISLNK(metadata.st_mode):
        raise BuildHarborError("A resolved Swift package path is a symlink or escapes its clone directory.")
    if directory and not stat.S_ISDIR(metadata.st_mode):
        raise BuildHarborError("A resolved Swift package checkout is not a directory.")
    if not directory and not stat.S_ISREG(metadata.st_mode):
        raise BuildHarborError("A resolved Swift package manifest is not a regular file.")
    return resolved


def _read_resolved_manifest(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(path, flags)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_FILE_BYTES:
            raise BuildHarborError("A resolved Swift package manifest is not a bounded regular file.")
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
        if len(data) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise BuildHarborError("A resolved Swift package manifest changed during inspection.")
        return data
    except BuildHarborError:
        raise
    except OSError as exc:
        raise BuildHarborError("Cannot read a resolved Swift package manifest.") from exc
    finally:
        if fd is not None:
            os.close(fd)


def inspect_resolved_packages(package_clones: Path) -> dict[str, str] | None:
    """Snapshot one-level SwiftPM checkout manifests without launching a tool."""

    raw_root = Path(os.path.abspath(package_clones))
    try:
        raw_root.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BuildHarborError("Cannot inspect the Swift package clone directory.") from exc
    root = _secure_resolved_path(raw_root, raw_root, directory=True)
    checkouts_path = root / "checkouts"
    try:
        checkouts_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BuildHarborError("Cannot inspect the Swift package checkouts directory.") from exc
    checkouts = _secure_resolved_path(checkouts_path, root, directory=True)
    try:
        entries = sorted(checkouts.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise BuildHarborError("Cannot enumerate resolved Swift package checkouts.") from exc
    if not entries:
        return None
    if len(entries) > _MAX_DIRECTORY_ENTRIES:
        raise BuildHarborError("Resolved Swift packages exceed the supported checkout count.")

    fingerprints: dict[str, str] = {}
    total_bytes = 0
    for entry in entries:
        if _Inspector._unsafe_text(entry.name):
            raise BuildHarborError("A resolved Swift package checkout name contains unsafe characters.")
        checkout = _secure_resolved_path(entry, root, directory=True)
        manifest = _secure_resolved_path(checkout / "Package.swift", root, directory=False)
        data = _read_resolved_manifest(manifest)
        total_bytes += len(data)
        if total_bytes > _MAX_TOTAL_BYTES:
            raise BuildHarborError("Resolved Swift package manifests exceed the supported total byte count.")
        _screen_package_manifest(data)
        fingerprints[str(manifest)] = hashlib.sha256(data).hexdigest()
    return dict(sorted(fingerprints.items()))


def inspect_projects(identity: Path, policy_root: Path, managed_settings: set[str]) -> ProjectGraph:
    """Inspect local project/workspace membership without launching Xcode or writing files."""

    return _Inspector(policy_root, managed_settings).inspect(identity)


def revalidate_graph(graph: ProjectGraph, policy_root: Path) -> None:
    """Fail if any inspected source, member, target, include, or shared scheme changed."""

    current = inspect_projects(graph.identity, policy_root, set(graph.managed_settings))
    if (
        current.projects != graph.projects
        or current.fingerprints != graph.fingerprints
        or current.targets != graph.targets
        or current.remote_packages != graph.remote_packages
    ):
        raise BuildHarborError("The inspected Xcode project graph changed before execution.")
