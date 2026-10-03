# ADR 0001: Guard external storage before routing builds

- Status: Accepted
- Date: 2026-10-03
- Applies to: BuildHarbor 0.1.x

## Context

Apple command-line builds can place products, intermediate data, package data, module caches, temporary files, and test results in several locations. Moving only one directory leaves other high-write paths on internal storage. A global Xcode preference or home-directory symlink also changes behavior for every project and can silently point at a missing or different volume.

BuildHarbor needs a per-project, reviewable route to an external SSD. The route must stop before execution when the device identity or path layout is wrong. It must also remain honest about its reach: Xcode project settings and arbitrary build scripts can define their own output paths, and 0.1 does not inspect all effective settings.

## Decision

BuildHarbor separates portable intent from machine identity:

- `buildharbor.toml` is portable and may be committed with the project.
- `.buildharbor.local.toml` is ignored and contains the local `mount`, `volume_uuid`, and `storage_root`.

The 0.1 command surface is limited to `doctor`, `plan`, and `run` for explicit build and test operations. It requires one explicit `-project` pointing to a plain `.xcodeproj` and rejects workspaces and nested project references. Its initial toolchain allowlist accepts exactly Xcode 27.0 (build 27A266a). On Apple Silicon with macOS 27.0 (26A428), the repository fixtures have completed guarded build and test runs and a loopback HTTP Git dependency has exercised the routed checkout and repository cache. [The verification record](../verification.md) gives the exact evidence and its limits. `plan` identifies the Xcode installation from static application metadata; it does not execute `xcodebuild` and it does not create directories, symlinks, locks, or receipts.

`run` uses this order:

1. Parse and validate both configuration layers.
2. Accept only the documented command selectors and small build-setting allowlist. Reject `-xcconfig`, caller-supplied managed output flags, and caller-supplied managed output settings.
3. Verify that the configured mount is mounted, external, APFS, and has the configured volume UUID. A directory at the expected mount path is not sufficient.
4. Resolve the storage root and every managed destination. Refuse path escapes, every existing symlink in a managed path, and files or directories that conflict with the planned layout. Version 0.1 deliberately does not try to decide that a symlink is safe.
5. Acquire one advisory `flock` per project and recheck the mutable guard state before filesystem preparation and child execution.
6. Create only the approved external output directories needed by this run. Do not replace or retarget project symlinks.
7. Execute the argument vector directly, with no shell interpolation. Route source-package checkouts and the source-control repository cache through Xcode's supported package paths, compiler/module caches through the supported `MODULE_CACHE_DIR`, `CLANG_MODULE_CACHE_PATH`, and `COMPILATION_CACHE_CAS_PATH` build settings, and temporary data through `TMPDIR`. Do not rely on unverified Swift environment-variable aliases.
8. Forward supported termination signals, wait for the child, release the lock, and make a best-effort receipt write with the action, success/failure result, and shell-style exit code. If the guard fails before verified storage is prepared, report through stderr and the process exit status without writing a receipt.

There is no fallback to internal storage. A failed guard stops the run.

The first release does not run archive/export operations. It also does not claim that command-line routing covers workspaces, nested projects, GUI Xcode, arbitrary run scripts, custom build settings outside the allowlist, or every cache the Apple toolchain may use. Workspace and nested-project traversal belongs in 0.2 after effective-setting evidence exists for every member.

## Routing evidence

The installed `xcodebuild -help` for Xcode 27.0 (27A266a), checked on 2026-10-03, documents `-derivedDataPath`, `-resultBundlePath`, `-clonedSourcePackagesDirPath`, and `-packageCachePath` for current build/test routing. The same help documents `-archivePath`, `-exportPath`, `-localizationPath`, and XCFramework `-output`; those operations remain outside 0.1.

Apple's [Build settings reference](https://developer.apple.com/documentation/xcode/build-settings-reference) documents `OBJROOT` for intermediate files, `SYMROOT` for build products, and `DSTROOT` for install products. Apple also states that command-line build settings have highest precedence in [Configuring the build settings of a target](https://developer.apple.com/documentation/xcode/configuring-the-build-settings-of-a-target/). Swift's installed `build` and `test` help documents `--scratch-path` and `--cache-path`.

The primary [swift-build 6.4.0 settings source](https://github.com/swiftlang/swift-build/blob/swift-6.4.0-RELEASE/Sources/SWBCore/Settings/Settings.swift#L2115-L2122) uses `COMPILATION_CACHE_CAS_PATH` when supplied and otherwise places the compilation cache under Derived Data. Its [Swift specification](https://github.com/swiftlang/swift-build/blob/swift-6.4.0-RELEASE/Sources/SWBUniversalPlatform/Specs/Swift.xcspec#L1079-L1083) maps `CLANG_MODULE_CACHE_PATH` to Swift's `-module-cache-path`, while the [Clang specification](https://github.com/swiftlang/swift-build/blob/swift-6.4.0-RELEASE/Sources/SWBUniversalPlatform/Specs/Clang.xcspec#L425-L434) derives it from `MODULE_CACHE_DIR` and emits `-fmodules-cache-path`. swift-build's [temporary-directory implementation](https://github.com/swiftlang/swift-build/blob/swift-6.4.0-RELEASE/Sources/SWBUtil/Path.swift#L181-L184) explicitly respects `TMPDIR`, but that remains best-effort routing rather than proof that every subprocess uses it.

SwiftPM 6.4 [normalizes a `file://` dependency to a local path](https://github.com/swiftlang/swift-package-manager/blob/swift-6.4.0-RELEASE/Sources/PackageModel/DependencyMapper.swift#L84-L133), and its [repository manager skips local repositories in the shared cache by default](https://github.com/swiftlang/swift-package-manager/blob/swift-6.4.0-RELEASE/Sources/SourceControl/RepositoryManager.swift#L309-L313). The apparent cache miss in the first local fixture was therefore not evidence that `-packageCachePath` had failed. `SWIFTPM_TESTS_PACKAGECACHE` changes that branch, but the source [labels local-package caching as test-only](https://github.com/swiftlang/swift-package-manager/blob/swift-6.4.0-RELEASE/Sources/SourceControl/RepositoryManager.swift#L24-L64); BuildHarbor does not use it. A loopback HTTP Git fixture then populated `PackageCache/repositories` as recorded in [the live verification](../verification.md).

SwiftPM also models [manifest and repository caches as separate locations](https://github.com/swiftlang/swift-package-manager/blob/swift-6.4.0-RELEASE/Sources/Workspace/Workspace+Configuration.swift#L135-L145). Version 0.1 supports routing the source-control repository cache demonstrated by the fixture. It does not claim that Xcode's manifest compilation cache or other global package metadata follows `-packageCachePath`.

These sources support individual routing controls; they do not prove that every write made by a project or toolchain is redirected.

## Alternatives considered

### Change Xcode's global Derived Data preference

This is easy for one path but changes GUI behavior for every project, does not carry a volume UUID guard, and does not cover all managed outputs.

### Replace global developer directories with symlinks

This can move more existing data, but it is a migration with deletion and recovery concerns. It also couples unrelated projects to one mounted path. That is a different product boundary from guarded per-project execution.

### Clean caches when internal storage is low

Cleanup reacts after storage has already been consumed and destroys reusable data. BuildHarbor 0.1 routes new work and performs no deletion.

### Wrap the build in a filesystem sandbox

A useful sandbox would need a much wider compatibility and security design for Xcode, simulators, signing, package resolution, and project scripts. The 0.1 guard is intentionally narrower.

### Monitor the volume in a background service

A daemon adds lifecycle, privilege, update, and recovery responsibilities. The 0.1 checks stay in the foreground command that owns the build.

## Consequences

The design is predictable and reviewable: a committed project file describes intent while local device identity stays local, and an absent or changed volume blocks execution. It also means a developer must invoke builds and tests through BuildHarbor to receive the guard.

The guard reduces mistaken-destination risk but does not remove the race between validation and later writes. A drive can disconnect during a build, and a project script can write outside managed paths. Effective-setting conflict detection is therefore planned for 0.2, and compatibility remains restricted to exact toolchain builds with evidence.
