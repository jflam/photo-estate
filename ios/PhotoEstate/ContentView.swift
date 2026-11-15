import SwiftUI

/// The photo app is the web app, so the Library tab is just a web view of it. The Uploads tab is the
/// one thing the web app can't do: reach into the phone's photo library.
struct ContentView: View {
    @StateObject private var uploader = ForegroundUploader()

    var body: some View {
        TabView {
            WebView(url: UploadRequest.baseURL)
                .ignoresSafeArea()                 // the page handles the notch and home bar itself
                .tabItem { Label("Library", systemImage: "photo.on.rectangle") }
            UploadsView(uploader: uploader)
                .tabItem { Label("Uploads", systemImage: "icloud.and.arrow.up") }
        }
    }
}
