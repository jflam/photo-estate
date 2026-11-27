import CryptoKit
import Foundation
import Photos

/// How one photo or video resource becomes one request to the Photo Estate server.
///
/// The server keeps uploads in a folder `YYYY/MM/DD/<name>-<id>.<ext>` and files that already exist
/// are a no-op, so the system uploader and the in-app uploader can both send the same photo safely:
/// they build the same path from the same asset.
enum UploadRequest {
    static var baseURL: URL { Server.baseURL }

    /// What a request says about a file, read from PhotoKit (`fields`) or given (a test).
    struct Fields: Equatable {
        var created: Date?
        var filename: String?
        var localIdentifier: String
        var latitude: Double?
        var longitude: Double?
        var favorite = false
        var screenshot = false           // PhotoKit's screenshot subtype: the server's hold reads it (X-Screenshot)
    }

    /// The originals only: the still or video as it was captured, and a Live Photo's video.
    static func wanted(_ r: PHAssetResource) -> Bool {
        r.type == .photo || r.type == .video || r.type == .pairedVideo
    }

    /// The resource's file name (iOS 27 / Xcode 27 renamed `originalFilename` to `filename`; older SDKs use `originalFilename`).
    static func name(_ r: PHAssetResource) -> String? {
        #if compiler(>=6.4)
        if #available(iOS 27, macOS 27, *) { return r.filename }
        #endif
        return r.originalFilename
    }

    static func fields(asset: PHAsset, resource: PHAssetResource) -> Fields {
        Fields(created: asset.creationDate, filename: name(resource), localIdentifier: asset.localIdentifier,
               latitude: asset.location?.coordinate.latitude, longitude: asset.location?.coordinate.longitude,
               favorite: asset.isFavorite, screenshot: asset.mediaSubtypes.contains(.photoScreenshot))
    }

    static func path(asset: PHAsset, resource: PHAssetResource) -> String { path(fields(asset: asset, resource: resource)) }

    /// `2026/11/14/IMG_0042-3fa9c1b.HEIC`: capture day in UTC, the original name, and 7 hex digits
    /// of the asset's identifier so two photos called IMG_0042 never collide.
    static func path(_ f: Fields) -> String {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        let d = cal.dateComponents([.year, .month, .day], from: f.created ?? Date(timeIntervalSince1970: 0))
        let name = (f.filename ?? "photo") as NSString
        let stem = clean(name.deletingPathExtension)
        let ext = clean(name.pathExtension).uppercased()
        let id = SHA256.hash(data: Data(f.localIdentifier.utf8)).prefix(4).map { String(format: "%02x", $0) }.joined().prefix(7)
        let file = "\(stem)-\(id)" + (ext.isEmpty ? "" : ".\(ext)")
        return String(format: "%04d/%02d/%02d/", d.year!, d.month!, d.day!) + file
    }

    /// The server only accepts letters, digits and `_ ( ) . + , ' = -` and space in a name.
    private static func clean(_ s: String) -> String {
        let ok = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 _().+,'=-")
        let out = String(s.map { ok.contains($0) ? $0 : "_" })
        return out.isEmpty ? "photo" : out
    }

    /// A name for files sent together, for `X-Batch: <id>/<count>`: when the server has all `count` of
    /// them it catalogues them at once instead of waiting for uploads to go quiet.
    static func newBatch() -> String {
        String(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12)).lowercased()
    }

    /// POST with the metadata the catalog needs as headers; the server writes the `.xmp` sidecar from them.
    static func make(asset: PHAsset, resource: PHAssetResource, batch: (id: String, count: Int)? = nil,
                     sha256: String? = nil, base: URL = baseURL) -> URLRequest {
        make(fields(asset: asset, resource: resource), batch: batch, sha256: sha256, base: base)
    }

    /// The same, from the fields. `sha256` (the in-app uploader, which has the file) lets the server refuse an
    /// opted-out photo before its body: the system uploader's jobs never have it (the extension reads no file).
    static func make(_ f: Fields, batch: (id: String, count: Int)? = nil, sha256: String? = nil, base: URL = baseURL) -> URLRequest {
        var req = URLRequest(url: base.appendingPathComponent("api/upload/" + path(f)))
        req.httpMethod = "POST"
        if let b = batch { req.setValue("\(b.id)/\(b.count)", forHTTPHeaderField: "X-Batch") }
        req.setValue(f.filename, forHTTPHeaderField: "X-Filename")
        if let d = f.created {
            let fmt = DateFormatter()
            fmt.locale = Locale(identifier: "en_US_POSIX")
            fmt.timeZone = TimeZone(identifier: "UTC")
            fmt.dateFormat = "yyyy-MM-dd'T'HH:mm:ss'Z'"
            req.setValue(fmt.string(from: d), forHTTPHeaderField: "X-Taken")
        }
        if let lat = f.latitude, let lon = f.longitude {
            req.setValue(String(lat), forHTTPHeaderField: "X-Latitude")
            req.setValue(String(lon), forHTTPHeaderField: "X-Longitude")
        }
        if f.favorite { req.setValue("true", forHTTPHeaderField: "X-Favorite") }
        if f.screenshot { req.setValue("1", forHTTPHeaderField: "X-Screenshot") }
        if let s = sha256 { req.setValue(s, forHTTPHeaderField: "X-Content-SHA256") }
        return req
    }

    /// What the server answered an upload: stored (201), already there (200), or refused by a tombstone (200 with
    /// X-Upload-Refused: the member opted out of these bytes, or the sysadmin purged them).
    enum Answer: Equatable { case stored, exists, refused(String), failed(Int) }

    static func answer(_ resp: HTTPURLResponse?) -> Answer {
        let code = resp?.statusCode ?? 0
        if code == 200, let why = resp?.value(forHTTPHeaderField: "X-Upload-Refused") { return .refused(why) }
        return code == 201 ? .stored : code == 200 ? .exists : .failed(code)
    }
}
