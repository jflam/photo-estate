import SwiftUI

/// A question item of the Inbox, opened: its questions (`GET /api/questions`, the item whose group the inbox item's
/// ref names), each with its photos, who asks and why, answered typed or spoken (Teller.swift), or put away with
/// "I don’t know". A question answered stays, with the answer under it; a spoken one says it is being written down
/// until the mini has. The web's page for the same is /inbox?ask=<group> (inbox.html).
struct QuestionsView: View {
    let group: String
    var onDone: () -> Void = {}
    @State private var item: QuestionItem?
    @State private var loaded = false
    @State private var problem: String?
    @State private var answered: [Int: Story] = [:]
    @State private var gone: Set<Int> = []
    @State private var looking: String?

    private var questions: [Question] { (item?.questions ?? []).filter { !gone.contains($0.id) } }
    private var waiting: Int { questions.filter { answered[$0.id] == nil }.count }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if !loaded {
                    ProgressView().frame(maxWidth: .infinity).padding(.top, 60)
                } else if let problem {
                    ContentUnavailableView("The questions could not be read", systemImage: "exclamationmark.bubble", description: Text(problem))
                } else if item == nil && answered.isEmpty {
                    ContentUnavailableView("Nothing to answer here", systemImage: "checkmark.circle",
                                           description: Text("These questions are answered, put away, or not yours."))
                } else {
                    Text("Answer in your own words, typed or spoken. Your answer is kept as a story about these photos, with your name and the day.")
                        .font(.callout).foregroundStyle(.secondary)
                    ForEach(questions) { q in
                        QuestionCard(question: q, answer: answered[q.id], look: { looking = $0 },
                                     onAnswered: { answered[q.id] = $0; if waiting == 0 { onDone() } },
                                     onGone: { gone.insert(q.id); if waiting == 0 { onDone() } })
                    }
                    if waiting == 0 {
                        Label(answered.isEmpty ? "Nothing left to answer here." : "That is all of them. Thank you.", systemImage: "checkmark.circle")
                            .foregroundStyle(.secondary).padding(.top, 6)
                    }
                }
            }
            .padding(16)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(Color.paper)
        .navigationTitle(StoryText.heading(item))
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
        .sheet(item: Binding(get: { looking.map(Look.init) }, set: { looking = $0?.sha })) { PhotoLook(sha: $0.sha) }
        .tint(.sun)
    }

    private func load() async {
        do {
            let items = QuestionItem.list(try await API.get("api/questions"))
            item = items.first { $0.group == group }
            problem = nil
        } catch {
            problem = (error as? APIError)?.description ?? "Your server isn’t reachable."
        }
        loaded = true
    }
}

private struct Look: Identifiable { let sha: String; var id: String { sha } }

/// One question: who asks, the question and why, its photos, and the teller (or the answer, once it is given).
private struct QuestionCard: View {
    let question: Question
    let answer: Story?
    var look: (String) -> Void
    var onAnswered: (Story) -> Void
    var onGone: () -> Void
    @StateObject private var teller: Teller
    @State private var story: Story?
    @State private var busy = false
    @State private var note: String?

    init(question: Question, answer: Story?, look: @escaping (String) -> Void, onAnswered: @escaping (Story) -> Void, onGone: @escaping () -> Void) {
        self.question = question
        self.answer = answer
        self.look = look
        self.onAnswered = onAnswered
        self.onGone = onGone
        let qid = question.id
        _teller = StateObject(wrappedValue: Teller { what in
            guard let s = Story(try await API.json(what.request(base: Server.baseURL, question: qid, photo: nil))) else {
                throw APIError(code: 0, description: "The server’s answer could not be read.")
            }
            return s
        })
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text([StoryText.askedBy(question), question.askedAt.map(StoryText.day) ?? ""].filter { !$0.isEmpty }.joined(separator: " · "))
                .font(.footnote).foregroundStyle(.secondary)
            Text(question.text).font(.title(19, .bold))
            if let why = question.why { Text(why).font(.callout).foregroundStyle(.secondary) }
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(question.photos, id: \.self) { sha in
                        Button { look(sha) } label: {
                            Thumb(sha: sha).frame(width: 96, height: 96).clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                        }
                        .buttonStyle(.plain).accessibilityLabel("Look at this photo")
                    }
                }
            }
            if let s = story ?? answer {
                Text("YOUR ANSWER").font(.caption2.weight(.semibold)).kerning(1).foregroundStyle(.secondary).padding(.top, 4)
                StoryRow(story: s, showQuestion: false)
                    .task(id: s.id) { await StoryFollow.until(s) { story = $0 } }
            } else {
                TellView(teller: teller, prompt: "Your answer: type it, or record it", sendLabel: "Send answer", dismiss: "I don’t know") {
                    dontKnow()
                }
                .disabled(busy)
                if let note { Text(note).font(.footnote).foregroundStyle(.red) }
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.well, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .onAppear { teller.onSent = { s in story = s; onAnswered(s) } }
    }

    private func dontKnow() {
        busy = true
        note = nil
        Task {
            defer { busy = false }
            do {
                _ = try await API.post("api/questions/\(question.id)/dismiss")
                Haptics.tap()
                onGone()
            } catch {
                note = (error as? APIError)?.description ?? "Your server isn’t reachable."
            }
        }
    }
}

/// A photo of a question, bigger.
struct PhotoLook: View {
    let sha: String
    @Environment(\.dismiss) private var dismiss
    @State private var image: UIImage?

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if let image { Image(uiImage: image).resizable().scaledToFit() } else { ProgressView().tint(.white) }
        }
        .overlay(alignment: .topTrailing) {
            Button { dismiss() } label: { Image(systemName: "xmark").modifier(GlassCircle()) }
                .foregroundStyle(.white).padding(16).accessibilityLabel("Close")
        }
        .task { image = await ImageStore.shared.image(.preview, sha) }
    }
}
