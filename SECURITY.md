# Security policy

BuildHarbor is a local command runner with a narrow storage guard. It is designed to stop an Apple build or test before it writes managed output when the configured external volume cannot be verified. It is not a filesystem sandbox, an access-control boundary, or protection from a malicious project.

## Supported versions

Security fixes are provided for the latest 0.1.x release while 0.1 is current. The policy will be updated when another release line becomes supported.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/Alexsvensson99/BuildHarbor/security/advisories/new) for this repository. Include the affected version, a minimal reproduction, expected and observed behavior, and the security impact. Redact usernames, real volume UUIDs, signing material, access tokens, and private project paths.

If private reporting is unavailable, open a public issue that asks for a private reporting channel without including vulnerability details, logs, or reproduction steps. Please do not test a report against data, machines, or accounts you do not own or have permission to use.

## Threat model

BuildHarbor assumes one trusted local user controls the project configuration, local configuration, source checkout, external volume, and command being run. Project build phases and tools execute with that user's normal authority. Configuration from an untrusted checkout is therefore code-adjacent input and must be reviewed before `run`.

The 0.1 security boundary covers:

- confusing an internal directory, another mounted volume, or a replaced mount with the approved external volume;
- a mismatched volume UUID, non-APFS filesystem, or non-external device;
- output paths that escape the approved storage root;
- existing symlinks or filesystem objects that conflict with the plan;
- two BuildHarbor runs for the same project racing each other;
- a caller supplying managed output flags, managed build settings, or `-xcconfig` to override routing;
- losing the outcome when a build fails or receives a signal.

`run` must revalidate the mounted APFS external volume and UUID before creating output directories and again at the execution boundary defined by the implementation. It must fail closed, hold an advisory lock for the project, and forward termination signals. After storage preparation and child execution it makes a best-effort receipt write. A guard failure before verified storage exists is reported on stderr because writing a receipt would violate the guard. `doctor` and `plan` are read-only; `doctor` does not inspect project input, and `plan` reads static Xcode application metadata without executing `xcodebuild`.

## Known residual risks

There is an unavoidable time-of-check/time-of-use window between volume validation and later filesystem writes by Xcode and its children. The volume can be disconnected, remounted, or fail during a build. BuildHarbor can detect the condition at its own checkpoints and report an incomplete or failed run, but it cannot make a mid-build disconnection atomic or guarantee the state of partially written build products.

Signals can also race with process creation and with grandchildren started by project scripts. BuildHarbor forwards supported signals and records what it observes, but a project process that daemonizes or ignores signals may outlive the direct child.

The storage guard controls the paths BuildHarbor manages. A project build phase, tool plugin, compiler, dependency, or explicitly accepted argument can still read or write elsewhere with the user's permissions. Receipts and logs may contain local paths, command arguments, bundle identifiers, and project metadata. Store and share them accordingly.

Advisory file locks coordinate cooperating BuildHarbor processes only. Other tools, GUI Xcode, and direct `xcodebuild` invocations do not honor those locks.

## Out of scope for 0.1

BuildHarbor does not claim to secure a hostile multi-user machine, isolate untrusted build code, police every file opened by Xcode, manage signing credentials, migrate or delete existing data, change global Xcode settings, or monitor builds as a background service. Version 0.1 also rejects workspaces and nested project references rather than claiming it has inspected every member's settings.
