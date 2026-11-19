import SwiftUI

/// An album as the server lists it (changes.py): a title, a note on what was asked for, and photos. A
/// `live` one is a search and holds whatever that finds now. One that is up for hiding (`op` "hide") is a
/// change set, reviewed and applied on the server's review page.
struct Album: Identifiable, Equatable {
    let id: Int
    var title = ""
    var prompt: String?
    var op = "view"
    var state = ""
    var live = false
    var n = 0
    var applied = 0            // photos it is hiding now
    var cover: [String] = []

    init?(json j: [String: Any]) {
        guard let id = (j["id"] as? NSNumber)?.intValue else { return nil }
        self.id = id
        title = j["title"] as? String ?? "Album"
        prompt = (j["prompt"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        op = j["op"] as? String ?? "view"
        state = j["state"] as? String ?? ""
        live = (j["kind"] as? String) == "live"
        n = (j["n"] as? NSNumber)?.intValue ?? 0
        applied = (j["applied"] as? NSNumber)?.intValue ?? 0
        cover = j["cover"] as? [String] ?? []
    }

    var hiding: Bool { op == "hide" }

    /// What it is doing to the library, if anything.
    var hidingNote: String? {
        guard hiding else { return nil }
        return applied > 0 ? "Hiding \(Album.photos(applied))" : state == "done" ? "Hidden" : state == "reverted" ? nil : "Up for hiding"
    }

    /// On its card: only what sets it apart from a plain album.
    var badge: String? { hiding ? hidingNote : live ? "Live" : nil }

    static func photos(_ n: Int) -> String { "\(n.formatted()) \(n == 1 ? "photo" : "photos")" }
}

/// The Albums tab's state: the albums made on the server, the albums that came in with the photos, and the
/// album that is open.
///
/// Albums are the server's (a person or an agent makes them there), so the list and each album's photos are
/// asked for; what was last heard is kept on the phone, so an album already looked at still opens with no
/// server. Its photos are drawn from the phone's catalog like any others.
@MainActor
final class AlbumsModel: ObservableObject {
    enum Phase: Equatable { case loading, ready, failed(String) }

    @Published private(set) var albums: [Album] = []
    @Published private(set) var phase = Phase.loading
    @Published private(set) var kept = false                 // showing what was last heard, not what the server says now
    @Published private(set) var sources: [(name: String, count: Int)] = []   // albums that came with the photos
    @Published private(set) var open: Album?
    @Published private(set) var grid = GridData()
    @Published private(set) var opening = Phase.loading
    @Published private(set) var working = false              // something asked of the server has not answered yet
    @Published var problem: String?                          // what it said when it would not
    @Published var notice: String?                           // what it did, said briefly
    @Published var rowHeight = UserDefaults.standard.object(forKey: "view.rowHeight") as? Double ?? 128 {
        didSet { UserDefaults.standard.set(rowHeight, forKey: "view.rowHeight") }
    }

    private struct Member {
        let sha: String
        let section: String
        let reason: String?
    }
    private var members: [Member] = []
    private var served: [String: Photo] = [:]     // the rows the server sent with the album
    private var gen = 0
    private var listGen = 0

    // MARK: the list

    func load() async {
        listGen += 1
        let g = listGen
        let counts = await Catalog.shared.counts(.album, Plan())
        guard g == listGen else { return }
        sources = counts.map { ($0.value, $0.count) }
        do {
            let j = try await API.get("api/albums")
            guard g == listGen else { return }
            albums = (j["sets"] as? [[String: Any]] ?? []).compactMap(Album.init(json:))
            Self.keep("albums", j)
            kept = false
            phase = .ready
        } catch {
            guard g == listGen else { return }
            if let j = Self.recall("albums") {
                albums = (j["sets"] as? [[String: Any]] ?? []).compactMap(Album.init(json:))
                kept = true
                phase = .ready
            } else if albums.isEmpty {
                phase = .failed(Self.words(error))
            }
        }
        if let open, let now = albums.first(where: { $0.id == open.id }), !kept { self.open = now }
    }

    // MARK: one album

    func show(_ album: Album) {
        Haptics.tap()
        open = album
        members = []
        served = [:]
        grid = GridData(id: grid.id + 1, reset: grid.reset + 1)
        Task { await fetch() }
    }

    /// A new album of these photos, fixed as they are now and in this order (Search makes one of what it
    /// finds). It opens as soon as the server has made it.
    func create(title: String, prompt: String, shas: [String]) async throws {
        let j = try await API.post("api/albums", ["title": title, "prompt": prompt, "items": shas.map { ["sha": $0] }])
        guard let album = Album(json: j) else { throw APIError(code: 0, description: "The server made no album.") }
        show(album)
    }

    /// Back to the list, which is asked for again: an album may have been made or changed meanwhile.
    func close() {
        gen += 1
        open = nil
        working = false
        Task { await load() }
    }

    /// Ask for the open album's photos again, a page at a time; the first page shows as soon as it is in.
    func fetch() async {
        guard let id = open?.id else { return }
        gen += 1
        let g = gen
        if members.isEmpty { opening = .loading }
        var all: [Member] = [], rows: [String: Photo] = [:], cursor: Int? = 0, raw: [[String: Any]] = [], info: [String: Any] = [:]
        do {
            while let at = cursor {
                let j = try await API.get("api/albums/\(id)/photos", [.init(name: "limit", value: "500"), .init(name: "cursor", value: String(at))])
                guard g == gen else { return }
                info = j["album"] as? [String: Any] ?? info
                if let a = Album(json: info), var now = open, now.id == a.id {     // (the list's cover is not sent here)
                    let cover = now.cover
                    now = a
                    now.cover = cover
                    open = now
                }
                for r in j["results"] as? [[String: Any]] ?? [] {
                    let p = Photo(json: r)
                    guard p.sha.count == 64 else { continue }
                    let m = Member(sha: p.sha, section: r["section"] as? String ?? "", reason: (r["reason"] as? String).flatMap { $0.isEmpty ? nil : $0 })
                    all.append(m)
                    rows[p.sha] = p
                    raw.append(["sha": m.sha, "section": m.section, "reason": m.reason ?? ""])
                }
                cursor = (j["next"] as? NSNumber)?.intValue
                if at == 0 || cursor == nil {
                    members = all
                    served = rows
                    await redraw(g)
                    guard g == gen else { return }
                    opening = .ready
                }
            }
            Self.keep("album-\(id)", ["album": info, "members": raw])
        } catch {
            guard g == gen else { return }
            if !members.isEmpty { opening = .ready; return }       // what is showing stays up
            if let j = Self.recall("album-\(id)") {
                members = (j["members"] as? [[String: Any]] ?? []).compactMap { r in
                    (r["sha"] as? String).map { Member(sha: $0, section: r["section"] as? String ?? "", reason: (r["reason"] as? String).flatMap { $0.isEmpty ? nil : $0 }) }
                }
                await redraw(g)
                guard g == gen else { return }
                opening = .ready
            } else {
                opening = .failed(Self.words(error))
            }
        }
    }

    /// The phone's catalog changed (a favourite, a sync): the same album, drawn from it again. With the list
    /// showing, the list is asked for again: an album's cover is a favourite of it if it has one, and it
    /// is the server that picks it.
    func catalogChanged() {
        Task {
            guard open != nil else { return await load() }
            let counts = await Catalog.shared.counts(.album, Plan())
            sources = counts.map { ($0.value, $0.count) }
            if !members.isEmpty { await redraw(gen) }
        }
    }

    /// The album's photos as tiles, in its order, under its sections. A photo is drawn from the phone's
    /// catalog when it is there (so a favourite set a moment ago shows), else from the row the server sent.
    private func redraw(_ g: Int) async {
        let list = members
        let known = await Catalog.shared.tiles(of: list.map(\.sha))
        guard g == gen else { return }
        var tiles: [Tile] = [], runs: [GridData.Run] = [], title = "", count = 0
        for m in list {
            guard let t = known[m.sha] ?? served[m.sha].map({ Tile(sha: $0.sha, ar: Float($0.ar), when: $0.when, city: $0.city, fav: $0.fav) }) else { continue }
            if count > 0 && m.section != title {
                runs.append(.init(title: title, count: count))
                count = 0
            }
            title = m.section
            count += 1
            tiles.append(t)
        }
        if count > 0 { runs.append(.init(title: title, count: count)) }
        let sectioned = runs.contains { !$0.title.isEmpty }
        grid = GridData(tiles: tiles, grain: 0, id: grid.id + 1, reset: grid.reset, runs: sectioned ? runs : nil)
    }

    func photo(_ sha: String) async -> Photo? {
        if let p = await Catalog.shared.photo(sha) { return p }
        return served[sha]
    }

    func reason(_ sha: String) -> String? { members.first { $0.sha == sha }?.reason }

    /// Under the album's title: how many, what kind it is, and what was asked for.
    var subtitle: String {
        guard let a = open else { return "" }
        let n = opening == .ready ? grid.tiles.count : a.n
        let line = [Album.photos(max(n, a.n)), a.live ? "Live: whatever its search finds" : "", a.hidingNote ?? "", kept ? "as last seen" : ""]
            .filter { !$0.isEmpty }.joined(separator: "  ·  ")
        return a.prompt.map { "\(line)\n\($0)" } ?? line
    }

    // MARK: what can be done with it. Each is done to all its photos, on the server.

    /// Every photo in it a favourite, or none of them.
    func favourite(_ on: Bool) {
        guard let a = open else { return }
        act {
            let got = try await API.post("api/albums/\(a.id)/favorite", ["fav": on])
            let changed = (got["changed"] as? NSNumber)?.intValue ?? 0
            await Favourites.keep(self.members.map(\.sha), on)
            self.notice = changed == 0 ? (on ? "They were all favourites already" : "None of them was a favourite")
                        : "\(Album.photos(changed)) \(on ? "made a favourite" : "taken out of favourites")"
        }
    }

    /// Put it up for hiding: it becomes a change set, to be looked over and applied on the review page.
    func proposeHiding(then review: @escaping () -> Void) {
        guard let a = open else { return }
        act {
            _ = try await API.post("api/albums/\(a.id)/hide")
            await self.fetch()
            review()
        }
    }

    /// Forget the album. Its photos stay in the library.
    func discard() {
        guard let a = open else { return }
        act {
            _ = try await API.post("api/albums/\(a.id)/discard")
            Self.forget("album-\(a.id)")
            self.close()
        }
    }

    private func act(_ work: @escaping () async throws -> Void) {
        guard !working else { return }
        working = true
        Task {
            defer { working = false }
            do { try await work() } catch {
                problem = (error as? APIError)?.notAllowed == true ? "Albums can’t be changed from this account." : Self.words(error)
            }
        }
    }

    static func words(_ error: Error) -> String {
        error is URLError ? "Your server isn’t reachable right now." : (error as? APIError)?.description ?? error.localizedDescription
    }

    // MARK: what was last heard, kept beside the catalog

    private static func file(_ name: String) -> URL {
        let dir = Catalog.folder.appendingPathComponent("albums", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("\(name).json")
    }

    private static func keep(_ name: String, _ j: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: j) else { return }
        try? data.write(to: file(name), options: .atomic)
    }

    private static func recall(_ name: String) -> [String: Any]? {
        (try? Data(contentsOf: file(name))).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
    }

    private static func forget(_ name: String) { try? FileManager.default.removeItem(at: file(name)) }
}

/// The Albums tab: sets of photos. Those made on the server (by hand, or by an agent asked in a chat connected
/// to the library) are cards; under them, the albums that came in with the photos, each a filter in Search. An
/// album is somewhere to look and something to act on: its photos can be made favourites together, downloaded,
/// or put up for hiding, which makes it a change set to review.
struct AlbumsView: View {
    @ObservedObject var model: AlbumsModel
    @EnvironmentObject private var router: Router
    @ObservedObject private var sync = SyncEngine.shared
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        ZStack {
            AlbumList(model: model)
                .opacity(model.open == nil ? 1 : 0)
                .allowsHitTesting(model.open == nil)
                .accessibilityHidden(model.open != nil)
            if model.open != nil { AlbumPage(model: model).transition(.opacity) }
        }
        .animation(.snappy(duration: 0.22), value: model.open?.id)
        .background(Color.paper)
        .onAppear { Task { await model.load() } }
        .onChange(of: scenePhase) { _, phase in if phase == .active { Task { await model.load() } } }
        .onChange(of: sync.revision) { _, _ in model.catalogChanged() }
        .onChange(of: sync.online) { _, on in if on && (model.kept || model.phase != .ready) { Task { await model.load() } } }
        .alert("That didn’t work", isPresented: Binding(get: { model.problem != nil }, set: { if !$0 { model.problem = nil } })) {
            Button("OK", role: .cancel) {}
        } message: { Text(model.problem ?? "") }
    }
}

/// Every album: the ones made here as cards, then the ones that came with the photos.
private struct AlbumList: View {
    @ObservedObject var model: AlbumsModel
    @EnvironmentObject private var router: Router

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Albums").font(.title(28, .bold))
                    Text(count).font(.subheadline).foregroundStyle(.secondary)
                }
                .padding(.horizontal, 2)
                .padding(.top, 12)
                .padding(.bottom, 4)

                if case .failed(let why) = model.phase {
                    blank("Couldn’t load the albums", why) { Button("Try again") { Task { await model.load() } }.buttonStyle(.glassProminent) }
                } else if model.albums.isEmpty && model.phase == .ready {
                    blank("No albums yet", "Search for some photos and make an album of what it finds, or ask for one in a chat connected to this library: “the best five pictures of Ben for every year”, “our trips to the coast”. It appears here when it is made.") { EmptyView() }
                }
                ForEach(model.albums) { album in
                    Button { model.show(album) } label: { AlbumCard(album: album) }
                        .buttonStyle(CardPress())
                }

                if !model.sources.isEmpty {
                    Text("From your sources").font(.title(22, .bold)).padding(.horizontal, 2).padding(.top, 18)
                    VStack(spacing: 0) {
                        ForEach(Array(model.sources.enumerated()), id: \.element.name) { i, s in
                            if i > 0 { Divider().padding(.leading, 16) }
                            // An album that came with the photos: those photos, in Search.
                            Button { router.find(Query(picks: [.album: [s.name]])) } label: {
                                HStack {
                                    Text(s.name).font(.title(17, .medium)).foregroundStyle(.primary).multilineTextAlignment(.leading)
                                    Spacer()
                                    Text(s.count.formatted()).foregroundStyle(.secondary).monospacedDigit()
                                    Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                                }
                                .padding(.horizontal, 16).padding(.vertical, 13)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .background(Color.well, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                }
            }
            .padding(.horizontal, 14)
            .padding(.bottom, 24)
        }
        .refreshable { await model.load() }
        .background(Color.paper)
    }

    private var count: String {
        guard model.phase == .ready else { return model.phase == .loading ? "Asking your server…" : " " }
        let made = model.albums.isEmpty ? "None made here yet" : "\(model.albums.count.formatted()) made here"
        return [made, model.sources.isEmpty ? "" : "\(model.sources.count.formatted()) from your sources", model.kept ? "Offline: as last seen" : ""]
            .filter { !$0.isEmpty }.joined(separator: "  ·  ")
    }

    private func blank<More: View>(_ title: String, _ why: String, @ViewBuilder more: () -> More) -> some View {
        VStack(spacing: 12) {
            EstateMark(progress: 0.1).frame(width: 110).opacity(0.5)
            Text(title).font(.title(22, .bold))
            Text(why).font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
            more()
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 36)
        .frame(maxWidth: .infinity)
    }
}

/// An album's card: its cover (a favourite of it if it has one, as the server picks), edge to edge, with its
/// title and how many over it.
private struct AlbumCard: View {
    let album: Album

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 18, style: .continuous)
        Cover(sha: album.cover.first ?? "")
            .frame(height: 210)
            .overlay(alignment: .topLeading) {
                LinearGradient(colors: [.black.opacity(0.5), .clear], startPoint: .top, endPoint: .center).allowsHitTesting(false)
            }
            .overlay(alignment: .topLeading) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(album.title).font(.title(24, .bold)).lineLimit(2).multilineTextAlignment(.leading)
                    Text(Album.photos(album.n)).font(.subheadline.weight(.medium)).opacity(0.9)
                }
                .foregroundStyle(.white)
                .shadow(color: .black.opacity(0.35), radius: 3)
                .padding(16)
            }
            .overlay(alignment: .bottomTrailing) {
                if let badge = album.badge {
                    Text(badge).font(.caption.weight(.semibold)).foregroundStyle(.white)
                        .padding(.horizontal, 10).padding(.vertical, 5)
                        .background(.black.opacity(0.55), in: Capsule())
                        .padding(12)
                }
            }
            .clipShape(shape)
            .contentShape(shape)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel([album.title, Album.photos(album.n), album.badge ?? ""].filter { !$0.isEmpty }.joined(separator: ", "))
            .accessibilityAddTraits(.isButton)
    }
}

/// One album: its photos under its sections, and what can be done with all of them.
private struct AlbumPage: View {
    @ObservedObject var model: AlbumsModel
    @EnvironmentObject private var router: Router
    @ObservedObject private var sync = SyncEngine.shared
    @Environment(\.openURL) private var openURL
    @State private var barHeight: CGFloat = 0
    @State private var reviewing = false
    @State private var deleting = false

    var body: some View {
        ZStack(alignment: .top) {
            GridView(data: model.grid, rowHeight: model.rowHeight, title: model.open?.title ?? "", subtitle: model.subtitle, topInset: barHeight,
                     onRefresh: { await model.fetch() },
                     onRowHeight: { model.rowHeight = $0 },
                     viewer: { session in AnyView(ViewerScreen(session: session, lookup: model.photo, reason: model.reason, route: router.find)) })
                .ignoresSafeArea()
            if model.grid.tiles.isEmpty { empty }
            bar.onGeometryChange(for: CGFloat.self) { $0.size.height } action: { barHeight = $0 }
        }
        .background(Color.paper)
        .overlay(alignment: .bottom) {
            if let notice = model.notice {
                Text(notice).font(.subheadline.weight(.medium))
                    .padding(.horizontal, 16).padding(.vertical, 10)
                    .glassEffect(.regular, in: .capsule)
                    .padding(.bottom, 14)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                    .task(id: notice) {
                        try? await Task.sleep(for: .seconds(2.6))
                        if !Task.isCancelled { model.notice = nil }
                    }
            }
        }
        .animation(.snappy(duration: 0.25), value: model.notice)
        .gesture(EdgeSwipe(changed: { _ in }, ended: { x, v in if x > 90 || v > 500 { Haptics.tick(); model.close() } }))
        .confirmationDialog("Delete the album “\(model.open?.title ?? "")”? Its photos stay in the library.", isPresented: $deleting, titleVisibility: .visible) {
            Button("Delete album", role: .destructive) { model.discard() }
        }
        .sheet(isPresented: $reviewing, onDismiss: {      // what was decided there changes the album, and perhaps the library
            Task { await model.fetch(); await model.load(); await sync.sync() }
        }) {
            if let a = model.open {
                NavigationStack {
                    WebView(url: Server.baseURL.appendingPathComponent("changes/\(a.id)")).ignoresSafeArea(edges: .bottom)
                        .navigationTitle("Review").navigationBarTitleDisplayMode(.inline)
                        .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Done") { reviewing = false }.fontWeight(.semibold) } }
                }
                .tint(.sun)
            }
        }
    }

    private var bar: some View {
        HStack(spacing: 8) {
            Button { model.close() } label: { Image(systemName: "chevron.backward").modifier(GlassCircle()) }
                .buttonStyle(.plain)
                .accessibilityLabel("All albums")
            Spacer(minLength: 0)
            HStack(spacing: 6) {
                Menu {
                    Picker("Photo size", selection: Binding(get: { Estate.densities.min { abs($0 - model.rowHeight) < abs($1 - model.rowHeight) } ?? 128 },
                                                            set: { model.rowHeight = $0 })) {
                        Label("Smallest", systemImage: "square.grid.4x3.fill").tag(Estate.densities[0])
                        Label("Small", systemImage: "square.grid.3x3.fill").tag(Estate.densities[1])
                        Label("Medium", systemImage: "square.grid.3x2.fill").tag(Estate.densities[2])
                        Label("Large", systemImage: "square.grid.2x2.fill").tag(Estate.densities[3])
                        Label("Largest", systemImage: "rectangle.grid.1x2.fill").tag(Estate.densities[4])
                    }
                } label: {
                    Image(systemName: "line.3.horizontal.decrease").foregroundStyle(Color.primary).frame(width: 46, height: 44).contentShape(Rectangle())
                }
                .accessibilityLabel("View options")
                Menu { actions } label: {
                    Group {
                        if model.working { ProgressView() } else { Image(systemName: "ellipsis").foregroundStyle(Color.primary) }
                    }
                    .frame(width: 46, height: 44).contentShape(Rectangle())
                }
                .accessibilityLabel("This album")
                .disabled(model.working)
            }
            .padding(.horizontal, 2)
            .glassEffect(.regular.interactive(), in: .capsule)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 4)
    }

    @ViewBuilder private var actions: some View {
        if let a = model.open {
            Button { model.favourite(true) } label: { Label("Make all favourites", systemImage: "heart.fill") }
            Button { model.favourite(false) } label: { Label("Take all out of favourites", systemImage: "heart.slash") }
            Button { openURL(API.url("api/albums/\(a.id)/download")) } label: { Label("Download the originals", systemImage: "arrow.down.circle") }
            Divider()
            // Hiding is only proposed here: it is looked over and applied on the review page, as any change set.
            if a.hiding {
                Button { reviewing = true } label: { Label("Review what it hides", systemImage: "eye.slash") }
            } else {
                Button { model.proposeHiding { reviewing = true } } label: { Label("Hide these photos…", systemImage: "eye.slash") }
            }
            if a.applied == 0 {
                Button(role: .destructive) { deleting = true } label: { Label("Delete this album", systemImage: "trash") }
            }
        }
    }

    @ViewBuilder private var empty: some View {
        VStack(spacing: 12) {
            switch model.opening {
            case .loading:
                ProgressView()
            case .failed(let why):
                EstateMark(progress: 0.1).frame(width: 110).opacity(0.5)
                Text("Couldn’t open this album").font(.title(22, .bold))
                Text(why).font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                Button("Try again") { Task { await model.fetch() } }.buttonStyle(.glassProminent)
            case .ready:
                EstateMark(progress: 0.1).frame(width: 110).opacity(0.5)
                Text("Nothing in it yet").font(.title(22, .bold))
                Text(model.open?.live == true ? "Its search finds no photos now."
                     : "Photos are added to an album by asking for them in a chat connected to this library, or from a search.")
                    .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
            }
        }
        .padding(36)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .allowsHitTesting(model.opening != .ready)
    }
}
