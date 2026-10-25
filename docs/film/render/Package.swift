// swift-tools-version:6.0
import PackageDescription

// Demo stills of the OS-intent HUD, rendered OFFSCREEN with FAKE data only.
// HUDCore + HUDView.swift + HintDots.swift are symlinks into ../../../hud (the real HUD code);
// the renderer never opens a socket, registers a hotkey or puts a window on screen.
let package = Package(
    name: "hud-render",
    platforms: [.macOS(.v14)],
    dependencies: [.package(path: "../../../settings")],
    targets: [
        .target(name: "HUDCore", dependencies: [.product(name: "OISettings", package: "settings")]),
        .executableTarget(name: "HUDRender",
                          dependencies: ["HUDCore", .product(name: "OISettings", package: "settings")]),
    ],
    swiftLanguageModes: [.v5]
)
