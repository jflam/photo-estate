import CryptoKit
import Foundation
import Photos

/// How one photo or video resource becomes one request to the Photo Estate server.
///
/// The server keeps uploads in a folder `YYYY/MM/DD/<name>-<id>.<ext>` and files that already exist
/// are a no-op, so the system uploader and the in-app uploader can both send the same photo safely:
/// they build the same path from the same asset.
enum UploadRequest {
    /// The server's address, from Info.plist (`SERVER_BASE` in project.yml).
    static var baseURL: URL {
        let s = Bundle.main.object(forInfoDictionaryKey: "BackgroundUploadURLBase") as? String ?? ""
        return URL(string: s) ?? URL(string: "https://johns-mac-mini.taila5abb.ts.net:8766")!
    }

    /// The originals only: the still or video as it was captured, and a Live Photo's video.
    static func wanted(_ r: PHAssetResource) -> Bool {
        r.type == .photo || r.type == .video || r.type == .pairedVideo
    }

    /// `2026/11/14/IMG_0042-3fa9c1b.HEIC`: capture day in UTC, the original name, and 7 hex digits
    /// of the asset's identifier so two photos called IMG_0042 never collide.
    static func path(asset: PHAsset, resource: PHAssetResource) -> String {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        let d = cal.dateComponents([.year, .month, .day], from: asset.creationDate ?? Date(timeIntervalSince1970: 0))
        let name = (resource.filename ?? "photo") as NSString
        let stem = clean(name.deletingPathExtension)
        let ext = clean(name.pathExtension).uppercased()
        let id = SHA256.hash(data: Data(asset.localIdentifier.utf8)).prefix(4).map { String(format: "%02x", $0) }.joined().prefix(7)
        let file = "\(stem)-\(id)" + (ext.isEmpty ? "" : ".\(ext)")
        return String(format: "%04d/%02d/%02d/", d.year!, d.month!, d.day!) + file
    }

    /// The server only accepts letters, digits and `_ ( ) . + , ' = -` and space in a name.
    private static func clean(_ s: String) -> String {
        let ok = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 _().+,'=-")
        let out = String(s.map { ok.contains($0) ? $0 : "_" })
        return out.isEmpty ? "photo" : out
    }

    /// POST with the metadata the catalog needs as headers; the server writes the `.xmp` sidecar from them.
    static func make(asset: PHAsset, resource: PHAssetResource, base: URL = baseURL) -> URLRequest {
        let rel = path(asset: asset, resource: resource)
        var req = URLRequest(url: base.appendingPathComponent("api/upload/" + rel))
        req.httpMethod = "POST"
        req.setValue(resource.filename, forHTTPHeaderField: "X-Filename")
        if let d = asset.creationDate {
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.timeZone = TimeZone(identifier: "UTC")
            f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss'Z'"
            req.setValue(f.string(from: d), forHTTPHeaderField: "X-Taken")
        }
        if let loc = asset.location?.coordinate {
            req.setValue(String(loc.latitude), forHTTPHeaderField: "X-Latitude")
            req.setValue(String(loc.longitude), forHTTPHeaderField: "X-Longitude")
        }
        if asset.isFavorite { req.setValue("true", forHTTPHeaderField: "X-Favorite") }
        return req
    }
}
