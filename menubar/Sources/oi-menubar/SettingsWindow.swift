import AppKit
import SwiftUI
import MenubarCore
import OISettings

// W26: the Settings… window. SwiftUI, a sidebar of sections; each change is validated live and, when the
// whole form is valid, written ~0.3 s later through OISettings (SettingsStore.update: atomic, unknown keys kept).
// The daemons pick it up by themselves: oi-hud on its next show (mtime cache), oi live on its next tick (~2 s),
// oi-watcher within 5 s. Liquid Glass on macOS 26 (section cards), with the HUD's fallback (material) on older
// systems and when Increase Contrast / Reduce Transparency is on.

/// Same rule as oi-hud's `liquidGlassAvailable()` (HintDots.swift).
@MainActor func settingsGlassAvailable() -> Bool {
    let ws = NSWorkspace.shared
    guard !ws.accessibilityDisplayShouldIncreaseContrast, !ws.accessibilityDisplayShouldReduceTransparency else { return false }
    if #available(macOS 26, *) { return true }
    return false
}

@MainActor
final class SettingsModel: ObservableObject {
    @Published var form: SettingsForm { didSet { if form != oldValue { scheduleSave() } } }
    @Published var section: SettingsForm.Section? = .hud
    @Published private(set) var savedAt: Date?
    @Published private(set) var writeError: String?
    @Published var domainsText: String
    @Published var denylistText: String
    private(set) var settings: OISettings
    /// The form as last loaded / saved: only fields that differ from it are written (W26 review: a menu Pause
    /// toggled while the window is open must survive the next edit here).
    private var baseline: SettingsForm
    private(set) var live: LiveStatus?
    private var pending: DispatchWorkItem?
    let url: URL
    let openHotkeys: () -> Void

    init(url: URL = SettingsStore.defaultURL, openHotkeys: @escaping () -> Void) {
        self.url = url; self.openHotkeys = openHotkeys
        let s = SettingsStore.load(url)
        settings = s
        form = SettingsForm(s)
        baseline = SettingsForm(s)
        domainsText = SettingsForm(s).localOnlyDomains.joined(separator: "\n")
        denylistText = s.denylistApps.joined(separator: "\n")
        live = LiveStatus.load(dataDir: url.deletingLastPathComponent())
    }

    /// Re-reads the file (on each present) so the window shows what the daemons see.
    func reload() {
        pending?.cancel(); pending = nil
        settings = SettingsStore.load(url)
        live = LiveStatus.load(dataDir: url.deletingLastPathComponent())
        let f = SettingsForm(settings)
        baseline = f
        if f != form { form = f; pending?.cancel(); pending = nil }
        domainsText = f.localOnlyDomains.joined(separator: "\n")
        denylistText = f.denylistApps.joined(separator: "\n")
    }

    var problems: [SettingsForm.Section: [String]] { form.problems() }

    private func scheduleSave() {
        pending?.cancel()
        guard form.isValid else { return }       // live validation: nothing is written while a value is out of range
        let w = DispatchWorkItem { [weak self] in MainActor.assumeIsolated { self?.save() } }
        pending = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: w)
    }

    func save() {
        pending = nil
        let f = form, base = baseline
        do {
            settings = try SettingsStore.update(at: url) { f.apply(to: &$0, since: base) }   // merged into the current file
            baseline = f
            savedAt = Date(); writeError = nil
            log("settings window: saved")
        } catch {
            writeError = "\(error)"
            log("settings window: save failed: \(error)")
        }
    }

    func reset(_ s: SettingsForm.Section) {
        form.reset(s)
        if s == .claude { domainsText = "" }
        if s == .privacy { denylistText = form.denylistApps.joined(separator: "\n") }
    }

    func setDomains(_ text: String) {
        domainsText = text
        form.localOnlyDomains = text.split(whereSeparator: { $0 == "\n" || $0 == "," }).map(String.init)
    }

    func setDenylist(_ text: String) {
        denylistText = text
        form.denylistApps = text.split(separator: "\n").map(String.init)
    }
}

// MARK: views

struct SettingsRootView: View {
    @ObservedObject var model: SettingsModel
    let glass = settingsGlassAvailable()

    var body: some View {
        NavigationSplitView {
            List(SettingsForm.Section.allCases, selection: $model.section) { s in
                Label(s.title, systemImage: s.symbol)
                    .badge(model.problems[s] == nil ? 0 : 1)
                    .tag(s)
            }
            .navigationSplitViewColumnWidth(min: 170, ideal: 180, max: 220)
        } detail: {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let s = model.section { SectionCard(model: model, section: s, glass: glass) }
                    footer
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(minWidth: 680, minHeight: 470)
    }

    @ViewBuilder private var footer: some View {
        HStack(spacing: 6) {
            if let e = model.writeError {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
                Text("Not saved: \(e)").foregroundStyle(.red)
            } else if !model.form.isValid {
                Image(systemName: "exclamationmark.circle").foregroundStyle(.orange)
                Text("Not saved until every value is valid.")
            } else if let t = model.savedAt {
                Image(systemName: "checkmark.circle").foregroundStyle(.green)
                Text("Saved \(t.formatted(date: .omitted, time: .standard)) — the HUD applies it on its next show (≤ 5 s).")
            } else {
                Text("Changes are saved as you make them.")
            }
        }
        .font(.callout).foregroundStyle(.secondary)
    }
}

/// One section: title, Reset, the controls, its validation messages. Glass card on macOS 26.
struct SectionCard: View {
    @ObservedObject var model: SettingsModel
    let section: SettingsForm.Section
    let glass: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label(section.title, systemImage: section.symbol).font(.title2.weight(.semibold))
                Spacer()
                if section.resettable {
                    Button("Reset") { model.reset(section) }.help("Put this section back to its defaults")
                }
            }
            Divider()
            content
            ForEach(model.problems[section] ?? [], id: \.self) { p in
                Label(p, systemImage: "exclamationmark.circle.fill").foregroundStyle(.red).font(.callout)
            }
        }
        .padding(18)
        .modifier(CardBackground(glass: glass))
    }

    private var f: Binding<SettingsForm> { $model.form }

    @ViewBuilder private var content: some View {
        switch section {
        case .hud:
            Picker("Automatic shows", selection: f.autoMode) {
                Text("Hints (dots)").tag("hint"); Text("Full cross").tag("full"); Text("Off").tag("off")
            }.pickerStyle(.segmented)
            Note("Hints: coloured dots, ⌥⌘ develops them. Off: nothing automatic; ⌥⌘ still summons.")
            NumberRow("Hint dot size", value: f.hintDotSize, range: SettingsForm.hintDotSizeRange, unit: "pt", step: 1)
        case .timings:
            NumberRow("Hint duration", value: f.hintTTLms, range: SettingsForm.hintTTLRange, unit: "ms", step: 250)
            NumberRow("Hint cooldown", value: f.hintCooldownS, range: SettingsForm.hintCooldownRange, unit: "s", step: 1)
            NumberRow("Full-cross duration", value: f.fullTTLms, range: SettingsForm.fullTTLRange, unit: "ms", step: 500)
        case .pointer:
            Toggle("Hide the HUD on fast pointer travel", isOn: f.pointerHide)
            NumberRow("Grace after a show", value: f.pointerGraceMs, range: SettingsForm.pointerGraceRange, unit: "ms", step: 250)
                .disabled(!model.form.pointerHide)
            NumberRow("Speed that hides it", value: f.pointerFastPxS, range: SettingsForm.pointerSpeedRange, unit: "px/s", step: 100)
                .disabled(!model.form.pointerHide)
        case .colors:
            Note("One colour per key, stable across shows. Light = on light backgrounds, Dark = Dark mode and the HUD capsules.")
            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 10) {
                GridRow { Text("Key").foregroundStyle(.secondary); Text("Light").foregroundStyle(.secondary); Text("Dark").foregroundStyle(.secondary); Text("") }
                ForEach(keyOrder, id: \.self) { m in
                    GridRow {
                        Text("\(number(m))  \(m.symbol)").font(.body.monospaced())
                        ColorPicker("", selection: colorBinding(m, dark: false), supportsOpacity: false).labelsHidden()
                        ColorPicker("", selection: colorBinding(m, dark: true), supportsOpacity: false).labelsHidden()
                        Button("Reset") { model.form.resetColor(m) }.controlSize(.small)
                    }
                }
            }
        case .fillOrder:
            Note("Which key gets the most likely option, then the next. An option's number is its key, never its rank.")
            ForEach(0..<4, id: \.self) { rank in
                Picker("Rank \(rank + 1)\(rank == 0 ? " (most likely)" : "")", selection: Binding(
                    get: { model.form.fillOrder.indices.contains(rank) ? model.form.fillOrder[rank] : .ctrl },
                    set: { model.form.setFill(rank: rank, to: $0) })) {
                    ForEach(keyOrder, id: \.self) { m in Text("\(m.symbol)  (\(number(m)))").tag(m) }
                }.frame(maxWidth: 320)
            }
        case .keys:
            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
                ForEach(SettingsForm.keyMap(model.settings.hotkeys), id: \.label) { row in
                    GridRow { Text(row.label).foregroundStyle(.secondary); Text(row.value).font(.body.monospaced()) }
                }
            }
            Button("Hotkeys…") { model.openHotkeys() }
        case .claude:
            Toggle("Claude variant (long-press an option)", isOn: f.claudeEnabled)
            Toggle("Enrich options with Claude", isOn: f.claudeEnrich)
            LabeledContent("Model") { Text(SettingsForm.claudeModel).foregroundStyle(.secondary) }
            Text("Client domains kept away from Claude (one per line)").font(.callout)
            TextEditor(text: Binding(get: { model.domainsText }, set: { model.setDomains($0) }))
                .font(.body.monospaced()).frame(height: 110)
                .scrollContentBackground(.hidden).padding(6)
                .background(RoundedRectangle(cornerRadius: 8).fill(.background.opacity(0.5)))
            Note("Mail from these domains (and pages on them, subdomains included) is never sent to Claude.")
        case .screenshots:
            Toggle("Offer to send new screenshots", isOn: f.shotsEnabled)
            Toggle("Also clipboard images", isOn: f.shotsClipboard).disabled(!model.form.shotsEnabled)
            Picker("Default contact", selection: f.fallbackContact) {
                Text("None").tag("")
                ForEach(SettingsForm.recipients(model.settings), id: \.slug) { r in Text(r.name).tag(r.slug) }
                if !model.form.fallbackContact.isEmpty,
                   !SettingsForm.recipients(model.settings).contains(where: { $0.slug == model.form.fallbackContact }) {
                    Text(model.form.fallbackContact).tag(model.form.fallbackContact)
                }
            }.frame(maxWidth: 320)
        case .proactive:   // W24
            Note("Hints when something changes without you acting (ring around the dots). Each source is read-only and off until you turn it on.")
            ForEach(SettingsForm.proactiveSources, id: \.self) { k in
                Toggle(Self.sourceLabel(k), isOn: Binding(get: { model.form.proactiveSources[k] ?? false },
                                                         set: { model.form.proactiveSources[k] = $0 }))
            }
            NumberRow("Hint threshold", value: f.proactiveMinPct, range: SettingsForm.proactiveMinPctRange, unit: "%", step: 5)
            NumberRow("At most one hint every", value: f.proactiveIntervalMin, range: SettingsForm.proactiveIntervalRange, unit: "min", step: 1)
            LabeledContent("Quiet hours") {
                HStack(spacing: 6) {
                    TextField("22:00", text: f.quietStart).frame(width: 64)
                    Text("→")
                    TextField("08:00", text: f.quietEnd).frame(width: 64)
                    Text("Paris time · both empty = none").foregroundStyle(.secondary).font(.callout)
                }
            }
            Toggle("Respect Focus (when readable)", isOn: f.respectFocus)
            Note("Client mail and dashboard outages skip the threshold. Folders, dashboard URL and feeds: settings.json proactive.")
        case .vocal:
            Note("Who ⌃⌥ can send a vocal to (read-only; settings vocal_recipients).")
            ForEach(SettingsForm.recipients(model.settings), id: \.slug) { r in
                LabeledContent(r.name) { Text(r.slug).font(.body.monospaced()).foregroundStyle(.secondary) }
            }
        case .laya:
            let l = SettingsForm.laya(model.settings, live: model.live)
            LabeledContent("Predictor") { Text(l.predictor).font(.body.monospaced()) }
            LabeledContent("Checkpoint") { Text(l.checkpoint).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle) }
            LabeledContent("Last training") { Text(l.trained) }
            Note("Read-only: the predictor changes only through `oi laya-promote` once the pass bar is met.")
        case .privacy:
            Toggle("Pause logging", isOn: f.paused)
            Toggle("Ask before anything leaves the Mac (outward gate)", isOn: f.outwardGate)
            Text("Denylisted apps (one per line)").font(.callout)
            TextEditor(text: Binding(get: { model.denylistText }, set: { model.setDenylist($0) }))
                .font(.body.monospaced()).frame(height: 110)
                .scrollContentBackground(.hidden).padding(6)
                .background(RoundedRectangle(cornerRadius: 8).fill(.background.opacity(0.5)))
            Note("These apps give activate:<App> tokens only — no titles, URLs or ⌘ chords.")
        }
    }

    static func sourceLabel(_ k: String) -> String {
        switch k {
        case "mail": "Mail (sender + subject of new mail)"
        case "agents": "Agent messages (Telegram chat titles)"
        case "files": "New files (Desktop, chosen folders)"
        case "cockpit": "Ops dashboard (outage, alerts)"
        case "metrics": "Metrics (local feed)"
        case "markets": "Markets (local feed)"
        case "decisions": "Dashboard decisions"
        case "calendar": "Upcoming meetings (Calendar)"
        case "prs": "Pull requests awaiting you"
        default: k
        }
    }

    /// fn ⌃ ⌥ ⌘ = keys 1 2 3 4.
    private var keyOrder: [TapModifier] { [.fn, .ctrl, .option, .cmd] }
    private func number(_ m: TapModifier) -> Int { (keyOrder.firstIndex(of: m) ?? 0) + 1 }

    private func colorBinding(_ m: TapModifier, dark: Bool) -> Binding<Color> {
        Binding(get: {
            let c = model.form.colors[m] ?? HUDColors.defaults[m]!
            return Color(nsColor: .fromHex(dark ? c.dark : c.light))
        }, set: { new in
            var c = model.form.colors[m] ?? HUDColors.defaults[m]!
            let hex = NSColor(new).hexString
            if dark { c.dark = hex } else { c.light = hex }
            model.form.colors[m] = c
        })
    }
}

/// An integer field with a stepper; out-of-range values stay typed and show a problem (never written).
struct NumberRow: View {
    let title: String
    @Binding var value: Int
    let range: ClosedRange<Int>
    let unit: String
    let step: Int
    init(_ title: String, value: Binding<Int>, range: ClosedRange<Int>, unit: String, step: Int) {
        self.title = title; _value = value; self.range = range; self.unit = unit; self.step = step
    }
    var body: some View {
        LabeledContent(title) {
            HStack(spacing: 6) {
                TextField("", value: $value, format: .number.grouping(.never))
                    .frame(width: 80).multilineTextAlignment(.trailing)
                    .foregroundStyle(range.contains(value) ? Color.primary : Color.red)
                Stepper("", value: $value, in: range, step: step).labelsHidden()
                Text("\(unit)  (\(range.lowerBound)–\(range.upperBound))").foregroundStyle(.secondary).font(.callout)
            }
        }
    }
}

struct Note: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View { Text(text).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
}

/// Liquid Glass card on macOS 26; material fallback elsewhere (or with Increase Contrast / Reduce Transparency).
struct CardBackground: ViewModifier {
    let glass: Bool
    func body(content: Content) -> some View {
        if #available(macOS 26, *), glass {
            content.glassEffect(.regular, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        } else {
            content.background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        }
    }
}

extension NSColor {
    static func fromHex(_ hex: String) -> NSColor {
        guard let c = HUDColor.rgb(hex) else { return .gray }
        return NSColor(srgbRed: c.r, green: c.g, blue: c.b, alpha: 1)
    }
    var hexString: String {
        let c = usingColorSpace(.sRGB) ?? self
        func b(_ v: CGFloat) -> Int { Int((min(max(v, 0), 1) * 255).rounded()) }
        return String(format: "#%02X%02X%02X", b(c.redComponent), b(c.greenComponent), b(c.blueComponent))
    }
}

@MainActor
final class SettingsWindowController: NSObject, NSWindowDelegate {
    let model: SettingsModel
    private let window: NSWindow

    init(url: URL = SettingsStore.defaultURL, openHotkeys: @escaping () -> Void) {
        model = SettingsModel(url: url, openHotkeys: openHotkeys)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 520),
                          styleMask: [.titled, .closable, .resizable, .miniaturizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        super.init()
        window.title = "OS-intent Settings"
        window.isReleasedWhenClosed = false
        window.toolbarStyle = .unified
        window.contentViewController = NSHostingController(rootView: SettingsRootView(model: model))
        window.setContentSize(NSSize(width: 760, height: 520))
        window.center()
        window.delegate = self
    }

    func present() {
        model.reload()
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    var windowNumber: Int { window.windowNumber }
    var nsWindow: NSWindow { window }

    /// A pending (debounced) change is written when the window closes.
    func windowWillClose(_ note: Notification) {
        if model.form.isValid, model.form != SettingsForm(SettingsStore.load(model.url)) { model.save() }
    }
}
