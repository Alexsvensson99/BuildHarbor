# BuildHarbor

BuildHarbor checks an external APFS volume before routing selected Xcode build outputs to it. The published 0.1.0 release supports guarded command-line builds and tests. The validated 0.2 development checkpoint adds effective-setting checks, restrictive workspace traversal, local archives, and local Copy App exports. The current local 0.3 work adds bounded, read-only storage reporting.

I want an external build drive to be an explicit part of the workflow. A path under `/Volumes` is not enough: the expected disk needs to be mounted, unlocked, writable, and identified by its volume UUID. If those checks fail, BuildHarbor stops before launching the build.

The intended audience is independent Apple developers and small teams using command-line builds, including builds started by coding agents. For now, I am focusing on my own controlled projects and internal validation. It is too early to invite other developers to use it.

BuildHarbor uses Python 3.11 or later and has no third-party runtime dependencies. The source is MIT licensed.

## Requirements and installation

- macOS. `doctor`, `plan`, and `run` need a full Xcode installation selected through `xcode-select` or `DEVELOPER_DIR`; `report` does not require Xcode.
- **Xcode 27.0, build 27A266a** for build/test/archive/export workflows. Both the published release and the current development line accept only this exact distribution. Other versions need their own integration evidence before support is added.
- Python **3.11+**, `pip`, and Git.
- A mounted, unlocked **external APFS** volume. Build/test/archive/export require it to be writable and above the configured capacity threshold. Reporting requires read/traverse access but permits a read-only or low-capacity volume.

Install the published 0.1.0 release in a virtual environment:

```sh
git clone --branch v0.1.0 --depth 1 https://github.com/Alexsvensson99/BuildHarbor.git
cd BuildHarbor
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/buildharbor --version
```

Use `.venv/bin/buildharbor` in the examples below, or activate the environment with `source .venv/bin/activate`. This release is distributed through GitHub; it is not published to PyPI or Homebrew. Installing the source uses setuptools as a build dependency.

## Current development status

Version 0.2 is implemented on the development branch and has completed one controlled local sequence on Xcode 27.0 (27A266a): build, test, a workspace build covering two member projects, archive, and export. The exported application passed strict code-signature verification as ad-hoc with no team identifier, and the sequence did not launch Xcode's GUI. The 94-test unit/process suite also passed under Python 3.11.13 and 3.13.4. This is local development evidence; version 0.2 has not passed its new-release CI gate and has not been published as a GitHub release. [The 0.2 verification record](docs/verification-0.2.md) gives the exact evidence and remaining limits.

Version 0.3 reporting is implemented in the current local checkout. A controlled real-storage fixture has passed with project, shared, unattributed, absent-root, and incomplete outcomes while preserving the checked content and metadata. All 117 unit/process tests pass under Python 3.11.13 and 3.13.4. The final guarded archive/export regression and clean-install checks are still running. Version 0.3 is not part of the published 0.1.0 install and has not been released. [The 0.3 verification record](docs/verification-0.3.md) will carry the final evidence boundary.

The 0.2 command surface keeps `doctor`, `plan`, and `run`:

- For build, test, and archive, `plan` parses the selected project or workspace, its shared scheme, referenced projects, configurations, phases, rules, package references, and managed settings. Export planning reads and fingerprints the managed archive and its receipt. Planning remains read-only and never starts `xcodebuild`.
- For build, test, and archive, `run` repeats the static checks, takes the project lock, and runs a bounded `-showBuildSettings -json` query for the selected scheme and action. When the graph contains more than one project, it also queries every statically found project with `-alltargets`. A missing member, unexpected target, unresolved path, unsafe output setting, script phase, custom rule, or unsupported copy destination blocks the requested action.
- The selected-scheme query uses the same managed Derived Data, package, compiler-cache, temporary, and result paths as the requested action. Member `-alltargets` queries omit `-derivedDataPath`, which is not a supported combination in the tested toolchain, and instead supply explicit managed output roots, package paths, cache settings, and `TMPDIR`.
- `archive` is limited to one simple macOS application with signing disabled or manual ad-hoc signing using identity `-`. Teams, provisioning profiles, keychains, extra signing flags, and private certificate identities are rejected. BuildHarbor validates effective signing settings and structurally inspects the archive/application; it does not run `codesign` to audit the produced signature or team. The strict ad-hoc/no-team result above is controlled-fixture evidence.
- `-exportArchive` accepts only a successful, unchanged BuildHarbor-managed archive. BuildHarbor generates a fixed local `mac-application` / `export` options plist and unique export directory. It rejects archive trees containing symlinks, hard-linked regular files, special files, or more than one application.

The portable configuration and receipt format remain at `schema_version = 1`. Version 0.2 adds receipt fields for source identity, settings validation, inputs, and archive identity without changing the meaning of existing fields. See [ADR 0002](docs/adr/0002-effective-settings-and-local-export.md) for the complete decision and current limits.

## Current local checkout examples

These examples use unreleased 0.2/0.3 behavior from this source checkout. They do not work with the published 0.1.0 tag shown in the installation section. `-B` keeps Python bytecode out of the checkout.

Plan and run the two-member workspace fixture:

```sh
PYTHONPATH=src python3 -B -m buildharbor plan -- \
  -workspace fixtures/HarborWorkspace/HarborWorkspace.xcworkspace \
  -scheme HarborWorkspace -configuration Debug -sdk macosx \
  -destination 'platform=macOS' build
PYTHONPATH=src python3 -B -m buildharbor run -- \
  -workspace fixtures/HarborWorkspace/HarborWorkspace.xcworkspace \
  -scheme HarborWorkspace -configuration Debug -sdk macosx \
  -destination 'platform=macOS' build
```

Archive the simple macOS app, then export the exact managed archive path printed by the archive plan or receipt:

```sh
PYTHONPATH=src python3 -B -m buildharbor run -- \
  -project fixtures/HarborApp/HarborApp.xcodeproj \
  -scheme HarborApp -configuration Release \
  -destination 'platform=macOS' archive
PYTHONPATH=src python3 -B -m buildharbor plan -- \
  -exportArchive -archivePath '/managed/path/from/archive-receipt.xcarchive'
PYTHONPATH=src python3 -B -m buildharbor run -- \
  -exportArchive -archivePath '/managed/path/from/archive-receipt.xcarchive'
```

Report the configured checkout's managed storage in text or JSON:

```sh
PYTHONPATH=src python3 -B -m buildharbor report
PYTHONPATH=src python3 -B -m buildharbor report --json
```

To classify storage for several configured checkouts that share the exact same mount, volume UUID, and storage root, repeat `--project-dir`:

```sh
PYTHONPATH=src python3 -B -m buildharbor report \
  --project-dir /path/to/project-a \
  --project-dir /path/to/project-b
```

`report` does not require Xcode. It still requires readable project configuration and the correctly identified external APFS volume.

## First run with the published 0.1.0 release

The repository includes a small macOS framework and hostless XCTest fixture. It needs no simulator, signing account, or third-party package download.

1. Inspect **your** disk with `diskutil info "/Volumes/Developer SSD"`. Replace the example disk name with its actual mount point. Copy the **Volume UUID**, not the disk/container UUID.
2. Copy `examples/buildharbor.local.toml.example` to `.buildharbor.local.toml` in this checkout.
3. Edit the copy with your mount point, volume UUID, and a storage directory below that mount. The directory may be missing; BuildHarbor creates it only when you run a build.

```toml
# .buildharbor.local.toml — private to this machine, ignored by Git
schema_version = 1
mount = "/Volumes/Developer SSD"
volume_uuid = "REPLACE-WITH-YOUR-APFS-VOLUME-UUID"
storage_root = "/Volumes/Developer SSD/BuildHarbor"
```

Portable policy lives in `buildharbor.toml`, which you can commit:

```toml
schema_version = 1
project_id = "buildharbor-fixture"
minimum_free_gib = 2
```

Then run the complete workflow:

```sh
buildharbor doctor
buildharbor plan -- -project fixtures/HarborFixture/HarborFixture.xcodeproj \
  -scheme HarborFixture -destination 'platform=macOS' build
buildharbor plan --json -- -project fixtures/HarborFixture/HarborFixture.xcodeproj \
  -scheme HarborFixture -destination 'platform=macOS' test
buildharbor run -- -project fixtures/HarborFixture/HarborFixture.xcodeproj \
  -scheme HarborFixture -destination 'platform=macOS' -jobs 2 build
buildharbor run -- -project fixtures/HarborFixture/HarborFixture.xcodeproj \
  -scheme HarborFixture -destination 'platform=macOS' -jobs 2 \
  -parallel-testing-enabled NO test
```

For your own project, place both configuration files in its root and add `.buildharbor.local.toml` to **that project's** `.gitignore`. Keep the portable policy separate from the local file. Run the commands there, or use `buildharbor plan --project-dir /path/to/project -- ...`. Project arguments resolve relative to that directory.

## What each command does in the current local checkout

**`doctor`** reads configuration, the mounted volume's UUID and APFS format, external-disk status, available capacity, permission metadata, and selected Xcode. It does not write a probe. “Permission metadata permits access” is not proof that a real write will succeed. `doctor --json` makes the same distinction.

**`plan`** validates and prints an argument array, the controlled environment changes, and planned output paths. It creates no directories, resolves no packages, and does not execute `xcodebuild`. Xcode identity comes from the selected installation's version plist. Project and workspace inspection is a bounded static read of source metadata. A plan is a preview: `run` validates again and allocates its own unique receipt, result, archive, or export names.

The installed launcher disables Python bytecode writes before importing BuildHarbor. When running directly from source, use `PYTHONPATH=src python3 -B -m buildharbor` to keep interpreter cache files out of the checkout too.

**`run`** validates again, creates directories on the verified volume, takes an exclusive lock for that checkout and Xcode distribution, and checks a small real write. Project actions perform the bounded effective-setting queries described above; export rechecks the bound archive and receipt. It then rechecks the inputs, volume, and unique destinations before executing the requested action. Output streams directly to your terminal. Interrupt and termination signals are forwarded to each Xcode process group. The normal exit code is Xcode's exit code; signal exits use `128 + signal`. Configuration and wrapper failures use code `2`.

The local receipt includes the BuildHarbor and Xcode versions, action, result, planned paths, observed directory contents, settings-validation summary, input identities where applicable, and unverified behavior. A successful archive receipt also binds the archive digest and the one inspected application. An empty directory is not evidence that Xcode used it. Receipts omit command arguments, the inherited environment, and the captured settings payload. Build logs still belong to Xcode and your project; treat them as potentially private.

**`report`** scans one configured storage root without running Xcode or offering cleanup. `--project-dir` may be repeated, but every configuration must name the exact same mount, volume UUID, and storage root. The text and `--json` forms show per-project, shared, and unattributed unique regular-file totals. A complete report exits 0. An incomplete report preserves observed lower bounds, lists stable issue codes, and exits 2. A verified volume with an absent storage root is a complete empty report.

Reporting opens directories and regular files read-only to compare inode metadata; it does not read file contents or write managed storage. The scanner does not count directories. It counts symlinks and special files as ignored, without following them. Logical bytes are unique-inode `st_size`; allocated bytes are unique-inode `st_blocks * 512`. Neither value is an APFS physical-usage, clone-sharing, or reclaimable-capacity estimate. The scan is best-effort and non-atomic.

## Routed locations

Each checkout gets a storage key derived from its project path, beneath your chosen root. Xcode distributions have separate subdirectories. Repeated builds reuse these paths; every build, test, or archive action gets a unique `.xcresult` bundle. Settings queries use separate unique result bundles.

| Output | Routing control |
| --- | --- |
| Build/test DerivedData, products, intermediates | `-derivedDataPath`, `SYMROOT`, `OBJROOT` |
| Package clones and repository cache | `-clonedSourcePackagesDirPath`, `-packageCachePath` |
| Clang/Swift module cache | `MODULE_CACHE_DIR`, `CLANG_MODULE_CACHE_PATH` build settings |
| Compilation cache | `COMPILATION_CACHE_CAS_PATH` |
| Precompiled headers | `SHARED_PRECOMPS_DIR` |
| Install staging used by build settings | `DSTROOT` |
| Temporary files for processes that honor it | `TMPDIR` |
| Test results | `-resultBundlePath` |
| Archive | `-archivePath`; Xcode's archive work roots are derived beneath the managed external Derived Data directory |
| Local export | managed `-exportPath` and a generated `-exportOptionsPlist` |

These controls are checked against the selected distribution and primary sources. See the [published 0.1 routing evidence](docs/verification.md) and [0.2 development evidence](docs/verification-0.2.md) for the distinction between configured paths, observed output, and unverified behavior. This is a storage workflow, not a claim that every write happens on the SSD.

## Accepted arguments and failure behavior

The published 0.1.0 release requires one `-project`, one shared `-scheme`, and `build` or `test`. The 0.2 checkpoint and current 0.3 checkout also accept `archive`, or the exact export form `-exportArchive -archivePath ARCHIVE`. A build, test, or archive chooses exactly one `-project` or `-workspace` and one statically inspected shared scheme. A graph with more than one project must also provide explicit `-configuration` and `-sdk` values so every member query uses the same selection.

The build, test, and archive actions accept these value options:

```text
-project  -workspace  -scheme  -configuration  -destination  -sdk  -arch
-jobs  -destination-timeout  -parallel-testing-enabled
-parallel-testing-worker-count  -maximum-parallel-testing-workers
-maximum-concurrent-test-device-destinations
-maximum-concurrent-test-simulator-destinations
-enableCodeCoverage  -testLanguage  -testRegion
```

It also accepts `-quiet`, `-showBuildTimingSummary`, `-disableAutomaticPackageResolution`, `-onlyUsePackageVersionsFromResolvedFile`, `-skipPackageUpdates`, and inline `-only-testing:Identifier` / `-skip-testing:Identifier`. The only caller-supplied build settings are `CODE_SIGNING_ALLOWED`, `CODE_SIGNING_REQUIRED`, `ONLY_ACTIVE_ARCH`, and `ENABLE_TESTABILITY`, each set to `YES` or `NO`.

Unknown actions/options, duplicate flags/settings, caller-supplied output paths, command-line xcconfig files, and inherited output, compiler, or signing overrides are rejected. Test-only options require `test`. `.xctestplan` files, `TestPlanReference` scheme entries, and caller-supplied `-testPlan` are rejected because their contents are not inspected. Private or autogenerated schemes, script phases, custom rules, unsupported object types, unsafe copy destinations, unresolved graph references, and managed settings in project or xcconfig source are also rejected. Effective output settings must resolve beneath the approved storage root for the selected scheme and every statically inspected member target. This conservative subset can block a valid Xcode project.

If the disk is absent, locked, wrong, read-only, below the configured capacity threshold, or inaccessible, the build does not start. BuildHarbor never creates a replacement mount directory or silently chooses internal storage. Existing symlinks in managed output paths are rejected, including links that remain on the same disk. Another BuildHarbor run using the same build directory receives a lock error.

## Limits to understand before using it

- BuildHarbor controls selected output locations. It is **not a filesystem sandbox**. Arbitrary build scripts, package plugins, Xcode services, simulators, and macOS may write elsewhere.
- It does not control builds launched directly from Xcode's graphical interface, or another tool that bypasses BuildHarbor and its lock.
- The current development line checks a defined set of effective output settings. It does not prove that every Xcode setting or project tool is harmless, and it rejects script phases and custom rules instead of trying to analyze their effects.
- The 0.3 report is bounded to 200,000 directory entries, 100,000 distinct inodes, depth 64, and 30 seconds. Its deadline is checked between filesystem operations and cannot interrupt a kernel I/O call that is already blocked.
- Reporting does not modify file content, mode, modification time, or change time under BuildHarbor's control. Opening files and directories for metadata can still let the operating system update access time, so BuildHarbor makes no access-time guarantee.
- Preflight checks and directory-fd operations reduce accidental path mistakes. They cannot guarantee protection against every mount change, malicious local race, force-kill, or mid-build disconnection. A failed or disconnected destination can also prevent receipt creation; that failure is reported.
- There is no automatic cleanup, data migration, global Xcode configuration, simulator relocation, background service, or registry publication. Archive/export support remains limited to the local macOS workflow above.
- The JSON objects carry `schema_version: 1`; their full contract is still experimental until 1.0.
- The current compatibility allowlist remains one exact Xcode build. The controlled 0.2 fixture evidence does not establish real signing/distribution, large-project, iOS, simulator, or broader project compatibility.
- Some SwiftPM global manifest/metadata caches use toolchain-selected locations. The package-cache routing verified here covers repository caching; it does not relocate every SwiftPM cache.

External storage and Xcode path configuration already exist. BuildHarbor's focus is the tested volume guard and a plan/run/receipt workflow you can review. [Comparison with DevCleaner, mac-ssd-rescue, VibeChard, and fastlane](docs/comparison.md).

See the [roadmap and internal validation plan](ROADMAP.md), [contribution guide](CONTRIBUTING.md), [security scope](SECURITY.md), [storage-guard decision](docs/adr/0001-storage-guard.md), [0.2 settings/archive decision](docs/adr/0002-effective-settings-and-local-export.md), and [0.3 reporting decision](docs/adr/0003-read-only-storage-reporting.md).
