# Contributing to BuildHarbor

Thank you for helping make BuildHarbor safer and easier to understand. The useful contributions here are usually small and specific: one guard case, one clearer diagnostic, one verified toolchain fixture, or one documentation correction.

BuildHarbor 0.1.0 has a deliberately limited contract. It supports `doctor`, read-only `plan`, and guarded `run` for explicit command-line builds and tests. Archive/export, cleanup, migration, global Xcode settings, GUI Xcode behavior, background services, and general filesystem sandboxing are outside the current release.

## Before changing code

Read the README for the canonical setup and verification commands. Check the roadmap and open issues before expanding scope. For a security problem, follow [SECURITY.md](SECURITY.md) instead of opening a public issue with exploit details.

The runtime baseline is Python 3.11 or newer and the runtime uses only the Python standard library. Please discuss a new runtime dependency before building work around it. Version 0.1 currently accepts exactly Xcode 27.0 (build 27A266a), one plain `.xcodeproj`, and no nested project references. On Apple Silicon with macOS 27.0 (26A428), the repository fixtures have completed guarded build and test runs, including a loopback HTTP Git dependency that exercised the routed checkout and repository cache. Read [the verification record](docs/verification.md) before making a compatibility claim. It does not establish support for another Xcode build, project shape, host architecture, or global manifest and metadata cache routing.

## Configuration boundaries

Keep portable project intent in `buildharbor.toml`. Keep the machine-specific mount, volume UUID, and storage root in `.buildharbor.local.toml`, which must remain ignored by Git.

Examples and fixtures must use obviously fake paths and UUIDs. Do not commit personal usernames, real external-volume identifiers, signing material, build logs containing secrets, or private project names. A report or receipt can expose paths and command arguments, so redact it before attaching it to an issue.

## Change expectations

- Preserve the read-only contract of `doctor` and `plan`.
- Preserve the fail-closed volume guard. There is no silent fallback to internal storage.
- Treat managed output flags, managed build settings, and `-xcconfig` as conflicts rather than allowing a caller to bypass routing.
- Keep command execution argument-based. Do not add shell interpretation for convenience.
- Preserve per-project locking and signal forwarding. Keep receipts truthful and best-effort after verified storage preparation; early guard failures must remain stderr and exit-status outcomes rather than writing to an unverified destination.
- Keep diagnostics useful without reflecting arbitrary unsupported input or unknown operating-system error text. Those values can contain credentials or private paths. Name a path or setting only when BuildHarbor has validated that it is safe to report.
- Do not add migration or cleanup as an incidental helper to another change.
- Keep workspaces and nested project references blocked in 0.1. Supporting them requires settings traversal and evidence across every referenced project.

Tests should exercise behavior through temporary directories and controlled command/disk-inspection fixtures. They must not require a contributor's real external SSD, modify global Xcode preferences, invoke GUI Xcode, or depend on private sample projects. The automated suite simulates volume inspection and build processes; passing it does not prove that a live Xcode build works on external storage.

Run both repository checks from the project root:

```sh
PYTHONPATH=src python3 -m unittest discover -v
python3 scripts/check_publication.py
```

The publication check looks for local configuration, private paths, credential patterns, generated outputs, and other files that should not be published. Its success still requires a manual review before publication.

## Pull requests

Keep a pull request focused and explain the concrete trigger and resulting behavior. Include:

- the problem and the boundary it affects;
- the before/after behavior;
- the exact checks run and their results;
- any validation that remains unavailable;
- documentation changes when a public contract or limitation changes.

Before requesting review, confirm that unrelated files are untouched, local configuration is not tracked, fixtures contain no private data, and the change does not make a broader compatibility or security claim than the evidence supports.

By contributing, you agree that your contribution is licensed under the repository's MIT License.
