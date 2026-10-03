# Version 0.1.0 verification

The first release was exercised on **Apple Silicon, macOS 27.0 (26A428), Xcode 27.0 (27A266a), Python 3.13.4**, with a mounted external APFS volume. This record describes those checks. It does not imply support for adjacent Xcode/macOS versions, Intel Macs, iOS simulators, signing workflows, or large production projects.

## Local checks on 2026-10-03

| Check | Result and evidence boundary |
| --- | --- |
| Python suite | 40 tests passed. Volume/system responses are simulated; child-process, lock, and signal tests use real small Python subprocesses. |
| Repository fixture | Real framework build succeeded; hostless XCTest executed 1 test with 0 failures. The fixture compiles Swift, C, and a repository-owned Swift package. |
| Package routing | A task-owned Git package served over loopback HTTP produced a checkout in `SourcePackages/checkouts` and a repository cache in `PackageCache/repositories`. No external package host was required. |
| Repeat build | The repeat build reused both C/Swift object files and all 138 existing compiled `.pcm` modules without changing their modification times. This proves reuse for this fixture, not a general performance improvement. |
| Remote-package test | The same fixture copy with the Git package passed 1 XCTest with 0 failures and wrote a unique `.xcresult` bundle beneath the planned results directory. |
| Compiler data | Module files, explicit precompiled modules, and compilation-cache database files were observed under the external project storage. Database presence does not prove a compilation-cache hit. |
| Temporary files | SwiftPM lock files and a temporary directory were observed beneath the supplied `TMPDIR`. This is evidence of use by part of the toolchain, not every process. |
| Installation | `pip install --no-compile` into a separate virtual environment produced a working CLI. The installed launcher created no package bytecode when running version, doctor, and plan. Source file names, sizes, and modification times were unchanged by doctor/plan. |
| Publication review | Publishable files and reachable Git history were checked for machine configuration, private path/UUID values, credential patterns, and generated outputs, followed by manual review. |

The real integration ran sequentially with a bounded Xcode job count. The test fixture used paths containing spaces. It did not need a simulator or GUI application. Local diagnostics after the checks contained no new matching fixture or XCTest reports in the review window; this is a bounded observation.

The tests simulate missing volumes, wrong UUIDs, internal/non-APFS volumes, locked/read-only disks, permission errors, symlink escapes, destination replacement/loss, duplicate flags, unsupported actions, and malformed input. They also cover concurrent runs, SIGINT/SIGTERM/SIGHUP delivery and child reaping, child exit status, receipt failures, and rejected commands never starting a build. No test disconnected the real drive.

## What remains unverified

- Full filesystem write coverage, effective settings, arbitrary scripts/plugins, GUI Xcode, workspaces, nested projects, xcconfig files, and other Xcode distributions.
- Network authentication, package registries, binary artifact caches, and projects with signing requirements.
- SwiftPM's global manifest/metadata caches: the package-cache flag is verified here for the repository cache. Some global SwiftPM metadata still uses toolchain-selected locations outside BuildHarbor's project tree.
- Precompiled-header and install-staging writes: the paths are configured, but this fixture does not produce a PCH or perform an install action. An empty directory is recorded as empty.
- Complete protection from mid-build disconnection, hostile filesystem races, force-kills, or descendants that detach from the process group.
- External pilot results. The three-developer pilot in the roadmap is planned; nobody was contacted or enrolled for this release.

Receipts report observed presence after a run. Existing cached contents may predate that run. They are not a filesystem trace or proof that no writes occurred elsewhere.

## Repeat the checks

For simulated tests, no external disk or Xcode is needed:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -v
python3 scripts/check_publication.py
```

For real integration, first configure the ignored local volume file as described in the README. Review the script, verify capacity and thermal conditions, and run:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 scripts/verify_xcode.py --jobs 2
```

The opt-in script validates the external volume before copying the fixture. It creates a private evidence directory on that volume, a local Git remote, and a transient server bound only to `127.0.0.1`. It builds, repeats the build, tests, and inspects the resulting files. It stops the server and retains logs, receipts, fixture sources, and a private summary. It does not delete the evidence or modify global Xcode settings. Inspect those files locally; do not publish them without redacting paths and machine metadata.

## CI boundary

The `CI` workflow installs the package and runs simulated-system and small process tests on Linux/Python 3.11 and macOS/Python 3.13. It does **not** create an external APFS volume or run the real Xcode integration. Its status belongs to the exact commit/tag shown in [GitHub Actions](https://github.com/Alexsvensson99/BuildHarbor/actions). Local Xcode evidence and CI success are separate checks.

## Primary routing references

- The installed Xcode 27.0 `xcodebuild -help` documents `-derivedDataPath`, `-clonedSourcePackagesDirPath`, `-packageCachePath`, and `-resultBundlePath`. The installed Clang/Swift `.xcspec` files were also inspected.
- Apple's [build settings reference](https://developer.apple.com/documentation/xcode/build-settings-reference) documents build-product, intermediate, and module-cache settings.
- [swift-build 6.4.0 settings](https://github.com/swiftlang/swift-build/blob/swift-6.4.0-RELEASE/Sources/SWBCore/Settings/Settings.swift) and [CAS options](https://github.com/swiftlang/swift-build/blob/swift-6.4.0-RELEASE/Sources/SWBCore/Settings/CASOptions.swift) describe compilation-cache paths.
- The [Swift compiler specification](https://github.com/swiftlang/swift-build/blob/swift-6.4.0-RELEASE/Sources/SWBUniversalPlatform/Specs/Swift.xcspec) and [Clang specification](https://github.com/swiftlang/swift-build/blob/swift-6.4.0-RELEASE/Sources/SWBUniversalPlatform/Specs/Clang.xcspec) map the module-cache build settings to compiler flags.
- Swift's [driver documentation](https://github.com/swiftlang/swift/blob/main/docs/Driver.md) explains `TMPDIR` for temporary intermediates. It does not promise that every Xcode component honors that variable.

Source documentation explains the intended controls; the local checks above establish the narrower observed behavior.
