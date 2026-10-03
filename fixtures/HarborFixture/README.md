# HarborFixture

HarborFixture is a small, repository-owned macOS Xcode integration fixture. Its shared `HarborFixture` scheme builds a Swift framework containing a C source file, resolves the repository-local `HarborSupport` Swift package, and runs one hostless XCTest.

Run it from the repository root with BuildHarbor's SSD-routed Xcode command. The fixture intentionally defines no DerivedData, package-cache, result-bundle, compiler-cache, archive, export, or temporary paths of its own.

```sh
buildharbor run -- \
  -project fixtures/HarborFixture/HarborFixture.xcodeproj \
  -scheme HarborFixture \
  -destination 'platform=macOS' \
  build

buildharbor run -- \
  -project fixtures/HarborFixture/HarborFixture.xcodeproj \
  -scheme HarborFixture \
  -destination 'platform=macOS' \
  -parallel-testing-enabled NO \
  test
```

Configure your own external APFS volume in the ignored local configuration first; follow the repository README. BuildHarbor validates the configured UUID and creates a unique result-bundle path for each test run. This local package exercises package compilation, but does not fetch a remote repository. Remote clone/cache routing needs separate integration evidence.
