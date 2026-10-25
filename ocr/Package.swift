// swift-tools-version: 6.0
// oi-ocr (W17): on-device text recognition of a screenshot with Apple's Vision framework.
// `oi-ocr <image>` prints the recognised text on stdout (nothing else, nothing logged, no network).
// Build with SwiftPM only (CommandLineTools, no Xcode.app): `swift build -c release`.
import PackageDescription

let package = Package(
    name: "oi-ocr",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "oi-ocr", targets: ["oi-ocr"])],
    targets: [
        // Vision request + test-image rendering (CoreText), unit-tested.
        .target(name: "OCRCore"),
        .executableTarget(name: "oi-ocr", dependencies: ["OCRCore"]),
        .testTarget(name: "OCRCoreTests", dependencies: ["OCRCore"]),
    ],
    swiftLanguageModes: [.v6]
)
