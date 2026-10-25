import Foundation
import OISettings

/// Fixed-slot layout (W10, replaces the W2-W9 radial arc): four places around the pointer,
/// one per option modifier, never re-flowed. Offsets are y-down (SwiftUI), `radius` from the centre.
public enum FixedSlots {
    public static func offset(_ p: SlotPosition, radius: Double) -> (x: Double, y: Double) {
        switch p {
        case .left: (-radius, 0)
        case .top: (0, -radius)
        case .right: (radius, 0)
        case .bottom: (0, radius)
        }
    }

    /// W11: the same four places with capsules of `capsuleWidth` — the left and right ones move
    /// out so their inner edge stays `gap` px from the centre (the pointer spot stays empty); top and
    /// bottom stay at `radius`. W10's 168-px capsules sat exactly there (100 − 84 = 16 px), so for them
    /// nothing moves; W11's 252-px capsules sit at ±142.
    public static func offset(_ p: SlotPosition, radius: Double, capsuleWidth: Double, gap: Double = 16) -> (x: Double, y: Double) {
        let side = max(radius, capsuleWidth / 2 + gap)
        switch p {
        case .left: return (-side, 0)
        case .right: return (side, 0)
        default: return offset(p, radius: radius)
        }
    }

    /// The wish option's own fixed place, below-right (≈ 4:30): under the bottom capsule, its left
    /// edge just right of centre, so it overlaps neither the bottom nor the right capsule nor the
    /// reserved 7:30 place.
    public static func wishOffset(radius: Double, capsuleHeight: Double, wishWidth: Double) -> (x: Double, y: Double) {
        (wishWidth / 2 + 8, radius + capsuleHeight + 16)
    }

    /// W12: the permanent "Send a vocal to…" option, the mirror of the wish place, below-left
    /// (≈ 7:30): its right edge just left of centre.
    public static func vocalOffset(radius: Double, capsuleHeight: Double, vocalWidth: Double) -> (x: Double, y: Double) {
        (-(vocalWidth / 2 + 8), radius + capsuleHeight + 16)
    }

    /// Option key (1-4) → its place. The modifier of option k is its tap in `hotkeys` (an invalid set
    /// counts as the defaults); a chord option takes the place of its default modifier (option 1 fn,
    /// 2 ⌃, 3 ⌥, 4 ⌘) when free, else the first free place. Places are unique and depend only on the
    /// key, never on how many options are shown: an absent option leaves its place empty.
    /// Default (the user 15:45): fn left, ⌃ top, ⌥ right, ⌘ bottom → since W21 (number = key) option 1 (fn) left,
    /// 2 (⌃) top, 3 (⌥) right, 4 (⌘) bottom: clockwise from the left, the numbers in keyboard order.
    public static func positions(hotkeys: HotkeySet, slots: [TapModifier: SlotPosition]) -> [Int: SlotPosition] {
        let set = hotkeys.problems().isEmpty ? hotkeys : HotkeySet.defaults
        let map = HUDSettings.valid(slots) ? slots : HUDSettings.defaultSlots
        var out: [Int: SlotPosition] = [:]
        var used = Set<SlotPosition>()
        for k in 1...maxOptions {                          // taps first: their place is theirs
            if let t = set.option(k)?.tap, let p = map[t], used.insert(p).inserted { out[k] = p }
        }
        for k in 1...maxOptions where out[k] == nil {     // chords: their default modifier's place if free
            if let d = HotkeySet.defaults.option(k)?.tap, let p = map[d], used.insert(p).inserted { out[k] = p }
        }
        for k in 1...maxOptions where out[k] == nil {     // ... else the first free place
            if let p = SlotPosition.allCases.first(where: { !used.contains($0) }) { out[k] = p; used.insert(p) }
        }
        return out
    }
}

/// The HUD is a cross centred on the pointer: the click target sits in the empty middle,
/// so it is never covered. While the pointer stays inside `leash` px of the
/// centre the HUD stays put (so the options can be reached); beyond it the centre is pulled
/// toward the pointer by `factor` of the excess per tick (a soft, damped follow).
public func leashStep(centre: (x: Double, y: Double), pointer: (x: Double, y: Double),
                      leash: Double, factor: Double) -> (x: Double, y: Double) {
    let dx = pointer.x - centre.x, dy = pointer.y - centre.y
    let dist = hypot(dx, dy)
    guard dist > leash, dist > 0 else { return centre }
    let k = (dist - leash) / dist * factor
    return (centre.x + dx * k, centre.y + dy * k)
}

/// Keep a centre so that a box of `halfSize` around it stays inside `screen`.
public func clampCentre(_ c: (x: Double, y: Double),
                        screen: (minX: Double, minY: Double, maxX: Double, maxY: Double),
                        halfSize: (w: Double, h: Double)) -> (x: Double, y: Double) {
    (min(max(c.x, screen.minX + halfSize.w), screen.maxX - halfSize.w),
     min(max(c.y, screen.minY + halfSize.h), screen.maxY - halfSize.h))
}
