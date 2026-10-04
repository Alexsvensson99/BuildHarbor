# Version 0.2 development verification

This is the historical internal checkpoint. Version 0.2 was not released separately; its implementation is included in 0.3.0. See the [0.3 verification record](verification-0.3.md) for subsequent checks and release gates.

Version 0.2 was exercised locally on **Apple Silicon (arm64), macOS 27.0 (26A428), Xcode 27.0 (27A266a)**. The automated unit/process suite passed under **Python 3.11.13** and **Python 3.13.4**. This record describes a development milestone on 2026-10-04. Version 0.2 has not run its new-branch GitHub CI gate and has not been published as a release.

The private raw logs, receipts, generated results, and source copies remain on the approved external APFS volume. This public record omits their absolute paths, local volume identity, and run UUIDs.

## Automated checks

The same **94 unit and small process tests** passed under Python 3.11.13 and Python 3.13.4. They cover the 0.1 guards plus the 0.2 static project parser, workspace/nested-project traversal, shared schemes, package metadata, effective-setting validation, bounded settings subprocesses, archive/export binding, signing restrictions, unique outputs, input mutation, child cleanup, and privacy-oriented diagnostics.

Volume and Xcode responses are simulated in these tests. Small Python subprocesses exercise timeout, signal-forwarding, output-limit, and reaping behavior. A green automated suite is not evidence that Xcode used the external routes; the live fixtures below provide that narrower evidence.

## Remote-package regression

Private evidence set `integration-e0476115c63d` ran a repository-owned macOS fixture whose Git package was served by a transient loopback HTTP server. It used paths containing spaces and ran sequentially with a bounded Xcode job count.

| Run | Result | Observed evidence |
| --- | --- | --- |
| First build | Exit 0 | 2 compiled object files, 138 compiled modules, 1 package checkout, and 1 repository-cache entry under the managed project storage. |
| Repeat build | Exit 0 | The same counts remained, and the selected object/module modification times were unchanged. |
| Test | Exit 0 | 2 compiled object files, 234 compiled modules, 1 package checkout, and 1 repository-cache entry; the test action wrote its managed result bundle and receipt. |

The summary records `reused_objects_and_modules: true`. This demonstrates reuse for this fixture and exact toolchain. It is not a general performance measurement or a claim that every package or compiler cache is external.

The loopback HTTP transport matters. SwiftPM can treat a `file://` dependency as local and skip its shared repository cache. This fixture exercised the repository-cache route without contacting an external package host.

## Full 0.2 milestone

Private evidence set `milestones-4bb2226ba6f6`, attempt `93e8d7bc`, completed the following sequence. Every requested action returned exit 0.

| Action | Effective-setting evidence | Result |
| --- | --- | --- |
| Project build | 1 selected target; 42 managed path settings checked | Build and receipt succeeded. |
| Project test | 2 selected targets; 84 managed path settings checked | Test and receipt succeeded. |
| Two-member workspace build | 2 selected targets; both static member projects queried; 170 managed path settings checked | Workspace build and receipt succeeded. Xcode returned two exact duplicate member records, which were ignored under the narrow exact-equality rule. |
| macOS archive | 1 selected target; 43 managed path settings checked | One simple macOS application archive and its digest-bound receipt succeeded. |
| Local Copy App export | Archive and receipt identity rechecked | Export and receipt succeeded using BuildHarbor's generated `mac-application` / `export` options. |

The milestone also confirmed that `plan` left the inspected source metadata unchanged and that an output conflict in a non-selected workspace member blocked execution. Xcode did not expose definition-source locations in its settings JSON, so receipts accurately record `definition_sources: not_exposed_by_xcode` rather than inventing a source.

The exported application passed `codesign --verify --strict`. Its signature was ad-hoc, it had no team identifier, and its bundle identifier was `org.example.HarborApp`. No signing account, private identity, keychain selection, provisioning profile, distribution signing, simulator, or Xcode GUI launch was used.

## Corrected baselines and retained limits

An earlier baseline did not route a unique result bundle for build failures. That baseline is not the evidence above. The implementation now supplies a unique result bundle for every build, test, and archive action, and the automated suite covers the build-failure path.

Earlier archive experiments also showed that forcing the ordinary build `SYMROOT`, `OBJROOT`, and `DSTROOT` values onto the main archive action broke Xcode's packaging layout. The successful milestone lets `-derivedDataPath` establish the action-specific `ArchiveIntermediates` tree, then checks its resolved product, intermediate, and installation roots before archive execution.

These checks did **not** trace every filesystem write. They establish that the inspected outputs, package checkout/repository cache, results, archive, export, compiler artifacts, and receipts appeared under the managed external project storage. They do not prove that Xcode, SwiftPM, compilers, dependencies, or macOS wrote nothing elsewhere. In particular, global SwiftPM manifest/metadata caches and other toolchain-managed state remain outside the supported routing claim.

Other limits remain:

- Compatibility is restricted to the exact macOS/Xcode/architecture combination above and the controlled repository fixtures.
- The accepted graph excludes script phases, custom build rules, private schemes, unsupported copy destinations and object types, complex package forms, and unresolved references.
- Archive/export covers one simple macOS application with disabled or manual ad-hoc signing. It does not cover Developer ID, App Store, notarization, provisioning, installers, iOS-family platforms, or caller-supplied export options.
- The settings checks cover the managed output-setting set and safe product-relative values. They do not turn Xcode into a filesystem sandbox.
- No real-drive disconnection was performed mid-action. Mount replacement, force-kill, and detached descendant behavior remain bounded by the documented checkpoints and process model.
- There has been no use by external developers and no broader-project compatibility test.

## Release and CI boundary

The local milestone is complete, but the 0.2 branch has not run the repository's GitHub Actions matrix and no 0.2 release has been tagged or published. The release gate still requires a clean-checkout CI result, publication checks, manual privacy review, and reconciliation of the final documentation with the exact candidate revision.

The GitHub workflow uses simulated system responses and small process tests on Linux/Python 3.11 and macOS/Python 3.13. It does not create an external APFS volume or run Xcode. Local Xcode evidence and CI success are separate checks.

## Repeating the checks

The automated checks do not need Xcode or an external disk:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -v
python3 scripts/check_publication.py
```

The live scripts are opt-in and require the ignored local volume configuration described in the README:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 scripts/verify_xcode.py --jobs 2
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 scripts/verify_milestones.py --jobs 2
```

Both scripts validate the external volume before writing evidence and retain their private logs, receipts, fixture copies, and summaries on that volume. Review them before running. They do not clean earlier evidence or change global Xcode preferences.

## Primary references

- The installed Xcode 27.0 `xcodebuild -help` documents `-showBuildSettings`, `-json`, `-alltargets`, `-derivedDataPath`, `-resultBundlePath`, `-clonedSourcePackagesDirPath`, `-packageCachePath`, `-archivePath`, `-exportArchive`, `-exportPath`, and `-exportOptionsPlist`.
- Apple's [build settings reference](https://developer.apple.com/documentation/xcode/build-settings-reference) documents the output settings checked by BuildHarbor.
- Apple's [Configuring the build settings of a target](https://developer.apple.com/documentation/xcode/configuring-the-build-settings-of-a-target/) describes build-setting levels and command-line precedence.
- [ADR 0002](adr/0002-effective-settings-and-local-export.md) records why planning stays static, why effective queries run only inside guarded execution, and why archive/export remains local and non-credentialed.

Primary documentation supports the individual command and setting controls. The results above establish only the observed behavior of these controlled fixtures on the exact environment stated here.
