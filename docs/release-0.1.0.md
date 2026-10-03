BuildHarbor 0.1.0 adds a volume guard and a reviewable plan/run/receipt workflow for selected Xcode outputs on an external APFS disk.

I kept this release focused on three commands: `doctor`, read-only `plan`, and `run` for explicit `build` and `test` actions. It checks the configured volume UUID, refuses unsafe or conflicting managed paths, reuses build directories, allocates unique test results, and records local run receipts. It does not change global Xcode settings or migrate existing data.

## Install

You need macOS, Python 3.11+, Git, and **Xcode 27.0 (27A266a)** selected. This is the only accepted Xcode distribution in 0.1.0.

```sh
git clone --branch v0.1.0 --depth 1 https://github.com/Alexsvensson99/BuildHarbor.git
cd BuildHarbor
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/buildharbor --version
```

Follow the [first-run configuration and complete example](https://github.com/Alexsvensson99/BuildHarbor/blob/v0.1.0/README.md#first-run). Put your real disk UUID and mount path only in the ignored local file.

## Verification

- 40 simulated-system and real small-process tests cover validation, read-only planning, concurrency, signals, and rejected-run behavior.
- The real repository-owned macOS fixture built and passed XCTest on Apple Silicon, macOS 27.0 (26A428), Xcode 27.0 (27A266a).
- Loopback Git integration verified external package checkout and repository-cache paths. Repeated builds reused compiled objects and modules; `.xcresult`, compiler cache data, and temporary files were inspected on the external volume.
- A separate installed-package check verified the CLI and read-only doctor/plan behavior.
- CI checks installation and simulated-system/process behavior on Linux/Python 3.11 and macOS/Python 3.13; it is separate from the local Xcode integration.

See the [verification record](https://github.com/Alexsvensson99/BuildHarbor/blob/v0.1.0/docs/verification.md) for evidence boundaries and reproduction commands.

## Known limits

Version 0.1.0 accepts a conservative set of arguments for a single plain `.xcodeproj`. Workspaces, nested project references, xcconfig overrides, archive/export, GUI Xcode, cleanup, migration, and background services are outside this release. Other Xcode builds are rejected until verified.

BuildHarbor is not a filesystem sandbox. Build scripts, plugins, Xcode services, simulators, macOS, and some SwiftPM global metadata caches may write elsewhere. Preflight validation cannot guarantee protection from every mid-build disconnection, and destination failure may prevent a receipt from being saved. JSON/configuration contracts remain experimental.

The [roadmap](https://github.com/Alexsvensson99/BuildHarbor/blob/v0.1.0/ROADMAP.md) starts with a three-developer pilot before deciding whether to expand into archive/export and better effective-setting analysis. The pilot has not been run.

MIT licensed. No PyPI, Homebrew, or other registry publication is included.
