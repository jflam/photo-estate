import SwiftUI

/// The Library tab: every photo, newest first, including the ones on this iPhone the server doesn't
/// have yet; one bar at the top narrows it to today, this week or this month and holds the view options. The photos run to the edges of the
/// screen and the heading leaves with them; the handle at the right edge says where you are.
struct LibraryView: View {
    @ObservedObject var model: LibraryModel
    @EnvironmentObject private var router: Router
    @ObservedObject private var sync = SyncEngine.shared
    @ObservedObject private var local = LocalPhotos.shared
    @State private var settings = false
    @State private var barHeight: CGFloat = 0

    var body: some View {
        NavigationStack {
            ZStack(alignment: .top) {
                GridView(data: model.grid, rowHeight: model.rowHeight, title: model.title, subtitle: model.subtitle, topInset: barHeight,
                         onRefresh: { ImageStore.shared.retryNetwork(); await sync.sync(); local.refresh() },
                         onRowHeight: { model.rowHeight = $0 },
                         viewer: { session in AnyView(ViewerScreen(session: session, model: model, route: router.find)) })
                    .ignoresSafeArea()
                    .overlay {
                        if model.grid.tiles.isEmpty && !model.loading {
                            if let r = model.query.recent { NothingRecent(recent: r) { model.query.recent = nil } } else { Welcome() }
                        }
                    }
                TopBar(model: model, settings: $settings)
                    .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { barHeight = $0 }
            }
            .toolbar(.hidden, for: .navigationBar)
        }
        .sheet(isPresented: $settings) { ThisPhoneView() }
        .onAppear { local.start(); model.reload(reset: false) }
        .onChange(of: sync.revision) { _, _ in local.refresh(); model.catalogChanged() }
        .onChange(of: local.revision) { _, _ in model.catalogChanged() }
    }
}

/// The Library's one row of controls, floating over the photos: how far back to look on the left,
/// the view options and what is on this iPhone on the right.
struct TopBar: View {
    @ObservedObject var model: LibraryModel
    @Binding var settings: Bool
    @ObservedObject private var sync = SyncEngine.shared
    @ObservedObject private var downloads = Downloader.shared

    var body: some View {
        HStack(spacing: 8) {
            RecentPicker(model: model)
            Spacer(minLength: 0)
            HStack(spacing: 0) {
                ViewMenu(model: model).frame(width: 42, height: 40)
                Button { settings = true } label: {
                    Image(systemName: !sync.online ? "wifi.slash" : sync.state == .syncing || downloads.running ? "arrow.down" : "iphone")
                        .contentTransition(.symbolEffect(.replace))
                        .foregroundStyle(sync.online ? Color.primary : Color.sun)
                        .frame(width: 42, height: 40)
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

/// All of it, or just the recent past: what was taken today, this week, this month.
struct RecentPicker: View {
    @ObservedObject var model: LibraryModel

    var body: some View {
        HStack(spacing: 0) {
            segment("All", nil)
            ForEach(Recent.allCases) { segment($0.short, $0) }
        }
        .padding(3)
        .glassEffect(.regular.interactive(), in: .capsule)
    }

    private func segment(_ title: String, _ r: Recent?) -> some View {
        let on = model.query.recent == r
        return Button {
            guard !on else { return }
            Haptics.tick()
            var q = model.query
            q.recent = r
            withAnimation(.snappy(duration: 0.2)) { model.query = q }
        } label: {
            Text(title)
                .font(.subheadline.weight(.medium))
                .padding(.horizontal, 12)
                .frame(height: 34)
                .foregroundStyle(on ? Color.black : Color.primary)
                .background { if on { Capsule().fill(Color.sunLit) } }
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(r?.title ?? "All photos")
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}

/// Nothing taken in the stretch asked for.
struct NothingRecent: View {
    let recent: Recent
    var all: () -> Void

    var body: some View {
        VStack(spacing: 14) {
            EstateMark(progress: 0.1).frame(width: 110).opacity(0.5)
            Text(recent == .today ? "No photos today yet" : "No photos \(recent.title.lowercased()) yet").font(.title(22, .bold))
            Button("Show all photos", action: all).buttonStyle(.glass).padding(.top, 4)
        }
        .padding(36)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// Order, size and near-identical shots: how a list of photos is laid out.
struct ViewMenu: View {
    @ObservedObject var model: LibraryModel
    @ObservedObject private var local = LocalPhotos.shared

    var body: some View {
        Menu {
            if !model.ranked {
                Picker("Order", selection: Binding(get: { model.oldFirst }, set: { v in var q = model.query; q.oldFirst = v; model.query = q })) {
                    Label("Newest first", systemImage: "arrow.down").tag(false)
                    Label("Oldest first", systemImage: "arrow.up").tag(true)
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
            Toggle("Hide near-identical shots", systemImage: "square.on.square.dashed", isOn: $model.hideDups)
            Toggle("Hide screenshots", systemImage: "rectangle.portrait.slash", isOn: $model.hideShots)
            Toggle("Show photos not yet on the server", systemImage: "iphone", isOn: $local.show)
        } label: { Image(systemName: "line.3.horizontal.decrease").foregroundStyle(Color.primary) }
        .accessibilityLabel("View options")
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
