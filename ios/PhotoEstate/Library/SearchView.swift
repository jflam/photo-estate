import SwiftUI

/// The Search tab: finding photos. The field is always at the top. With nothing asked, the page under it
/// lays out the threads to pull (trips, years, places, people); with a question, it shows the answer as a
/// grid, with the filters under the field. The field is the app's own, not the system's search bar: that
/// one moves between the navigation bar and the tab bar from one iOS version to the next, and went missing.
struct SearchView: View {
    @ObservedObject var model: LibraryModel
    @EnvironmentObject private var router: Router
    @ObservedObject private var sync = SyncEngine.shared
    @State private var draft = ""
    @State private var sheet: Pill?
    @State private var barHeight: CGFloat = 0
    @FocusState private var typing: Bool

    /// Typing something that is not yet the question: offer filters for it instead of the page behind.
    private var suggesting: Bool {
        let typed = draft.trimmingCharacters(in: .whitespaces)
        return typing && !typed.isEmpty && typed != model.query.text
    }

    var body: some View {
        ZStack(alignment: .top) {
            Group {
                if suggesting {
                    suggestions
                } else if model.query.isEmpty {
                    BrowseView(model: model, sheet: $sheet, top: barHeight) { idea in draft = idea; submit() }
                } else {
                    results
                }
            }
            bar.onGeometryChange(for: CGFloat.self) { $0.size.height } action: { barHeight = $0 }
        }
        .background(Color.paper)
        .onChange(of: model.query.text) { _, t in if t != draft { draft = t } }
        .sheet(item: $sheet) { pill in
            FilterSheet(pill: pill, model: model).presentationDetents([.medium, .large]).presentationCornerRadius(28)
        }
        .onAppear { model.reload(reset: false) }
        .onChange(of: sync.revision) { _, _ in model.catalogChanged() }
    }

    private func submit() {
        typing = false
        model.search(draft)
    }

    // MARK: the bar: the field, always; under it, the filters of whatever is showing

    private var bar: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                if !model.query.isEmpty {
                    Button {
                        typing = false
                        draft = ""
                        if model.canGoBack { model.back() } else { model.clear() }
                    } label: { Image(systemName: "chevron.backward").font(.body.weight(.semibold)).frame(width: 36, height: 44).contentShape(Rectangle()) }
                        .foregroundStyle(.primary)
                        .accessibilityLabel("Back")
                }
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    TextField("Describe a photo, or a name or place", text: $draft)
                        .focused($typing)
                        .submitLabel(.search)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .onSubmit { submit() }
                    if !draft.isEmpty {
                        Button { draft = ""; if !model.query.text.isEmpty { model.search("") } } label: { Image(systemName: "xmark.circle.fill") }
                            .foregroundStyle(.secondary)
                            .accessibilityLabel("Clear text")
                    }
                }
                .padding(.horizontal, 14)
                .frame(height: 46)
                .background(Color.well, in: Capsule())
                .contentShape(Capsule())
                .onTapGesture { typing = true }
                if typing {
                    Button("Cancel") { typing = false; draft = model.query.text }.foregroundStyle(Color.sun)
                } else if !model.query.isEmpty {
                    ViewMenu(model: model).frame(width: 36, height: 44)
                }
            }
            .padding(.horizontal, 14)
            .animation(.snappy(duration: 0.2), value: typing)

            if !model.query.isEmpty && !suggesting {
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
                            .foregroundStyle(Color.sun)
                        }
                    }
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 16)
                }
            }
        }
        .padding(.top, 6)
        .padding(.bottom, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.paper.opacity(0.9))
        .background(.ultraThinMaterial)
    }

    // MARK: the answer

    private var results: some View {
        GridView(data: model.grid, rowHeight: model.rowHeight, title: model.title, subtitle: model.subtitle, topInset: barHeight,
                 onNearEnd: { model.loadMore() },
                 onRefresh: { ImageStore.shared.retryNetwork(); await sync.sync() },
                 onRowHeight: { model.rowHeight = $0 },
                 viewer: { session in AnyView(ViewerScreen(session: session, model: model, route: router.find)) })
            .ignoresSafeArea(edges: .bottom)
            .overlay { if model.grid.tiles.isEmpty && !model.loading { nothing } }
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
                Text("Not without the server").font(.title(22, .bold))
            } else {
                Text("Nothing found").font(.title(22, .bold))
                Text(model.plan.text.isEmpty ? "No photo matches these filters."
                     : model.notice != nil ? "Nothing on this iPhone mentions “\(model.plan.text)”." : "No photo looks like “\(model.plan.text)”.")
                    .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
            }
            Button("Start again") { draft = ""; model.clear() }.buttonStyle(.glass).padding(.top, 4)
        }
        .padding(36)
    }

    // MARK: while typing

    private var suggestions: some View {
        let typed = draft.trimmingCharacters(in: .whitespaces)
        return ScrollView {
            VStack(spacing: 0) {
                row("sparkle.magnifyingglass", "“\(typed)”", "Photos that look like this") { submit() }
                ForEach(model.vocab.suggest(typed, trips: model.trips), id: \.key) { s in
                    Divider().padding(.leading, 54)
                    row(Pill.symbol(for: s.facet), s.label, "\(s.facet.title)  ·  \(s.count.formatted())") {
                        typing = false
                        draft = ""
                        var q = model.query
                        q.text = ""
                        if !q.picked(s.facet).contains(s.value) { q.toggle(s.facet, s.value) }
                        model.go(q)
                    }
                }
            }
            .padding(.top, barHeight + 4)
        }
        .scrollDismissesKeyboard(.interactively)
    }

    private func row(_ symbol: String, _ title: String, _ detail: String, _ run: @escaping () -> Void) -> some View {
        Button(action: run) {
            HStack(spacing: 14) {
                Image(systemName: symbol).foregroundStyle(Color.sun).frame(width: 26)
                VStack(alignment: .leading, spacing: 1) {
                    Text(title).font(.body.weight(.medium)).foregroundStyle(.primary).lineLimit(1)
                    Text(detail).font(.footnote).foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 11)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

/// Search with nothing asked yet: the ways into the library, drawn from the phone's own catalog.
struct BrowseView: View {
    @ObservedObject var model: LibraryModel
    @Binding var sheet: Pill?
    var top: CGFloat = 0            // the search bar drawn over the top
    var search: (String) -> Void

    @State private var years: [(year: Int, count: Int)] = []
    @State private var favourites = 0
    private static let ideas = ["sunset over water", "dog on the beach", "birthday cake", "snowy mountains", "city at night", "a child laughing"]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 30) {
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

                if !model.trips.isEmpty { trips }
                if years.count > 1 { timeline }
                if favourites > 0 {
                    Button { model.go(Query(fav: true)) } label: {
                        HStack(spacing: 14) {
                            Image(systemName: "heart.fill").font(.title3).foregroundStyle(Color.sun)
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Favourites").font(.title(19)).foregroundStyle(.primary)
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
            .padding(.top, top + 10)
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
            Text(text).font(.title(24, .bold))
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
                                        Text(trip.title.replacingOccurrences(of: "Trip to ", with: "")).font(.title(19)).lineLimit(3).multilineTextAlignment(.leading)
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
