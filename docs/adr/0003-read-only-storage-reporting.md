# ADR 0003: Report managed storage with conservative inode attribution

- Status: Accepted for 0.3 development; controlled report fixture passed, final verification pending
- Date: 2026-10-04
- Applies to: BuildHarbor 0.3 development line

## Context

BuildHarbor's managed root can contain reusable output for several configured checkouts as well as receipts, evidence, and data that no supplied project configuration can identify. A useful report has to distinguish project, shared, and unattributed data without double counting hard links or implying that filesystem metadata measures physical APFS use.

Reporting must remain separate from cleanup. It must not create a missing storage root, alter managed data, invoke Xcode, or turn an incomplete traversal into a confident total. It also needs fixed work limits so a very large or changing tree cannot consume unbounded time or memory.

## Decision

### Command and configuration boundary

The development command is:

```text
buildharbor report [--project-dir DIR ...] [--json]
```

With no `--project-dir`, the current directory supplies the configuration. The option may be repeated to classify storage for several checkouts. Every loaded configuration must name the exact same mount, volume UUID, and storage root. Project roots are resolved before scanning.

Repeated configuration for the same resolved project root is deduplicated. One resolved root with conflicting project IDs is rejected. When different resolved roots claim the same project ID, that ID is marked ambiguous and omitted from the known-project buckets; matching storage directories are unattributed.

`report` does not inspect or require Xcode. It verifies that the configured mount is present, unlocked, external APFS, UUID-matched, and readable/traversable. Unlike build preparation, reporting permits a read-only volume and does not enforce `minimum_free_gib`.

### Traversal is read-only and bounded

The scanner opens the mount, storage-root components, directories, and regular files with read-only and no-follow flags. Regular files are opened only to compare `fstat` metadata with the directory entry; their contents are never read. Symlinks are counted and skipped without being followed. Special files are counted and skipped without being opened. Directory sizes are not included.

The public command uses fixed maximums:

- 200,000 directory entries;
- 100,000 distinct inode keys;
- depth 64; and
- 30 seconds.

The deadline is cooperative. It is checked between filesystem operations and cannot interrupt a kernel I/O call that is already blocked. Hitting a limit produces an incomplete report instead of raising the limit or continuing without bounds.

The scanner intentionally performs no create, write, truncate, rename, unlink, cleanup, migration, or compression operation. Controlled tests compare content, mode, modification time, and change time before and after a scan. Access time is excluded from that guarantee because the operating system may update it when files or directories are opened for metadata.

### Accounting uses unique regular-file inodes

Each observed regular-file inode contributes once to the totals:

- `logical_bytes` is its `st_size`;
- `allocated_bytes` is `st_blocks * 512`; and
- `regular_files` increases by one.

These are logical size and inode-reported allocated blocks. They are not measurements of APFS physical usage, clone sharing, snapshots, compression savings, purgeable space, or reclaimable capacity.

BuildHarbor recognizes a project-owned location only under `projects/<project_id>-<16 lowercase hexadecimal characters>/...` for a non-ambiguous supplied project ID. It collects the known owners seen for every inode and compares observed links with `st_nlink`:

- exactly one known owner and all links observed: that project's bucket;
- more than one known owner and all links observed: `shared`;
- an unknown path, ambiguous project ID, no known owner, an outside or otherwise unseen hard link, changing/conflicting inode metadata, or an incomplete link observation: `unattributed`.

This rule favors under-attribution. BuildHarbor does not assign an inode to a project when any observed or metadata-indicated link makes ownership uncertain.

### Completion and failure semantics are explicit

A storage report has schema version 1, kind `storage_report`, the current `tool_version`, one bucket per known project, `shared`, `unattributed`, ignored-entry counts, ambiguous IDs, measurement definitions, and stable issue-code counts. Text output omits scanned file names; JSON also reports categories and issue codes rather than individual paths.

`status: complete` and exit 0 mean the bounded traversal finished and the volume/root identity checks passed. The result is still `consistency: best_effort_non_atomic`: files can change between calls, and the scanner does not freeze the filesystem.

`status: incomplete`, `is_lower_bound: true`, and exit 2 mean that one or more portions could not be inspected safely. Totals already observed are retained as lower bounds. Permission failures, unreadable or changing entries/directories, metadata conflicts, filesystem boundaries, directory cycles, limit exhaustion, root identity changes, and volume revalidation failures use stable issue codes. Unexpected configuration errors also exit 2 through the normal bounded CLI error response.

A correctly verified volume with no storage root returns `storage_root_state: absent`, zero totals, complete status, and exit 0. The root is not created. A volume or root that cannot be verified is unavailable and incomplete rather than an empty success.

### Reporting remains informational

The command does not suggest that large buckets are safe to delete and does not offer a cleanup action. Project attribution describes the supplied configurations and observed links, not liveness, value, age, rebuild cost, or deletion safety.

## Evidence boundary

One controlled real-storage fixture completed on the configured external APFS volume. It reconciled four unique regular files: one project-owned, one shared across known projects, and two unattributed. The fixture reported 97 logical bytes and 16,384 allocated bytes, ignored one symlink, and returned complete status. Separate cases verified a safely absent root and an incomplete lower-bound result. The checked fixture metadata and content remained unchanged.

A live scan of BuildHarbor's own managed root also returned complete status with no issues. Its transient local totals are intentionally not a public compatibility claim. All 117 unit/process tests pass under Python 3.11.13 and 3.13.4. The final archive/export regression and clean-install, publication, and privacy checks remain open. [The 0.3 verification record](../verification-0.3.md) separates completed and pending evidence, and no 0.3 release has been published.

## Alternatives considered

### Sum directory entry sizes

Summing every pathname double counts hard links. Directory `st_size` also does not represent the regular-file data managed beneath it. Unique inode accounting gives a reconcilable logical view.

### Attribute by path alone

A regular file can have links under more than one project or outside the scanned root. Path-only attribution would assign shared or incompletely observed data to one project. The chosen rule uses all observed owners and `st_nlink`, then moves uncertainty to unattributed.

### Report allocated blocks as physical disk use

`st_blocks * 512` is useful inode metadata but does not resolve APFS clones, compression, snapshots, or reclaimability. Labelling it as physical use would be false precision.

### Treat failures as zero

A zero after a permission failure or disconnected volume looks like an empty root. Incomplete status, issue codes, lower-bound labels, and exit 2 preserve the distinction.

### Add cleanup to the report

Storage measurement does not establish deletion safety. Cleanup would need separate ownership, liveness, recovery, and authorization rules and remains outside BuildHarbor.

## Consequences

The report is useful for comparing managed project, shared, and unattributed regular-file storage without changing it. It is deterministic for a stable tree, bounded, privacy-aware, and usable when Xcode is unavailable or the correctly identified volume is read-only or below the build-capacity threshold.

It remains a point-in-time best effort over a live filesystem. Unknown and incompletely observed hard links reduce attribution, and concurrent mutations can make a report incomplete. The numbers describe logical bytes and allocated blocks, not physical APFS consumption or reclaimable space. The operation can also wait longer than 30 seconds if a kernel filesystem call blocks, because user-space deadline checks cannot preempt that call.
