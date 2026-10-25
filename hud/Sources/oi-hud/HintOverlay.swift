import AppKit
import ApplicationServices
import QuartzCore
import HUDCore
import OISettings

/// A transparent, click-through, non-activating panel covering one screen (W11).
final class OverlayPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

/// One hint ready to draw: its option key, how, where (Cocoa global coordinates) and in which colour.
struct PlannedHint {
    let key: Int
    let drawing: HintDrawing
    let rect: CGRect              // window / dock: the target frame; edge: the screen frame; halo: unused
    let edge: HUDHint.EdgeDir?
    let color: NSColor
}

/// Draws the W11 target hints while the HUD is visible, in each option's colour:
/// - window: a soft rounded outline + glow pulsing (≈ 1.2 s period, low alpha) around the frame of the
///   app's frontmost on-screen window on this desk (CGWindowListCopyWindowInfo(.optionOnScreenOnly):
///   owner pid + bounds, layer 0; window titles are never read; no permission needed);
/// - dock: not on this desk → the same glow on the app's Dock icon, read with Accessibility on the Dock
///   process (read-only AXPosition/AXSize of the item whose title is the app), only when granted;
/// - halo: neither → the HUD draws a halo ring around that option's own icon (HUDModel.halos);
/// - edge: a soft vertical bar on the pointer screen's edge with a spring bounce (3 cycles), then a slow
///   breathing pulse.
/// Reduce Motion: static outline / bar. All hints at once, low alpha. Core Animation only (render
/// server, 120 Hz on ProMotion); `teardown()` removes every layer and orders every panel out, so
/// nothing is drawn and nothing animates while the HUD is hidden. Panels never take a click or focus.
final class HintOverlay {
    private var panels: [OverlayPanel] = []

    // MARK: resolve

    /// Where each hinted option's target is now. Returns the plan (overlay hints) and the keys that fall
    /// back to a halo around their capsule icon.
    func resolve(options: [HUDOption], colors: [Int: NSColor], axTrusted: Bool) -> (plan: [PlannedHint], halos: Set<Int>) {
        let hinted = hintedOptions(options)
        guard !hinted.isEmpty else { return ([], []) }
        let mainH = Double(NSScreen.screens.first?.frame.height ?? 0)
        var windowsCache: [WindowInfo]?
        var plan: [PlannedHint] = []
        var halos = Set<Int>()
        for (key, hint) in hinted {
            let color = colors[key] ?? .white
            switch hint {
            case .edge(let dir):
                let p = NSEvent.mouseLocation
                let screen = NSScreen.screens.first { NSMouseInRect(p, $0.frame, false) } ?? NSScreen.main
                guard let f = screen?.frame else { continue }
                plan.append(PlannedHint(key: key, drawing: .edge, rect: f, edge: dir, color: color))
            case .element(let rect):    // W16: the page button itself, when it is on a screen
                let r = cocoaRect(fromCG: rect, mainScreenHeight: mainH)
                let onScreen = NSScreen.screens.contains { $0.frame.intersects(r) }
                if resolveDrawing(hint, windowFound: onScreen, dockFound: false) == .element {
                    plan.append(PlannedHint(key: key, drawing: .element, rect: r, edge: nil, color: color))
                } else {
                    halos.insert(key)
                }
            case .window(let app):
                if windowsCache == nil { windowsCache = Self.onScreenWindows() }
                let win = frontmostWindowFrame(app: app, pids: Self.pids(of: app), windows: windowsCache ?? [])
                let dock = win == nil && axTrusted ? Self.dockIconFrame(app: app) : nil
                switch resolveDrawing(hint, windowFound: win != nil, dockFound: dock != nil) {
                case .window:
                    plan.append(PlannedHint(key: key, drawing: .window, rect: cocoaRect(fromCG: win!, mainScreenHeight: mainH),
                                            edge: nil, color: color))
                case .dock:
                    plan.append(PlannedHint(key: key, drawing: .dock, rect: cocoaRect(fromCG: dock!, mainScreenHeight: mainH),
                                            edge: nil, color: color))
                default:
                    halos.insert(key)
                }
            }
        }
        return (plan, halos)
    }

    private static func onScreenWindows() -> [WindowInfo] {
        let raw = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] ?? []
        let me = ProcessInfo.processInfo.processIdentifier
        return raw.compactMap(WindowInfo.init).filter { $0.pid != me }
    }

    private static func pids(of app: String) -> Set<Int32> {
        Set(NSWorkspace.shared.runningApplications.filter {
            $0.localizedName?.caseInsensitiveCompare(app) == .orderedSame
                || $0.bundleURL?.deletingPathExtension().lastPathComponent.caseInsensitiveCompare(app) == .orderedSame
        }.map(\.processIdentifier))
    }

    /// The app's Dock icon frame (CG global coordinates), read-only AX on the Dock. nil when absent.
    private static func dockIconFrame(app: String) -> CGRect? {
        guard let dock = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.dock").first else { return nil }
        let root = AXUIElementCreateApplication(dock.processIdentifier)
        AXUIElementSetMessagingTimeout(root, 0.2)
        for list in children(root) where string(list, kAXRoleAttribute) == (kAXListRole as String) {
            for item in children(list) where string(item, kAXTitleAttribute)?.caseInsensitiveCompare(app) == .orderedSame {
                var pos = CGPoint.zero, size = CGSize.zero
                guard let pv = value(item, kAXPositionAttribute), let sv = value(item, kAXSizeAttribute),
                      AXValueGetValue(pv as! AXValue, .cgPoint, &pos), AXValueGetValue(sv as! AXValue, .cgSize, &size),
                      size.width > 0 else { continue }
                return CGRect(origin: pos, size: size)
            }
        }
        return nil
    }

    private static func value(_ e: AXUIElement, _ attr: String) -> CFTypeRef? {
        var v: CFTypeRef?
        return AXUIElementCopyAttributeValue(e, attr as CFString, &v) == .success ? v : nil
    }
    private static func string(_ e: AXUIElement, _ attr: String) -> String? { value(e, attr) as? String }
    private static func children(_ e: AXUIElement) -> [AXUIElement] {
        (value(e, kAXChildrenAttribute) as? [AXUIElement]) ?? []
    }

    // MARK: draw

    func draw(_ plan: [PlannedHint], reduceMotion: Bool, contrast: Bool) {
        teardown()
        let screens = NSScreen.screens
        for screen in screens {
            let sf = screen.frame
            let here = plan.filter { h in
                h.drawing == .edge ? h.rect == sf : sf.intersects(h.rect.insetBy(dx: -12, dy: -12))
            }
            guard !here.isEmpty else { continue }
            let panel = OverlayPanel(contentRect: sf, styleMask: [.borderless, .nonactivatingPanel],
                                     backing: .buffered, defer: false)
            // just below the HUD; above the Dock when a Dock icon glows on this screen
            let dockLevel = Int(CGWindowLevelForKey(.dockWindow))
            panel.level = here.contains { $0.drawing == .dock } ? NSWindow.Level(rawValue: dockLevel + 1)
                                                                 : NSWindow.Level(rawValue: NSWindow.Level.floating.rawValue - 1)
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle, .stationary]
            panel.isOpaque = false
            panel.backgroundColor = .clear
            panel.hasShadow = false
            panel.ignoresMouseEvents = true
            panel.hidesOnDeactivate = false
            panel.animationBehavior = .none
            let view = NSView(frame: NSRect(origin: .zero, size: sf.size))
            view.wantsLayer = true
            panel.contentView = view
            for h in here {
                let layer = h.drawing == .edge
                    ? edgeLayer(h, screen: sf, reduceMotion: reduceMotion, contrast: contrast)
                    : outlineLayer(h, screen: sf, reduceMotion: reduceMotion, contrast: contrast)
                view.layer?.addSublayer(layer)
            }
            panel.orderFrontRegardless()
            panels.append(panel)
        }
    }

    /// Removes every layer (and so every animation) and orders every overlay panel out.
    func teardown() {
        for p in panels {
            p.contentView?.layer?.sublayers?.forEach { $0.removeAllAnimations(); $0.removeFromSuperlayer() }
            p.orderOut(nil)
        }
        panels = []
    }

    var isDrawing: Bool { !panels.isEmpty }

    private func outlineLayer(_ h: PlannedHint, screen sf: CGRect, reduceMotion: Bool, contrast: Bool) -> CALayer {
        let local = h.rect.offsetBy(dx: -sf.minX, dy: -sf.minY)
        let isDock = h.drawing == .dock
        let r = local.insetBy(dx: isDock ? -3 : -2, dy: isDock ? -3 : -2)
        let shape = CAShapeLayer()
        shape.frame = CGRect(origin: .zero, size: sf.size)
        let corner: CGFloat = isDock ? min(r.width, r.height) * 0.24 : (h.drawing == .element ? 8 : 11)   // W16
        shape.path = CGPath(roundedRect: r, cornerWidth: corner, cornerHeight: corner, transform: nil)
        shape.fillColor = isDock ? h.color.withAlphaComponent(0.18).cgColor : nil
        shape.strokeColor = h.color.cgColor
        shape.lineWidth = contrast ? 4 : 2.5
        shape.shadowColor = h.color.cgColor
        shape.shadowOpacity = contrast ? 0.6 : 0.9
        shape.shadowRadius = contrast ? 6 : 12
        shape.shadowOffset = .zero
        shape.shadowPath = shape.path?.copy(strokingWithWidth: shape.lineWidth, lineCap: .round, lineJoin: .round, miterLimit: 1)
        let low: Float = contrast ? 0.55 : 0.25, high: Float = contrast ? 0.95 : 0.65
        if reduceMotion {
            shape.opacity = high
        } else {
            shape.opacity = low
            let pulse = CABasicAnimation(keyPath: "opacity")
            pulse.fromValue = low; pulse.toValue = high
            pulse.duration = 0.6; pulse.autoreverses = true; pulse.repeatCount = .infinity   // 1.2 s period
            pulse.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            shape.add(pulse, forKey: "pulse")
        }
        return shape
    }

    private func edgeLayer(_ h: PlannedHint, screen sf: CGRect, reduceMotion: Bool, contrast: Bool) -> CALayer {
        let w: CGFloat = contrast ? 10 : 8, height = sf.height * 0.34, inset: CGFloat = 5
        let right = h.edge == .right
        let bar = CALayer()
        bar.frame = CGRect(x: right ? sf.width - inset - w : inset, y: (sf.height - height) / 2, width: w, height: height)
        bar.cornerRadius = w / 2
        bar.backgroundColor = h.color.cgColor
        bar.shadowColor = h.color.cgColor
        bar.shadowOpacity = 0.9
        bar.shadowRadius = contrast ? 6 : 14
        bar.shadowOffset = .zero
        bar.shadowPath = CGPath(roundedRect: bar.bounds, cornerWidth: w / 2, cornerHeight: w / 2, transform: nil)
        let low: Float = contrast ? 0.6 : 0.3, high: Float = contrast ? 0.95 : 0.75
        if reduceMotion { bar.opacity = high; return bar }
        bar.opacity = high
        // spring bounce toward the centre of the screen, 3 decaying cycles (≈ 1.3 s) ...
        let inward: CGFloat = right ? -1 : 1
        let bounce = CAKeyframeAnimation(keyPath: "transform.translation.x")
        bounce.values = [0, 18, 0, 10, 0, 4, 0].map { $0 * inward }
        bounce.keyTimes = [0, 0.14, 0.3, 0.46, 0.62, 0.8, 1]
        bounce.timingFunctions = (0..<6).map { CAMediaTimingFunction(name: $0 % 2 == 0 ? .easeOut : .easeIn) }
        bounce.duration = 1.3
        bar.add(bounce, forKey: "bounce")
        // ... then a slow breathing pulse (2.4 s period) for the rest of the HUD's life
        let breathe = CABasicAnimation(keyPath: "opacity")
        breathe.fromValue = high; breathe.toValue = low
        breathe.duration = 1.2; breathe.autoreverses = true; breathe.repeatCount = .infinity
        breathe.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        breathe.beginTime = CACurrentMediaTime() + 1.3
        breathe.fillMode = .backwards
        bar.add(breathe, forKey: "breathe")
        return bar
    }
}

extension NSColor {
    /// "#RRGGBB" → sRGB colour (white when malformed).
    static func hex(_ s: String) -> NSColor {
        guard let c = HUDColor.rgb(s) else { return .white }
        return NSColor(srgbRed: c.r, green: c.g, blue: c.b, alpha: 1)
    }
}
