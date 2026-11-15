import ExtensionFoundation
import Photos

/// Runs when the system has upload work for Photo Estate: it queues one upload job per new photo, and
/// the system sends them (also with the app closed, over the Tailscale connection). iOS 27 API.
///
/// Only photos taken after the first run are queued: the starting point is a PhotoKit change token,
/// saved in this extension's own defaults. Older photos are sent from the app's "Upload recent" button.
@main
class UploadExtension: PHBackgroundResourceUploadJobExtension {
    required init() {}

    private let defaults = UserDefaults.standard
    private let tokenKey = "changeToken"
    private let pendingKey = "pendingIdentifiers"   // inserted photos not yet turned into jobs (job limit reached)

    func processJobs() async -> PHBackgroundResourceUploadProcessingResult {
        var made = 0
        var note = ""
        let result = run(&made, &note)
        let label = result == .completed ? "completed" : result == .processing ? "processing" : "failure"
        ExtensionStatus.record(label, queued: made, note: note)
        return result
    }

    private func run(_ made: inout Int, _ note: inout String) -> PHBackgroundResourceUploadProcessingResult {
        settleFinishedJobs()
        let library = PHPhotoLibrary.shared()

        guard let token = savedToken() else {      // first run: start from now
            save(token: library.currentChangeToken)
            note = "first run: watching for photos from now on"
            return .completed
        }
        var pending = defaults.stringArray(forKey: pendingKey) ?? []
        do {
            var last = token
            for change in try library.fetchPersistentChanges(since: token) {
                last = change.changeToken
                if let d = try? change.changeDetails(for: .asset) {
                    pending += d.insertedLocalIdentifiers.filter { !pending.contains($0) }
                }
            }
            save(token: last)
        } catch {
            note = "could not read changes: \((error as NSError).code)"
            return .failure
        }
        pending = queue(pending, made: &made, note: &note)
        defaults.set(pending, forKey: pendingKey)
        return pending.isEmpty ? .completed : .processing
    }

    func willTerminate() async {}

    // MARK: jobs

    /// Acknowledge finished jobs (this frees room under the job limit) and retry failed ones once.
    private func settleFinishedJobs() {
        let library = PHPhotoLibrary.shared()
        for action in [PHAssetResourceUploadJob.Action.acknowledge, .retry] {
            let jobs = PHAssetResourceUploadJob.fetchJobs(action: action, options: nil)
            guard jobs.count > 0 else { continue }
            try? library.performChangesAndWait {
                jobs.enumerateObjects { job, _, _ in
                    let r = PHAssetResourceUploadJobChangeRequest(for: job)
                    if action == .acknowledge { r?.acknowledge() } else { r?.retry(destination: nil) }
                }
            }
        }
    }

    /// One job per original resource. Returns the identifiers that could not be queued yet.
    private func queue(_ ids: [String], made: inout Int, note: inout String) -> [String] {
        guard !ids.isEmpty else { return [] }
        var total = 0                  // a closure below runs the change block, so count in a local, not the inout
        defer { made = total }
        let assets = PHAsset.fetchAssets(withLocalIdentifiers: ids, options: nil)
        var found: [PHAsset] = []
        assets.enumerateObjects { a, _, _ in found.append(a) }
        var left: [String] = []        // photos that vanished since they were seen are simply dropped
        for (i, asset) in found.enumerated() {
            do {
                try PHPhotoLibrary.shared().performChangesAndWait {
                    for r in PHAssetResource.assetResources(for: asset) where UploadRequest.wanted(r) {
                        total += 1
                        PHAssetResourceUploadJobChangeRequest.creationRequestForJob(destination: UploadRequest.make(asset: asset, resource: r), resource: r)
                    }
                }
            } catch {
                note = "job create failed: \((error as NSError).code)"
                left = found[i...].map(\.localIdentifier)    // limitExceeded or similar: try again next time
                break
            }
        }
        return left
    }

    // MARK: change token

    private func savedToken() -> PHPersistentChangeToken? {
        guard let data = defaults.data(forKey: tokenKey) else { return nil }
        return try? NSKeyedUnarchiver.unarchivedObject(ofClass: PHPersistentChangeToken.self, from: data)
    }

    private func save(token: PHPersistentChangeToken) {
        if let data = try? NSKeyedArchiver.archivedData(withRootObject: token, requiringSecureCoding: true) {
            defaults.set(data, forKey: tokenKey)
        }
    }
}
