import Foundation
import Testing
@testable import OIWatcherCore
import OISettings

private func proactive(_ json: String) -> JSONValue? {
    try? JSONDecoder().decode(JSONValue.self, from: Data(json.utf8))
}

@Suite("Files connector (watcher side)")
struct FilesTests {
    let home = "/Users/you"

    @Test func foldersFollowTheProactiveSettings() {
        #expect(FileFolders.folders(proactive: nil, home: home) == nil)
        #expect(FileFolders.folders(proactive: proactive(#"{"sources":{"files":false}}"#), home: home) == nil)
        #expect(FileFolders.folders(proactive: proactive(#"{"sources":{"files":"yes"}}"#), home: home) == nil)   // JSON true only
        #expect(FileFolders.folders(proactive: proactive(#"{"sources":{"files":true}}"#), home: home) == ["/Users/you/Desktop"])
        let p = proactive(#"{"sources":{"files":true},"files":{"folders":["~/Desktop", 3, " ", "~/Library/CloudStorage/GoogleDrive-x/My Drive", "/tmp/x"]}}"#)
        #expect(FileFolders.folders(proactive: p, home: home) ==
                ["/Users/you/Desktop", "/Users/you/Library/CloudStorage/GoogleDrive-x/My Drive", "/tmp/x"])
        #expect(FileFolders.folders(proactive: proactive(#"{"sources":{"files":true},"files":{"folders":[]}}"#), home: home) == [])
        let many = (0..<20).map { "\"/f\($0)\"" }.joined(separator: ",")
        #expect(FileFolders.folders(proactive: proactive(#"{"sources":{"files":true},"files":{"folders":[\#(many)]}}"#), home: home)?.count == 12)
    }

    @Test func wantedSkipsHiddenLocksScreenshotsAndPartials() {
        #expect(FileFolders.wanted("devis.pdf"))
        for n in [".DS_Store", "~$rapport.docx", "Screenshot 2026-10-21 at 10.00.00.png", "Capture d’écran 2026.png",
                  "big.zip.crdownload", "x.PART", "a.download"] {
            #expect(!FileFolders.wanted(n), "\(n)")
        }
    }

    @Test func folderClassMatchesPython() {
        #expect(FileFolders.folderClass("/Users/you/Desktop", home: home) == "desktop")
        #expect(FileFolders.folderClass("/Users/you/Downloads", home: home) == "downloads")
        #expect(FileFolders.folderClass("/Users/you/Library/CloudStorage/GoogleDrive-x/My Drive", home: home) == "drive")
        #expect(FileFolders.folderClass("/tmp/Mes Dossiers Été", home: home) == "mes-dossiers-ete")
        #expect(FileFolders.folderClass("/tmp/___", home: home) == "folder")
    }

    @Test func firstListingPrimesThenOnlyNewWantedNames() {
        var d = FolderDiff()
        #expect(d.appeared(["old.pdf", "b.txt"]) == [])                         // files there at start: never news
        #expect(d.appeared(["old.pdf", "b.txt", "new.pdf", ".hidden", "x.crdownload"]) == ["new.pdf"])
        #expect(d.appeared(["old.pdf", "b.txt", "new.pdf"]) == [])              // the same listing: nothing
        #expect(d.appeared(["b.txt", "new.pdf"]) == [])                        // a removal is not an appearance
        #expect(d.appeared(["b.txt", "new.pdf", "old.pdf"]) == ["old.pdf"])     // back again = new again
    }

    @Test func liveFileGoneWhenRemovedOrOpened() {
        var live = LiveFiles()
        var used: [String: String] = [:]
        #expect(live.appear("/D/a.pdf", used: nil) == [] && live.appear("/D/b.pdf", used: nil) == [])
        #expect(live.appear("/D/a.pdf", used: nil) == [])                       // already live: no duplicate
        #expect(live.ended(in: "/D", listing: ["a.pdf", "b.pdf"], used: { used[$0] }).isEmpty)   // still new
        used["/D/a.pdf"] = "2026-10-21T10:00:00Z"                               // opened
        let opened = live.ended(in: "/D", listing: ["a.pdf", "b.pdf"], used: { used[$0] })
        #expect(opened.map(\.0) == ["/D/a.pdf"] && opened.map(\.1) == [.opened])
        let removed = live.ended(in: "/D", listing: [], used: { used[$0] })
        #expect(removed.map(\.0) == ["/D/b.pdf"] && removed.map(\.1) == [.removed])
        #expect(live.paths.isEmpty)
    }

    @Test func otherFoldersAreUntouchedAndForgetIsSilent() {
        var live = LiveFiles()
        _ = live.appear("/D/a.pdf", used: nil)
        _ = live.appear("/E/b.pdf", used: nil)
        #expect(live.ended(in: "/D", listing: ["a.pdf"], used: { _ in nil }).isEmpty)
        #expect(live.ended(in: "/E", listing: ["b.pdf"], used: { _ in nil }).isEmpty)
        live.forget(folder: "/E")
        #expect(live.paths == ["/D/a.pdf"])
    }

    @Test func moreThanMaxLiveDropsTheOldest() {
        var live = LiveFiles(max: 2)
        _ = live.appear("/D/1", used: nil)
        _ = live.appear("/D/2", used: nil)
        #expect(live.appear("/D/3", used: nil) == ["/D/1"])
        #expect(live.paths == ["/D/2", "/D/3"])
    }

    @Test func eventJSONCarriesMetadataOnly() throws {
        let t = Date(timeIntervalSince1970: 0)
        let live = String(decoding: try AWEvent.encodeBatch([.fileLive(at: t, path: "/Users/you/Desktop/devis.pdf", cls: "desktop", mtime: 12, size: 34)]), as: UTF8.self)
        #expect(live == #"[{"data":{"class":"desktop","kind":"file","mtime":12,"path":"/Users/you/Desktop/devis.pdf","size":34,"state":"live"},"duration":0,"timestamp":"1970-01-01T00:00:00.000Z"}]"#)
        let gone = String(decoding: try AWEvent.encodeBatch([.fileGone(at: t, path: "/D/a", cls: "d", reason: .opened)]), as: UTF8.self)
        #expect(gone.contains(#""state":"gone""#) && gone.contains(#""reason":"opened""#) && !gone.contains("size"))
        #expect(BucketKind.file.bucketID(hostname: "h") == "oi-watcher-file_h" && BucketKind.file.awType == "os.file.state")
    }

    @Test func statusFileShape() throws {
        let s = FilesStatus(ts: "2026-10-21T19:05:00Z", pid: 7, enabled: true, folders: ["/Users/you/Desktop": .unreadable])
        #expect(String(decoding: try s.encoded(), as: UTF8.self) ==
                #"{"enabled":true,"folders":{"/Users/you/Desktop":"unreadable"},"pid":7,"ts":"2026-10-21T19:05:00Z"}"# + "\n")
    }
}
