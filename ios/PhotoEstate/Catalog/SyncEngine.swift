import Foundation
import Network
import SwiftUI

/// Keeps the phone's catalog in step with the server's.
///
/// The server is the only writer, so this is one-way: ask the feed (`GET /api/sync`, sync.py) for what
/// changed since the cursor we hold, apply each page in one transaction together with the new cursor,
/// repeat until it says there is no more. A page that fails leaves the cursor where it was, so the next
/// run carries on from there; with no network the catalog simply stays as it is and the app works from it.
@MainActor
final class SyncEngine: ObservableObject {
    static let shared = SyncEngine()

    enum State: Equatable { case idle, syncing, failed(String) }

    @Published private(set) var state = State.idle
    @Published private(set) var photos = 0            // in the phone's catalog
    @Published private(set) var serverTotal = 0       // in the server's, as last heard
    @Published private(set) var lastSynced: Date?
    /// Did the last request to the server get an answer. Kept current while the app is open: any request
    /// says so (`markOnline`), a change of network asks again, and while it is false so does a slow knock.
    @Published private(set) var online = true { didSet { if !online && oldValue { knock() } } }
    /// Goes up whenever the catalog's contents changed: views reload on it.
    @Published private(set) var revision = 0

    private var running: Task<Void, Never>?
    private let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 20
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    private let monitor = NWPathMonitor()
    private var connected = true
    private var knocking: Task<Void, Never>?

    private init() {
        if let t = UserDefaults.standard.object(forKey: "lastSynced") as? Date { lastSynced = t }
        Task { photos = await Catalog.shared.count() }
        monitor.pathUpdateHandler = { [weak self] path in
            Task { @MainActor in
                guard let self else { return }
                let now = path.status == .satisfied
                guard now != self.connected else { return }
                self.connected = now
                if now { ImageStore.shared.retryNetwork(); await self.sync() } else { self.online = false }   // airplane mode: no need to ask
            }
        }
        monitor.start(queue: .global(qos: .utility))
    }

    /// While the server can't be reached and the app is in front, ask again, soon and then every half minute, so the
    /// app notices on its own when it is back (the tailnet coming up changes no network the phone reports).
    private func knock() {
        knocking?.cancel()
        knocking = Task {
            var wait = 5            // soon the first time: one request that failed is often only a blip
            while !online {
                try? await Task.sleep(for: .seconds(wait))
                wait = 30
                if Task.isCancelled || online { return }
                guard connected, UIApplication.shared.applicationState == .active else { continue }
                ImageStore.shared.retryNetwork()
                await sync()
                if online { Downloader.shared.restart() }
            }
        }
    }

    /// Start a sync unless one is running; returns when it ends.
    func sync() async {
        if let running { return await running.value }
        let task = Task { await run() }
        running = task
        await task.value
        running = nil
    }

    func markOnline(_ on: Bool) { if online != on { online = on } }

    /// The phone's copy was changed here to match something the server has just agreed to (a favourite).
    func catalogEdited() { revision += 1 }

    private func run() async {
        state = .syncing
        do {
            var applied = 0
            while true {
                let cursor = try await Catalog.shared.store.read { db in
                    (try Catalog.get(db, "epoch") ?? "", try Catalog.get(db, "since") ?? "0", try Catalog.get(db, "after") ?? "")
                }
                var url = URLComponents(url: Server.baseURL.appendingPathComponent("api/sync"), resolvingAgainstBaseURL: false)!
                url.queryItems = [.init(name: "epoch", value: cursor.0), .init(name: "since", value: cursor.1),
                                  .init(name: "after", value: cursor.2), .init(name: "limit", value: "2000")]
                let (data, resp) = try await session.data(from: url.url!)
                online = true
                let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
                if code == 503 {             // the server is still loading its catalog
                    try await Task.sleep(for: .seconds(3))
                    continue
                }
                guard code == 200, let j = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let epoch = j["epoch"] as? String, let since = (j["since"] as? NSNumber)?.intValue,
                      let after = j["after"] as? String else {
                    throw SQLiteError(description: code == 404 ? "The server is too old to sync with: update it." : "The server said \(code).")
                }
                let rows = j["photos"] as? [[String: Any]] ?? []
                let deleted = j["deleted"] as? [String] ?? []
                let more = j["more"] as? Bool ?? false
                let trips = j["trips"].flatMap { try? JSONSerialization.data(withJSONObject: $0) }.flatMap { String(data: $0, encoding: .utf8) }
                let count = try await Catalog.shared.store.write { db -> Int in
                    try db.transaction {
                        try Catalog.apply(db, rows: rows, deleted: deleted, epoch: epoch)
                        try Catalog.set(db, "epoch", epoch)
                        try Catalog.set(db, "since", String(since))
                        try Catalog.set(db, "after", after)
                        if !more {
                            try Catalog.dropOtherEpochs(db, keep: epoch)
                            if let trips { try Catalog.set(db, "trips", trips) }
                        }
                    }
                    return try db.one("SELECT count(*) FROM photos") { Int($0.int(0)) } ?? 0
                }
                serverTotal = (j["total"] as? NSNumber)?.intValue ?? serverTotal
                if !rows.isEmpty || !deleted.isEmpty || count != photos {
                    applied += rows.count + deleted.count
                    photos = count
                    revision += 1
                }
                if !more { break }
            }
            lastSynced = Date()
            UserDefaults.standard.set(lastSynced, forKey: "lastSynced")
            state = .idle
        } catch {
            if error is URLError { online = false }
            state = .failed((error as? SQLiteError)?.description ?? error.localizedDescription)
        }
    }
}
