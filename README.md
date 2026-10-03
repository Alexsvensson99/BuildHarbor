# BuildHarbor

BuildHarbor checks an external APFS volume before routing selected Xcode build outputs to it. You can inspect a plan, run a command-line build or test, and read a local receipt afterward.

I want an external build drive to be an explicit part of the workflow. A path under `/Volumes` is not enough: the expected disk needs to be mounted, unlocked, writable, and identified by its volume UUID. If those checks fail, BuildHarbor stops before launching the build.

This first version is for independent Apple developers and small teams using command-line builds, including builds started by coding agents. It uses Python 3.11 or later and has no third-party runtime dependencies. The source is MIT licensed.

## Requirements and installation

- macOS with a full Xcode installation selected through `xcode-select` or `DEVELOPER_DIR`.
- **Xcode 27.0, build 27A266a.** Version 0.1.0 accepts only this distribution. Other versions need their own integration evidence before support is added.
- Python **3.11+**, `pip`, and Git.
- A mounted, unlocked **external APFS** volume with at least the free capacity your project policy requires.

Install from the release tag in a virtual environment:

```sh
git clone --branch v0.1.0 --depth 1 https://github.com/Alexsvensson99/BuildHarbor.git
cd BuildHarbor
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/buildharbor --version
```

Use `.venv/bin/buildharbor` in the examples below, or activate the environment with `source .venv/bin/activate`. This release is distributed through GitHub; it is not published to PyPI or Homebrew. Installing the source uses setuptools as a build dependency.

## First run

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

## What each command does

**`doctor`** reads configuration, the mounted volume's UUID and APFS format, external-disk status, available capacity, permission metadata, and selected Xcode. It does not write a probe. “Permission metadata permits access” is not proof that a real write will succeed. `doctor --json` makes the same distinction.

**`plan`** validates and prints an argument array, two environment changes (`DEVELOPER_DIR` and `TMPDIR`), and planned output paths. It creates no directories, resolves no packages, and does not execute `xcodebuild`. Xcode identity comes from the selected installation's version plist. A plan is a preview: `run` validates again and allocates its own unique receipt/result names.

The installed launcher disables Python bytecode writes before importing BuildHarbor. When running directly from source, use `PYTHONPATH=src python3 -B -m buildharbor` to keep interpreter cache files out of the checkout too.

**`run`** validates again, creates directories on the verified volume, takes an exclusive lock for that checkout and Xcode distribution, checks a small real write, revalidates immediately before launch, and executes `xcodebuild`. Output streams directly to your terminal. Interrupt and termination signals are forwarded to its process group. The normal exit code is the build's exit code; signal exits use `128 + signal`. Configuration and wrapper failures use code `2`.

The local receipt includes the BuildHarbor and Xcode versions, action, result, planned paths, observed directory contents, and unverified behavior. An empty directory is not evidence that Xcode used it. Receipts omit command arguments and the inherited environment. Build logs still belong to Xcode and your scripts; treat them as potentially private.

## Routed locations

Each checkout gets a storage key derived from its project path, beneath your chosen root. Xcode distributions have separate subdirectories. Repeated builds reuse these paths; `test` gets a unique `.xcresult` bundle.

| Output | Routing control |
| --- | --- |
| DerivedData, products, intermediates | `-derivedDataPath`, `SYMROOT`, `OBJROOT` |
| Package clones and repository cache | `-clonedSourcePackagesDirPath`, `-packageCachePath` |
| Clang/Swift module cache | `MODULE_CACHE_DIR`, `CLANG_MODULE_CACHE_PATH` build settings |
| Compilation cache | `COMPILATION_CACHE_CAS_PATH` |
| Precompiled headers | `SHARED_PRECOMPS_DIR` |
| Install staging used by build settings | `DSTROOT` |
| Temporary files for processes that honor it | `TMPDIR` |
| Test results | `-resultBundlePath` |

These controls are checked against the selected distribution and primary sources. See [routing evidence](docs/verification.md) for the distinction between configured paths, observed output, and unverified behavior. This is a storage workflow, not a claim that every write happens on the SSD.

## Accepted arguments and failure behavior

Version 0.1.0 requires exactly one action, `build` or `test`, an explicit `-project`, and an explicit `-scheme`. It accepts these value options:

```text
-project  -scheme  -configuration  -destination  -sdk  -arch
-jobs  -destination-timeout  -parallel-testing-enabled
-parallel-testing-worker-count  -maximum-parallel-testing-workers
-maximum-concurrent-test-device-destinations
-maximum-concurrent-test-simulator-destinations
-testPlan  -enableCodeCoverage  -testLanguage  -testRegion
```

It also accepts `-quiet`, `-showBuildTimingSummary`, `-disableAutomaticPackageResolution`, `-onlyUsePackageVersionsFromResolvedFile`, `-skipPackageUpdates`, and inline `-only-testing:Identifier` / `-skip-testing:Identifier`. The only caller-supplied build settings are `CODE_SIGNING_ALLOWED`, `CODE_SIGNING_REQUIRED`, `ONLY_ACTIVE_ARCH`, and `ENABLE_TESTABILITY`, each set to `YES` or `NO`.

Unknown actions/options, duplicate flags/settings, caller-supplied output paths, command-line xcconfig files, and inherited output/compiler overrides are rejected. Test-only options require `test`. Workspaces, nested project references, literal output/compiler overrides, and xcconfig references are also rejected. This conservative subset may block a valid project. It is a deliberate limit of the first release; [0.2.0](ROADMAP.md) includes better effective-setting analysis.

If the disk is absent, locked, wrong, read-only, below the configured capacity threshold, or inaccessible, the build does not start. BuildHarbor never creates a replacement mount directory or silently chooses internal storage. Existing symlinks in managed output paths are rejected, including links that remain on the same disk. Another BuildHarbor run using the same build directory receives a lock error.

## Limits to understand before using it

- BuildHarbor controls selected output locations. It is **not a filesystem sandbox**. Arbitrary build scripts, package plugins, Xcode services, simulators, and macOS may write elsewhere.
- It does not control builds launched directly from Xcode's graphical interface, or another tool that bypasses BuildHarbor and its lock.
- It does not fully evaluate effective settings or prove that your project cannot redirect an output. Workspaces, nested projects, and xcconfig references are unsupported. Read the plan and review project scripts/settings before using it with private or unfamiliar code.
- Preflight checks and directory-fd operations reduce accidental path mistakes. They cannot guarantee protection against every mount change, malicious local race, force-kill, or mid-build disconnection. A failed or disconnected destination can also prevent receipt creation; that failure is reported.
- There is no automatic cleanup, data migration, global Xcode configuration, simulator relocation, background service, archive/export support, or registry publication in this release.
- The JSON objects carry `schema_version: 1`; their full contract is still experimental until 1.0.
- The initial compatibility evidence is a small macOS fixture. It does not establish iOS/simulator, signing, large-project, or external-pilot compatibility.
- Some SwiftPM global manifest/metadata caches use toolchain-selected locations. The package-cache routing verified here covers repository caching; it does not relocate every SwiftPM cache.

External storage and Xcode path configuration already exist. BuildHarbor's focus is the tested volume guard and a plan/run/receipt workflow you can review. [Comparison with DevCleaner, mac-ssd-rescue, VibeChard, and fastlane](docs/comparison.md).

See the [roadmap and three-developer pilot](ROADMAP.md), [contribution guide](CONTRIBUTING.md), [security scope](SECURITY.md), and [architecture decision](docs/adr/0001-storage-guard.md).
