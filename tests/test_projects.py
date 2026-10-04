"""Pure-Python, read-only tests for bounded Xcode source-graph inspection."""

from pathlib import Path
import re
import shutil
import tempfile
import unittest

from buildharbor.errors import BuildHarborError
from buildharbor.projects import inspect_projects, inspect_resolved_packages, revalidate_graph


MANAGED = {"SYMROOT", "OBJROOT", "CONFIGURATION_BUILD_DIR"}


def project_text(objects: str = "") -> str:
    if not objects:
        return "// !$*UTF8*$!\n{ objects = {}; }\n"
    targets = re.findall(r"\b([A-Za-z0-9]+)\s*=\s*\{\s*isa\s*=\s*PBX(?:Native|Aggregate)Target", objects)
    support = (
        "ROOT = { isa = PBXProject; buildConfigurationList = ROOT_CONFIGS; mainGroup = ROOT_GROUP; "
        f"targets = ({','.join(targets)}); }};\n"
        "ROOT_CONFIGS = { isa = XCConfigurationList; buildConfigurations = (); };\n"
        "ROOT_GROUP = { isa = PBXGroup; children = (); sourceTree = \"<group>\"; };\n"
    )
    return "// !$*UTF8*$!\n{ objects = {\n" + support + objects + "\n}; rootObject = ROOT; }\n"


def target(target_id: str, name: str) -> str:
    return (
        f"{target_id} = {{ isa = PBXNativeTarget; name = {name}; "
        "buildConfigurationList = ROOT_CONFIGS; buildRules = (); buildPhases = (); dependencies = (); };\n"
    )


class ProjectInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="buildharbor-projects-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def make_project(self, relative: str, objects: str = "") -> Path:
        project = self.root / relative
        project.mkdir(parents=True)
        (project / "project.pbxproj").write_text(project_text(objects), encoding="utf-8")
        return project

    def write_scheme(self, owner: Path, project: str, target_id: str, *, actions: str = "") -> Path:
        directory = owner / "xcshareddata/xcschemes"
        directory.mkdir(parents=True, exist_ok=True)
        scheme = directory / "Shared.xcscheme"
        scheme.write_text(
            "<Scheme>"
            f"{actions}"
            "<BuildAction><BuildActionEntries><BuildActionEntry>"
            f'<BuildableReference BlueprintIdentifier="{target_id}" ReferencedContainer="container:{project}"/>'
            "</BuildActionEntry></BuildActionEntries></BuildAction>"
            "</Scheme>",
            encoding="utf-8",
        )
        return scheme

    def test_empty_synthetic_project_is_allowed_and_revalidation_detects_change(self):
        project = self.make_project("Empty.xcodeproj")
        graph = inspect_projects(project, self.root, MANAGED)
        self.assertEqual(graph.projects, (project,))
        self.assertEqual(graph.targets[project], ())
        self.assertIn(str(project / "project.pbxproj"), graph.fingerprints)
        revalidate_graph(graph, self.root)

        (project / "project.pbxproj").write_text(project_text(target("T1", "Changed")), encoding="utf-8")
        with self.assertRaises(BuildHarborError):
            revalidate_graph(graph, self.root)

    def test_repository_fixture_parses_without_running_xcode(self):
        repository = Path(__file__).resolve().parents[1]
        fixture_root = repository / "fixtures/HarborFixture"
        project = fixture_root / "HarborFixture.xcodeproj"
        graph = inspect_projects(project, fixture_root, MANAGED)
        self.assertEqual(graph.projects, (project,))
        self.assertEqual(graph.targets[project], ("HarborFixture", "HarborFixtureTests"))
        self.assertTrue(any(path.endswith("HarborFixture.xcscheme") for path in graph.fingerprints))
        self.assertTrue(any(path.endswith("HarborSupport/Package.swift") for path in graph.fingerprints))

    def test_repository_workspace_fixture_inventories_every_static_member(self):
        repository = Path(__file__).resolve().parents[1]
        fixture_root = repository / "fixtures/HarborWorkspace"
        workspace = fixture_root / "HarborWorkspace.xcworkspace"

        graph = inspect_projects(workspace, fixture_root, MANAGED)

        expected = {
            fixture_root / "Primary/HarborPrimary.xcodeproj",
            fixture_root / "Primary/Nested/HarborNested.xcodeproj",
        }
        self.assertEqual(set(graph.projects), expected)
        self.assertEqual(graph.targets[fixture_root / "Primary/HarborPrimary.xcodeproj"], ("HarborPrimary",))
        self.assertEqual(graph.targets[fixture_root / "Primary/Nested/HarborNested.xcodeproj"], ("HarborNested",))

        conflict = fixture_root / "ConflictWorkspace.xcworkspace"
        with self.assertRaisesRegex(BuildHarborError, "Managed setting"):
            inspect_projects(conflict, fixture_root, MANAGED)

    def test_workspace_groups_and_nested_projects_are_fully_inventoried(self):
        workspace = self.root / "All.xcworkspace"
        workspace.mkdir()
        (workspace / "contents.xcworkspacedata").write_text(
            '<Workspace><Group location="group:Modules">'
            '<FileRef location="group:Primary.xcodeproj"/>'
            "</Group></Workspace>",
            encoding="utf-8",
        )
        primary = self.make_project(
            "Modules/Primary.xcodeproj",
            target("T1", "Primary")
            + "F1 = { isa = PBXFileReference; lastKnownFileType = wrapper.pb-project; "
            "path = Nested/Nested.xcodeproj; sourceTree = SOURCE_ROOT; };",
        )
        nested = self.make_project("Modules/Nested/Nested.xcodeproj", target("T2", "Nested"))
        self.write_scheme(workspace, "Modules/Primary.xcodeproj", "T1")

        graph = inspect_projects(workspace, self.root, MANAGED)
        self.assertEqual(graph.projects, tuple(sorted((primary, nested), key=str)))
        self.assertEqual(graph.targets[primary], ("Primary",))
        self.assertEqual(graph.targets[nested], ("Nested",))

    def test_nested_workspace_shared_schemes_are_snapshotted_and_checked(self):
        outer = self.root / "Outer.xcworkspace"
        inner = self.root / "Inner.xcworkspace"
        outer.mkdir()
        inner.mkdir()
        (outer / "contents.xcworkspacedata").write_text(
            '<Workspace><FileRef location="group:Inner.xcworkspace"/></Workspace>', encoding="utf-8"
        )
        (inner / "contents.xcworkspacedata").write_text(
            '<Workspace><FileRef location="group:App.xcodeproj"/></Workspace>', encoding="utf-8"
        )
        self.make_project("App.xcodeproj", target("T1", "App"))
        scheme = self.write_scheme(inner, "App.xcodeproj", "T1")

        graph = inspect_projects(outer, self.root, MANAGED)
        self.assertIn(str(scheme), graph.fingerprints)

        self.write_scheme(inner, "App.xcodeproj", "T1", actions="<TestAction><PreActions/></TestAction>")
        with self.assertRaisesRegex(BuildHarborError, "pre-actions"):
            inspect_projects(outer, self.root, MANAGED)

    def test_shared_scheme_test_plan_references_are_rejected(self):
        project = self.make_project("Planned.xcodeproj", target("T1", "Planned"))
        self.write_scheme(
            project,
            "Planned.xcodeproj",
            "T1",
            actions=(
                '<TestAction><TestPlans><TestPlanReference '
                'reference="container:Plans/Planned.xctestplan" default="YES"/>'
                "</TestPlans></TestAction>"
            ),
        )

        with self.assertRaisesRegex(BuildHarborError, "test plans"):
            inspect_projects(project, self.root, MANAGED)

    def test_conditional_managed_setting_cannot_hide_behind_comment_markers(self):
        project = self.make_project(
            "Conflict.xcodeproj",
            'C1 = { isa = XCBuildConfiguration; note = "literal /* marker"; '
            'buildSettings = { "SYMROOT[sdk=macosx*]" = /tmp/escape; }; marker = "*/"; };',
        )
        with self.assertRaisesRegex(BuildHarborError, "Managed setting SYMROOT"):
            inspect_projects(project, self.root, MANAGED)

    def test_scripts_custom_rules_and_escaping_copy_phases_are_rejected(self):
        cases = {
            "script": "S1 = { isa = PBXShellScriptBuildPhase; shellScript = echo; };",
            "rule": "R1 = { isa = PBXBuildRule; script = echo; };",
            "target-rule": target("T1", "App").replace("buildRules = ();", "buildRules = (R1);")
            + "R1 = { isa = PBXBuildFile; };",
            "copy": "C1 = { isa = PBXCopyFilesBuildPhase; dstSubfolderSpec = 10; dstPath = ../../escape; };",
            "unknown": "U1 = { isa = PBXFutureExecutablePhase; command = echo; };",
            "malformed-source-tree": "F1 = { isa = PBXFileReference; path = Source.swift; sourceTree = (bad); };",
        }
        for name, objects in cases.items():
            project = self.make_project(f"{name}.xcodeproj", objects)
            with self.subTest(name=name), self.assertRaises(BuildHarborError):
                inspect_projects(project, self.root, MANAGED)

    def test_recursive_xcconfig_includes_are_fingerprinted_and_checked(self):
        config_dir = self.root / "Config"
        config_dir.mkdir()
        base = config_dir / "Base.xcconfig"
        child = config_dir / "Child.xcconfig"
        base.write_text('#include "Child.xcconfig"\nPRODUCT_NAME = Safe\n', encoding="utf-8")
        child.write_text("OTHER_SETTING = yes\n", encoding="utf-8")
        project = self.make_project(
            "Configured.xcodeproj",
            "F1 = { isa = PBXFileReference; lastKnownFileType = text.xcconfig; "
            "path = Config/Base.xcconfig; sourceTree = SOURCE_ROOT; };"
            "C1 = { isa = XCBuildConfiguration; baseConfigurationReference = F1; buildSettings = {}; };",
        )
        graph = inspect_projects(project, self.root, MANAGED)
        self.assertIn(str(base), graph.fingerprints)
        self.assertIn(str(child), graph.fingerprints)

        child.write_text('OBJROOT[config=Debug] = "/tmp/escape"\n', encoding="utf-8")
        with self.assertRaisesRegex(BuildHarborError, "Managed setting OBJROOT"):
            inspect_projects(project, self.root, MANAGED)

    def test_xcconfig_cycles_missing_optional_includes_and_symlinks_fail_closed(self):
        config_dir = self.root / "Config"
        config_dir.mkdir()
        base = config_dir / "Base.xcconfig"
        child = config_dir / "Child.xcconfig"
        base.write_text('#include "Child.xcconfig"\n', encoding="utf-8")
        child.write_text('#include "Base.xcconfig"\n', encoding="utf-8")
        project = self.make_project(
            "Configured.xcodeproj",
            "F1 = { isa = PBXFileReference; path = Config/Base.xcconfig; sourceTree = SOURCE_ROOT; };"
            "C1 = { isa = XCBuildConfiguration; baseConfigurationReference = F1; buildSettings = {}; };",
        )
        with self.assertRaisesRegex(BuildHarborError, "cycle"):
            inspect_projects(project, self.root, MANAGED)

        base.write_text('#include? "Missing.xcconfig"\n', encoding="utf-8")
        with self.assertRaises(BuildHarborError):
            inspect_projects(project, self.root, MANAGED)

        actual = config_dir / "Actual.xcconfig"
        actual.write_text("OTHER_SETTING = yes\n", encoding="utf-8")
        linked = config_dir / "Linked.xcconfig"
        linked.symlink_to(actual)
        base.write_text('#include "Linked.xcconfig"\n', encoding="utf-8")
        with self.assertRaisesRegex(BuildHarborError, "symlink"):
            inspect_projects(project, self.root, MANAGED)

    def test_local_package_manifest_is_fingerprinted_and_executable_extensions_are_rejected(self):
        package = self.root / "Packages/Local"
        package.mkdir(parents=True)
        manifest = package / "Package.swift"
        manifest.write_text(
            "import PackageDescription\nlet package = Package(name: \"Local\", targets: [.target(name: \"Local\")])\n",
            encoding="utf-8",
        )
        project = self.make_project(
            "PackageApp.xcodeproj",
            "P1 = { isa = XCLocalSwiftPackageReference; relativePath = Packages/Local; };",
        )
        graph = inspect_projects(project, self.root, MANAGED)
        self.assertIn(str(manifest), graph.fingerprints)

        manifest.write_text(
            "import PackageDescription\nlet package = Package(name: \"Local\", targets: [.plugin(name: \"Run\")])\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BuildHarborError, "plugins"):
            inspect_projects(project, self.root, MANAGED)

    def test_http_remote_package_with_literal_exact_version_is_statically_accepted(self):
        repository = Path(__file__).resolve().parents[1]
        source = self.root / "RemoteFixture"
        shutil.copytree(repository / "fixtures/HarborFixture", source)
        project = source / "HarborFixture.xcodeproj"
        pbx = project / "project.pbxproj"
        text = pbx.read_text(encoding="utf-8")
        text = text.replace("XCLocalSwiftPackageReference", "XCRemoteSwiftPackageReference")
        text = text.replace(
            "relativePath = Packages/HarborSupport;",
            'repositoryURL = "http://127.0.0.1:8123/HarborSupport.git"; '
            "requirement = { kind = exactVersion; version = 1.0.0; };",
        )
        pbx.write_text(text, encoding="utf-8")

        graph = inspect_projects(project, source, MANAGED)

        self.assertEqual(graph.remote_packages, ("http://127.0.0.1:8123/HarborSupport.git",))

        file_project = self.make_project(
            "FileRemote.xcodeproj",
            'P1 = { isa = XCRemoteSwiftPackageReference; repositoryURL = "file:///Volumes/Packages/Repo.git"; '
            "requirement = { kind = exactVersion; version = 2.1.0-beta.1+fixture; }; };",
        )
        self.assertEqual(
            inspect_projects(file_project, self.root, MANAGED).remote_packages,
            ("file:///Volumes/Packages/Repo.git",),
        )

    def test_remote_package_metadata_rejects_credentials_and_nonexact_requirements(self):
        cases = {
            "credentials": (
                "https://user:password@example.invalid/Package.git",
                "{ kind = exactVersion; version = 1.0.0; }",
            ),
            "encoded-credentials": (
                "https://user%3Apassword%40example.invalid/Package.git",
                "{ kind = exactVersion; version = 1.0.0; }",
            ),
            "branch": ("https://example.invalid/Package.git", "{ kind = branch; branch = main; }"),
            "variable-version": (
                "https://example.invalid/Package.git",
                "{ kind = exactVersion; version = CURRENT_VERSION; }",
            ),
        }
        for name, (url, requirement) in cases.items():
            project = self.make_project(
                f"remote-{name}.xcodeproj",
                f'P1 = {{ isa = XCRemoteSwiftPackageReference; repositoryURL = "{url}"; '
                f"requirement = {requirement}; }};"
                "D1 = { isa = XCSwiftPackageProductDependency; package = P1; productName = Product; };",
            )
            with self.subTest(name=name), self.assertRaises(BuildHarborError):
                inspect_projects(project, self.root, MANAGED)

    def test_resolved_package_manifests_are_bounded_snapshotted_and_screened(self):
        clones = self.root / "SourcePackages"
        self.assertIsNone(inspect_resolved_packages(clones))
        manifest = clones / "checkouts/Safe/Package.swift"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            "import PackageDescription\nlet package = Package(name: \"Safe\", targets: [.target(name: \"Safe\")])\n",
            encoding="utf-8",
        )

        fingerprints = inspect_resolved_packages(clones)

        self.assertIsNotNone(fingerprints)
        assert fingerprints is not None
        self.assertEqual(set(fingerprints), {str(manifest)})
        manifest.write_text(
            "import PackageDescription\n"
            "let package = Package(name: \"Unsafe\", targets: [. /* hidden */ plugin(name: \"Run\")])\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BuildHarborError, "plugins"):
            inspect_resolved_packages(clones)

        manifest.write_text(
            "import PackageDescription\nlet package = Package(name: \"Safe\", targets: [.target(name: \"Safe\")])\n",
            encoding="utf-8",
        )
        alias = clones / "checkouts/Alias"
        alias.symlink_to(manifest.parent, target_is_directory=True)
        with self.assertRaisesRegex(BuildHarborError, "symlink"):
            inspect_resolved_packages(clones)

    def test_workspace_traversal_outside_symlink_cycle_and_entities_are_rejected(self):
        workspace = self.root / "Unsafe.xcworkspace"
        workspace.mkdir()
        contents = workspace / "contents.xcworkspacedata"
        cases = (
            '<Workspace><FileRef location="group:../Outside.xcodeproj"/></Workspace>',
            '<!DOCTYPE x [<!ENTITY e "value">]><Workspace/>',
        )
        for xml in cases:
            contents.write_text(xml, encoding="utf-8")
            with self.subTest(xml=xml), self.assertRaises(BuildHarborError):
                inspect_projects(workspace, self.root, MANAGED)

        project = self.make_project("Actual.xcodeproj")
        link = self.root / "Linked.xcodeproj"
        link.symlink_to(project, target_is_directory=True)
        contents.write_text('<Workspace><FileRef location="group:Linked.xcodeproj"/></Workspace>', encoding="utf-8")
        with self.assertRaisesRegex(BuildHarborError, "symlink"):
            inspect_projects(workspace, self.root, MANAGED)

        other = self.root / "Other.xcworkspace"
        other.mkdir()
        contents.write_text('<Workspace><FileRef location="group:Other.xcworkspace"/></Workspace>', encoding="utf-8")
        (other / "contents.xcworkspacedata").write_text(
            '<Workspace><FileRef location="group:Unsafe.xcworkspace"/></Workspace>', encoding="utf-8"
        )
        with self.assertRaisesRegex(BuildHarborError, "cycle"):
            inspect_projects(workspace, self.root, MANAGED)

    def test_parent_segments_may_stay_inside_policy_but_never_cross_or_mask_symlinks(self):
        modules = self.root / "Modules"
        modules.mkdir()
        project = self.make_project("Shared/Safe.xcodeproj")
        workspace = modules / "All.xcworkspace"
        workspace.mkdir()
        contents = workspace / "contents.xcworkspacedata"
        contents.write_text(
            '<Workspace><FileRef location="group:../Shared/Safe.xcodeproj"/></Workspace>', encoding="utf-8"
        )
        self.assertEqual(inspect_projects(workspace, self.root, MANAGED).projects, (project,))

        outside = self.root.parent / f"{self.root.name}-outside.xcodeproj"
        outside.mkdir()
        self.addCleanup(lambda: outside.rmdir())
        (outside / "project.pbxproj").write_text(project_text(), encoding="utf-8")
        self.addCleanup(lambda: (outside / "project.pbxproj").unlink())
        contents.write_text(
            f'<Workspace><FileRef location="group:../../{outside.name}"/></Workspace>', encoding="utf-8"
        )
        with self.assertRaisesRegex(BuildHarborError, "escapes"):
            inspect_projects(workspace, self.root, MANAGED)

        child = modules / "Child"
        child.mkdir()
        masked = self.make_project("Modules/Child/Masked.xcodeproj")
        link = modules / "Link"
        link.symlink_to(child, target_is_directory=True)
        # Both lexical normalization and symlink-aware resolution end at the
        # same project, but the original spelling still traverses Link.
        contents.write_text(
            '<Workspace><FileRef location="group:Link/../Child/Masked.xcodeproj"/></Workspace>', encoding="utf-8"
        )
        self.assertTrue(masked.is_dir())
        with self.assertRaisesRegex(BuildHarborError, "symlink"):
            inspect_projects(workspace, self.root, MANAGED)

    def test_shared_scheme_actions_unknown_targets_and_later_additions_fail_closed(self):
        project = self.make_project("App.xcodeproj", target("T1", "App"))
        self.write_scheme(project, "App.xcodeproj", "T1", actions="<PreActions/>")
        with self.assertRaises(BuildHarborError):
            inspect_projects(project, self.root, MANAGED)

        self.write_scheme(project, "App.xcodeproj", "UNKNOWN")
        with self.assertRaisesRegex(BuildHarborError, "outside the inspected graph"):
            inspect_projects(project, self.root, MANAGED)

        scheme = self.write_scheme(project, "App.xcodeproj", "T1")
        graph = inspect_projects(project, self.root, MANAGED)
        (scheme.parent / "Second.xcscheme").write_text(scheme.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaises(BuildHarborError):
            revalidate_graph(graph, self.root)

    def test_private_scheme_cannot_shadow_a_reviewed_shared_scheme(self):
        project = self.make_project("App.xcodeproj", target("T1", "App"))
        self.write_scheme(project, "App.xcodeproj", "T1")
        private = project / "xcuserdata/user.xcuserdatad/xcschemes"
        private.mkdir(parents=True)
        (private / "xcschememanagement.plist").write_text("{}", encoding="utf-8")
        inspect_projects(project, self.root, MANAGED)

        (private / "Shared.xcscheme").write_text("<Scheme/>", encoding="utf-8")
        with self.assertRaisesRegex(BuildHarborError, "Private Xcode schemes"):
            inspect_projects(project, self.root, MANAGED)

    def test_workspace_group_depth_is_bounded(self):
        workspace = self.root / "Deep.xcworkspace"
        workspace.mkdir()
        nested = '<FileRef location="group:Missing.xcodeproj"/>'
        for _ in range(70):
            nested = f"<Group>{nested}</Group>"
        (workspace / "contents.xcworkspacedata").write_text(
            f"<Workspace>{nested}</Workspace>", encoding="utf-8"
        )
        with self.assertRaisesRegex(BuildHarborError, "depth"):
            inspect_projects(workspace, self.root, MANAGED)

    def test_duplicate_openstep_keys_and_ambiguous_target_names_are_rejected(self):
        duplicate = self.make_project("Duplicate.xcodeproj")
        (duplicate / "project.pbxproj").write_text("{ objects = {}; objects = {}; }", encoding="utf-8")
        with self.assertRaisesRegex(BuildHarborError, "duplicate"):
            inspect_projects(duplicate, self.root, MANAGED)

        ambiguous = self.make_project(
            "Ambiguous.xcodeproj", target("T1", "Same") + target("T2", "Same")
        )
        with self.assertRaisesRegex(BuildHarborError, "ambiguous"):
            inspect_projects(ambiguous, self.root, MANAGED)

    def test_only_exact_empty_objects_dictionary_gets_synthetic_fixture_exception(self):
        malformed = {
            "top-level-setting": "{ objects = {}; SYMROOT = elsewhere; }",
            "top-level-reference": "{ objects = {}; baseConfigurationReference = ABC; }",
            "missing-objects": "{ rootObject = ROOT; }",
            "bare-file-reference": "{ F1 = { isa = PBXFileReference; path = File; }; }",
            "missing-root": "{ objects = { F1 = { isa = PBXFileReference; path = File; }; }; }",
        }
        for name, text in malformed.items():
            project = self.make_project(f"{name}.xcodeproj")
            (project / "project.pbxproj").write_text(text, encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(BuildHarborError):
                inspect_projects(project, self.root, MANAGED)


if __name__ == "__main__":
    unittest.main()
