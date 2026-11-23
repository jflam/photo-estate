import Foundation

/// Questions and stories (multiplayer phase 7, stories.py on the server), as the phone reads and sends them: plain
/// values and functions, with no UIKit, so `ios/Tests/run.sh` checks them on the Mac. The screens are
/// QuestionsView.swift and Teller.swift; the words are the web pages' (app/story-flow.js, app/ask-flow.js).
///
/// A story is sent typed (JSON `{text}`) or spoken: the recording is the whole body of the request, `audio/mp4`
/// (AVAudioRecorder's AAC in an .m4a). An answer goes to `POST /api/questions/<id>/answer`, a story told from the
/// viewer to `POST /api/stories` (typed: `{photos: [sha], text}`; spoken: `?photos=<sha>`).

/// A story as `GET /api/photos/<sha>/stories` lists one.
struct Story: Identifiable, Equatable {
    var id: Int
    var teller: String
    var toldAt: Date?
    var text: String?
    var question: String?
    var photos: [String]
    var recording: String?          // its address on the server, to play
    var transcript: String?
    var transcriptState: String?    // nil (typed), waiting, done, failed

    init?(_ j: Any?) {
        guard let d = j as? [String: Any], let id = (d["id"] as? NSNumber)?.intValue else { return nil }
        self.id = id
        teller = (d["told_by"] as? [String: Any])?["name"] as? String ?? "Someone"
        toldAt = (d["told_at"] as? String).flatMap(StoryText.date)
        text = d["text"] as? String
        question = (d["question"] as? [String: Any])?["text"] as? String
        photos = d["photos"] as? [String] ?? []
        recording = (d["recording"] as? [String: Any])?["url"] as? String
        transcript = d["transcript"] as? String
        transcriptState = d["transcript_state"] as? String
    }

    var waiting: Bool { recording != nil && transcriptState == "waiting" }
}

/// A question as `GET /api/questions` lists it, and the inbox item it is dealt in.
struct Question: Identifiable, Equatable {
    var id: Int
    var text: String
    var why: String?
    var photos: [String]            // shas
    var askedAt: Date?
    var asker: String
    var askerIsAgent: Bool
    var forWhom: String?
    var toYou: Bool

    init?(_ j: Any?) {
        guard let d = j as? [String: Any], let id = (d["id"] as? NSNumber)?.intValue else { return nil }
        self.id = id
        text = d["text"] as? String ?? ""
        why = d["why"] as? String
        photos = (d["photos"] as? [[String: Any]] ?? []).compactMap { $0["sha"] as? String }
        askedAt = (d["asked_at"] as? String).flatMap(StoryText.date)
        let by = d["asked_by"] as? [String: Any]
        asker = by?["name"] as? String ?? "Someone"
        askerIsAgent = by?["kind"] as? String == "agent"
        forWhom = d["for"] as? String
        toYou = d["to"] as? String == "you"
    }
}

struct QuestionItem: Equatable {
    var item: Int
    var groupKey: String?
    var questions: [Question]

    init?(_ j: Any?) {
        guard let d = j as? [String: Any], let item = (d["item"] as? NSNumber)?.intValue else { return nil }
        self.item = item
        groupKey = d["group_key"] as? String
        questions = (d["questions"] as? [Any] ?? []).compactMap(Question.init)
    }

    /// The group this item is: its key, or `q<id>` for a question asked on its own (as the inbox item's ref says).
    var group: String { groupKey ?? "q\(questions.map(\.id).min() ?? 0)" }

    static func list(_ j: [String: Any]) -> [QuestionItem] { (j["items"] as? [Any] ?? []).compactMap(QuestionItem.init) }
}

enum StoryText {
    /// Who asks, and whom: "Ada asks you", "The estate’s agent asks the family, for Ben".
    static func askedBy(_ q: Question) -> String {
        let who = q.askerIsAgent ? "The estate’s agent" : q.asker
        let whom = q.toYou ? "you" : "the family"
        return "\(who) asks \(whom)" + (q.askerIsAgent ? ", for \(q.forWhom ?? "the family")" : "")
    }

    /// The title of a question item.
    static func heading(_ item: QuestionItem?) -> String {
        guard let item else { return "Nothing to answer here" }
        let n = item.questions.count
        return n == 1 ? "A question for you" : "\(n) questions" + (item.groupKey.map { " about \($0)" } ?? "")
    }

    /// What a spoken story's words are now: the transcript, or where it is. nil for a typed one.
    static func transcript(_ s: Story) -> (text: String, final: Bool)? {
        guard s.recording != nil else { return nil }
        switch s.transcriptState {
        case "done": return (s.transcript ?? "(nothing could be heard in it)", true)
        case "failed": return ("Not written down yet: it will be tried again.", false)
        default: return ("Being written down…", false)
        }
    }

    /// "Ben · Nov 23, 2026 · spoken"
    static func toldBy(_ s: Story) -> String {
        [s.teller, s.toldAt.map(day) ?? "", s.recording != nil ? "spoken" : ""].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    static func day(_ d: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US")
        f.dateFormat = "MMM d, yyyy"
        return f.string(from: d)
    }

    /// A recording's length as a clock: "0:07", "12:34".
    static func clock(_ seconds: Double) -> String {
        let t = max(0, Int(seconds.rounded(.down)))
        return "\(t / 60):" + String(format: "%02d", t % 60)
    }

    static func date(_ s: String) -> Date? { ISO8601DateFormatter().date(from: s) }

    /// The group an inbox item's ref names (`ask:<group>`), or nil for an item that is no question.
    static func group(ref: String) -> String? { ref.hasPrefix("ask:") ? String(ref.dropFirst(4)) : nil }
}

/// The teller's states, as the web's (app/story-flow.js): a strict table, so a step it does not list is a bug.
enum TellState: String, CaseIterable {
    case idle, asking, refused, recording, recorded, sending, sent, failed

    static let steps: [TellState: Set<TellState>] = [
        .idle: [.asking, .sending],
        .asking: [.recording, .refused, .idle],
        .refused: [.asking, .idle],
        .recording: [.recorded, .failed, .idle],
        .recorded: [.asking, .sending, .idle],
        .sending: [.sent, .failed],
        .sent: [.idle],
        .failed: [.sending, .asking, .idle],
    ]

    func can(_ to: TellState) -> Bool { Self.steps[self]?.contains(to) ?? false }
}

/// What is sent, and where.
enum StorySend {
    case text(String)
    case recording(Data)

    static let audioType = "audio/mp4"           // AVAudioRecorder's AAC .m4a: a type the server takes (stories.AUDIO)
    static let longest: TimeInterval = 15 * 60   // a recording stops by itself then (well inside the server's 8 MiB)

    /// The request that answers question `qid` (or, with `qid` nil, tells a story about `photo`).
    func request(base: URL, question qid: Int?, photo: String?) -> URLRequest {
        var c = URLComponents(url: base.appendingPathComponent(qid.map { "api/questions/\($0)/answer" } ?? "api/stories"),
                              resolvingAgainstBaseURL: false)!
        if qid == nil, case .recording = self, let photo { c.queryItems = [URLQueryItem(name: "photos", value: photo)] }
        var r = URLRequest(url: c.url!)
        r.httpMethod = "POST"
        switch self {
        case .text(let t):
            var body: [String: Any] = ["text": t]
            if qid == nil, let photo { body["photos"] = [photo] }
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try? JSONSerialization.data(withJSONObject: body)
        case .recording(let d):
            r.setValue(Self.audioType, forHTTPHeaderField: "Content-Type")
            r.httpBody = d
        }
        return r
    }
}
