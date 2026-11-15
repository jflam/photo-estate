import Foundation

/// What the upload extension last did, left in an app group so the app can show it. iOS decides when
/// the extension runs, and without this there is no way to tell "never ran" from "ran and sent nothing".
enum ExtensionStatus {
    static let group = "group.com.jflam.photoestate"
    private static var defaults: UserDefaults? { UserDefaults(suiteName: group) }

    struct Entry: Codable {
        var at: Date
        var result: String        // completed / processing / failure
        var queued: Int           // upload jobs created in this run
        var note: String
    }

    /// Called by the extension at the end of each run. Keeps the last 20, newest first.
    static func record(_ result: String, queued: Int, note: String = "") {
        guard let d = defaults else { return }
        var all = history()
        all.insert(Entry(at: Date(), result: result, queued: queued, note: note), at: 0)
        d.set(try? JSONEncoder().encode(Array(all.prefix(20))), forKey: "history")
    }

    static func history() -> [Entry] {
        guard let data = defaults?.data(forKey: "history") else { return [] }
        return (try? JSONDecoder().decode([Entry].self, from: data)) ?? []
    }

    /// Also lets the app see the group is working at all.
    static var available: Bool { defaults != nil }
}
