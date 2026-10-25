// swift-tools-version: 6.0
// OISettings: the shared reader/writer for ~/.local/share/os-intent/settings.json.
// Used by oi-menubar (read/write), oi-watcher (reads `paused`) and oi-hud (reads `hud_enabled`).
// Foundation only, no AppKit, no permissions. `swift test` covers the codec and the atomic write.
import PackageDescription

let package = Package(
    name: "oi-settings",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "OISettings", targets: ["OISettings"]),
    ],
    targets: [
        .target(name: "OISettings"),
        .testTarget(name: "OISettingsTests", dependencies: ["OISettings"]),
    ],
    swiftLanguageModes: [.v6]
)
