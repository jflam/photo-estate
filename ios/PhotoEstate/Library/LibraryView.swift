import SwiftUI

/// The Library tab: the grid, the search field, and the filters. Native, and answered from the phone's
/// own catalog, so it opens at once and works with no network.
struct LibraryView: View {
    @StateObject private var model = LibraryModel()
    @ObservedObject private var sync = SyncEngine.shared
    @State private var draft = ""
    @State private var viewer: Opened?
    @State private var sheet: Pill?
    @State private var reveal: Int?
    @State private var headerHeight: CGFloat = 0

    private struct Opened: Identifiable {
        let id = UUID()
        let tiles: [Tile]
        let index: Int
    }

    var body: some View {
        NavigationStack {
            GridView(data: model.grid, rowHeight: model.rowHeight,
                     onOpen: { i in viewer = Opened(tiles: model.grid.tiles, index: i) },
                     onNearEnd: { model.loadMore() },
                     onRefresh: { ImageStore.shared.retryNetwork(); await sync.sync() },
                     reveal: reveal, topInset: headerHeight)
                .ignoresSafeArea()
                .overlay { empty }
                .overlay(alignment: .top) { header.onGeometryChange(for: CGFloat.self) { $0.size.height } action: { headerHeight = $0 } }
                .navigationTitle(model.title)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { toolbar }
                .searchable(text: $draft, prompt: "Describe a photo, or a name or place")
                .searchSuggestions { suggestions }
                .onSubmit(of: .search) { model.search(draft) }
                .onChange(of: model.query.text) { _, t in if t != draft { draft = t } }
                .onChange(of: draft) { _, t in if t.isEmpty && !model.query.text.isEmpty { model.search("") } }
        }
        .fullScreenCover(item: $viewer) { v in
            ViewerScreen(tiles: v.tiles, index: v.index, model: model) { reveal = $0 }
        }
        .sheet(item: $sheet) { pill in
            FilterSheet(pill: pill, model: model).presentationDetents([.medium, .large])
        }
        .onAppear { model.reload(reset: false) }
        .onChange(of: sync.revision) { _, _ in model.catalogChanged() }
    }

    // MARK: header: what is showing, and the filters

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(Pill.allCases) { pill in chip(pill) }
                }
                .padding(.horizontal, 12)
            }
            HStack(spacing: 6) {
                if !sync.online { Label("Offline", systemImage: "wifi.slash").labelStyle(.titleAndIcon).foregroundStyle(.orange) }
                Text(model.subtitle).lineLimit(1)
                Spacer(minLength: 0)
            }
            .font(.footnote)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 14)
            if model.plan.couldRead && model.query.similar == nil {
                Button(model.query.plain ? "Read the names and places in it instead" : "Search for the words instead") {
                    var q = model.query
                    q.plain.toggle()
                    model.go(q)
                }
                .font(.footnote)
                .padding(.horizontal, 14)
            }
            if let notice = model.notice {
                Text(notice).font(.footnote).foregroundStyle(.secondary).padding(.horizontal, 14).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.top, 6)
        .padding(.bottom, 8)
        .background(.bar)
    }

    private func chip(_ pill: Pill) -> some View {
        let value = pill.value(model.query, trips: model.trips)
        return Button {
            if pill == .fav { var q = model.query; q.fav.toggle(); model.query = q } else { sheet = pill }
        } label: {
            HStack(spacing: 5) {
                if let symbol = pill.symbol { Image(systemName: value == nil ? symbol : "\(symbol).fill").imageScale(.small) }
                Text(value ?? pill.title).lineLimit(1)
                if value != nil && pill != .fav {
                    Image(systemName: "xmark").imageScale(.small).fontWeight(.semibold)
                        .onTapGesture { model.query = pill.cleared(model.query) }
                        .accessibilityLabel("Clear \(pill.title)")
                }
            }
            .font(.subheadline.weight(.medium))
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .foregroundStyle(value == nil ? Color.primary : Color.white)
            .background(value == nil ? AnyShapeStyle(.quaternary) : AnyShapeStyle(Color.accentColor), in: Capsule())
        }
        .buttonStyle(.plain)
    }

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        if model.canGoBack {
            ToolbarItem(placement: .topBarLeading) {
                Button { model.back() } label: { Image(systemName: "chevron.backward") }.accessibilityLabel("Back")
            }
        }
        ToolbarItem(placement: .topBarTrailing) {
            Menu {
                if !model.ranked {
                    Picker("Order", selection: Binding(get: { model.oldFirst }, set: { v in var q = model.query; q.oldFirst = v; model.query = q })) {
                        Label("Newest first", systemImage: "arrow.down").tag(false)
                        Label("Oldest first", systemImage: "arrow.up").tag(true)
                    }
                }
                Picker("Photo size", selection: $model.rowHeight) {
                    Text("Small photos").tag(96.0)
                    Text("Medium photos").tag(128.0)
                    Text("Large photos").tag(196.0)
                }
                Toggle("Hide near-identical shots", isOn: $model.hideDups)
                if !model.query.isEmpty {
                    Button("Clear search and filters", systemImage: "xmark.circle", role: .destructive) { draft = ""; model.clear() }
                }
            } label: { Image(systemName: "ellipsis.circle") }
            .accessibilityLabel("View options")
        }
    }

    @ViewBuilder private var suggestions: some View {
        let typed = draft.trimmingCharacters(in: .whitespaces)
        if typed == model.query.text && !typed.isEmpty {
            EmptyView()                      // these words are what is showing: don't cover the answer
        } else if typed.isEmpty {
            ForEach(["sunset over water", "dog on the beach", "birthday cake", "snowy mountains", "city at night", "is:fav"], id: \.self) { idea in
                Label(idea, systemImage: "sparkle.magnifyingglass").searchCompletion(idea)
            }
        } else {
            ForEach(model.vocab.suggest(typed, trips: model.trips), id: \.key) { s in
                Button {
                    draft = ""
                    var q = model.query
                    q.text = ""
                    if !q.picked(s.facet).contains(s.value) { q.toggle(s.facet, s.value) }
                    model.go(q)
                } label: {
                    HStack {
                        Image(systemName: Pill.symbol(for: s.facet)).foregroundStyle(.secondary).frame(width: 24)
                        VStack(alignment: .leading) {
                            Text(s.label)
                            Text("\(s.facet.title) · \(s.count.formatted())").font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder private var empty: some View {
        if model.grid.tiles.isEmpty && !model.loading {
            if sync.photos == 0 {
                switch sync.state {
                case .failed(let why):
                    ContentUnavailableView {
                        Label("Can't reach your library", systemImage: "wifi.slash")
                    } description: {
                        Text("\(why)\nPhotos appear here once this iPhone has fetched the catalog from \(Server.baseURL.host() ?? "your server").")
                    } actions: {
                        Button("Try again") { Task { await sync.sync() } }
                    }
                default:
                    ContentUnavailableView { ProgressView() } description: { Text("Fetching your library…") }
                }
            } else if model.query.similar != nil && model.notice != nil {
                ContentUnavailableView("Not available offline", systemImage: "wifi.slash")
            } else {
                ContentUnavailableView {
                    Label("Nothing found", systemImage: "photo.on.rectangle.angled")
                } description: {
                    Text(model.plan.text.isEmpty ? "No photo matches these filters." : model.notice != nil ? "Nothing on this iPhone mentions “\(model.plan.text)”." : "No photo looks like “\(model.plan.text)”.")
                } actions: {
                    Button("Clear search and filters") { draft = ""; model.clear() }
                }
            }
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
            if self == .trips { picked = picked.map { id in trips.first { String($0.id) == id }?.title ?? "Trip" } }
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
