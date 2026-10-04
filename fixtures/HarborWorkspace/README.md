# HarborWorkspace

This directory contains controlled workspace and nested-project traversal fixtures for BuildHarbor 0.2.

`HarborWorkspace.xcworkspace` is the positive fixture. It has two relative, in-tree project members and a shared scheme. `HarborPrimary.xcodeproj` also contains a real nested reference to `HarborNested.xcodeproj`, including container proxies, a reference proxy, and an explicit target dependency. Both targets are small unsigned static C libraries with no scripts, plugins, build rules, user schemes, or project-defined output paths.

`ConflictWorkspace.xcworkspace` is inspection-only negative input. Its scheme builds only the safe primary target, but the workspace also contains `HarborConflict.xcodeproj`. That non-scheme member references `Conflict.xcconfig`, which deliberately sets `SYMROOT` to an internal project-relative directory. BuildHarbor must discover and reject that member before any directory creation or Xcode launch. Never build the conflict workspace directly; `DO_NOT_BUILD_INTERNAL_OUTPUT` must never be created.

Future guarded validation should use the positive workspace with an explicit `-workspace`, shared `-scheme`, and macOS destination. Every workspace member and every nested project reference must resolve inside the policy root without symlinks before BuildHarbor plans any output.
