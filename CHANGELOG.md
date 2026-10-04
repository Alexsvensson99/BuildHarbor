# Changelog

## Unreleased — 0.2 development

The implementation has completed one controlled local build/test/workspace/archive/export sequence on the exact Xcode allowlist, and 94 automated tests have passed under Python 3.11.13 and 3.13.4. [The development verification record](docs/verification-0.2.md) gives the evidence boundary. New-release CI and review remain open. This section describes the development tree, not a published release or a broader compatibility claim.

### Added

- Bounded static traversal for projects, workspaces, nested project references, shared schemes, configurations, xcconfig source, package references, targets, and build phases.
- Guarded effective-setting inspection during `run`: one selected-scheme/action query plus `-alltargets` queries for every statically discovered member project.
- Fail-closed checks for scripts, custom rules, unknown graph objects, unsafe copy destinations, missing member settings, unresolved output paths, and output paths outside managed storage.
- A narrow local `archive` action for one simple macOS application using disabled signing or manual ad-hoc identity `-` without a team, profile, keychain, extra signing flags, or private identity.
- A narrow `-exportArchive -archivePath ARCHIVE` operation bound to a successful, unchanged BuildHarbor archive. BuildHarbor generates fixed local Copy App export options and unique output paths.
- Archive input checks that reject symlinks, multiply linked regular files, special files, cross-filesystem entries, and ambiguous application contents.
- Additive receipt metadata for project/source identity, settings validation, export inputs, and successful archive identity while keeping receipt `schema_version` 1.

### Changed

- Archive packaging roots now follow Xcode's action-specific layout beneath the managed external Derived Data directory. BuildHarbor checks those resolved roots before execution instead of forcing generic build-root settings onto the archive action.
- Workspace and nested-project actions require a shared scheme and complete static traversal. Multi-project graphs also require explicit configuration and SDK selection for member settings queries.

### Maintenance

- Route a unique result bundle for every build, test, and archive action so an Xcode failure result cannot fall back to an unmanaged location.
- Give each loopback Git integration fixture a unique repository identity so SwiftPM does not reuse a same-version cache entry from an earlier run.

## 0.1.0 — 2026-10-03

Initial release with `doctor`, read-only `plan`, and guarded `run` for explicit Xcode `build` and `test` actions.

- Separate portable policy and ignored machine-local volume configuration.
- External APFS identity, capacity, permission metadata, and output-path checks.
- Managed DerivedData, package storage, compiler caches, temporary files, and unique test results.
- Per-checkout build lock, signal forwarding, child exit codes, and local run receipts.
- Standard-library runtime, Python 3.11+, MIT license, and a repository-owned macOS fixture.

Compatibility is limited to the Xcode distribution in [the verification record](docs/verification.md). The first version has a conservative argument subset and does not evaluate every effective setting. It does not relocate existing data or control GUI builds.
