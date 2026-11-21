import Foundation
import SwiftUI

/// What the grid shows: the photos, how they are grouped, and whether this is a new list (scroll to
/// the top) or the same list grown or refreshed (stay put).
struct GridData: Equatable {
    var tiles: [Tile] = []
    var grain = 7             // 0: one flat list (ranked); 7: by month; 10: by day
    var id = 0                // changes with every new list
    var reset = 0             // changes when the question changed, not just the answer
    var runs: [Run]? = nil    // the list's own sections, in order (an album's): these head it, not months or days

    /// A stretch of the list under one caption.
    struct Run: Equatable {
        let title: String
        let count: Int
    }

    static func == (a: GridData, b: GridData) -> Bool { a.id == b.id }
}

/// How far out the Library is zoomed, as in Photos: a card a year, a card a month, or every photo. A month's
/// card opens that month alone (`month`), which is a place of its own and not one of the bar's three.
enum Scale: String, Identifiable {
    case years, months, all, month
    static let bar: [Scale] = [.years, .months, .all]
    var id: String { rawValue }
    var title: String { switch self { case .years: "Years"; case .months: "Months"; case .all: "All"; case .month: "Month" } }
    var isCards: Bool { self == .years || self == .months }
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
    /// How much the list shows, what the view options hide and the order it is in, as rows for the settings
    /// page: the Library says none of this under its own title, where it was noise above the photos.
    @Published private(set) var facts: [(label: String, value: String)] = []
    @Published private(set) var ranked = false
    @Published private(set) var loading = true
    @Published private(set) var order = Order.new
    /// The order in words when it is not the one this list would have anyway ("best first"), for the line
    /// under the title and the mark on the view button.
    @Published private(set) var orderNote: String?
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
    @Published var scale = Scale.bar.first { $0.rawValue == UserDefaults.standard.string(forKey: "view.scale") } ?? .all {
        didSet { if Scale.bar.contains(scale) { UserDefaults.standard.set(scale.rawValue, forKey: "view.scale") } }
    }
    /// The month showing when `scale` is `month` ("2019-07"), and its photos: the library's own list from
    /// where the month starts to where it ends, so its order is the Library's order (best first is this
    /// month's best first).
    @Published private(set) var monthKey: String?
    @Published private(set) var monthGrid = GridData()
    @Published private(set) var monthTitle = ""
    @Published private(set) var monthSubtitle = ""
    @Published private(set) var monthFooter: GridFooter?
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

    /// The Library remembers the order chosen for it, like the other view options. Search does not: what is
    /// asked for there has its own natural order (a trip from its start, a stack best first).
    private let remembersOrder: Bool

    init(remembersOrder: Bool = false) {
        self.remembersOrder = remembersOrder
        if remembersOrder { query.order = UserDefaults.standard.string(forKey: "view.order").flatMap(Order.init(rawValue:)) }
    }

    /// The order picked in the view menu.
    func setOrder(_ o: Order) {
        if remembersOrder { UserDefaults.standard.set(o.rawValue, forKey: "view.order") }
        var q = query
        q.order = o
        query = q
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

    /// A year's card opens its months; a month's card opens that month, alone.
    func open(_ p: Period) {
        Haptics.tap()
        trail.append(p)
        anchor = p.key
        let month = p.key.count > 4
        if month {
            monthKey = p.key
            remonth(reset: true)
        }
        withAnimation(.snappy(duration: 0.25)) { scale = month ? .month : .months }
    }

    /// The month before or after, in this one's place: Back is still the Months, at the month now showing.
    func goMonth(_ key: String) {
        guard scale == .month, key != monthKey else { return }
        monthKey = key
        anchor = key
        if let last = trail.last, last.key.count > 4, let p = months.first(where: { $0.key == key }) { trail[trail.count - 1] = p }
        remonth(reset: true)
    }

    /// The month showing, cut again from the library's list: it changed, or another month was asked for.
    private func remonth(reset: Bool) {
        guard let key = monthKey else { return }
        let tiles = grid.tiles.filter { $0.when.hasPrefix(key) }
        monthSeq += 1
        monthGrid = GridData(tiles: tiles, grain: 0, id: monthSeq, reset: monthGrid.reset + (reset ? 1 : 0))
        let card = months.first { $0.key == key }
        monthTitle = Dates.heading(key)
        let n = tiles.isEmpty ? "No photos" : "\(tiles.count.formatted()) \(tiles.count == 1 ? "photo" : "photos")"
        monthSubtitle = [card?.places ?? "", n, order == .best ? "best first" : order == .old ? "oldest first" : ""]
            .filter { !$0.isEmpty }.joined(separator: " · ")
        let keys = months.map(\.key).sorted()
        let i = keys.firstIndex(of: key) ?? keys.firstIndex { $0 > key } ?? keys.count
        let here = i < keys.count && keys[i] == key
        monthFooter = GridFooter(before: i > 0 ? keys[i - 1] : nil, after: i + (here ? 1 : 0) < keys.count ? keys[i + (here ? 1 : 0)] : nil)
    }
    private var monthSeq = 0

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
            let usual: Order = q.stack != nil ? .best : scope != nil ? .old : .new
            var order = (q.order == .match ? nil : q.order) ?? usual       // `match` is only for a described search
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
                    // The server chose the photos (what matches the words) and their order: closest match first,
                    // or the best of them first if that was asked for. "More like this" is by likeness only.
                    order = q.order == .best && hasBest && q.similar == nil ? .best : .match
                } catch {
                    guard g == gen else { return }
                    SyncEngine.shared.markOnline(false)
                    next = nil
                    if q.similar != nil {
                        note = "“More like this” compares what photos look like, which needs your server. It isn't reachable right now."
                    } else {
                        tiles = await Catalog.shared.tiles(plan, oldFirst: old, hideDups: false, words: Reader.match(plan.text) ?? "\"\"")
                        if order == .best { tiles = LibraryModel.bestFirst(tiles, within: nil) }
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
            let said = isRanked ? order == .match : order == usual
            orderNote = said ? nil : order == .best ? (q.isEmpty ? "best first in each month" : "best first") : order == .old ? "oldest first" : "newest first"
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
            remonth(reset: false)
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

    /// Can an album be made of the answer showing: it has photos the server holds, and it has an end ("More like
    /// this" has none: it is every photo, by likeness).
    var albumable: Bool { !loading && !query.isEmpty && query.similar == nil && grid.tiles.contains { !$0.isLocal } }

    /// What to call an album of this answer: the question, in words.
    var albumTitle: String { title.trimmingCharacters(in: CharacterSet(charactersIn: "“”")) }

    /// Every photo of the answer showing, in its order: not only those loaded so far. Photos that are on this
    /// iPhone only are left out, as the server has not got them to put in an album.
    func everySha() async throws -> [String] {
        let q = query, g = gen
        var shas = grid.tiles.filter { !$0.isLocal }.map(\.sha)
        var cursor = ranked ? next : nil
        while let at = cursor {
            let page = try await Remote.search(q, cursor: at, dedupe: hideDups && q.stack == nil)
            guard g == gen else { throw CancellationError() }      // the question changed meanwhile
            shas += page.photos.map(\.sha)
            cursor = page.next
        }
        return shas
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

    private static func tile(_ p: Photo) -> Tile { Tile(p) }

    private func describe(_ q: Query, _ plan: Plan, _ scope: Scope?) {
        // What the view options hide (near-identical shots, screenshots) is said, so a count never reads as less than there is.
        let hidden = max(total - matching, 0)
        let noun = { (k: Int) in q.videos ? (k == 1 ? "video" : "videos") : k == 1 ? "photo" : "photos" }
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
            parts = [matching == 0 ? "No matches" : ranked ? "\(n), \(order == .best ? "the closest matches, your best first" : "best match first")" : n]
            if !reading.isEmpty { parts.insert([who.isEmpty ? "" : "with \(who)", place.isEmpty ? "" : "in \(place)"].filter { !$0.isEmpty }.joined(separator: ", "), at: 0) }
        } else if q.stack != nil {
            title = "Stack of \(matching.formatted())"
            parts = ["Near-identical shots taken together"] + (order == .best && orderNote == nil ? ["best first"] : [])
        } else if let scope {
            title = scope.title.replacingOccurrences(of: "Trip to ", with: ""); parts = [Dates.span(scope.start, scope.end), n]
        } else if q.isFiltered {
            let places = (q.picked(.city) + q.picked(.region) + q.picked(.country)).map { $0.split(separator: ",").first.map(String.init) ?? $0 }
            var range = ""
            if q.from != nil || q.to != nil { range = Dates.range(q.from, q.to) }
            let bits = [q.picked(.person).joined(separator: " & "), places.joined(separator: ", "), q.picked(.album).joined(separator: ", "),
                        (q.picked(.camera) + q.picked(.lens)).joined(separator: ", "), range].filter { !$0.isEmpty }
            title = bits.isEmpty ? (q.fav && q.videos ? "Favourite videos" : q.fav ? "Favourites" : q.videos ? "Videos" : "Photos")
                                 : bits.joined(separator: " · ")
            parts = [n] + (q.fav && !bits.isEmpty ? ["favourites"] : []) + (q.videos && !bits.isEmpty ? ["videos"] : [])
        } else {
            title = "Library"
            parts = hidden > 0 ? ["\(LibraryModel.about(matching)) of \(LibraryModel.about(total)) photos", "\(LibraryModel.about(hidden)) hidden"]
                               : ["\(LibraryModel.about(matching)) photos"]
        }
        facts = [("Showing", hidden > 0 ? "\(matching.formatted()) of \(total.formatted()) photos" : "\(matching.formatted()) \(noun(matching))")]
            + (hidden > 0 ? [("Hidden by the view options", hidden.formatted())] : [])
            + [("Order", order == .best ? (q.isEmpty ? "Best first in each month" : "Best first") : order == .old ? "Oldest first" : "Newest first")]
        parts += plan.applied
        if let orderNote, !ranked { parts.append(orderNote) }      // a ranked list has said its order already
        if pending > 0 { parts.append("\(pending.formatted()) not uploaded") }
        subtitle = parts.filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

/// Where the app is, and the lists it shows: the whole library in time order, the albums, and whatever
/// was asked for. Anything that names a set of photos (a place in a photo's details, "More like this")
/// goes through `find`, which shows that set in Search.
@MainActor
final class Router: ObservableObject {
    enum Tab: Hashable { case library, albums, search }
    @Published var tab = Tab.library
    let library = LibraryModel(remembersOrder: true)
    let albums = AlbumsModel()
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
