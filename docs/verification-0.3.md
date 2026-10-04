# Version 0.3 development verification

Version 0.3 is implemented locally. This record covers the 2026-10-04 checks on Apple Silicon (arm64), macOS 27.0 (26A428), and Xcode 27.0 (27A266a). The public release remains 0.1.0. Neither 0.2 nor 0.3 has been tagged, pushed, or published as a new release, and the new GitHub Actions matrix has not run.

## Automated checks

All **117 unit and small-process tests** passed under both **Python 3.11.13** and **Python 3.13.4**. The suite covers simulated volume failures, path guards, static project/workspace inspection, effective settings, locks, signals, child reaping, archive/export provenance, report accounting, read-only metadata, and CLI exit status.

The report tests include same-project and cross-project hard links, unknown or outside links, duplicate project IDs, ignored symlinks and special files, absent storage, permission and volume failures, entry/inode/depth/time bounds, non-finite limits, and descriptor cleanup after an initial metadata-read failure. CLI tests confirm that reporting needs no Xcode, does not create an absent root, and permits a verified read-only or low-capacity volume without weakening the identity checks.

These tests simulate macOS volume and Xcode responses. They are separate from the real-volume and Xcode checks below.

## Real-volume storage reporting

The opt-in `scripts/verify_reporting.py` created a small, isolated fixture on the already verified external APFS volume. Private evidence set `report-45dae4777702404fa5b8b51c398c4da2` records:

| Category | Unique regular files | Logical bytes | Allocated bytes |
| --- | ---: | ---: | ---: |
| Project alpha | 1 | 22 | 4,096 |
| Project beta | 0 | 0 | 0 |
| Shared | 1 | 23 | 4,096 |
| Unattributed | 2 | 52 | 8,192 |
| Total | 4 | 97 | 16,384 |

Same-project links counted once, links shared by two configured projects counted once as shared, and links involving an unknown location or an unobserved name outside the storage root remained unattributed. One symlink to an outside file was skipped. The full scan was complete with no issues. A missing storage root remained absent and returned a complete empty report. A deliberately limited scan returned an explicit incomplete result with `entry_limit_reached`.

Before/after snapshots matched file contents, types, modes, sizes, modification times, and change times. Access times were excluded because the operating system can update them during reads. The reporting command itself does not create, remove, rewrite, or timestamp files, read regular-file contents, or start Xcode.

A separate read-only scan of the actual configured BuildHarbor storage completed successfully: 20,587 unique regular-file inodes, 57 skipped symlinks, and no issues at that point. Those counts are an observation, not a persistent inventory or capacity claim.

Logical bytes use `st_size`; allocated bytes use `st_blocks * 512`, once per observed device/inode pair. Directory metadata is excluded. These figures do not measure APFS physical consumption, clone sharing, or reclaimable capacity. Scans are non-atomic, and the time limit is checked between filesystem calls; it cannot interrupt stalled kernel I/O. See [ADR 0003](adr/0003-read-only-storage-reporting.md) for the classification contract.

## Build, test, workspace, archive, and export

The [0.2 verification record](verification-0.2.md) retains the full successful build/test/workspace/archive/export milestone and the remote-package build/rebuild/test regression, including cache reuse on the exact allowlisted Xcode.

Final review added three restrictions before completing 0.3:

- Shared schemes containing `TestPlans` or `TestPlanReference`, and caller-supplied `-testPlan`, are rejected. Their external JSON and environment semantics are not inspected by the current parser.
- Archive fingerprinting now bounds directory-name collection while reading it and limits recursion depth, rather than collecting an unbounded directory before checking its size.
- Generated export options are rebound to the exact device/inode, regular-file type, single link, fixed bytes, and stable metadata after the archive/receipt rechecks and immediately before export starts. Mutation tests confirm that overwrites, same-content file replacements, and new hard links block the action.

After those changes, a targeted real archive/export regression passed under version **0.3.0**, reusing the controlled fixture and caches. Private evidence `final-guards-f3254aef` records archive exit 0, export exit 0, and unchanged source metadata during planning. The exported application again passed `codesign --verify --strict`, with an ad-hoc signature and no team identifier.

Runtime archive checks validate effective signing settings and inspect the produced application's structure. BuildHarbor does not run a general post-build signature/team audit. The strict signature result above is controlled-fixture evidence. Archive/export support remains limited to a simple macOS app, disabled or manual ad-hoc signing, and local Copy App export; distribution and private signing identities remain unsupported.

## Clean installation

The isolated installation check passed from local candidate revision `42712e097fcd81d6e639c72a5d722843cbc98e02`. `scripts/verify_install.py` extracted that committed source, copied the ignored configuration only into the private source copy, and installed version **0.3.0** into a fresh virtual environment on the verified external volume. Build dependencies, temporary data, and installation evidence also used that volume.

Private evidence set `install-6b190939f2af426396fc6d767911f8b3` records exit 0 for `--version`, `doctor`, `plan`, and `report`. Doctor was ready for planning; the build plan correctly left effective settings pending until a guarded run; the storage report was complete. The check did not execute Xcode or a build action.

Snapshots matched all 154 source-tree entries and 16 installed-package entries before and after those commands. Contents, modes, modification times, and change times were unchanged; no package bytecode was present before or after. Access times were excluded. Subsequent changes to this candidate only finalize documentation and the verification record.

## Final review

The publication checker passed for all 81 publishable files and reachable Git history. Manual review kept local configuration, private paths, real volume UUIDs, credentials, generated output, and raw logs outside that history. Relative documentation links and whitespace checks passed.

A bounded read-only diagnostic review covered 2026-10-04 02:27:00–02:56:35 UTC, after the Xcode runs had finished. It parsed one recent diagnostic file and found no matching Harbor, XCTest, xcodebuild, Swift, or Clang reports, with no inventory warnings. No diagnostic files were removed or modified. This is a time- and process-scoped observation, not a general machine-health claim.

## Remaining release work

The local checks do not replace the complete GitHub Actions matrix, a review of the final public candidate, or a separate release decision. No external developer program has started. Broader project shapes, other Xcode distributions, real signing/distribution, GUI Xcode, and complete filesystem isolation remain outside the verified claim.

Repeat the report check only when needed, after configuring the ignored local volume file:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 scripts/verify_reporting.py
```

This creates a new retained evidence directory. It does not clean earlier data. The `report` command itself remains read-only.
