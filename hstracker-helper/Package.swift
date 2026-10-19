// swift-tools-version:5.7
import PackageDescription

// Pinned to the realm-swift release that bundles the same Realm Core (12.11.0)
// as HSTracker 3.7.0, so the database file format is never upgraded behind
// HSTracker's back.
let package = Package(
    name: "hstracker-helper",
    platforms: [.macOS(.v11)],
    dependencies: [
        .package(url: "https://github.com/realm/realm-swift.git", exact: "10.32.3")
    ],
    targets: [
        .executableTarget(
            name: "hstracker-helper",
            dependencies: [.product(name: "Realm", package: "realm-swift")]
        )
    ]
)
