# BuildHarbor roadmap

BuildHarbor starts deliberately narrow: it routes explicit command-line Apple builds and tests to one verified external APFS volume. It does not change Xcode's global settings, migrate existing data, clean caches, run in the background, or make the build process a filesystem sandbox.

The runtime baseline is Python 3.11 or newer using only the standard library. The initial toolchain allowlist accepts exactly Xcode 27.0 (build 27A266a). On Apple Silicon with macOS 27.0 (26A428), the repository fixtures have completed guarded build and test runs and an HTTP Git dependency has populated the routed checkout and repository cache. A repeated build reused the checked compiler artifacts. [The verification record](docs/verification.md) states the exact environment and limits. Automated tests still simulate volume inspection and build execution. Broader support requires its own evidence, and a nearby version number is not enough.

## 0.1.0 — guarded build and test routing

### Scope

- `doctor` validates configuration, volume identity/capacity, path metadata, and the selected toolchain without changing them. It takes no build arguments and does not inspect a project or workspace.
- `plan` resolves the portable `buildharbor.toml` together with the ignored `.buildharbor.local.toml`, then shows the guard decisions, paths, and explicit build or test invocation. It remains read-only and identifies Xcode from its static application metadata rather than running `xcodebuild`.
- `run` executes only the planned build or test after rechecking that the configured mount is the expected external APFS volume with the configured UUID.
- Version 0.1 accepts one explicit `-project` and rejects workspaces and projects containing nested project references because it cannot yet traverse and evaluate their settings safely.
- Portable project intent stays in `buildharbor.toml`. Machine-specific `mount`, `volume_uuid`, and `storage_root` values stay in `.buildharbor.local.toml` and out of version control.
- Build products, Derived Data, source-package checkouts and repository cache, compiler/module caches, temporary data, and test results use project-specific locations beneath the approved storage root. Manifest compilation caches and other global Xcode or SwiftPM metadata remain outside the supported routing claim.
- Existing symlinks in any managed path are rejected, including symlinks that currently resolve beneath the storage root. Missing directories may be planned, while conflicting files and unsafe path components block the operation. `plan` does not repair them; `run` creates only the directories needed for the accepted plan.
- One advisory file lock is held per project during `run`. Signals are forwarded to the child process. After storage preparation and child execution, BuildHarbor makes a best-effort attempt to write a local receipt with the action and exit status. A guard failure before verified storage exists is reported on stderr and cannot safely produce a receipt.

### Dependencies

- macOS with the expected external storage mounted and unlocked.
- An external APFS volume whose mounted identity and UUID match local configuration.
- Python 3.11 or newer.
- Xcode 27.0 (27A266a) and one plain `.xcodeproj` with no nested project references.
- A project whose command uses the documented, narrow selector and build-setting surface. BuildHarbor rejects managed output flags, managed output settings, and `-xcconfig` instead of allowing them to override routing.

### Acceptance criteria

- `doctor` and `plan` leave the filesystem unchanged in success and failure cases.
- A missing, internal, non-APFS, UUID-mismatched, or replaced mount blocks `run` before it creates output directories or starts a build.
- A managed-path symlink or conflicting filesystem object blocks `run` with a bounded diagnostic. Unknown operating-system errors and arbitrary unsupported input are not reflected verbatim because they may contain secrets.
- Two `run` processes for the same project cannot build concurrently; unrelated project locks remain independent.
- Interrupt and termination signals reach the child process group, the lock is released, and the shell-style exit status remains observable. A receipt records success or failure and the exit code when the verified destination remains available; early guard failures and receipt-write failures remain stderr/exit-status outcomes.
- Build and test fixtures cover spaces in paths and use fake volume identities. No personal mount path or real volume UUID is committed.
- Documentation describes the limits above and does not imply GUI Xcode coverage, cleanup, migration, archive/export support, or filesystem isolation.

## Three-developer pilot

The 0.1 pilot is capped at exactly three external developers. It begins only after the 0.1 acceptance criteria pass; this roadmap does not authorize contacting or enrolling anyone.

For each developer, record:

- setup minutes, measured from starting the documented install to the first successful `doctor` and `plan`;
- minutes to the first successful guarded `run`;
- every project-specific exception or configuration change needed;
- every false block, with the command, reason, and the later evidence showing it was safe;
- every unsafe condition that was incorrectly allowed;
- repeat usage, counted as successful guarded runs on separate days.

Expand beyond the pilot only if all three developers complete a build or test, all three return for at least two later days, median setup is at most 15 minutes, no setup exceeds 30 minutes, there are no unsafe allows, and there are no unresolved false blocks. Hold the rollout if any condition is missed. The resulting evidence decides whether 0.2 work proceeds or 0.1 needs another corrective release.

## 0.2 — archive/export and effective-setting conflicts

### Scope

- Add explicit archive and export operations with their own external paths and receipts.
- Inspect effective Xcode settings before execution and block project, target, configuration, or script-controlled output paths that escape the approved storage root.
- Add workspace and nested-project traversal only when every member's relevant settings can be inspected before execution.
- Keep the 0.1 guard, lock, signal, and no-fallback behavior.

### Dependencies

- A completed three-developer pilot or an explicit hold decision with the blocking findings resolved.
- Stable 0.1 configuration and receipt schemas.
- Primary-source and executable evidence for every newly managed archive/export flag and effective setting.
- Fixtures covering workspace membership, nested project references, and conflicts in non-root projects.

### Acceptance criteria

- Archive and export plans identify every managed destination before execution.
- A conflicting effective setting blocks execution and names its setting, value, and source when Xcode exposes that source.
- A workspace or nested-project plan accounts for every referenced project before execution; unresolved or inaccessible members block the plan.
- Successful archives and exports remain beneath the approved storage root and are represented accurately in receipts.
- Existing 0.1 project files continue to validate without semantic changes.

## 0.3 — read-only storage reporting

### Scope

- Report storage attributed to a configured project, storage shared by several configured projects, and storage that BuildHarbor cannot attribute.
- Keep reporting read-only. Do not delete, prune, migrate, compress, or offer an automatic cleanup action.
- Make uncertainty visible instead of assigning ambiguous data to a project.

### Dependencies

- Stable project identifiers and output layout from 0.1 and archive/export layout from 0.2.
- A documented classification rule for project, shared, and unattributed bytes.

### Acceptance criteria

- Fixture totals reconcile to the measured total without double counting.
- Symlinks and hard links cannot make the report count the same bytes twice.
- Permission failures, disconnected volumes, and incomplete scans produce an explicit incomplete result rather than zero.
- Running the report changes no file timestamps or project state under BuildHarbor's control.

## 1.0 — stable contracts

### Scope

- Stabilize the portable configuration, local configuration, plan, receipt, and exit-status contracts.
- Publish a compatibility policy and a migration path for any later schema change.
- Provide an easy, reproducible installation path without adding runtime dependencies.
- Support the Xcode versions and project shapes demonstrated by external pilot evidence.

### Dependencies

- Successful 0.1 pilot evidence and follow-up external pilots for archive/export and reporting.
- Documented compatibility fixtures for every supported Xcode build.
- At least one release cycle proving that older supported configuration and receipt schemas remain readable.

### Acceptance criteria

- Supported 0.x configuration files retain their meaning or fail with a precise migration instruction.
- A clean installation, first `doctor`, and first `plan` follow one documented path and require no source checkout edits.
- Compatibility claims name exact tested Xcode builds and macOS versions.
- External pilots cover build, test, archive/export, disconnection, conflict, and recovery cases without an unsafe allow.
- Security, contribution, and comparison documents match the shipped behavior.
