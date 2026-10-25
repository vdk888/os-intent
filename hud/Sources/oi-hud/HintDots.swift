import AppKit
import SwiftUI
import HUDCore

// W19: the hint dots, and Liquid Glass (macOS 26+ SDK: SwiftUI `glassEffect` / `GlassEffectContainer` /
// `glassEffectID`, AppKit `NSGlassEffectView`) for the HUD capsules, the dots and the wish / vocal panels.
// Fallback to the pre-W19 NSVisualEffectView material on macOS < 26 and whenever Increase Contrast or Reduce
// Transparency is on.

/// Glass is used when the system has it and the user did not ask for contrast / less transparency.
func liquidGlassAvailable() -> Bool {
    let ws = NSWorkspace.shared
    guard !ws.accessibilityDisplayShouldIncreaseContrast, !ws.accessibilityDisplayShouldReduceTransparency else { return false }
    if #available(macOS 26, *) { return true }
    return false
}

/// The hint: one dot per hinted option, in its key's colour, close to the HUD centre in the direction of the
/// option's fixed place. Never clickable (the panel ignores the mouse while a hint is drawn).
/// W20: `dotSize` pt (settings `hud.hint_dot_size`), a thin white ring inside a black hairline (readable on
/// any background; thicker and opaque with Increase Contrast), a gentle 2-cycle pulse on appear (Reduce Motion: none).
struct HintDotsView: View {
    let dots: [(key: Int, x: Double, y: Double)]
    let colors: [Int: Color]
    let glass: Bool
    let ns: Namespace.ID
    var dotSize: Double = HintDotLayout.dotSize
    var pulsed = false
    var ring = false                       // W24: proactive hint = a thin ring around the dots ("something changed")

    var body: some View {
        let c = CGPoint(x: Metrics.panelSize.width / 2, y: Metrics.panelSize.height / 2)
        let style = HintDotLayout.style(increaseContrast: NSWorkspace.shared.accessibilityDisplayShouldIncreaseContrast)
        if ring {
            let d = HintDotLayout.ringRadius(dotSize: dotSize) * 2
            Circle()
                .stroke(Color.white.opacity(HintDotLayout.ringAlpha), lineWidth: HintDotLayout.ringWidth)
                .overlay(Circle().stroke(Color.black.opacity(0.5), lineWidth: 0.5).padding(-HintDotLayout.ringWidth / 2 - 0.25))
                .frame(width: d, height: d)
                .position(x: c.x, y: c.y)
                .allowsHitTesting(false)
        }
        ForEach(dots, id: \.key) { d in
            let color = colors[d.key] ?? Color.white
            HintDot(color: color, size: dotSize, style: style, pulsed: pulsed)
                .w19Glass(glass, tint: color.opacity(style.alpha), interactive: false, id: "opt\(d.key)",
                          ns: ns, shape: Circle())
                .position(x: c.x + d.x, y: c.y + d.y)
        }
    }
}

/// W20: one dot: the colour fill, a white ring, a black hairline outside it; `pulsed` = the appear pulse's
/// big phase (driven by the controller, HUDModel.dotPulse).
private struct HintDot: View {
    let color: Color
    let size: Double
    let style: HintDotLayout.Style
    let pulsed: Bool

    var body: some View {
        Circle()
            .fill(color.opacity(style.alpha))
            .overlay(Circle().strokeBorder(Color.white.opacity(0.95), lineWidth: style.whiteRing))
            .overlay(Circle().stroke(Color.black.opacity(0.7), lineWidth: style.blackRing)
                .padding(-style.blackRing / 2))
            .frame(width: size, height: size)
            .scaleEffect(pulsed ? HintDotLayout.pulseScale : 1)
    }
}

/// Wraps the HUD content in a GlassEffectContainer (capsules and dots of one show morph into each other).
struct GlassGroup<Content: View>: View {
    let on: Bool
    var spacing: CGFloat = 12
    @ViewBuilder var content: Content

    var body: some View {
        if #available(macOS 26, *), on {
            GlassEffectContainer(spacing: spacing) { content }
        } else {
            content
        }
    }
}

extension View {
    /// Liquid Glass in `shape` tinted with `tint`, with an identity for morphing (`id` nil = none).
    /// `on` false (or macOS < 26) = the view unchanged (the caller draws its fallback).
    @ViewBuilder
    func w19Glass(_ on: Bool, tint: Color?, interactive: Bool, id: String?, ns: Namespace.ID, shape: some Shape) -> some View {
        if #available(macOS 26, *), on {
            self.glassEffect(Glass.regular.tint(tint).interactive(interactive), in: shape)
                .glassEffectID(id, in: ns)
        } else {
            self
        }
    }
}

/// AppKit panels (wish, vocal): a rounded Liquid Glass root whose `content` holds the subviews; nil = no glass
/// (the caller keeps its NSVisualEffectView).
func glassPanelRoot(size: NSSize, radius: CGFloat) -> (root: NSView, content: NSView)? {
    guard liquidGlassAvailable() else { return nil }
    if #available(macOS 26, *) {
        let g = NSGlassEffectView(frame: NSRect(origin: .zero, size: size))
        g.cornerRadius = radius
        g.style = .regular
        let content = NSView(frame: NSRect(origin: .zero, size: size))
        g.contentView = content
        return (g, content)
    }
    return nil
}

/// W19: an option badge "2 ⌃": the number bold, the key symbol smaller and secondary; any other badge as one text.
struct BadgeLabel: View {
    let badge: String
    var body: some View {
        if let p = splitOptionBadge(badge) {
            HStack(spacing: 2) {
                Text(p.number).font(.system(size: 10, weight: .bold, design: .rounded))
                Text(p.symbol).font(.system(size: 8.5, weight: .medium, design: .rounded)).opacity(0.7)
            }
            .lineLimit(1).fixedSize()
        } else {
            Text(badge)
        }
    }
}
