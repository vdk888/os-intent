import Foundation
import Testing
@testable import OCRCore

@Suite struct OCRTests {
    func tmp(_ name: String) -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("oi-ocr-\(UUID().uuidString)-\(name)")
    }

    @Test func readsAGeneratedPNG() throws {
        let url = tmp("t.png")
        defer { try? FileManager.default.removeItem(at: url) }
        try OCR.renderPNG("Facture 4217 en retard\nDeploy failed: 502 Bad Gateway", to: url)
        let t0 = Date()
        let text = try OCR.recognize(url: url)
        let ms = Int(Date().timeIntervalSince(t0) * 1000)
        print("oi-ocr accurate: \(ms) ms")      // the latency only, never the text in real use
        #expect(text.contains("4217"))
        #expect(text.lowercased().contains("retard"))
        #expect(text.contains("502"))
        #expect(text.split(separator: "\n").count == 2)
    }

    @Test func fastModeAlsoReads() throws {
        let url = tmp("f.png")
        defer { try? FileManager.default.removeItem(at: url) }
        try OCR.renderPNG("HELLO OCR 42", to: url, fontSize: 48)
        #expect(try OCR.recognize(url: url, fast: true).contains("42"))
    }

    @Test func blankImageGivesNoText() throws {
        let url = tmp("b.png")
        defer { try? FileManager.default.removeItem(at: url) }
        try OCR.renderPNG("", to: url)
        #expect(try OCR.recognize(url: url) == "")
    }

    @Test func unreadableFileThrows() {
        let url = tmp("x.png")
        try? Data("not an image".utf8).write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }
        #expect(throws: OCR.Failure.unreadable) { try OCR.load(url) }
    }
}
