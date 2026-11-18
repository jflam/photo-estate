import SwiftUI

/// What is on this iPhone, and what it should keep: opened from the Library's bar. The ring is the
/// point of it: how much of the library is here, and so works with no network.
struct ThisPhoneView: View {
    @ObservedObject private var sync = SyncEngine.shared
    @ObservedObject private var downloads = Downloader.shared
    @Environment(\.dismiss) private var dismiss
    @State private var usage = ImageStore.Usage()
    @State private var confirmClear = false
    #if !NO_BACKGROUND_UPLOAD
    @StateObject private var uploader = ForegroundUploader()
    #endif

    private var held: Double { downloads.thumbsWant > 0 ? Double(downloads.thumbsHave) / Double(downloads.thumbsWant) : 0 }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    VStack(spacing: 14) {
                        ZStack {
                            Circle().stroke(Color.well, lineWidth: 10)
                            Circle().trim(from: 0, to: held).stroke(Color.sun, style: StrokeStyle(lineWidth: 10, lineCap: .round))
                                .rotationEffect(.degrees(-90)).animation(.easeOut(duration: 0.5), value: held)
                            VStack(spacing: 0) {
                                Text(held, format: .percent.precision(.fractionLength(0))).font(.title(34, .bold)).monospacedDigit().contentTransition(.numericText())
                                Text("on this iPhone").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .frame(width: 150, height: 150)
                        .padding(.top, 8)
                        Text("\(sync.photos.formatted()) photos").font(.title(22))
                        Text(statusLine).font(.footnote).foregroundStyle(sync.online ? Color.secondary : Color.sun).multilineTextAlignment(.center)
                    }
                    .frame(maxWidth: .infinity)
                    .listRowBackground(Color.clear)
                }

                Section {
                    Toggle("Every thumbnail", isOn: $downloads.keepThumbs)
                    Picker("Full-screen copies", selection: $downloads.full) {
                        ForEach(Downloader.Full.allCases) { Text($0.title).tag($0) }
                    }
                    if downloads.full != .viewed {
                        LabeledContent("Full-screen kept", value: "\(downloads.fullHave.formatted()) of \(downloads.fullWant.formatted())")
                    }
                    Toggle("Use mobile data", isOn: $downloads.cellular)
                } header: { Text("Keep") } footer: {
                    Text("Thumbnails let the whole library scroll with no network: about 2 GB for 100,000 photos. Full-screen copies are kept for what you choose here, and for any photo you open.")
                }

                Section {
                    LabeledContent("Thumbnails", value: bytes(usage.thumbBytes))
                    LabeledContent("Full-screen", value: "\(bytes(usage.fullBytes))  ·  \(usage.fullCount.formatted())")
                    Stepper("Full-screen up to \(downloads.capGB) GB", value: $downloads.capGB, in: 1...200)
                    Button("Remove full-screen copies", role: .destructive) { confirmClear = true }
                } header: { Text("Space") } footer: {
                    Text("When full, the photos you looked at longest ago go first. Photos you asked to keep never do.")
                }

                Section {
                    if LocalPhotos.shared.allowed {
                        LabeledContent("Only on this iPhone", value: "\(LocalPhotos.shared.pending.count.formatted()) photos")
                    }
                    LabeledContent("Last synced", value: sync.lastSynced.map { $0.formatted(.relative(presentation: .named)) } ?? "Never")
                    Button(sync.state == .syncing ? "Syncing…" : "Sync now") { Task { ImageStore.shared.retryNetwork(); await sync.sync(); downloads.restart() } }
                        .disabled(sync.state == .syncing)
                    #if !NO_BACKGROUND_UPLOAD
                    NavigationLink("Uploads from this iPhone") { UploadsView(uploader: uploader) }
                    #endif
                    NavigationLink("Name people") { web("people", "People") }
                    NavigationLink("Library info") { web("stats", "Library info") }
                } header: { Text("Server") } footer: {
                    Text([Server.baseURL.host() ?? "", "Build \(Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "?")"].filter { !$0.isEmpty }.joined(separator: "  ·  "))
                }
            }
            .scrollContentBackground(.hidden)
            .background(Color.paper)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() }.fontWeight(.semibold) } }
            .task(id: "\(downloads.thumbsHave)/\(downloads.fullHave)/\(downloads.running)") { usage = await ImageStore.shared.usage() }
            .confirmationDialog("Remove the full-screen copies kept on this iPhone?", isPresented: $confirmClear, titleVisibility: .visible) {
                Button("Remove \(bytes(usage.fullBytes))", role: .destructive) {
                    Task { downloads.stop(); await ImageStore.shared.clearFull(); usage = await ImageStore.shared.usage(); downloads.restart() }
                }
            } message: { Text("They stay on the server, and are fetched again when needed.") }
        }
        .tint(.sun)
    }

    private var statusLine: String {
        if case .failed(let why) = sync.state { return "\(why) Working from what is here." }
        if sync.state == .syncing { return "Catching up with the server…" }
        if !downloads.status.isEmpty { return downloads.status }
        return sync.online ? "Up to date." : "Offline. Working from what is here."
    }

    private func web(_ path: String, _ title: String) -> some View {
        WebView(url: Server.baseURL.appendingPathComponent(path)).ignoresSafeArea(edges: .bottom)
            .navigationTitle(title).navigationBarTitleDisplayMode(.inline)
    }

    private func bytes(_ n: Int64) -> String { ByteCountFormatter.string(fromByteCount: n, countStyle: .file) }
}
