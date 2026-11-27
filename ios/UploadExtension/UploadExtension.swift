import ExtensionFoundation
import Photos

/// Runs when the system has upload work for Photo Estate: it queues one upload job per new photo, and
/// the system sends them (also with the app closed, over the Tailscale connection). iOS 27 API.
///
/// Only photos taken after the first run are queued: the starting point is a PhotoKit change token,
/// saved in this extension's own defaults. Older photos are sent from the app's "Upload recent" button.
///
/// The phone gate (PhoneGate.swift): unless the member's policy is `everything`, a new photo is not queued but
/// recorded as a candidate for the app's Inbox, and what the member approved there and the app has not sent
/// yet is queued instead. Before the app has read the policy, nothing new is queued.
@main
class UploadExtension: PHBackgroundResourceUploadJobExtension {
    required init() {}

    private let defaults = UserDefaults.standard
    private let tokenKey = "changeToken"
    private let pendingKey = "pendingIdentifiers"   // an older build's photos with no job yet: GateState.pending now

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
        let older = defaults.stringArray(forKey: pendingKey) ?? []   // an older build kept them here
        var pending: [String] = []
        var inserted: [String] = []
        do {
            var last = token
            for change in try library.fetchPersistentChanges(since: token) {
                last = change.changeToken
                if let d = try? change.changeDetails(for: .asset) {
                    inserted += d.insertedLocalIdentifiers.filter { !inserted.contains($0) }
                }
            }
            // The gate decides what becomes a job: everything new (as before), or only what the member approved. What
            // it returns stays in GateState.pending, in this one locked write, until its jobs exist (Gate.queued):
            // ended anywhere after it, the next run makes them.
            var held = 0
            pending = GateStore.update { s -> [String] in
                let before = s.candidates.count
                s.pending += older.filter { !s.pending.contains($0) }
                let out = Gate.route(inserted, &s)
                held = s.candidates.count - before
                return out
            }
            if !older.isEmpty { defaults.removeObject(forKey: pendingKey) }
            if held > 0 { note = "\(held) new photo\(held == 1 ? "" : "s") waiting in the app's Inbox" }
            save(token: last)
        } catch {
            note = "could not read changes: \((error as NSError).code)"
            return .failure
        }
        let gate = GateStore.read()
        pending.removeAll { Gate.held($0, gate) }        // kept back, or to look over, on this iPhone: never a job
        pending = queue(pending, made: &made, note: &note)
        let left = pending
        GateStore.update { Gate.queued(left: left, &$0) }
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
        var found: [(PHAsset, [PHAssetResource])] = []
        assets.enumerateObjects { a, _, _ in
            found.append((a, PHAssetResource.assetResources(for: a).filter(UploadRequest.wanted)))
        }
        // Every file queued in this run is one batch: the server starts cataloguing when the last arrives.
        let batch = (id: UploadRequest.newBatch(), count: found.reduce(0) { $0 + $1.1.count })
        var left: [String] = []        // photos that vanished since they were seen are simply dropped
        for (i, (asset, resources)) in found.enumerated() {
            do {
                try PHPhotoLibrary.shared().performChangesAndWait {
                    for r in resources {
                        total += 1
                        PHAssetResourceUploadJobChangeRequest.creationRequestForJob(
                            destination: UploadRequest.make(asset: asset, resource: r, batch: batch), resource: r)
                    }
                }
            } catch {
                note = "job create failed: \((error as NSError).code)"
                left = found[i...].map(\.0.localIdentifier)  // limitExceeded or similar: try again next time; this
                                                            // batch never completes, so the server falls back to its quiet period
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
