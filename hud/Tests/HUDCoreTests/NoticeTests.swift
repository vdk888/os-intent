import Foundation
import Testing
@testable import HUDCore

// oi live says a failed screenshot forward in one line, on the toast the vocal / page failures use.
@Suite struct NoticeTests {
    private func line(_ s: String) -> Data { Data(s.utf8) }

    @Test func noticeDecodesToOneLine() throws {
        #expect(try decodeCommand(line(#"{"cmd":"notice","text":"Not sent to Atlas: copy failed"}"#))
                == .notice(text: "Not sent to Atlas: copy failed"))
        #expect(try decodeCommand(line(#"{"cmd":"notice","text":"  a\nb  "}"#)) == .notice(text: "a b"))
    }

    @Test func emptyOrLongNoticeIsAnError() {
        #expect(throws: CommandError.badNotice) { try decodeCommand(line(#"{"cmd":"notice"}"#)) }
        #expect(throws: CommandError.badNotice) { try decodeCommand(line(#"{"cmd":"notice","text":"  "}"#)) }
        let long = String(repeating: "x", count: maxNoticeChars + 1)
        #expect(throws: CommandError.badNotice) { try decodeCommand(line("{\"cmd\":\"notice\",\"text\":\"\(long)\"}")) }
    }
}
