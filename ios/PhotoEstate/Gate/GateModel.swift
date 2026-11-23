import Photos
import SwiftUI
import Vision

/// The app's half of the phone gate (PhoneGate.swift has the rules and the state) and the Inbox's data: who
/// this phone's member is (`GET /api/me`), the batches made here, the mini's own items (`GET /api/inbox`), the
/// older photos offered a chunk at a time, and the opt-outs (`/api/tombstones`).
@MainActor
final class GateModel: ObservableObject {
    static let shared = GateModel()

    struct Me: Equatable {
        var id: Int?
        var name: String
        var role: String
        var policy: UploadPolicy
        var joined: Date?

        var policyText: String {
            switch policy {
            case .everything: "Shares everything from this iPhone"
            case .review: "Looks over new photos before they are shared"
            case .review_flagged: "Looks over what may be private; the rest is shared"
            }
        }
    }

    /// An item of the member's inbox on the mini (member_inbox.py).
    struct Item: Identifiable, Equatable {
        var id: Int
        var kind: String
        var ref: String
        var title: String
        var opens: String?
        var when: Date?

        var kindText: String {
            switch kind {
            case "member_request": "Asking to be let in"
            case "review": "To look over"
            case "lens_proposed": "Proposed estate default"
            case "lens_changed": "A lens you follow"
            case "held": "Kept private to you"
            case "question": "A question for you"
            case "series_update": "New for a series"
            default: kind
            }
        }
        /// A notice waits on nobody: opening it puts it away (as on the web's inbox).
        var notice: Bool { kind == "lens_changed" }
        /// A question item opens natively (QuestionsView); its group, from its ref (`ask:<group>`).
        var group: String? { kind == "question" ? StoryText.group(ref: ref) : nil }
    }

    /// Older photos not yet offered: since the member joined, then a year at a time.
    struct Chunk: Identifiable, Equatable {
        var id: String          // "since", "2019", "undated"
        var title: String
        var ids: [String]
    }

    @Published private(set) var state = GateStore.read()
    @Published private(set) var me: Me?
    @Published private(set) var items: [Item] = []
    @Published private(set) var chunks: [Chunk] = []
    @Published private(set) var working: String?
    @Published var problem: String?

    var gated: Bool { state.policy?.gated ?? false }
    var badge: Int { (gated ? state.batches.count : 0) + items.count }

    private var refreshing = false

    func reload() { state = GateStore.read() }

    /// When the app comes to the front, and on pull-to-refresh: who I am, my opt-outs, my inbox on the mini;
    /// new photos the extension saw become a batch; what is approved is sent.
    func refresh() async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        await readMe()
        reload()
        if gated {
            await tellServer()
            if let j = try? await API.get("api/tombstones"), let hs = j["tombstones"] as? [String] {
                GateStore.update { $0.serverTombstones = Set(hs) }
            }
        }
        if let j = try? await API.get("api/inbox") {
            items = (j["items"] as? [[String: Any]] ?? []).compactMap { r in
                guard let id = (r["id"] as? NSNumber)?.intValue else { return nil }
                return Item(id: id, kind: r["kind"] as? String ?? "", ref: r["ref"] as? String ?? "", title: r["title"] as? String ?? "",
                            opens: r["opens"] as? String, when: (r["updated_at"] as? String).flatMap(Self.iso))
            }
        }
        reload()
        if gated, !state.candidates.isEmpty { await gather(state.candidates, title: nil) }
        if !state.approved.isEmpty { ForegroundUploader.shared.send(state.approved) }
        loadChunks()
    }

    private func readMe() async {
        do {
            let j = try await API.get("api/me")
            let policy = UploadPolicy(rawValue: j["upload_policy"] as? String ?? "") ?? .review
            let m = Me(id: (j["id"] as? NSNumber)?.intValue, name: j["name"] as? String ?? "?", role: j["role"] as? String ?? "member",
                       policy: policy, joined: (j["joined_at"] as? String).flatMap(Self.iso))
            me = m
            GateStore.update { Gate.me(member: m.id, name: m.name, policy: m.policy, joined: m.joined, &$0) }
        } catch let e as APIError where e.code == 404 {
            // A server from before members: everything is shared, as it always was.
            GateStore.update { Gate.me(member: nil, name: "", policy: .everything, joined: nil, &$0) }
        } catch {}
    }

    // MARK: batches

    /// Make these photos a batch (candidates the extension saw, or a chunk of older photos). Under review flagged
    /// each is looked at first (`Flags`), and only what may be private is asked about.
    func gather(_ ids: [String], title: String?) async {
        let policy = state.policy ?? .review
        var flagged: [String: String] = [:]
        let assets = PHAsset.fetchAssets(withLocalIdentifiers: ids, options: nil)     // (the Hidden album is left out)
        var found: [PHAsset] = []
        assets.enumerateObjects { a, _, _ in found.append(a) }
        if policy == .review_flagged {
            for (i, a) in found.enumerated() {
                working = "Looking at \(i + 1) of \(found.count)…"
                if let why = await Flags.why(a) { flagged[a.localIdentifier] = why }
            }
        } else {
            for a in found { if let why = Flags.quick(a) { flagged[a.localIdentifier] = why } }   // hints only
        }
        working = nil
        let present = found.map(\.localIdentifier)
        let name = title ?? (present.count == 1 ? "1 new photo" : "\(present.count) new photos")
        GateStore.update { s in
            s.candidates.removeAll { !present.contains($0) && ids.contains($0) }   // gone from Photos, or hidden
            Gate.gather(present, flagged: flagged, title: name, &s)
        }
        reload()
        if !state.approved.isEmpty { ForegroundUploader.shared.send(state.approved) }
    }

    func toggle(_ asset: String, in batch: String) {
        GateStore.update { Gate.toggle(asset, in: batch, &$0) }
        reload()
    }

    /// Share the batch but what is marked. The marked are hashed first (each original file, as the store will
    /// key it): the hashes are the tombstones the server is told of.
    func apply(_ batch: UploadBatch) async {
        var hashes: [String: [String]] = [:]
        let marked = Array(batch.marked)
        for (i, id) in marked.enumerated() {
            working = "Keeping \(i + 1) of \(marked.count) back…"
            hashes[id] = await Self.hashes(of: id)
        }
        working = nil
        GateStore.update { s in
            s.told = true
            Gate.apply(batch.id, hashes: hashes, &s)
        }
        reload()
        await tellServer()
        ForegroundUploader.shared.send(state.approved)
    }

    /// Take an opt-out back: the tombstones are lifted and the photo is sent.
    func shareAfterAll(_ asset: String) async {
        GateStore.update { Gate.shareAfterAll(asset, &$0) }
        reload()
        await tellServer()
        ForegroundUploader.shared.send(state.approved)
    }

    func markTold() { GateStore.update { $0.told = true }; reload() }

    /// POST /api/tombstones with what the server has not heard yet; kept for next time if it cannot be reached.
    private func tellServer() async {
        let s = GateStore.read()
        guard !s.unsentAdd.isEmpty || !s.unsentLift.isEmpty else { return }
        do {
            _ = try await API.post("api/tombstones", ["add": s.unsentAdd, "lift": s.unsentLift])
            GateStore.update { Gate.told(added: s.unsentAdd, lifted: s.unsentLift, &$0) }
            problem = nil
        } catch {
            problem = "Your server has not been told what you kept back yet: it will be, next time it can be reached."
        }
        reload()
    }

    /// The SHA-256 of each original file of a photo (a Live Photo has two).
    static func hashes(of id: String) async -> [String] {
        guard let asset = PHAsset.fetchAssets(withLocalIdentifiers: [id], options: nil).firstObject else { return [] }
        var out: [String] = []
        for r in PHAssetResource.assetResources(for: asset) where UploadRequest.wanted(r) {
            let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            defer { try? FileManager.default.removeItem(at: file) }
            let o = PHAssetResourceRequestOptions()
            o.isNetworkAccessAllowed = true
            if (try? await PHAssetResourceManager.default().writeData(for: r, toFile: file, options: o)) != nil,
               let sha = try? FileHash.sha256(file) { out.append(sha) }
        }
        return out
    }

    // MARK: older photos

    /// The photos on this iPhone the gate has no place for yet, by chunk: since joining, then each year. Not for
    /// `everything`, whose backlog is "Upload recent photos", as before.
    func loadChunks() {
        guard gated, PHPhotoLibrary.authorizationStatus(for: .readWrite) == .authorized else { chunks = []; return }
        let known = Gate.known(state), skipped = Set(state.skipped), joined = state.joined
        Task.detached(priority: .utility) {
            let all = PHAsset.fetchAssets(with: PHFetchOptions())
            var by: [String: [String]] = [:]
            all.enumerateObjects { a, _, _ in
                guard !known.contains(a.localIdentifier) else { return }
                by[Gate.chunk(of: a.creationDate, joined: joined), default: []].append(a.localIdentifier)
            }
            let out = Gate.order(by.keys).filter { !skipped.contains($0) }
                .map { Chunk(id: $0, title: Gate.chunkTitle($0, joined: joined), ids: by[$0] ?? []) }
            await MainActor.run { self.chunks = out }
        }
    }

    func share(_ c: Chunk) async {
        let n = c.ids.count
        await gather(c.ids, title: "\(c.title): \(n) photo\(n == 1 ? "" : "s")")
        loadChunks()
    }

    func skip(_ c: Chunk) {
        GateStore.update { if !$0.skipped.contains(c.id) { $0.skipped.append(c.id) } }
        reload()
        chunks.removeAll { $0.id == c.id }
    }

    // MARK: the mini's items

    /// A question item whose every question is answered or put away: done on the server too.
    func done(_ item: Item) {
        items.removeAll { $0.id == item.id }
    }

    func opened(_ item: Item) {
        guard item.notice else { return }
        items.removeAll { $0.id == item.id }
        Task { _ = try? await API.post("api/inbox/\(item.id)/dismiss") }
    }

    nonisolated static func iso(_ s: String) -> Date? {
        let f = ISO8601DateFormatter()
        return f.date(from: s) ?? f.date(from: s.replacingOccurrences(of: " ", with: "T") + (s.hasSuffix("Z") ? "" : "Z"))
    }
}

/// What may be private, decided on the phone (review flagged): PhotoKit's own word for a screenshot or a screen
/// recording, then Vision's fast text recognition on a small copy: a picture that is mostly text (a document,
/// a receipt, a screenshot sent from elsewhere) is flagged.
enum Flags {
    static func quick(_ a: PHAsset) -> String? {
        if a.mediaSubtypes.contains(.photoScreenshot) { return "a screenshot" }
        if a.mediaSubtypes.contains(.videoScreenRecording) { return "a screen recording" }
        return nil
    }

    static func why(_ a: PHAsset) async -> String? {
        if let q = quick(a) { return q }
        guard a.mediaType == .image, let image = await small(a), let cg = image.cgImage else { return nil }
        let req = VNRecognizeTextRequest()
        req.recognitionLevel = .fast
        req.usesLanguageCorrection = false
        try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
        let lines = (req.results ?? []).filter { ($0.topCandidates(1).first?.confidence ?? 0) >= 0.3 }
        let area = lines.reduce(0.0) { $0 + Double($1.boundingBox.width * $1.boundingBox.height) }
        return lines.count >= 6 || area >= 0.12 ? "text: it may be a document, a receipt or a screenshot" : nil
    }

    private static func small(_ a: PHAsset) async -> UIImage? {
        let o = PHImageRequestOptions()
        o.deliveryMode = .highQualityFormat
        o.isNetworkAccessAllowed = true
        o.isSynchronous = false
        return await withCheckedContinuation { k in
            PHImageManager.default().requestImage(for: a, targetSize: CGSize(width: 900, height: 900), contentMode: .aspectFit, options: o) { img, _ in
                k.resume(returning: img)
            }
        }
    }
}
