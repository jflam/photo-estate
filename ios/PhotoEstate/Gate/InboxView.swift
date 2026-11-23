import Photos
import SwiftUI

/// The Inbox tab: what waits for this member to decide, in one list. The phone's own batches (photos to look
/// over before they are shared: the phone gate, PhoneGate.swift), the mini's items (`GET /api/inbox`), each
/// opening the page that decides it, and the older photos offered a chunk at a time.
struct InboxView: View {
    @ObservedObject private var gate = GateModel.shared
    @ObservedObject private var uploader = ForegroundUploader.shared

    var body: some View {
        NavigationStack {
            List {
                if let me = gate.me {
                    Section {
                        LabeledContent("You are", value: me.name)
                        Text(me.policyText).font(.footnote).foregroundStyle(.secondary)
                    }
                }
                if gate.gated {
                    Section {
                        if gate.state.batches.isEmpty {
                            Text("Nothing to look over. New photos come here before anything is shared.").foregroundStyle(.secondary)
                        }
                        ForEach(gate.state.batches.reversed()) { b in
                            NavigationLink { ReviewView(batchID: b.id) } label: { BatchRow(batch: b) }
                        }
                        if let w = gate.working { Label(w, systemImage: "hourglass").foregroundStyle(.secondary) }
                        if uploader.running || uploader.total > 0 { Text(uploader.status).font(.footnote).foregroundStyle(.secondary) }
                    } header: { Text("On this iPhone") } footer: {
                        if let p = gate.problem { Text(p) }
                    }
                }
                Section("From your library") {
                    if gate.items.isEmpty { Text("Nothing waiting for you.").foregroundStyle(.secondary) }
                    ForEach(gate.items) { item in
                        NavigationLink {
                            page(item).onAppear { gate.opened(item) }
                        } label: {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(item.kindText.uppercased()).font(.caption2.weight(.semibold)).kerning(1).foregroundStyle(Color.sun)
                                Text(item.title)
                                if let w = item.when { Text(w.formatted(.relative(presentation: .named))).font(.caption).foregroundStyle(.secondary) }
                            }
                        }
                    }
                }
                if gate.gated && !gate.chunks.isEmpty {
                    Section {
                        ForEach(gate.chunks) { c in
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(c.title).font(.title(17))
                                    Text("\(c.ids.count) photo\(c.ids.count == 1 ? "" : "s")").font(.footnote).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Button("Skip") { gate.skip(c) }.buttonStyle(.borderless).foregroundStyle(.secondary)
                                Button("Share…") { Task { await gate.share(c) } }.buttonStyle(.borderless).fontWeight(.semibold)
                                    .padding(.leading, 12)
                            }
                        }
                    } header: { Text("Older photos") } footer: {
                        Text("Photos taken before you joined are offered a year at a time. Share… puts them here to look over first; Skip leaves them on this iPhone and offers them no more.")
                    }
                }
                if gate.gated {
                    Section {
                        NavigationLink { KeptBackView() } label: {
                            LabeledContent("Kept back on this iPhone", value: "\(gate.state.optedOut.count)")
                        }
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .background(Color.paper)
            .navigationTitle("Inbox")
            .refreshable { await gate.refresh() }
        }
        .tint(.sun)
    }

    private func page(_ item: GateModel.Item) -> some View {
        let url = URL(string: item.opens ?? "/inbox", relativeTo: Server.baseURL)?.absoluteURL ?? Server.baseURL
        return WebView(url: url).ignoresSafeArea(edges: .bottom)
            .navigationTitle(item.kindText).navigationBarTitleDisplayMode(.inline)
    }
}

private struct BatchRow: View {
    let batch: UploadBatch

    var body: some View {
        HStack(spacing: 12) {
            HStack(spacing: -14) {
                ForEach(batch.assets.prefix(3), id: \.self) { id in
                    LocalThumb(id: id).frame(width: 40, height: 40).clipShape(RoundedRectangle(cornerRadius: 7))
                        .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(Color.paper, lineWidth: 2))
                }
            }
            VStack(alignment: .leading, spacing: 2) {
                Text("TO LOOK OVER").font(.caption2.weight(.semibold)).kerning(1).foregroundStyle(Color.sun)
                Text(batch.title)
                Text(batch.marked.isEmpty ? "Nothing sent yet" : "\(batch.marked.count) kept back so far").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

/// One batch, reviewed as a change set is: everything is shared unless marked; a mark keeps a photo back.
struct ReviewView: View {
    let batchID: String
    @ObservedObject private var gate = GateModel.shared
    @Environment(\.dismiss) private var dismiss
    @State private var asking = false

    private var batch: UploadBatch? { gate.state.batches.first { $0.id == batchID } }

    var body: some View {
        Group {
            if let b = batch {
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(gate.state.told && gate.state.policy == .review_flagged
                             ? "These may be private (a screenshot, a document, text). Everything else was shared. Tap a photo to keep it back."
                             : "Everything here is shared with the family unless you tap it to keep it back. What you keep back stays on this iPhone and is never sent.")
                            .font(.callout).foregroundStyle(.secondary)
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 104), spacing: 4)], spacing: 4) {
                            ForEach(b.assets, id: \.self) { id in
                                Button { Haptics.tick(); gate.toggle(id, in: b.id) } label: { cell(id, b) }
                                    .buttonStyle(.plain)
                                    .accessibilityLabel(b.marked.contains(id) ? "Kept back" : "Shared")
                                    .accessibilityHint(b.flagged[id] ?? "")
                            }
                        }
                    }
                    .padding(16)
                }
                .safeAreaInset(edge: .bottom) {
                    Button { if gate.state.told { go(b) } else { asking = true } } label: {
                        Text(b.marked.isEmpty ? "Share all \(b.assets.count)" : "Share \(b.sharing), keep \(b.marked.count) back")
                            .fontWeight(.semibold).frame(maxWidth: .infinity).padding(.vertical, 6)
                    }
                    .buttonStyle(.glassProminent).padding(16).disabled(gate.working != nil)
                }
                .navigationTitle(b.title).navigationBarTitleDisplayMode(.inline)
                .alert("Before you share", isPresented: $asking) {
                    Button("Share") { go(b) }
                    Button("Not yet", role: .cancel) {}
                } message: {
                    Text("Shared photos become part of the family estate. You can withdraw one from view later, but not erase it.")
                }
            } else {
                ContentUnavailableView("Done", systemImage: "checkmark.circle", description: Text("This batch has been shared."))
            }
        }
        .background(Color.paper)
    }

    private func go(_ b: UploadBatch) {
        Task { await gate.apply(b); dismiss() }
    }

    private func cell(_ id: String, _ b: UploadBatch) -> some View {
        let off = b.marked.contains(id)
        return LocalThumb(id: id)
            .aspectRatio(1, contentMode: .fill)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .opacity(off ? 0.35 : 1)
            .overlay(alignment: .bottomLeading) {
                if off {
                    Label("Kept back", systemImage: "eye.slash.fill").font(.caption2.weight(.semibold))
                        .padding(.horizontal, 7).padding(.vertical, 4).background(.black.opacity(0.6), in: Capsule())
                        .foregroundStyle(.white).padding(5)
                }
            }
            .overlay(alignment: .topTrailing) {
                if b.flagged[id] != nil {
                    Image(systemName: "text.viewfinder").font(.caption.weight(.bold)).padding(5)
                        .background(Color.sunLit, in: Circle()).foregroundStyle(.black).padding(5)
                }
            }
    }
}

/// The photos this member kept back on this iPhone, and the one way to take that back: share after all.
struct KeptBackView: View {
    @ObservedObject private var gate = GateModel.shared

    var body: some View {
        List {
            let ids = gate.state.optedOut.keys.sorted()
            if ids.isEmpty { Text("Nothing kept back.").foregroundStyle(.secondary) }
            ForEach(ids, id: \.self) { id in
                HStack(spacing: 12) {
                    LocalThumb(id: id).frame(width: 56, height: 56).clipShape(RoundedRectangle(cornerRadius: 8))
                    Text((gate.state.optedOut[id] ?? []).isEmpty ? "Not sent: on this iPhone only" : "Not sent, and refused from your other phones")
                        .font(.footnote).foregroundStyle(.secondary)
                    Spacer()
                    Button("Share after all") { Task { await gate.shareAfterAll(id) } }.buttonStyle(.borderless).fontWeight(.semibold)
                }
            }
        }
        .scrollContentBackground(.hidden)
        .background(Color.paper)
        .navigationTitle("Kept back")
        .navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .bottom) {
            Text("A photo kept back is never sent from this iPhone, and your server refuses it from any phone of yours. Only you can take that back, here.")
                .font(.footnote).foregroundStyle(.secondary).padding(16)
        }
    }
}

/// A photo of this iPhone's Photos library, by identifier, small.
struct LocalThumb: View {
    let id: String
    @State private var image: UIImage?

    var body: some View {
        Rectangle().fill(Color.well)
            .overlay { if let image { Image(uiImage: image).resizable().scaledToFill() } }
            .clipped()
            .task(id: id) {
                guard let a = PHAsset.fetchAssets(withLocalIdentifiers: [id], options: nil).firstObject else { return }
                let o = PHImageRequestOptions()
                o.deliveryMode = .highQualityFormat
                o.isNetworkAccessAllowed = true
                o.resizeMode = .fast
                image = await withCheckedContinuation { k in
                    PHImageManager.default().requestImage(for: a, targetSize: CGSize(width: 240, height: 240), contentMode: .aspectFill, options: o) { img, _ in
                        k.resume(returning: img)
                    }
                }
            }
    }
}
