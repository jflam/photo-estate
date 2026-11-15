import SwiftUI

/// Library: the photos, native, from the phone's own catalog and image store. Uploads: the phone's
/// photo library, sent to the server. Offline: what is kept on the phone.
struct ContentView: View {
    #if !NO_BACKGROUND_UPLOAD
    @StateObject private var uploader = ForegroundUploader()
    #endif
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        TabView {
            LibraryView()
                .tabItem { Label("Library", systemImage: "photo.on.rectangle") }
            #if !NO_BACKGROUND_UPLOAD
            UploadsView(uploader: uploader)
                .tabItem { Label("Uploads", systemImage: "icloud.and.arrow.up") }
            #endif
            OfflineView()
                .tabItem { Label("Offline", systemImage: "arrow.down.circle") }
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            guard phase == .active else { return }
            Task {                       // catch up with the server, then fetch what should be on the phone
                ImageStore.shared.retryNetwork()
                await SyncEngine.shared.sync()
                Downloader.shared.restart()
            }
        }
    }
}
