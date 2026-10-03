import XCTest
@testable import HarborFixture

final class HarborFixtureTests: XCTestCase {
    func testFrameworkUsesLocalPackageAndClangSource() {
        XCTAssertEqual(HarborFixture.greeting(for: "  Harbor  "), "Hello, Harbor")
        XCTAssertEqual(HarborFixtureClangAdd(2, 3), 5)
    }
}
