import SwiftUI

/// A photo private to its sharer (multiplayer phase 5, private.py), as the viewer shows it: the private mark,
/// and for a photo this member shared, withdrawing it from the family and releasing it again. The words are the
/// web viewer's (app/private-flow.js). What it is comes from `GET /api/photos/<sha>/why`'s `private`; a change is
/// `POST /api/private {shas: [sha], private}`, which answers with the line as it is now.
struct PrivateLine: Equatable {
    var sharer: Bool                 // this member shared it, and so may withdraw it
    var why: String?                 // private to them: new | held | withdrawn; nil when it is not
    var since: String?
    var until: String?
    var others: Int                  // other sharers who do not hold it private (while any do, the family sees it)

    var isPrivate: Bool { why != nil }

    init?(_ j: Any?) {
        guard let p = j as? [String: Any] else { return nil }
        sharer = p["sharer"] as? Bool ?? false
        let mine = p["mine"] as? [String: Any]
        why = mine?["why"] as? String
        since = mine?["since"] as? String
        until = mine?["until"] as? String
        others = (p["others_sharing"] as? NSNumber)?.intValue ?? 0
    }

    var says: String {
        let reason = why == "held" ? "held, as it looks like a receipt, a screenshot or a document"
                   : why == "withdrawn" ? "you withdrew it" : "it is being checked"
        let day = since.flatMap { GateModel.iso($0) }.map { "Since \($0.formatted(date: .abbreviated, time: .omitted)): " } ?? ""
        let also = others > 0 ? ". \(others) other member\(others == 1 ? "" : "s") shared it too, so the family still sees it" : ""
        return "\(day)\(reason)\(also)."
    }

    static func load(_ sha: String) async -> PrivateLine? {
        PrivateLine((try? await API.get("api/photos/\(sha)/why"))?["private"])
    }

    /// Withdraw (true) or release (false). Throws what the server said.
    static func change(_ sha: String, _ on: Bool) async throws -> PrivateLine? {
        PrivateLine(try await API.post("api/private", ["shas": [sha], "private": on])["private"])
    }
}

/// The details' row: the private line and Release, or for a photo the member shared, Withdraw from the family.
struct PrivateRow: View {
    let sha: String
    @Binding var line: PrivateLine?
    @State private var busy = false
    @State private var note: String?

    var body: some View {
        if let p = line, p.isPrivate || p.sharer {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .top, spacing: 14) {
                    Image(systemName: p.isPrivate ? "lock.fill" : "person.2").font(.system(size: 17)).foregroundStyle(Color.sunLit).frame(width: 24)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(p.isPrivate ? "Private to you" : "Shared by you").font(.title(17))
                        Text(p.isPrivate ? p.says : "Withdraw it, and only you will see it. You can release it again at any time.")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
                Button(p.isPrivate ? "Release" : "Withdraw from the family") { act(!p.isPrivate) }
                    .fontWeight(.semibold).disabled(busy).padding(.leading, 38)
                if let note { Text(note).font(.footnote).foregroundStyle(Color.sunLit).padding(.leading, 38) }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.well, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        }
    }

    private func act(_ on: Bool) {
        busy = true
        note = nil
        Haptics.tap()
        Task {
            defer { busy = false }
            do {
                line = try await PrivateLine.change(sha, on)
                note = on ? "Withdrawn: only you see it now. Release it here at any time." : "Released: the family sees it again."
            } catch {
                note = (error as? APIError)?.description ?? "Your server isn’t reachable."
            }
        }
    }
}
