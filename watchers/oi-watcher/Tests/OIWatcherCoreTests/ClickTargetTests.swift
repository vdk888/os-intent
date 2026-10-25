import Foundation
import Testing
@testable import OIWatcherCore

// W16: which mouse-ups in Safari become click tokens (label only, page only, never a field).

private typealias N = ClickTarget.Node

@Suite struct ClickTargetTests {
    @Test func textInsideAButtonIsTheButton() {
        let chain = [N(role: "AXStaticText"), N(role: "AXButton", label: "Squash and merge"), N(role: "AXGroup"),
                     N(role: "AXWebArea")]
        let r = ClickTarget.resolve(chain)
        #expect(r?.role == "button" && r?.slug == "squash-and-merge")
        let link = ClickTarget.resolve([N(role: "AXLink", label: "Files changed"), N(role: "AXWebArea")])
        #expect(link?.role == "link" && link?.slug == "files-changed")
    }

    @Test func fieldsEmptyLabelsAndPlainTextAreNeverLogged() {
        #expect(ClickTarget.resolve([N(role: "AXTextField"), N(role: "AXGroup"), N(role: "AXWebArea")]) == nil)
        #expect(ClickTarget.resolve([N(role: "AXSecureTextField"), N(role: "AXButton", label: "x"), N(role: "AXWebArea")]) == nil)
        #expect(ClickTarget.resolve([N(role: "AXButton", label: "  "), N(role: "AXWebArea")]) == nil)
        #expect(ClickTarget.resolve([N(role: "AXStaticText"), N(role: "AXGroup"), N(role: "AXWebArea")]) == nil)
    }

    @Test func outsideThePageIsNeverLogged() {
        // Safari's toolbar / tab bar: the chain never reaches the page
        #expect(ClickTarget.resolve([N(role: "AXButton", label: "Back"), N(role: "AXToolbar"), N(role: "AXWindow")]) == nil)
        // a button far above the clicked node (more than maxButtonDepth levels) is not "this" click
        let deep = Array(repeating: N(role: "AXGroup"), count: 8) + [N(role: "AXButton", label: "x"), N(role: "AXWebArea")]
        #expect(ClickTarget.resolve(deep) == nil)
    }

    @Test func denylistAndEvent() throws {
        #expect(ClickTarget.allowed(denylist: ["Passwords"]))
        #expect(!ClickTarget.allowed(denylist: ["safari"]))
        let ev = AWEvent.click(at: Date(timeIntervalSince1970: 0), host: "github.com", role: "button", slug: "squash-and-merge")
        let body = String(decoding: try AWEvent.encodeBatch([ev]), as: UTF8.self)
        #expect(body.contains(#""data":{"host":"github.com","label":"squash-and-merge","role":"button"}"#))
        #expect(BucketKind.click.bucketID(hostname: "h") == "oi-watcher-click_h" && BucketKind.click.awType == "ui.click")
    }
}
