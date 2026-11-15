import Foundation

/// What the upload extension last did, left in an app group so the app can show it. iOS decides when
/// the extension runs, and without this there is no way to tell "never ran" from "ran and sent nothing".
enum ExtensionStatus {
    static let group = "group.com.jflam.photoestate"
    private static let key = "log"
    private static let keep = 50
    private static var defaults: UserDefaults? { UserDefaults(suiteName: group) }

    struct Entry: Codable {
        var at: Date              // the latest run this row stands for
        var since: Date           // the first of them
        var count: Int            // how many back-to-back runs did nothing new, merged into this row
        var result: String        // completed / processing / failure
        var queued: Int           // upload jobs created in this run
        var note: String

        /// A run that did nothing worth a row of its own.
        var isQuiet: Bool { result == "completed" && queued == 0 && note.isEmpty }
    }

    /// Called by the extension at the end of each run. Newest first, last 50 rows. Runs that did nothing
    /// are merged into the previous quiet row ("Nothing new ×7") so they cannot push real events out.
    static func record(_ result: String, queued: Int, note: String = "") {
        guard let d = defaults else { return }
        let now = Date()
        var all = history()
        let e = Entry(at: now, since: now, count: 1, result: result, queued: queued, note: note)
        if e.isQuiet, let last = all.first, last.isQuiet {
            all[0].at = now
            all[0].count += 1
        } else {
            all.insert(e, at: 0)
        }
        d.set(try? JSONEncoder().encode(Array(all.prefix(keep))), forKey: key)
    }

    static func history() -> [Entry] {
        guard let data = defaults?.data(forKey: key) else { return [] }
        return (try? JSONDecoder().decode([Entry].self, from: data)) ?? []
    }

    /// Also lets the app see the group is working at all.
    static var available: Bool { defaults != nil }
}
