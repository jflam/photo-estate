import Photos
import UIKit

/// The copy of a catalogued photo that this iPhone's own Photos library already holds. Much of the
/// library was taken on this phone, so its full-screen image is often here already: nothing to
/// download, and it is there in airplane mode.
///
/// It is found the way `LocalPhotos` decides a photo has been uploaded (taken in the same second, or a
/// file of the same name within a day), and then trusted only when its pixel size is the same too:
/// showing a different photo than the tile promised would be worse than showing a soft one. A photo
/// cropped on the phone since it was uploaded therefore doesn't count, and comes from the server.
enum OnPhone {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var looked: [String: String?] = [:]     // sha -> localIdentifier; nil: looked, not here

    /// The Photos library changed: look again.
    static func forget() { lock.withLock { looked.removeAll() } }

    private static var allowed: Bool {
        let s = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        return s == .authorized || s == .limited
    }

    static func asset(_ sha: String) async -> PHAsset? {
        guard allowed, LocalPhotos.id(sha) == nil else { return nil }
        if let hit = lock.withLock({ looked[sha] }) {
            guard let id = hit else { return nil }
            if let a = PHAsset.fetchAssets(withLocalIdentifiers: [id], options: nil).firstObject { return a }
        }
        guard let photo = await Catalog.shared.photo(sha) else { return nil }
        let a = find(photo)
        lock.withLock { looked[sha] = .some(a?.localIdentifier) }
        return a
    }

    private static func find(_ p: Photo) -> PHAsset? {
        guard let w = p.w, let h = p.h, w > 0, h > 0 else { return nil }
        let sameSize = { (a: PHAsset) in (a.pixelWidth == w && a.pixelHeight == h) || (a.pixelWidth == h && a.pixelHeight == w) }
        let stems = p.path.map(LocalPhotos.stems) ?? []
        let named = { (a: PHAsset) -> Bool in
            let rs = PHAssetResource.assetResources(for: a)
            guard let name = (rs.first { $0.type == .photo } ?? rs.first)?.originalFilename else { return false }
            return stems.contains((name as NSString).deletingPathExtension.uppercased())
        }

        // Taken in the same second: UTC give or take one, or the same wall-clock time where no zone was known.
        let wall = DateFormatter()
        wall.locale = Locale(identifier: "en_US_POSIX")
        wall.timeZone = .current
        wall.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        let utc = p.taken.flatMap(LocalPhotos.seconds)
        var spans: [(Date, Date)] = []
        if let utc { spans.append((Date(timeIntervalSince1970: Double(utc - 1)), Date(timeIntervalSince1970: Double(utc + 2)))) }
        for text in [utc == nil ? p.taken : nil, p.local] {
            if let text, let d = wall.date(from: String(text.prefix(19))) { spans.append((d, d.addingTimeInterval(1))) }
        }
        for (from, to) in spans {
            let found = taken(from, to).filter(sameSize)
            if found.count == 1 { return found[0] }
            if let a = found.first(where: named) { return a }      // several in that second: the name decides
        }

        // Else a file of the same name within a day (another copy's time can be a zone away).
        guard !stems.isEmpty, let s = utc ?? p.local.flatMap({ LocalPhotos.seconds(String($0.prefix(19)) + "Z") }) else { return nil }
        let day = 26.0 * 3600, at = Date(timeIntervalSince1970: Double(s))
        return taken(at.addingTimeInterval(-day), at.addingTimeInterval(day)).filter(sameSize).first(where: named)
    }

    private static func taken(_ from: Date, _ to: Date) -> [PHAsset] {
        let opts = PHFetchOptions()
        opts.predicate = NSPredicate(format: "mediaType == %d AND creationDate >= %@ AND creationDate < %@",
                                     PHAssetMediaType.image.rawValue, from as NSDate, to as NSDate)
        var out: [PHAsset] = []
        PHAsset.fetchAssets(with: opts).enumerateObjects { a, _, _ in out.append(a) }
        return out
    }

    /// The image at about `px` on its long side, from what Photos holds on the phone itself (never from
    /// iCloud: the server is as near, and has exactly the photo the catalog means). `sharp` is false
    /// when Photos has only kept a smaller copy here: better than a thumbnail, with nothing else to show.
    static func image(_ sha: String, px: CGFloat) async -> (image: UIImage, sharp: Bool)? {
        guard let asset = await asset(sha) else { return nil }
        let long = CGFloat(max(asset.pixelWidth, asset.pixelHeight, 1))
        let ar = CGFloat(max(asset.pixelWidth, 1)) / CGFloat(max(asset.pixelHeight, 1))
        let size = ar >= 1 ? CGSize(width: px, height: px / ar) : CGSize(width: px * ar, height: px)
        for mode in [PHImageRequestOptionsDeliveryMode.highQualityFormat, .fastFormat] {
            let o = PHImageRequestOptions()
            o.deliveryMode = mode
            o.resizeMode = .fast
            o.isNetworkAccessAllowed = false
            let got = await withCheckedContinuation { (k: CheckedContinuation<(UIImage, Bool)?, Never>) in
                PHImageManager.default().requestImage(for: asset, targetSize: size, contentMode: .aspectFit, options: o) { img, info in
                    k.resume(returning: img.map { ($0, (info?[PHImageResultIsDegradedKey] as? Bool) ?? false) })
                }
            }
            guard let (img, degraded) = got else { continue }
            let have = max(img.size.width, img.size.height) * img.scale
            let sharp = !degraded && have >= min(px, long) * 0.6
            if sharp || have > 500 { return (img.preparingForDisplay() ?? img, sharp) }
        }
        return nil
    }
}
