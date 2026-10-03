import Foundation
import HarborSupport

public enum HarborFixture {
    public static func greeting(for name: String) -> String {
        "Hello, \(HarborSupport.normalized(name))"
    }
}
