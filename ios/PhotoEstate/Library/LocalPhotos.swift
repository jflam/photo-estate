import Photos
import SwiftUI
import UIKit

/// Photos on this iPhone that the server has not catalogued yet: just taken, waiting for the uploader,
/// or never sent. They join the Library in time order, so it shows everything the Photos app does.
///
/// The server's catalog knows a photo by the hash of its bytes, which the phone can't compute without
/// exporting every original, so a photo here counts as already there when the catalog has one taken in
/// the same second (UTC, give or take one), or at the same wall-clock time to the second for the photos
/// whose time zone was never known. The catalog holds stills only, so videos are left out.
///
/// Their tiles are named `local:<PHAsset localIdentifier>` instead of by hash, and their images come from
/// Photos, not from the server.
@MainActor
final class LocalPhotos: NSObject, ObservableObject, PHPhotoLibraryChangeObserver {
    static let shared = LocalPhotos()
    nonisolated static let prefix = "local:"

    /// Newest first.
    @Published private(set) var pending: [Tile] = []
    @Published private(set) var revision = 0
    @Published private(set) var allowed = false
    @Published var show = UserDefaults.standard.object(forKey: "view.showLocal") as? Bool ?? true {
        didSet { UserDefaults.standard.set(show, forKey: "view.showLocal"); revision += 1 }
    }

    private var assets: [String: PHAsset] = [:]
    private var known: (utc: Set<Int>, wall: Set<String>)?
    private var knownRevision = -1
    private var running: Task<Void, Never>?
    private var again = false
    private var observing = false
    private let images = PHCachingImageManager()

    nonisolated static func id(_ sha: String) -> String? { sha.hasPrefix(prefix) ? String(sha.dropFirst(prefix.count)) : nil }

    /// Ask for the photo library once, then look.
    func start() {
        switch PHPhotoLibrary.authorizationStatus(for: .readWrite) {
        case .authorized, .limited:
            allowed = true
            observe()
            refresh()
        case .notDetermined:
            Task {
                let s = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
                allowed = s == .authorized || s == .limited
                if allowed { observe(); refresh() }
            }
        default:
            allowed = false
        }
    }

    private func observe() {
        guard !observing else { return }
        observing = true
        PHPhotoLibrary.shared().register(self)
    }

    nonisolated func photoLibraryDidChange(_ change: PHChange) {
        Task { @MainActor in self.refresh() }
    }

    /// Compare the phone's photos with the catalog again. Calls that come while one runs fold into one more.
    func refresh() {
        guard allowed else { return }
        if running != nil { again = true; return }
        running = Task {
            repeat {
                again = false
                await run()
            } while again
            running = nil
        }
    }

    private func run() async {
        let rev = SyncEngine.shared.revision
        if known == nil || knownRevision != rev {
            known = await LocalPhotos.catalogTimes()
            knownRevision = rev
        }
        guard let known else { return }
        let found = await Task.detached(priority: .utility) { LocalPhotos.unmatched(known) }.value
        assets = Dictionary(found.map { ($0.localIdentifier, $0) }, uniquingKeysWith: { a, _ in a })
        let tiles = found.map(LocalPhotos.tile)
        if tiles != pending {
            pending = tiles
            revision += 1
        }
    }

    /// When every catalogued photo was taken: as UTC seconds, and as wall-clock text for the ones with no zone.
    private static func catalogTimes() async -> (utc: Set<Int>, wall: Set<String>) {
        (try? await Catalog.shared.store.read { db in
            var utc = Set<Int>(), wall = Set<String>()
            for (taken, local) in try db.query("SELECT taken, local FROM photos", [], { ($0.string(0), $0.string(1)) }) {
                if let t = taken, let s = seconds(t) { utc.insert(s) } else if let t = taken { wall.insert(String(t.prefix(19))) }
                if let l = local { wall.insert(String(l.prefix(19))) }
            }
            return (utc, wall)
        }) ?? ([], [])
    }

    /// "2019-12-27T21:22:31+00:00" (or "Z") as seconds since 1970; nil without a zone.
    nonisolated static func seconds(_ s: String) -> Int? {
        let u = Array(s.utf8)
        guard u.count >= 20 else { return nil }
        func num(_ a: Int, _ n: Int) -> Int? {
            var v = 0
            for i in a..<a + n { guard u[i] >= 48 && u[i] <= 57 else { return nil }; v = v * 10 + Int(u[i] - 48) }
            return v
        }
        guard let y = num(0, 4), let mo = num(5, 2), let d = num(8, 2), let h = num(11, 2), let mi = num(14, 2), let se = num(17, 2) else { return nil }
        var i = 19
        if i < u.count, u[i] == UInt8(ascii: ".") { i += 1; while i < u.count, u[i] >= 48, u[i] <= 57 { i += 1 } }
        guard i < u.count else { return nil }
        var offset = 0
        if u[i] == UInt8(ascii: "Z") {
            offset = 0
        } else if u[i] == UInt8(ascii: "+") || u[i] == UInt8(ascii: "-") {
            let rest = Array(u[(i + 1)...]).filter { $0 != UInt8(ascii: ":") }
            guard rest.count >= 4 else { return nil }
            let oh = Int(rest[0] - 48) * 10 + Int(rest[1] - 48), om = Int(rest[2] - 48) * 10 + Int(rest[3] - 48)
            offset = (oh * 3600 + om * 60) * (u[i] == UInt8(ascii: "-") ? -1 : 1)
        } else {
            return nil
        }
        // days from the civil date (Howard Hinnant's algorithm)
        let yy = mo <= 2 ? y - 1 : y
        let era = (yy >= 0 ? yy : yy - 399) / 400
        let yoe = yy - era * 400
        let doy = (153 * (mo + (mo > 2 ? -3 : 9)) + 2) / 5 + d - 1
        let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy
        let days = era * 146097 + doe - 719468
        return days * 86400 + h * 3600 + mi * 60 + se - offset
    }

    /// The still photos in the library that the catalog has no photo for, newest first.
    nonisolated private static func unmatched(_ known: (utc: Set<Int>, wall: Set<String>)) -> [PHAsset] {
        let opts = PHFetchOptions()
        opts.predicate = NSPredicate(format: "mediaType == %d", PHAssetMediaType.image.rawValue)
        opts.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
        let all = PHAsset.fetchAssets(with: opts)
        let wall = DateFormatter()
        wall.locale = Locale(identifier: "en_US_POSIX")
        wall.timeZone = .current
        wall.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        var out: [PHAsset] = []
        all.enumerateObjects { a, _, _ in
            guard let d = a.creationDate else { return }
            let s = Int(d.timeIntervalSince1970.rounded(.down))
            if known.utc.contains(s) || known.utc.contains(s - 1) || known.utc.contains(s + 1) { return }
            if known.wall.contains(wall.string(from: d)) { return }
            out.append(a)
        }
        return out
    }

    private static let wallClock: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return f
    }()

    private static func tile(_ a: PHAsset) -> Tile {
        let ar = a.pixelHeight > 0 ? Float(a.pixelWidth) / Float(a.pixelHeight) : 0.75
        return Tile(sha: prefix + a.localIdentifier, ar: ar, when: a.creationDate.map { wallClock.string(from: $0) } ?? "", city: nil, fav: a.isFavorite)
    }

    // MARK: what the rest of the app asks

    /// The pending tiles a question allows: only for browsing by time, favourites and dates (people,
    /// places and the rest are known only once the server has the photo).
    func tiles(for q: Query) -> [Tile] {
        guard show, allowed, q.text.isEmpty, q.similar == nil, q.event == nil, !q.picks.values.contains(where: { !$0.isEmpty }) else { return [] }
        let since = q.recent?.since
        return pending.filter { t in
            if q.fav && !t.fav { return false }
            if let since, t.when < since { return false }
            if q.from != nil || q.to != nil {
                let ym = Catalog.monthNumber(t.when)
                if ym < (q.from ?? 0) || ym > (q.to ?? 1_000_000) { return false }
            }
            return true
        }
    }

    /// The image at about `px` on its long side, from the phone or iCloud.
    func image(_ sha: String, px: CGFloat) async -> UIImage? {
        guard let id = LocalPhotos.id(sha), let asset = asset(id) else { return nil }
        let o = PHImageRequestOptions()
        o.deliveryMode = .highQualityFormat
        o.resizeMode = .fast
        o.isNetworkAccessAllowed = true
        let ar = CGFloat(max(asset.pixelWidth, 1)) / CGFloat(max(asset.pixelHeight, 1))
        let size = ar >= 1 ? CGSize(width: px, height: px / ar) : CGSize(width: px * ar, height: px)
        let manager = images
        return await withCheckedContinuation { (k: CheckedContinuation<UIImage?, Never>) in
            manager.requestImage(for: asset, targetSize: size, contentMode: .aspectFit, options: o) { img, _ in k.resume(returning: img) }
        }
    }

    /// The original file, for sharing.
    func original(_ sha: String) async -> URL? {
        guard let id = LocalPhotos.id(sha), let asset = asset(id),
              let r = PHAssetResource.assetResources(for: asset).first(where: { $0.type == .photo }) ?? PHAssetResource.assetResources(for: asset).first
        else { return nil }
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("share-local-\(UUID().uuidString.prefix(8))", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent(r.originalFilename)
        let o = PHAssetResourceRequestOptions()
        o.isNetworkAccessAllowed = true
        do { try await PHAssetResourceManager.default().writeData(for: r, toFile: file, options: o) } catch { return nil }
        return file
    }

    /// What is known about it before the server has looked: when, where, its size and name.
    func photo(_ sha: String) -> Photo? {
        guard let id = LocalPhotos.id(sha), let a = asset(id) else { return nil }
        var p = Photo(json: [:])
        p.sha = sha
        p.local = a.creationDate.map { LocalPhotos.wallClock.string(from: $0) }
        p.tz = TimeZone.current.identifier
        p.lat = a.location?.coordinate.latitude
        p.lon = a.location?.coordinate.longitude
        p.fav = a.isFavorite
        p.w = a.pixelWidth
        p.h = a.pixelHeight
        p.ar = Double(a.pixelHeight > 0 ? Double(a.pixelWidth) / Double(a.pixelHeight) : 0.75)
        p.path = PHAssetResource.assetResources(for: a).first(where: { $0.type == .photo })?.originalFilename
        return p
    }

    private func asset(_ id: String) -> PHAsset? {
        if let a = assets[id] { return a }
        return PHAsset.fetchAssets(withLocalIdentifiers: [id], options: nil).firstObject
    }
}

extension Tile {
    /// On this iPhone, not yet on the server.
    var isLocal: Bool { sha.hasPrefix(LocalPhotos.prefix) }
}

/// Two lists, each newest (or oldest) first with undated last, as one.
func mergeByTime(_ a: [Tile], _ b: [Tile], oldFirst: Bool) -> [Tile] {
    guard !b.isEmpty else { return a }
    guard !a.isEmpty else { return oldFirst ? b.reversed() : b }
    let bs = oldFirst ? Array(b.reversed()) : b
    let before = { (x: Tile, y: Tile) -> Bool in
        if x.when.isEmpty != y.when.isEmpty { return !x.when.isEmpty }
        return oldFirst ? x.when < y.when : x.when > y.when
    }
    var out: [Tile] = []
    out.reserveCapacity(a.count + bs.count)
    var i = 0, j = 0
    while i < a.count && j < bs.count {
        if before(bs[j], a[i]) { out.append(bs[j]); j += 1 } else { out.append(a[i]); i += 1 }
    }
    out += a[i...]
    out += bs[j...]
    return out
}
