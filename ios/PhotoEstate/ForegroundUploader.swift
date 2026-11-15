import Photos
import SwiftUI

/// Sends photos from inside the app. It is the way to send photos that were taken before the system
/// uploader was switched on, and the fallback if the system uploader does not run.
///
/// Each resource is exported to a temporary file (fetching it from iCloud if it was optimised away),
/// uploaded in one POST, and forgotten. The server ignores files it already has, so running this twice,
/// or alongside the system uploader, only costs the bandwidth.
@MainActor
final class ForegroundUploader: ObservableObject {
    @Published var running = false
    @Published var status = "Nothing sent yet."
    @Published var sent = 0
    @Published var skipped = 0
    @Published var failed = 0
    @Published var total = 0

    private var task: Task<Void, Never>?

    func start(days: Int) {
        guard !running else { return }
        running = true
        sent = 0; skipped = 0; failed = 0
        task = Task { await run(days: days); running = false }
    }

    func stop() { task?.cancel() }

    private func run(days: Int) async {
        let opts = PHFetchOptions()
        opts.predicate = NSPredicate(format: "creationDate > %@", Date().addingTimeInterval(-Double(days) * 86400) as NSDate)
        opts.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
        let assets = PHAsset.fetchAssets(with: opts)
        total = assets.count
        status = total == 0 ? "No photos in the last \(days) days." : "Sending \(total) photos and videos…"
        var i = 0
        while i < assets.count, !Task.isCancelled {
            let asset = assets.object(at: i)
            for r in PHAssetResource.assetResources(for: asset) where UploadRequest.wanted(r) {
                switch await upload(asset, r) {
                case .sent: sent += 1
                case .exists: skipped += 1
                case .failed(let why): failed += 1; status = why
                }
            }
            i += 1
            if failed == 0 { status = "Sent \(sent), already there \(skipped), of \(total)." }
        }
        if Task.isCancelled { status = "Stopped. Sent \(sent), already there \(skipped)." }
    }

    private enum Outcome { case sent, exists, failed(String) }

    private func upload(_ asset: PHAsset, _ r: PHAssetResource) async -> Outcome {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: file) }
        do {
            let o = PHAssetResourceRequestOptions()
            o.isNetworkAccessAllowed = true
            try await PHAssetResourceManager.default().writeData(for: r, toFile: file, options: o)
            let (data, resp) = try await URLSession.shared.upload(for: UploadRequest.make(asset: asset, resource: r), fromFile: file)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            switch code {
            case 201: return .sent
            case 200: return .exists
            default: return .failed("\(r.filename ?? "?"): server said \(code) \(String(decoding: data.prefix(120), as: UTF8.self))")
            }
        } catch {
            return .failed("\(r.filename ?? "?"): \(error.localizedDescription)")
        }
    }
}
