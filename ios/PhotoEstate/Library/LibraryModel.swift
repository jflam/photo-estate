import Foundation
import SwiftUI

/// What the grid shows: the photos, how they are grouped, and whether this is a new list (scroll to
/// the top) or the same list grown or refreshed (stay put).
struct GridData: Equatable {
    var tiles: [Tile] = []
    var grain = 7             // 0: one flat list (ranked); 7: by month; 10: by day
    var id = 0                // changes with every new list
    var reset = 0             // changes when the question changed, not just the answer

    static func == (a: GridData, b: GridData) -> Bool { a.id == b.id }
}

/// How far out the Library is zoomed, as in Photos: a card a year, a card a month, or every photo.
enum Scale: String, CaseIterable, Identifiable {
    case years, months, all
    var id: String { rawValue }
    var title: String { switch self { case .years: "Years"; case .months: "Months"; case .all: "All" } }
    var grain: Int { switch self { case .years: 4; case .months: 7; case .all: 0 } }
}

/// One year's or one month's photos, as a card: its key ("2019", "2019-07"), the photo on the
/// front, how many and where.
struct Period: Identifiable, Equatable {
    let key: String
    let cover: Tile
    let count: Int
    let places: String
    var id: String { key }
    var back: Scale { key.count > 4 ? .months : .years }      // the view this card is in
}

/// Where the grid should scroll to: the section whose key starts with `key`. `id` changes with every ask.
struct Jump: Equatable {
    var key = ""
    var id = 0
}

/// The Library tab's state: the question (`query`) and its answer (`grid`).
///
/// Browsing, filters, names, places and dates are answered from the phone's catalog, online or not.
/// Describing a photo ("dog in the snow") and "More like this" rank by what photos look like, which
/// needs the image model on the server; without the server, a description falls back to matching its
/// words against names, places, albums, captions and file names.
@MainActor
final class LibraryModel: ObservableObject {
    @Published var query = Query() { didSet { if query != oldValue { reload(reset: true) } } }
    @Published private(set) var grid = GridData()
    @Published private(set) var plan = Plan()
    @Published private(set) var title = "Library"
    @Published private(set) var subtitle = ""
    @Published private(set) var notice: String?
    @Published private(set) var ranked = false
    @Published private(set) var loading = true
    @Published private(set) var order = Order.new
    /// Has the server said which photos are best: without that there is no "Best first" to offer.
    @Published private(set) var hasBest = false
    @Published private(set) var canGoBack = false
    @Published private(set) var vocab = Vocab()
    @Published private(set) var trips: [Trip] = []
    @Published var hideDups = UserDefaults.standard.object(forKey: "view.hideDups") as? Bool ?? true {
        didSet { UserDefaults.standard.set(hideDups, forKey: "view.hideDups"); reload(reset: false) }
    }
    /// Browsing and filtering only: a described search ("boarding pass") still finds screenshots.
    @Published var hideShots = UserDefaults.standard.object(forKey: "view.hideShots") as? Bool ?? true {
        didSet { UserDefaults.standard.set(hideShots, forKey: "view.hideShots"); reload(reset: false) }
    }
    @Published var scale = Scale(rawValue: UserDefaults.standard.string(forKey: "view.scale") ?? "") ?? .all {
        didSet { UserDefaults.standard.set(scale.rawValue, forKey: "view.scale") }
    }
    @Published private(set) var years: [Period] = []
    @Published private(set) var months: [Period] = []
    @Published private(set) var jump = Jump()
    /// The cards tapped to get here, oldest first: what a swipe from the left edge goes back through.
    @Published private(set) var trail: [Period] = []
    /// Where Years or Months opens: the first card whose key starts with this.
    @Published private(set) var anchor: String?

    /// Years, Months or All from the bar: a fresh start, nothing to go back to.
    func pick(_ s: Scale) {
        trail = []
        anchor = nil
        withAnimation(.snappy(duration: 0.25)) { scale = s }
    }

    /// One step back, like a browser: to the view the last card was tapped in, at that card.
    func retrace() {
        guard let p = trail.popLast() else { return }
        anchor = p.key
        scale = p.back
    }
    @Published var rowHeight = UserDefaults.standard.object(forKey: "view.rowHeight") as? Double ?? 128 {
        didSet { UserDefaults.standard.set(rowHeight, forKey: "view.rowHeight") }
    }

    private var gen = 0
    private var history: [Query] = []
    private var vocabRevision = -1
    private var served: [String: Photo] = [:]     // rows the server sent with a ranked answer
    private var next: Int?                        // where the server's ranked list continues
    private var matching = 0
    private var pending = 0                       // of those, on this iPhone only
    private var total = 0                         // what the question selects with nothing hidden: for "of N"
    private var loadingMore = false

    // MARK: changing the question

    /// Replace the question, remembering the old one for Back.
    func go(_ q: Query) {
        guard q != query else { return }
        history.append(query)
        if history.count > 30 { history.removeFirst() }
        canGoBack = true
        query = q
    }

    func back() {
        guard let q = history.popLast() else { return }
        canGoBack = !history.isEmpty
        query = q
    }

    func clear() { go(Query()) }

    /// A year's card opens its months; a month's card opens its photos.
    func open(_ p: Period) {
        Haptics.tap()
        jump = Jump(key: p.key, id: jump.id + 1)
        trail.append(p)
        anchor = p.key
        withAnimation(.snappy(duration: 0.25)) { scale = p.key.count > 4 ? .all : .months }
    }

    func search(_ text: String) {
        var q = query
        q.text = text.trimmingCharacters(in: .whitespaces)
        q.plain = false
        q.similar = nil
        go(q)
    }

    func toggle(_ f: Facet, _ value: String) {
        var q = query
        q.toggle(f, value)
        query = q                                  // refining: not a step to go Back through
    }

    func photo(_ sha: String) async -> Photo? {
        if LocalPhotos.id(sha) != nil { return LocalPhotos.shared.photo(sha) }
        if let p = await Catalog.shared.photo(sha) { return p }
        return served[sha]
    }

    // MARK: answering it

    func catalogChanged() { reload(reset: false) }

    func reload(reset: Bool) {
        gen += 1
        let g = gen
        loading = true
        next = nil                                 // the old list's place: no page is fetched from it while this loads
        Task {
            let rev = SyncEngine.shared.revision
            if vocabRevision != rev {
                vocab = await Catalog.shared.vocab()
                trips = await Catalog.shared.trips()
                hasBest = await Catalog.shared.hasBest()
                vocabRevision = rev
            }
            guard g == gen else { return }
            let q = query
            var plan = Reader.plan(q, vocab)
            var tiles: [Tile] = []
            var note: String?
            var isRanked = false
            var pending = 0
            let scope = await Catalog.shared.scope(plan)
            // A stack shows its best photo first; an event or a trip reads from its start; anything else, newest first.
            var order = q.order ?? (q.stack != nil ? .best : scope != nil ? .old : .new)
            if order == .best && !hasBest { order = scope != nil || q.stack != nil ? .old : .new }
            let old = order == .old
            let collapse = hideDups && q.stack == nil       // a stack's own photos are never behind one another
            if q.similar != nil || !plan.text.isEmpty {
                do {
                    let page = try await Remote.search(q, cursor: 0, dedupe: collapse)
                    guard g == gen else { return }
                    SyncEngine.shared.markOnline(true)
                    served = Dictionary(page.photos.map { ($0.sha, $0) }, uniquingKeysWith: { a, _ in a })
                    tiles = page.photos.map(LibraryModel.tile)
                    next = page.next
                    matching = page.matching
                    total = matching
                    plan.text = page.text; plan.names = page.names; plan.places = page.places
                    isRanked = true
                } catch {
                    guard g == gen else { return }
                    SyncEngine.shared.markOnline(false)
                    next = nil
                    if q.similar != nil {
                        note = "“More like this” compares what photos look like, which needs your server. It isn't reachable right now."
                    } else {
                        tiles = await Catalog.shared.tiles(plan, oldFirst: old, hideDups: false, words: Reader.match(plan.text) ?? "\"\"")
                        note = "Offline: showing photos whose people, places, albums, captions or file names contain “\(plan.text)”. Finding photos by what's in them needs your server."
                    }
                    matching = tiles.count
                    total = matching
                }
            } else {
                tiles = await Catalog.shared.tiles(plan, oldFirst: old, hideDups: collapse, hideShots: hideShots)
                let mine = LocalPhotos.shared.tiles(for: q, hideShots: hideShots)        // on this iPhone, not yet on the server
                tiles = mergeByTime(tiles, mine, oldFirst: old)
                // Best first is per view: in the whole library it is the best of each month, the months in order as
                // ever; in anything asked for (a person, a trip, a year, a stack) it is the best of all of it.
                if order == .best { tiles = LibraryModel.bestFirst(tiles, within: q.isEmpty ? 7 : nil) }
                pending = mine.count
                next = nil
                matching = tiles.count
                total = await Catalog.shared.count(plan) + LocalPhotos.shared.tiles(for: q).count
            }
            guard g == gen else { return }
            // The count on a tile says "there are more behind this one": not so when stacks are spread out.
            if !collapse { for i in tiles.indices where tiles[i].stack != 0 { tiles[i].stack = 0 } }
            self.plan = plan
            ranked = isRanked
            self.order = order
            notice = note
            self.pending = pending
            var grain = 7
            let flat = order == .best && !q.isEmpty      // not in time order: no months or days to head
            if isRanked || flat { grain = 0 }
            else if scope != nil { grain = 10 }
            else if let a = q.from, let b = q.to, b - a <= 1 { grain = 10 }
            grid = GridData(tiles: tiles, grain: grain, id: grid.id + 1, reset: grid.reset + (reset ? 1 : 0))
            if !isRanked && !flat {
                years = LibraryModel.periods(tiles, 4)
                months = LibraryModel.periods(tiles, 7)
            }
            describe(q, plan, scope)
            loading = false
        }
    }

    /// The grid is near the end of a ranked list: fetch the server's next page.
    func loadMore() {
        guard ranked, let cursor = next, !loadingMore else { return }
        loadingMore = true
        let g = gen
        Task {
            defer { loadingMore = false }
            // Only onto the list it was asked for, still at the place it was asked from.
            guard let page = try? await Remote.search(query, cursor: cursor, dedupe: hideDups), g == gen, next == cursor else { return }
            for p in page.photos { served[p.sha] = p }
            next = page.next
            var more = page.photos.map(LibraryModel.tile)
            if !(hideDups && query.stack == nil) { for i in more.indices { more[i].stack = 0 } }
            grid = GridData(tiles: grid.tiles + more, grain: 0, id: grid.id + 1, reset: grid.reset)
        }
    }

    /// The best photos first (ties keep their order). `within` 7: inside each month only, for a list in time
    /// order, so the months stay where they are.
    static func bestFirst(_ tiles: [Tile], within grain: Int?) -> [Tile] {
        // Sorted by place, not in place: Swift's sort is not promised to keep ties in order.
        func sorted(_ run: ArraySlice<Tile>) -> [Tile] {
            run.enumerated().sorted { a, b in a.element.best != b.element.best ? a.element.best > b.element.best : a.offset < b.offset }.map(\.element)
        }
        guard let grain else { return sorted(tiles[...]) }
        var out: [Tile] = []
        out.reserveCapacity(tiles.count)
        var i = 0
        while i < tiles.count {
            let key = tiles[i].when.prefix(grain)
            var j = i + 1
            while j < tiles.count, tiles[j].when.prefix(grain) == key { j += 1 }
            out += sorted(tiles[i..<j])
            i = j
        }
        return out
    }

    /// The photos grouped by year (`grain` 4) or month (7), in the list's order. The card's photo is a
    /// favourite if there is one, a landscape one if not; among those the best one, or the one nearest
    /// the middle of the stretch when the server has not said which photos are best.
    static func periods(_ tiles: [Tile], _ grain: Int) -> [Period] {
        var out: [Period] = []
        var i = 0
        while i < tiles.count {
            let key = tiles[i].when.prefix(grain)
            var j = i
            var cities: [Substring: Int] = [:]
            while j < tiles.count, tiles[j].when.prefix(grain) == key {
                if let c = tiles[j].city?.split(separator: ",").first { cities[c, default: 0] += 1 }
                j += 1
            }
            if key.count == grain {                          // undated photos have no year or month
                let mid = (i + j) / 2
                var best = i, score = (-1, -Float.infinity, -Int.max)
                for k in i..<j {
                    let t = tiles[k]
                    let s = ((t.fav ? 2 : 0) + (t.ar >= 1.2 && t.ar <= 1.9 ? 1 : 0), t.best, -abs(k - mid))
                    if s > score { score = s; best = k }
                }
                let top = cities.sorted { ($0.value, $1.key) > ($1.value, $0.key) }.prefix(grain == 4 ? 3 : 2).map { String($0.key) }
                out.append(Period(key: String(key), cover: tiles[best], count: j - i, places: top.joined(separator: " · ")))
            }
            i = j
        }
        return out
    }

    /// A count for the Library's heading, near enough: 81K, 8.1K, 950.
    static func about(_ n: Int) -> String {
        let r = n >= 10_000 ? (n + 500) / 1000 * 1000 : n >= 1000 ? (n + 50) / 100 * 100 : n
        return r.formatted(.number.notation(.compactName))
    }

    private static func tile(_ p: Photo) -> Tile {
        Tile(sha: p.sha, ar: Float(p.ar), when: p.when, city: p.city, fav: p.fav,
             stack: Int32(p.stackN), best: p.best.map { Float($0) } ?? -.infinity)
    }

    private func describe(_ q: Query, _ plan: Plan, _ scope: Scope?) {
        // What the view options hide (near-identical shots, screenshots) is said, so a count never reads as less than there is.
        let hidden = max(total - matching, 0)
        let noun = { (k: Int) in k == 1 ? "photo" : "photos" }
        let n = hidden > 0 ? "\(matching.formatted()) of \(total.formatted()) \(noun(total)) · \(hidden.formatted()) hidden"
                           : "\(matching.formatted()) \(noun(matching))"
        let both = { (xs: [String], word: String) -> String in
            xs.count > 1 ? xs.dropLast().joined(separator: ", ") + " \(word) " + xs.last! : xs.first ?? ""
        }
        let who = both(plan.names, "&"), place = both(plan.places, "or")
        let reading = [who, place.isEmpty ? "" : (who.isEmpty ? "" : "in ") + place].filter { !$0.isEmpty }.joined(separator: " ")
        var parts: [String] = []
        if !reading.isEmpty && plan.text.isEmpty {
            title = reading; parts = [n]
        } else if q.similar != nil {
            title = "More like this"; parts = ["Closest in look and subject"]
        } else if !plan.text.isEmpty {
            title = "“\(plan.text)”"
            parts = [matching == 0 ? "No matches" : ranked ? "\(n), best match first" : n]
            if !reading.isEmpty { parts.insert([who.isEmpty ? "" : "with \(who)", place.isEmpty ? "" : "in \(place)"].filter { !$0.isEmpty }.joined(separator: ", "), at: 0) }
        } else if q.stack != nil {
            title = "Stack of \(matching.formatted())"
            parts = ["Near-identical shots taken together"] + (order == .best ? ["best first"] : [])
        } else if let scope {
            title = scope.title.replacingOccurrences(of: "Trip to ", with: ""); parts = [Dates.span(scope.start, scope.end), n]
        } else if q.isFiltered {
            let places = (q.picked(.city) + q.picked(.region) + q.picked(.country)).map { $0.split(separator: ",").first.map(String.init) ?? $0 }
            var range = ""
            if q.from != nil || q.to != nil { range = Dates.range(q.from, q.to) }
            let bits = [q.picked(.person).joined(separator: " & "), places.joined(separator: ", "), q.picked(.album).joined(separator: ", "),
                        (q.picked(.camera) + q.picked(.lens)).joined(separator: ", "), range].filter { !$0.isEmpty }
            title = bits.isEmpty ? (q.fav ? "Favourites" : "Photos") : bits.joined(separator: " · ")
            parts = [n] + (q.fav && !bits.isEmpty ? ["favourites"] : [])
        } else {
            title = "Library"
            parts = hidden > 0 ? ["\(LibraryModel.about(matching)) of \(LibraryModel.about(total)) photos", "\(LibraryModel.about(hidden)) hidden"]
                               : ["\(LibraryModel.about(matching)) photos"]
        }
        parts += plan.applied
        if pending > 0 { parts.append("\(pending.formatted()) not uploaded") }
        subtitle = parts.filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

/// Where the app is, and the two lists it shows: the whole library in time order, and whatever was
/// asked for. Anything that names a set of photos (a place in a photo's details, "More like this")
/// goes through `find`, which shows that set in Search.
@MainActor
final class Router: ObservableObject {
    enum Tab: Hashable { case library, search }
    @Published var tab = Tab.library
    let library = LibraryModel()
    let search = LibraryModel()

    func find(_ q: Query) {
        search.go(q)
        tab = .search
    }
}

/// Catalog times are local wall-clock strings ("2019-07-14T12:22:33"); these turn them into words.
enum Dates {
    private static let utc = TimeZone(identifier: "UTC")!
    private static func formatter(_ template: String) -> DateFormatter {
        let f = DateFormatter()
        f.timeZone = utc
        f.setLocalizedDateFormatFromTemplate(template)
        return f
    }
    private static let month = formatter("MMMMyyyy"), day = formatter("EEEEdMMMMyyyy"), short = formatter("dMMMyyyy")
    private static let full = formatter("EEEEdMMMMyyyy"), time = formatter("jmm")

    static func date(_ s: String) -> Date? {
        let p = s.prefix(19).split(whereSeparator: { "-T: ".contains($0) }).compactMap { Int($0) }
        guard p.count >= 2 else { return nil }
        var c = DateComponents()
        c.year = p[0]; c.month = p[1]; c.day = p.count > 2 ? p[2] : 1
        c.hour = p.count > 3 ? p[3] : 0; c.minute = p.count > 4 ? p[4] : 0; c.second = p.count > 5 ? p[5] : 0
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = utc
        return cal.date(from: c)
    }

    /// A section's heading from its key: "2019-07" or "2019-07-14".
    static func heading(_ key: String) -> String {
        guard let d = date(key) else { return "Undated" }
        return (key.count > 7 ? day : month).string(from: d)
    }

    private static let shortMonth = formatter("MMMyyyy"), shortDay = formatter("dMMMyyyy")
    /// For the scrubber: "Jul 2019", or "14 Jul 2019" for a day.
    static func short(_ key: String) -> String { date(key).map { (key.count > 7 ? shortDay : shortMonth).string(from: $0) } ?? "Undated" }

    static func long(_ s: String) -> String { date(s).map { full.string(from: $0) } ?? "Undated" }
    static func clock(_ s: String) -> String { s.count >= 16 ? (date(s).map { time.string(from: $0) } ?? "") : "" }
    static func brief(_ s: String) -> String { date(s).map { short.string(from: $0) } ?? "" }

    static func span(_ a: String, _ b: String) -> String {
        let x = brief(a), y = brief(b)
        return x == y || y.isEmpty ? x : "\(x) – \(y)"
    }

    static func monthName(_ ym: Int) -> String { date(Query.month(ym)).map { month.string(from: $0) } ?? "" }

    static func range(_ from: Int?, _ to: Int?) -> String {
        switch (from, to) {
        case let (a?, b?) where a == b: return monthName(a)
        case let (a?, b?) where a % 12 == 0 && b == a + 11: return String(a / 12)
        case let (a?, b?): return "\(monthName(a)) – \(monthName(b))"
        case let (a?, nil): return "Since \(monthName(a))"
        case let (nil, b?): return "Until \(monthName(b))"
        default: return ""
        }
    }
}
