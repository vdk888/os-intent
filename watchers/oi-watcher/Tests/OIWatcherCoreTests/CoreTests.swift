import Foundation
import Testing
@testable import OIWatcherCore

// CGEventFlags raw bits
let CMD: UInt64 = 0x0010_0000, SHIFT: UInt64 = 0x0002_0000, OPT: UInt64 = 0x0008_0000, CTRL: UInt64 = 0x0004_0000

@Suite("Chord formatting")
struct ChordTests {
    @Test func commandLetter() {
        #expect(Chord.chord(keyCode: 8, cgEventFlags: CMD, layoutCharacter: "c") == "⌘C")
    }
    @Test func noCommandNeverAChord() {
        // Plain typing, shift-typing, ctrl/opt combos: never a chord.
        for flags in [UInt64(0), SHIFT, OPT, CTRL, SHIFT | OPT | CTRL] {
            #expect(Chord.chord(keyCode: 8, cgEventFlags: flags, layoutCharacter: "c") == nil)
            #expect(Chord.format(modifiers: ChordModifiers(cgEventFlags: flags), keyName: "C") == nil)
        }
    }
    @Test func modifierOrderMatchesContract() {
        #expect(Chord.chord(keyCode: 21, cgEventFlags: CMD | SHIFT, layoutCharacter: "'") == "⌘⇧4")
        #expect(Chord.chord(keyCode: 0, cgEventFlags: CMD | SHIFT | OPT | CTRL, layoutCharacter: "q") == "⌘⇧⌥⌃Q")
        #expect(Chord.chord(keyCode: 9, cgEventFlags: CMD | OPT, layoutCharacter: "v") == "⌘⌥V")
    }
    @Test func digitRowIgnoresAzertyBaseCharacter() {
        // AZERTY: keycode 18 base char is "&"; the token still says 1.
        #expect(Chord.keyName(keyCode: 18, layoutCharacter: "&") == "1")
        #expect(Chord.keyName(keyCode: 29, layoutCharacter: "à") == "0")
    }
    @Test func layoutCharacterWinsForLetters() {
        // AZERTY: keycode 12 (ANSI Q) produces "a".
        #expect(Chord.keyName(keyCode: 12, layoutCharacter: "a") == "A")
    }
    @Test func functionAndSpecialKeys() {
        #expect(Chord.chord(keyCode: 122, cgEventFlags: CMD, layoutCharacter: nil) == "⌘F1")
        #expect(Chord.chord(keyCode: 111, cgEventFlags: CMD, layoutCharacter: nil) == "⌘F12")
        #expect(Chord.chord(keyCode: 48, cgEventFlags: CMD, layoutCharacter: "\t") == "⌘TAB")
        #expect(Chord.chord(keyCode: 49, cgEventFlags: CMD, layoutCharacter: " ") == "⌘SPACE")
        #expect(Chord.chord(keyCode: 124, cgEventFlags: CMD, layoutCharacter: nil) == "⌘→")
        #expect(Chord.chord(keyCode: 36, cgEventFlags: CMD, layoutCharacter: "\r") == "⌘RETURN")
    }
    @Test func ansiFallbackAndUnknown() {
        #expect(Chord.keyName(keyCode: 8, layoutCharacter: nil) == "C")
        #expect(Chord.keyName(keyCode: 8, layoutCharacter: "\u{10}") == "C")  // control char → fallback
        #expect(Chord.keyName(keyCode: 200, layoutCharacter: nil) == nil)
        #expect(Chord.chord(keyCode: 200, cgEventFlags: CMD, layoutCharacter: nil) == nil)
    }
}

@Suite("Event JSON")
struct EventTests {
    let t = Date(timeIntervalSince1970: 1_792_490_636.125)  // 2026-10-20T10:03:56.125Z (exact in binary)

    func json(_ e: AWEvent) throws -> String {
        String(decoding: try AWEvent.encodeBatch([e]), as: UTF8.self)
    }
    @Test func timestampIsUTCWithMillis() {
        #expect(AWEvent.isoTimestamp(t) == "2026-10-20T10:03:56.125Z")
    }
    @Test func spaceEvent() throws {
        #expect(try json(.space(at: t, index: 3, delta: 1)) ==
            #"[{"data":{"delta":1,"space":3},"duration":0,"timestamp":"2026-10-20T10:03:56.125Z"}]"#)
        #expect(try json(.space(at: t, index: nil, delta: 0)) ==
            #"[{"data":{"delta":0,"space":null},"duration":0,"timestamp":"2026-10-20T10:03:56.125Z"}]"#)
    }
    @Test func chordEvent() throws {
        #expect(try json(.chord(at: t, "⌘C")) ==
            #"[{"data":{"chord":"⌘C"},"duration":0,"timestamp":"2026-10-20T10:03:56.125Z"}]"#)
    }
    @Test func safariEventKeepsSlashesUnescaped() throws {
        let tab = SafariTab(url: "https://example.org/a/b", title: "Ex \"quoted\"")
        #expect(try json(.safari(at: t, tab)) ==
            #"[{"data":{"title":"Ex \"quoted\"","url":"https://example.org/a/b"},"duration":0,"timestamp":"2026-10-20T10:03:56.125Z"}]"#)
    }
    @Test func roundTripsAsJSONArray() throws {
        let data = try AWEvent.encodeBatch([.chord(at: t, "⌘V"), .space(at: t, index: 1, delta: -1)])
        let arr = try #require(try JSONSerialization.jsonObject(with: data) as? [[String: Any]])
        #expect(arr.count == 2)
        #expect(arr[0]["duration"] as? Double == 0)
    }
}

@Suite("Bucket ids")
struct BucketTests {
    @Test func naming() {
        #expect(BucketKind.space.bucketID(hostname: "my-mac") == "oi-watcher-space_my-mac")
        #expect(BucketKind.key.bucketID(hostname: "h") == "oi-watcher-key_h")
        #expect(BucketKind.safari.bucketID(hostname: "h") == "oi-watcher-safari_h")
    }
    @Test func createBody() {
        #expect(Buckets.createBody(kind: .safari, hostname: "h") ==
                ["client": "oi-watcher", "type": "web.tab.current", "hostname": "h"])
        #expect(Set(BucketKind.allCases.map(\.awType)).count == 6)   // W16: + ui.click, W17: + shot, + file
    }
}

@Suite("Safari URL/title handling")
struct SafariTabTests {
    @Test func keepsSchemeHostPath() {
        let tab = SafariTab.normalise(rawURL: "https://GitHub.com/octocat/repo/pull/3?tab=files#diff", rawTitle: "  PR #3 ")
        #expect(tab == SafariTab(url: "https://github.com/octocat/repo/pull/3", title: "PR #3"))
    }
    @Test func dropsCredentialsQueryAndFragment() {
        let tab = SafariTab.normalise(rawURL: "https://user:pw@example.org:8443/login?token=SECRET#x", rawTitle: "Login")
        #expect(tab?.url == "https://example.org:8443/login")
        #expect(tab?.url.contains("SECRET") == false)
        #expect(tab?.url.contains("pw") == false)
    }
    @Test func emptyPathBecomesSlash() {
        #expect(SafariTab.normalise(rawURL: "http://example.org", rawTitle: nil)?.url == "http://example.org/")
    }
    @Test func nonWebAndEmptyAreSkipped() {
        for raw in [nil, "", "   ", "missing value", "about:blank", "favorites://", "file:///Users/you/a.pdf", "bookmarks://"] {
            #expect(SafariTab.normalise(rawURL: raw, rawTitle: "t") == nil)
        }
    }
    @Test func titleMissingOrLong() {
        #expect(SafariTab.normalise(rawURL: "https://a.b/", rawTitle: "missing value")?.title == "")
        let long = String(repeating: "x", count: 1000)
        #expect(SafariTab.normalise(rawURL: "https://a.b/", rawTitle: long)?.title.count == SafariTab.maxTitleLength)
    }
}

@Suite("Spaces")
struct SpacesTests {
    @Test func indexPerDisplay() {
        let displays: [[UInt64]] = [[1, 5, 9], [12, 14]]
        #expect(Spaces.index(of: 9, inDisplays: displays) == 3)
        #expect(Spaces.index(of: 14, inDisplays: displays) == 2)
        #expect(Spaces.index(of: 99, inDisplays: displays) == nil)
    }
    @Test func delta() {
        #expect(Spaces.delta(previous: 1, current: 2) == 1)
        #expect(Spaces.delta(previous: 3, current: 1) == -1)
        #expect(Spaces.delta(previous: 2, current: 2) == 0)
        #expect(Spaces.delta(previous: nil, current: 2) == 0)
        #expect(Spaces.delta(previous: 2, current: nil) == 0)
    }
    @Test func parsesManagedDisplaySpacesShape() {
        let managed: [[String: Any]] = [
            ["Display Identifier": "Main", "Spaces": [["id64": 1, "type": 0], ["ManagedSpaceID": 7, "type": 4]]],
            ["Display Identifier": "X", "Spaces": [["id64": NSNumber(value: 20)]]],
            ["Display Identifier": "Empty"],
        ]
        #expect(Spaces.displaySpaceIDs(from: managed) == [[1, 7], [20], []])
    }
}

// MARK: - W17 screenshots

@Suite struct ShotTests {
    @Test func screenshotNames() {
        #expect(ShotFiles.isScreenshot("Screenshot 2026-10-20 at 18.13.03.png"))
        #expect(ShotFiles.isScreenshot("Capture d’écran 2026-10-20 à 18.13.03.png"))
        #expect(ShotFiles.isScreenshot("Capture d'écran 2026-10-20 à 18.13.03.PNG"))
        // decomposed é (as some file systems return it) still matches
        #expect(ShotFiles.isScreenshot("Capture d’e\u{301}cran 2026-10-20 à 18.13.03.png"))
        #expect(ShotFiles.isScreenshot("Screenshot 2026-10-20 at 18.13.03.jpg"))
        #expect(!ShotFiles.isScreenshot(".Screenshot 2026-10-20 at 18.13.03.png"))     // still being written
        #expect(!ShotFiles.isScreenshot("Screenshot 2026-10-20 at 18.13.03.mov"))      // a recording
        #expect(!ShotFiles.isScreenshot("Screenshot"))
        #expect(!ShotFiles.isScreenshot("notes.png"))
        #expect(ShotFiles.isScreenshot("Shot 2026.png", customName: "Shot"))
        #expect(!ShotFiles.isScreenshot("Shot 2026.png"))
    }

    @Test func locationDefaultAndOverrides() {
        let dirs: Set<String> = ["/Users/you/Pictures/Shots", "/tmp/x"]
        let isDir: (String) -> Bool = { dirs.contains($0) }
        #expect(ShotFiles.location(defaultsValue: nil, home: "/Users/you", isDirectory: isDir) == "/Users/you/Desktop")
        #expect(ShotFiles.location(defaultsValue: "~/Pictures/Shots\n", home: "/Users/you", isDirectory: isDir) == "/Users/you/Pictures/Shots")
        #expect(ShotFiles.location(defaultsValue: "/gone", home: "/Users/you", isDirectory: isDir) == "/Users/you/Desktop")
        #expect(ShotFiles.location(defaultsValue: "/Users/you/Pictures/Shots", home: "/Users/you", override: "/tmp/x",
                                   isDirectory: isDir) == "/tmp/x")
    }

    @Test func newShotsInAListing() {
        let seen: Set<String> = ["Screenshot 1.png"]
        #expect(ShotFiles.newShots(in: ["Screenshot 2.png", "Screenshot 1.png", "a.txt", ".Screenshot 3.png"], seen: seen)
                == ["Screenshot 2.png"])
    }

    @Test func clipboardImageTypes() {
        #expect(ClipboardShot.imageType(in: ["public.tiff", "public.png"]) == "public.png")
        #expect(ClipboardShot.imageType(in: ["public.tiff"]) == "public.tiff")
        #expect(ClipboardShot.imageType(in: ["public.utf8-plain-text"]) == nil)
        #expect(ClipboardShot.imageType(in: ["public.jpeg"]) == nil)
        #expect(ClipboardShot.imageType(in: ["public.png", "org.nspasteboard.ConcealedType"]) == nil)
    }

    @Test func clipboardChangeCounter() {
        var c = ChangeCounter()
        let seq = [5, 5, 6, 6, 9].map { c.changed($0) }      // the first poll only primes
        #expect(seq == [false, false, true, false, true])
    }

    @Test func clipFileNamesAndPrune() {
        let n = ClipboardShot.fileName(at: Date(timeIntervalSince1970: 1_792_512_000.25))
        #expect(n.hasPrefix("clip-20") && n.hasSuffix("Z.png") && n.contains(".250Z"))
        let names = (1...5).map { "clip-2026102\($0)T000000.000Z.png" } + ["other.png"]
        #expect(ClipboardShot.prune(names, keep: 3) == ["clip-20261021T000000.000Z.png", "clip-20261022T000000.000Z.png"])
        #expect(ClipboardShot.prune(names, keep: 10).isEmpty)
    }

    @Test func shotEventAndBucket() throws {
        let ev = AWEvent.shot(at: Date(timeIntervalSince1970: 0), path: "/Users/you/Desktop/Screenshot 1.png", src: "file")
        let json = String(decoding: try AWEvent.encodeBatch([ev]), as: UTF8.self)
        #expect(json.contains(#""kind":"shot""#) && json.contains(#""path":"/Users/you/Desktop/Screenshot 1.png""#)
                && json.contains(#""src":"file""#))
        #expect(BucketKind.shot.bucketID(hostname: "h") == "oi-watcher-shot_h")
        #expect(BucketKind.shot.awType == "os.screenshot")
    }
    // a screenshot is live while its thumbnail / markup window is on screen
    @Test func shotWindowsLive() {
        let screen = ShotWindows.Screen(x: 0, y: 0, w: 1512, h: 982)
        let thumb = ShotWindows.Window(owner: "screencaptureui", layer: 25, x: 1512 - 190, y: 982 - 130, w: 170, h: 110)
        let overlay = ShotWindows.Window(owner: "screencaptureui", layer: 1000, x: 0, y: 0, w: 1512, h: 982)
        let toolbar = ShotWindows.Window(owner: "screencaptureui", layer: 25, x: 400, y: 880, w: 700, h: 60)
        let markup = ShotWindows.Window(owner: "screencaptureui", x: 300, y: 150, w: 900, h: 600)
        let safari = ShotWindows.Window(owner: "Safari", x: 1512 - 190, y: 982 - 130, w: 170, h: 110)
        #expect(ShotWindows.live([safari, thumb], screens: [screen]))
        #expect(ShotWindows.live([markup], screens: [screen]))
        #expect(!ShotWindows.live([overlay], screens: [screen]))          // ⌘⇧4 selection: no shot yet
        #expect(!ShotWindows.live([toolbar], screens: [screen]))          // ⌘⇧5 toolbar: no shot yet
        #expect(!ShotWindows.live([safari], screens: [screen]))
        var hidden = thumb; hidden.alpha = 0
        #expect(!ShotWindows.live([hidden], screens: [screen]))
        let second = ShotWindows.Screen(x: 1512, y: -200, w: 2560, h: 1440)   // the thumbnail on a second display
        let thumb2 = ShotWindows.Window(owner: "screencaptureui", x: 1512 + 2560 - 190, y: 1240 - 130, w: 170, h: 110)
        #expect(ShotWindows.live([thumb2], screens: [screen, second]))
    }

    @Test func liveEdges() {
        var e = LiveEdge()
        #expect([false, true, true, false, false, true].map { e.update($0) } == [nil, "live", nil, "gone", nil, "live"])
        let ev = AWEvent.shotState(at: Date(timeIntervalSince1970: 0), state: "gone", src: "thumbnail")
        let json = String(decoding: (try? AWEvent.encodeBatch([ev])) ?? Data(), as: UTF8.self)
        #expect(json.contains(#""kind":"shot_state""#) && json.contains(#""state":"gone""#) && json.contains(#""src":"thumbnail""#))
    }
}
