# Changelog

## 0.1.0 — 2026-10-03

Initial release with `doctor`, read-only `plan`, and guarded `run` for explicit Xcode `build` and `test` actions.

- Separate portable policy and ignored machine-local volume configuration.
- External APFS identity, capacity, permission metadata, and output-path checks.
- Managed DerivedData, package storage, compiler caches, temporary files, and unique test results.
- Per-checkout build lock, signal forwarding, child exit codes, and local run receipts.
- Standard-library runtime, Python 3.11+, MIT license, and a repository-owned macOS fixture.

Compatibility is limited to the Xcode distribution in [the verification record](docs/verification.md). The first version has a conservative argument subset and does not evaluate every effective setting. It does not relocate existing data or control GUI builds.
