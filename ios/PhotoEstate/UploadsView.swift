import Photos
import SwiftUI

struct UploadsView: View {
    @ObservedObject var uploader: ForegroundUploader
    @State private var access = PHPhotoLibrary.authorizationStatus(for: .readWrite)
    @State private var systemUploader = "Off"
    @State private var systemOn = false
    @State private var days = 7
    @State private var history = ExtensionStatus.history()

    var body: some View {
        NavigationStack {
            List {
                Section("Server") {
                    LabeledContent("Address", value: UploadRequest.baseURL.absoluteString)
                }
                Section {
                    LabeledContent("Photo access", value: accessText)
                    LabeledContent("Background upload", value: systemUploader)
                    Button(systemOn ? "Turn off background upload" : "Turn on background upload", role: systemOn ? .destructive : nil) {
                        Task { systemOn ? disable() : await enable() }
                    }
                } header: { Text("New photos") } footer: {
                    Text("Once on, iOS sends each new photo to your server in the background, even with this app closed. Photos taken before you turn it on are not sent: use the button below for those.")
                }
                Section {
                    if !ExtensionStatus.available { Text("App group not available").foregroundStyle(.secondary) }
                    else if history.isEmpty { Text("The extension has not run yet.").foregroundStyle(.secondary) }
                    ForEach(Array(history.enumerated()), id: \.offset) { _, e in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(e.at.formatted(date: .abbreviated, time: .standard))
                            Text("\(e.result), \(e.queued) queued" + (e.note.isEmpty ? "" : " · \(e.note)")).font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                } header: { Text("Background upload log") } footer: {
                    Text("Each line is one time iOS woke the upload extension. Pull down to refresh.")
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
            .refreshable { refresh() }
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
        history = ExtensionStatus.history()
        access = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        systemOn = PHPhotoLibrary.shared().uploadJobExtensionEnabled
        systemUploader = systemOn ? "On" : "Off"
    }

    private func disable() {
        do { try PHPhotoLibrary.shared().disableUploadJobExtension(); refresh() }
        catch { let e = error as NSError; systemUploader = "Could not turn off: \(e.domain) \(e.code)" }
    }

    private func enable() async {
        access = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
        guard access == .authorized else { return }
        // Enabling twice is an error (PHPhotosErrorDomain 3202), so only enable what is off.
        if PHPhotoLibrary.shared().uploadJobExtensionEnabled { refresh(); return }
        do {
            try PHPhotoLibrary.shared().enableUploadJobExtension(with: nil)
            refresh()
        } catch {
            let e = error as NSError
            systemUploader = "Could not turn on: \(e.domain) \(e.code) \(e.localizedDescription)"
        }
    }
}
