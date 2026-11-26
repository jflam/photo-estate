import AVFoundation
import Combine
import MapKit
import SwiftUI
import UIKit

/// One photo, full screen. It grows out of its tile and goes back into it; between photos you swipe;
/// pinch or double-tap to look closer; flick up for what is known about it. It shows whatever is on
/// the phone at once (the thumbnail, then the full-screen copy if it has been kept) and sharpens from
/// the server when it can. A video plays from the server when its page is up; a Live Photo's clip plays
/// while the photo is held (Movie.swift).
struct ViewerScreen: View {
    @ObservedObject var session: ViewerSession
    var lookup: @MainActor (String) async -> Photo?     // what the list it came from knows of a photo
    var reason: (String) -> String? = { _ in nil }      // why a photo is in the album it was opened from
    @ObservedObject private var sync = SyncEngine.shared
    var route: (Query) -> Void              // a link out of the viewer: show these photos instead

    @State private var chrome = true
    @State private var info: Photo?           // the photo whose details are showing
    @State private var photo: Photo?
    @State private var sharing = false
    @State private var stack: [Tile] = []     // the photos of this one's stack, best first; empty for a photo on its own
    @State private var picked: Tile?          // the one of them showing, when it is not the one on top
    @State private var favs: [String: Bool] = [:]     // favourites set here, which the list this came from has not heard of
    @State private var saving: Set<String> = []
    @State private var note: String?          // why a favourite (or a video's file) could not be had
    @State private var playback = Playback()  // the video that is up, for its controls; not observed here, only by them
    @State private var priv: PrivateLine?     // whether the photo is private to this member, or theirs to withdraw (Private.swift)

    private var isFav: Bool { favs[tile.sha] ?? tile.fav }

    /// The photo on screen: the list's own, or the one picked from its stack.
    private var tile: Tile { picked ?? session.tile }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            PagerView(session: session, lookup: lookup, playback: playback,
                      onTap: { withAnimation(.easeOut(duration: 0.2)) { chrome.toggle() } },
                      onInfo: { info = photo }, onZoomedIn: { withAnimation(.easeOut(duration: 0.2)) { chrome = false } }, visible: chrome,
                      shown: picked, shownAt: session.index,
                      top: AnyView(caption.environment(\.colorScheme, .dark)),
                      bottom: AnyView(VStack(spacing: 2) { strip; controls; actions }.environment(\.colorScheme, .dark).tint(.sunLit)))
                .ignoresSafeArea()
        }
        .task(id: session.index) {
            // Another photo of the list: its own stack, from the top. Left and right go from stack to stack;
            // the strip is how to look through one.
            picked = nil
            stack = []
            let top = session.tile
            note = nil
            let p = await lookup(top.sha)
            photo = p
            if top.stack > 1, let id = p?.stackOf {
                let members = await Catalog.shared.stackTiles(id)
                if !Task.isCancelled, session.tile.sha == top.sha, members.count > 1 { stack = members }
            }
        }
        .task(id: picked?.sha) { photo = await lookup(tile.sha) }
        .task(id: tile.sha) {
            priv = nil
            guard !tile.isLocal else { return }
            let sha = tile.sha, got = await PrivateLine.load(sha)
            if !Task.isCancelled, tile.sha == sha { priv = got }
        }
        .sheet(item: $info) { shown in
            InfoView(photo: shown, reason: reason(shown.sha), priv: $priv) { q in
                info = nil
                session.close()
                route(q)
            }
            .presentationDetents([.medium, .large])
            .presentationBackground(.regularMaterial)
            .presentationCornerRadius(28)
        }
        .statusBarHidden(!chrome)
        .environment(\.colorScheme, .dark)
        .tint(.sunLit)
    }

    // what and when, as a caption above the print
    private var caption: some View {
        HStack(alignment: .top) {
            Button { session.close() } label: { Image(systemName: "xmark").modifier(GlassCircle()) }
                .accessibilityLabel("Close")
            Spacer()
            VStack(spacing: 3) {
                Text(Dates.long(tile.when)).font(.title(17))
                Text([Dates.clock(tile.when), tile.city?.split(separator: ",").first.map(String.init) ?? "", tile.isLocal ? "Not yet on the server" : "", sync.online ? "" : "Offline"]
                        .filter { !$0.isEmpty }.joined(separator: "  ·  "))
                    .font(.caption).opacity(0.75)
                if let note { Text(note).font(.caption.weight(.medium)).foregroundStyle(Color.sunLit) }
                if priv?.isPrivate == true {
                    Label("Private to you", systemImage: "lock.fill")
                        .font(.caption2.weight(.semibold))
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(.black.opacity(0.35), in: Capsule())
                        .padding(.top, 3)
                }
                if tile.live && !tile.video {
                    Label("LIVE", systemImage: "livephoto")
                        .font(.caption2.weight(.semibold)).kerning(1)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(.black.opacity(0.35), in: Capsule())
                        .padding(.top, 3)
                        .accessibilityLabel("Live Photo")
                        .accessibilityHint("Touch and hold the photo to play it")
                }
            }
            .multilineTextAlignment(.center)
            .shadow(color: .black.opacity(0.6), radius: 6)
            .padding(.top, 5)
            Spacer()
            // A favourite is the server's to keep, so a photo it does not have yet has no heart.
            Button { favour() } label: {
                Image(systemName: isFav ? "heart.fill" : "heart").foregroundStyle(isFav ? Color.sunLit : Color.white)
                    .contentTransition(.symbolEffect(.replace)).modifier(GlassCircle())
            }
            .accessibilityLabel("Favourite").accessibilityAddTraits(isFav ? .isSelected : [])
            .opacity(tile.isLocal ? 0 : 1).disabled(tile.isLocal)
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 14)
        .padding(.top, 6)
        .padding(.bottom, 10)
    }

    /// The stack this photo is on top of, as a row of small prints: tap one to look at it. Slide the row
    /// when it is longer than the screen.
    @ViewBuilder private var strip: some View {
        if stack.count > 1 {
            ScrollViewReader { reader in
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 5) {
                        ForEach(Array(stack.enumerated()), id: \.element.sha) { i, t in
                            let on = t.sha == tile.sha
                            Button {
                                Haptics.tick()
                                picked = t.sha == session.tile.sha ? nil : t
                                withAnimation(.snappy(duration: 0.2)) { reader.scrollTo(t.sha, anchor: .center) }
                            } label: {
                                Thumb(sha: t.sha)
                                    .frame(width: on ? 46 : 38, height: 52)
                                    .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
                                    .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(.white, lineWidth: on ? 2 : 0))
                                    .opacity(on ? 1 : 0.7)
                            }
                            .buttonStyle(.plain)
                            .id(t.sha)
                            .accessibilityLabel("Shot \(i + 1) of \(stack.count)" + (i == 0 ? ", the best" : ""))
                            .accessibilityAddTraits(on ? .isSelected : [])
                        }
                    }
                    .padding(.horizontal, 14)
                    .frame(minWidth: UIScreen.main.bounds.width)      // a short stack sits in the middle
                }
                .frame(width: UIScreen.main.bounds.width, height: 58)
                .animation(.snappy(duration: 0.2), value: tile.sha)
            }
        }
    }

    /// A video's play button and playhead, above the actions.
    @ViewBuilder private var controls: some View {
        if tile.video { VideoBar(playback: playback, sha: tile.sha, dur: tile.dur).padding(.bottom, 4) }
    }

    private var actions: some View {
        HStack(spacing: 4) {
            action(sharing ? "ellipsis" : "square.and.arrow.up", "Share") { share() }
            action("info.circle", "Details") { info = photo }
            if !tile.isLocal {        // comparing looks needs the server to have seen the photo
                action("sparkles", "More like this") {
                    session.close()
                    route(Query(similar: tile.sha))
                }
            }
        }
        .padding(.horizontal, 6)
        .glassEffect(.regular.interactive(), in: .capsule)
        .foregroundStyle(.white)
        .padding(8)
    }

    /// A favourite: shown at once, then said to the server; if the server will not have it, taken back.
    private func favour() {
        let sha = tile.sha, on = !isFav
        guard !saving.contains(sha) else { return }
        saving.insert(sha)
        note = nil
        Haptics.tap()
        favs[sha] = on
        Task {
            defer { saving.remove(sha) }
            do { try await Favourites.set(sha, on) } catch {
                favs[sha] = !on
                if tile.sha == sha {
                    note = (error as? APIError)?.notAllowed == true ? "Favourites can’t be changed from this account"
                         : error is URLError ? "Favourites need your server, which isn’t reachable" : "Couldn’t save the favourite"
                }
            }
        }
    }

    private func action(_ symbol: String, _ label: String, _ run: @escaping () -> Void) -> some View {
        Button(action: run) {
            Image(systemName: symbol).font(.system(size: 19, weight: .medium)).frame(width: 62, height: 50).contentShape(Rectangle())
        }
        .accessibilityLabel(label)
    }

    /// Share the original file when the server can be reached (so Save Image keeps its date and place),
    /// else the best copy on the phone. A video is shared as its file, fetched from the server (Save Video
    /// keeps it a video); with no server there is nothing to share, and the caption says so. A Live Photo
    /// is shared as its still alone.
    private func share() {
        guard !sharing else { return }
        sharing = true
        note = nil
        let sha = tile.sha, video = tile.video, name = photo?.filename ?? "\(sha.prefix(12))\(video ? ".mp4" : ".jpg")"
        Task {
            defer { sharing = false }
            let dir = FileManager.default.temporaryDirectory.appendingPathComponent("share-\(sha.prefix(8))", isDirectory: true)
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            var file: URL?
            if tile.isLocal { file = await LocalPhotos.shared.original(sha) }
            var req = URLRequest(url: video ? Server.videoFile(sha) : Server.baseURL.appendingPathComponent("original/\(sha)"))
            req.timeoutInterval = 20
            if file == nil, !tile.isLocal, let (tmp, resp) = try? await URLSession.shared.download(for: req), (resp as? HTTPURLResponse)?.statusCode == 200 {
                let dest = dir.appendingPathComponent(name)
                try? FileManager.default.removeItem(at: dest)
                if (try? FileManager.default.moveItem(at: tmp, to: dest)) != nil { file = dest }
            }
            if file == nil && video {
                if tile.sha == sha { note = "Videos are shared from your server, which isn’t reachable" }
                return
            }
            if file == nil { file = await ImageStore.shared.fetch(.preview, sha) }
            if file == nil, let data = await ImageStore.shared.thumbData(sha, network: false) {
                let dest = dir.appendingPathComponent("\(sha.prefix(12)).jpg")
                if (try? data.write(to: dest)) != nil { file = dest }
            }
            guard let file, let top = UIApplication.shared.topController else { return }
            let sheet = UIActivityViewController(activityItems: [file], applicationActivities: nil)
            sheet.popoverPresentationController?.sourceView = top.view
            top.present(sheet, animated: true)
        }
    }
}

extension UIApplication {
    var topController: UIViewController? {
        var top = connectedScenes.compactMap { ($0 as? UIWindowScene)?.keyWindow }.first?.rootViewController
        while let next = top?.presentedViewController { top = next }
        return top
    }
}

// MARK: details

/// What is known about a photo, laid out like the back of a print: when, where, who, and how it was
/// taken. Everything that names a set of photos is a way into that set.
struct InfoView: View {
    let photo: Photo
    var reason: String? = nil       // why it is in the album it was opened from
    @Binding var priv: PrivateLine?  // private to this member, or theirs to withdraw
    var go: (Query) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(Dates.long(photo.when)).font(.title(26, .bold))
                    Text([Dates.clock(photo.when), photo.tz ?? ""].filter { !$0.isEmpty }.joined(separator: "  ·  "))
                        .font(.subheadline).foregroundStyle(.secondary)
                    if let d = photo.desc { Text(d).font(.callout).italic().foregroundStyle(.secondary).padding(.top, 6) }
                    if let reason {
                        Label("In this album: \(reason)", systemImage: "rectangle.stack")
                            .font(.footnote).foregroundStyle(.secondary).padding(.top, 6)
                    }
                    if LocalPhotos.id(photo.sha) != nil {
                        Label("On this iPhone, not yet on your server. Places, people and the rest come once it is.", systemImage: "arrow.up.circle")
                            .font(.footnote).foregroundStyle(.secondary).padding(.top, 6)
                    }
                }
                PrivateRow(sha: photo.sha, line: $priv)
                if photo.video {
                    fact("play.rectangle", "Video", photo.dur.map { Tile.clock(Float($0)) } ?? "")
                } else if photo.live != nil {
                    fact("livephoto", "Live Photo", "Press and hold the photo to play it")
                }
                if photo.place != nil || photo.lat != nil { place }
                if photo.event != nil || photo.trip != nil {
                    VStack(spacing: 0) {
                        if let e = photo.event {
                            link("calendar.day.timeline.left", photo.eventTitle ?? "That day", "Everything taken around then") { go(Query(event: e)) }
                        }
                        if photo.event != nil && photo.trip != nil { Divider().padding(.leading, 52) }
                        if let t = photo.trip {
                            link("airplane", (photo.tripTitle ?? "This trip").replacingOccurrences(of: "Trip to ", with: ""), "The whole trip") { go(Query(picks: [.trip: [String(t)]])) }
                        }
                    }
                    .background(Color.well, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                }
                if let s = photo.stackOf, photo.stackN > 1 {
                    link("square.stack", "Stack of \(photo.stackN)", "Near-identical shots taken together") { go(Query(stack: s)) }
                        .background(Color.well, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                }
                if !photo.people.isEmpty { chips("With", photo.people, .person) }
                if !photo.albums.isEmpty { chips("In", photo.albums, .album) }
                if LocalPhotos.id(photo.sha) == nil { StoriesSection(sha: photo.sha) }
                if photo.camera != nil || !exposure.isEmpty { camera }
                Text([photo.filename, photo.w.flatMap { w in photo.h.map { "\(w) × \($0)" } } ?? ""].filter { !$0.isEmpty }.joined(separator: "  ·  "))
                    .font(.caption).foregroundStyle(.tertiary).textSelection(.enabled)
            }
            .padding(.horizontal, 20)
            .padding(.top, 30)
            .padding(.bottom, 24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollDismissesKeyboard(.interactively)
        .environment(\.colorScheme, .dark)
        .tint(.sunLit)
    }

    private var place: some View {
        Button { if let c = photo.city { go(Query(picks: [.city: [c]])) } } label: {
            VStack(alignment: .leading, spacing: 0) {
                if let lat = photo.lat, let lon = photo.lon {
                    let at = CLLocationCoordinate2D(latitude: lat, longitude: lon)
                    Map(initialPosition: .region(MKCoordinateRegion(center: at, latitudinalMeters: 6000, longitudinalMeters: 6000)), interactionModes: []) {
                        Marker("", coordinate: at).tint(Color.sunLit)
                    }
                    .frame(height: 140)
                    .allowsHitTesting(false)
                }
                if let name = photo.place {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(name).font(.title(17)).foregroundStyle(.primary).multilineTextAlignment(.leading)
                            let sub = [photo.county ?? "", photo.placeInferred ? "from photos taken around then" : ""].filter { !$0.isEmpty }.joined(separator: "  ·  ")
                            if !sub.isEmpty { Text(sub).font(.footnote).foregroundStyle(.secondary) }
                        }
                        Spacer()
                        if photo.city != nil { Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary) }
                    }
                    .padding(14)
                }
            }
            .background(Color.well)
            .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        }
        .buttonStyle(.plain)
        .disabled(photo.city == nil)
    }

    /// Something said of the photo that leads nowhere: that it is a video and how long, or a Live Photo.
    private func fact(_ symbol: String, _ title: String, _ detail: String) -> some View {
        HStack(spacing: 14) {
            Image(systemName: symbol).font(.system(size: 17)).foregroundStyle(Color.sunLit).frame(width: 24)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.title(17)).foregroundStyle(.primary)
                if !detail.isEmpty { Text(detail).font(.footnote).foregroundStyle(.secondary).monospacedDigit() }
            }
            Spacer()
        }
        .padding(14)
        .background(Color.well, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .accessibilityElement(children: .combine)
    }

    private func link(_ symbol: String, _ title: String, _ detail: String, _ run: @escaping () -> Void) -> some View {
        Button(action: run) {
            HStack(spacing: 14) {
                Image(systemName: symbol).font(.system(size: 17)).foregroundStyle(Color.sunLit).frame(width: 24)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.title(17)).foregroundStyle(.primary).multilineTextAlignment(.leading)
                    Text(detail).font(.footnote).foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
            }
            .padding(14)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func chips(_ label: String, _ names: [String], _ facet: Facet) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(label.uppercased()).font(.caption2.weight(.semibold)).kerning(1.2).foregroundStyle(.secondary)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(names, id: \.self) { name in
                        Button(name) { go(Query(picks: [facet: [name]])) }
                            .font(.subheadline.weight(.medium))
                            .padding(.horizontal, 14).padding(.vertical, 8)
                            .background(Color.well, in: Capsule())
                            .foregroundStyle(.primary)
                    }
                }
            }
            .scrollClipDisabled()
        }
    }

    /// How it was taken, as the plate on a camera: the body and lens, then the four numbers.
    private var camera: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let name = photo.camera {
                Button { go(Query(picks: [.camera: [name]])) } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(name).font(.title(17)).foregroundStyle(.primary)
                            if let lens = photo.lens { Text(lens).font(.footnote).foregroundStyle(.secondary) }
                        }
                        Spacer()
                        Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            if !exposure.isEmpty {
                HStack(spacing: 0) {
                    ForEach(Array(exposure.enumerated()), id: \.offset) { i, e in
                        if i > 0 { Divider().frame(height: 26) }
                        VStack(spacing: 3) {
                            Text(e.value).font(.system(.callout, design: .monospaced).weight(.medium))
                            Text(e.label.uppercased()).font(.system(size: 9, weight: .semibold)).kerning(1).foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity)
                    }
                }
            }
        }
        .padding(14)
        .background(Color.well, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
    }

    private var exposure: [(label: String, value: String)] {
        var out: [(String, String)] = []
        let n = { (x: Double) in x.formatted(.number.precision(.fractionLength(0...1))) }
        if let f = photo.focal35 ?? photo.focal { out.append(("Focal", "\(n(f)) mm")) }
        if let a = photo.fnumber { out.append(("Aperture", "ƒ/\(n(a))")) }
        if let e = photo.exposure, e > 0 { out.append(("Shutter", e < 1 ? "1/\(Int((1 / e).rounded()))" : "\(n(e))s")) }
        if let i = photo.iso { out.append(("ISO", "\(Int(i))")) }
        return out
    }
}

// MARK: paging and zooming (UIKit)

struct PagerView: UIViewControllerRepresentable {
    let session: ViewerSession
    var lookup: @MainActor (String) async -> Photo?
    var playback: Playback
    var onTap: () -> Void
    var onInfo: () -> Void
    var onZoomedIn: () -> Void  // looking closer: the caption and controls get out of the way
    var visible: Bool           // the caption and the controls; a tap on the photo hides them
    var shown: Tile? = nil      // a photo of the stack to show in place of the one on top
    var shownAt = 0             // ...of the photo at this place in the list
    var top: AnyView
    var bottom: AnyView

    func makeUIViewController(context: Context) -> PagerController {
        let c = PagerController(tiles: session.tiles, index: session.index, lookup: lookup, playback: playback)
        c.onIndex = { [session] i in DispatchQueue.main.async { session.index = i; Haptics.tick() } }
        c.onZoom = { [session] z in
            if z && !session.zoomed { context.coordinator.zoomedIn() }
            session.zoomed = z
        }
        return c
    }

    final class Coordinator { var zoomedIn: () -> Void = {} }
    func makeCoordinator() -> Coordinator { Coordinator() }

    func updateUIViewController(_ c: PagerController, context: Context) {
        context.coordinator.zoomedIn = onZoomedIn
        c.onTap = onTap
        c.onInfo = onInfo
        c.top.rootView = top
        c.bottom.rootView = bottom
        c.setChrome(visible)
        c.show(shown, at: shownAt)
    }
}

final class PagerController: UIPageViewController, UIPageViewControllerDataSource, UIPageViewControllerDelegate, UIGestureRecognizerDelegate {
    var onIndex: (Int) -> Void = { _ in }
    var onTap: () -> Void = {}
    var onZoom: (Bool) -> Void = { _ in }
    var onInfo: () -> Void = {}
    // Two small bars, not one sheet over the photo: whatever they don't cover belongs to the photo's gestures.
    let top = UIHostingController(rootView: AnyView(EmptyView()))
    let bottom = UIHostingController(rootView: AnyView(EmptyView()))
    private let tiles: [Tile]
    private let lookup: @MainActor (String) async -> Photo?
    private let playback: Playback

    init(tiles: [Tile], index: Int, lookup: @escaping @MainActor (String) async -> Photo?, playback: Playback) {
        self.tiles = tiles
        self.lookup = lookup
        self.playback = playback
        super.init(transitionStyle: .scroll, navigationOrientation: .horizontal, options: [.interPageSpacing: 20])
        dataSource = self
        delegate = self
        setViewControllers([page(index)!], direction: .forward, animated: false)
    }

    required init?(coder: NSCoder) { fatalError() }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .clear
        let pan = UIPanGestureRecognizer(target: self, action: #selector(pulled(_:)))
        pan.delegate = self
        view.addGestureRecognizer(pan)
        for bar in [top, bottom] {
            bar.view.backgroundColor = .clear
            bar.safeAreaRegions = []
            bar.sizingOptions = .intrinsicContentSize
            bar.view.translatesAutoresizingMaskIntoConstraints = false
            view.addSubview(bar.view)
        }
        NSLayoutConstraint.activate([
            top.view.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            top.view.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            top.view.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            bottom.view.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -4),
            bottom.view.centerXAnchor.constraint(equalTo: view.centerXAnchor),
        ])
    }

    func setChrome(_ visible: Bool) {
        guard (top.view.alpha > 0.5) != visible else { return }
        UIView.animate(withDuration: 0.2) { self.top.view.alpha = visible ? 1 : 0; self.bottom.view.alpha = visible ? 1 : 0 }
    }

    private func page(_ i: Int) -> PhotoPage? {
        guard tiles.indices.contains(i) else { return nil }
        let p = make(tiles[i], i)
        // Keep the neighbours' full-screen copies warm, so a swipe lands on a sharp photo.
        for j in [i + 1, i - 1] where tiles.indices.contains(j) { Task { await ImageStore.shared.warm(tiles[j].sha) } }
        return p
    }

    private func make(_ tile: Tile, _ i: Int) -> PhotoPage {
        let p = PhotoPage(tile: tile, index: i, lookup: lookup, playback: playback)
        p.onTap = { [weak self] in self?.onTap() }
        p.onZoom = { [weak self] z in self?.onZoom(z) }
        return p
    }

    private var current: PhotoPage? { viewControllers?.first as? PhotoPage }

    /// Show `tile` on the page that is up (another photo of its stack), or with nil the list's own photo
    /// again. The page keeps its place in the list, so a swipe still goes to the next photo of the list.
    func show(_ tile: Tile?, at index: Int) {
        // Only the page it was picked for: just after a swipe the page is already the next photo's.
        guard let c = current, c.index == index, tiles.indices.contains(c.index) else { return }
        let want = tile ?? tiles[c.index]
        guard c.tile.sha != want.sha else { return }
        setViewControllers([make(want, c.index)], direction: .forward, animated: false)
    }

    func pageViewController(_ pvc: UIPageViewController, viewControllerBefore vc: UIViewController) -> UIViewController? {
        page((vc as! PhotoPage).index - 1)
    }

    func pageViewController(_ pvc: UIPageViewController, viewControllerAfter vc: UIViewController) -> UIViewController? {
        page((vc as! PhotoPage).index + 1)
    }

    func pageViewController(_ pvc: UIPageViewController, didFinishAnimating finished: Bool, previousViewControllers: [UIViewController], transitionCompleted completed: Bool) {
        if completed, let c = current { onIndex(c.index) }
    }

    // Flick up for details. (Pulling down is the zoom transition's own gesture: the photo follows the
    // finger back to its tile.)
    func gestureRecognizerShouldBegin(_ g: UIGestureRecognizer) -> Bool {
        guard let pan = g as? UIPanGestureRecognizer, let c = current, !c.isZoomed else { return false }
        let v = pan.velocity(in: view)
        return v.y < 0 && abs(v.y) > abs(v.x) * 1.3
    }

    @objc private func pulled(_ pan: UIPanGestureRecognizer) {
        guard let v = current?.view else { return }
        let t = pan.translation(in: view)
        switch pan.state {
        case .changed:
            v.transform = CGAffineTransform(translationX: 0, y: min(0, t.y) * 0.35)
        case .ended, .cancelled:
            if pan.state == .ended, t.y < -50 || pan.velocity(in: view).y < -600 { onInfo() }
            UIView.animate(withDuration: 0.35, delay: 0, usingSpringWithDamping: 0.85, initialSpringVelocity: 0) { v.transform = .identity }
        default: break
        }
    }
}

final class PhotoPage: UIViewController, UIScrollViewDelegate {
    let tile: Tile
    let index: Int
    var onTap: () -> Void = {}
    var onZoom: (Bool) -> Void = { _ in }
    private let scroll = UIScrollView()
    private let imageView = UIImageView()
    private var fitted = CGSize.zero
    private var task: Task<Void, Never>?
    private var large = false
    private var sharp = false           // the full-screen image is showing, not just the thumbnail
    private var asked = false           // ...and it has been asked for, and didn't come
    private var back: AnyCancellable?
    private lazy var aspect = CGFloat(max(tile.ar, 0.05))    // a video's is corrected from its poster and its picture

    // a video, or a Live Photo's clip: made when the page is the one up, let go when it is not
    private let lookup: @MainActor (String) async -> Photo?
    private let playback: Playback
    private lazy var movie = MovieView(fill: !tile.video)
    private let spinner = UIActivityIndicatorView(style: .medium)
    private let problem = MovieNote()
    private var up = false              // the current page
    private var failed = false
    private var held = false            // a Live Photo being held
    private var patience: Task<Void, Never>?

    init(tile: Tile, index: Int, lookup: @escaping @MainActor (String) async -> Photo?, playback: Playback) {
        self.tile = tile
        self.index = index
        self.lookup = lookup
        self.playback = playback
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError() }

    var isZoomed: Bool { scroll.zoomScale > 1.01 }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .clear
        scroll.frame = view.bounds
        scroll.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        scroll.delegate = self
        scroll.minimumZoomScale = 1
        scroll.maximumZoomScale = 8
        scroll.showsVerticalScrollIndicator = false
        scroll.showsHorizontalScrollIndicator = false
        scroll.contentInsetAdjustmentBehavior = .never
        scroll.decelerationRate = .fast
        view.addSubview(scroll)
        imageView.contentMode = .scaleAspectFit
        imageView.clipsToBounds = true
        scroll.addSubview(imageView)
        let double = UITapGestureRecognizer(target: self, action: #selector(doubleTapped(_:)))
        double.numberOfTapsRequired = 2
        let single = UITapGestureRecognizer(target: self, action: #selector(tapped))
        single.require(toFail: double)
        scroll.addGestureRecognizer(double)
        scroll.addGestureRecognizer(single)
        if tile.video || tile.live {
            movie.frame = imageView.bounds
            movie.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            movie.alpha = tile.video ? 1 : 0
            imageView.addSubview(movie)
            spinner.color = .white
            spinner.hidesWhenStopped = true
            problem.isHidden = true
            view.addSubview(spinner)
            view.addSubview(problem)
        }
        if tile.live && !tile.video {
            // Touch and hold to see it move, as in Photos. Nothing else in the viewer holds.
            let hold = UILongPressGestureRecognizer(target: self, action: #selector(holding(_:)))
            hold.minimumPressDuration = 0.25
            scroll.addGestureRecognizer(hold)
        }

        imageView.image = ImageStore.shared.ready(tile.sha)
        if let i = imageView.image { fit(i.size) }
        load()
        // A page that could only show its thumbnail (the server was away) sharpens when the server is back,
        // and a video that could not play tries again.
        back = SyncEngine.shared.$online.removeDuplicates().dropFirst().filter { $0 }.receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                self?.sharpen()
                if let self, self.up, self.failed { self.play() }
            }
    }

    private func load() {
        task?.cancel()
        asked = false
        let sha = tile.sha
        task = Task { [weak self] in
            if self?.imageView.image == nil, let t = await ImageStore.shared.thumb(sha), !Task.isCancelled {
                self?.imageView.image = t
                self?.fit(t.size)
            }
            let p = await ImageStore.shared.image(.preview, sha)
            guard !Task.isCancelled, let self else { return }
            guard let p else { self.asked = true; return }
            self.sharp = true
            self.fit(p.size)
            if !self.large { UIView.transition(with: self.imageView, duration: 0.15, options: .transitionCrossDissolve) { self.imageView.image = p } }
        }
    }

    private func sharpen() { if asked && !sharp { load() } }

    // The pager keeps the pages either side made ready, so one made while the server was away comes
    // back as it was: ask again when it is the one being looked at.
    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        sharpen()
        up = true
        if tile.video || tile.live { play() }
    }

    deinit { task?.cancel(); patience?.cancel() }

    /// A video's shape, from its poster or its picture: the catalog may not know it. A photo's is the catalog's.
    private func fit(_ size: CGSize) {
        guard tile.video, size.width > 0, size.height > 0 else { return }
        let ar = size.width / size.height
        guard abs(ar - aspect) / aspect > 0.02 else { return }
        aspect = ar
        fitted = .zero
        view.setNeedsLayout()
    }

    // MARK: a video, or a Live Photo's clip

    /// The page is up: a video starts (where a search found it, if one did); a Live Photo's clip is made
    /// ready, to play the moment the photo is held.
    private func play() {
        failed = false
        problem.isHidden = true
        let sha = tile.sha, video = tile.video, at = Double(tile.at)
        movie.onReady = { [weak self] in
            guard let self else { return }
            self.patience?.cancel()
            self.spinner.stopAnimating()
            self.playback.update(sha, length: self.movie.length, ready: true)
        }
        movie.onFailed = { [weak self] in self?.cannotPlay() }
        movie.onSize = { [weak self] in self?.fit($0) }
        if video {
            movie.onPlaying = { [weak self] on in self?.playback.update(sha, playing: on) }
            movie.onTime = { [weak self] t in self?.playback.update(sha, time: t) }
            movie.onWaiting = { [weak self] waiting in
                if waiting { self?.spinner.startAnimating() } else { self?.spinner.stopAnimating() }
            }
            movie.onEnd = { [weak self] in self?.playback.update(sha, playing: false) }
        } else {
            movie.onEnd = { [weak self] in self?.letGo() }
        }
        Task { [weak self] in
            var item: AVPlayerItem?
            if LocalPhotos.id(sha) != nil {
                if let asset = await LocalPhotos.shared.video(sha) { item = AVPlayerItem(asset: asset) }
            } else if video {
                item = AVPlayerItem(url: Server.video(sha))
            } else if let clip = await self?.lookup(sha)?.live {
                item = AVPlayerItem(url: Server.video(clip))
            }
            guard let self, self.up else { return }
            guard let item else { if video { self.cannotPlay() }; return }
            self.movie.load(item)
            guard video else { return }
            self.playback.attach(sha, self.movie, length: Double(self.tile.dur))
            if at > 0.5 { self.movie.player?.seek(to: CMTime(seconds: at, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero) { _ in } }
            self.movie.player?.play()
            // Asked of a server that isn't there, AVPlayer can wait a long time before it gives up: say so sooner.
            self.patience?.cancel()
            self.patience = Task { [weak self] in
                try? await Task.sleep(for: .seconds(12))
                guard !Task.isCancelled, let self, self.up, !self.movie.isReady else { return }
                self.cannotPlay()
            }
        }
    }

    private func cannotPlay() {
        guard up else { return }
        failed = true
        patience?.cancel()
        spinner.stopAnimating()
        guard tile.video else { return }       // a Live Photo is still a photo: it says so only when held
        movie.player?.pause()
        problem.text = LocalPhotos.id(tile.sha) != nil ? "This video can’t be read from Photos."
                     : "Videos play from your server, which isn’t reachable right now."
        problem.isHidden = false
        view.setNeedsLayout()
    }

    private func stop() {
        up = false
        patience?.cancel()
        spinner.stopAnimating()
        movie.release()
        playback.detach(tile.sha)
        if tile.live { held = false; movie.alpha = 0 }
    }

    @objc private func holding(_ g: UILongPressGestureRecognizer) {
        switch g.state {
        case .began:
            if failed || !up {
                flash("The moving part of a Live Photo plays from your server, which isn’t reachable.")
                return
            }
            held = true
            if movie.isReady { liveStart() } else {
                movie.onReady = { [weak self] in if self?.held == true { self?.liveStart() } }      // as soon as it can
            }
        case .ended, .cancelled, .failed:
            letGo()
        default: break
        }
    }

    private func liveStart() {
        guard let p = movie.player else { return }
        Haptics.tap()
        p.seek(to: .zero)
        p.play()
        UIView.animate(withDuration: 0.12) { self.movie.alpha = 1 }
    }

    /// Back to the still: the hold ended, or the clip did.
    private func letGo() {
        held = false
        movie.player?.pause()
        UIView.animate(withDuration: 0.25) { self.movie.alpha = 0 }
    }

    private func flash(_ text: String) {
        problem.text = text
        problem.isHidden = false
        view.setNeedsLayout()
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(2.5))
            self?.problem.isHidden = true
        }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        spinner.center = CGPoint(x: view.bounds.midX, y: view.bounds.midY)
        if !problem.isHidden {
            let s = problem.sizeThatFits(CGSize(width: view.bounds.width - 48, height: 0))
            problem.frame = CGRect(x: (view.bounds.width - s.width) / 2, y: view.bounds.midY - s.height / 2, width: s.width, height: s.height)
        }
        guard scroll.bounds.size != fitted, scroll.bounds.width > 0 else { return }
        fitted = scroll.bounds.size
        scroll.zoomScale = 1
        let ar = aspect
        let size = fitted.width / fitted.height > ar ? CGSize(width: fitted.height * ar, height: fitted.height)
                                                     : CGSize(width: fitted.width, height: fitted.width / ar)
        imageView.frame = CGRect(origin: .zero, size: size)
        scroll.contentSize = size
        centre()
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        scroll.zoomScale = 1
        view.transform = .identity
        if tile.video || tile.live { stop() }
    }

    private func centre() {
        let x = max((scroll.bounds.width - scroll.contentSize.width) / 2, 0), y = max((scroll.bounds.height - scroll.contentSize.height) / 2, 0)
        scroll.contentInset = UIEdgeInsets(top: y, left: x, bottom: y, right: x)
    }

    func viewForZooming(in scrollView: UIScrollView) -> UIView? { imageView }

    func scrollViewDidZoom(_ scrollView: UIScrollView) {
        centre()
        // Not zoomed, there is nothing to pan: leave drags to the pager and to the pull-down that closes.
        scroll.isScrollEnabled = isZoomed
        onZoom(isZoomed)
    }

    func scrollViewDidEndZooming(_ scrollView: UIScrollView, with view: UIView?, atScale scale: CGFloat) {
        guard scale > 1.6, !large, !tile.video else { return }      // zoomed in far enough to see more than 1600 px holds
        large = true
        let sha = tile.sha
        Task { [weak self] in
            if let img = await ImageStore.shared.image(.large, sha) { self?.imageView.image = img } else { self?.large = false }
        }
    }

    @objc private func tapped() { onTap() }

    @objc private func doubleTapped(_ g: UITapGestureRecognizer) {
        if isZoomed {
            scroll.setZoomScale(1, animated: true)
        } else {
            let p = g.location(in: imageView), s: CGFloat = 2.5
            let w = scroll.bounds.width / s, h = scroll.bounds.height / s
            scroll.zoom(to: CGRect(x: p.x - w / 2, y: p.y - h / 2, width: w, height: h), animated: true)
        }
    }
}

// MARK: stories

/// The stories told about a photo (`GET /api/photos/<sha>/stories`): who told each and when, the words or the
/// transcript, the recording to play; and one quiet row, "Tell a story", which opens the teller (Teller.swift). A
/// photo with no story shows only the row.
struct StoriesSection: View {
    let sha: String
    @State private var stories: [Story] = []
    @State private var telling = false
    @StateObject private var teller: Teller

    init(sha: String) {
        self.sha = sha
        _teller = StateObject(wrappedValue: Teller { what in
            guard let s = Story(try await API.json(what.request(base: Server.baseURL, question: nil, photo: sha))) else {
                throw APIError(code: 0, description: "The server’s answer could not be read.")
            }
            return s
        })
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !stories.isEmpty {
                Text("STORIES").font(.caption2.weight(.semibold)).kerning(1.2).foregroundStyle(.secondary)
                ForEach(stories) { s in
                    StoryRow(story: s).task(id: s.id) { await StoryFollow.until(s) { got in update(got) } }
                }
            }
            if telling {
                TellView(teller: teller, prompt: "What do you remember about it? Type it, or record it", sendLabel: "Tell it",
                         dismiss: "Cancel") { teller.cancel(); teller.discard(); telling = false }
                    .padding(14)
                    .background(Color.well, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            } else {
                Button { Haptics.tick(); teller.again(); telling = true } label: {
                    HStack(spacing: 14) {
                        Image(systemName: "text.bubble").font(.system(size: 17)).foregroundStyle(Color.sunLit).frame(width: 24)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(stories.isEmpty ? "Tell a story" : "Tell another story").font(.title(17)).foregroundStyle(.primary)
                            Text("Typed or spoken: kept with your name and the day").font(.footnote).foregroundStyle(.secondary)
                        }
                        Spacer()
                    }
                    .padding(14)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .background(Color.well, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            }
        }
        .task(id: sha) {
            if let j = try? await API.get("api/photos/\(sha)/stories") { stories = (j["stories"] as? [Any] ?? []).compactMap(Story.init) }
        }
        .onAppear { teller.onSent = { s in update(s); telling = false } }
    }

    private func update(_ s: Story) {
        if let i = stories.firstIndex(where: { $0.id == s.id }) { stories[i] = s } else { stories.append(s) }
    }
}
