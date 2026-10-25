// swift-tools-version: 6.0
// oi-menubar: menu-bar app for OS-intent (status, pause/resume, permissions, denylist, settings).
// Build with SwiftPM only (CommandLineTools, no Xcode.app): `swift build -c release`.
import PackageDescription

let package = Package(
    name: "oi-menubar",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "oi-menubar", targets: ["oi-menubar"]),
    ],
    dependencies: [
        // Shared settings.json reader/writer (also used by oi-watcher and oi-hud).
        .package(path: "../settings"),
    ],
    targets: [
        // Pure logic (no AppKit, no permissions): launchctl parsing, permission mapping,
        // `oi stats --json` decoding, status → menu entries. Unit-tested.
        .target(name: "MenubarCore", dependencies: [.product(name: "OISettings", package: "settings")]),
        // The app: NSStatusItem + NSMenu, probes (launchctl, aw-server, TCC), actions.
        .executableTarget(
            name: "oi-menubar",
            dependencies: ["MenubarCore", .product(name: "OISettings", package: "settings")],
            linkerSettings: [
                // Embed an Info.plist: stable bundle id for TCC rows, LSUIElement (no Dock icon).
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", Context.packageDirectory + "/Support/Info.plist",
                ]),
            ]
        ),
        .testTarget(name: "MenubarCoreTests", dependencies: ["MenubarCore", .product(name: "OISettings", package: "settings")]),
    ],
    swiftLanguageModes: [.v6]
)
