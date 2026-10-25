import CoreGraphics
import CoreText
import Foundation
import ImageIO
import UniformTypeIdentifiers
import Vision

/// W17: on-device OCR. Vision `VNRecognizeTextRequest` (accurate, language correction, fr + en), no
/// network: the pixels never leave the Mac. The text is returned to the caller only.
public enum OCR {
    public enum Failure: Error, Equatable { case unreadable, vision(String) }

    public static let languages = ["fr-FR", "en-US"]
    public static let maxPixels = 40_000_000     // a 5K screen ×2 is ~29 M: refuse anything absurd

    /// The image at `url` (png, jpeg, heic, tiff…) as a CGImage.
    public static func load(_ url: URL) throws -> CGImage {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let img = CGImageSourceCreateImageAtIndex(src, 0, nil),
              img.width * img.height <= maxPixels else { throw Failure.unreadable }
        return img
    }

    /// Recognised lines, top to bottom (Vision's reading order), each the best candidate.
    public static func recognize(_ image: CGImage, fast: Bool = false) throws -> [String] {
        let req = VNRecognizeTextRequest()
        req.recognitionLevel = fast ? .fast : .accurate
        req.usesLanguageCorrection = !fast
        req.recognitionLanguages = languages
        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        do { try handler.perform([req]) } catch { throw Failure.vision(String(describing: error)) }
        return (req.results ?? []).compactMap { $0.topCandidates(1).first?.string }
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    public static func recognize(url: URL, fast: Bool = false) throws -> String {
        try recognize(load(url), fast: fast).joined(separator: "\n")
    }

    /// Tests and `oi-ocr --render`: `text` drawn black on white (one line per "\n") into a PNG at `url`.
    public static func renderPNG(_ text: String, to url: URL, fontSize: CGFloat = 36) throws {
        let lines = text.components(separatedBy: "\n")
        let width = 1200, lineH = Int(fontSize * 1.6), height = max(1, lines.count) * lineH + 80
        guard let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { throw Failure.unreadable }
        ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
        let font = CTFontCreateWithName("Helvetica" as CFString, fontSize, nil)
        for (i, line) in lines.enumerated() {
            let attr = NSAttributedString(string: line, attributes: [
                NSAttributedString.Key(kCTFontAttributeName as String): font,
                NSAttributedString.Key(kCTForegroundColorAttributeName as String): CGColor(red: 0, green: 0, blue: 0, alpha: 1)])
            let ct = CTLineCreateWithAttributedString(attr)
            ctx.textPosition = CGPoint(x: 40, y: CGFloat(height - 40 - (i + 1) * lineH) + fontSize * 0.4)
            CTLineDraw(ct, ctx)
        }
        guard let img = ctx.makeImage(),
              let dst = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil) else {
            throw Failure.unreadable
        }
        CGImageDestinationAddImage(dst, img, nil)
        guard CGImageDestinationFinalize(dst) else { throw Failure.unreadable }
    }
}
