import Foundation
import UIKit

/// The fields a search can be narrowed by. The raw values are the server's parameter names.
enum Facet: String, CaseIterable, Identifiable {
    case country, region, city, person, album, camera, lens, trip
    var id: String { rawValue }

    var title: String {
        switch self {
        case .country: "Country"
        case .region: "Region"
        case .city: "Place"
        case .person: "Person"
        case .album: "Album"
        case .camera: "Camera"
        case .lens: "Lens"
        case .trip: "Trip"
        }
    }
}

/// How a list is ordered: by time, or the best photos first; and for a described search, which the server
/// ranks, by how well each photo matches (`match`) or, of the photos that match, the best first.
enum Order: String { case new, old, best, match }

/// The whole question, as the web app's `S` (chapter 13 of the book).
struct Query: Equatable {
    var text = ""
    var plain = false                 // do not read names and places in `text`
    var similar: String?              // "More like this" for this photo
    var picks: [Facet: [String]] = [:]
    var event: Int64?
    var stack: String?                // the photos of one stack, named as the catalog names it
    var from: Int?                    // months as year*12 + month-1, inclusive
    var to: Int?
    var fav = false
    var order: Order?

    var isFiltered: Bool { picks.values.contains { !$0.isEmpty } || event != nil || stack != nil || from != nil || to != nil || fav }
    var isEmpty: Bool { text.isEmpty && similar == nil && !isFiltered }

    func picked(_ f: Facet) -> [String] { picks[f] ?? [] }

    mutating func toggle(_ f: Facet, _ value: String) {
        var xs = picked(f)
        if let i = xs.firstIndex(of: value) { xs.remove(at: i) } else { xs.append(value) }
        picks[f] = xs.isEmpty ? nil : xs
    }

    static func month(_ ym: Int) -> String { String(format: "%04d-%02d", ym / 12, ym % 12 + 1) }

    /// The same question as the server's query string, for searches only it can answer.
    func items(limit: Int, cursor: Int, dedupe: Bool) -> [URLQueryItem] {
        var out: [URLQueryItem] = []
        if !text.isEmpty { out.append(.init(name: "q", value: text)) }
        if plain { out.append(.init(name: "names", value: "0")) }
        if let similar { out.append(.init(name: "sha", value: similar)) }
        for f in Facet.allCases { for v in picked(f) { out.append(.init(name: f.rawValue, value: v)) } }
        if let event { out.append(.init(name: "event", value: String(event))) }
        if let from { out.append(.init(name: "from", value: Query.month(from))) }
        if let to { out.append(.init(name: "to", value: Query.month(to))) }
        if fav { out.append(.init(name: "fav", value: "1")) }
        if let stack { out.append(.init(name: "stack", value: stack)) }
        if let order { out.append(.init(name: "sort", value: order.rawValue)) }
        out += [.init(name: "limit", value: String(limit)), .init(name: "cursor", value: String(cursor)),
                .init(name: "dedupe", value: dedupe ? "1" : "0")]
        return out
    }
}

struct Clause {
    let key: String
    let sql: String
    let args: [Any?]
}

/// A query, read: one SQL condition per kind of filter, and whatever words are left to describe the photo.
struct Plan {
    var clauses: [Clause] = []
    var text = ""                     // left for the image model (or, offline, for matching words)
    var names: [String] = []          // people read from the typed words
    var places: [String] = []         // places read from the typed words
    var couldRead = false             // the words held names or places, read or not
    var applied: [String] = []        // typed filters, as "place:denmark"
    var scopeEvent: Int64?
    var scopeTrip: Int64?

    func condition(skip: String? = nil) -> (sql: String, args: [Any?]) {
        let used = clauses.filter { $0.key != skip }
        return (used.isEmpty ? "1" : used.map { "(\($0.sql))" }.joined(separator: " AND "), used.flatMap { $0.args })
    }
}

/// The words the library knows: people, places, albums, cameras. Read from the catalog, used to find
/// names and places in what is typed and to suggest filters. Rebuilt when the catalog changes.
struct Vocab {
    struct Entry {
        let facet: Facet
        let label: String
        let count: Int
        let lower: String
    }
    var entries: [Entry] = []
    var who: [String: String] = [:]         // "ben" and "ben lam" -> "Ben Lam"
    var places: [String: String] = [:]      // "san diego" -> "San Diego"
    var cities: Set<String> = []
    var regex: NSRegularExpression?

    static func load(_ db: SQLite) throws -> Vocab {
        var v = Vocab()
        for (f, sql) in [(Facet.person, "SELECT name, count(*) FROM tags WHERE kind='person' GROUP BY name"),
                         (.album, "SELECT name, count(*) FROM tags WHERE kind='album' GROUP BY name"),
                         (.city, "SELECT city, count(*) FROM photos WHERE city IS NOT NULL GROUP BY city"),
                         (.region, "SELECT region, count(*) FROM photos WHERE region IS NOT NULL GROUP BY region"),
                         (.country, "SELECT country, count(*) FROM photos WHERE country IS NOT NULL GROUP BY country"),
                         (.camera, "SELECT camera, count(*) FROM photos WHERE camera IS NOT NULL GROUP BY camera"),
                         (.lens, "SELECT lens, count(*) FROM photos WHERE lens IS NOT NULL GROUP BY lens")] {
            v.entries += try db.query(sql) { Entry(facet: f, label: $0.text(0), count: Int($0.int(1)), lower: $0.text(0).lowercased()) }
        }
        // First names stand for a person when only one person has that name; full names always do.
        var firsts: [String: [String]] = [:]
        let people = v.entries.filter { $0.facet == .person }.map(\.label).sorted()
        for n in people { firsts[n.split(separator: " ").first.map { $0.lowercased() } ?? "", default: []].append(n) }
        for (f, ns) in firsts where ns.count == 1 && !f.isEmpty { v.who[f] = ns[0] }
        for n in people { v.who[n.lowercased()] = n }
        for f in [Facet.city, .region, .country] {
            for e in v.entries where e.facet == f {
                let name = e.label.split(separator: ",").first.map { $0.trimmingCharacters(in: .whitespaces) } ?? e.label
                guard !name.isEmpty else { continue }
                if v.places[name.lowercased()] == nil { v.places[name.lowercased()] = name }
                if f == .city { v.cities.insert(name.lowercased()) }
            }
        }
        for k in v.who.keys { v.places[k] = nil }      // a person first
        let alt = { (keys: [String]) -> String in
            keys.isEmpty ? "(?!)" : keys.sorted { $0.count > $1.count }.map(NSRegularExpression.escapedPattern).joined(separator: "|")
        }
        v.regex = try? NSRegularExpression(
            pattern: "(?<![\\w'’])(?:(?<who>\(alt(Array(v.who.keys))))|(?<where>\(alt(Array(v.places.keys)))))(['’]s)?(?![\\w'’])",
            options: [.caseInsensitive])
        return v
    }

    /// Filters to offer for what has been typed so far: values holding every typed word, best first.
    func suggest(_ typed: String, trips: [Trip], limit: Int = 7) -> [(facet: Facet, label: String, value: String, count: Int, key: String)] {
        let words = typed.lowercased().split(separator: " ").map(String.init)
        guard !words.isEmpty else { return [] }
        var scored: [(Double, Facet, String, String, Int)] = []
        func score(_ lower: String, _ count: Int, _ f: Facet) -> Double? {
            var best = 0.0
            for w in words {
                guard let r = lower.range(of: w) else { return nil }
                let s = r.lowerBound == lower.startIndex ? 3.0 : (lower[lower.index(before: r.lowerBound)].isLetter ? 1.0 : 2.0)
                best = max(best, s)
            }
            return best * 20 + log10(Double(max(count, 1))) - (f == .lens ? 8 : 0)
        }
        for e in entries { if let s = score(e.lower, e.count, e.facet) { scored.append((s, e.facet, e.label, e.label, e.count)) } }
        for t in trips { if let s = score(t.title.lowercased(), t.n, .trip) { scored.append((s, .trip, t.title, String(t.id), t.n)) } }
        return scored.sorted { $0.0 > $1.0 }.prefix(limit).map { ($0.1, $0.2, $0.3, $0.4, "\($0.1.rawValue)/\($0.3)") }
    }
}

/// Reads a `Query` the way the server does (embed.py: parse, read_words, select), against the phone's catalog.
enum Reader {
    // First names that are also everyday words: a person only when capitalized ("Sunny") or "Sunny's".
    static let commonWords = Set("""
        sunny lily rose rosie daisy iris violet ivy holly heather hazel jasmine poppy ruby amber pearl \
        jade crystal coral olive berry autumn summer winter april may june august dawn eve faith hope joy grace \
        honey candy ginger pepper cookie buddy rocky max jack bill mark will pat sue bob rob rod guy ray art \
        frank earl duke king prince queen bishop hunter chase page brook river sky storm rain snow forest \
        glen dale cliff rocco sandy dusty misty penny chip bud bunny kitty angel star sterling destiny
        """.split(separator: " ").map(String.init))
    // Countries and states that are also everyday words: a place only when capitalized.
    static let commonPlaces: Set<String> = ["china", "turkey", "chad", "jordan", "georgia", "guinea", "japan", "chile", "cuba", "wales", "jersey"]
    static let filler: Set<String> = ["photo", "photos", "picture", "pictures", "pic", "pics", "of", "the", "all", "show", "me", "find", "trip", "trips", "visit"]
    static let nameJoiners: Set<String> = ["and", "&", "+", "with", ","]
    static let placeJoiners: Set<String> = ["in", "at", "from", "near", "around", "to", "and", "or", ","]
    private static let whoMark: Character = "\u{E000}", whereMark: Character = "\u{E001}"

    private static let filterRE = try! NSRegularExpression(
        pattern: "\\b(place|camera|album|person|people|year|date|event|trip|is):(\"([^\"]*)\"|(\\S+))", options: [.caseInsensitive])
    private static let tokenRE = try! NSRegularExpression(pattern: "[\u{E000}\u{E001}]|,|[^\\s,\u{E000}\u{E001}]+")
    private static let checker = UITextChecker()

    /// A city whose name is an ordinary lower-case word ("reading", "bath") is a place only when capitalized.
    @MainActor private static func isEverydayWord(_ w: String) -> Bool {
        let r = checker.rangeOfMisspelledWord(in: w, range: NSRange(location: 0, length: w.utf16.count), startingAt: 0, wrap: false, language: "en")
        return r.location == NSNotFound
    }

    /// "ben and matthew in san diego" -> (["Ben Lam", "Matthew Lam"], ["San Diego"], "").
    @MainActor static func readWords(_ text: String, _ vocab: Vocab) -> (names: [String], places: [String], rest: String) {
        guard let re = vocab.regex else { return ([], [], text) }
        let ns = text as NSString
        var names: [String] = [], places: [String] = [], parts: [String] = [], at = 0
        for m in re.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            let whoR = m.range(withName: "who"), whereR = m.range(withName: "where")
            let isWho = whoR.location != NSNotFound
            let word = ns.substring(with: isWho ? whoR : whereR)
            let key = word.lowercased()
            let capital = (word.first?.isUppercase ?? false) || m.range(at: 3).location != NSNotFound
            if isWho {
                if commonWords.contains(key) && !capital { continue }
                guard let val = vocab.who[key] else { continue }
                if !names.contains(val) { names.append(val) }
            } else {
                if !capital && !key.contains(" ") && (commonWords.contains(key) || commonPlaces.contains(key)
                                                      || (vocab.cities.contains(key) && isEverydayWord(key))) { continue }
                guard let val = vocab.places[key] else { continue }
                if !places.contains(val) { places.append(val) }
            }
            parts += [ns.substring(with: NSRange(location: at, length: m.range.location - at)), String(isWho ? whoMark : whereMark)]
            at = m.range.location + m.range.length
        }
        if names.isEmpty && places.isEmpty { return ([], [], text) }
        let joined = (parts + [ns.substring(from: at)]).joined(separator: " ")
        let toks = tokenRE.matches(in: joined, range: NSRange(location: 0, length: (joined as NSString).length))
            .map { (joined as NSString).substring(with: $0.range) }
        let joiners = [String(whoMark): nameJoiners, String(whereMark): placeJoiners]
        var keep: [String] = []
        for (i, t) in toks.enumerated() where joiners[t] == nil {
            let near = (i > 0 ? [toks[i - 1]] : []) + (i + 1 < toks.count ? [toks[i + 1]] : [])
            if near.contains(where: { joiners[$0]?.contains(t.lowercased()) ?? false }) { continue }
            keep.append(t)
        }
        let rest = keep.joined(separator: " ").trimmingCharacters(in: CharacterSet(charactersIn: " ,"))
        let everything = filler.union(nameJoiners).union(placeJoiners)
        let onlyFiller = rest.split(separator: " ").allSatisfy { everything.contains($0.lowercased()) }
        return (names, places, onlyFiller ? "" : rest)
    }

    private static func marks(_ n: Int) -> String { Array(repeating: "?", count: n).joined(separator: ",") }

    @MainActor static func plan(_ q: Query, _ vocab: Vocab) -> Plan {
        var p = Plan()
        for f in [Facet.country, .region, .city, .camera, .lens] where !q.picked(f).isEmpty {
            p.clauses.append(Clause(key: f.rawValue, sql: "p.\(f.rawValue) IN (\(marks(q.picked(f).count)))", args: q.picked(f)))
        }
        let trips = q.picked(.trip).compactMap { Int64($0) }
        if !trips.isEmpty { p.clauses.append(Clause(key: "trip", sql: "p.trip IN (\(marks(trips.count)))", args: trips)) }
        if !q.picked(.album).isEmpty {      // any of the albums
            p.clauses.append(Clause(key: "album", sql: "p.id IN (SELECT id FROM tags WHERE kind='album' AND name IN (\(marks(q.picked(.album).count))))",
                                    args: q.picked(.album)))
        }
        let together = { (names: [String]) in     // all of the people
            names.map { _ in "p.id IN (SELECT id FROM tags WHERE kind='person' AND name=?)" }.joined(separator: " AND ")
        }
        if !q.picked(.person).isEmpty { p.clauses.append(Clause(key: "person", sql: together(q.picked(.person)), args: q.picked(.person))) }
        if q.from != nil || q.to != nil {
            p.clauses.append(Clause(key: "date", sql: "p.ym BETWEEN ? AND ?", args: [q.from ?? 0, q.to ?? 1_000_000]))
        }
        if q.fav { p.clauses.append(Clause(key: "fav", sql: "p.fav=1", args: [])) }
        if let s = q.stack { p.clauses.append(Clause(key: "stack", sql: "p.stack=?", args: [s])) }
        if let e = q.event {
            p.clauses.append(Clause(key: "event", sql: "p.event=?", args: [e]))
            p.scopeEvent = e
        } else if trips.count == 1 {
            p.scopeTrip = trips[0]
        }

        // typed filters: place:oregon year:2010 is:fav
        let ns = q.text as NSString
        var typed: [String] = [], typedArgs: [Any?] = []
        for m in filterRE.matches(in: q.text, range: NSRange(location: 0, length: ns.length)) {
            let key = ns.substring(with: m.range(at: 1)).lowercased()
            let val = ns.substring(with: m.range(at: 3).location != NSNotFound ? m.range(at: 3) : m.range(at: 4))
            let v = val.lowercased().trimmingCharacters(in: .whitespaces)
            switch key {
            case "place": typed.append("instr(p.place_lc, ?) > 0"); typedArgs.append(v)
            case "camera": typed.append("instr(p.camera_lc, ?) > 0"); typedArgs.append(v)
            case "album": typed.append("instr(p.albums_lc, ?) > 0"); typedArgs.append(v)
            case "person", "people": typed.append("instr(p.people_lc, ?) > 0"); typedArgs.append(v)
            case "year":
                let parts = v.split(separator: "-", maxSplits: 1).map(String.init)
                guard let lo = parts.first.flatMap({ Int($0) }) else { continue }
                typed.append("CAST(substr(p.whenk, 1, 4) AS INTEGER) BETWEEN ? AND ?")
                typedArgs += [lo, parts.count > 1 ? Int(parts[1]) ?? lo : lo]
            case "date": typed.append("substr(p.whenk, 1, ?) = ?"); typedArgs += [v.count, v]
            case "event", "trip":
                guard let id = Int64(v) else { continue }
                typed.append("p.\(key)=?"); typedArgs.append(id)
                if key == "event" { p.scopeEvent = id } else { p.scopeTrip = id }
            case "is" where ["fav", "favorite", "favourite"].contains(v): typed.append("p.fav=1")
            default: continue
            }
            p.applied.append("\(key):\(val)")
        }
        if !typed.isEmpty { p.clauses.append(Clause(key: "typed", sql: typed.joined(separator: " AND "), args: typedArgs)) }
        var text = filterRE.stringByReplacingMatches(in: q.text, range: NSRange(location: 0, length: ns.length), withTemplate: " ")
        text = text.split(whereSeparator: \.isWhitespace).joined(separator: " ")

        let (names, places, rest) = readWords(text, vocab)
        p.couldRead = !names.isEmpty || !places.isEmpty
        if p.couldRead && !q.plain {
            p.names = names; p.places = places
            if !names.isEmpty { p.clauses.append(Clause(key: "names", sql: together(names), args: names)) }
            if !places.isEmpty {
                p.clauses.append(Clause(key: "places", sql: places.map { _ in "p.place_words LIKE ?" }.joined(separator: " OR "),
                                        args: places.map { "%\(Catalog.words($0))%" }))
            }
            text = rest
        }
        p.text = text
        return p
    }

    /// The words of a description as a full-text query: every word must appear, the last as a prefix.
    static func match(_ text: String) -> String? {
        let words = Catalog.words(text).split(separator: " ").filter { !filler.contains(String($0)) }
        guard !words.isEmpty else { return nil }
        return words.enumerated().map { i, w in "\"\(w)\"" + (i == words.count - 1 ? "*" : "") }.joined(separator: " ")
    }
}

struct Scope: Equatable {
    let title: String
    let start: String
    let end: String
}

/// Answers from the phone's catalog.
extension Catalog {
    /// Every photo the plan selects, in time order. `words`: also require these words (offline text search).
    func tiles(_ plan: Plan, oldFirst: Bool, hideDups: Bool, hideShots: Bool = false, words: String? = nil) async -> [Tile] {
        var (sql, args) = plan.condition()
        if let words { sql += " AND p.id IN (SELECT rowid FROM fts WHERE fts MATCH ?)"; args.append(words) }
        if hideDups { sql += " AND p.dup=0" }
        if hideShots { sql += " AND NOT \(Screenshots.sql)" }
        let size = "CASE WHEN p.stack IS NULL THEN 0 ELSE (SELECT count(*) FROM photos s WHERE s.stack=p.stack) END"
        let q = "SELECT p.sha, p.ar, p.whenk, p.city, p.fav, \(size), p.best FROM photos p WHERE \(sql) ORDER BY p.whenk='' , p.whenk \(oldFirst ? "ASC" : "DESC"), p.sha"
        return (try? await store.read { db in
            try db.query(q, args) {
                Tile(sha: $0.text(0), ar: Float($0.double(1)), when: $0.text(2), city: $0.string(3), fav: $0.bool(4),
                     stack: Int32($0.int(5)), best: $0.optDouble(6).map { Float($0) } ?? -.infinity)
            }
        }) ?? []
    }

    /// The photos of one stack, best first (the one on top first), for the viewer's strip.
    func stackTiles(_ stack: String) async -> [Tile] {
        (try? await store.read { db in
            try db.query("SELECT sha, ar, whenk, city, fav, best FROM photos WHERE stack=? ORDER BY best IS NULL, best DESC, whenk, sha", [stack]) {
                Tile(sha: $0.text(0), ar: Float($0.double(1)), when: $0.text(2), city: $0.string(3), fav: $0.bool(4),
                     best: $0.optDouble(5).map { Float($0) } ?? -.infinity)
            }
        }) ?? []
    }

    /// These photos as tiles, by id: what an album's list of ids is drawn from. Ids the catalog does not
    /// hold (hidden photos, ones not yet fetched) are simply not in the answer.
    func tiles(of shas: [String]) async -> [String: Tile] {
        (try? await store.read { db in
            var out: [String: Tile] = [:]
            var at = 0
            while at < shas.count {
                let chunk = Array(shas[at..<min(at + 400, shas.count)])
                at += chunk.count
                let marks = Array(repeating: "?", count: chunk.count).joined(separator: ",")
                for t in try db.query("SELECT sha, ar, whenk, city, fav, best FROM photos WHERE sha IN (\(marks))", chunk, {
                    Tile(sha: $0.text(0), ar: Float($0.double(1)), when: $0.text(2), city: $0.string(3), fav: $0.bool(4),
                         best: $0.optDouble(5).map { Float($0) } ?? -.infinity)
                }) { out[t.sha] = t }
            }
            return out
        }) ?? [:]
    }

    /// How many photos the plan selects, nothing hidden: what a heading's "of N" counts.
    func count(_ plan: Plan) async -> Int {
        let (sql, args) = plan.condition()
        return (try? await store.read { try $0.one("SELECT count(*) FROM photos p WHERE \(sql)", args) { Int($0.int(0)) } ?? 0 }) ?? 0
    }

    /// Counts per value of a facet under the plan; a facet ignores its own selection (people do not:
    /// choosing a second person means photos of them together).
    func counts(_ facet: Facet, _ plan: Plan) async -> [(value: String, count: Int)] {
        let (sql, args) = plan.condition(skip: facet == .person ? nil : facet.rawValue)
        let q: String
        switch facet {
        case .person, .album:
            q = "SELECT t.name, count(*) FROM tags t JOIN photos p ON p.id=t.id WHERE t.kind='\(facet.rawValue)' AND \(sql) GROUP BY t.name ORDER BY 2 DESC, 1"
        default:
            q = "SELECT p.\(facet.rawValue), count(*) FROM photos p WHERE p.\(facet.rawValue) IS NOT NULL AND \(sql) GROUP BY 1 ORDER BY 2 DESC, 1"
        }
        return (try? await store.read { db in try db.query(q, args) { ($0.text(0), Int($0.int(1))) } }) ?? []
    }

    /// Photos per month under the plan, ignoring its date range: [(year*12+month-1, count)], oldest first.
    func months(_ plan: Plan) async -> [(ym: Int, count: Int)] {
        let (sql, args) = plan.condition(skip: "date")
        return (try? await store.read { db in
            try db.query("SELECT p.ym, count(*) FROM photos p WHERE p.ym >= 0 AND \(sql) GROUP BY 1 ORDER BY 1", args) { (Int($0.int(0)), Int($0.int(1))) }
        }) ?? []
    }

    func scope(_ plan: Plan) async -> Scope? {
        let (col, title, id): (String, String, Int64)
        if let e = plan.scopeEvent { (col, title, id) = ("event", "event_title", e) }
        else if let t = plan.scopeTrip { (col, title, id) = ("trip", "trip_title", t) }
        else { return nil }
        return try? await store.read { db in
            try db.one("SELECT max(\(title)), min(nullif(whenk, '')), max(whenk), count(*) FROM photos WHERE \(col)=?", [id]) { r -> Scope? in
                r.int(3) == 0 ? nil : Scope(title: r.string(0) ?? "Untitled", start: r.text(1), end: r.text(2))
            } ?? nil
        }
    }

    func vocab() async -> Vocab {
        (try? await store.read { try Vocab.load($0) }) ?? Vocab()
    }
}

/// Searches only the server can answer: ranking by what a photo looks like needs the image model and
/// every photo's vector. The answer has the same rows as the catalog.
enum Remote {
    struct Page {
        var photos: [Photo] = []
        var next: Int?
        var matching = 0
        var text = ""
        var names: [String] = []
        var places: [String] = []
        var filters: [String] = []
    }

    private static let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 12
        return URLSession(configuration: c)
    }()

    static func search(_ q: Query, cursor: Int, dedupe: Bool) async throws -> Page {
        var url = URLComponents(url: Server.baseURL.appendingPathComponent(q.similar == nil ? "api/search" : "api/similar"),
                                resolvingAgainstBaseURL: false)!
        url.queryItems = q.items(limit: cursor == 0 ? 300 : 500, cursor: cursor, dedupe: dedupe)
        let (data, resp) = try await session.data(from: url.url!)
        guard (resp as? HTTPURLResponse)?.statusCode == 200, let j = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw URLError(.badServerResponse)
        }
        return Page(photos: (j["results"] as? [[String: Any]] ?? []).map(Photo.init(json:)),
                    next: (j["next"] as? NSNumber)?.intValue, matching: (j["matching"] as? NSNumber)?.intValue ?? 0,
                    text: j["text"] as? String ?? "", names: j["people"] as? [String] ?? [], places: j["places"] as? [String] ?? [],
                    filters: j["filters"] as? [String] ?? [])
    }
}
