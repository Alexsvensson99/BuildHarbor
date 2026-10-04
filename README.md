# BuildHarbor

BuildHarbor checks an external APFS volume before routing selected command-line Xcode output to it. Version 0.3.0 supports guarded build, test, archive, and local Copy App export; bounded project/workspace inspection; and read-only reporting for managed storage.

I want an external build drive to be an explicit part of the workflow. A path under `/Volumes` is not enough: the expected disk must be mounted, unlocked, and identified by its volume UUID. Build actions also require writable storage and the configured free capacity. If those checks fail, BuildHarbor does not silently fall back to internal storage.

BuildHarbor is still intentionally narrow. I use it with controlled projects and fixtures; there has been no external developer program or broad compatibility validation. It is a storage guard and reviewable command workflow, not a filesystem sandbox.

The runtime is Python 3.11 or later with no third-party runtime dependencies. The source is MIT licensed.

## Requirements and installation

- macOS. `doctor`, `plan`, and `run` need a full Xcode installation selected through `xcode-select` or `DEVELOPER_DIR`; `report` does not require Xcode.
- **Xcode 27.0, build 27A266a** for build, test, archive, and export. No adjacent Xcode version is implied.
- Python **3.11+**, `pip`, and Git.
- A mounted, unlocked **external APFS** volume. Build actions require write access and the configured free capacity. Reporting needs read/traverse access but permits a read-only or low-capacity volume.

Install version 0.3.0 from its GitHub tag in a virtual environment:

```sh
git clone --branch v0.3.0 --depth 1 https://github.com/Alexsvensson99/BuildHarbor.git
cd BuildHarbor
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/buildharbor --version
```

Use `.venv/bin/buildharbor` below, or activate the environment with `source .venv/bin/activate`. BuildHarbor is distributed through GitHub; it is not published to PyPI, Homebrew, or another registry. Installing the source uses setuptools as a build dependency.

## Configure a project

1. Inspect your disk with `diskutil info "/Volumes/Developer SSD"`. Replace the example name with the real mount point and copy the **Volume UUID**, not the disk or container UUID.
2. Copy `examples/buildharbor.local.toml.example` to `.buildharbor.local.toml` in the project root.
3. Add `.buildharbor.local.toml` to that project's `.gitignore` and edit it with the local mount, UUID, and storage root.

```toml
# .buildharbor.local.toml — private to this machine
schema_version = 1
mount = "/Volumes/Developer SSD"
volume_uuid = "REPLACE-WITH-YOUR-APFS-VOLUME-UUID"
storage_root = "/Volumes/Developer SSD/BuildHarbor"
```

Portable policy stays in `buildharbor.toml` and may be committed:

```toml
schema_version = 1
project_id = "my-project"
minimum_free_gib = 20
```

Run `buildharbor doctor` first. It reads the configuration, volume identity and capacity, permission metadata, and selected Xcode without writing a probe.

## Build and test

`plan` is read-only. It parses the selected project or workspace and shared scheme, reads static Xcode application metadata, and prints the command, controlled environment, and planned outputs without executing `xcodebuild` or resolving packages. It is a preview: `run` validates again and allocates its own unique outputs.

```sh
buildharbor plan -- \
  -project fixtures/HarborFixture/HarborFixture.xcodeproj \
  -scheme HarborFixture -destination 'platform=macOS' build
buildharbor run -- \
  -project fixtures/HarborFixture/HarborFixture.xcodeproj \
  -scheme HarborFixture -destination 'platform=macOS' -jobs 2 build
buildharbor run -- \
  -project fixtures/HarborFixture/HarborFixture.xcodeproj \
  -scheme HarborFixture -destination 'platform=macOS' \
  -parallel-testing-enabled NO test
```

These commands use the repository-owned fixture. Substitute your own accepted project and shared scheme after configuring its checkout.

For a workspace or nested graph with more than one project, provide explicit configuration and SDK values. BuildHarbor checks the selected scheme/action settings and every statically discovered member project before starting the requested action.

```sh
buildharbor plan -- \
  -workspace fixtures/HarborWorkspace/HarborWorkspace.xcworkspace \
  -scheme HarborWorkspace -configuration Debug -sdk macosx \
  -destination 'platform=macOS' build
buildharbor run -- \
  -workspace fixtures/HarborWorkspace/HarborWorkspace.xcworkspace \
  -scheme HarborWorkspace -configuration Debug -sdk macosx \
  -destination 'platform=macOS' build
```

During `run`, one bounded `-showBuildSettings -json` query covers the selected scheme and action. A multi-project graph also gets one `-project ... -alltargets` query per static member. Member queries omit `-derivedDataPath` and use explicit output roots, package routes, compiler/cache settings, and `TMPDIR`. Xcode 27 may repeat identical member records; only exact identical member duplicates are ignored. Missing, unknown, or conflicting targets block the action.

`run` holds one advisory lock per project and streams the requested Xcode action's output. It returns Xcode's exit status, uses `128 + signal` after a forwarded terminating signal, and uses exit 2 for BuildHarbor validation or wrapper failures. After verified storage preparation and child execution, it makes a best-effort receipt write; failures before storage is verified remain stderr and exit-status outcomes.

## Archive and local export

Archive support is limited to one simple macOS application with signing disabled or manual ad-hoc signing using identity `-`. Teams, profiles, keychains, extra signing flags, private identities, other platforms, and distribution workflows are rejected.

```sh
buildharbor run -- -project fixtures/HarborApp/HarborApp.xcodeproj \
  -scheme HarborApp -configuration Release \
  -destination 'platform=macOS' archive
```

Use the exact managed archive path from the successful archive run receipt for export. A path shown by an earlier plan belongs to that preview and is not the path allocated by `run`.

```sh
buildharbor plan -- \
  -exportArchive -archivePath '/managed/path/from/archive-receipt.xcarchive'
buildharbor run -- \
  -exportArchive -archivePath '/managed/path/from/archive-receipt.xcarchive'
```

Export accepts only a successful, unchanged BuildHarbor-managed archive for the same project storage and exact Xcode build. BuildHarbor generates fixed local `mac-application` / `export` options and unique outputs. Archive trees containing symlinks, multiply linked regular files, special files, cross-filesystem entries, more than one application, or changed contents are rejected.

The runtime validates effective signing settings and structurally inspects the archive and application. It does not run `codesign` to audit the produced signature or team. Strict ad-hoc/no-team verification in the fixture is evidence for that fixture, not a general signature audit.

## Read-only storage reporting

Report one configured checkout in text or schema-versioned JSON:

```sh
buildharbor report
buildharbor report --json
```

Repeat `--project-dir` to classify several configured checkouts that share the exact same mount, volume UUID, and storage root:

```sh
buildharbor report \
  --project-dir /path/to/project-a \
  --project-dir /path/to/project-b
```

The report counts each observed regular-file inode once. One known owner is project data, several known owners are shared, and ambiguous IDs, unknown locations, unseen/outside hard links, or conflicting metadata are unattributed. Directories are excluded. Symlinks and special files are skipped and counted without being followed or opened.

Reports include the configured project IDs used for attribution. Treat those IDs as potentially sensitive and redact them before sharing output when they identify private work.

Logical bytes are `st_size`; allocated bytes are `st_blocks * 512`. Neither number is APFS physical usage, clone sharing, snapshot usage, purgeable space, or reclaimable capacity. Scans are best-effort and non-atomic.

A complete report exits 0. An incomplete scan retains observed lower bounds, lists stable issue codes, and exits 2. A verified volume with an absent storage root returns a complete empty report without creating the root. The fixed ceilings are 200,000 entries, 100,000 distinct inodes, depth 64, and 30 seconds. The deadline is cooperative between filesystem calls and cannot interrupt blocked kernel I/O.

Reporting does not read regular-file contents or intentionally write content or metadata. Controlled checks preserve content, mode, modification time, and change time. Access time is excluded because the operating system may update it when metadata descriptors are opened.

## Routed locations

Each checkout gets a storage key beneath the configured root, and each Xcode distribution has separate reusable build directories. Every build, test, and archive action gets a unique result bundle; settings queries use separate unique result bundles.

| Output | Routing control |
| --- | --- |
| Build/test DerivedData, products, intermediates | `-derivedDataPath`, `SYMROOT`, `OBJROOT` |
| Package clones and repository cache | `-clonedSourcePackagesDirPath`, `-packageCachePath` |
| Clang/Swift module cache | `MODULE_CACHE_DIR`, `CLANG_MODULE_CACHE_PATH` |
| Compilation cache | `COMPILATION_CACHE_CAS_PATH` |
| Precompiled headers | `SHARED_PRECOMPS_DIR` |
| Install staging used by build settings | `DSTROOT` |
| Temporary files for processes that honor it | `TMPDIR` |
| Action and settings-query results | `-resultBundlePath` |
| Archive | `-archivePath`; action-specific roots below external Derived Data |
| Local export | managed `-exportPath` and generated `-exportOptionsPlist` |

The remote-package fixture verified repository-cache routing. Some SwiftPM manifest/metadata caches and other toolchain-managed state still use locations outside the supported claim. These controls do not prove that every Xcode, compiler, dependency, plugin, or macOS write occurs on the external volume.

## Accepted command surface

Build, test, and archive require exactly one explicit `-project` or `-workspace` and one statically inspected shared scheme. Accepted value options are:

```text
-project  -workspace  -scheme  -configuration  -destination  -sdk  -arch
-jobs  -destination-timeout  -parallel-testing-enabled
-parallel-testing-worker-count  -maximum-parallel-testing-workers
-maximum-concurrent-test-device-destinations
-maximum-concurrent-test-simulator-destinations
-enableCodeCoverage  -testLanguage  -testRegion
```

Accepted switches are `-quiet`, `-showBuildTimingSummary`, `-disableAutomaticPackageResolution`, `-onlyUsePackageVersionsFromResolvedFile`, and `-skipPackageUpdates`, plus inline `-only-testing:Identifier` and `-skip-testing:Identifier`. The caller-supplied build settings are limited to `CODE_SIGNING_ALLOWED`, `CODE_SIGNING_REQUIRED`, `ONLY_ACTIVE_ARCH`, and `ENABLE_TESTABILITY`, each set to `YES` or `NO`.

Unknown or duplicate arguments, caller-supplied output paths, command-line xcconfig files, inherited output/compiler/signing overrides, private or autogenerated schemes, script phases, custom rules, unsupported objects or copy destinations, unresolved graph references, and source-defined managed output settings are rejected. Shared schemes that select test plans through `TestPlans` or `TestPlanReference`, and caller-supplied `-testPlan`, are unsupported because BuildHarbor does not inspect that input graph.

This conservative surface can block a valid Xcode project.

## Limits and evidence

- BuildHarbor does not control GUI Xcode or tools that bypass its command and advisory lock.
- The volume can disconnect or change after a check. BuildHarbor revalidates at defined boundaries but cannot make Xcode writes atomic.
- Settings and receipts can contain project metadata and local paths. Review them before sharing.
- There is no cleanup, migration, global Xcode configuration, simulator relocation, background service, signing/distribution automation, or registry publication.
- Configuration, plan, receipt, doctor, error, and storage-report JSON use `schema_version: 1`; these contracts remain experimental until 1.0.
- Compatibility is limited to the exact platform, toolchain, and controlled project shapes in the verification record.

All 117 unit and small-process tests passed under Python 3.11.13 and 3.13.4. Controlled local checks passed build, test, a two-member workspace build, archive, local export, and report classification on Apple Silicon with macOS 27.0 (26A428) and Xcode 27.0 (27A266a). An isolated installation of the committed candidate revision also passed before release preparation; that is not a fresh installation from the final tag. The evidence does not establish broader project, platform, signing, or filesystem compatibility.

See the [0.3 verification record](docs/verification-0.3.md), [release notes](docs/release-0.3.0.md), [roadmap](ROADMAP.md), [contribution guide](CONTRIBUTING.md), [security policy](SECURITY.md), [storage-guard decision](docs/adr/0001-storage-guard.md), [effective-settings/archive decision](docs/adr/0002-effective-settings-and-local-export.md), and [reporting decision](docs/adr/0003-read-only-storage-reporting.md).
