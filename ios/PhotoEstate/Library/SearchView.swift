import SwiftUI

/// The Search tab: finding photos. With nothing asked, it lays out the threads to pull (trips, years,
/// places, people); with a question, it shows the answer as a grid, with the filters above it.
struct SearchView: View {
    @ObservedObject var model: LibraryModel
    @EnvironmentObject private var router: Router
    @ObservedObject private var sync = SyncEngine.shared
    @State private var draft = ""
    @State private var sheet: Pill?
    @State private var headerHeight: CGFloat = 0
    @State private var searching = false

    var body: some View {
        NavigationStack {
            Group {
                if model.query.isEmpty {
                    BrowseView(model: model, sheet: $sheet) { idea in draft = idea; model.search(idea) }
                } else {
                    results
                }
            }
            .background(Color.paper)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar(model.query.isEmpty ? .hidden : .visible, for: .navigationBar)
            .toolbar {
                if !model.query.isEmpty {
                    ToolbarItem(placement: .topBarLeading) {
                        Button { draft = ""; if model.canGoBack { model.back() } else { model.clear() } } label: { Image(systemName: "chevron.backward").foregroundStyle(Color.primary) }
                            .accessibilityLabel("Back")
                    }
                    ToolbarItem(placement: .topBarTrailing) { ViewMenu(model: model) }
                }
            }
        }
        .searchable(text: $draft, isPresented: $searching, prompt: "Describe a photo, or a name")
        .searchSuggestions { suggestions }
        .onSubmit(of: .search) { searching = false; model.search(draft) }
        .onChange(of: model.query.text) { _, t in if t != draft { draft = t } }
        .sheet(item: $sheet) { pill in
            FilterSheet(pill: pill, model: model).presentationDetents([.medium, .large]).presentationCornerRadius(28)
        }
        .onAppear { model.reload(reset: false) }
        .onChange(of: sync.revision) { _, _ in model.catalogChanged() }
    }

    // MARK: the answer

    private var results: some View {
        GridView(data: model.grid, rowHeight: model.rowHeight, title: model.title, subtitle: model.subtitle, topInset: headerHeight,
                 onNearEnd: { model.loadMore() },
                 onRefresh: { ImageStore.shared.retryNetwork(); await sync.sync() },
                 onRowHeight: { model.rowHeight = $0 },
                 viewer: { session in AnyView(ViewerScreen(session: session, model: model, route: router.find)) })
            .ignoresSafeArea()
            .overlay { if model.grid.tiles.isEmpty && !model.loading { nothing } }
            .overlay(alignment: .top) { header.onGeometryChange(for: CGFloat.self) { $0.size.height } action: { headerHeight = $0 } }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) { ForEach(Pill.allCases.sorted { a, b in (a.value(model.query, trips: model.trips) != nil ? 0 : 1) < (b.value(model.query, trips: model.trips) != nil ? 0 : 1) }) { chip($0) } }
                    .padding(.horizontal, 14).animation(.snappy, value: model.query)
            }
            if !sync.online || model.notice != nil || (model.plan.couldRead && model.query.similar == nil) {
                VStack(alignment: .leading, spacing: 4) {
                    if let notice = model.notice {
                        Label(notice, systemImage: "wifi.slash").fixedSize(horizontal: false, vertical: true)
                    } else if !sync.online {
                        Label("Offline: from what is on this iPhone", systemImage: "wifi.slash").foregroundStyle(Color.sun)
                    }
                    if model.plan.couldRead && model.query.similar == nil {
                        Button(model.query.plain ? "Read the names and places in it instead" : "Search for the words instead") {
                            var q = model.query
                            q.plain.toggle()
                            model.go(q)
                        }
                    }
                }
                .font(.footnote)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 16)
            }
        }
        .padding(.top, 6)
        .padding(.bottom, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.paper.opacity(0.92))
        .background(.ultraThinMaterial)
    }

    private func chip(_ pill: Pill) -> some View {
        let value = pill.value(model.query, trips: model.trips)
        return Button {
            Haptics.tick()
            if pill == .fav { var q = model.query; q.fav.toggle(); model.query = q } else { sheet = pill }
        } label: {
            HStack(spacing: 5) {
                if let symbol = pill.symbol { Image(systemName: value == nil ? symbol : "\(symbol).fill").imageScale(.small) }
                Text(value ?? pill.title).lineLimit(1)
                if value != nil && pill != .fav {
                    Image(systemName: "xmark").font(.caption2.weight(.bold)).opacity(0.7)
                        .onTapGesture { model.query = pill.cleared(model.query) }
                        .accessibilityLabel("Clear \(pill.title)")
                }
            }
            .font(.subheadline.weight(.medium))
            .padding(.horizontal, 13)
            .padding(.vertical, 8)
            .foregroundStyle(value == nil ? Color.primary : Color.black)
            .background(value == nil ? Color.well : Color.sunLit, in: Capsule())
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder private var nothing: some View {
        VStack(spacing: 14) {
            EstateMark(progress: 0.1).frame(width: 110).opacity(0.5)
            if model.query.similar != nil && model.notice != nil {
                Text("Not without the server").font(.serif(22, .bold))
            } else {
                Text("Nothing found").font(.serif(22, .bold))
                Text(model.plan.text.isEmpty ? "No photo matches these filters."
                     : model.notice != nil ? "Nothing on this iPhone mentions “\(model.plan.text)”." : "No photo looks like “\(model.plan.text)”.")
                    .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
            }
            Button("Start again") { draft = ""; model.clear() }.buttonStyle(.glass).padding(.top, 4)
        }
        .padding(36)
    }

    // MARK: while typing

    @ViewBuilder private var suggestions: some View {
        let typed = draft.trimmingCharacters(in: .whitespaces)
        if !typed.isEmpty && typed != model.query.text {
            ForEach(model.vocab.suggest(typed, trips: model.trips), id: \.key) { s in
                Button {
                    draft = ""
                    searching = false
                    var q = model.query
                    q.text = ""
                    if !q.picked(s.facet).contains(s.value) { q.toggle(s.facet, s.value) }
                    model.go(q)
                } label: {
                    HStack(spacing: 12) {
                        Image(systemName: Pill.symbol(for: s.facet)).foregroundStyle(Color.sun).frame(width: 26)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(s.label).font(.serif(17)).foregroundStyle(.primary)
                            Text("\(s.facet.title)  ·  \(s.count.formatted())").font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
    }
}

/// Search with nothing asked yet: the ways into the library, drawn from the phone's own catalog.
struct BrowseView: View {
    @ObservedObject var model: LibraryModel
    @Binding var sheet: Pill?
    var search: (String) -> Void

    @State private var years: [(year: Int, count: Int)] = []
    @State private var favourites = 0
    private static let ideas = ["sunset over water", "dog on the beach", "birthday cake", "snowy mountains", "city at night", "a child laughing"]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 30) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Search").font(.serif(40, .bold))
                    Text("Describe what's in a photo, or pull a thread.").font(.subheadline).foregroundStyle(.secondary)
                }
                .padding(.horizontal, 16)

                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(Self.ideas, id: \.self) { idea in
                            Button { search(idea) } label: {
                                Label(idea, systemImage: "sparkle").font(.subheadline.weight(.medium))
                                    .padding(.horizontal, 13).padding(.vertical, 9)
                                    .background(Color.well, in: Capsule()).foregroundStyle(.primary)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.horizontal, 16)
                }
                .padding(.top, -12)

                if !model.trips.isEmpty { trips }
                if years.count > 1 { timeline }
                if favourites > 0 {
                    Button { model.go(Query(fav: true)) } label: {
                        HStack(spacing: 14) {
                            Image(systemName: "heart.fill").font(.title3).foregroundStyle(Color.sun)
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Favourites").font(.serif(19)).foregroundStyle(.primary)
                                Text("\(favourites.formatted()) photos").font(.footnote).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                        }
                        .padding(16)
                        .background(Color.well, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .padding(.horizontal, 16)
                }
                threads("Places", .place, .country)
                threads("People", .people, .person)
                threads("Albums", .albums, .album)
                threads("Cameras", .camera, .camera)
            }
            .padding(.top, 8)
            .padding(.bottom, 40)
        }
        .scrollDismissesKeyboard(.immediately)
        .task(id: SyncEngine.shared.revision) {
            let months = await Catalog.shared.months(Plan())
            years = Dictionary(grouping: months, by: { $0.ym / 12 }).map { ($0.key, $0.value.reduce(0) { $0 + $1.count }) }.sorted { $0.0 < $1.0 }
            favourites = (try? await Catalog.shared.store.read { try $0.one("SELECT count(*) FROM photos WHERE fav=1") { Int($0.int(0)) } ?? 0 }) ?? 0
        }
    }

    private func title(_ text: String, all: Pill? = nil) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(text).font(.serif(24, .bold))
            Spacer()
            if let all { Button("See all") { sheet = all }.font(.subheadline) }
        }
        .padding(.horizontal, 16)
    }

    // Trips, as prints laid on a table: the cover, and its caption over the bottom of it.
    private var trips: some View {
        VStack(alignment: .leading, spacing: 12) {
            title("Trips", all: model.trips.count > 6 ? .trips : nil)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 12) {
                    ForEach(model.trips.prefix(24)) { trip in
                        Button { model.go(Query(picks: [.trip: [String(trip.id)]])) } label: {
                            Cover(sha: trip.cover)
                                .frame(width: 220, height: 290)
                                .overlay(alignment: .bottomLeading) {
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(trip.title.replacingOccurrences(of: "Trip to ", with: "")).font(.serif(19)).lineLimit(3).multilineTextAlignment(.leading)
                                        Text("\(Dates.span(trip.start, trip.end))  ·  \(trip.n.formatted())").font(.caption).opacity(0.8)
                                    }
                                    .foregroundStyle(.white)
                                    .padding(14)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .background(LinearGradient(colors: [.clear, .black.opacity(0.75)], startPoint: .top, endPoint: .bottom).padding(.top, -50))
                                }
                                .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 16)
                .scrollTargetLayout()
            }
            .scrollTargetBehavior(.viewAligned)
        }
    }

    // The library's whole life as one line of bars: a year each, as tall as it was busy.
    private var timeline: some View {
        let most = Double(years.map(\.count).max() ?? 1)
        let step = years.count > 24 ? 5 : years.count > 10 ? 2 : 1
        return VStack(alignment: .leading, spacing: 14) {
            title("Through the years")
            HStack(alignment: .bottom, spacing: years.count > 20 ? 3 : 6) {
                ForEach(years, id: \.year) { y in
                    Button {
                        Haptics.tick()
                        model.go(Query(from: y.year * 12, to: y.year * 12 + 11))
                    } label: {
                        VStack(spacing: 6) {
                            RoundedRectangle(cornerRadius: 3, style: .continuous)
                                .fill(Color.sun.opacity(0.35 + 0.65 * sqrt(Double(y.count) / most)))
                                .frame(height: max(4, 96 * sqrt(Double(y.count) / most)))
                            Text(y.year % step == 0 ? (years.count > 14 ? "’" + String(format: "%02d", y.year % 100) : String(y.year)) : " ")
                                .font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary).fixedSize()
                                .frame(height: 12)
                        }
                        .frame(maxWidth: .infinity, alignment: .bottom)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("\(y.year), \(y.count) photos")
                }
            }
            .frame(height: 118, alignment: .bottom)
            .padding(.horizontal, 16)
        }
    }

    // The most-photographed few of a kind, as chips; the rest behind "See all".
    @ViewBuilder private func threads(_ name: String, _ pill: Pill, _ facet: Facet) -> some View {
        let top = model.vocab.entries.filter { $0.facet == facet }.sorted { $0.count > $1.count }
        if !top.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                title(name, all: top.count > 8 || pill.facets.count > 1 ? pill : nil)
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(top.prefix(12), id: \.label) { e in
                            Button { model.go(Query(picks: [facet: [e.label]])) } label: {
                                HStack(spacing: 7) {
                                    Text(e.label).font(.subheadline.weight(.medium)).foregroundStyle(.primary)
                                    Text(e.count.formatted()).font(.footnote).foregroundStyle(.secondary).monospacedDigit()
                                }
                                .padding(.horizontal, 14).padding(.vertical, 10)
                                .background(Color.well, in: Capsule())
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.horizontal, 16)
                }
            }
        }
    }
}

/// A photo as a cover: the thumbnail at once, then the sharper copy.
struct Cover: View {
    let sha: String
    @State private var image: UIImage?

    var body: some View {
        Color.well
            .overlay { if let image { Image(uiImage: image).resizable().scaledToFill().transition(.opacity) } }
            .clipped()
            .task(id: sha) {
                guard !sha.isEmpty else { return }
                image = await ImageStore.shared.thumb(sha)
                if let sharp = await ImageStore.shared.image(.preview, sha) { withAnimation(.easeOut(duration: 0.25)) { image = sharp } }
            }
    }
}

/// The filter chips above the grid.
enum Pill: String, CaseIterable, Identifiable {
    case place, people, albums, trips, dates, camera, fav
    var id: String { rawValue }

    var title: String {
        switch self {
        case .place: "Where"
        case .people: "People"
        case .albums: "Albums"
        case .trips: "Trips"
        case .dates: "When"
        case .camera: "Camera"
        case .fav: "Favourites"
        }
    }

    var symbol: String? { self == .fav ? "heart" : nil }

    var facets: [Facet] {
        switch self {
        case .place: [.country, .region, .city]
        case .people: [.person]
        case .albums: [.album]
        case .trips: [.trip]
        case .camera: [.camera, .lens]
        case .dates, .fav: []
        }
    }

    static func symbol(for f: Facet) -> String {
        switch f {
        case .country, .region, .city: "mappin.and.ellipse"
        case .person: "person.crop.circle"
        case .album: "rectangle.stack"
        case .camera, .lens: "camera"
        case .trip: "airplane"
        }
    }

    /// What the chip says when it has a selection: the first value, "+N" for the rest.
    func value(_ q: Query, trips: [Trip]) -> String? {
        switch self {
        case .fav: return q.fav ? "Favourites" : nil
        case .dates: return q.from == nil && q.to == nil ? nil : Dates.range(q.from, q.to)
        default:
            var picked = facets.flatMap { q.picked($0) }
            if self == .trips { picked = picked.map { id in (trips.first { String($0.id) == id }?.title ?? "Trip").replacingOccurrences(of: "Trip to ", with: "") } }
            guard let first = picked.first else { return nil }
            let short = self == .place ? (first.split(separator: ",").first.map(String.init) ?? first) : first
            return picked.count > 1 ? "\(short) +\(picked.count - 1)" : short
        }
    }

    func cleared(_ q: Query) -> Query {
        var q = q
        switch self {
        case .fav: q.fav = false
        case .dates: q.from = nil; q.to = nil
        default: for f in facets { q.picks[f] = nil }
        }
        return q
    }
}
