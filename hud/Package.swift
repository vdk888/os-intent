// swift-tools-version:6.0
import PackageDescription

// HUDCore = pure logic (protocol, layout, rate limit, velocity gate), unit-tested.
// oi-hud  = AppKit/SwiftUI/Carbon executable (daemon + client).
let package = Package(
    name: "oi-hud",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "oi-hud", targets: ["oi-hud"])],
    // Shared settings.json reader: `oi-hud serve` honours `hud_enabled` on each show.
    dependencies: [.package(path: "../settings")],
    targets: [
        .target(name: "HUDCore", dependencies: [.product(name: "OISettings", package: "settings")]),
        .executableTarget(
            name: "oi-hud",
            dependencies: ["HUDCore", .product(name: "OISettings", package: "settings")],
            linkerSettings: [
                // Embedded Info.plist: a bare SwiftPM executable has no bundle, and the Speech
                // Recognition / Microphone prompts need their usage strings (W6 wish dictation).
                .unsafeFlags([
                    "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist",
                    "-Xlinker", Context.packageDirectory + "/Support/Info.plist",
                ]),
            ]
        ),
        .testTarget(name: "HUDCoreTests", dependencies: ["HUDCore"]),
    ],
    // Swift 5 language mode: the daemon shares a few file descriptors between the socket
    // threads and the main thread; Swift 6 strict concurrency would need actors for a prototype.
    swiftLanguageModes: [.v5]
)
