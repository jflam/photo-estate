import Photos
import SwiftUI

struct UploadsView: View {
    @ObservedObject var uploader: ForegroundUploader
    @State private var access = PHPhotoLibrary.authorizationStatus(for: .readWrite)
    @State private var systemUploader = "Off"
    @State private var days = 7

    var body: some View {
        NavigationStack {
            List {
                Section("Server") {
                    LabeledContent("Address", value: UploadRequest.baseURL.absoluteString)
                }
                Section {
                    LabeledContent("Photo access", value: accessText)
                    LabeledContent("Background upload", value: systemUploader)
                    Button("Turn on background upload") { Task { await enable() } }
                } header: { Text("New photos") } footer: {
                    Text("Once on, iOS sends each new photo to your server in the background, even with this app closed. Photos taken before you turn it on are not sent: use the button below for those.")
                }
                Section {
                    Stepper("Last \(days) days", value: $days, in: 1...3650)
                    Button(uploader.running ? "Stop" : "Upload recent photos") {
                        uploader.running ? uploader.stop() : uploader.start(days: days)
                    }
                    Text(uploader.status).font(.footnote).foregroundStyle(.secondary)
                    if uploader.total > 0 { ProgressView(value: Double(uploader.sent + uploader.skipped + uploader.failed), total: Double(max(uploader.total, 1))) }
                } header: { Text("Older photos") } footer: {
                    Text("Keep the app open while this runs. Photos the server already has are skipped.")
                }
            }
            .navigationTitle("Uploads")
            .onAppear { refresh() }
        }
    }

    private var accessText: String {
        switch access {
        case .authorized: "Full access"
        case .limited: "Limited (needs Full)"
        case .denied, .restricted: "Denied: change it in Settings"
        default: "Not asked yet"
        }
    }

    private func refresh() {
        access = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        systemUploader = PHPhotoLibrary.shared().uploadJobExtensionEnabled ? "On" : "Off"
    }

    private func enable() async {
        access = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
        guard access == .authorized else { return }
        do {
            try PHPhotoLibrary.shared().setUploadJobExtensionEnabled(true)
            refresh()
        } catch {
            systemUploader = "Could not turn on: \(error.localizedDescription)"
        }
    }
}
