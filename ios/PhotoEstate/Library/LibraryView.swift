import SwiftUI

/// The Library tab: every photo, newest first, including the ones on this iPhone the server doesn't
/// have yet. As in Photos, the bar at the top zooms out to a card a year or a card a month; a year opens
/// its months, a month opens its photos, and a swipe in from the left edge goes back the way you came,
/// with the view you came from showing underneath. The photos run to the edges of the screen and the
/// heading leaves with them; the handle at the right edge says where you are.
struct LibraryView: View {
    @ObservedObject var model: LibraryModel
    @EnvironmentObject private var router: Router
    @ObservedObject private var sync = SyncEngine.shared
    @ObservedObject private var local = LocalPhotos.shared
    @State private var settings = false
    @State private var barHeight: CGFloat = 0
    @State private var width: CGFloat = 400
    @State private var swipe: CGFloat = 0           // how far the current view has been pulled right

    var body: some View {
        let moving = swipe > 0
        NavigationStack {
            ZStack(alignment: .top) {
                if moving, let last = model.trail.last {    // the view a swipe goes back to, sliding in from behind
                    PeriodsView(model: model, scale: last.back, anchor: last.key, topInset: barHeight)
                        .id("behind")
                        .offset(x: (swipe - width) * 0.3)
                        .overlay { Color.black.opacity(0.12 * (1 - swipe / width)).ignoresSafeArea().allowsHitTesting(false) }
                }
                // The title alone: how many, what is hidden and the order are on the settings page.
                GridView(data: model.grid, rowHeight: model.rowHeight, title: model.title, subtitle: "", topInset: barHeight,
                         jump: model.jump,
                         onRefresh: { ImageStore.shared.retryNetwork(); await sync.sync(); local.refresh() },
                         onRowHeight: { model.rowHeight = $0 },
                         viewer: { session in AnyView(ViewerScreen(session: session, lookup: model.photo, route: router.find)) })
                    .ignoresSafeArea()
                    .opacity(model.scale == .all ? 1 : 0)
                    .allowsHitTesting(model.scale == .all)
                    .offset(x: model.scale == .all ? swipe : 0)
                    .shadow(color: .black.opacity(moving && model.scale == .all ? 0.25 : 0), radius: 10)
                if model.scale == .month {               // one month, alone: its photos, then the months either side
                    GridView(data: model.monthGrid, rowHeight: model.rowHeight, title: model.monthTitle, subtitle: model.monthSubtitle,
                             topInset: barHeight,
                             onRefresh: { ImageStore.shared.retryNetwork(); await sync.sync(); local.refresh() },
                             onRowHeight: { model.rowHeight = $0 },
                             footer: model.monthFooter, onFooter: { model.goMonth($0) },
                             viewer: { session in AnyView(ViewerScreen(session: session, lookup: model.photo, route: router.find)) })
                        .ignoresSafeArea()
                        .transition(.opacity)
                        .offset(x: swipe)
                        .shadow(color: .black.opacity(moving ? 0.25 : 0), radius: 10)
                }
                if model.grid.tiles.isEmpty && !model.loading { Welcome().ignoresSafeArea() }
                if model.scale.isCards && !model.grid.tiles.isEmpty {
                    PeriodsView(model: model, scale: model.scale, anchor: model.anchor, topInset: barHeight)
                        .id(model.scale)
                        .transition(.opacity)
                        .offset(x: swipe)
                        .shadow(color: .black.opacity(moving ? 0.25 : 0), radius: 10)
                }
                TopBar(model: model, settings: $settings)
                    .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { barHeight = $0 }
            }
            .toolbar(.hidden, for: .navigationBar)
            .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = max($0, 1) }
            .gesture(EdgeSwipe(changed: { x in
                guard !model.trail.isEmpty else { return }
                swipe = max(0, min(x, width))
            }, ended: { x, v in
                guard !model.trail.isEmpty, swipe > 0 else { swipe = 0; return }
                if x > width * 0.4 || v > 500 {
                    Haptics.tick()
                    withAnimation(.snappy(duration: 0.22)) { swipe = width } completion: {
                        var t = Transaction()
                        t.disablesAnimations = true
                        withTransaction(t) { model.retrace(); swipe = 0 }
                    }
                } else {
                    withAnimation(.snappy(duration: 0.22)) { swipe = 0 }
                }
            }))
        }
        .sheet(isPresented: $settings) { ThisPhoneView(library: model) }
        .onAppear { local.start(); model.reload(reset: false) }
        .onChange(of: sync.revision) { _, _ in local.refresh(); model.catalogChanged() }
        .onChange(of: local.revision) { _, _ in model.catalogChanged() }
    }
}

/// The Library's one row of controls, floating over the photos: Years / Months / All on the left,
/// the view options and what is on this iPhone on the right.
struct TopBar: View {
    @ObservedObject var model: LibraryModel
    @Binding var settings: Bool
    @ObservedObject private var sync = SyncEngine.shared
    @ObservedObject private var downloads = Downloader.shared

    var body: some View {
        HStack(spacing: 8) {
            ScalePicker(model: model)
            Spacer(minLength: 0)
            // Two buttons side by side: each is a full finger wide and answers anywhere in its own half, with a
            // gap between, so a tap meant for one never lands on the other.
            HStack(spacing: 6) {
                ViewMenu(model: model, width: 46)
                Button { settings = true } label: {
                    Image(systemName: !sync.online ? "wifi.slash" : sync.state == .syncing || downloads.running ? "arrow.down" : "iphone")
                        .contentTransition(.symbolEffect(.replace))
                        .foregroundStyle(sync.online ? Color.primary : Color.sun)
                        .frame(width: 46, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("On this iPhone")
            }
            .padding(.horizontal, 2)
            .glassEffect(.regular.interactive(), in: .capsule)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 4)
    }
}

/// Years, Months, All: how far out the Library is zoomed.
struct ScalePicker: View {
    @ObservedObject var model: LibraryModel
    @Namespace private var pill

    var body: some View {
        HStack(spacing: 0) {
            ForEach(Scale.bar) { segment($0) }
        }
        .padding(3)
        .glassEffect(.regular.interactive(), in: .capsule)
    }

    private func segment(_ s: Scale) -> some View {
        let on = model.scale == s
        return Button {
            guard !on else { return }
            Haptics.tick()
            model.pick(s)
        } label: {
            Text(s.title)
                .font(.subheadline.weight(.medium))
                .padding(.horizontal, 13)
                .frame(height: 34)
                .foregroundStyle(on ? Color.black : Color.primary)
                .background { if on { Capsule().fill(Color.sunLit).matchedGeometryEffect(id: "pill", in: pill) } }
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(s == .all ? "All photos" : s.title)
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}

/// A card a year or a card a month, newest first (or oldest, as the photos are ordered). Tapping one
/// zooms in: a year to its months, a month to its photos.
struct PeriodsView: View {
    @ObservedObject var model: LibraryModel
    let scale: Scale
    var anchor: String? = nil          // start at the first card whose key starts with this
    let topInset: CGFloat

    var body: some View {
        let periods = scale == .years ? model.years : model.months
        ScrollViewReader { reader in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: scale == .years ? 16 : 12) {
                    Text(model.title).font(.title(28, .bold))
                    .padding(.horizontal, 2)
                    .padding(.top, 12)
                    .padding(.bottom, 4)
                    ForEach(Array(periods.enumerated()), id: \.element.id) { i, p in
                        // A year's months are under that year.
                        if scale == .months, i == 0 || periods[i - 1].key.prefix(4) != p.key.prefix(4) {
                            Text(String(p.key.prefix(4))).font(.title(24, .bold))
                                .padding(.horizontal, 2).padding(.top, i == 0 ? 0 : 12)
                                .accessibilityAddTraits(.isHeader)
                                .id("year " + p.key.prefix(4))
                        }
                        Button { model.open(p) } label: { PeriodCard(period: p, big: scale == .years) }
                            .buttonStyle(CardPress())
                            .id(p.key)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.bottom, 24)
            }
            .safeAreaPadding(.top, topInset + 6)           // under the bar, and where a tapped year scrolls to
            .scrollIndicators(.hidden)
            .background(Color.paper)
            .overlay(alignment: .top) {                     // paper fading out below the status bar, as over the grid
                GeometryReader { g in
                    LinearGradient(stops: [.init(color: .paper, location: 0), .init(color: .paper.opacity(0.85), location: 0.7), .init(color: .paper.opacity(0), location: 1)],
                                   startPoint: .top, endPoint: .bottom)
                        .frame(height: g.safeAreaInsets.top + 26)
                        .ignoresSafeArea()
                }
                .allowsHitTesting(false)
            }
            .onAppear {
                // Coming in from a year, or back from a month or a year: start at that card.
                // A year opens at its heading.
                if let anchor, let key = periods.first(where: { $0.key.hasPrefix(anchor) })?.key {
                    reader.scrollTo(scale == .months && anchor.count == 4 ? "year " + anchor : key, anchor: .top)
                }
            }
        }
    }
}

/// One year's or month's card: its photo, edge to edge, with the year or month over it.
struct PeriodCard: View {
    let period: Period
    let big: Bool
    @State private var image: UIImage?

    var body: some View {
        let title = big ? period.key : Dates.heading(period.key)
        let detail = [big ? "" : period.places, "\(period.count.formatted()) \(period.count == 1 ? "photo" : "photos")"]
            .filter { !$0.isEmpty }.joined(separator: " · ")
        Color.well
            .frame(height: big ? 300 : 210)
            .overlay {
                if let image {
                    Image(uiImage: image).resizable().scaledToFill().transition(.opacity)
                }
            }
            .overlay(alignment: .topLeading) {
                LinearGradient(colors: [.black.opacity(0.5), .clear], startPoint: .top, endPoint: .center)
                    .allowsHitTesting(false)
            }
            .overlay(alignment: .topLeading) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.title(big ? 34 : 24, .bold))
                    Text(detail).font(.subheadline.weight(.medium)).opacity(0.9)
                    if big && !period.places.isEmpty {
                        Text(period.places).font(.subheadline).opacity(0.85).lineLimit(1)
                    }
                }
                .foregroundStyle(.white)
                .shadow(color: .black.opacity(0.35), radius: 3)
                .padding(16)
            }
            .clipShape(RoundedRectangle(cornerRadius: big ? 22 : 18, style: .continuous))
            .contentShape(RoundedRectangle(cornerRadius: big ? 22 : 18, style: .continuous))
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(title), \(detail)")
            .accessibilityAddTraits(.isButton)
            .task(id: period.cover.sha) { await load(period.cover.sha) }
    }

    /// The thumbnail at once, then a sharper one: a card is far wider than a grid tile.
    private func load(_ sha: String) async {
        image = ImageStore.shared.ready(sha)
        if image == nil, let t = await ImageStore.shared.thumb(sha), !Task.isCancelled { withAnimation(.easeOut(duration: 0.2)) { image = t } }
        try? await Task.sleep(for: .milliseconds(150))         // scrolled past: don't fetch the big one
        guard !Task.isCancelled, let big = await ImageStore.shared.image(.preview, sha), !Task.isCancelled else { return }
        image = big
    }
}

/// A swipe in from the left edge of the screen. A UIKit edge pan, so it takes nothing from the grid:
/// taps and scrolling go on as before.
struct EdgeSwipe: UIGestureRecognizerRepresentable {
    var changed: (CGFloat) -> Void
    var ended: (_ distance: CGFloat, _ speed: CGFloat) -> Void

    func makeUIGestureRecognizer(context: Context) -> UIScreenEdgePanGestureRecognizer {
        let g = UIScreenEdgePanGestureRecognizer()
        g.edges = .left
        return g
    }

    func handleUIGestureRecognizerAction(_ g: UIScreenEdgePanGestureRecognizer, context: Context) {
        let x = g.translation(in: g.view).x
        switch g.state {
        case .began, .changed: changed(x)
        case .ended: ended(x, g.velocity(in: g.view).x)
        case .cancelled, .failed: ended(0, 0)
        default: break
        }
    }
}

/// A card gives a little under the finger.
struct CardPress: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .animation(.snappy(duration: 0.18), value: configuration.isPressed)
    }
}

/// Order, size and near-identical shots: how a list of photos is laid out.
struct ViewMenu: View {
    @ObservedObject var model: LibraryModel
    var width: CGFloat = 44
    @ObservedObject private var local = LocalPhotos.shared

    var body: some View {
        Menu {
            if model.ranked {
                // A described search: the photos that match, closest first or your best first. ("More like this"
                // is by likeness and nothing else.)
                if model.query.similar == nil && model.hasBest {
                    Picker("Order", selection: Binding(get: { model.order }, set: { model.setOrder($0) })) {
                        Label("Best match first", systemImage: "text.magnifyingglass").tag(Order.match)
                        Label("Best first", systemImage: "sparkles").tag(Order.best)
                    }
                }
            } else {
                Picker("Order", selection: Binding(get: { model.order }, set: { model.setOrder($0) })) {
                    Label("Newest first", systemImage: "arrow.down").tag(Order.new)
                    Label("Oldest first", systemImage: "arrow.up").tag(Order.old)
                    if model.hasBest {
                        Label(model.query.isEmpty && model.scale != .month ? "Best first in each month" : "Best first", systemImage: "sparkles").tag(Order.best)
                    }
                }
            }
            Picker("Photo size", selection: Binding(get: { Estate.densities.min { abs($0 - model.rowHeight) < abs($1 - model.rowHeight) } ?? 128 },
                                                    set: { model.rowHeight = $0 })) {
                Label("Smallest", systemImage: "square.grid.4x3.fill").tag(Estate.densities[0])
                Label("Small", systemImage: "square.grid.3x3.fill").tag(Estate.densities[1])
                Label("Medium", systemImage: "square.grid.3x2.fill").tag(Estate.densities[2])
                Label("Large", systemImage: "square.grid.2x2.fill").tag(Estate.densities[3])
                Label("Largest", systemImage: "rectangle.grid.1x2.fill").tag(Estate.densities[4])
            }
            Toggle("Stack near-identical shots", systemImage: "square.stack", isOn: $model.hideDups)
            Toggle("Hide screenshots", systemImage: "rectangle.portrait.slash", isOn: $model.hideShots)
            Toggle("Show photos not yet on the server", systemImage: "iphone", isOn: $local.show)
        } label: {
            // The whole box is the button, not just the glyph in it. A dot says the order is not the usual one.
            Image(systemName: "line.3.horizontal.decrease").foregroundStyle(Color.primary)
                .overlay(alignment: .topTrailing) {
                    if model.orderNote != nil { Circle().fill(Color.sun).frame(width: 7, height: 7).offset(x: 5, y: -4) }
                }
                .frame(width: width, height: 44)
                .contentShape(Rectangle())
        }
        .accessibilityLabel("View options")
        .accessibilityValue(model.orderNote ?? "")
    }
}

/// Before there are photos: the first fetch, or no way to reach the server yet.
struct Welcome: View {
    @ObservedObject private var sync = SyncEngine.shared

    var body: some View {
        let failed: String? = { if case .failed(let why) = sync.state { return why }; return nil }()
        VStack(spacing: 18) {
            EstateMark(progress: failed != nil ? 0.15 : sync.serverTotal > 0 ? Double(sync.photos) / Double(max(sync.serverTotal, 1)) : 0.3)
                .frame(width: 150)
                .padding(.bottom, 6)
            if let failed {
                Text("Can't reach your library").font(.title(24, .bold))
                Text("\(failed)\nYour photos appear here once this iPhone has fetched the catalog from \(Server.baseURL.host() ?? "your server").")
                    .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                Button("Try again") { Task { await sync.sync() } }
                    .buttonStyle(.glassProminent).padding(.top, 4)
            } else {
                Text("Bringing your library home").font(.title(24, .bold))
                Text(sync.serverTotal > 0 ? "\(sync.photos.formatted()) of \(sync.serverTotal.formatted()) photos" : "Fetching the catalog…")
                    .font(.subheadline).foregroundStyle(.secondary).monospacedDigit().contentTransition(.numericText())
            }
        }
        .padding(36)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.paper)
    }
}
