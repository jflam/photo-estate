// The phone gate's logic (Shared/PhoneGate.swift) and the upload request (Shared/UploadRequest.swift), on the Mac:
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

test("share after all lifts the tombstone, or forgets one the server never heard of") {
    var s = GateState()
    s.policy = .review
    s.optedOut = ["a": ["h1"], "b": ["h2"]]
    s.serverTombstones = ["h1"]
    s.unsentAdd = ["h2"]
    Gate.shareAfterAll("a", &s)
    check(s.unsentLift == ["h1"] && !s.serverTombstones.contains("h1") && s.approved == ["a"], "a: lifted, and sent")
    Gate.shareAfterAll("b", &s)
    check(s.unsentAdd.isEmpty && s.unsentLift == ["h1"] && s.approved == ["a", "b"], "b: never told, so nothing to lift")
    check(!Gate.refused("h1", s) && !Gate.refused("h2", s), "neither is refused now")
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

print(failures == 0 ? "ok: \(checks) checks passed" : "\(failures) of \(checks) checks failed")
exit(failures == 0 ? 0 : 1)
