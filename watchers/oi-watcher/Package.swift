// swift-tools-version: 6.0
// oi-watcher: Spaces, ⌘ chords and the Safari front tab → local ActivityWatch buckets.
// Build with SwiftPM only (CommandLineTools, no Xcode.app): `swift build -c release`.
import PackageDescription

let package = Package(
    name: "oi-watcher",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "oi-watcher", targets: ["oi-watcher"]),
    ],
    dependencies: [
        // Shared settings.json reader (the watcher honours `paused`).
        .package(path: "../../settings"),
    ],
    targets: [
        // Pure logic (no permissions, no AppKit): chord formatting, event JSON,
        // bucket naming, URL/title handling, space index/delta. Unit-tested.
        .target(name: "OIWatcherCore",
                dependencies: [.product(name: "OISettings", package: "settings")]),   // W16: PageLabel (click tokens)
        // The daemon: NSWorkspace observers, CGEvent tap, NSAppleScript, REST client.
        .executableTarget(
            name: "oi-watcher",
            dependencies: ["OIWatcherCore", .product(name: "OISettings", package: "settings")],
            linkerSettings: [
                // Embed an Info.plist so TCC shows a stable name/bundle id and the
                // Automation prompt carries a usage string (CLI tools have no bundle).
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", Context.packageDirectory + "/Support/Info.plist",
                ]),
            ]
        ),
        .testTarget(name: "OIWatcherCoreTests", dependencies: ["OIWatcherCore"]),
    ],
    swiftLanguageModes: [.v6]
)
