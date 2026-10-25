import Foundation

// Mouse clicks on the HUD. Pure parts:
// the per-capsule hit test (the panel lets mouse events through everywhere else) and the cross's
// bounding box (inside it, fast pointer travel never hides the HUD: moving toward a capsule is expected).
// Coordinates are relative to the HUD centre, y-down (as HUDSlot).

/// The key of the capsule under `p`, by its real stadium shape (a rounded end is not a hit in its
/// corner), else nil.
public func capsuleHit(_ p: (x: Double, y: Double), slots: [HUDSlot]) -> Int? {
    for s in slots {
        let dx = abs(p.x - s.x), dy = abs(p.y - s.y), r = s.height / 2
        guard dx <= s.width / 2, dy <= r else { continue }
        let flat = max(s.width / 2 - r, 0)
        if dx <= flat || (dx - flat) * (dx - flat) + dy * dy <= r * r { return s.key }
    }
    return nil
}

/// The box around every capsule, grown by `margin` (nil when nothing is drawn).
public func crossBounds(_ slots: [HUDSlot], margin: Double) -> (minX: Double, minY: Double, maxX: Double, maxY: Double)? {
    guard !slots.isEmpty else { return nil }
    return (slots.map { $0.x - $0.width / 2 }.min()! - margin, slots.map { $0.y - $0.height / 2 }.min()! - margin,
            slots.map { $0.x + $0.width / 2 }.max()! + margin, slots.map { $0.y + $0.height / 2 }.max()! + margin)
}

/// Whether `p` is inside the cross (+ margin): the pointer-travel hide is off there.
public func insideCross(_ p: (x: Double, y: Double), slots: [HUDSlot], margin: Double = 24) -> Bool {
    guard let b = crossBounds(slots, margin: margin) else { return false }
    return p.x >= b.minX && p.x <= b.maxX && p.y >= b.minY && p.y <= b.maxY
}
