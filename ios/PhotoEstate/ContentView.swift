import SwiftUI

/// Four places: the Library, which is every photo in time order; Albums, which are sets of them made
/// on the server; the Inbox, what waits for this member to decide (the phone's own batches of photos to look
/// over, and the server's items); and Search, which is finding some of them. What the phone keeps, and what
/// it sends to the server, sit behind the Library's bar.
struct ContentView: View {
    @StateObject private var router = Router()
    @ObservedObject private var gate = GateModel.shared
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        TabView(selection: $router.tab) {
            // The Library is home, so its tab carries the app's mark (the icon's house-shaped print) as a glyph
            // that takes the bar's tint, as a tab's image should; the icon itself, in colour, is not for bars.
            Tab("Library", image: "LibraryTab", value: Router.Tab.library) {
                LibraryView(model: router.library)
            }
            Tab("Albums", systemImage: "rectangle.stack", value: Router.Tab.albums) {
                AlbumsView(model: router.albums)
            }
            Tab("Inbox", systemImage: "tray", value: Router.Tab.inbox) {
                InboxView()
            }
            .badge(gate.badge)
            Tab(value: Router.Tab.search, role: .search) {
                SearchView(model: router.search)
            }
        }
        .tint(.sun)
        .environmentObject(router)
        .onChange(of: scenePhase, initial: true) { _, phase in
            guard phase == .active else { return }
            Task {                       // catch up with the server, then fetch what should be on the phone
                ImageStore.shared.retryNetwork()
                await SyncEngine.shared.sync()
                Downloader.shared.restart()
                LocalPhotos.shared.refresh()
                await GateModel.shared.refresh()     // who I am, the inbox, new photos to look over, what to send
            }
        }
    }
}
