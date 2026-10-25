import Foundation
import CoreGraphics
import Testing
@testable import HUDCore
import OISettings

// W16: page actions — snapshot JSON, the socket commands, re-find, confirm, press guard, hints.

private func obj(_ line: String) -> [String: Any] {
    (try! JSONSerialization.jsonObject(with: Data(line.utf8))) as! [String: Any]
}

@Suite struct PageSnapshotTests {
    @Test func jsonCarriesLabelsAndFramesOnly() {
        let s = PageSnapshot(trusted: true, app: "Safari", host: "github.com",
                             elements: [PageElement(role: "button", label: "Squash and merge",
                                                    frame: CGRect(x: 10.04, y: 20, width: 140, height: 32))], ms: 12)
        let line = s.jsonLine()
        #expect(line.hasSuffix("\n"))
        let o = obj(line)
        #expect(o["event"] as? String == "page" && o["trusted"] as? Bool == true && o["host"] as? String == "github.com")
        let e = (o["elements"] as! [[String: Any]])[0]
        #expect(Set(e.keys) == ["role", "label", "slug", "frame"])
        #expect(e["slug"] as? String == "squash-and-merge" && (e["frame"] as? [Double]) == [10, 20, 140, 32])
        let untrusted = obj(PageSnapshot(trusted: false, reason: "not_trusted").jsonLine())
        #expect((untrusted["elements"] as! [Any]).isEmpty && untrusted["reason"] as? String == "not_trusted")
    }

    @Test func labelCappedAt80() {
        #expect(PageElement(role: "link", label: String(repeating: "a", count: 200), frame: .zero).label.count == 80)
    }

    @Test func hostOfPageURL() {
        #expect(pageHost(URL(string: "https://GitHub.com/octocat/x/pull/2")) == "github.com")
        #expect(pageHost(URL(string: "http://127.0.0.1:8716/")) == "127.0.0.1:8716")
        #expect(pageHost(URL(string: "favorites://")) == nil)
        #expect(pageHost(nil) == nil)
    }
}

@Suite struct PageCommandTests {
    @Test func decode() {
        #expect(decodePageCommand(Data(#"{"cmd":"page"}"#.utf8)) == .success(.page))
        #expect(decodePageCommand(Data(#"{"cmd":"ax_press","role":"button","slug":"squash-and-merge","host":"GitHub.com"}"#.utf8))
                == .success(.press(role: "button", slug: "squash-and-merge", host: "github.com", dryRun: false)))
        #expect(decodePageCommand(Data(#"{"cmd":"ax_press","role":"link","slug":"files","dry_run":true}"#.utf8))
                == .success(.press(role: "link", slug: "files", host: nil, dryRun: true)))
        #expect(decodePageCommand(Data(#"{"cmd":"ax_press","role":"textfield","slug":"x"}"#.utf8))
                == .failure(PressOutcome(ok: false, reason: "bad_target")))
        #expect(decodePageCommand(Data(#"{"cmd":"ax_press","role":"button","slug":"Not a slug"}"#.utf8))
                == .failure(PressOutcome(ok: false, reason: "bad_target")))
        // not ours: the normal decoder takes them
        #expect(decodePageCommand(Data(#"{"cmd":"show","options":[]}"#.utf8)) == nil)
        #expect(decodePageCommand(Data("junk".utf8)) == nil)
    }

    @Test func pressOutcomeLineAndToast() {
        let o = obj(PressOutcome(ok: false, reason: "gone").jsonLine())
        #expect(o["event"] as? String == "ax_press" && o["ok"] as? Bool == false && o["reason"] as? String == "gone")
        #expect(PressOutcome(ok: false, reason: "gone").toast == "Page changed — nothing pressed")
        #expect(PressOutcome(ok: false, reason: "host_changed").toast == "Page changed — nothing pressed")
        #expect(PressOutcome(ok: false, reason: "not_trusted").toast?.contains("Accessibility") == true)
        #expect(PressOutcome(ok: true).toast == nil)
    }
}

@Suite struct RefindTests {
    let els = [PageElement(role: "link", label: "Squash and merge", frame: CGRect(x: 0, y: 0, width: 10, height: 10)),
               PageElement(role: "button", label: "Squash and merge", frame: CGRect(x: 0, y: -900, width: 100, height: 30)),
               PageElement(role: "button", label: "Squash  and merge!", frame: CGRect(x: 0, y: 400, width: 100, height: 30)),
               PageElement(role: "button", label: "Delete branch", frame: CGRect(x: 0, y: 500, width: 100, height: 30))]

    @Test func byRoleAndSlugPreferringTheVisibleOne() {
        let vp = CGRect(x: 0, y: 100, width: 1200, height: 800)
        #expect(refind(role: "button", slug: "squash-and-merge", in: els, element: { $0 }, viewport: vp)?.frame.minY == 400)
        #expect(refind(role: "button", slug: "squash-and-merge", in: els, element: { $0 })?.frame.minY == -900)   // page order
        #expect(refind(role: "link", slug: "squash-and-merge", in: els, element: { $0 })?.role == "link")
    }

    @Test func goneIsNil() {
        #expect(refind(role: "button", slug: "merge-pull-request", in: els, element: { $0 }) == nil)
        #expect(refind(role: "button", slug: "squash-and-merge", in: [PageElement](), element: { $0 }) == nil)
    }
}

@Suite struct PressGuardTests {
    @Test func needsAFreshSingleUseTicketForThatTarget() {
        var g = PressGuard()
        do { let r = g.redeem(role: "button", slug: "copy", at: 1); #expect(r == "no_tap") }             // never tapped
        g.issue(target: "button:copy", confirmed: false, at: 10)
        do { let r = g.redeem(role: "button", slug: "other", at: 10.1); #expect(r == "no_tap") }          // another target
        do { let r = g.redeem(role: "button", slug: "copy", at: 10.2); #expect(r == nil) }                // allowed once ...
        do { let r = g.redeem(role: "button", slug: "copy", at: 10.3); #expect(r == "no_tap") }           // ... only once
        g.issue(target: "button:copy", confirmed: false, at: 20)
        do { let r = g.redeem(role: "button", slug: "copy", at: 25.5); #expect(r == "no_tap") }          // stale (> 5 s)
    }

    @Test func irreversibleNeedsTheGateValidatedTicket() {
        var g = PressGuard()
        g.issue(target: "button:squash-and-merge", confirmed: false, at: 1)      // the tap alone

        do { let r = g.redeem(role: "button", slug: "squash-and-merge", at: 1.1); #expect(r == "not_confirmed") }
        g.issue(target: "button:squash-and-merge", confirmed: true, at: 2)       // the outward gate's validate
        do { let r = g.redeem(role: "button", slug: "squash-and-merge", at: 2.1); #expect(r == nil) }
    }
}

@Suite struct PageOptionProtocolTests {
    @Test func confirmTargetAndElementHintDecode() throws {
        let line = #"{"cmd":"show","options":[{"key":1,"label":"Squash & merge","icon":"sf:arrow.triangle.merge","confirm":true,"target":"button:squash-and-merge","hint":{"kind":"element","rect":[10,20,140,32]}},{"key":2,"label":"Copy","target":"button:copy"},{"key":3,"label":"Close","target":"button:close-pull-request","hint":{"kind":"element","rect":[1,2,0,0]}}]}"#
        guard case .show(let opts, _) = try decodeCommand(Data(line.utf8)) else { Issue.record("not a show"); return }
        #expect(opts[0].confirm == true && opts[0].needsConfirm)
        #expect(opts[0].hint == .element(rect: CGRect(x: 10, y: 20, width: 140, height: 32)))
        #expect(!opts[1].needsConfirm && opts[1].target == "button:copy")
        #expect(opts[2].needsConfirm)                  // irreversible label: confirm even when the driver forgot it
        #expect(opts[2].hint == nil)                   // empty rect: no hint, the show still works
    }

    @Test func elementHintRoundTripAndDrawing() throws {
        let h = HUDHint.element(rect: CGRect(x: 1, y: 2, width: 3, height: 4))
        let back = try JSONDecoder().decode(HUDHint.self, from: try JSONEncoder().encode(h))
        #expect(back == h)
        #expect(resolveDrawing(h, windowFound: true, dockFound: false) == .element)
        #expect(resolveDrawing(h, windowFound: false, dockFound: true) == .halo)     // off screen: halo on the icon
        #expect(hintedOptions([HUDOption(key: 1, label: "x", hint: h)]).count == 1)
    }
}

@Suite struct WishPagePressTests {
    @Test func confirmResolutionCarriesThePageTarget() throws {
        let r = try JSONDecoder().decode(Resolution.self, from: Data(#"{"id":5,"state":"confirm","label":"Click “Squash & merge”? ⇧ again","press":"button:squash-and-merge"}"#.utf8))
        #expect(r.press == "button:squash-and-merge" && PageLabel.target(r.press) != nil)
        let plain = try JSONDecoder().decode(Resolution.self, from: Data(#"{"id":6,"state":"done","label":"Telegram"}"#.utf8))
        #expect(plain.press == nil)
        var m = WishResolveMachine()
        _ = m.enter("merge", context: ResolveContext(app: "Safari", space: 1), connected: true)
        var mine = r; mine.id = 1                 // the machine's first request id
        _ = m.receive(mine)
        #expect(m.isConfirming)            // the controller issues the ticket only now, on ⇧ / Enter
    }
}
