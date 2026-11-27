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
///   alone keeps it, and a photo marked in a batch, from ever being sent (`held`); a new phone knows only the
///   hashes (`GET /api/tombstones`), which the in-app uploader, having the file, checks before it sends and the
///   server checks again. "Share after all" lifts the hashes; the photo is approved once the server has answered.
/// - **Approved** photos are sent by the app at once (ForegroundUploader.send); what it could not send, the
///   extension makes jobs for on its next run (kept in `pending` until it has).
/// - Until the app has read `/api/me`, the policy is not known and the extension makes no job (a candidate is
///   safe; a photo sent is for good). Under `everything` the candidates are approved.
/// - specs/PhoneGate.tla is a model of all of this, with every writer; ios/Tests/main.swift replays its traces.
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
    var pending: [String] = []              // taken by the extension, no job made yet (Gate.route, Gate.queued)
    var lifting: [String: [String]] = [:]   // shared after all: identifier -> hashes whose lift the server has not answered
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
        pending = try c.decodeIfPresent([String].self, forKey: .pending) ?? []
        lifting = try c.decodeIfPresent([String: [String]].self, forKey: .lifting) ?? [:]
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
        var k = Set(s.candidates).union(s.approved).union(s.pending).union(s.shared).union(s.optedOut.keys).union(s.lifting.keys)
        for b in s.batches { k.formUnion(b.assets) }
        return k
    }

    /// A photo this phone holds back, by its identifier: kept back, or in a batch waiting for the member's word.
    /// Neither uploader sends it, whatever the policy (a mark is a word to keep it back before the batch is applied).
    static func held(_ asset: String, _ s: GateState) -> Bool {
        s.optedOut[asset] != nil || s.batches.contains { $0.assets.contains(asset) }
    }

    /// The extension's: photos PhotoKit says were added since its last run. Returns those to make jobs for now:
    /// under `everything` all of them, as before; otherwise none of them (they become candidates). Either way the
    /// approved photos the app has not sent are taken too, and what an earlier run took and made no job for. A photo
    /// held back here is never a job. What it returns stays in `pending`, in this same locked write, until
    /// `queued` says its jobs exist: an extension ended in between loses nothing (specs/PhoneGate.tla).
    static func route(_ inserted: [String], _ s: inout GateState) -> [String] {
        var out = s.pending + s.approved
        s.approved = []
        if s.policy == .everything {
            out += inserted
        } else {
            let k = known(s)
            for id in inserted where !k.contains(id) && !s.candidates.contains(id) { s.candidates.append(id) }
        }
        out = unique(out.filter { !held($0, s) })
        if s.policy?.gated ?? true { s.shared.formUnion(out) }
        s.pending = out
        return out
    }

    /// The extension's, once it has made the jobs it could: `left` are those it could not make (the job limit).
    static func queued(left: [String], _ s: inout GateState) {
        s.pending = left
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
    /// worked out by the caller, who has PhotoKit), which wait in `unsentAdd` for the server (every one: what
    /// serverTombstones says may be a lift behind). The batch is done, but for a marked photo with no hashes (its
    /// file could not be read, or it was marked while the others were hashed): it stays in the batch, marked, never
    /// sent, until it can be kept back with its hashes. Returns (shared, kept back).
    @discardableResult
    static func apply(_ batch: String, hashes: [String: [String]], _ s: inout GateState) -> (share: [String], keep: [String]) {
        guard let i = s.batches.firstIndex(where: { $0.id == batch }) else { return ([], []) }
        var b = s.batches.remove(at: i)
        let share = b.assets.filter { !b.marked.contains($0) }
        let keep = b.assets.filter { b.marked.contains($0) && !(hashes[$0] ?? []).isEmpty }
        let stay = b.assets.filter { b.marked.contains($0) && (hashes[$0] ?? []).isEmpty }
        for id in keep {
            let hs = hashes[id] ?? []
            s.optedOut[id] = hs
            s.unsentLift.removeAll { hs.contains($0) }
            s.unsentAdd += hs.filter { !s.unsentAdd.contains($0) }
        }
        s.approved += share.filter { !s.approved.contains($0) }
        if !stay.isEmpty {
            b.assets = stay
            b.marked = Set(stay)
            b.flagged = b.flagged.filter { stay.contains($0.key) }
            s.batches.insert(b, at: i)
        }
        return (share, keep)
    }

    /// "Share after all": an opt-out taken back, by the member who made it, on their phone. Its hashes are lifted
    /// (every one: one still in unsentAdd may be in a POST under way), and so are the photos kept back with the same
    /// bytes (a copy kept back would refuse it, and the server would no longer refuse the copy). They wait in
    /// `lifting` until the server has answered the lift (`told`), so that nothing sends them while it still refuses
    /// them; a photo with no hashes is approved at once.
    static func shareAfterAll(_ asset: String, _ s: inout GateState) {
        guard s.optedOut[asset] != nil else { return }
        var ids: Set<String> = [asset], lift = Set(s.optedOut[asset] ?? [])
        var grew = !lift.isEmpty
        while grew {                       // the copies, and theirs
            grew = false
            for (id, hs) in s.optedOut where !ids.contains(id) && !lift.isDisjoint(with: hs) {
                ids.insert(id)
                lift.formUnion(hs)
                grew = true
            }
        }
        for id in ids.sorted() {
            let hs = s.optedOut.removeValue(forKey: id) ?? []
            s.shared.remove(id)
            if hs.isEmpty {
                if !s.approved.contains(id) { s.approved.append(id) }
            } else {
                s.lifting[id] = hs
            }
        }
        s.unsentAdd.removeAll { lift.contains($0) }
        s.unsentLift += lift.sorted().filter { !s.unsentLift.contains($0) }
        s.serverTombstones.subtract(lift)
    }

    /// Is a file with this hash opted out by this member: here, on another of their phones, or not yet said to the server.
    static func refused(_ sha: String, _ s: GateState) -> Bool {
        let sha = sha.lowercased()
        if s.unsentLift.contains(sha) { return false }
        return s.serverTombstones.contains(sha) || s.unsentAdd.contains(sha) || s.optedOut.values.contains { $0.contains(sha) }
    }

    /// The uploader found that `asset` is opted out (its hash, here or by the server): it is kept back here too. Not
    /// while this phone has a lift of that hash on its way: the server refuses it only until it hears the lift.
    static func keptBack(_ asset: String, sha: String, _ s: inout GateState) {
        guard !s.unsentLift.contains(sha.lowercased()) else { return }
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

    /// The server has these hashes now (POST /api/tombstones answered). A photo shared after all whose hashes have
    /// all been lifted is approved.
    static func told(added: [String], lifted: [String], _ s: inout GateState) {
        s.unsentAdd.removeAll { added.contains($0) }
        s.unsentLift.removeAll { lifted.contains($0) }
        s.serverTombstones.formUnion(added)
        s.serverTombstones.subtract(lifted)
        for (id, hs) in s.lifting.sorted(by: { $0.key < $1.key }) where !hs.contains(where: s.unsentLift.contains) {
            s.lifting.removeValue(forKey: id)
            if !s.approved.contains(id) { s.approved.append(id) }
        }
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

/// The app's talk with the server about tombstones (GateModel.tellServer: POST /api/tombstones, then Gate.told; and
/// refresh's GET /api/tombstones, then serverTombstones), one at a time. Two POSTs under way at once may reach the
/// server in either order (an add after the lift meant to undo it), and the answer to a GET read before a POST and
/// written after it puts an older copy over a newer one (specs/PhoneGate.tla). A call while one runs is left to it:
/// it goes round again, with the unsent lists as they are then.
struct ServerTurns {
    private(set) var busy = false
    private var again = false, read = false

    /// May this call talk to the server now (`read`: it wants GET /api/tombstones too)? If not, the one under way
    /// goes round again for it.
    mutating func begin(read: Bool) -> Bool {
        self.read = self.read || read
        if busy { again = true; return false }
        busy = true
        return true
    }

    /// A round starts: it POSTs what is unsent, then GETs if a call asked for it (the answer).
    mutating func round() -> Bool {
        defer { read = false; again = false }
        return read
    }

    /// A round is over: true to go round again (a call came meanwhile), false when it is done.
    mutating func end() -> Bool {
        if again { return true }
        busy = false
        return false
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
