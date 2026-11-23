import CryptoKit
import Foundation

/// The phone gate (multiplayer phase 6; docs/notes/MULTIPLAYER-PLAN.md): a member whose upload policy is
/// `review` or `review_flagged` looks at new photos on this iPhone before any is sent. The mini does not know
/// a photo exists until it is shared, so everything here lives on the phone, in one small file in the app
/// group that the app and the upload extension both read and write (`GateStore`).
///
///   new photo ──(extension)──► candidate ──(app)──► batch ──(review: apply)──► approved ──► sent
///                    │ everything: as before, a job at once        └─ marked ──► opted out (a tombstone)
///
/// - A **candidate** is a photo the extension saw arrive and made no job for. The app turns candidates into
///   a **batch**, an item of its Inbox. Under `review_flagged` only what may be private is in the batch
///   (PhotoKit says a screenshot, Vision sees text); the rest is approved without asking.
/// - Reviewing is a change set's review: everything is shared unless marked. A mark is an opt-out.
/// - An **opt-out** is a tombstone: the SHA-256 of each original file, as the store keys it, kept here with
///   the photo's identifier and sent (only the hashes) to `POST /api/tombstones`. On this phone the identifier
///   alone keeps it from ever becoming a job; a new phone knows only the hashes (`GET /api/tombstones`), which
///   the in-app uploader, having the file, checks before it sends and the server checks again.
/// - **Approved** photos are sent by the app at once (ForegroundUploader.send); what it could not send, the
///   extension makes jobs for on its next run.
/// - Until the app has read `/api/me`, the policy is not known and the extension makes no job (a candidate is
///   safe; a photo sent is for good). Under `everything` the candidates are approved.
enum UploadPolicy: String, Codable {
    case everything, review, review_flagged

    var gated: Bool { self != .everything }
}

/// One of the app's Inbox items made on the phone: photos to look over before they are shared.
struct UploadBatch: Codable, Equatable, Identifiable {
    var id: String
    var made: Date
    var title: String
    var assets: [String]                    // PhotoKit local identifiers, in the order shown
    var flagged: [String: String] = [:]     // identifier -> why it may be private
    var marked: Set<String> = []            // opted out: kept on this iPhone

    var sharing: Int { assets.count - marked.count }
}

struct GateState: Codable, Equatable {
    var member: Int?                        // members.id, from /api/me
    var name: String?
    var policy: UploadPolicy?               // nil: not known yet (no job is made)
    var joined: Date?
    var candidates: [String] = []
    var batches: [UploadBatch] = []
    var approved: [String] = []             // to be sent
    var shared: Set<String> = []            // sent, or made a job: never offered again (gated policies only)
    var optedOut: [String: [String]] = [:]  // identifier -> the SHA-256 of each of its original files
    var serverTombstones: Set<String> = []  // this member's, as GET /api/tombstones last said
    var unsentAdd: [String] = []            // hashes the server has not been told of yet
    var unsentLift: [String] = []
    var skipped: [String] = []              // chunks of older photos passed over ("2014")
    var told = false                        // the line about withdrawing but not erasing has been shown

    init() {}

    // Every field may be missing from a file an older build wrote.
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        member = try c.decodeIfPresent(Int.self, forKey: .member)
        name = try c.decodeIfPresent(String.self, forKey: .name)
        policy = try? c.decodeIfPresent(UploadPolicy.self, forKey: .policy)
        joined = try c.decodeIfPresent(Date.self, forKey: .joined)
        candidates = try c.decodeIfPresent([String].self, forKey: .candidates) ?? []
        batches = try c.decodeIfPresent([UploadBatch].self, forKey: .batches) ?? []
        approved = try c.decodeIfPresent([String].self, forKey: .approved) ?? []
        shared = try c.decodeIfPresent(Set<String>.self, forKey: .shared) ?? []
        optedOut = try c.decodeIfPresent([String: [String]].self, forKey: .optedOut) ?? [:]
        serverTombstones = try c.decodeIfPresent(Set<String>.self, forKey: .serverTombstones) ?? []
        unsentAdd = try c.decodeIfPresent([String].self, forKey: .unsentAdd) ?? []
        unsentLift = try c.decodeIfPresent([String].self, forKey: .unsentLift) ?? []
        skipped = try c.decodeIfPresent([String].self, forKey: .skipped) ?? []
        told = try c.decodeIfPresent(Bool.self, forKey: .told) ?? false
    }
}

/// The gate's decisions, as plain functions of the state (tested by ios/Tests, no device needed).
enum Gate {
    /// Every photo the gate already has a place for: never offered again.
    static func known(_ s: GateState) -> Set<String> {
        var k = Set(s.candidates).union(s.approved).union(s.shared).union(s.optedOut.keys)
        for b in s.batches { k.formUnion(b.assets) }
        return k
    }

    /// The extension's: photos PhotoKit says were added since its last run. Returns those to make jobs for now:
    /// under `everything` all of them, as before; otherwise none of them (they become candidates). Either way the
    /// approved photos the app has not sent are taken too. An opted-out photo is never a job.
    static func route(_ inserted: [String], _ s: inout GateState) -> [String] {
        var out = s.approved
        s.approved = []
        if s.policy == .everything {
            out += inserted
        } else {
            let k = known(s)
            for id in inserted where !k.contains(id) && !s.candidates.contains(id) { s.candidates.append(id) }
        }
        out = unique(out.filter { s.optedOut[$0] == nil })
        if s.policy?.gated ?? true { s.shared.formUnion(out) }
        return out
    }

    /// The app's: `ids` (candidates, or a chunk of older photos) become a batch to look over. Under
    /// `review_flagged` only the flagged ones are asked about and the rest are approved at once, once the member
    /// has been told what sharing means (`told`: until then, everything is asked about, so nothing is sent
    /// before the line is shown); under `everything` all are approved. Returns the batch, if one was made.
    @discardableResult
    static func gather(_ ids: [String], flagged: [String: String], title: String, _ s: inout GateState,
                       now: Date = Date(), id: String = UUID().uuidString) -> UploadBatch? {
        let k = known(s).subtracting(s.candidates)
        let ids = unique(ids.filter { !k.contains($0) })
        s.candidates.removeAll { ids.contains($0) }
        guard !ids.isEmpty else { return nil }
        var ask = ids
        switch s.policy ?? .review {
        case .everything: ask = []
        case .review_flagged: ask = s.told ? ids.filter { flagged[$0] != nil } : ids   // the first time, all: the line is shown first
        case .review: break
        }
        s.approved += ids.filter { !ask.contains($0) }
        guard !ask.isEmpty else { return nil }
        let b = UploadBatch(id: id, made: now, title: title, assets: ask, flagged: flagged.filter { ask.contains($0.key) })
        s.batches.append(b)
        return b
    }

    static func toggle(_ asset: String, in batch: String, _ s: inout GateState) {
        guard let i = s.batches.firstIndex(where: { $0.id == batch }) else { return }
        if s.batches[i].marked.contains(asset) { s.batches[i].marked.remove(asset) } else { s.batches[i].marked.insert(asset) }
    }

    /// Apply a batch: the unmarked are approved, the marked opted out with the hashes of their files (`hashes`,
    /// worked out by the caller, who has PhotoKit), which wait in `unsentAdd` for the server. The batch is done.
    /// Returns (shared, kept back).
    @discardableResult
    static func apply(_ batch: String, hashes: [String: [String]], _ s: inout GateState) -> (share: [String], keep: [String]) {
        guard let i = s.batches.firstIndex(where: { $0.id == batch }) else { return ([], []) }
        let b = s.batches.remove(at: i)
        let keep = b.assets.filter { b.marked.contains($0) }, share = b.assets.filter { !b.marked.contains($0) }
        for id in keep {
            let hs = hashes[id] ?? []
            s.optedOut[id] = hs
            s.unsentLift.removeAll { hs.contains($0) }
            s.unsentAdd += hs.filter { !s.unsentAdd.contains($0) && !s.serverTombstones.contains($0) }
        }
        s.approved += share.filter { !s.approved.contains($0) }
        return (share, keep)
    }

    /// "Share after all": an opt-out taken back, by the member who made it, on their phone. Its hashes are lifted
    /// (or, if the server never heard of them, forgotten) and the photo is approved.
    static func shareAfterAll(_ asset: String, _ s: inout GateState) {
        guard let hs = s.optedOut.removeValue(forKey: asset) else { return }
        let unsent = Set(s.unsentAdd)
        s.unsentAdd.removeAll { hs.contains($0) }
        s.unsentLift += hs.filter { !unsent.contains($0) && !s.unsentLift.contains($0) }
        s.serverTombstones.subtract(hs)
        s.shared.remove(asset)
        if !s.approved.contains(asset) { s.approved.append(asset) }
    }

    /// Is a file with this hash opted out by this member: here, on another of their phones, or not yet said to the server.
    static func refused(_ sha: String, _ s: GateState) -> Bool {
        let sha = sha.lowercased()
        if s.unsentLift.contains(sha) { return false }
        return s.serverTombstones.contains(sha) || s.unsentAdd.contains(sha) || s.optedOut.values.contains { $0.contains(sha) }
    }

    /// The uploader found that `asset` is opted out (its hash, here or by the server): it is kept back here too.
    static func keptBack(_ asset: String, sha: String, _ s: inout GateState) {
        var hs = s.optedOut[asset] ?? []
        if !hs.contains(sha) { hs.append(sha) }
        s.optedOut[asset] = hs
        s.approved.removeAll { $0 == asset }
    }

    static func sent(_ asset: String, _ s: inout GateState) {
        s.approved.removeAll { $0 == asset }
        if s.policy?.gated ?? true { s.shared.insert(asset) }
    }

    /// What /api/me said. A different member on this phone starts again; under `everything` the candidates go.
    static func me(member: Int?, name: String, policy: UploadPolicy, joined: Date?, _ s: inout GateState) {
        if s.member != nil, s.member != member {
            s = GateState()
        }
        s.member = member
        s.name = name
        s.policy = policy
        s.joined = joined
        if policy == .everything, !s.candidates.isEmpty {
            s.approved += s.candidates.filter { !s.approved.contains($0) }
            s.candidates = []
        }
    }

    /// The server has these hashes now (POST /api/tombstones answered).
    static func told(added: [String], lifted: [String], _ s: inout GateState) {
        s.unsentAdd.removeAll { added.contains($0) }
        s.unsentLift.removeAll { lifted.contains($0) }
        s.serverTombstones.formUnion(added)
        s.serverTombstones.subtract(lifted)
    }

    /// Older photos are offered in coarse chunks: "since you joined", then a year at a time, newest first.
    static func chunk(of date: Date?, joined: Date?, calendar: Calendar = .current) -> String {
        guard let date else { return "undated" }
        if let joined, date >= joined { return "since" }
        return String(calendar.component(.year, from: date))
    }

    static func chunkTitle(_ key: String, joined: Date?) -> String {
        switch key {
        case "since": return "Since you joined" + (joined.map { " (\($0.formatted(date: .abbreviated, time: .omitted)))" } ?? "")
        case "undated": return "With no date"
        default: return key
        }
    }

    /// Chunk keys in the order they are offered: since joined, then years newest first, then undated.
    static func order(_ keys: some Sequence<String>) -> [String] {
        Set(keys).sorted { a, b in
            let rank = { (k: String) in k == "since" ? 0 : k == "undated" ? 2 : 1 }
            return rank(a) != rank(b) ? rank(a) < rank(b) : a > b
        }
    }

    static func unique(_ xs: [String]) -> [String] {
        var seen = Set<String>()
        return xs.filter { seen.insert($0).inserted }
    }
}

/// The gate's state, in one JSON file in the app group (the app and the extension are two processes; an
/// exclusive file lock makes each read-change-write whole). Without the group (the Simulator-only project,
/// or a test) it is in the app's own Application Support, or `directory`.
enum GateStore {
    nonisolated(unsafe) static var directory: URL? = nil

    static var folder: URL {
        if let d = directory { return d }
        if let g = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: ExtensionStatus.group) { return g }
        let a = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: a, withIntermediateDirectories: true)
        return a
    }

    static var file: URL { folder.appendingPathComponent("gate.json") }

    static func read() -> GateState {
        locked { load() }
    }

    @discardableResult
    static func update<T>(_ change: (inout GateState) -> T) -> T {
        locked {
            var s = load()
            let before = s
            let out = change(&s)
            if s != before, let data = try? JSONEncoder().encode(s) {
                try? data.write(to: file, options: .atomic)
            }
            return out
        }
    }

    /// Start again: a "new phone" (only the app's own Settings, or a test, does this).
    static func erase() { locked { try? FileManager.default.removeItem(at: file) } }

    private static func load() -> GateState {
        guard let data = try? Data(contentsOf: file) else { return GateState() }
        return (try? JSONDecoder().decode(GateState.self, from: data)) ?? GateState()
    }

    private static func locked<T>(_ body: () -> T) -> T {
        let fd = open(folder.appendingPathComponent("gate.lock").path, O_CREAT | O_RDWR, 0o644)
        if fd >= 0 { flock(fd, LOCK_EX) }
        defer { if fd >= 0 { flock(fd, LOCK_UN); close(fd) } }
        return body()
    }
}

/// SHA-256 of a file, as the store keys it (estate.py hashes the bytes the same way), read a megabyte at a time.
enum FileHash {
    static func sha256(_ url: URL) throws -> String {
        let h = try FileHandle(forReadingFrom: url)
        defer { try? h.close() }
        var hasher = SHA256()
        while let chunk = try h.read(upToCount: 1 << 20), !chunk.isEmpty { hasher.update(data: chunk) }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
