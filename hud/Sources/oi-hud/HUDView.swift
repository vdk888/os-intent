import AppKit
import SwiftUI
import QuartzCore
import HUDCore
import OISettings

enum Metrics {
    /// The window: W15 room around the cross for the glide's sub-rings (transparent, click-through outside
    /// the capsules; it may extend past the screen edge, the sub-rings are clamped to the screen).
    static let panelSize = CGSize(width: 1200, height: 900)
    /// The cross + wish (4:30) + vocal (7:30, W12): what is kept on screen when the HUD is placed at the pointer.
    static let contentSize = CGSize(width: 600, height: 400)
    static let ringRadius: Double = 80
    /// W11: wide enough for 22 characters ("Switch to Telegram", "Open notion.so/…") + a 28-pt icon.
    /// The user 20/10 16:37 Paris: smaller, more discreet (was 252x44). Since 16:40 ("less text, more symbols")
    /// the width follows the content (`capsuleWidth`, 72-200); this is the maximum.
    /// W19 (smaller, more subtle): height 28 (was 32), label 10.5 pt (was 11.5), icon 16 pt (was 20).
    static let capsuleSize = CGSize(width: 200, height: 28)
    static let labelPt: CGFloat = 10.5
    static let labelFont = NSFont.systemFont(ofSize: labelPt, weight: .medium)
    static let badgeFont: NSFont = {
        let f = NSFont.systemFont(ofSize: 9.5, weight: .semibold)
        return f.fontDescriptor.withDesign(.rounded).flatMap { NSFont(descriptor: $0, size: 9.5) } ?? f
    }()
    static let iconSize: Double = 16      // W19: was 20
    static let wishWidth: Double = 210
    /// How far the cross reaches from the centre (the side capsules' outer edge).
    static var reach: Double {
        abs(FixedSlots.offset(.right, radius: ringRadius, capsuleWidth: capsuleSize.width).x) + capsuleSize.width / 2
    }

    /// The text a capsule shows: none for the wish and vocal options (icon + key only, the user 16:40),
    /// the fitted label otherwise.
    static func shownLabel(_ o: HUDOption) -> String {
        o.key == wishOptionKey || o.key == vocalOptionKey ? "" : fitLabel(o.label)
    }

    /// A capsule's width from its content (icon + label + badge + padding, 72-200).
    static func width(_ o: HUDOption, badge: String) -> Double {
        func w(_ s: String, _ f: NSFont) -> Double { s.isEmpty ? 0 : Double((s as NSString).size(withAttributes: [.font: f]).width) }
        return capsuleWidth(iconSize: iconSize, labelWidth: w(shownLabel(o), labelFont),
                             badgeWidth: w(badge, badgeFont) + (splitOptionBadge(badge) != nil ? 4 : 0))   // W19: bold number
    }

    /// Every option's centre offset and size (W10: each at the fixed place of its key, the wish
    /// option at its own fixed place below-right, W12 the vocal option below-left).
    static func slots(_ options: [HUDOption], _ positions: [Int: SlotPosition], badges: [Int: String]) -> [HUDSlot] {
        hudSlots(options: options, positions: positions, radius: ringRadius, capsuleHeight: capsuleSize.height) {
            width($0, badge: badges[$0.key] ?? "")
        }
    }
    static let appear = 0.15                 // 150 ms fade + scale
    static let spring = Animation.spring(response: 0.3, dampingFraction: 0.8)
}

final class HUDModel: ObservableObject {
    @Published var options: [HUDOption] = []
    /// Option key → fixed place (settings.json `hud.slots` + `hotkeys`, resolved on each show).
    var positions: [Int: SlotPosition] = FixedSlots.positions(hotkeys: .defaults, slots: HUDSettings.defaultSlots)
    @Published var visible = false
    /// Badge per option key (from settings.json `hotkeys`, e.g. "⌥1", "⌥`" for the wish option).
    var badges: [Int: String] = [:]
    var reduceMotion = false
    var increaseContrast = false
    /// W11: option key → its colour (dark variant: the capsules are always dark), stable per key.
    var colors: [Int: Color] = [:]
    /// W11: options whose target is not on screen: a halo pulses around their icon (cleared on hide).
    @Published var halos: Set<Int> = []
    /// The capsule under the pointer (clickable): a subtle highlight in its key's colour.
    @Published var hovered: Int?
    // W14 Claude-assisted options
    var claudeKeys: Set<Int> = []           // options with a Claude variant (long-press ring drawn only on them)
    @Published var ringKey: Int?            // the capsule whose modifier is held
    @Published var ringFill: Double = 0     // 0...1, full = release now runs the Claude variant
    @Published var working: Int?            // Claude works on this option: spinner, the others dimmed
    @Published var card: String?            // the result card (≤ 4 lines); the capsules are hidden meanwhile
    var cardOK = true
    @Published var cardOpen = false         // the card offers « Open in Mail » (a draft saved in place)
    // W15 glide
    @Published var rings: [SubRing] = []    // the open sub-rings (ancestors of the last expansion)
    @Published var trail: [Int]?            // the hovered path during a glide (its ancestors are highlighted)
    @Published var thinkingPath: [Int]?     // the capsule whose follow-ups are being asked (progress arc)
    @Published var thinkingFill: Double = 0
    var colorHex: [Int: String] = [:]       // option key → its colour (dark variant), the base of the sub-ring tints
    // W19
    @Published var dots: [(key: Int, x: Double, y: Double)] = []   // the hint's dots (empty = no hint drawn)
    @Published var dotSize: Double = HintDotLayout.dotSize           // W20: settings hud.hint_dot_size
    @Published var dotPulse = false                                   // W20: the appear pulse's big phase
    @Published var hintRing = false                                   // W24: a proactive hint (ring around the dots)
    var glass = false                       // Liquid Glass (macOS 26+, no Increase Contrast / Reduce Transparency)
    // end W19
    @Published var contextCard: ShownContextCard?   // the glide direction's context card
}

/// the context card on screen: which card, its level (2 small, 3 big) and its centre (relative to the HUD's).
struct ShownContextCard: Equatable {
    let card: ContextCard
    let level: Int
    let x: Double
    let y: Double
}

private struct RingItem: Identifiable {
    let id: String
    let path: [Int]
    let option: HUDOption
    let point: CGPoint
    let width: Double
    let tint: Color?
}

private struct Item: Identifiable {
    let id: String
    let option: HUDOption
    let point: CGPoint
    let width: Double
}

struct HUDView: View {
    @ObservedObject var model: HUDModel
    /// (key, claude): claude = a ⌥-click (W14; right-click is caught by the hosting view)
    let onPress: (Int, Bool) -> Void
    /// W15: a click on a sub-ring capsule (its path)
    var onPressPath: ([Int]) -> Void = { _ in }
    /// a click on the card's « Open in Mail »
    var onOpenDraft: () -> Void = {}
    @Namespace private var glassNS          // W19: dots and capsules of one key morph into each other

    private var ringItems: [RingItem] {
        let c = CGPoint(x: Metrics.panelSize.width / 2, y: Metrics.panelSize.height / 2)
        return model.rings.flatMap { r in
            r.slots.compactMap { sl -> RingItem? in
                guard let o = r.option(sl.key) else { return nil }
                let path = r.parent + [sl.key]
                let tint = model.colorHex[r.parent[0]].flatMap { GlideTint.hex(base: $0, path: path) }   // W23: the root's colour
                return RingItem(id: path.map(String.init).joined(separator: ".") + "#" + o.label, path: path, option: o,
                                point: CGPoint(x: c.x + sl.x, y: c.y + sl.y), width: sl.width,
                                tint: tint.map { Color(nsColor: .hex($0)) })
            }
        }
    }

    private func onTrail(_ path: [Int]) -> Bool { model.trail.map { $0.starts(with: path) } ?? false }
    private var items: [Item] {
        let c = CGPoint(x: Metrics.panelSize.width / 2, y: Metrics.panelSize.height / 2)
        let byKey = Dictionary(model.options.map { ($0.key, $0) }, uniquingKeysWith: { a, _ in a })
        return Metrics.slots(model.options, model.positions, badges: model.badges).compactMap { sl in
            guard let o = byKey[sl.key] else { return nil }
            // W10: identity = place + label. A capsule never travels to another place: a new
            // option at a place crossfades in situ (no matchedGeometryEffect across slots).
            let id = "\(sl.position?.rawValue ?? (o.key == vocalOptionKey ? "vocal" : "wish"))#\(o.label)"
            return Item(id: id, option: o, point: CGPoint(x: c.x + sl.x, y: c.y + sl.y), width: sl.width)
        }
    }

    var body: some View {
        GlassGroup(on: model.glass) {        // W19: Liquid Glass container (morphs dots → capsules)
        ZStack {
            if !model.dots.isEmpty {         // W19: the hint (dots only)
                HintDotsView(dots: model.dots, colors: model.colors, glass: model.glass, ns: glassNS,
                             dotSize: model.dotSize, pulsed: model.dotPulse, ring: model.hintRing)
                    .transition(.opacity)
            }
            if let card = model.card {
                ResultCard(text: card, ok: model.cardOK, open: model.cardOpen ? onOpenDraft : nil)
                    .transition(.opacity)
                    .position(x: Metrics.panelSize.width / 2, y: Metrics.panelSize.height / 2)
            } else {
                ForEach(items) { item in
                    let k = item.option.key
                    OptionCapsule(option: item.option, contrast: model.increaseContrast,
                                  badge: model.badges[k] ?? "⌥\(k)", width: item.width,
                                  tint: model.colors[k], halo: model.halos.contains(k),
                                  reduceMotion: model.reduceMotion, hovered: model.hovered == k || onTrail([k]),
                                  ring: model.ringKey == k ? model.ringFill : (model.thinkingPath == [k] ? model.thinkingFill : nil),
                                  working: model.working == k,
                                  glass: model.glass, glassID: "opt\(k)", ns: glassNS)
                        .opacity(model.working == nil || model.working == k ? 1 : 0.35)
                        .transition(model.reduceMotion ? .opacity : .opacity.combined(with: .scale(scale: 0.8)))
                        .position(item.point)
                        .onTapGesture { onPress(k, NSEvent.modifierFlags.contains(.option)) }
                }
                // W15: the glide's sub-rings, around their parent capsule, in its colour family (lighter tints)
                ForEach(ringItems) { item in
                    OptionCapsule(option: item.option, contrast: model.increaseContrast, badge: "", width: item.width,
                                  tint: item.tint, reduceMotion: model.reduceMotion, hovered: onTrail(item.path),
                                  ring: model.thinkingPath == item.path ? model.thinkingFill : nil,
                                  glass: model.glass, ns: glassNS)
                        .transition(model.reduceMotion ? .opacity : .opacity.combined(with: .scale(scale: 0.3)))
                        .position(item.point)
                        .onTapGesture { onPressPath(item.path) }
                }
                if let cc = model.contextCard {   // what the direction is about (never a click target)
                    ContextCardView(card: cc.card, level: cc.level, tint: model.colors[cc.card.key],
                                    contrast: model.increaseContrast)
                        .transition(.opacity)
                        .position(x: Metrics.panelSize.width / 2 + cc.x, y: Metrics.panelSize.height / 2 + cc.y)
                        .allowsHitTesting(false)
                }
            }
        }
        .frame(width: Metrics.panelSize.width, height: Metrics.panelSize.height)
        }
        .opacity(model.visible || !model.dots.isEmpty ? 1 : 0)                  // W19: or a hint
        .scaleEffect(model.visible || !model.dots.isEmpty || model.reduceMotion ? 1 : 0.88)
        .environment(\.colorScheme, .dark)
    }
}

struct OptionCapsule: View {
    let option: HUDOption
    let contrast: Bool
    let badge: String
    let width: Double
    var tint: Color? = nil          // W11: the key's colour (nil = the wish and vocal options, neutral)
    var halo = false                // W11: the target is not on screen: pulse a ring around the icon
    var reduceMotion = false
    var hovered = false             // the pointer is over it: it is a click target
    var ring: Double? = nil         // W14: long-press progress (0...1) while its modifier is held
    var working = false             // W14: Claude works on it: a spinner instead of the badge
    // W19: Liquid Glass (the key's colour as the glass tint; interactive = it reacts under the pointer)
    var glass = false
    var glassID: String? = nil
    var ns: Namespace.ID? = nil

    var body: some View {
        HStack(spacing: 6) {             // content-sized (Metrics.width), centred when the 72-px minimum applies
            IconView(ref: HUDIcon(option.icon), tint: tint)
                .frame(width: Metrics.iconSize, height: Metrics.iconSize)
                .overlay { if halo, let tint { IconHalo(color: tint, reduceMotion: reduceMotion) } }
            let label = Metrics.shownLabel(option)
            if !label.isEmpty {
                Text(label)
                    .font(.system(size: Metrics.labelPt, weight: .medium))   // W19
                    .foregroundStyle(.primary)
                    .lineLimit(1).minimumScaleFactor(0.85).truncationMode(.tail)
            }
            if working {
                ProgressView().controlSize(.small).scaleEffect(0.7).frame(width: 18, height: 14)
            } else if !badge.isEmpty {
                BadgeLabel(badge: badge)   // W19: "2 ⌃" = bold number + secondary symbol
                    .font(.system(size: 9.5, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(tint != nil ? Color.black.opacity(0.85) : (contrast ? .primary : .secondary))
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(Capsule().fill(tint.map { $0.opacity(contrast ? 1 : 0.9) }
                                               ?? Color.primary.opacity(contrast ? 0.25 : 0.12)))
            }
        }
        .padding(.leading, 6).padding(.trailing, 6)
        .frame(width: width, height: Metrics.capsuleSize.height)
        .background {
            if glass, ns != nil { EmptyView() }   // W19: the glass below is the background
            else if contrast { Capsule().fill(Color.black) } else {
                ZStack {   // the blur, then the key's colour as a light wash (under the content)
                    CapsuleMaterial()
                    if let tint { Capsule().fill(tint.opacity(hovered ? 0.32 : 0.16)) }
                    else if hovered { Capsule().fill(Color.white.opacity(0.12)) }
                }
            }
        }
        .overlay(Capsule().strokeBorder(tint.map { $0.opacity(contrast || hovered ? 1 : 0.55) }
                                        ?? (contrast ? Color.white : Color.white.opacity(hovered ? 0.45 : 0.16)),
                                        lineWidth: contrast ? 2 : (tint != nil || hovered ? 1 : 0.5)))
        .overlay {
            if let ring {   // W14: fills while the modifier is held; full = the release runs the Claude variant
                Capsule().inset(by: -2).trim(from: 0, to: ring)
                    .stroke(tint ?? Color.white, style: StrokeStyle(lineWidth: ring >= 1 ? 3 : 2, lineCap: .round))
            }
        }
        .modifier(GlassOrShadow(glass: glass, tint: tint, hovered: hovered, glassID: glassID, ns: ns, contrast: contrast))   // W19
        .contentShape(Capsule())
    }
}

/// W14: the result of a Claude-assisted action (≤ 4 lines), at the HUD's centre for its ttl; Esc hides it.
/// W19: Liquid Glass behind a capsule (tinted with the key's colour, interactive) or, without glass, the pre-W19 shadow.
struct GlassOrShadow: ViewModifier {
    let glass: Bool
    let tint: Color?
    let hovered: Bool
    let glassID: String?
    let ns: Namespace.ID?
    let contrast: Bool
    func body(content: Content) -> some View {
        if glass, let ns {
            content.w19Glass(true, tint: tint.map { $0.opacity(hovered ? 0.45 : 0.28) }, interactive: true,
                             id: glassID, ns: ns, shape: Capsule())
        } else {
            content.shadow(color: .black.opacity(contrast ? 0 : 0.18), radius: 6, y: 2)
        }
    }
}

struct ResultCard: View {
    let text: String
    let ok: Bool
    /// « Open in Mail » under the text (a reply draft saved in place); nil = no button
    var open: (() -> Void)? = nil
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: ok ? "sparkles" : "exclamationmark.triangle")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(ok ? Color.white : Color.orange)
                Text(text)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(Color.white)
                    .lineLimit(4)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let open {
                Label("Open in Mail", systemImage: "envelope.open")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Color.white)
                    .padding(.horizontal, 9).padding(.vertical, 4)
                    .background(Capsule().fill(Color.white.opacity(0.16)))
                    .contentShape(Capsule())
                    .onTapGesture { open() }
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
        .frame(maxWidth: 320, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.black.opacity(0.8)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Color.white.opacity(0.18), lineWidth: 0.5))
        .shadow(color: .black.opacity(0.25), radius: 8, y: 3)
    }
}

/// the context card. Level 2: the « what » in one line. Level 3: + the summary (3-4 lines), the key
/// excerpt (verbatim, quoted) and why it matters (the wiki page + snippet). The result card's theme (dark, as the
/// capsules always are), the direction's colour as the accent; Increase Contrast = solid black + white border.
struct ContextCardView: View {
    let card: ContextCard
    let level: Int
    var tint: Color? = nil
    var contrast = false

    var body: some View {
        let size = card.size(level: level)
        let accent = tint ?? Color.white
        Group {
            if level >= 3 {
                VStack(alignment: .leading, spacing: 5) {
                    Text(card.what).font(.system(size: 14, weight: .semibold)).lineLimit(1).truncationMode(.tail)
                    ForEach(Array(card.summary.enumerated()), id: \.offset) { _, line in
                        Text(line).font(.system(size: 12.5)).foregroundStyle(Color.white.opacity(0.9))
                            .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    }
                    if card.summary.isEmpty, card.pending {
                        Text("Preparing the summary…").font(.system(size: 12.5).italic()).foregroundStyle(Color.white.opacity(0.6))
                    }
                    if let ex = card.excerpt {
                        Text("“\(ex)”").font(.system(size: 12).italic()).foregroundStyle(Color.white.opacity(0.75))
                            .lineLimit(4).fixedSize(horizontal: false, vertical: true).padding(.top, 2)
                    }
                    if let why = card.why {
                        HStack(alignment: .top, spacing: 5) {
                            Image(systemName: "books.vertical").font(.system(size: 11, weight: .semibold)).foregroundStyle(accent)
                            Text(why.snippet.map { "\(why.pageName) — \($0)" } ?? why.pageName)
                                .font(.system(size: 11.5)).foregroundStyle(Color.white.opacity(0.8))
                                .lineLimit(3).fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(.top, 2)
                    }
                }
                .padding(.horizontal, 14).padding(.vertical, 8)
                .frame(width: size.width, alignment: .leading)
            } else {
                HStack(spacing: 6) {
                    Circle().fill(accent).frame(width: 6, height: 6)
                    Text(card.what).font(.system(size: 11, weight: .medium)).lineLimit(1).truncationMode(.tail)
                }
                .padding(.horizontal, 10)
                .frame(width: size.width, height: size.height, alignment: .leading)
            }
        }
        .foregroundStyle(Color.white)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.black.opacity(contrast ? 1 : 0.8)))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
            .strokeBorder(contrast ? Color.white : accent.opacity(0.55), lineWidth: contrast ? 1.5 : 0.75))
        .shadow(color: .black.opacity(contrast ? 0 : 0.25), radius: 8, y: 3)
    }
}

/// W11: the halo fallback (target app not on this desk, Dock icon not readable): a ring pulsing in
/// the key's colour around the option's icon. Static under Reduce Motion. Exists only while drawn.
/// (Core Animation layer: no SwiftUI @State, the macro plugin is missing from the CLT toolchain.)
struct IconHalo: NSViewRepresentable {
    let color: Color
    let reduceMotion: Bool

    /// Keeps the ring on the icon's bounds whenever AppKit lays the view out.
    final class RingView: NSView {
        override func layout() {
            super.layout()
            guard let ring = layer?.sublayers?.first as? CAShapeLayer else { return }
            ring.frame = bounds
            ring.path = CGPath(ellipseIn: bounds.insetBy(dx: -4, dy: -4), transform: nil)
        }
    }

    func makeNSView(context: Context) -> NSView {
        let v = RingView()
        v.wantsLayer = true
        v.layer?.masksToBounds = false
        let ring = CAShapeLayer()
        let c = NSColor(color).cgColor
        ring.strokeColor = c; ring.fillColor = nil; ring.lineWidth = 2
        ring.shadowColor = c; ring.shadowOpacity = 0.9; ring.shadowRadius = 4; ring.shadowOffset = .zero
        v.layer?.addSublayer(ring)
        if reduceMotion { ring.opacity = 0.8 } else {
            ring.opacity = 0.3
            let pulse = CABasicAnimation(keyPath: "opacity")
            pulse.fromValue = 0.3; pulse.toValue = 0.9
            pulse.duration = 0.6; pulse.autoreverses = true; pulse.repeatCount = .infinity
            pulse.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            ring.add(pulse, forKey: "pulse")
        }
        return v
    }

    func updateNSView(_ v: NSView, context: Context) { v.needsLayout = true }

    static func dismantleNSView(_ v: NSView, coordinator: ()) {
        v.layer?.sublayers?.forEach { $0.removeAllAnimations(); $0.removeFromSuperlayer() }
    }
}

/// NSVisualEffectView with the HUD material, masked to a capsule (maskImage is the
/// reliable way to round a behind-window blur; a SwiftUI clipShape does not clip it).
struct CapsuleMaterial: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let v = NSVisualEffectView()
        v.material = .hudWindow
        v.blendingMode = .behindWindow
        v.state = .active
        v.maskImage = CapsuleMaterial.mask
        return v
    }
    func updateNSView(_ v: NSVisualEffectView, context: Context) {}

    static let mask: NSImage = {
        let r = Metrics.capsuleSize.height / 2
        let img = NSImage(size: NSSize(width: 2 * r + 1, height: 2 * r), flipped: false) { rect in
            NSColor.black.setFill()
            NSBezierPath(roundedRect: rect, xRadius: r, yRadius: r).fill()
            return true
        }
        img.capInsets = NSEdgeInsets(top: r, left: r, bottom: r, right: r)
        img.resizingMode = .stretch
        return img
    }()
}

struct IconView: View {
    let ref: HUDIcon
    var tint: Color? = nil
    var body: some View {
        switch ref {
        case .initial(let c):          // W12 agent picker: the initial in a disc of the key's colour
            ZStack {
                Circle().fill(tint ?? Color.primary.opacity(0.3))
                Text(c).font(.system(size: 9.5, weight: .bold, design: .rounded)).foregroundStyle(Color.black.opacity(0.85))   // W19: 9.5 pt in the 16-pt disc
            }
        case .app(let path):
            Image(nsImage: IconCache.icon(path)).resizable().interpolation(.high).scaledToFit()
        case .sf(let name):
            Image(systemName: name).resizable().scaledToFit().padding(3).foregroundStyle(.primary)
        case .none:
            Circle().fill(Color.primary.opacity(0.2)).padding(5)
        }
    }
}

enum IconCache {
    private static var cache: [String: NSImage] = [:]
    static func icon(_ path: String) -> NSImage {
        if let i = cache[path] { return i }
        let i = NSWorkspace.shared.icon(forFile: path)   // real app icon (generic icon if missing)
        cache[path] = i
        return i
    }
}
