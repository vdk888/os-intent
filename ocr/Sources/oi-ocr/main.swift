import Foundation
import OCRCore

// oi-ocr (W17). Prints the recognised text on stdout and nothing else (never a log of the text).
let usage = """
usage: oi-ocr [--fast] <image>          recognised text on stdout (exit 2: unreadable image, 3: Vision error)
       oi-ocr --render "<text>" <out.png>   draw a test PNG (tests)
"""

var args = Array(CommandLine.arguments.dropFirst())
if args.first == "--render", args.count == 3 {
    do { try OCR.renderPNG(args[1], to: URL(fileURLWithPath: args[2])); exit(0) } catch { exit(2) }
}
let fast = args.first == "--fast"
if fast { args.removeFirst() }
guard args.count == 1, !args[0].hasPrefix("-") else { print(usage); exit(64) }
do {
    let text = try OCR.recognize(url: URL(fileURLWithPath: args[0]), fast: fast)
    FileHandle.standardOutput.write(Data((text.isEmpty ? "" : text + "\n").utf8))
    exit(0)
} catch OCR.Failure.unreadable {
    exit(2)
} catch {
    exit(3)
}
