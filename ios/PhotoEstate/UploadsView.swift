import Photos
import SwiftUI

struct UploadsView: View {
    @ObservedObject var uploader: ForegroundUploader
    @ObservedObject private var gate = GateModel.shared
    @State private var access = PHPhotoLibrary.authorizationStatus(for: .readWrite)
    @State private var systemUploader = "Off"
    @State private var systemOn = false
    @State private var days = 7
    @State private var history = ExtensionStatus.history()
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            List {
                Section("Server") {
                    LabeledContent("Address", value: UploadRequest.baseURL.absoluteString)
                    if let me = gate.me {
                        LabeledContent("You are", value: me.name)
                        Text(me.policyText).font(.footnote).foregroundStyle(.secondary)
                    }
                }
                Section {
                    LabeledContent("Photo access", value: accessText)
                    LabeledContent("Background upload", value: systemUploader)
                    if systemOn { LabeledContent("Last activity", value: lastActivity) }
                    Button(systemOn ? "Turn off background upload" : "Turn on background upload", role: systemOn ? .destructive : nil) {
                        Task { systemOn ? disable() : await enable() }
                    }
                } header: { Text("New photos") } footer: {
                    Text(gate.gated ? "Once on, iOS hands each new photo to this app in the background, and it waits in your Inbox for you to look over: nothing is sent until you have. Older photos are offered in the Inbox a year at a time."
                         : "Once on, iOS sends each new photo to your server in the background, even with this app closed. Photos taken before you turn it on are not sent: use the button below for those.")
                }
                if !gate.gated { Section {
                    Stepper("Last \(days) days", value: $days, in: 1...3650)
                    Button(uploader.running ? "Stop" : "Upload recent photos") {
                        uploader.running ? uploader.stop() : uploader.start(days: days)
                    }
                    Text(uploader.status).font(.footnote).foregroundStyle(.secondary)
                    if uploader.total > 0 { ProgressView(value: Double(uploader.sent + uploader.skipped + uploader.failed), total: Double(max(uploader.total, 1))) }
                } header: { Text("Older photos") } footer: {
                    Text("Keep the app open while this runs. Photos the server already has are skipped.")
                } }
                if systemOn { activity }
            }
            .navigationTitle("Uploads")
            .navigationBarTitleDisplayMode(.inline)
            .onAppear { refresh() }
            .refreshable { refresh() }
            .onChange(of: scenePhase) { _, p in if p == .active { refresh() } }
        }
    }

    // MARK: background upload activity

    /// "3 min ago · nothing new", for the line under the toggle.
    private var lastActivity: String {
        guard let e = history.first else { return "none yet" }
        let when = RelativeDateTimeFormatter().localizedString(for: e.at, relativeTo: Date())
        return "\(when) · \(summary(e, short: true))"
    }

    private func summary(_ e: ExtensionStatus.Entry, short: Bool = false) -> String {
        switch e.result {
        case "failure": return "failed" + (e.note.isEmpty ? "" : ": \(e.note)")
        case "processing": return "sending" + (e.queued > 0 ? " \(e.queued)" : "")
        default:
            if e.queued > 0 { return e.queued == 1 ? "1 photo sent to iOS" : "\(e.queued) photos sent to iOS" }
            return e.note.isEmpty ? "nothing new" : e.note
        }
    }

    private func row(_ e: ExtensionStatus.Entry) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(e.count > 1 ? "Nothing new ×\(e.count)" : summary(e).prefix(1).uppercased() + summary(e).dropFirst())
            Text(e.count > 1 ? "\(e.since.formatted(date: .abbreviated, time: .shortened)) to \(e.at.formatted(date: .omitted, time: .shortened))"
                             : e.at.formatted(date: .abbreviated, time: .standard))
                .font(.footnote).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var activity: some View {
        Section {
            if !ExtensionStatus.available {
                Text("App group not available").foregroundStyle(.secondary)
            } else if history.isEmpty {
                Text("iOS has not woken the upload extension yet. It decides when; plugging in on Wi-Fi helps.").foregroundStyle(.secondary)
            }
            ForEach(Array(history.prefix(3).enumerated()), id: \.offset) { _, e in row(e) }
            if history.count > 3 {
                DisclosureGroup("Earlier (\(history.count - 3))") {
                    ForEach(Array(history.dropFirst(3).enumerated()), id: \.offset) { _, e in row(e) }
                }
            }
        } header: { Text("Background upload activity") } footer: {
            Text("Each row is a time iOS woke the upload extension. Runs that found nothing are merged into one row.")
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
