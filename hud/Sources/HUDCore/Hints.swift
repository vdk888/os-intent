import Foundation
import CoreGraphics
import OISettings

// W11: a colour per option key, and the target of each option pulses
// subtly in that colour while the HUD is visible. Pure logic here; the drawing is oi-hud HintOverlay.

// MARK: - colour per key

/// The colour of option `key`: the colour of the modifier that owns the option's FIXED place
/// (settings.json `hud.slots`), so colour, place and key always agree and stay the same across shows
/// (⌃ blue top, ⌥ yellow right, ⌘ green bottom, fn pink left by default). nil for the wish option.
public enum OptionColors {
    public static func modifier(forOption key: Int, positions: [Int: SlotPosition],
                                slots: [TapModifier: SlotPosition]) -> TapModifier? {
        guard let p = positions[key] else { return nil }
        let map = HUDSettings.valid(slots) ? slots : HUDSettings.defaultSlots
        return map.first(where: { $0.value == p })?.key
    }

    public static func color(forOption key: Int, positions: [Int: SlotPosition], hud: HUDSettings) -> HUDColor? {
        modifier(forOption: key, positions: positions, slots: hud.slots).map { hud.colors[$0] }
    }

    /// Light or dark variant ("#RRGGBB").
    public static func variant(_ c: HUDColor, dark: Bool) -> String { dark ? c.dark : c.light }
}

// MARK: - label fitting

/// The longest label a capsule shows (W11: "Switch to Telegram", "Open notion.so/…" fit whole).
public let maxLabelChars = 22

/// Fit a label in `max` characters without cutting a word in the middle, keeping the app or host name:
/// 1. a page label "Open host/path…" keeps the host: "Open notion.so/…";
/// 2. still too long: drop the verb ("Switch to ", "Open ", "Slide to "): "notion.so/…", "Microsoft Excel";
/// 3. still too long: whole words + "…" ("Microsoft PowerPoint…"); a single word longer than `max`
///    is the only case cut inside a word.
public func fitLabel(_ label: String, max n: Int = maxLabelChars) -> String {
    var work = label.trimmingCharacters(in: .whitespaces)
    if work.count <= n { return work }
    if let slash = work.firstIndex(of: "/") {
        work = String(work[..<slash]) + "/…"
        if work.count <= n { return work }
    }
    for verb in ["Switch to ", "Open ", "Slide to "] where work.hasPrefix(verb) {
        work = String(work.dropFirst(verb.count))
        if work.count <= n { return work }
        break
    }
    var acc = ""
    for w in work.split(separator: " ") {
        let next = acc.isEmpty ? String(w) : acc + " " + w
        if next.count + 1 > n { break }
        acc = next
    }
    if !acc.isEmpty { return acc + "…" }
    var cut = String(work.prefix(n - 1))
    while let last = cut.last, last == "." || last == "/" || last == "-" { cut.removeLast() }
    return cut + "…"
}

// MARK: - window frame matching

/// One on-screen window from CGWindowListCopyWindowInfo(.optionOnScreenOnly): owner name and pid,
/// layer, bounds (CG global coordinates, y down from the top of the main display) and alpha. Window
/// titles are never read.
public struct WindowInfo: Equatable, Sendable {
    public var ownerName: String
    public var pid: Int32
    public var layer: Int
    public var bounds: CGRect
    public var alpha: Double

    public init(ownerName: String, pid: Int32, layer: Int, bounds: CGRect, alpha: Double = 1) {
        self.ownerName = ownerName; self.pid = pid; self.layer = layer; self.bounds = bounds; self.alpha = alpha
    }

    /// From one CGWindowList dictionary (kCGWindowOwnerName, kCGWindowOwnerPID, kCGWindowLayer,
    /// kCGWindowBounds, kCGWindowAlpha); nil when a field is missing.
    public init?(_ d: [String: Any]) {
        guard let pid = (d["kCGWindowOwnerPID"] as? NSNumber)?.int32Value,
              let layer = (d["kCGWindowLayer"] as? NSNumber)?.intValue,
              let b = d["kCGWindowBounds"] as? [String: Any],
              let x = (b["X"] as? NSNumber)?.doubleValue, let y = (b["Y"] as? NSNumber)?.doubleValue,
              let w = (b["Width"] as? NSNumber)?.doubleValue, let h = (b["Height"] as? NSNumber)?.doubleValue
        else { return nil }
        self.init(ownerName: d["kCGWindowOwnerName"] as? String ?? "", pid: pid, layer: layer,
                  bounds: CGRect(x: x, y: y, width: w, height: h),
                  alpha: (d["kCGWindowAlpha"] as? NSNumber)?.doubleValue ?? 1)
    }
}

/// The frame of `app`'s frontmost normal window on the current desk: the first window in the list
/// (CGWindowList order is front to back) owned by one of `pids` (or named `app` when no pid is
/// known), on layer 0, visible and not a sliver (≥ 80×60). nil = the app has no window on this desk.
public func frontmostWindowFrame(app: String, pids: Set<Int32>, windows: [WindowInfo]) -> CGRect? {
    windows.first { w in
        w.layer == 0 && w.alpha > 0.01 && w.bounds.width >= 80 && w.bounds.height >= 60
            && (pids.isEmpty ? w.ownerName.caseInsensitiveCompare(app) == .orderedSame : pids.contains(w.pid))
    }?.bounds
}

/// CG global rect (y down from the top of the main display) → Cocoa screen rect (y up).
public func cocoaRect(fromCG r: CGRect, mainScreenHeight h: Double) -> CGRect {
    CGRect(x: r.minX, y: h - r.maxY, width: r.width, height: r.height)
}

// MARK: - what gets drawn

/// How a hint is drawn: around the app's window, on its Dock icon, as a halo around the option's own
/// icon in the HUD, or as a bar on a screen edge.
public enum HintDrawing: String, Equatable, Sendable {
    case window, dock, halo, edge
    case element      // W16: an outline around the page button itself
}

public struct DrawnHint: Equatable, Sendable {
    public var key: Int
    public var drawn: HintDrawing
    public init(key: Int, drawn: HintDrawing) { self.key = key; self.drawn = drawn }
}

/// The fallback chain for one hint: window on this desk → Dock icon (only with Accessibility, read-only
/// AX on the Dock) → halo around the option's capsule icon. An edge hint is always an edge bar.
public func resolveDrawing(_ hint: HUDHint, windowFound: Bool, dockFound: Bool) -> HintDrawing {
    switch hint {
    case .edge: return .edge
    case .window: return windowFound ? .window : (dockFound ? .dock : .halo)
    case .element: return windowFound ? .element : .halo     // W16: windowFound = the rect is on a screen
    }
}

/// The options that carry a hint (the wish option never does).
public func hintedOptions(_ options: [HUDOption]) -> [(key: Int, hint: HUDHint)] {
    options.compactMap { o in
        guard o.key != wishOptionKey, let h = o.hint else { return nil }
        return (o.key, h)
    }
}

// MARK: - overlay lifecycle

/// The hint overlay's lifecycle (W11): drawn only while the HUD is visible. `show` (a new show or an
/// in-place replacement) draws the current hints, replacing the previous ones; `finish` (the single
/// exit of the HUD) tears everything down. Invariant: `drawn` is empty whenever `visible` is false,
/// so nothing is on screen and nothing animates (no CPU) while the HUD is hidden.
public struct HintOverlayLifecycle: Sendable {
    public enum Effect: Equatable, Sendable {
        case draw([DrawnHint])     // (re)build the overlay panels with exactly these hints
        case teardown              // order out and release every overlay panel, stop every animation
    }

    public private(set) var visible = false
    public private(set) var drawn: [DrawnHint] = []

    public init() {}

    public mutating func show(_ hints: [DrawnHint]) -> [Effect] {
        visible = true
        if hints.isEmpty {
            defer { drawn = [] }
            return drawn.isEmpty ? [] : [.teardown]
        }
        drawn = hints
        return [.draw(hints)]
    }

    public mutating func finish() -> [Effect] {
        visible = false
        defer { drawn = [] }
        return drawn.isEmpty ? [] : [.teardown]
    }
}
