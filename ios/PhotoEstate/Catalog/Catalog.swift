import Foundation

/// A photo as the catalog knows it: the row the server sends for it (appendix B of the book).
struct Photo: Identifiable, Equatable {
    var sha = ""
    var ext = ""
    var taken: String?
    var local: String?
    var tz: String?
    var lat: Double?
    var lon: Double?
    var desc: String?
    var fav = false
    var w: Int?
    var h: Int?
    var ar = 1.3333
    var path: String?
    var camera: String?
    var lens: String?
    var focal: Double?
    var focal35: Double?
    var fnumber: Double?
    var exposure: Double?
    var iso: Double?
    var place: String?
    var city: String?
    var region: String?
    var country: String?
    var county: String?
    var placeInferred = false
    var event: Int64?
    var eventTitle: String?
    var trip: Int64?
    var tripTitle: String?
    var people: [String] = []
    var albums: [String] = []
    var stackOf: String?      // the stack it is in (near-identical shots taken together), named by one of its photos
    var stackN = 0            // how many photos that stack holds
    var best: Double?         // what "Best first" sorts by: larger is better
    var video = false         // a video, not a still: it plays from the server, at `/video/<sha>`
    var dur: Double?          // a video's length in seconds
    var live: String?         // a Live Photo's moving part: the sha of its short clip, also at `/video/<sha>`
    var at: Double?           // a video a typed search found: where in it the words match best, in seconds

    var id: String { sha }
    var when: String { local ?? taken ?? "" }
    var filename: String { path.map { ($0 as NSString).lastPathComponent } ?? "\(sha.prefix(12))\(ext)" }

    /// From the server's JSON.
    init(json j: [String: Any]) {
        func s(_ k: String) -> String? { (j[k] as? String).flatMap { $0.isEmpty ? nil : $0 } }
        func d(_ k: String) -> Double? { (j[k] as? NSNumber)?.doubleValue }
        func n(_ k: String) -> Int64? { (j[k] as? NSNumber)?.int64Value }
        sha = s("sha") ?? ""; ext = s("ext") ?? ""
        taken = s("taken"); local = s("local"); tz = s("tz")
        lat = d("lat"); lon = d("lon"); desc = s("desc")
        fav = (j["fav"] as? Bool) ?? false
        w = n("w").map(Int.init); h = n("h").map(Int.init); ar = d("ar") ?? 1.3333
        path = s("path"); camera = s("camera"); lens = s("lens")
        focal = d("focal"); focal35 = d("focal35"); fnumber = d("fnumber"); exposure = d("exposure"); iso = d("iso")
        place = s("place"); city = s("city"); region = s("region"); country = s("country"); county = s("county")
        placeInferred = (j["place_inferred"] as? Bool) ?? false
        event = n("event"); eventTitle = s("event_title"); trip = n("trip"); tripTitle = s("trip_title")
        people = (j["people"] as? [String]) ?? []; albums = (j["albums"] as? [String]) ?? []
        stackOf = s("stack_of"); stackN = n("stack").map(Int.init) ?? 0; best = d("best")
        video = s("kind") == "video"; dur = d("dur"); live = s("live"); at = d("at")
    }

    static let columns = """
        sha, ext, taken, local, tz, lat, lon, descr, fav, w, h, ar, path, camera, lens, focal, focal35, fnumber, \
        exposure, iso, place, city, region, country, county, place_inferred, event, event_title, trip, trip_title, people, albums, \
        stack, best, kind, dur, live
        """

    /// From a row selected with `columns`.
    init(row r: Row) {
        func list(_ i: Int32) -> [String] {
            (r.string(i)?.data(using: .utf8)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String] } ?? []
        }
        sha = r.text(0); ext = r.text(1); taken = r.string(2); local = r.string(3); tz = r.string(4)
        lat = r.optDouble(5); lon = r.optDouble(6); desc = r.string(7); fav = r.bool(8)
        w = r.optInt(9).map(Int.init); h = r.optInt(10).map(Int.init); ar = r.double(11)
        path = r.string(12); camera = r.string(13); lens = r.string(14)
        focal = r.optDouble(15); focal35 = r.optDouble(16); fnumber = r.optDouble(17); exposure = r.optDouble(18); iso = r.optDouble(19)
        place = r.string(20); city = r.string(21); region = r.string(22); country = r.string(23); county = r.string(24)
        placeInferred = r.bool(25); event = r.optInt(26); eventTitle = r.string(27); trip = r.optInt(28); tripTitle = r.string(29)
        people = list(30); albums = list(31)
        stackOf = r.string(32); best = r.optDouble(33)
        video = r.string(34) == "video"; dur = r.optDouble(35); live = r.string(36)
    }
}

/// What the grid needs to draw a photo, and nothing else: a library of 100,000 is held as these.
struct Tile: Equatable {
    let sha: String
    let ar: Float
    let when: String      // local wall-clock time, "" if undated
    let city: String?     // "Paris, Île-de-France, France"
    let fav: Bool
    var stack: Int32 = 0              // photos in its stack, 0 for a photo on its own
    var best: Float = -.infinity      // what "Best first" sorts by; -infinity when the server has not said
    var video = false                 // a video: the grid shows its length, the viewer plays it
    var live = false                  // a Live Photo: a still with a short clip that plays while held
    var dur: Float = 0                // a video's length in seconds
    var at: Float = 0                 // where a video starts playing: where a typed search matched it

    /// What the grid needs of a row the server sent (a search, an album) or the catalog read.
    init(_ p: Photo) {
        self.init(sha: p.sha, ar: Float(p.ar), when: p.when, city: p.city, fav: p.fav, stack: Int32(p.stackN),
                  best: p.best.map { Float($0) } ?? -.infinity)
        video = p.video; live = p.live != nil; dur = Float(p.dur ?? 0); at = Float(p.at ?? 0)
    }

    init(sha: String, ar: Float, when: String, city: String?, fav: Bool, stack: Int32 = 0, best: Float = -.infinity) {
        self.sha = sha; self.ar = ar; self.when = when; self.city = city; self.fav = fav; self.stack = stack; self.best = best
    }

    /// The columns `init(row:)` reads, in order, for a `SELECT ... FROM photos`.
    static let columns = "sha, ar, whenk, city, fav, best, kind, dur, live IS NOT NULL"

    /// From a row selected with `columns`, and a stack size if the query counted one.
    init(row r: Row, stack: Int32 = 0) {
        self.init(sha: r.text(0), ar: Float(r.double(1)), when: r.text(2), city: r.string(3), fav: r.bool(4), stack: stack,
                  best: r.optDouble(5).map { Float($0) } ?? -.infinity)
        video = r.string(6) == "video"; dur = Float(r.optDouble(7) ?? 0); live = r.bool(8)
    }

    /// A video's length as a clock: 0:07, 12:30, 1:02:03.
    static func clock(_ seconds: Float) -> String {
        let s = max(Int(seconds.rounded()), 0)
        return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60) : String(format: "%d:%02d", s / 60, s % 60)
    }
}

struct Trip: Identifiable, Equatable {
    let id: Int64
    let title: String
    let start: String
    let end: String
    let n: Int
    let cover: String
}

/// The phone's own copy of the catalog: one SQLite file, filled by `SyncEngine`, read by everything else.
/// It is a replica (the server is the only writer), so it can always be thrown away and fetched again.
final class Catalog {
    static let shared = Catalog()

    static let folder: URL = {
        var url = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("PhotoEstate", isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        var v = URLResourceValues()
        v.isExcludedFromBackup = true      // everything here can be fetched from the server again
        try? url.setResourceValues(v)
        return url
    }()

    private static let schema = """
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY, sha TEXT UNIQUE NOT NULL, ext TEXT, taken TEXT, local TEXT, tz TEXT,
            whenk TEXT NOT NULL DEFAULT '', ym INTEGER NOT NULL DEFAULT -1,
            lat REAL, lon REAL, descr TEXT, fav INTEGER NOT NULL DEFAULT 0, dup INTEGER NOT NULL DEFAULT 0,
            w INTEGER, h INTEGER, ar REAL NOT NULL DEFAULT 1.3333, path TEXT, camera TEXT, lens TEXT,
            focal REAL, focal35 REAL, fnumber REAL, exposure REAL, iso REAL,
            place TEXT, city TEXT, region TEXT, country TEXT, county TEXT, place_inferred INTEGER NOT NULL DEFAULT 0,
            event INTEGER, event_title TEXT, trip INTEGER, trip_title TEXT, people TEXT, albums TEXT,
            place_lc TEXT, place_words TEXT, camera_lc TEXT, albums_lc TEXT, people_lc TEXT, epoch TEXT,
            stack TEXT, best REAL, kind TEXT, dur REAL, live TEXT);
        CREATE INDEX IF NOT EXISTS photos_when ON photos(whenk);
        CREATE INDEX IF NOT EXISTS photos_event ON photos(event);
        CREATE INDEX IF NOT EXISTS photos_trip ON photos(trip);
        CREATE TABLE IF NOT EXISTS tags (kind TEXT NOT NULL, name TEXT NOT NULL, id INTEGER NOT NULL,
            PRIMARY KEY (kind, name, id)) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS tags_id ON tags(id);
        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, tokenize='unicode61 remove_diacritics 2');
        """

    /// A catalog from an older build gets the columns it lacks: two for stacks and "Best first", three for
    /// videos and Live Photos. They fill in as the server sends rows again (a row changes when the server
    /// begins to send a new field of it); until then every photo held is a still, as it always was.
    private static func migrate(_ db: SQLite) throws {
        let have = Set(try db.query("PRAGMA table_info(photos)") { $0.text(1) })
        for (name, type) in [("stack", "TEXT"), ("best", "REAL"), ("kind", "TEXT"), ("dur", "REAL"), ("live", "TEXT")] where !have.contains(name) {
            try db.exec("ALTER TABLE photos ADD COLUMN \(name) \(type)")
        }
        try db.exec("CREATE INDEX IF NOT EXISTS photos_stack ON photos(stack)")
    }

    let store: Store

    private init() {
        let path = Catalog.folder.appendingPathComponent("catalog.db").path
        do {
            store = try Store(path: path, schema: Catalog.schema, migrate: Catalog.migrate)
        } catch {     // a damaged replica is not worth repairing: start again
            for suffix in ["", "-wal", "-shm"] { try? FileManager.default.removeItem(atPath: path + suffix) }
            store = try! Store(path: path, schema: Catalog.schema, migrate: Catalog.migrate)
        }
    }

    // MARK: writing (SyncEngine)

    /// Lower-cased with everything but letters and digits turned into single spaces, and a space at each
    /// end, so `LIKE '% san diego %'` matches whole words only.
    static func words(_ s: String) -> String {
        var out = " ", gap = true
        for ch in s.lowercased() {
            if ch.isLetter || ch.isNumber { out.append(ch); gap = false }
            else if !gap { out.append(" "); gap = true }
        }
        return gap ? out : out + " "
    }

    static func monthNumber(_ when: String) -> Int {
        let p = when.split(separator: "-", maxSplits: 2)
        guard p.count >= 2, let y = Int(p[0]), let m = Int(p[1].prefix(2)), y > 1900, (1...12).contains(m) else { return -1 }
        return y * 12 + m - 1
    }

    private static let upsert = """
        INSERT INTO photos (sha, ext, taken, local, tz, whenk, ym, lat, lon, descr, fav, dup, w, h, ar, path, camera, lens,
            focal, focal35, fnumber, exposure, iso, place, city, region, country, county, place_inferred, event, event_title,
            trip, trip_title, people, albums, place_lc, place_words, camera_lc, albums_lc, people_lc, epoch, stack, best,
            kind, dur, live)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(sha) DO UPDATE SET ext=excluded.ext, taken=excluded.taken, local=excluded.local, tz=excluded.tz,
            whenk=excluded.whenk, ym=excluded.ym, lat=excluded.lat, lon=excluded.lon, descr=excluded.descr, fav=excluded.fav,
            dup=excluded.dup, w=excluded.w, h=excluded.h, ar=excluded.ar, path=excluded.path, camera=excluded.camera,
            lens=excluded.lens, focal=excluded.focal, focal35=excluded.focal35, fnumber=excluded.fnumber,
            exposure=excluded.exposure, iso=excluded.iso, place=excluded.place, city=excluded.city, region=excluded.region,
            country=excluded.country, county=excluded.county, place_inferred=excluded.place_inferred, event=excluded.event,
            event_title=excluded.event_title, trip=excluded.trip, trip_title=excluded.trip_title, people=excluded.people,
            albums=excluded.albums, place_lc=excluded.place_lc, place_words=excluded.place_words, camera_lc=excluded.camera_lc,
            albums_lc=excluded.albums_lc, people_lc=excluded.people_lc, epoch=excluded.epoch,
            stack=excluded.stack, best=excluded.best, kind=excluded.kind, dur=excluded.dur, live=excluded.live
        """

    /// One page of the server's feed. Runs inside the caller's transaction.
    static func apply(_ db: SQLite, rows: [[String: Any]], deleted: [String], epoch: String) throws {
        for sha in deleted {
            guard let id = try db.one("SELECT id FROM photos WHERE sha=?", [sha], { $0.int(0) }) else { continue }
            try remove(db, id)
        }
        for j in rows {
            let p = Photo(json: j)
            guard p.sha.count == 64 else { continue }
            let placeText = [p.place, p.county].compactMap { $0 }.joined(separator: " ")
            let json = { (xs: [String]) in String(data: (try? JSONSerialization.data(withJSONObject: xs)) ?? Data("[]".utf8), encoding: .utf8) }
            try db.run(upsert, [
                p.sha, p.ext, p.taken, p.local, p.tz, p.when, monthNumber(p.when), p.lat, p.lon, p.desc, p.fav,
                (j["dup"] as? Bool) ?? false, p.w, p.h, p.ar, p.path, p.camera, p.lens, p.focal, p.focal35, p.fnumber,
                p.exposure, p.iso, p.place, p.city, p.region, p.country, p.county, p.placeInferred, p.event, p.eventTitle,
                p.trip, p.tripTitle, json(p.people), json(p.albums), placeText.lowercased(), words(placeText),
                [p.camera, p.lens].compactMap { $0 }.joined(separator: " ").lowercased(),
                p.albums.joined(separator: " | ").lowercased(), p.people.joined(separator: " | ").lowercased(), epoch,
                p.stackOf, p.best, p.video ? "video" : nil, p.dur, p.live])
            guard let id = try db.one("SELECT id FROM photos WHERE sha=?", [p.sha], { $0.int(0) }) else { continue }
            try db.run("DELETE FROM tags WHERE id=?", [id])
            try db.run("DELETE FROM fts WHERE rowid=?", [id])
            for name in Set(p.people) { try db.run("INSERT INTO tags VALUES ('person', ?, ?)", [name, id]) }
            for name in Set(p.albums) { try db.run("INSERT INTO tags VALUES ('album', ?, ?)", [name, id]) }
            let text = ([p.desc, p.filename, p.camera, p.lens, p.place, p.county, p.eventTitle, p.tripTitle].compactMap { $0 }
                        + p.people + p.albums).joined(separator: " \n ")
            try db.run("INSERT INTO fts (rowid, text) VALUES (?, ?)", [id, text])
        }
    }

    private static func remove(_ db: SQLite, _ id: Int64) throws {
        try db.run("DELETE FROM tags WHERE id=?", [id])
        try db.run("DELETE FROM fts WHERE rowid=?", [id])
        try db.run("DELETE FROM photos WHERE id=?", [id])
    }

    /// After a full fetch under a new epoch: whatever the server did not send is gone.
    static func dropOtherEpochs(_ db: SQLite, keep epoch: String) throws {
        for id in try db.query("SELECT id FROM photos WHERE epoch IS NOT ?", [epoch], { $0.int(0) }) { try remove(db, id) }
    }

    static func get(_ db: SQLite, _ k: String) throws -> String? {
        try db.one("SELECT v FROM kv WHERE k=?", [k]) { $0.string(0) } ?? nil
    }

    static func set(_ db: SQLite, _ k: String, _ v: String) throws {
        try db.run("INSERT OR REPLACE INTO kv VALUES (?, ?)", [k, v])
    }

    // MARK: reading

    func count() async -> Int {
        (try? await store.read { try $0.one("SELECT count(*) FROM photos") { Int($0.int(0)) } ?? 0 }) ?? 0
    }

    func photo(_ sha: String) async -> Photo? {
        try? await store.read { db in
            guard var p = try db.one("SELECT \(Photo.columns) FROM photos WHERE sha=?", [sha], { Photo(row: $0) }) else { return nil }
            if let s = p.stackOf { p.stackN = try db.one("SELECT count(*) FROM photos WHERE stack=?", [s]) { Int($0.int(0)) } ?? 0 }
            return p
        }
    }

    /// Has the server said which photos are best. Until it has, there is no "Best first".
    func hasBest() async -> Bool {
        ((try? await store.read { try $0.one("SELECT 1 FROM photos WHERE best IS NOT NULL LIMIT 1") { _ in true } }) ?? nil) ?? false
    }

    func trips() async -> [Trip] {
        let text = (try? await store.read { try Catalog.get($0, "trips") }) ?? nil
        guard let data = text?.data(using: .utf8), let list = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return [] }
        return list.compactMap { t in
            guard let id = (t["id"] as? NSNumber)?.int64Value else { return nil }
            return Trip(id: id, title: t["title"] as? String ?? "Trip", start: t["start"] as? String ?? "", end: t["end"] as? String ?? "",
                        n: (t["n"] as? NSNumber)?.intValue ?? 0, cover: t["cover"] as? String ?? "")
        }
    }
}
