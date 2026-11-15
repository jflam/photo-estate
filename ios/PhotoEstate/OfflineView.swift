import SwiftUI

/// What is on this iPhone and what it should keep: the catalog's sync with the server, and the photos
/// stored for use with no network.
struct OfflineView: View {
    @ObservedObject private var sync = SyncEngine.shared
    @ObservedObject private var downloads = Downloader.shared
    @State private var usage = ImageStore.Usage()
    @State private var confirmClear = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    LabeledContent("Photos", value: sync.photos.formatted())
                    LabeledContent("Last synced", value: sync.lastSynced.map { $0.formatted(.relative(presentation: .named)) } ?? "Never")
                    LabeledContent("Server", value: sync.online ? "Reachable" : "Not reachable")
                    switch sync.state {
                    case .syncing: HStack { Text("Syncing…"); Spacer(); ProgressView() }
                    case .failed(let why): Text(why).font(.footnote).foregroundStyle(.orange)
                    case .idle: EmptyView()
                    }
                    Button("Sync now") { Task { ImageStore.shared.retryNetwork(); await sync.sync(); downloads.restart() } }
                        .disabled(sync.state == .syncing)
                } header: { Text("Catalog") } footer: {
                    Text("This iPhone keeps its own copy of the catalog from \(Server.baseURL.host() ?? "your server"), so browsing, filters and searches for people, places and dates work with no network. It catches up whenever the app opens.")
                }

                Section {
                    Toggle("Keep every thumbnail", isOn: $downloads.keepThumbs)
                    progress("Thumbnails", downloads.thumbsHave, downloads.thumbsWant, usage.thumbBytes)
                    Picker("Full-screen photos", selection: $downloads.full) {
                        ForEach(Downloader.Full.allCases) { Text($0.title).tag($0) }
                    }
                    if downloads.full != .viewed { progress("Full-screen", downloads.fullHave, downloads.fullWant, nil) }
                    Toggle("Download over mobile data", isOn: $downloads.cellular)
                    if !downloads.status.isEmpty {
                        HStack {
                            Text(downloads.status).font(.footnote).foregroundStyle(.secondary)
                            Spacer()
                            if downloads.running { ProgressView() }
                        }
                    }
                } header: { Text("Photos on this iPhone") } footer: {
                    Text("Thumbnails make the whole library scroll with no network (about 2 GB per 100,000 photos). Full-screen copies are kept for the photos you choose here, and for any photo you open.")
                }

                Section {
                    Stepper("Up to \(downloads.capGB) GB", value: $downloads.capGB, in: 1...200)
                    LabeledContent("Used now", value: "\(bytes(usage.fullBytes)) · \(usage.fullCount.formatted()) photos")
                    Button("Remove full-screen photos", role: .destructive) { confirmClear = true }
                } header: { Text("Full-screen storage") } footer: {
                    Text("When full, the photos you looked at longest ago are removed first. Photos you asked to keep are never removed.")
                }

                Section("On the server") {
                    NavigationLink("People") { WebView(url: Server.baseURL.appendingPathComponent("people")).ignoresSafeArea(edges: .bottom).navigationTitle("People").navigationBarTitleDisplayMode(.inline) }
                    NavigationLink("Library info") { WebView(url: Server.baseURL.appendingPathComponent("stats")).ignoresSafeArea(edges: .bottom).navigationTitle("Library info").navigationBarTitleDisplayMode(.inline) }
                    NavigationLink("Web app") { WebView(url: Server.baseURL).ignoresSafeArea(edges: .bottom).navigationTitle("Web app").navigationBarTitleDisplayMode(.inline) }
                }
            }
            .navigationTitle("Offline")
            .task(id: "\(downloads.thumbsHave)/\(downloads.fullHave)/\(downloads.running)") { usage = await ImageStore.shared.usage() }
            .confirmationDialog("Remove the full-screen photos kept on this iPhone?", isPresented: $confirmClear, titleVisibility: .visible) {
                Button("Remove \(bytes(usage.fullBytes))", role: .destructive) {
                    Task { downloads.stop(); await ImageStore.shared.clearFull(); usage = await ImageStore.shared.usage(); downloads.restart() }
                }
            } message: { Text("They stay on the server, and are fetched again when needed.") }
        }
    }

    private func bytes(_ n: Int64) -> String { ByteCountFormatter.string(fromByteCount: n, countStyle: .file) }

    private func progress(_ title: String, _ have: Int, _ want: Int, _ size: Int64?) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(title)
                Spacer()
                Text("\(have.formatted()) of \(want.formatted())" + (size.map { " · \(bytes($0))" } ?? "")).foregroundStyle(.secondary).monospacedDigit()
            }
            ProgressView(value: Double(have), total: Double(max(want, 1)))
        }
    }
}
