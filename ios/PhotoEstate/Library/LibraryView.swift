import SwiftUI

/// The Library tab: every photo, newest first, and nothing else. The photos run to the edges of the
/// screen and the heading leaves with them; the handle at the right edge says where you are.
struct LibraryView: View {
    @ObservedObject var model: LibraryModel
    @EnvironmentObject private var router: Router
    @ObservedObject private var sync = SyncEngine.shared
    @ObservedObject private var downloads = Downloader.shared
    @State private var settings = false

    var body: some View {
        NavigationStack {
            GridView(data: model.grid, rowHeight: model.rowHeight, title: "Library", subtitle: model.subtitle,
                     onRefresh: { ImageStore.shared.retryNetwork(); await sync.sync() },
                     onRowHeight: { model.rowHeight = $0 },
                     viewer: { session in AnyView(ViewerScreen(session: session, model: model, route: router.find)) })
                .ignoresSafeArea()
                .overlay { if model.grid.tiles.isEmpty && !model.loading { Welcome() } }
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) { ViewMenu(model: model) }
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { settings = true } label: {
                            Image(systemName: !sync.online ? "wifi.slash" : sync.state == .syncing || downloads.running ? "arrow.down" : "iphone")
                                .contentTransition(.symbolEffect(.replace))
                                .foregroundStyle(sync.online ? Color.primary : Color.sun)
                        }
                        .accessibilityLabel("On this iPhone")
                    }
                }
        }
        .sheet(isPresented: $settings) { ThisPhoneView() }
        .onAppear { model.reload(reset: false) }
        .onChange(of: sync.revision) { _, _ in model.catalogChanged() }
    }
}

/// Order, size and near-identical shots: how a list of photos is laid out.
struct ViewMenu: View {
    @ObservedObject var model: LibraryModel

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
