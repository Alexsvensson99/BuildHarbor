# Changelog

## 0.3.0 — 2026-10-05

Version 0.3.0 is the first release line after 0.1.0. It includes the archive/export and effective-setting work developed during the unpublished 0.2 checkpoint, plus read-only storage reporting. See the [release notes](docs/release-0.3.0.md) and [verification record](docs/verification-0.3.md) for the exact compatibility and evidence boundary.

### Added

- Bounded static traversal for projects, workspaces, nested project references, shared schemes, configurations, xcconfig source, package references, targets, and build phases.
- Guarded effective-setting inspection during `run`: one selected-scheme/action query plus `-alltargets` queries for every static member in a multi-project graph.
- Local `archive` for one simple macOS application using disabled signing or manual ad-hoc identity `-`, without teams, profiles, keychains, extra signing flags, or private identities.
- Receipt-bound local Copy App export with fixed generated options and unique output paths.
- `report [--project-dir DIR ...] [--json]` for bounded, read-only accounting of one exact configured storage root without requiring Xcode.
- Inode-based project, shared, and unattributed report buckets with logical and allocated byte counts, ignored-entry counters, stable incomplete-scan issues, and `tool_version` in schema-1 output.

### Safety and compatibility

- Fail closed on scripts, custom rules, unknown graph objects, unsafe copy destinations, unresolved members, missing/conflicting effective settings, and output paths outside managed storage.
- Reject shared schemes that select test plans through `TestPlans` or `TestPlanReference`, and caller-supplied `-testPlan`, because their separate input graph is not inspected.
- Bind export to an unchanged successful BuildHarbor archive, and reject archive symlinks, multiply linked files, special files, cross-filesystem entries, ambiguous applications, changed content, or exceeded inspection bounds.
- Revalidate the generated export-options file by exact inode, link count, length, metadata, and bytes immediately before export.
- Keep archive packaging roots in Xcode's action-specific layout beneath managed external Derived Data instead of forcing ordinary build roots onto the archive action.
- Keep storage reporting read-only and conservative: unknown paths, ambiguous project IDs, unseen hard links, and conflicting metadata remain unattributed; incomplete scans return observed lower bounds and exit 2.

### Maintenance

- Route a unique result bundle for every build, test, and archive action so Xcode failure results cannot fall back to an unmanaged location.
- Give each loopback Git fixture a unique repository identity so SwiftPM does not reuse a same-version cache entry from an earlier run.
- Preserve configuration and receipt `schema_version` 1 while adding source, settings, input, and archive receipt fields; include `tool_version` in schema-1 storage reports.

## 0.1.0 — 2026-10-03

Initial release with `doctor`, read-only `plan`, and guarded `run` for explicit Xcode `build` and `test` actions.

- Separate portable policy and ignored machine-local volume configuration.
- External APFS identity, capacity, permission metadata, and output-path checks.
- Managed DerivedData, package storage, compiler caches, temporary files, and unique test results.
- Per-checkout build lock, signal forwarding, child exit codes, and local run receipts.
- Standard-library runtime, Python 3.11+, MIT license, and a repository-owned macOS fixture.

Compatibility is limited to the Xcode distribution in [the verification record](docs/verification.md). The first version has a conservative argument subset and does not evaluate every effective setting. It does not relocate existing data or control GUI builds.
