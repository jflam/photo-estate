import Foundation

enum Server {
    /// The server's address, from Info.plist (`SERVER_BASE` in project.yml).
    static var baseURL: URL {
        let s = Bundle.main.object(forInfoDictionaryKey: "BackgroundUploadURLBase") as? String ?? ""
        return URL(string: s) ?? URL(string: "https://home-compute.taila5abb.ts.net:8766")!
    }

    /// A member-only link to one photo in this estate (`/?photo=<sha>`).
    static func photoURL(_ sha: String) -> URL {
        var c = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        c?.path = "/"
        c?.queryItems = [URLQueryItem(name: "photo", value: sha)]
        return c?.url ?? baseURL
    }

    /// A debug build started with `-demoMember ben` (`xcrun simctl launch <device> com.jflam.photoestate -demoMember ben`)
    /// is that member of a demo server: every request, the web views' too, carries the `demo_member` cookie, which
    /// only a server started with `--demo` reads (photo_app.py). A session cookie, for that launch only; a release
    /// build, or a launch without the argument, has none, and through `tailscale serve` the login decides.
    static var demoCookie: HTTPCookie? {
        #if DEBUG
        guard let who = UserDefaults.standard.string(forKey: "demoMember"), !who.isEmpty, let host = baseURL.host() else { return nil }
        return HTTPCookie(properties: [.domain: host, .path: "/", .name: "demo_member", .value: who.lowercased()])
        #else
        return nil
        #endif
    }

    static func useDemoMember() {
        if let c = demoCookie { HTTPCookieStorage.shared.setCookie(c) }
    }
}
