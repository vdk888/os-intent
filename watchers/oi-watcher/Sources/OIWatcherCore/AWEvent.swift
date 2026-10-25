import Foundation

/// A JSON scalar for event `data` (string, int or null — all three sources need no more).
public enum JSONScalar: Equatable, Sendable, Encodable {
    case string(String)
    case int(Int)
    case null

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .int(let i): try c.encode(i)
        case .null: try c.encodeNil()
        }
    }
}

/// One ActivityWatch event: {"timestamp", "duration", "data"}.
public struct AWEvent: Equatable, Sendable, Encodable {
    public var timestamp: Date
    public var duration: Double
    public var data: [String: JSONScalar]

    public init(timestamp: Date, duration: Double = 0, data: [String: JSONScalar]) {
        self.timestamp = timestamp
        self.duration = duration
        self.data = data
    }

    enum CodingKeys: String, CodingKey { case timestamp, duration, data }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(AWEvent.isoTimestamp(timestamp), forKey: .timestamp)
        try c.encode(duration, forKey: .duration)
        try c.encode(data, forKey: .data)
    }

    /// ISO-8601 UTC with milliseconds, e.g. 2026-10-20T10:03:56.123Z.
    public static func isoTimestamp(_ date: Date) -> String {
        date.formatted(Date.ISO8601FormatStyle(includingFractionalSeconds: true, timeZone: .gmt))
    }

    /// Body for POST /api/0/buckets/<id>/events (a JSON array). Sorted keys, unescaped slashes.
    public static func encodeBatch(_ events: [AWEvent]) throws -> Data {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try enc.encode(events)
    }

    // MARK: constructors for the three sources

    public static func space(at t: Date, index: Int?, delta: Int) -> AWEvent {
        AWEvent(timestamp: t, data: ["space": index.map { .int($0) } ?? .null, "delta": .int(delta)])
    }

    public static func chord(at t: Date, _ chord: String) -> AWEvent {
        AWEvent(timestamp: t, data: ["chord": .string(chord)])
    }

    public static func safari(at t: Date, _ tab: SafariTab) -> AWEvent {
        AWEvent(timestamp: t, data: ["url": .string(tab.url), "title": .string(tab.title)])
    }
}
