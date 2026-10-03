// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "HarborSupport",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "HarborSupport", targets: ["HarborSupport"]),
    ],
    targets: [
        .target(name: "HarborSupport"),
    ]
)
