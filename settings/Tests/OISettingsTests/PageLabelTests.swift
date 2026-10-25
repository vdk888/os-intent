import Foundation
import Testing
@testable import OISettings

// W16: the label rules shared by oi-hud and oi-watcher (same output as python vocab.slug).

@Suite struct PageLabelTests {
    @Test func slugMatchesPython() {
        #expect(PageLabel.slug("Squash and merge") == "squash-and-merge")
        #expect(PageLabel.slug("  Confirm  squash & merge! ") == "confirm-squash-merge")
        #expect(PageLabel.slug("Écrire un e-mail") == "ecrire-un-e-mail")
        #expect(PageLabel.slug("✓ Approve") == "approve")
        #expect(PageLabel.slug("…") == "")
        #expect(PageLabel.slug(nil) == "")
        #expect(PageLabel.slug("a very long button label that goes on and on") == "a-very-long-button-label-that")  // 30, no trailing -
    }

    @Test func roles() {
        #expect(PageLabel.role(ax: "AXButton") == "button")
        #expect(PageLabel.role(ax: "AXPopUpButton") == "button")
        #expect(PageLabel.role(ax: "AXLink") == "link")
        for r in ["AXTextField", "AXSecureTextField", "AXStaticText", "AXTextArea", nil] as [String?] {
            #expect(PageLabel.role(ax: r) == nil)
        }
    }

    @Test func irreversibleWholeWords() {
        for s in ["squash-and-merge", "delete-branch", "send", "pay-now", "close-pull-request", "publish"] {
            #expect(PageLabel.irreversible(slug: s), "\(s)")
        }
        for s in ["approve", "merged-by", "sender", "files-changed", "copy", ""] {
            #expect(!PageLabel.irreversible(slug: s), "\(s)")
        }
    }

    @Test func target() {
        #expect(PageLabel.target("button:squash-and-merge")! == ("button", "squash-and-merge"))
        #expect(PageLabel.target("link:files")! == ("link", "files"))
        #expect(PageLabel.target("field:x") == nil)
        #expect(PageLabel.target("button:Not A Slug") == nil)
        #expect(PageLabel.target("button") == nil)
        #expect(PageLabel.target(nil) == nil)
    }
}
