import Foundation
import Network
import SwiftUI

/// Brings photos onto the phone ahead of time, so the library is there with no network: a thumbnail
/// of every photo, and full-screen copies of the ones chosen in Settings. Runs while the app is open,
/// on Wi-Fi unless told otherwise, newest photos first, and picks up where it left off.
@MainActor
final class Downloader: ObservableObject {
    static let shared = Downloader()

    enum Full: String, CaseIterable, Identifiable {
        case viewed, favourites, year, all
        var id: String { rawValue }
        var title: String {
            switch self {
            case .viewed: "Only what I've looked at"
            case .favourites: "Favourites"
            case .year: "Favourites and the last year"
            case .all: "Every photo"
            }
        }
    }

    private static let d = UserDefaults.standard
    @Published var keepThumbs = d.object(forKey: "offline.thumbs") as? Bool ?? true { didSet { Self.d.set(keepThumbs, forKey: "offline.thumbs"); restart() } }
    @Published var full = Full(rawValue: d.string(forKey: "offline.full") ?? "") ?? .favourites { didSet { Self.d.set(full.rawValue, forKey: "offline.full"); restart() } }
    @Published var cellular = d.bool(forKey: "offline.cellular") { didSet { Self.d.set(cellular, forKey: "offline.cellular"); restart() } }
    @Published var capGB = d.object(forKey: "offline.capGB") as? Int ?? 5 { didSet { Self.d.set(capGB, forKey: "offline.capGB"); restart() } }

    @Published private(set) var thumbsHave = 0
    @Published private(set) var thumbsWant = 0
    @Published private(set) var fullHave = 0
    @Published private(set) var fullWant = 0
    @Published private(set) var status = ""
    @Published private(set) var running = false

    private var task: Task<Void, Never>?
    private let monitor = NWPathMonitor()
    private var expensive = false
    private var connected = true

    private init() {
        monitor.pathUpdateHandler = { [weak self] path in
            Task { @MainActor in
                guard let self else { return }
                let changed = self.expensive != path.isExpensive || self.connected != (path.status == .satisfied)
                self.expensive = path.isExpensive
                self.connected = path.status == .satisfied
                if changed { self.restart() }
            }
        }
        monitor.start(queue: .global(qos: .utility))
    }

    var capBytes: Int64 { Int64(capGB) << 30 }

    func restart() {
        task?.cancel()
        task = Task { await run() }
    }

    func stop() { task?.cancel() }

    /// The photos to keep full-screen copies of, newest first.
    private func wantedFull() async -> [String] {
        let sql: String
        switch full {
        case .viewed: return []
        case .favourites: sql = "fav=1"
        case .year:
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd"
            sql = "fav=1 OR whenk >= '\(f.string(from: Date().addingTimeInterval(-365 * 86400)))'"
        case .all: sql = "1"
        }
        return (try? await Catalog.shared.store.read { try $0.query("SELECT sha FROM photos WHERE \(sql) ORDER BY whenk DESC") { $0.text(0) } }) ?? []
    }

    private func run() async {
        running = true
        defer { running = false }
        let store = ImageStore.shared
        let all = (try? await Catalog.shared.store.read { try $0.query("SELECT sha FROM photos ORDER BY whenk DESC") { $0.text(0) } }) ?? []
        let held = await store.thumbsHeld()
        let known = Set(all)
        if !all.isEmpty { await store.forget(held.subtracting(known).map { $0 }) }     // photos deleted on the server
        let wantFull = await wantedFull()
        await store.trim(to: capBytes, keep: Set(wantFull))
        if Task.isCancelled { return }

        let thumbsTodo = keepThumbs ? all.filter { !held.contains($0) } : []
        let fullTodo = wantFull.filter { !store.has(.preview, $0) }
        thumbsWant = keepThumbs ? all.count : held.intersection(known).count
        thumbsHave = thumbsWant - thumbsTodo.count
        fullWant = wantFull.count
        fullHave = fullWant - fullTodo.count
        guard !thumbsTodo.isEmpty || !fullTodo.isEmpty else { status = all.isEmpty ? "" : "Everything is on this iPhone."; return }
        guard connected else { status = "No network: will carry on when there is one."; return }
        guard !expensive || cellular else { status = "Waiting for Wi-Fi."; return }

        status = "Downloading…"
        store.retryNetwork()
        var ok = await fetch(thumbsTodo, lanes: 6, each: { await store.thumbData($0) != nil }, done: { self.thumbsHave += $0 })
        if ok { ok = await fetch(fullTodo, lanes: 3, each: { await store.fetch(.preview, $0, touch: false) != nil }, done: { self.fullHave += $0 }) }
        if Task.isCancelled { return }
        status = ok ? "Everything is on this iPhone." : "Can't reach the server: will carry on later."
    }

    /// Fetch each item, `lanes` at a time. Gives up (false) after a run of failures: the server is away.
    private func fetch(_ items: [String], lanes: Int, each: @escaping (String) async -> Bool, done: @escaping (Int) -> Void) async -> Bool {
        var next = 0, failures = 0, pending = 0
        return await withTaskGroup(of: Bool.self) { group in
            func add() { if next < items.count { let s = items[next]; next += 1; group.addTask { await each(s) } } }
            for _ in 0..<lanes { add() }
            for await ok in group {
                if Task.isCancelled { group.cancelAll(); return false }
                if ok { failures = 0; pending += 1 } else { failures += 1 }
                if pending >= 25 || next >= items.count { done(pending); pending = 0 }
                if failures >= 12 { group.cancelAll(); done(pending); return false }
                if !ok { try? await Task.sleep(for: .milliseconds(400)) }
                add()
            }
            done(pending)
            return true
        }
    }
}
