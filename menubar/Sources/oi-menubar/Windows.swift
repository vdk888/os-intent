import AppKit
import OISettings

@MainActor private func makeWindow(title: String, size: NSSize) -> NSWindow {
    let w = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                     styleMask: [.titled, .closable], backing: .buffered, defer: false)
    w.title = title
    w.isReleasedWhenClosed = false
    w.center()
    return w
}

@MainActor private func button(_ title: String, _ target: AnyObject, _ action: Selector) -> NSButton {
    NSButton(title: title, target: target, action: action)
}

/// Denylist editor: one app name per line (as shown in the app switcher). Saves `denylist_apps`.
/// A denylisted app yields `activate:<App>` tokens only (no title, url or ⌘-chord tokens);
/// the built-in Passwords / Keychain Access / 1Password always apply on top.
@MainActor
final class DenylistWindowController: NSObject {
    private let window = makeWindow(title: "OS-intent — Denylist", size: NSSize(width: 380, height: 340))
    private let text = NSTextView()
    private let onSave: () -> Void

    init(onSave: @escaping () -> Void) {
        self.onSave = onSave
        super.init()
        let help = NSTextField(wrappingLabelWithString: "One app per line. These apps produce activate:<App> tokens only — no titles, URLs or ⌘ chords. Applied on the next token rebuild.")
        help.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        text.isRichText = false
        text.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        text.isAutomaticTextReplacementEnabled = false
        text.isAutomaticSpellingCorrectionEnabled = false
        text.autoresizingMask = [.width]
        scroll.documentView = text
        let save = button("Save", self, #selector(saveAction)); save.keyEquivalent = "\r"
        let cancel = button("Cancel", self, #selector(cancelAction)); cancel.keyEquivalent = "\u{1b}"
        let buttons = NSStackView(views: [NSView(), cancel, save])
        let stack = NSStackView(views: [help, scroll, buttons])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 14, left: 14, bottom: 14, right: 14)
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = NSView()
        window.contentView!.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor),
            stack.topAnchor.constraint(equalTo: window.contentView!.topAnchor),
            stack.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor),
            scroll.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -28),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 200),
            buttons.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -28),
            help.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -28),
        ])
    }

    func present(_ apps: [String]) {
        text.string = apps.joined(separator: "\n")
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    @objc private func saveAction() {
        let apps = OISettings.cleanDenylist(text.string.split(separator: "\n").map(String.init))
        do { try SettingsStore.update { $0.denylistApps = apps } } catch { log("denylist save failed: \(error)") }
        log("denylist saved (\(apps.count) apps)")
        window.close()
        onSave()
    }

    @objc private func cancelAction() { window.close() }
}

/// Click, then press the combination (like the macOS Keyboard Shortcuts prefs). W11: in
/// `capturesTaps` mode it records a modifier TAP instead: press the modifiers together (⌥ and ⌘ for the
/// summon default), release them all, no other key; a key pressed meanwhile restarts the capture.
@MainActor
final class RecorderField: NSTextField {
    var hotkey: Hotkey { didSet { stringValue = hotkey.display } }
    var onChange: ((Hotkey) -> Void)?
    var capturesTaps = false
    private var tapUnion: [TapModifier] = []
    private var recording = false {
        didSet { tapUnion = []; stringValue = recording ? (capturesTaps ? "Press modifiers together…" : "Press keys…") : hotkey.display }
    }

    init(_ h: Hotkey) {
        hotkey = h
        super.init(frame: .zero)
        isEditable = false; isSelectable = false; isBezeled = true; bezelStyle = .roundedBezel
        alignment = .center
        stringValue = h.display
        widthAnchor.constraint(equalToConstant: 140).isActive = true
    }
    required init?(coder: NSCoder) { fatalError() }

    override var acceptsFirstResponder: Bool { true }
    override func mouseDown(with event: NSEvent) { guard isEnabled else { return }; window?.makeFirstResponder(self); recording = true }
    override func resignFirstResponder() -> Bool { recording = false; return super.resignFirstResponder() }

    override func keyDown(with event: NSEvent) {
        guard recording else { return super.keyDown(with: event) }
        if capturesTaps { tapUnion = []; stringValue = "Modifiers only…"; return }   // a chord is not a tap
        record(event)
    }
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        guard recording, window?.firstResponder === self else { return super.performKeyEquivalent(with: event) }
        if capturesTaps { tapUnion = []; stringValue = "Modifiers only…"; return true }
        record(event); return true
    }

    /// Tap capture: the union of modifiers held; recorded when they are all released.
    override func flagsChanged(with event: NSEvent) {
        guard recording, capturesTaps else { return super.flagsChanged(with: event) }
        let f = event.modifierFlags
        var now: [TapModifier] = []
        if f.contains(.control) { now.append(.ctrl) }
        if f.contains(.option) { now.append(.option) }
        if f.contains(.command) { now.append(.cmd) }
        if f.contains(.function) { now.append(.fn) }
        if f.contains(.shift) { now.append(.shift) }
        if now.isEmpty {
            guard !tapUnion.isEmpty else { return }
            hotkey = .tapSet(tapUnion)
            recording = false
            window?.makeFirstResponder(nil)
            onChange?(hotkey)
            return
        }
        for m in now where !tapUnion.contains(m) { tapUnion.append(m) }
        stringValue = Hotkey.tapSet(tapUnion).display + "…"
    }

    private func record(_ event: NSEvent) {
        let f = event.modifierFlags
        var mods: [String] = []
        if f.contains(.control) { mods.append("control") }
        if f.contains(.option) { mods.append("option") }
        if f.contains(.shift) { mods.append("shift") }
        if f.contains(.command) { mods.append("command") }
        hotkey = Hotkey.from(keyCode: Int(event.keyCode), modifiers: mods)
        recording = false
        window?.makeFirstResponder(nil)
        onChange?(hotkey)
    }
}

/// Hotkeys editor. Options 1-4: a segmented choice "⌃ tap | ⌥ tap | ⌘ tap | fn tap | chord" (W9:
/// a tap = the bare modifier pressed and released alone while the HUD is visible); "chord" enables the
/// recorder (click, press the combination). Wish while the HUD is up (W11): "⇧ tap | ⌃ tap | ⌥ tap | ⌘ tap |
/// fn tap" (default ⇧; tap only). Summon (W10, W11): "⌥⌘ tap | ⇧ tap | record tap |
/// chord" (default ⌥⌘ pressed together: shows the HUD with the current options at any time); "record tap"
/// captures any modifier set pressed together (click, press them, release). Dismiss and wish are chords
/// only. Validation (collisions, typing keys without modifier) blocks Save; "Reset to defaults" restores
/// ⌃ ⌥ ⌘ fn taps, Esc, ⌥`, ⌥⌘ summon, ⌃⌥ vocal. Vocal (W12): "⌃⌥ tap | record tap | chord".
@MainActor
final class HotkeysWindowController: NSObject {
    private let window = makeWindow(title: "OS-intent — HUD hotkeys", size: NSSize(width: 640, height: 520))
    private var fields: [String: RecorderField] = [:]
    private var modes: [String: NSSegmentedControl] = [:]
    private var lastChord: [String: Hotkey] = [:]
    private let problems = NSTextField(wrappingLabelWithString: "")
    private var saveButton: NSButton!
    private var current = HotkeySet.defaults

    override init() {
        super.init()
        let help = NSTextField(wrappingLabelWithString: "Options 1-4: a tap = press and release the modifier alone while the HUD is shown (taps need Input Monitoring for oi-hud; without it the HUD uses ⌥1-⌥4). Summon: press ⌥ and ⌘ together and release them (no other key) any time to show the HUD with the current options; record tap captures another modifier set. Wish while the HUD is up: tap ⇧ to open the wish panel (⌥` works any time). Send a vocal to… (W12): with the HUD up, press ⌃ and ⌥ together and release them: pick the agent with its key (⌃⌥ again = next page), speak, ⌃⌥ or Enter sends, Esc cancels (Speech Recognition + Microphone for oi-hud). Chord: click the field, then press the combination (physical key positions). The HUD reads these on its next show.")
        help.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        let grid = NSGridView()
        for (action, _) in HotkeySet.actions {
            let f = RecorderField(HotkeySet.chordFallback(action))
            f.onChange = { [weak self] h in
                guard let self else { return }
                if !h.isTap { self.lastChord[action] = h }
                self.current[action] = h; self.validate()
            }
            fields[action] = f
            var row: [NSView] = [NSTextField(labelWithString: HotkeySet.windowLabel(action))]   // W19/W21: "Option 2 (⌃ control)"
            let presets = HotkeySet.tapPresets(action)
            if !presets.isEmpty {
                let labels = presets.map { "\($0.display) tap" } + (HotkeySet.capturesTapSet(action) ? ["record tap"] : [])
                    + (HotkeySet.allowsChord(action) ? ["chord"] : [])
                let seg = NSSegmentedControl(labels: labels,
                                             trackingMode: .selectOne, target: self, action: #selector(modeChanged(_:)))
                seg.identifier = NSUserInterfaceItemIdentifier(action)
                modes[action] = seg
                row.append(seg)
            } else {
                row.append(NSView())
            }
            if !HotkeySet.allowsChord(action) { f.isHidden = true }   // wish_visible: a tap only
            row.append(f)
            grid.addRow(with: row)
        }
        grid.column(at: 0).xPlacement = .trailing
        grid.rowSpacing = 8
        problems.textColor = .systemRed
        problems.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        let reset = button("Reset to defaults", self, #selector(resetAction))
        let cancel = button("Cancel", self, #selector(cancelAction))
        saveButton = button("Save", self, #selector(saveAction))
        let buttons = NSStackView(views: [reset, NSView(), cancel, saveButton])
        let stack = NSStackView(views: [help, grid, problems, buttons])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 14, left: 14, bottom: 14, right: 14)
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = NSView()
        window.contentView!.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor),
            stack.topAnchor.constraint(equalTo: window.contentView!.topAnchor),
            stack.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor),
            help.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -28),
            problems.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -28),
            buttons.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -28),
        ])
    }

    func present(_ set: HotkeySet) {
        load(set)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    private func load(_ set: HotkeySet) {
        current = set
        lastChord = [:]
        for (action, _) in HotkeySet.actions { show(action) }
        validate()
    }

    /// Segment indexes of a row: the presets, then "record tap" (summon), then "chord".
    private func recordIndex(_ action: String) -> Int? {
        HotkeySet.capturesTapSet(action) ? HotkeySet.tapPresets(action).count : nil
    }
    private func chordIndex(_ action: String) -> Int {
        HotkeySet.allowsChord(action) ? HotkeySet.tapPresets(action).count + (HotkeySet.capturesTapSet(action) ? 1 : 0) : -1
    }

    /// Syncs one row's controls with `current[action]`.
    private func show(_ action: String) {
        guard let h = current[action], let f = fields[action] else { return }
        let presets = HotkeySet.tapPresets(action)
        if h.isTap {
            if let i = presets.firstIndex(of: h) {
                modes[action]?.selectedSegment = i
                f.capturesTaps = false
                f.hotkey = lastChord[action] ?? HotkeySet.chordFallback(action)
                f.isEnabled = false
            } else {                                 // a recorded tap (summon): shown in the field
                modes[action]?.selectedSegment = recordIndex(action) ?? 0
                f.capturesTaps = true
                f.hotkey = h
                f.isEnabled = true
            }
        } else {
            modes[action]?.selectedSegment = chordIndex(action)
            f.capturesTaps = false
            f.hotkey = h
            f.isEnabled = true
        }
    }

    @objc private func modeChanged(_ sender: NSSegmentedControl) {
        guard let action = sender.identifier?.rawValue, let f = fields[action] else { return }
        let i = sender.selectedSegment
        let presets = HotkeySet.tapPresets(action)
        if current[action]?.isTap == false { lastChord[action] = current[action] }
        if i == chordIndex(action) {
            current[action] = lastChord[action] ?? HotkeySet.chordFallback(action)   // ⌥n / ⌥⇧Space by default
        } else if i == recordIndex(action) {
            // keep the current tap until a new set is recorded (click the field, press, release)
            if current[action]?.isTap != true { current[action] = HotkeySet.defaultSummon }
            f.capturesTaps = true
            f.hotkey = current[action]!
            f.isEnabled = true
            validate()
            return
        } else if presets.indices.contains(i) {
            current[action] = presets[i]
        }
        show(action)
        validate()
    }

    private func validate() {
        let p = current.problems()
        problems.stringValue = p.joined(separator: "\n")
        saveButton.isEnabled = p.isEmpty
    }

    @objc private func resetAction() { load(.defaults) }
    @objc private func cancelAction() { window.close() }
    @objc private func saveAction() {
        guard current.problems().isEmpty else { return }
        let set = current
        do { try SettingsStore.update { $0.hotkeys = set } } catch { log("hotkeys save failed: \(error)") }
        log("hotkeys saved")
        window.close()
    }
}
