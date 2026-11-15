import Foundation
import UIKit

enum ImageKind: String {
    case thumb      // 360 px: the grid
    case preview    // 1600 px: the viewer
    case large      // 4096 px: zoomed in
}

/// Requests for the same image share one download.
private actor Inflight {
    private var tasks: [String: Task<Data?, Never>] = [:]

    func data(_ key: String, _ make: @escaping () async -> Data?) async -> Data? {
        if let t = tasks[key] { return await t.value }
        let t = Task { await make() }
        tasks[key] = t
        let d = await t.value
        tasks[key] = nil
        return d
    }
}

/// The phone's copies of the photos.
///
/// Thumbnails are small and there are a great many, so they live as rows of one SQLite file
/// (`thumbs.db`): one file to open instead of 100,000, and a read is a B-tree lookup. Full-screen
/// images are files, named by the photo's hash, in a folder trimmed to a size cap, least recently
/// viewed first. Both sit in Application Support, so iOS never clears them behind the app's back, and
/// neither is backed up: the server has them all.
final class ImageStore {
    static let shared = ImageStore()

    private let thumbs: Store
    private let memory = NSCache<NSString, UIImage>()
    private let inflight = Inflight()
    private let fullDir = Catalog.folder.appendingPathComponent("full", isDirectory: true)
    private let lock = NSLock()
    private var quietUntil = Date.distantPast     // after a failed request, don't try the network for a moment
    private let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.httpMaximumConnectionsPerHost = 8
        c.urlCache = nil                          // we keep what we fetch ourselves
        c.timeoutIntervalForRequest = 20
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    private init() {
        let path = Catalog.folder.appendingPathComponent("thumbs.db").path
        let schema = "CREATE TABLE IF NOT EXISTS thumbs (sha TEXT PRIMARY KEY, data BLOB NOT NULL);"
        do {
            thumbs = try Store(path: path, schema: schema)
        } catch {
            for suffix in ["", "-wal", "-shm"] { try? FileManager.default.removeItem(atPath: path + suffix) }
            thumbs = try! Store(path: path, schema: schema)
        }
        for k in [ImageKind.preview, .large] {
            try? FileManager.default.createDirectory(at: fullDir.appendingPathComponent(k.rawValue), withIntermediateDirectories: true)
        }
        memory.totalCostLimit = 160 << 20
    }

    // MARK: network

    private var reachable: Bool {
        lock.lock(); defer { lock.unlock() }
        return Date() >= quietUntil
    }

    private func download(_ kind: ImageKind, _ sha: String) async -> Data? {
        guard reachable else { return nil }
        let url = Server.baseURL.appendingPathComponent("\(kind.rawValue)/\(sha)")
        do {
            let (data, resp) = try await session.data(from: url)
            guard (resp as? HTTPURLResponse)?.statusCode == 200, !data.isEmpty else { return nil }
            return data
        } catch {
            if (error as? URLError)?.code != .cancelled {
                lock.lock(); quietUntil = Date().addingTimeInterval(8); lock.unlock()
            }
            return nil
        }
    }

    /// Try the network again now (the app came to the front, or the user asked).
    func retryNetwork() {
        lock.lock(); quietUntil = .distantPast; lock.unlock()
    }

    private static func decode(_ data: Data) async -> UIImage? {
        await Task.detached(priority: .userInitiated) { UIImage(data: data)?.preparingForDisplay() }.value
    }

    private func remember(_ key: String, _ image: UIImage) {
        memory.setObject(image, forKey: key as NSString, cost: Int(image.size.width * image.size.height * image.scale * image.scale * 4))
    }

    // MARK: thumbnails

    /// A thumbnail that is already decoded, without waiting. The grid asks this first.
    func ready(_ sha: String) -> UIImage? { memory.object(forKey: sha as NSString) }

    func thumb(_ sha: String) async -> UIImage? {
        if let i = ready(sha) { return i }
        var data = await thumbData(sha, network: false)
        if data == nil {            // not on the phone: ask the server, unless the grid has already scrolled past
            try? await Task.sleep(for: .milliseconds(60))
            if Task.isCancelled { return nil }
            data = await thumbData(sha)
        }
        guard let data, let image = await ImageStore.decode(data) else { return nil }
        remember(sha, image)
        return image
    }

    /// The JPEG: from the phone if it is here, else from the server (and then kept).
    func thumbData(_ sha: String, network: Bool = true) async -> Data? {
        if let d = try? await thumbs.read({ try $0.one("SELECT data FROM thumbs WHERE sha=?", [sha]) { $0.data(0) } ?? nil }) { return d }
        guard network else { return nil }
        return await inflight.data(sha) {
            guard let d = await self.download(.thumb, sha) else { return nil }
            _ = try? await self.thumbs.write { try $0.run("INSERT OR REPLACE INTO thumbs VALUES (?, ?)", [sha, d]) }
            return d
        }
    }

    func thumbsHeld() async -> Set<String> {
        Set((try? await thumbs.read { try $0.query("SELECT sha FROM thumbs") { $0.text(0) } }) ?? [])
    }

    // MARK: full-screen images

    func file(_ kind: ImageKind, _ sha: String) -> URL {
        fullDir.appendingPathComponent(kind.rawValue).appendingPathComponent("\(sha).jpg")
    }

    func has(_ kind: ImageKind, _ sha: String) -> Bool { FileManager.default.fileExists(atPath: file(kind, sha).path) }

    /// The file for a full-screen image: kept from before, else fetched and kept. Nil when it is not on
    /// the phone and the server cannot be reached.
    func fetch(_ kind: ImageKind, _ sha: String, touch: Bool = true) async -> URL? {
        let url = file(kind, sha)
        if FileManager.default.fileExists(atPath: url.path) {
            if touch { try? FileManager.default.setAttributes([.modificationDate: Date()], ofItemAtPath: url.path) }
            return url
        }
        let ok = await inflight.data("\(kind.rawValue)/\(sha)") {
            guard let d = await self.download(kind, sha) else { return nil }
            return (try? d.write(to: url, options: .atomic)) == nil ? nil : Data([1])
        }
        return ok == nil ? nil : url
    }

    func image(_ kind: ImageKind, _ sha: String) async -> UIImage? {
        guard let url = await fetch(kind, sha) else { return nil }
        return await Task.detached(priority: .userInitiated) { UIImage(contentsOfFile: url.path)?.preparingForDisplay() }.value
    }

    // MARK: housekeeping

    struct Usage {
        var thumbCount = 0
        var thumbBytes: Int64 = 0
        var fullCount = 0
        var fullBytes: Int64 = 0
    }

    private func fullFiles() -> [(url: URL, bytes: Int64, used: Date)] {
        var out: [(URL, Int64, Date)] = []
        for k in [ImageKind.preview, .large] {
            let dir = fullDir.appendingPathComponent(k.rawValue)
            let keys: [URLResourceKey] = [.fileSizeKey, .contentModificationDateKey]
            for u in (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: keys)) ?? [] {
                let v = try? u.resourceValues(forKeys: Set(keys))
                out.append((u, Int64(v?.fileSize ?? 0), v?.contentModificationDate ?? .distantPast))
            }
        }
        return out
    }

    func usage() async -> Usage {
        var u = Usage()
        u.thumbCount = (try? await thumbs.read { try $0.one("SELECT count(*) FROM thumbs") { Int($0.int(0)) } ?? 0 }) ?? 0
        let path = Catalog.folder.appendingPathComponent("thumbs.db").path
        for suffix in ["", "-wal"] {
            u.thumbBytes += ((try? FileManager.default.attributesOfItem(atPath: path + suffix))?[.size] as? NSNumber)?.int64Value ?? 0
        }
        let files = await Task.detached { self.fullFiles() }.value
        u.fullCount = files.count
        u.fullBytes = files.reduce(0) { $0 + $1.bytes }
        return u
    }

    /// Delete full-screen images, least recently viewed first, until they fit in `cap` bytes. `keep`
    /// (photos the user asked to have offline) are never deleted.
    func trim(to cap: Int64, keep: Set<String>) async {
        await Task.detached(priority: .utility) {
            var files = self.fullFiles()
            var total = files.reduce(0) { $0 + $1.bytes }
            guard total > cap else { return }
            files.sort { $0.used < $1.used }
            for f in files where total > cap {
                if keep.contains(f.url.deletingPathExtension().lastPathComponent) { continue }
                try? FileManager.default.removeItem(at: f.url)
                total -= f.bytes
            }
        }.value
    }

    /// Remove photos the catalog no longer has.
    func forget(_ shas: [String]) async {
        guard !shas.isEmpty else { return }
        _ = try? await thumbs.write { db in try db.transaction { for s in shas { try db.run("DELETE FROM thumbs WHERE sha=?", [s]) } } }
        for s in shas { for k in [ImageKind.preview, .large] { try? FileManager.default.removeItem(at: file(k, s)) } }
    }

    func clearFull() async {
        for f in fullFiles() { try? FileManager.default.removeItem(at: f.url) }
    }

    func clearThumbs() async {
        memory.removeAllObjects()
        _ = try? await thumbs.write { try $0.exec("DELETE FROM thumbs; VACUUM;") }
    }
}
