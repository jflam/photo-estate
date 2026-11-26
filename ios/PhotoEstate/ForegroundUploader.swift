import Photos
import SwiftUI

/// Sends photos from inside the app. It is the way to send photos that were taken before the system
/// uploader was switched on, the fallback if the system uploader does not run, and how the photos a
/// member approved in the Inbox are sent at once (the phone gate: PhoneGate.swift).
///
/// Each resource is exported to a temporary file (fetching it from iCloud if it was optimised away),
/// hashed, uploaded in one POST with its hash (`X-Content-SHA256`), and forgotten. The server ignores files
/// it already has, so running this twice, or alongside the system uploader, only costs the bandwidth. A photo
/// this phone holds back by its identifier (kept back, or in a batch to look over: `Gate.held`) is not read. A
/// file whose hash this member opted out of is not sent: the phone knows (`Gate.refused`), or the server
/// says so before the body (`X-Upload-Refused`), and the photo is kept back on this iPhone from then on.
@MainActor
final class ForegroundUploader: ObservableObject {
    static let shared = ForegroundUploader()

    @Published var running = false
    @Published var status = "Nothing sent yet."
    @Published var sent = 0
    @Published var skipped = 0
    @Published var kept = 0             // opted out: not sent
    @Published var failed = 0
    @Published var total = 0

    private var task: Task<Void, Never>?

    /// The last `days` days of photos ("Upload recent photos", the `everything` policy's backlog).
    func start(days: Int) {
        guard !running else { return }
        let opts = PHFetchOptions()
        opts.predicate = NSPredicate(format: "creationDate > %@", Date().addingTimeInterval(-Double(days) * 86400) as NSDate)
        opts.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
        begin(PHAsset.fetchAssets(with: opts), empty: "No photos in the last \(days) days.")
    }

    /// These photos, approved in the Inbox (by identifier: PhotoKit leaves out what is in the Hidden album).
    func send(_ ids: [String]) {
        guard !running, !ids.isEmpty else { return }
        begin(PHAsset.fetchAssets(withLocalIdentifiers: ids, options: nil), empty: "Nothing to send.")
    }

    func stop() { task?.cancel() }

    private func begin(_ assets: PHFetchResult<PHAsset>, empty: String) {
        running = true
        sent = 0; skipped = 0; kept = 0; failed = 0
        task = Task { await run(assets, empty: empty); running = false; GateModel.shared.reload() }
    }

    private func run(_ assets: PHFetchResult<PHAsset>, empty: String) async {
        total = assets.count
        status = total == 0 ? empty : "Sending \(total) photos and videos…"
        var files = 0
        assets.enumerateObjects { a, _, _ in files += PHAssetResource.assetResources(for: a).filter(UploadRequest.wanted).count }
        let batch = (id: UploadRequest.newBatch(), count: files)   // the server catalogues them when the last is in
        var i = 0
        while i < assets.count, !Task.isCancelled {
            let asset = assets.object(at: i)
            if Gate.held(asset.localIdentifier, GateStore.read()) { kept += 1; i += 1; continue }   // kept back, or to look over
            var done = true, back = false
            for r in PHAssetResource.assetResources(for: asset) where UploadRequest.wanted(r) {
                switch await upload(asset, r, batch) {
                case .sent: sent += 1
                case .exists: skipped += 1
                case .kept(let sha):
                    back = true
                    GateStore.update { Gate.keptBack(asset.localIdentifier, sha: sha, &$0) }
                case .failed(let why): failed += 1; done = false; status = why
                }
            }
            if back { kept += 1 } else if done { GateStore.update { Gate.sent(asset.localIdentifier, &$0) } }
            i += 1
            if failed == 0 { status = "Sent \(sent), already there \(skipped)" + (kept > 0 ? ", kept back \(kept)" : "") + ", of \(total)." }
        }
        if Task.isCancelled { status = "Stopped. Sent \(sent), already there \(skipped)." }
    }

    private enum Outcome { case sent, exists, kept(String), failed(String) }

    private func upload(_ asset: PHAsset, _ r: PHAssetResource, _ batch: (id: String, count: Int)) async -> Outcome {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: file) }
        let name = UploadRequest.name(r) ?? "?"
        do {
            let o = PHAssetResourceRequestOptions()
            o.isNetworkAccessAllowed = true
            try await PHAssetResourceManager.default().writeData(for: r, toFile: file, options: o)
            let sha = try FileHash.sha256(file)
            if Gate.refused(sha, GateStore.read()) { return .kept(sha) }     // opted out, by this member: not sent
            let req = UploadRequest.make(asset: asset, resource: r, batch: batch, sha256: sha)
            let (data, resp) = try await URLSession.shared.upload(for: req, fromFile: file)
            switch UploadRequest.answer(resp as? HTTPURLResponse) {
            case .stored: return .sent
            case .exists: return .exists
            case .refused(let why): return why == "opted_out" ? .kept(sha) : .exists   // purged: the estate will never take it
            case .failed(let code): return .failed("\(name): server said \(code) \(String(decoding: data.prefix(120), as: UTF8.self))")
            }
        } catch {
            return .failed("\(name): \(error.localizedDescription)")
        }
    }
}
