import OISettings

/// One Carbon hotkey registration: virtual key code (physical position), Carbon modifier mask, id.
public struct HotkeyRegistration: Equatable, Sendable {
    public var code: UInt32
    public var mods: UInt32
    public var id: UInt32
}

public let dismissHotkeyID: UInt32 = 100

/// What to arm for one `show`:
/// - `regs`: Carbon hotkeys — the chord options (ids 1-4), dismiss (id 100) and, W12, a chord vocal key (id 400);
/// - `taps`: bare modifier taps → option key (W9), served by the passive event tap; W11
///   also `wish_visible` (default ⇧) → `wishOptionKey`: the wish option by one key while visible.
/// When `tapsAvailable` is false (the event tap could not be created: Input Monitoring missing)
/// every tap slot n falls back to the Carbon chord ⌥n, unless ⌥n is already taken by another action.
/// `wish` is NOT part of this plan: the daemon registers it permanently (Wish.swift `wishRegistration`).
/// An invalid set (unknown key, collision, bare typing key, tap on dismiss/wish) falls back to the
/// defaults as a whole so the HUD always stays answerable; `fellBack` says so.
public func hotkeyPlan(optionKeys: [Int], hotkeys: HotkeySet, tapsAvailable: Bool = true)
    -> (regs: [HotkeyRegistration], taps: [TapModifier: Int], fellBack: Bool) {
    let problems = hotkeys.problems()
    let set = problems.isEmpty ? hotkeys : HotkeySet.defaults
    // every chord in the set (wish included): a fallback ⌥n must not shadow one of them
    let taken = Set(HotkeySet.actions.compactMap { a -> String? in
        guard let h = set[a.key], !h.isTap, let c = h.keyCode else { return nil }
        return "\(h.carbonModifiers)/\(c)"
    })
    var regs: [HotkeyRegistration] = []
    var taps: [TapModifier: Int] = [:]
    for k in optionKeys {
        guard let h = set.option(k) else { continue }
        if let tap = h.tap {
            if tapsAvailable { taps[tap] = k; continue }
            let fb = HotkeySet.legacyChord(k)
            guard let code = fb.keyCode, !taken.contains("\(fb.carbonModifiers)/\(code)") else { continue }
            regs.append(HotkeyRegistration(code: UInt32(code), mods: fb.carbonModifiers, id: UInt32(k)))
        } else if let code = h.keyCode {
            regs.append(HotkeyRegistration(code: UInt32(code), mods: h.carbonModifiers, id: UInt32(k)))
        }
    }
    if tapsAvailable, let t = set.wishVisible.tap, taps[t] == nil { taps[t] = wishOptionKey }
    // W12: the vocal key as a chord (a set tap is matched on the option-tap monitor's sets instead)
    if !set.vocal.isTap, let code = set.vocal.keyCode {
        regs.append(HotkeyRegistration(code: UInt32(code), mods: set.vocal.carbonModifiers, id: vocalHotkeyID))
    }
    if let code = set.dismiss.keyCode {
        regs.append(HotkeyRegistration(code: UInt32(code), mods: set.dismiss.carbonModifiers, id: dismissHotkeyID))
    }
    return (regs, taps, !problems.isEmpty)
}
