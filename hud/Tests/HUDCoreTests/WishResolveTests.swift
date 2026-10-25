import Foundation
import Testing
@testable import HUDCore

// W18: wish → action — protocol additions, the wish row's action, the panel's resolve state machine.

private let ctx = ResolveContext(app: "Mail", space: 2)

private func sent(_ effects: [WishResolveEffect]) -> [ResolveRequest] {
    effects.compactMap { if case .send(let r) = $0 { return r } else { return nil } }
}

private func line(_ s: String) -> Data { Data(s.utf8) }

@Suite struct WishResolveProtocolTests {
    @Test func decodesTheW18Commands() throws {
        #expect(try decodeCommand(line(#"{"cmd":"resolver"}"#)) == .w18(.resolver))
        let r = try decodeCommand(line(#"{"cmd":"resolution","id":7,"state":"done","label":"Telegram","token":"activate:Telegram","tier":"local","target":"Telegram"}"#))
        #expect(r == .w18(.resolution(Resolution(id: 7, state: .done, label: "Telegram", token: "activate:Telegram",
                                                  tier: "local", target: "Telegram"))))
        #expect(try decodeCommand(line(#"{"cmd":"resolution","id":2,"state":"preview","label":""}"#))
                == .w18(.resolution(Resolution(id: 2, state: .preview, label: ""))))
        #expect(try decodeCommand(line(#"{"cmd":"debug_wish","text":"tg","app":"Mail","enter":true}"#))
                == .w18(.debugWish(text: "tg", app: "Mail", enter: true, confirm: false)))
    }

    @Test func rejectsBadW18Commands() {
        #expect(throws: CommandError.badJSON) { try decodeCommand(line(#"{"cmd":"resolution","id":1,"state":"maybe","label":""}"#)) }
        #expect(throws: CommandError.badJSON) { try decodeCommand(line(#"{"cmd":"resolution","state":"done","label":""}"#)) }
        #expect(throws: CommandError.emptyWish) { try decodeCommand(line(#"{"cmd":"debug_wish","text":"  "}"#)) }
        #expect(throws: CommandError.unknownCommand("nope")) { try decodeCommand(line(#"{"cmd":"nope"}"#)) }
    }

    @Test func resolveEventJSON() throws {
        let ev = HUDEvent.resolve(ResolveRequest(id: 3, text: "ouvre télégram", final: true, context: ctx))
        let obj = try JSONSerialization.jsonObject(with: Data(ev.jsonLine().utf8)) as! [String: Any]
        #expect(obj["event"] as? String == "resolve" && obj["id"] as? Int == 3 && obj["final"] as? Bool == true)
        #expect(obj["text"] as? String == "ouvre télégram" && obj["confirmed"] as? Bool == false)
        let c = obj["context"] as! [String: Any]
        #expect(c["app"] as? String == "Mail" && c["space"] as? Int == 2)
        let none = HUDEvent.resolve(ResolveRequest(id: 1, text: "x", final: false, context: ResolveContext(app: nil, space: nil)))
        #expect(none.jsonLine().contains(#""context":{"app":null,"space":null}"#))
    }

    @Test func wishRowCarriesTheActionOnlyWhenResolved() throws {
        let c = WishContext(app: "Mail", space: 1, hudVisible: false, hudOptions: [])
        var row = WishRow(ts: "2026-10-20T16:00:00.000Z", text: "tg", source: .typed, context: c)
        #expect(!(try row.jsonLine()).contains("action"))           // a noted wish: unchanged row (W6 format)
        row.action = WishAction(token: "activate:Telegram", tier: "local", target: "Telegram")
        let l = try row.jsonLine()
        #expect(l.contains(#""action":{"target":"Telegram","tier":"local","token":"activate:Telegram"}"#))
        let back = try JSONDecoder().decode(WishRow.self, from: Data(l.utf8))
        #expect(back == row)
        let old = try JSONDecoder().decode(WishRow.self, from: Data(#"{"context":{"app":null,"space":null,"hud_visible":false,"hud_options":[]},"source":"typed","text":"x","ts":"t"}"#.utf8))
        #expect(old.action == nil)
    }
}

@Suite struct WishResolveMachineTests {
    @Test func previewIsDebouncedTextAndOnlyTheLatestAnswerShows() {
        var m = WishResolveMachine()
        #expect(m.textChanged("tel").isEmpty)
        let a = sent(m.previewDue(context: ctx))
        #expect(a.count == 1 && a[0].final == false && a[0].text == "tel")
        _ = m.textChanged("telegram")
        let b = sent(m.previewDue(context: ctx))
        #expect(b[0].id > a[0].id)
        #expect(m.receive(Resolution(id: a[0].id, state: .preview, label: "→ Telegram ?")).isEmpty)   // stale
        #expect(m.receive(Resolution(id: b[0].id, state: .preview, label: "→ Telegram")) == [.preview("→ Telegram")])
        _ = m.textChanged("   ")
        #expect(m.previewDue(context: ctx) == [.preview("")])
    }

    @Test func withoutResolverEnterIsTheOldSave() {
        var m = WishResolveMachine()
        #expect(m.enter("telegram", context: ctx, connected: false) == nil)
        #expect(m.enter("  ", context: ctx, connected: true) == nil)
    }

    @Test func enterResolvesThenFinishesWithTheAction() {
        var m = WishResolveMachine()
        let e = m.enter(" telegram ", context: ctx, connected: true)!
        let req = sent(e)[0]
        #expect(req.final && !req.confirmed && req.text == "telegram" && req.context == ctx)
        #expect(e.contains(.hint("Working…")))
        #expect(m.enter("telegram", context: ctx, connected: true) == [])          // a second Enter waits
        #expect(m.receive(Resolution(id: req.id, state: .preview, label: "→ x")).isEmpty)
        let f = m.receive(Resolution(id: req.id, state: .done, label: "Telegram", token: "activate:Telegram",
                                     tier: "local", target: "Telegram"))
        #expect(f == [.finish(action: WishAction(token: "activate:Telegram", tier: "local", target: "Telegram"),
                              note: "✓ Telegram", holdMs: 500)])
        #expect(m.phase == .typing)
    }

    @Test func notedAndFailedSaveWithoutAction() {
        var m = WishResolveMachine()
        var id = sent(m.enter("le sens de la vie", context: ctx, connected: true)!)[0].id
        #expect(m.receive(Resolution(id: id, state: .noted, label: "noted")) == [.finish(action: nil, note: "noted", holdMs: 600)])
        id = sent(m.enter("telegram", context: ctx, connected: true)!)[0].id
        #expect(m.receive(Resolution(id: id, state: .failed, label: "No mail selected"))
                == [.finish(action: nil, note: "No mail selected", holdMs: 1500)])
    }

    @Test func aSummaryKeepsItsLinesLonger() {
        var m = WishResolveMachine()
        let id = sent(m.enter("résume", context: ctx, connected: true)!)[0].id
        let f = m.receive(Resolution(id: id, state: .done, label: "a\nb\nc", token: "summary", tier: "local"))
        #expect(f == [.finish(action: WishAction(token: "summary", tier: "local", target: nil), note: "a · b · c", holdMs: 6000)])
    }

    @Test func irreversibleNeedsTheSecondKey() {
        var m = WishResolveMachine()
        let id = sent(m.enter("envoie à nova salut", context: ctx, connected: true)!)[0].id
        #expect(m.receive(Resolution(id: id, state: .confirm, label: "Send to Nova: “salut”? ⇧ again"))
                == [.hint("Send to Nova: “salut”? ⇧ again")])
        #expect(m.isConfirming)
        let c = sent(m.confirmKey(context: ctx))
        #expect(c.count == 1 && c[0].id == id && c[0].confirmed && c[0].text == "envoie à nova salut")
        #expect(m.confirmKey(context: ctx).isEmpty)                               // only once
    }

    @Test func enterAgainOnTheSameTextConfirmsToo() {
        var m = WishResolveMachine()
        let id = sent(m.enter("merge", context: ctx, connected: true)!)[0].id
        _ = m.receive(Resolution(id: id, state: .confirm, label: "Merge PR #134? ⇧ again"))
        let c = sent(m.enter("merge", context: ctx, connected: true)!)
        #expect(c[0].confirmed && c[0].id == id)
    }

    @Test func editingDropsTheConfirm() {
        var m = WishResolveMachine()
        let id = sent(m.enter("merge", context: ctx, connected: true)!)[0].id
        _ = m.receive(Resolution(id: id, state: .confirm, label: "Merge? ⇧ again"))
        #expect(m.textChanged("merge it") == [.hint(nil)])
        #expect(!m.isConfirming && m.confirmKey(context: ctx).isEmpty)
        let again = sent(m.enter("merge it", context: ctx, connected: true)!)
        #expect(!again[0].confirmed && again[0].id != id)                         // a new request, never a confirm
    }

    @Test func timeoutNotesTheWish() {
        var m = WishResolveMachine()
        let id = sent(m.enter("telegram", context: ctx, connected: true)!)[0].id
        #expect(m.timedOut(id: id + 1).isEmpty)
        #expect(m.timedOut(id: id) == [.finish(action: nil, note: "noted", holdMs: 600)])
        #expect(m.receive(Resolution(id: id, state: .done, label: "late", token: "t")).isEmpty)   // too late: ignored
    }
}

// W18 follow-up: Esc while "Working…" (or while a confirm waits) cancels the request on the resolver.
@Suite struct WishResolveCancelTests {
    @Test func closingWhileResolvingCancelsThatRequest() {
        var m = WishResolveMachine()
        let id = sent(m.enter("mes notes", context: ctx, connected: true)!)[0].id
        #expect(m.closed() == [.cancel(id: id)])
        #expect(m.phase == .typing && m.closed().isEmpty)
        #expect(m.receive(Resolution(id: id, state: .done, label: "x", token: "t")).isEmpty)
    }

    @Test func closingWhileConfirmingCancelsToo() {
        var m = WishResolveMachine()
        let id = sent(m.enter("envoie à nova salut", context: ctx, connected: true)!)[0].id
        _ = m.receive(Resolution(id: id, state: .confirm, label: "Send? ⇧ again"))
        #expect(m.closed() == [.cancel(id: id)])
    }

    @Test func closingAfterTheResultOrWhileTypingSendsNothing() {
        var m = WishResolveMachine()
        _ = m.textChanged("tele")
        #expect(m.closed().isEmpty)
        let id = sent(m.enter("telegram", context: ctx, connected: true)!)[0].id
        _ = m.receive(Resolution(id: id, state: .done, label: "Telegram", token: "activate:Telegram", tier: "local"))
        #expect(m.closed().isEmpty)                                 // the save closes the panel: nothing to cancel
    }

    @Test func cancelEventJSON() {
        #expect(HUDEvent.resolveCancel(id: 7).jsonLine() == #"{"event":"cancel","id":7}"# + "\n")
    }
}

// W18 follow-up (after W16): a cancelled page-action confirm drops its press ticket.
@Suite struct WishResolvePressRevokeTests {
    @Test func revokeDropsOnlyThatTicket() {
        var g = PressGuard()
        g.issue(target: "button:squash-and-merge", confirmed: true, at: 10)
        g.revoke(target: "button:close-pull-request")
        g.revoke(target: "button:squash-and-merge")
        #expect(g.redeem(role: "button", slug: "squash-and-merge", at: 10.5) == "no_tap")
        g.issue(target: "button:comment", confirmed: false, at: 20)
        g.revoke(target: "button:squash-and-merge")
        #expect(g.redeem(role: "button", slug: "comment", at: 20.5) == nil)          // another target: kept
    }

    @Test func cancelAfterTheConfirmIsSentNamesThatRequest() {
        var m = WishResolveMachine()
        let id = sent(m.enter("merge", context: ctx, connected: true)!)[0].id
        _ = m.receive(Resolution(id: id, state: .confirm, label: "Squash and merge? ⇧ again"))
        #expect(sent(m.confirmKey(context: ctx))[0].confirmed)
        #expect(m.closed() == [.cancel(id: id)])            // the controller revokes the ticket issued for `id`
    }
}
