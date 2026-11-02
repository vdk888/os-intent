import Carbon.HIToolbox
import HUDCore

/// Global hotkeys via Carbon RegisterEventHotKey: no TCC permission needed, and the key is
/// consumed (it does not also type into the front app).
/// - the HUD option/dismiss keys are TEMPORARY: registered on show, `unregisterAll()` on every hide;
/// - the wish key (and the summon key when it is a chord, W10) are PERMANENT: registered while the
///   daemon runs (`setPermanent`), and never touched by `unregisterAll()`.
final class HotKeys {
    static let escapeID: UInt32 = dismissHotkeyID
    private static weak var current: HotKeys?
    private static let signature = OSType(0x4F49_4844) // 'OIHD'
    private var refs: [EventHotKeyRef] = []
    private var permanent: [UInt32: EventHotKeyRef] = [:]   // id -> ref (wish 200, summon chord 300)
    private var handler: EventHandlerRef?
    var onPress: ((UInt32) -> Void)?

    init() {
        HotKeys.current = self
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ -> OSStatus in
            var hk = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                              nil, MemoryLayout<EventHotKeyID>.size, nil, &hk)
            let id = hk.id
            DispatchQueue.main.async { HotKeys.current?.onPress?(id) }
            return noErr
        }, 1, &spec, nil, &handler)
    }

    /// Registers the hotkeys of `plan` (see HUDCore.hotkeyPlan: settings.json `hotkeys`,
    /// default ⌥1-⌥4 + Esc; virtual key codes = physical key positions).
    func register(_ plan: [HotkeyRegistration]) {
        unregisterAll()
        for r in plan {
            if let ref = add(r) { refs.append(ref) }
        }
    }

    /// (Re-)installs the permanent hotkey `id`; nil removes it. Returns true when registered.
    @discardableResult
    func setPermanent(_ r: HotkeyRegistration?, id: UInt32) -> Bool {
        if let p = permanent.removeValue(forKey: id) { UnregisterEventHotKey(p) }
        guard let r, let ref = add(r) else { return false }
        permanent[id] = ref
        return true
    }

    private func add(_ r: HotkeyRegistration) -> EventHotKeyRef? {
        var ref: EventHotKeyRef?
        let st = RegisterEventHotKey(r.code, r.mods, EventHotKeyID(signature: HotKeys.signature, id: r.id),
                                     GetApplicationEventTarget(), 0, &ref)
        if st == noErr, let ref { return ref }
        log("RegisterEventHotKey(code \(r.code), id \(r.id)) failed: \(st)")
        return nil
    }

    /// Removes the temporary HUD keys only (the wish key stays).
    func unregisterAll() {
        for r in refs { UnregisterEventHotKey(r) }
        refs.removeAll()
    }

    var isRegistered: Bool { !refs.isEmpty }
    func isPermanentRegistered(_ id: UInt32) -> Bool { permanent[id] != nil }
}
