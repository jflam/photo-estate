import Foundation

// The phone's copy of the catalog (Catalog.swift on SQLite.swift), on the Mac: each step a sync and a favourite take on
// the replica, in the orders specs/PhoneReplica.tla found, on a scratch file. Run by ios/Tests/run-replica.sh.

var failures = 0

func check(_ ok: Bool, _ what: String) {
    print(ok ? "ok    \(what)" : "FAIL  \(what)")
    if !ok { failures += 1 }
}

let y = String(repeating: "a", count: 64)
let w = String(repeating: "b", count: 64)

func row(_ sha: String, fav: Bool = false, place: String = "Paris") -> [String: Any] {
    ["sha": sha, "ext": ".jpg", "taken": "2026-01-01T10:00:00", "fav": fav, "place": place, "people": ["Ben"]]
}

func page(_ rows: [[String: Any]], deleted: [String] = [], epoch: String = "E1", since: Int, after: String = "~",
          more: Bool = false) -> Catalog.Page {
    Catalog.Page(rows: rows, deleted: deleted, epoch: epoch, since: since, after: after, more: more, trips: "[]")
}

let dir = FileManager.default.temporaryDirectory.appendingPathComponent("replica-tests-\(UUID().uuidString)")
try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: dir) }

func replica(_ name: String) throws -> Store {
    try Store(path: dir.appendingPathComponent("\(name).db").path, schema: Catalog.schema, migrate: Catalog.migrate)
}

func photos(_ s: Store) async throws -> [String: Bool] {
    Dictionary(uniqueKeysWithValues: try await s.read { try $0.query("SELECT sha, fav FROM photos") { ($0.text(0), $0.bool(1)) } })
}

/// SyncEngine.run, one page: the cursor read, the page (as the server answered it) applied with it.
func sync(_ s: Store, _ p: Catalog.Page) async throws -> Bool {
    let asked = try await s.read { try Catalog.cursor($0) }
    return try await s.write { try Catalog.applyPage($0, p, asked: asked) }
}

// What must not change: pages applied in turn; a deletion; the cursor kept with each page.
do {
    let s = try replica("plain")
    _ = try await sync(s, page([row(y)], since: 1, after: y, more: true))
    _ = try await sync(s, page([row(w)], since: 1))
    check(try await photos(s) == [y: false, w: false], "two pages: both rows")
    _ = try await sync(s, page([], deleted: [w], since: 2))
    check(try await photos(s) == [y: false], "a deletion takes the row out")
    check(try await s.read { try Catalog.cursor($0) } == Catalog.Cursor(epoch: "E1", since: "2", after: "~"), "the cursor is the page's")
    try await s.write { try Catalog.keep($0, [y], true) }
    check(try await photos(s) == [y: true], "a favourite kept")
}

// Hypothesis 6, refuted: a sync stopped between pages across an epoch change. The new epoch's first page, then the sync
// stops; the next one carries on in the new epoch, and its last page drops what only the old epoch had.
do {
    let s = try replica("epoch")
    _ = try await sync(s, page([row(y), row(w)], since: 3))
    _ = try await sync(s, page([row(y)], epoch: "E2", since: 1, after: y, more: true))    // the feed's file was lost
    check(try await photos(s) == [y: false, w: false], "mid-way through a new epoch, the old rows stay until its end")
    _ = try await sync(s, page([], epoch: "E2", since: 1))
    check(try await photos(s) == [y: false], "at its end, what the new epoch did not send is dropped")
}

// KeptStays: specs/PhoneReplica.tla's trace. The phone has Y, not a favourite. A sync asks its next page; meanwhile Ada
// makes Y a favourite on the phone (Favourites.set: the server agrees, and the phone keeps it). The page, which the
// server answered before the favourite (Y as it was then, not a favourite), is applied after the keep: the phone says
// Y is not a favourite, though the server agreed it is, until a later sync brings Y again.
do {
    let s = try replica("kept")
    _ = try await sync(s, page([row(y), row(w)], since: 1))
    let asked = try await s.read { try Catalog.cursor($0) }             // the page in flight was asked with this
    try await s.write { try Catalog.keep($0, [y], true) }                // Favourites.keep, once the server agreed
    let applied = try await s.write { try Catalog.applyPage($0, page([row(y, place: "Lyon")], since: 2), asked: asked) }
    check(try await photos(s)[y] == true, "KeptStays: a page answered before a favourite the phone kept is not applied over it")
    let now = try await s.read { try Catalog.cursor($0) }
    check(!applied && (now.epoch, now.since, now.after) == (asked.epoch, asked.since, asked.after),
          "that page is not applied: the cursor is where it was, to ask again")
    _ = try await sync(s, page([row(y, fav: true, place: "Lyon")], since: 3))    // asked again: answered after the favourite
    check(try await photos(s) == [y: true, w: false], "the page asked again is applied")
}

print(failures == 0 ? "replica: ok" : "replica: \(failures) failed")
exit(failures == 0 ? 0 : 1)
