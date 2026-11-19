import SwiftUI

/// Three places: the Library, which is every photo in time order; Albums, which are sets of them made
/// on the server; and Search, which is finding some of them. What the phone keeps, and what it sends to the server, sit behind the Library's bar.
struct ContentView: View {
    @StateObject private var router = Router()
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
            }
        }
    }
}
