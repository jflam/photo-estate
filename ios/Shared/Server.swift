import Foundation

enum Server {
    /// The server's address, from Info.plist (`SERVER_BASE` in project.yml).
    static var baseURL: URL {
        let s = Bundle.main.object(forInfoDictionaryKey: "BackgroundUploadURLBase") as? String ?? ""
        return URL(string: s) ?? URL(string: "https://johns-mac-mini.taila5abb.ts.net:8766")!
    }
}
