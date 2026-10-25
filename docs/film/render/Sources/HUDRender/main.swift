import AppKit
import SwiftUI
import HUDCore
import OISettings

// Offscreen stills of the real HUD views (HUDView / OptionCapsule / HintDotsView) over a FAKE desktop.
// Every name, mail, page and agent below is invented for the demo. Usage: hud-render <out dir> [--glass]

let args = CommandLine.arguments
let outDir = URL(fileURLWithPath: args.count > 1 ? args[1] : "captures", isDirectory: true)
let useGlass = args.contains("--glass")
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)          // no Dock icon, no menu bar, never frontmost
app.appearance = NSAppearance(named: .darkAqua)

let canvas = CGSize(width: 1440, height: 900)
let pointer = CGPoint(x: 820, y: 470)          // where the HUD opens (the "mouse")

// ---- the same model set-up as HUDController.present(), with the default settings ---------------------------

func makeModel(_ options: [HUDOption], withWishAndVocal: Bool = true) -> HUDModel {
    let s = OISettings()
    let m = HUDModel()
    let opts = withWishAndVocal ? optionsWithWishAndVocal(options) : options
    m.glass = useGlass
    var badges: [Int: String] = [wishOptionKey: s.hotkeys.wishVisible.display,
                                 vocalOptionKey: VocalArm.display(s.hotkeys)]
    for k in 1...maxOptions {
        guard let h = s.hotkeys.option(k) else { continue }
        badges[k] = optionBadge(key: k, symbol: h.display)
    }
    m.badges = badges
    m.positions = FixedSlots.positions(hotkeys: s.hotkeys, slots: s.hud.slots)
    var colors: [Int: Color] = [:]
    for o in opts where (1...maxOptions).contains(o.key) {
        guard let c = OptionColors.color(forOption: o.key, positions: m.positions, hud: s.hud) else { continue }
        colors[o.key] = Color(nsColor: .hex(c.dark))
        m.colorHex[o.key] = c.dark
    }
    m.colors = colors
    m.options = opts
    m.visible = true
    return m
}

/// The W15 sub-ring around option `key`, placed by the HUD's own SubRingLayout.
func openSubRing(_ m: HUDModel, key: Int, _ children: [HUDOption]) {
    let top = Metrics.slots(m.options, m.positions, badges: m.badges)
    guard let parent = top.first(where: { $0.key == key }) else { return }
    let hw = Double(Metrics.panelSize.width / 2), hh = Double(Metrics.panelSize.height / 2)
    let avoid = top + [HUDSlot(key: Int.min, x: 0, y: 0, width: 48, height: 48, position: nil)]
    let placed = SubRingLayout.place(parent: parent, origin: (0, 0), keys: children.map(\.key),
                                     widths: children.map { Metrics.width($0, badge: "") },
                                     height: Metrics.capsuleSize.height, avoid: avoid,
                                     bounds: (-hw + 4, -hh + 4, hw - 4, hh - 4))
    let pairs = zip(children, placed).compactMap { o, s in s.map { (o, $0) } }
    m.rings = [SubRing(parent: [key], options: pairs.map(\.0), slots: pairs.map(\.1))]
}

extension NSColor {
    static func hex(_ s: String) -> NSColor {
        guard let c = HUDColor.rgb(s) else { return .white }
        return NSColor(srgbRed: c.r, green: c.g, blue: c.b, alpha: 1)
    }
}

// ---- a fake desktop ---------------------------------------------------------------------------------------

struct Wallpaper: View {
    var body: some View {
        ZStack {
            LinearGradient(colors: [Color(red: 0.05, green: 0.07, blue: 0.16), Color(red: 0.16, green: 0.10, blue: 0.30),
                                    Color(red: 0.03, green: 0.20, blue: 0.30)], startPoint: .topLeading, endPoint: .bottomTrailing)
            RadialGradient(colors: [Color(red: 0.45, green: 0.30, blue: 0.85).opacity(0.45), .clear],
                           center: UnitPoint(x: 0.75, y: 0.25), startRadius: 10, endRadius: 650)
            RadialGradient(colors: [Color(red: 0.10, green: 0.65, blue: 0.75).opacity(0.35), .clear],
                           center: UnitPoint(x: 0.15, y: 0.85), startRadius: 10, endRadius: 600)
        }
    }
}

struct MenuBarStrip: View {
    let app: String
    var body: some View {
        HStack(spacing: 18) {
            Image(systemName: "apple.logo").font(.system(size: 13, weight: .semibold))
            Text(app).font(.system(size: 13, weight: .bold))
            ForEach(["File", "Edit", "View", "Window", "Help"], id: \.self) { Text($0).font(.system(size: 13)) }
            Spacer()
            Image(systemName: "circle.grid.cross").font(.system(size: 12))   // the OS-intent menu bar item
            Image(systemName: "wifi"); Image(systemName: "battery.75percent")
            Text("Tue 10:42").font(.system(size: 13))
        }
        .foregroundStyle(.white.opacity(0.92))
        .padding(.horizontal, 16)
        .frame(height: 26)
        .background(.black.opacity(0.35))
    }
}

struct FakeWindow<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Circle().fill(Color(red: 1, green: 0.37, blue: 0.34)).frame(width: 12, height: 12)
                Circle().fill(Color(red: 1, green: 0.74, blue: 0.18)).frame(width: 12, height: 12)
                Circle().fill(Color(red: 0.16, green: 0.79, blue: 0.25)).frame(width: 12, height: 12)
                Spacer()
                Text(title).font(.system(size: 13, weight: .semibold)).foregroundStyle(.white.opacity(0.8))
                Spacer()
            }
            .padding(.horizontal, 14).frame(height: 38)
            .background(Color(white: 0.17))
            content
        }
        .background(Color(white: 0.12))
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(.white.opacity(0.12), lineWidth: 1))
        .shadow(color: .black.opacity(0.5), radius: 30, y: 12)
    }
}

struct MailRow: View {
    let from: String, subject: String, preview: String, time: String
    var selected = false
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack { Text(from).font(.system(size: 13, weight: .semibold)); Spacer(); Text(time).font(.system(size: 11)).opacity(0.6) }
            Text(subject).font(.system(size: 12, weight: .medium))
            Text(preview).font(.system(size: 11)).opacity(0.6).lineLimit(1)
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 14).padding(.vertical, 9)
        .background(selected ? Color.accentColor.opacity(0.55) : .clear)
    }
}

struct FakeMail: View {
    var body: some View {
        FakeWindow(title: "Inbox — 3 unread") {
            HStack(spacing: 0) {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(["Inbox", "Flagged", "Drafts", "Sent", "Archive"], id: \.self) {
                        Label($0, systemImage: $0 == "Inbox" ? "tray" : "folder").font(.system(size: 12)).foregroundStyle(.white.opacity(0.8))
                    }
                    Spacer()
                }
                .padding(14).frame(width: 150).background(Color(white: 0.15))
                VStack(spacing: 0) {
                    MailRow(from: "Sam Rivera", subject: "Thursday review: can we move it?", preview: "Hi, something came up on Thursday, could we…", time: "10:31", selected: true)
                    MailRow(from: "Dana Park", subject: "Q4 numbers (draft)", preview: "Here is the first cut of the Q4 numbers…", time: "09:58")
                    MailRow(from: "Northwind Billing", subject: "Invoice #1042", preview: "Your invoice is available…", time: "09:12")
                    MailRow(from: "Pat Lee", subject: "Coffee next week?", preview: "Back in town on Monday…", time: "Yesterday")
                    Spacer()
                }
                .frame(width: 330).background(Color(white: 0.11))
                VStack(alignment: .leading, spacing: 10) {
                    Text("Thursday review: can we move it?").font(.system(size: 18, weight: .bold))
                    Text("Sam Rivera  ·  to me  ·  10:31").font(.system(size: 12)).opacity(0.6)
                    Divider().overlay(.white.opacity(0.15))
                    Text("Hi,\n\nSomething came up on Thursday. Could we move the review to Friday, 10:00 or 14:00? I will bring the updated deck.\n\nThanks,\nSam")
                        .font(.system(size: 13)).lineSpacing(3)
                    Spacer()
                }
                .foregroundStyle(.white).padding(22).frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

struct FakeSafari: View {
    var body: some View {
        FakeWindow(title: "github.com/acme/rocket/pull/42") {
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 8) {
                    Image(systemName: "arrow.triangle.pull").foregroundStyle(.green)
                    Text("Faster boot sequence #42").font(.system(size: 20, weight: .bold))
                }
                Text("octocat wants to merge 3 commits into main from fast-boot").font(.system(size: 12)).opacity(0.65)
                RoundedRectangle(cornerRadius: 8).fill(Color(white: 0.16)).frame(height: 120)
                    .overlay(alignment: .topLeading) {
                        VStack(alignment: .leading, spacing: 8) {
                            Label("All checks have passed", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                            Label("This branch has no conflicts with the base branch", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                            Text("Squash and merge").font(.system(size: 13, weight: .semibold))
                                .padding(.horizontal, 14).padding(.vertical, 7)
                                .background(Color(red: 0.14, green: 0.53, blue: 0.24), in: RoundedRectangle(cornerRadius: 6))
                        }.font(.system(size: 12)).padding(14)
                    }
                Spacer()
            }
            .foregroundStyle(.white).padding(24).frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

struct FakeTelegram: View {
    var body: some View {
        FakeWindow(title: "Telegram") {
            HStack(spacing: 0) {
                VStack(spacing: 0) {
                    MailRow(from: "Atlas", subject: "PR #42 is green, merge?", preview: "Checks passed on fast-boot…", time: "10:40", selected: true)
                    MailRow(from: "Nova", subject: "Need your go on the Friday plan", preview: "Two options for the review…", time: "10:22")
                    MailRow(from: "Orion", subject: "Q4 numbers ready", preview: "First cut attached…", time: "09:50")
                    Spacer()
                }
                .frame(width: 330).background(Color(white: 0.11))
                VStack(alignment: .leading, spacing: 10) {
                    Text("Atlas").font(.system(size: 18, weight: .bold))
                    Text("bot  ·  2 unread").font(.system(size: 12)).opacity(0.6)
                    Divider().overlay(.white.opacity(0.15))
                    Text("PR #42 (Faster boot sequence) is green: all checks passed, no conflicts.\n\nMerge it now, or wait for Orion's review?")
                        .font(.system(size: 13)).lineSpacing(3)
                    Spacer()
                }
                .foregroundStyle(.white).padding(22).frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

struct Cursor: View {
    var body: some View {
        Image(systemName: "cursorarrow").font(.system(size: 22)).foregroundStyle(.white)
            .shadow(color: .black.opacity(0.8), radius: 2)
    }
}

struct Scene<Window: View>: View {
    let app: String
    let model: HUDModel
    @ViewBuilder let window: Window
    var caption: String? = nil
    var body: some View {
        ZStack(alignment: .topLeading) {
            Wallpaper()
            VStack(spacing: 0) { MenuBarStrip(app: app); Spacer() }
            window.frame(width: 1180, height: 700).position(x: 720, y: 470)
            HUDView(model: model, onPress: { _, _ in })
                .position(x: pointer.x, y: pointer.y)
            Cursor().position(x: pointer.x + 6, y: pointer.y + 9)
            if let caption {
                Text(caption).font(.system(size: 15, weight: .semibold)).foregroundStyle(.white)
                    .padding(.horizontal, 14).padding(.vertical, 8)
                    .background(.black.opacity(0.55), in: Capsule())
                    .position(x: canvas.width / 2, y: canvas.height - 34)
            }
        }
        .frame(width: canvas.width, height: canvas.height)
        .environment(\.colorScheme, .dark)
    }
}

// ---- render ---------------------------------------------------------------------------------------------------

@MainActor func render<V: View>(_ view: V, _ name: String) {
    let host = NSHostingView(rootView: view)
    host.frame = CGRect(origin: .zero, size: canvas)
    let win = NSWindow(contentRect: host.frame, styleMask: .borderless, backing: .buffered, defer: false)
    win.contentView = host                    // never ordered front: nothing appears on screen
    host.layoutSubtreeIfNeeded()
    RunLoop.main.run(until: Date().addingTimeInterval(0.4))   // let SwiftUI settle (no animation in flight)
    guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { print("no bitmap: \(name)"); return }
    host.cacheDisplay(in: host.bounds, to: rep)
    let url = outDir.appendingPathComponent(name)
    try? rep.representation(using: .png, properties: [:])?.write(to: url)
    print("wrote \(url.path) \(rep.pixelsWide)x\(rep.pixelsHigh)")
}

// Mail context: the predicted next steps (fn 1 left, ⌃ 2 top, ⌥ 3 right, ⌘ 4 bottom).
let mailOptions = [
    HUDOption(key: 1, label: "Open Calendar", icon: "sf:calendar"),
    HUDOption(key: 2, label: "Reply to Sam", icon: "sf:arrowshape.turn.up.left", claude: true),
    HUDOption(key: 3, label: "Forward to Orion", icon: "sf:arrowshape.turn.up.right", claude: true),
    HUDOption(key: 4, label: "Desk 2", icon: "sf:rectangle.split.3x1"),
]

MainActor.assumeIsolated {
    // 1. hint dots: the HUD whispers before it speaks
    let hint = makeModel(mailOptions)
    hint.options = []; hint.visible = false
    hint.dots = HintDotLayout.offsets(keys: [1, 2, 3, 4], positions: hint.positions).map { (key: $0.key, x: $0.x, y: $0.y) }
    render(Scene(app: "Mail", model: hint, window: { FakeMail() }, caption: "1 · Hint dots: four next steps, one key each"), "01-hint-dots.png")

    // 2. the full cross
    render(Scene(app: "Mail", model: makeModel(mailOptions), window: { FakeMail() },
                 caption: "2 · ⌥⌘: the cross. fn 1 · ⌃ 2 · ⌥ 3 · ⌘ 4"), "02-cross.png")

    // 3. glide: hold ⌥⌘, rest on "Reply to Sam", the follow-ups bloom
    let glide = makeModel(mailOptions)
    openSubRing(glide, key: 2, [
        HUDOption(key: 1, label: "Draft: Friday 10:00 ok", icon: "sf:sparkles", claude: true),
        HUDOption(key: 2, label: "Draft: Friday 14:00", icon: "sf:sparkles", claude: true),
        HUDOption(key: 3, label: "Ask Orion for deck", icon: "initial:O"),
    ])
    glide.trail = [2, 1]
    render(Scene(app: "Mail", model: glide, window: { FakeMail() }, caption: "3 · Glide: hold, rest, release. Three steps in one move"), "03-glide.png")

    // 4. long-press: the Claude variant fills its ring
    let lp = makeModel(mailOptions)
    lp.ringKey = 2; lp.ringFill = 0.72
    render(Scene(app: "Mail", model: lp, window: { FakeMail() }, caption: "4 · Hold ⌃: Claude drafts the reply (never sent)"), "04-long-press.png")

    // 5. result card
    let card = makeModel(mailOptions)
    card.card = "Sam asks to move Thursday's review.\nProposes Friday 10:00 or 14:00.\nDraft ready in Mail: accept 10:00."
    render(Scene(app: "Mail", model: card, window: { FakeMail() }, caption: "5 · The result, in place. You still press Send"), "05-result-card.png")

    // 6. vocal picker: ⌃⌥, then one key per agent
    let picker = makeModel(VocalPicker.options(page: 0, hotkeys: .defaults) + [HUDOption(key: vocalOptionKey, label: vocalOptionLabel, icon: vocalOptionIcon)],
                           withWishAndVocal: false)
    render(Scene(app: "Mail", model: picker, window: { FakeMail() }, caption: "6 · ⌃⌥: send a voice note. One key picks the agent"), "06-vocal-picker.png")

    // 7. page action (the outward gate asks before an irreversible press)
    let pageOpts = [
        HUDOption(key: 2, label: "Squash & merge", icon: "sf:arrow.triangle.merge", target: "button:squash-and-merge"),
        HUDOption(key: 3, label: "Summarise PR", icon: "sf:sparkles", claude: true),
        HUDOption(key: 4, label: "Ask Atlas: release?", icon: "initial:A"),
        HUDOption(key: 1, label: "Back to Mail", icon: "sf:arrow.uturn.backward"),
    ]
    let page = makeModel(pageOpts)
    render(Scene(app: "Safari", model: page, window: { FakeSafari() }, caption: "7 · Page actions: the button you were about to look for"), "07-page-action.png")

    // 8. a proactive hint: the state changed (a new client mail), the dots wear a thin ring
    let ring = makeModel(mailOptions)
    ring.options = []; ring.visible = false; ring.hintRing = true
    ring.dots = HintDotLayout.offsets(keys: [1, 2, 3, 4], positions: ring.positions).map { (key: $0.key, x: $0.x, y: $0.y) }
    render(Scene(app: "Mail", model: ring, window: { FakeMail() }, caption: "8 · Something changed: a ringed hint. ⌥⌘ opens what it is about"), "08-proactive-ring.png")

    // 9. level-3 context card: hold ⌥⌘ and rest on a direction, a card says what it is about and why it matters
    let ctx = makeModel(mailOptions)
    openSubRing(ctx, key: 2, [
        HUDOption(key: 1, label: "Draft: Friday 10:00 ok", icon: "sf:sparkles", claude: true),
        HUDOption(key: 2, label: "Draft: Friday 14:00", icon: "sf:sparkles", claude: true),
    ])
    ctx.trail = [2]
    let card3 = ContextCard(key: 2, what: "Sam Rivera · Thursday review: can we move it? · 10:31",
                            summary: ["Sam can't make Thursday's review.", "Proposes Friday 10:00 or 14:00.",
                                      "Will bring the updated deck."],
                            excerpt: "Could we move the review to Friday, 10:00 or 14:00?",
                            why: ContextWhy(page: "clients/northwind.md", snippet: "Northwind — active client, quarterly review with Sam Rivera"))
    let top = Metrics.slots(ctx.options, ctx.positions, badges: ctx.badges)
    if let anchor = top.first(where: { $0.key == 2 }) {
        let hw = Double(Metrics.panelSize.width / 2), hh = Double(Metrics.panelSize.height / 2)
        let avoid = top.filter { $0.key != 2 } + ctx.rings.flatMap(\.slots) + [HUDSlot(key: Int.min, x: 0, y: 0, width: 48, height: 48, position: nil)]
        if let p = ContextCardLayout.place(anchor: anchor, size: card3.size(level: 3), avoid: avoid, bounds: (-hw + 4, -hh + 4, hw - 4, hh - 4)) {
            ctx.contextCard = ShownContextCard(card: card3, level: 3, x: p.x, y: p.y)
        }
    }
    render(Scene(app: "Mail", model: ctx, window: { FakeMail() }, caption: "9 · Hold and rest: the card says what it is, and why it matters"), "09-context-card.png")

    // 10. an agent is waiting on Telegram: the app direction lists the live chats, one click each
    let tgOpts = [
        HUDOption(key: 1, label: "Telegram", icon: "sf:paperplane"),
        HUDOption(key: 2, label: "Reply to Atlas", icon: "initial:A"),
        HUDOption(key: 3, label: "Open PR #42", icon: "sf:arrow.triangle.pull"),
        HUDOption(key: 4, label: "Desk 2", icon: "sf:rectangle.split.3x1"),
    ]
    let tg = makeModel(tgOpts)
    let atlasCard = ContextCard(key: 1, what: "Atlas · 2 messages · since 10:40",
                                summary: ["PR #42 is green: checks passed, no conflicts.", "Asks: merge now or wait for Orion's review?"])
    openSubRing(tg, key: 1, [
        HUDOption(key: 1, label: "Atlas · 2", icon: "initial:A", card: atlasCard),
        HUDOption(key: 2, label: "Nova · 1", icon: "initial:N"),
    ])
    tg.trail = [1, 1]
    if let r = tg.rings.first, let anchor = r.slot(1) {
        let hw = Double(Metrics.panelSize.width / 2), hh = Double(Metrics.panelSize.height / 2)
        let avoid = Metrics.slots(tg.options, tg.positions, badges: tg.badges) + r.slots.filter { $0 != anchor }
            + [HUDSlot(key: Int.min, x: 0, y: 0, width: 48, height: 48, position: nil)]
        if let p = ContextCardLayout.place(anchor: anchor, size: atlasCard.size(level: 2), avoid: avoid, bounds: (-hw + 4, -hh + 4, hw - 4, hh - 4)) {
            tg.contextCard = ShownContextCard(card: atlasCard, level: 2, x: p.x, y: p.y)
        }
    }
    render(Scene(app: "Telegram", model: tg, window: { FakeTelegram() }, caption: "10 · An agent is waiting: its chat, one click away"), "10-agent-waiting.png")

    // 0. clean hero (no caption)
    render(Scene(app: "Mail", model: makeModel(mailOptions), window: { FakeMail() }), "00-hero.png")
}
