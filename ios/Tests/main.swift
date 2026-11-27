// The phone gate's logic (Shared/PhoneGate.swift), the upload request (Shared/UploadRequest.swift), and questions and
// stories (PhotoEstate/Gate/StoryModel.swift), on the Mac:
// no device, no Simulator, no photo library. `ios/Tests/run.sh` compiles them with this file and runs it.
import CryptoKit
import Foundation

var failures = 0, checks = 0

func check(_ ok: @autoclosure () -> Bool, _ what: String, line: Int = #line) {
    checks += 1
    if !ok() { failures += 1; print("FAIL line \(line): \(what)") }
}

func test(_ name: String, _ body: () throws -> Void) {
    do { try body() } catch { failures += 1; print("FAIL \(name): \(error)") }
}

let scratch = FileManager.default.temporaryDirectory.appendingPathComponent("gate-tests-\(UUID().uuidString)")
try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: scratch) }
GateStore.directory = scratch

test("hashing a file is the store's SHA-256") {
    let f = scratch.appendingPathComponent("a.bin")
    var bytes = Data(count: 3 << 20)                      // more than one read
    for i in 0..<bytes.count { bytes[i] = UInt8(i % 251) }
    try bytes.write(to: f)
    let want = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
    let got = try FileHash.sha256(f)
    check(got == want, "streamed hash matches")
    try Data("abc".utf8).write(to: f)
    let abc = try FileHash.sha256(f)
    check(abc == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "the known hash of abc")
}

test("everything: new photos are jobs at once, as before") {
    var s = GateState()
    s.policy = .everything
    check(Gate.route(["a", "b"], &s) == ["a", "b"], "both are jobs")
    check(s.candidates.isEmpty && s.shared.isEmpty, "nothing recorded for the owner's phone")
}

test("before the policy is known, and under review, new photos are candidates, never jobs") {
    var s = GateState()
    check(Gate.route(["a"], &s).isEmpty && s.candidates == ["a"], "unknown policy: a candidate")
    s.policy = .review
    check(Gate.route(["a", "b"], &s).isEmpty && s.candidates == ["a", "b"], "review: candidates, each once")
    Gate.me(member: 2, name: "Ben", policy: .everything, joined: nil, &s)
    check(s.candidates.isEmpty && s.approved == ["a", "b"], "everything after all: the candidates are approved")
    check(Gate.route([], &s) == ["a", "b"] && s.approved.isEmpty, "the extension takes the approved")
}

test("review: a batch, marks are opt-outs, applying approves the rest") {
    var s = GateState()
    s.policy = .review
    _ = Gate.route(["a", "b", "c"], &s)
    let b = Gate.gather(s.candidates, flagged: ["c": "a screenshot"], title: "3 new photos", &s, id: "B1")
    check(b?.assets == ["a", "b", "c"] && s.candidates.isEmpty && s.approved.isEmpty, "all three to look over, none sent")
    Gate.toggle("b", in: "B1", &s)
    check(s.batches[0].marked == ["b"] && s.batches[0].sharing == 2, "b marked")
    let got = Gate.apply("B1", hashes: ["b": ["h1", "h2"]], &s)
    check(got.share == ["a", "c"] && got.keep == ["b"], "shared and kept back")
    check(s.batches.isEmpty && s.approved == ["a", "c"] && s.optedOut == ["b": ["h1", "h2"]], "the batch is done")
    check(s.unsentAdd == ["h1", "h2"], "only the hashes go to the server")
    check(Gate.refused("H1", s) && !Gate.refused("h3", s), "a kept-back file is refused, by hash")
    check(Gate.route(["b"], &s) == ["a", "c"], "an opted-out photo is never a job, nor offered again")
    check(Gate.known(s).isSuperset(of: ["a", "b", "c"]) && s.candidates.isEmpty, "all three have their place")
    Gate.told(added: ["h1", "h2"], lifted: [], &s)
    check(s.unsentAdd.isEmpty && s.serverTombstones == ["h1", "h2"], "the server has them")
}

test("review flagged: only what may be private is asked about") {
    var s = GateState()
    s.policy = .review_flagged
    check(Gate.gather(["q", "r"], flagged: ["r": "text"], title: "t", &s, id: "F")?.assets == ["q", "r"] && s.approved.isEmpty,
          "before the first share's line has been shown, everything is asked about")
    s.batches = []
    s.told = true
    _ = Gate.route(["a", "b", "c"], &s)
    let b = Gate.gather(s.candidates, flagged: ["b": "text"], title: "t", &s)
    check(b?.assets == ["b"] && s.approved == ["a", "c"], "b to look over; a and c shared without asking")
    var t = GateState()
    t.policy = .review_flagged
    t.told = true
    check(Gate.gather(["x"], flagged: [:], title: "t", &t) == nil && t.approved == ["x"], "nothing flagged: no batch")
}

test("share after all lifts the tombstone, and the photo is sent once the server has heard the lift") {
    var s = GateState()
    s.policy = .review
    s.optedOut = ["a": ["h1"], "b": ["h2"], "c": []]
    s.serverTombstones = ["h1"]
    s.unsentAdd = ["h2"]
    Gate.shareAfterAll("a", &s)
    check(s.unsentLift == ["h1"] && !s.serverTombstones.contains("h1") && s.lifting == ["a": ["h1"]] && s.approved.isEmpty,
          "a: lifted, and waits for the server's answer")
    Gate.shareAfterAll("b", &s)
    check(s.unsentAdd.isEmpty && s.unsentLift == ["h1", "h2"] && s.lifting["b"] == ["h2"],
          "b: not told yet, but a POST may be carrying it: lifted too")
    check(!Gate.refused("h1", s) && !Gate.refused("h2", s), "neither is refused now")
    check(Gate.known(s).isSuperset(of: ["a", "b"]), "neither is offered again")
    Gate.told(added: [], lifted: ["h1"], &s)
    check(s.approved == ["a"] && s.lifting.keys.sorted() == ["b"], "a, lifted: approved")
    Gate.told(added: [], lifted: ["h2"], &s)
    check(s.approved == ["a", "b"] && s.lifting.isEmpty, "b too")
    Gate.shareAfterAll("c", &s)
    check(s.approved == ["a", "b", "c"], "c, with no hash: approved at once")
}

test("a new phone knows only the hashes; the uploader keeps such a photo back") {
    var s = GateState()
    s.policy = .review
    s.serverTombstones = ["h9"]
    s.approved = ["z"]
    check(Gate.refused("h9", s), "refused by its hash")
    Gate.keptBack("z", sha: "h9", &s)
    check(s.optedOut == ["z": ["h9"]] && s.approved.isEmpty, "kept back here from now on")
    Gate.sent("y", &s)
    check(s.shared.contains("y"), "a sent photo is never offered again")
}

test("a chunk of older photos leaves out what has a place, and joining is the first chunk") {
    let cal = Calendar(identifier: .gregorian)
    let joined = cal.date(from: DateComponents(year: 2026, month: 11, day: 22))!
    check(Gate.chunk(of: cal.date(from: DateComponents(year: 2026, month: 11, day: 23)), joined: joined, calendar: cal) == "since", "after joining")
    check(Gate.chunk(of: cal.date(from: DateComponents(year: 2019, month: 6, day: 1)), joined: joined, calendar: cal) == "2019", "a year")
    check(Gate.chunk(of: nil, joined: joined) == "undated", "no date")
    check(Gate.order(["2014", "since", "undated", "2019", "2026"]) == ["since", "2026", "2019", "2014", "undated"], "the order offered")
    var s = GateState()
    s.policy = .review
    s.shared = ["a"]
    s.optedOut = ["b": []]
    let batch = Gate.gather(["a", "b", "c", "c"], flagged: [:], title: "2019", &s)
    check(batch?.assets == ["c"], "only what the gate has no place for, once")
}

test("the state survives a round trip, and a file from an older build") {
    var s = GateState()
    s.policy = .review_flagged
    s.batches = [UploadBatch(id: "x", made: Date(timeIntervalSince1970: 0), title: "t", assets: ["a"], flagged: ["a": "text"], marked: ["a"])]
    s.optedOut = ["a": ["h"]]
    GateStore.update { $0 = s }
    check(GateStore.read() == s, "written and read back")
    try Data(#"{"policy":"review","candidates":["q"],"unknown":1}"#.utf8).write(to: GateStore.file)
    let old = GateStore.read()
    check(old.policy == .review && old.candidates == ["q"] && old.batches.isEmpty && !old.told, "missing fields have their defaults")
    check(old.pending.isEmpty && old.lifting.isEmpty, "pending and lifting too")
    s.pending = ["p"]
    s.lifting = ["l": ["h2"]]
    GateStore.update { $0 = s }
    check(GateStore.read() == s, "pending and lifting, written and read back")
    try Data(#"{"policy":"someday"}"#.utf8).write(to: GateStore.file)
    check(GateStore.read().policy == nil, "a policy this build does not know is not known")
    GateStore.erase()
    check(GateStore.read() == GateState(), "erased: a new phone")
}

test("a different member on this phone starts again") {
    var s = GateState()
    Gate.me(member: 2, name: "Ben", policy: .review, joined: nil, &s)
    s.optedOut = ["a": ["h"]]
    Gate.me(member: 2, name: "Ben", policy: .review_flagged, joined: nil, &s)
    check(s.optedOut.count == 1 && s.policy == .review_flagged, "the same member: kept")
    Gate.me(member: 3, name: "Cleo", policy: .review, joined: nil, &s)
    check(s.optedOut.isEmpty && s.member == 3, "another: started again")
}

test("the upload request: path, headers, the hash and the screenshot mark") {
    var f = UploadRequest.Fields(created: Date(timeIntervalSince1970: 1_763_139_751), filename: "IMG_0042.HEIC", localIdentifier: "ABC/L0/001")
    f.latitude = 47.6062; f.longitude = -122.3321; f.favorite = true; f.screenshot = true
    let base = URL(string: "http://127.0.0.1:8791")!
    let r = UploadRequest.make(f, batch: (id: "b1", count: 3), sha256: "ab" + String(repeating: "0", count: 62), base: base)
    let id = SHA256.hash(data: Data("ABC/L0/001".utf8)).prefix(4).map { String(format: "%02x", $0) }.joined().prefix(7)
    check(r.url?.absoluteString == "http://127.0.0.1:8791/api/upload/2025/11/14/IMG_0042-\(id).HEIC", "the path: \(r.url!)")
    check(r.httpMethod == "POST" && r.value(forHTTPHeaderField: "X-Batch") == "b1/3", "POST, batch")
    check(r.value(forHTTPHeaderField: "X-Taken") == "2025-11-14T17:02:31Z", "taken, UTC")
    check(r.value(forHTTPHeaderField: "X-Favorite") == "true" && r.value(forHTTPHeaderField: "X-Screenshot") == "1", "favourite, screenshot")
    check(r.value(forHTTPHeaderField: "X-Content-SHA256")?.hasPrefix("ab0") == true, "the hash")
    var plain = f
    plain.screenshot = false; plain.favorite = false
    let p = UploadRequest.make(plain, base: base)
    check(p.value(forHTTPHeaderField: "X-Screenshot") == nil && p.value(forHTTPHeaderField: "X-Content-SHA256") == nil
          && p.value(forHTTPHeaderField: "X-Batch") == nil, "an extension's job carries neither")
    check(UploadRequest.path(f) == UploadRequest.path(plain), "the same photo, the same path, whoever sends it")
}

test("what an upload's answer means") {
    let u = URL(string: "http://x/api/upload/a.jpg")!
    let resp = { (code: Int, h: [String: String]) in HTTPURLResponse(url: u, statusCode: code, httpVersion: nil, headerFields: h) }
    check(UploadRequest.answer(resp(201, [:])) == .stored, "201")
    check(UploadRequest.answer(resp(200, [:])) == .exists, "200")
    check(UploadRequest.answer(resp(200, ["X-Upload-Refused": "opted_out"])) == .refused("opted_out"), "refused")
    check(UploadRequest.answer(resp(409, [:])) == .failed(409) && UploadRequest.answer(nil) == .failed(0), "failures")
}

test("a question item, read: its group, who asks, the title") {
    let j: [String: Any] = ["items": [
        ["item": 9, "group_key": "Thanksgiving 1998", "questions": [
            ["id": 3, "text": "Who is this?", "why": "No one is named.", "photos": [["sha": "aa"], ["sha": "bb"]], "asked_at": "2026-11-23T10:00:00Z",
             "asked_by": ["id": 4, "name": "The estate's agent", "kind": "agent"], "for": "Ben", "to": "you"],
            ["id": 4, "text": "And this?", "photos": [["sha": "cc"]], "asked_by": ["id": 1, "name": "Ada", "kind": "person"], "to": "the family"]]],
        ["item": 8, "group_key": NSNull(), "questions": [["id": 2, "text": "Where?", "photos": [], "asked_by": ["id": 1, "name": "Ada", "kind": "person"], "to": "you"]]],
    ]]
    let items = QuestionItem.list(j)
    check(items.count == 2 && items[0].group == "Thanksgiving 1998" && items[1].group == "q2", "groups: \(items.map(\.group))")
    let q = items[0].questions[0]
    check(q.photos == ["aa", "bb"] && q.why == "No one is named." && q.askedAt != nil, "a question's photos, why, when")
    check(StoryText.askedBy(q) == "The estate’s agent asks you, for Ben", StoryText.askedBy(q))
    check(StoryText.askedBy(items[0].questions[1]) == "Ada asks the family", "a person asking the family")
    check(StoryText.heading(items[0]) == "2 questions about Thanksgiving 1998" && StoryText.heading(items[1]) == "A question for you"
          && StoryText.heading(nil) == "Nothing to answer here", "titles")
    check(StoryText.group(ref: "ask:q2") == "q2" && StoryText.group(ref: "album:3") == nil, "an inbox item's group")
}

test("a story, read: who and when, and the transcript or where it is") {
    let typed = Story(["id": 1, "told_by": ["id": 2, "name": "Ben"], "told_at": "2026-11-23T10:00:00Z", "text": "At the lake.", "photos": ["aa"]])
    check(typed != nil && StoryText.toldBy(typed!) == "Ben · Nov 23, 2026" && StoryText.transcript(typed!) == nil, "typed")
    var spoken = Story(["id": 2, "told_by": ["id": 3, "name": "Cleo"], "told_at": "2026-11-23T10:00:00Z", "photos": ["aa"],
                        "recording": ["url": "/api/stories/2/recording", "type": "audio/mp4"], "transcript_state": "waiting",
                        "question": ["id": 5, "text": "Who took it?"]])!
    check(spoken.waiting && spoken.recording == "/api/stories/2/recording" && spoken.question == "Who took it?", "spoken, waiting")
    check(StoryText.toldBy(spoken) == "Cleo · Nov 23, 2026 · spoken", StoryText.toldBy(spoken))
    check(StoryText.transcript(spoken)! == ("Being written down…", false), "waiting")
    spoken.transcriptState = "done"; spoken.transcript = "My brother took it."
    check(StoryText.transcript(spoken)! == ("My brother took it.", true) && !spoken.waiting, "written down")
    spoken.transcriptState = "failed"; spoken.transcript = nil
    check(StoryText.transcript(spoken)!.final == false, "failed: tried again")
    check(Story(["text": "no id"]) == nil, "no id, no story")
    check(StoryText.clock(0) == "0:00" && StoryText.clock(7.9) == "0:07" && StoryText.clock(754) == "12:34", "the clock")
}

test("the turns's table: the web's, every pair") {
    var legal = 0
    for a in TellState.allCases { for b in TellState.allCases where a.can(b) { legal += 1 } }
    check(legal == 19, "19 steps, as app/story-flow.js has: \(legal)")
    check(TellState.idle.can(.asking) && !TellState.idle.can(.recording), "the microphone is asked for first")
    check(TellState.recorded.can(.asking) && TellState.recorded.can(.sending) && !TellState.sending.can(.idle), "redo; sending ends sent or failed")
}

test("sending: an answer and a story, typed and spoken") {
    let base = URL(string: "http://127.0.0.1:8791")!
    let a = StorySend.text("At the lake.").request(base: base, question: 7, photo: nil)
    check(a.url?.absoluteString == "http://127.0.0.1:8791/api/questions/7/answer" && a.httpMethod == "POST", "an answer's address")
    check(a.value(forHTTPHeaderField: "Content-Type") == "application/json"
          && (try? JSONSerialization.jsonObject(with: a.httpBody!)) as? [String: String] == ["text": "At the lake."], "typed: {text}")
    let audio = Data([0, 0, 0, 0x20, 0x66, 0x74, 0x79, 0x70])
    let s = StorySend.recording(audio).request(base: base, question: 7, photo: "aa")
    check(s.url?.absoluteString == "http://127.0.0.1:8791/api/questions/7/answer" && s.httpBody == audio
          && s.value(forHTTPHeaderField: "Content-Type") == "audio/mp4", "spoken: the recording is the body")
    let t = StorySend.text("Grandma's garden").request(base: base, question: nil, photo: "bb")
    let tb = (try? JSONSerialization.jsonObject(with: t.httpBody!)) as? [String: Any]
    check(t.url?.absoluteString == "http://127.0.0.1:8791/api/stories" && tb?["photos"] as? [String] == ["bb"], "a story: its photo in the body")
    let r = StorySend.recording(audio).request(base: base, question: nil, photo: "bb")
    check(r.url?.absoluteString == "http://127.0.0.1:8791/api/stories?photos=bb" && r.httpBody == audio, "spoken: its photo in the query")
}

// MARK: specs/PhoneGate.tla's traces, replayed on the real Gate
//
// Each test below is a trace TLC printed for photo-estate/specs/PhoneGate.tla, run on the real PhoneGate.swift in the
// order the callers make their calls: GateModel (tellServer's read, its POST awaited, then Gate.told), the in-app
// uploader (ForegroundUploader: Gate.refused, the upload, Gate.keptBack or Gate.sent) and the extension
// (UploadExtension: Gate.route in one GateStore.update, then its jobs, which iOS sends with no hash). Those three need
// PhotoKit and cannot be compiled here, so their order of calls is written out below, as each makes them.

/// The server's side: this member's tombstones (private.change_tombstones) and the bytes it has (Admin.upload).
final class FakeServer {
    var tombs: Set<String> = []
    var got: Set<String> = []
    func post(_ t: Told) { tombs.formUnion(t.add); tombs.subtract(t.lift) }
    /// An upload of these bytes: stored, or refused by a tombstone (with the hash or without: a job's bytes are dropped).
    func upload(_ sha: String) -> Bool {
        if tombs.contains(sha) { return false }
        got.insert(sha)
        return true
    }
}

/// GateModel.tellServer, read: the unsent lists it POSTs (nil: nothing to say, it returns).
struct Told { var add: [String]; var lift: [String] }
func tellRead() -> Told? {
    let s = GateStore.read()
    return s.unsentAdd.isEmpty && s.unsentLift.isEmpty ? nil : Told(add: s.unsentAdd, lift: s.unsentLift)
}
/// ... and once its POST has been answered: Gate.told with the lists it read.
func told(_ t: Told) { GateStore.update { Gate.told(added: t.add, lifted: t.lift, &$0) } }
/// A whole tellServer, nothing else happening meanwhile.
func tellAll(_ server: FakeServer) { while let t = tellRead() { server.post(t); told(t) } }

/// ForegroundUploader.run for one photo of one file.
func upload(_ id: String, _ sha: String, _ server: FakeServer) {
    if Gate.held(id, GateStore.read()) { return }
    if Gate.refused(sha, GateStore.read()) { GateStore.update { Gate.keptBack(id, sha: sha, &$0) }; return }
    if server.upload(sha) { GateStore.update { Gate.sent(id, &$0) } } else { GateStore.update { Gate.keptBack(id, sha: sha, &$0) } }
}
/// ForegroundUploader.send(state.approved)
func sendApproved(_ sha: [String: String], _ server: FakeServer) { for id in GateStore.read().approved { upload(id, sha[id]!, server) } }

/// UploadExtension.run's Gate.route, in its GateStore.update: what it returns are the photos it makes jobs for.
func route(_ inserted: [String]) -> [String] { GateStore.update { Gate.route(inserted, &$0) } }

/// GateModel.apply: the hashes of the marked photos of the batch value the view holds, then Gate.apply.
func apply(_ batch: String, _ hashes: [String: [String]]) { GateStore.update { $0.told = true; Gate.apply(batch, hashes: hashes, &$0) } }

func fresh(_ policy: UploadPolicy?) {
    GateStore.erase()
    GateStore.update { $0.policy = policy; $0.member = 2 }
}

/// PhoneGate.tla's KeptBackIsTold: every photo kept back has its hashes, each with the server or waiting to be told.
func keptBackIsTold(_ server: FakeServer) -> Bool {
    let s = GateStore.read()
    return s.optedOut.values.allSatisfy { hs in !hs.isEmpty && hs.allSatisfy { server.tombs.contains($0) || s.unsentAdd.contains($0) } }
}

test("PhoneGate.tla KeptBackNeverSent: a photo marked in a batch is not sent once the policy is everything") {
    fresh(.review)
    GateStore.update { _ = Gate.gather(["a"], flagged: [:], title: "Since you joined", &$0, id: "B") }   // a chunk: new to the extension
    GateStore.update { Gate.me(member: 2, name: "Ben", policy: .everything, joined: nil, &$0) }         // a sysadmin sets everything
    GateStore.update { Gate.toggle("a", in: "B", &$0) }                                                // the member keeps it back
    let server = FakeServer()
    upload("a", "ha", server)                                                                         // "Upload recent photos"
    check(!server.got.contains("ha"), "the in-app uploader did not send it")
    check(!route(["a"]).contains("a"), "and the extension makes no job of it")
}

test("PhoneGate.tla ApprovedIsSent: the extension ended after Gate.route loses nothing") {
    fresh(nil)
    GateStore.update { Gate.me(member: 2, name: "Ben", policy: .everything, joined: nil, &$0) }
    check(route(["a", "b"]) == ["a", "b"], "new photos, under everything: jobs to make")
    // The token is saved; iOS ends the extension before its pending list is: the next run has nothing new.
    let next = route([])
    check(next.contains("a") && next.contains("b"), "the next run still makes their jobs: \(next)")
    fresh(.review)
    GateStore.update { $0.approved = ["c"] }
    check(route([]) == ["c"], "an approved photo, taken by the extension")
    let again = route([])                                       // ended before its pending list was saved
    check(again == ["c"] || GateStore.read().approved == ["c"], "it is still to be sent: \(again)")
}

test("PhoneGate.tla KeptBackIsTold: a photo kept back whose file could not be read is not kept back with no hash") {
    fresh(.review_flagged)
    GateStore.update { _ = Gate.gather(["a"], flagged: ["a": "text"], title: "1 new photo", &$0, id: "B") }
    GateStore.update { Gate.toggle("a", in: "B", &$0) }
    apply("B", ["a": []])                                       // hashes(of:): its original is in iCloud, and no network
    let server = FakeServer()
    tellAll(server)
    check(keptBackIsTold(server), "kept back with no hash to tell: \(GateStore.read().optedOut)")
    // And one marked while the others were hashed (the button is disabled, the photos are not).
    fresh(.review)
    GateStore.update { _ = Gate.gather(["a", "b"], flagged: [:], title: "2 new photos", &$0, id: "C") }
    GateStore.update { Gate.toggle("a", in: "C", &$0) }
    let handed = GateStore.read().batches[0]                    // the value ReviewView hands apply
    let hashes = Dictionary(uniqueKeysWithValues: handed.marked.map { ($0, ["h" + $0]) })   // await hashes(of:) for each
    GateStore.update { Gate.toggle("b", in: "C", &$0) }         // meanwhile, b is marked too
    apply("C", hashes)
    tellAll(server)
    check(keptBackIsTold(server), "b kept back with no hash: \(GateStore.read().optedOut)")
}

test("PhoneGate.tla ShareAfterAllStays: share after all while the opt-out's POST is under way") {
    fresh(.review)
    let server = FakeServer()
    GateStore.update { _ = Gate.gather(["a"], flagged: [:], title: "t", &$0, id: "B") }
    GateStore.update { Gate.toggle("a", in: "B", &$0) }
    apply("B", ["a": ["ha"]])
    let t = tellRead()!                                         // apply's tellServer: the POST of ha is on its way
    GateStore.update { Gate.shareAfterAll("a", &$0) }           // Share after all
    // (shareAfterAll's own tellServer: nothing to say before the fix; one at a time after it)
    server.post(t)                                              // the POST reaches the server
    sendApproved(["a": "ha"], server)                           // shareAfterAll's send(state.approved)
    told(t)
    tellAll(server)                                             // the next tellServer
    sendApproved(["a": "ha"], server)
    check(GateStore.read().optedOut["a"] == nil, "shared after all, it is kept back again")
    check(server.got.contains("ha"), "and never sent")
}

test("PhoneGate.tla ShareAfterAllStays: an answer of GET /api/tombstones read before a lift and written after it") {
    fresh(.review)
    let server = FakeServer()
    GateStore.update { _ = Gate.gather(["a"], flagged: [:], title: "t", &$0, id: "B") }
    GateStore.update { Gate.toggle("a", in: "B", &$0) }
    apply("B", ["a": ["ha"]])
    tellAll(server)                                             // the server holds ha
    // Before the fix, refresh's GET was its own, and shareAfterAll's lift went at once: answered before the lift, the
    // GET's answer was written after it, and ha was refused again. Now both take their turns (ServerTurns), one at a time.
    var turns = ServerTurns()
    check(turns.begin(read: true) && turns.round(), "refresh's tellServer(read: true): nothing to POST, then the GET")
    let answer = Array(server.tombs)                            // refresh's GET /api/tombstones is answered ...
    GateStore.update { Gate.shareAfterAll("a", &$0) }           // Share after all
    check(!turns.begin(read: false), "its tellServer is left to the one under way")
    GateStore.update { $0.serverTombstones = Set(answer) }      // ... and refresh writes its answer
    while turns.end() { _ = turns.round(); if let t = tellRead() { server.post(t); told(t) } }   // then the lift
    sendApproved(["a": "ha"], server)
    check(GateStore.read().optedOut["a"] == nil, "shared after all, it is kept back again")
    check(server.got.contains("ha"), "and never sent")
}

test("PhoneGate.tla ShareAfterAllStays: two POSTs of tellServer under way at once") {
    fresh(.review)
    let server = FakeServer()
    GateStore.update { _ = Gate.gather(["a"], flagged: [:], title: "t", &$0, id: "B") }
    GateStore.update { Gate.toggle("a", in: "B", &$0) }
    apply("B", ["a": ["ha"]])
    var turns = ServerTurns()
    check(turns.begin(read: false), "apply's tellServer")
    _ = turns.round()
    let t1 = tellRead()!                                        // the add of ha is on its way
    GateStore.update { Gate.shareAfterAll("a", &$0) }           // Share after all: the lift of ha
    if turns.begin(read: false) {                              // shareAfterAll's tellServer: were it to POST now, ...
        let t2 = tellRead()!
        server.post(t2); told(t2)                               // ... its lift could reach the server first
    }
    server.post(t1); told(t1)                                   // and the add after it
    while turns.end() { _ = turns.round(); if let t = tellRead() { server.post(t); told(t) } }
    sendApproved(["a": "ha"], server)
    check(GateStore.read().optedOut["a"] == nil && server.got.contains("ha"), "shared after all, and sent")
}

test("PhoneGate.tla KeptBackIsTold: a photo kept back while the lift of the same bytes is on its way") {
    fresh(.review)
    let server = FakeServer()
    GateStore.update { _ = Gate.gather(["a", "b"], flagged: [:], title: "t", &$0, id: "B") }   // a duplicate in Photos
    GateStore.update { Gate.toggle("b", in: "B", &$0) }
    GateStore.update { Gate.toggle("a", in: "B", &$0) }         // marked while b is hashed: it stays to look over
    apply("B", ["b": ["h"]])
    var turns = ServerTurns()
    check(turns.begin(read: false), "apply's tellServer")
    _ = turns.round()
    let t1 = tellRead()!                                        // the add of h is on its way
    GateStore.update { Gate.shareAfterAll("b", &$0) }           // b shared after all: the lift of h
    check(!turns.begin(read: false), "its tellServer is left to the one under way")
    server.post(t1); told(t1)
    check(turns.end(), "round again")
    _ = turns.round()
    let t2 = tellRead()!                                        // the lift of h is on its way
    apply("B", ["a": ["h"]])                                    // a, applied again, is kept back with the same bytes
    check(!turns.begin(read: false), "apply's tellServer is left to the one under way")
    server.post(t2); told(t2)
    while turns.end() { _ = turns.round(); if let t = tellRead() { server.post(t); told(t) } }
    check(keptBackIsTold(server), "a kept back, and the server does not refuse it: \(GateStore.read().optedOut), \(server.tombs)")
}

test("PhoneGate.tla KeptBackIsTold, ShareAfterAllStays: two photos with the same bytes, one shared after all") {
    fresh(.review)
    let server = FakeServer()
    GateStore.update { _ = Gate.gather(["a", "b"], flagged: [:], title: "t", &$0, id: "B") }   // a duplicate in Photos
    GateStore.update { Gate.toggle("a", in: "B", &$0); Gate.toggle("b", in: "B", &$0) }
    apply("B", ["a": ["h"], "b": ["h"]])
    tellAll(server)
    GateStore.update { Gate.shareAfterAll("a", &$0) }
    tellAll(server)
    sendApproved(["a": "h", "b": "h"], server)
    check(GateStore.read().optedOut["a"] == nil, "a, shared after all, is kept back again by b's hash")
    check(keptBackIsTold(server), "or b is kept back and the server no longer refuses its bytes: \(GateStore.read().optedOut), \(server.tombs)")
}

test("PhoneGate.tla KeptBackIsTold: kept back by the server while a lift of the same bytes is under way") {
    fresh(.everything)
    let server = FakeServer()
    server.tombs = ["h"]                                        // the other phone kept back these bytes
    upload("a", "h", server)                                    // "Upload recent photos": a is refused, kept back
    GateStore.update { Gate.shareAfterAll("a", &$0) }           // shared after all here: the lift waits to be sent
    upload("b", "h", server)                                    // the run goes on to b, a copy: the server still refuses it
    tellAll(server)                                             // the lift
    check(keptBackIsTold(server), "b kept back, and the server no longer refuses it: \(GateStore.read().optedOut), \(server.tombs)")
}

test("PhoneGate.tla ApprovedIsSent: shared after all while its opt-out's POST is under way, then a job") {
    fresh(.review)
    let server = FakeServer()
    GateStore.update { _ = Gate.gather(["a"], flagged: [:], title: "t", &$0, id: "B") }
    GateStore.update { Gate.toggle("a", in: "B", &$0) }
    apply("B", ["a": ["ha"]])
    let t = tellRead()!                                         // the POST of ha is on its way
    GateStore.update { Gate.shareAfterAll("a", &$0) }
    server.post(t)
    for id in route([]) { _ = server.upload(["a": "ha"][id]!) } // the extension makes its job; the system sends it
    told(t)
    tellAll(server)
    for id in route([]) { _ = server.upload(["a": "ha"][id]!) }
    sendApproved(["a": "ha"], server)
    check(server.got.contains("ha"), "approved, its job's bytes were dropped by the tombstone: lost")
}

print(failures == 0 ? "ok: \(checks) checks passed" : "\(failures) of \(checks) checks failed")
exit(failures == 0 ? 0 : 1)
