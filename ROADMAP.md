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

## Release gate after 0.1

Development remains maintainer-led and uses controlled fixtures. Local implementation may continue through 0.2 and 0.3, but that progress does not authorize or prove a new public release. Before another release is published, one candidate revision needs a dated internal validation record that meets every gate below:

- the full automated suite passes on every supported CI environment;
- the real Xcode fixture completes guarded build and test runs on each exact toolchain build claimed as supported;
- the package fixture fetches a repository over loopback HTTP and places both its checkout and repository cache beneath the planned storage root;
- a repeated build preserves the selected object and compiled-module modification times used to check cache reuse;
- fail-closed fixtures cover missing, replaced, internal, non-APFS, read-only, and UUID-mismatched volumes, plus symlink and destination conflicts, and confirm that rejected commands never start the child process;
- concurrency, signal-forwarding, child-reaping, exit-status, and receipt-failure checks pass;
- a clean installation completes `--version`, `doctor`, and `plan` without changing source files or creating package bytecode; and
- publication checks and manual review find no machine configuration, private paths, real volume UUIDs, credentials, or generated build output in publishable history.

Any unresolved unsafe allow, managed output unexpectedly written to internal storage, child process started after a rejected guard, or failure to reproduce the supported Xcode integration blocks a new release. Other failures must be recorded with their scope and either resolved or explicitly removed from the supported claim. Local development can continue while the gate is open.

## 0.2 — archive/export and effective-setting conflicts

Status: the implementation and one controlled local build/test/workspace/archive/export sequence have passed on Xcode 27.0 (27A266a). A separate remote-package regression completed build, repeat build, and test with routed repository cache and selected compiler-artifact reuse. The 94-test unit/process suite passed under Python 3.11.13 and 3.13.4. [The 0.2 verification record](docs/verification-0.2.md) gives the exact boundary. New-release CI and review remain open, and version 0.2 has not been published.

### Scope

- Keep `plan` strictly read-only. It performs bounded static traversal of a project or workspace, referenced projects, shared schemes, configurations, xcconfig source, phases, rules, package references, and managed settings without executing `xcodebuild`.
- During `run`, inspect the selected scheme/action settings and every statically discovered project target before the requested action starts. The selected query uses the action's managed routes. Member `-alltargets` queries omit `-derivedDataPath` and instead use explicit output roots, package/cache routes, and external temporary storage.
- Fail closed on private or autogenerated schemes, scripts, custom build rules, unsupported graph objects, unsafe copy phases, unresolved members, unknown targets, conflicting duplicates, unresolved output values, or managed paths outside the approved storage root.
- Add a narrow archive operation for exactly one simple macOS application with signing disabled or manual ad-hoc signing using identity `-`. Reject teams, profiles, keychains, extra signing flags, private identities, and other platforms or archive shapes.
- Derive archive packaging roots from the managed external Derived Data action layout and check the resolved effective values. Do not claim that generic root-setting injection works for archives.
- Add a narrow local Copy App export that accepts only a successful, unchanged BuildHarbor-managed archive, generates fixed `mac-application` / `export` options, and allocates unique outputs. Reject archive symlinks, hard-linked files, special files, cross-filesystem entries, and ambiguous app contents.
- Keep the 0.1 guard, lock, signal, and no-fallback behavior.
- Keep configuration and receipt `schema_version` 1. New receipt fields are additive and preserve existing field meanings.

### Dependencies

- The release gate above must pass before a new public release. It does not block local 0.2 or 0.3 implementation.
- Stable 0.1 configuration and receipt schemas.
- Primary-source and executable evidence for every newly managed archive/export flag and effective setting.
- Controlled fixtures covering workspace membership, nested project references, all static member targets, signing failures, archive contents, export binding, and conflicts in non-root projects.

### Acceptance criteria

- `plan` changes no filesystem state and never executes `xcodebuild`, including for workspaces, archives, and exports.
- Archive and export plans identify every managed destination before execution. Every action uses a unique result bundle; every archive/export leaf is unique and cannot overwrite an existing output.
- A conflicting effective setting blocks the action before its main `xcodebuild` process starts. Diagnostics name the setting and safe value, while stating that Xcode does not expose its definition source when that source is unavailable.
- A workspace or nested-project plan accounts for every referenced project, and `run` checks exactly every static member target. Unresolved, inaccessible, missing, unknown, or conflicting members block execution.
- Archive success requires an inspected single macOS application and a receipt-bound digest. Export requires that successful receipt and unchanged archive; neither operation accepts signing credentials.
- Successful archives and exports remain beneath the approved storage root and are represented accurately in schema-1 receipts.
- Existing 0.1 configuration files retain their meaning. Additive schema-1 receipt fields do not invalidate existing readers that ignore unknown fields.
- The controlled local milestone is recorded with its exact environment and limits. The complete new-release CI and review gates pass before any 0.2 release claim.

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
- Support only the Xcode versions and project shapes demonstrated by maintained internal compatibility fixtures.

### Dependencies

- Dated internal verification records for the stable build/test, archive/export, settings-analysis, and reporting contracts included in 1.0.
- Documented compatibility fixtures for every supported Xcode build and project shape.
- At least one release cycle proving that older supported configuration and receipt schemas remain readable.

### Acceptance criteria

- Supported 0.x configuration files retain their meaning or fail with a precise migration instruction.
- A clean installation, first `doctor`, and first `plan` follow one documented path and require no source checkout edits.
- Compatibility claims name exact tested Xcode builds and macOS versions.
- Controlled fixtures cover build, test, archive/export, disconnection, conflict, recovery, and schema compatibility without an unsafe allow.
- Security, contribution, and comparison documents match the shipped behavior.

Version 1.0 does not imply a broader-adoption program. If broader adoption is ever considered, it requires a separate readiness decision based on the product state at that time.
