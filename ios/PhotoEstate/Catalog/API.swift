import Foundation

/// What the server said when it would not do something: its own words when it gave any.
struct APIError: Error, CustomStringConvertible {
    let code: Int
    let description: String
    var notAllowed: Bool { code == 403 }
}

/// The server's JSON API, for what the phone's catalog cannot answer or may not decide: albums (made on
/// the server, by a person or an agent) and changes to the library (a favourite). Every call says whether
/// the server could be reached, so the app knows it is offline as soon as anything does.
enum API {
    private static let session: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 15
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    static func url(_ path: String, _ query: [URLQueryItem] = []) -> URL {
        var c = URLComponents(url: Server.baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty { c.queryItems = query }
        return c.url!
    }

    static func get(_ path: String, _ query: [URLQueryItem] = []) async throws -> [String: Any] {
        try await send(URLRequest(url: url(path, query)))
    }

    static func post(_ path: String, _ body: [String: Any] = [:]) async throws -> [String: Any] {
        var req = URLRequest(url: url(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        return try await send(req)
    }

    /// A request made elsewhere (a story's: StorySend), answered as JSON.
    static func json(_ req: URLRequest) async throws -> [String: Any] { try await send(req) }

    /// The bytes at a path (a story's recording, to play), or what the server said if it would not give them.
    static func data(_ path: String) async throws -> Data {
        let rel = path.hasPrefix("/") ? String(path.dropFirst()) : path
        let (data, resp) = try await session.data(for: URLRequest(url: url(rel)))
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard code / 100 == 2 else { throw APIError(code: code, description: "The server said \(code).") }
        return data
    }

    private static func send(_ req: URLRequest) async throws -> [String: Any] {
        let data: Data, resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            if error is URLError { await SyncEngine.shared.markOnline(false) }
            throw error
        }
        await SyncEngine.shared.markOnline(true)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard code / 100 == 2, let json else {
            throw APIError(code: code, description: json?["error"] as? String ?? "The server said \(code).")
        }
        return json
    }
}

/// Favourites set on the phone. The server is still the only writer of the catalog: it is asked first, and
/// only when it has agreed is the phone's copy changed to match, at once rather than at the next sync (which
/// brings the same row again).
enum Favourites {
    /// One photo. Throws what the server said if it would not have it.
    static func set(_ sha: String, _ on: Bool) async throws {
        _ = try await API.post("api/favorite", ["sha": sha, "fav": on])
        await keep([sha], on)
    }

    /// The server has made these favourites (or not): the phone's catalog now says so too.
    static func keep(_ shas: [String], _ on: Bool) async {
        try? await Catalog.shared.store.write { db in
            try db.transaction { for s in shas { try db.run("UPDATE photos SET fav=? WHERE sha=?", [on, s]) } }
        }
        await SyncEngine.shared.catalogEdited()
    }
}
