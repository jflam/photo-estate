import AVFoundation
import SwiftUI

/// Telling a story, typed or spoken: answering a question in the Inbox (QuestionsView.swift), or "Tell a story" in
/// the viewer's details. The states are the web's (TellState, StoryModel.swift); the microphone is asked for only when
/// Record is pressed. A recording is AVAudioRecorder's AAC in an .m4a in the app's temporary folder, listened to and
/// redone before it is sent, and sent as the whole body of the request (`audio/mp4`); the file goes once it is sent
/// or thrown away.
@MainActor
final class Teller: ObservableObject {
    @Published private(set) var state: TellState = .idle
    @Published var text = ""
    @Published private(set) var problem: String?
    @Published private(set) var seconds: Double = 0
    @Published private(set) var playing = false

    private let send: (StorySend) async throws -> Story
    private var recorder: AVAudioRecorder?
    private var player: AVAudioPlayer?
    private var file: URL?
    private var timer: Timer?
    private var pending: StorySend?
    var onSent: (Story) -> Void = { _ in }

    init(send: @escaping (StorySend) async throws -> Story) { self.send = send }

    var typed: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
    /// Whether there is a recording to listen to, send, or send again.
    var hasClip: Bool { file != nil && state != .recording }

    @discardableResult private func go(_ to: TellState) -> Bool {
        guard state.can(to) else { assertionFailure("teller: no step from \(state) to \(to)"); return false }
        state = to
        return true
    }

    /// Ask for the microphone (the system asks the person the first time), and record.
    func record() async {
        guard state.can(.asking) else { return }
        stopPlaying()
        dropFile()
        problem = nil
        go(.asking)
        let allowed: Bool
        switch AVAudioApplication.shared.recordPermission {        // asked once: the system remembers the answer
        case .granted: allowed = true
        case .denied: allowed = false
        default: allowed = await AVAudioApplication.requestRecordPermission()
        }
        guard state == .asking else { return }                     // cancelled meanwhile
        guard allowed else {
            problem = "The microphone isn’t allowed for Photo Estate. Allow it in Settings, or type instead."
            go(.refused)
            return
        }
        do {
            // The session and the recorder are started off the main thread: starting the microphone can take a while
            // (and on a Simulator whose Mac has not allowed it, never ends), and the screen must not wait on it.
            let f = FileManager.default.temporaryDirectory.appendingPathComponent("story-\(UUID().uuidString).m4a")
            let r = try await Task.detached(priority: .userInitiated) { () throws -> AVAudioRecorder in
                let s = AVAudioSession.sharedInstance()
                try s.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker, .allowBluetoothHFP])
                try s.setActive(true)
                let r = try AVAudioRecorder(url: f, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 44_100,
                                                             AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 64_000,
                                                             AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue])
                guard r.record(forDuration: StorySend.longest) else { throw CocoaError(.featureUnsupported) }
                return r
            }.value
            guard state == .asking else {                              // cancelled while the microphone was starting
                r.stop(); r.deleteRecording()
                return
            }
            recorder = r
            file = f
            seconds = 0
            go(.recording)
            timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
                Task { @MainActor in self?.tick() }
            }
        } catch {
            guard state == .asking else { return }
            problem = "This iPhone can’t record just now. Type instead."
            go(.refused)
        }
    }

    private func tick() {
        guard state == .recording, let r = recorder else { return }
        if r.isRecording { seconds = r.currentTime } else { stop() }   // (it stops by itself at the longest)
    }

    /// Stop recording: the clip, to listen to before sending.
    func stop() {
        guard state == .recording, let r = recorder else { return }
        seconds = max(seconds, r.currentTime)
        r.stop()
        recorder = nil
        timer?.invalidate()
        timer = nil
        go(.recorded)
    }

    /// Stop asking or recording, and keep nothing.
    func cancel() {
        switch state {
        case .recording:
            recorder?.stop(); recorder?.deleteRecording(); recorder = nil
            timer?.invalidate(); timer = nil
            file = nil
            go(.idle)
        case .asking, .refused:
            go(.idle)
        default: break
        }
    }

    /// Throw the clip (or a failure) away.
    func discard() {
        guard state == .recorded || state == .failed || state == .sent else { return }
        stopPlaying()
        dropFile()
        pending = nil
        go(.idle)
    }

    func play() {
        guard state == .recorded || state == .failed, let f = file else { return }
        if playing { stopPlaying(); return }
        try? AVAudioSession.sharedInstance().setCategory(.playback)
        player = try? AVAudioPlayer(contentsOf: f)
        playing = player?.play() ?? false
        Task { [weak self] in
            while let p = self?.player, p.isPlaying { try? await Task.sleep(for: .milliseconds(200)) }
            self?.playing = false
        }
    }

    private func stopPlaying() { player?.stop(); player = nil; playing = false }

    private func dropFile() {
        if let f = file { try? FileManager.default.removeItem(at: f) }
        file = nil
    }

    /// Send: the clip when there is one, else what is typed.
    func submit() async {
        if state == .recorded, let f = file, let d = try? Data(contentsOf: f) {
            stopPlaying()
            await sendNow(.recording(d))
        } else if state == .idle, !typed.isEmpty {
            await sendNow(.text(typed))
        }
    }

    /// Send again what failed (typed text as it is in the field now).
    func retry() async {
        guard state == .failed, let p = pending else { return }
        if case .text = p, !typed.isEmpty { await sendNow(.text(typed)) } else { await sendNow(p) }
    }

    private func sendNow(_ what: StorySend) async {
        pending = what
        problem = nil
        go(.sending)
        do {
            let s = try await send(what)
            go(.sent)
            text = ""
            dropFile()
            pending = nil
            onSent(s)
        } catch {
            problem = "Couldn’t send it: " + ((error as? APIError)?.description ?? "your server isn’t reachable.")
            go(.failed)
        }
    }

    func again() { if state == .sent { go(.idle) } }
}

/// The teller on screen. `dismiss` is a button that puts the question away ("I don’t know"), or closes the teller.
struct TellView: View {
    @ObservedObject var teller: Teller
    @FocusState private var typingNow: Bool
    var prompt = "Type it, or record it"
    var sendLabel = "Send"
    var dismiss: String?
    var onDismiss: () -> Void = {}

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            switch teller.state {
            case .asking:
                HStack { ProgressView(); Text("Waiting for the microphone…").font(.callout.weight(.semibold)); Spacer()
                    Button("Cancel") { teller.cancel() }.buttonStyle(.bordered) }
            case .refused:
                Text(teller.problem ?? "").font(.footnote).foregroundStyle(.red)
                HStack {
                    Button("Try again") { Task { await teller.record() } }.buttonStyle(.bordered)
                    Spacer()
                    Button("Type instead") { teller.cancel() }.buttonStyle(.borderedProminent)
                }
            case .recording:
                HStack(spacing: 10) {
                    Circle().fill(.red).frame(width: 10, height: 10)
                    Text("Recording").font(.callout.weight(.semibold))
                    Text(StoryText.clock(teller.seconds)).font(.callout.monospacedDigit()).foregroundStyle(.secondary)
                    Spacer()
                    Button("Cancel") { teller.cancel() }.buttonStyle(.bordered)
                    Button("Stop") { Haptics.tap(); teller.stop() }.buttonStyle(.borderedProminent)
                }
            case .sent:
                Label("Told. Thank you.", systemImage: "checkmark.circle.fill").foregroundStyle(Color.sun)
            case .idle, .recorded, .sending, .failed:
                if teller.hasClip { clip } else { typing }
            }
        }
        .controlSize(.small)
        .tint(.sun)
    }

    /// Typing (and a typed answer being sent, or that failed).
    @ViewBuilder private var typing: some View {
        TextField(prompt, text: $teller.text, axis: .vertical)
            .lineLimit(3...8)
            .padding(12)
            .background(Color.well, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .disabled(teller.state == .sending)
            .focused($typingNow)
            .toolbar {                                              // a field of several lines: Return is a new line, so Done puts the keyboard away
                if typingNow { ToolbarItemGroup(placement: .keyboard) { Spacer(); Button("Done") { typingNow = false } } }
            }
        if let p = teller.problem { Text(p).font(.footnote).foregroundStyle(.red) }
        HStack {
            Button { Haptics.tap(); typingNow = false; Task { await teller.record() } } label: { Label("Record", systemImage: "mic.fill") }
                .buttonStyle(.bordered).disabled(teller.state == .sending)
            Spacer()
            dismissButton
            if teller.state == .failed {
                Button("Try again") { Task { await teller.retry() } }.buttonStyle(.borderedProminent)
            } else {
                Button(teller.state == .sending ? "Sending…" : sendLabel) { typingNow = false; Task { await teller.submit() } }
                    .buttonStyle(.borderedProminent).disabled(teller.typed.isEmpty || teller.state == .sending)
            }
        }
    }

    /// The recording: listen to it, redo it, throw it away, send it.
    @ViewBuilder private var clip: some View {
        HStack(spacing: 12) {
            Button { teller.play() } label: {
                Image(systemName: teller.playing ? "pause.fill" : "play.fill").frame(width: 40, height: 40)
                    .background(Color.well, in: Circle())
            }
            .accessibilityLabel(teller.playing ? "Pause your recording" : "Listen to your recording")
            Text("Your recording").font(.callout.weight(.semibold))
            Text(StoryText.clock(teller.seconds)).font(.callout.monospacedDigit()).foregroundStyle(.secondary)
            Spacer()
        }
        if let p = teller.problem { Text(p).font(.footnote).foregroundStyle(.red) }
        HStack {
            if teller.state == .recorded { Button("Redo") { Task { await teller.record() } }.buttonStyle(.bordered) }
            if teller.state != .sending { Button("Discard") { teller.discard() }.buttonStyle(.bordered) }
            Spacer()
            if teller.state == .recorded { dismissButton }
            if teller.state == .failed {
                Button("Try again") { Task { await teller.retry() } }.buttonStyle(.borderedProminent)
            } else {
                Button(teller.state == .sending ? "Sending…" : sendLabel) { Task { await teller.submit() } }
                    .buttonStyle(.borderedProminent).disabled(teller.state == .sending)
            }
        }
    }

    @ViewBuilder private var dismissButton: some View {
        if let dismiss { Button(dismiss) { onDismiss() }.buttonStyle(.bordered) }
    }
}

/// One story, as the viewer's details and the Inbox show it: who told it and when, the words or the transcript (or
/// where it is), and its recording to play.
struct StoryRow: View {
    let story: Story
    var showQuestion = true
    @ObservedObject private var player = StoryPlayer.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(StoryText.toldBy(story)).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            if showQuestion, let q = story.question { Text("Answering “\(q)”").font(.footnote).foregroundStyle(.secondary) }
            if let t = story.text { Text(t).font(.callout) }
            if let t = StoryText.transcript(story) {
                Text(t.text).font(.callout).italic(!t.final).foregroundStyle(t.final ? .primary : .secondary)
            }
            if let url = story.recording {
                Button { player.toggle(story.id, url) } label: {
                    Label(player.playing == story.id ? "Pause" : player.loading == story.id ? "Loading…" : "Play the recording",
                          systemImage: player.playing == story.id ? "pause.circle.fill" : "play.circle.fill")
                        .font(.callout.weight(.semibold))
                }
                .buttonStyle(.plain).foregroundStyle(Color.sun).padding(.top, 3)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Color.well, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }
}

/// One recording playing at a time: fetched from the server (the member's own request, so the server decides who
/// may hear it) and played from memory.
@MainActor
final class StoryPlayer: NSObject, ObservableObject, AVAudioPlayerDelegate {
    static let shared = StoryPlayer()
    @Published private(set) var playing: Int?
    @Published private(set) var loading: Int?
    private var player: AVAudioPlayer?

    func toggle(_ id: Int, _ url: String) {
        if playing == id { stop(); return }
        stop()
        loading = id
        Task {
            defer { if loading == id { loading = nil } }
            guard let data = try? await API.data(url), loading == id else { return }
            try? AVAudioSession.sharedInstance().setCategory(.playback)
            try? AVAudioSession.sharedInstance().setActive(true)
            player = try? AVAudioPlayer(data: data)
            player?.delegate = self
            if player?.play() == true { playing = id }
        }
    }

    func stop() { player?.stop(); player = nil; playing = nil }

    nonisolated func audioPlayerDidFinishPlaying(_ p: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in self.playing = nil }
    }
}

/// Ask for a story again, gently, until its transcript is written down (or failed): every few seconds, less often as
/// it goes on, for at most ten minutes, while the screen asking is up (the task is cancelled with it).
enum StoryFollow {
    static func until(_ story: Story, update: @MainActor (Story) -> Void) async {
        guard story.waiting, let sha = story.photos.first else { return }
        var wait = 3.0
        let end = Date().addingTimeInterval(600)
        while Date() < end, !Task.isCancelled {
            try? await Task.sleep(for: .seconds(wait))
            wait = min(wait * 2, 20)
            guard let j = try? await API.get("api/photos/\(sha)/stories"),
                  let s = (j["stories"] as? [Any] ?? []).compactMap(Story.init).first(where: { $0.id == story.id }) else { continue }
            await update(s)
            if !s.waiting { return }
        }
    }
}
