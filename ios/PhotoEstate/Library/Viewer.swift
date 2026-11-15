import MapKit
import SwiftUI
import UIKit

/// One photo, full screen: swipe between photos, pinch or double-tap to zoom, swipe down to close,
/// swipe up for its details. It shows whatever is on the phone at once (the thumbnail, then the
/// full-screen copy if it has been kept) and sharpens from the server when it can.
struct ViewerScreen: View {
    let tiles: [Tile]
    @State var index: Int
    @ObservedObject var model: LibraryModel
    var onIndex: (Int) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var chrome = true
    @State private var info: Photo?           // the photo whose details are showing
    @State private var photo: Photo?
    @State private var drag: CGFloat = 0          // 0...1: how far the photo has been pulled down
    @State private var sharing = false

    private var tile: Tile { tiles[min(index, tiles.count - 1)] }

    var body: some View {
        GeometryReader { geo in
        ZStack {
            Color.black.opacity(1 - drag * 0.9).ignoresSafeArea()
            PagerView(tiles: tiles, index: $index, onTap: { withAnimation(.easeOut(duration: 0.18)) { chrome.toggle() } },
                      onDrag: { drag = $0 }, onClose: { dismiss() }, onInfo: { info = photo },
                      chrome: AnyView(bars.padding(.top, geo.safeAreaInsets.top).padding(.bottom, geo.safeAreaInsets.bottom)
                                        .opacity(chrome && drag == 0 ? 1 : 0).allowsHitTesting(chrome)))
                .ignoresSafeArea()
        }
        }
        .task(id: index) {
            onIndex(index)
            photo = await model.photo(tile.sha)
        }
        .sheet(item: $info) { shown in
            InfoView(photo: shown) { q in
                info = nil
                dismiss()
                model.go(q)
            }
            .presentationDetents([.medium, .large])
        }
        .statusBarHidden(!chrome)
        .preferredColorScheme(.dark)
        .presentationBackground(.clear)
    }

    private var bars: some View {
        VStack {
            HStack(alignment: .top) {
                round("xmark", "Close") { dismiss() }
                Spacer()
                VStack(spacing: 2) {
                    Text(Dates.long(tile.when)).font(.subheadline.weight(.semibold))
                    Text([Dates.clock(tile.when), tile.city?.split(separator: ",").first.map(String.init) ?? ""].filter { !$0.isEmpty }.joined(separator: " · "))
                        .font(.caption).foregroundStyle(.secondary)
                }
                .foregroundStyle(.white)
                .shadow(radius: 3)
                .padding(.top, 4)
                Spacer()
                round("info", "Details") { info = photo }
            }
            Spacer()
            HStack {
                round(sharing ? "ellipsis" : "square.and.arrow.up", "Share") { share() }
                Spacer()
                if tile.fav { Image(systemName: "heart.fill").foregroundStyle(.white).shadow(radius: 3).accessibilityLabel("Favourite") }
                Spacer()
                round("sparkles", "More like this") {
                    dismiss()
                    model.go(Query(similar: tile.sha))
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
    }

    private func round(_ symbol: String, _ label: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 17, weight: .semibold)).frame(width: 44, height: 44)
        }
        .foregroundStyle(.white)
        .background(.ultraThinMaterial, in: Circle())
        .accessibilityLabel(label)
    }

    /// Share the original file when the server can be reached (so Save Image keeps its date and place),
    /// else the best copy on the phone.
    private func share() {
        guard !sharing else { return }
        sharing = true
        let sha = tile.sha, name = photo?.filename ?? "\(sha.prefix(12)).jpg"
        Task {
            defer { sharing = false }
            let dir = FileManager.default.temporaryDirectory.appendingPathComponent("share-\(sha.prefix(8))", isDirectory: true)
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            var file: URL?
            var req = URLRequest(url: Server.baseURL.appendingPathComponent("original/\(sha)"))
            req.timeoutInterval = 20
            if let (tmp, resp) = try? await URLSession.shared.download(for: req), (resp as? HTTPURLResponse)?.statusCode == 200 {
                let dest = dir.appendingPathComponent(name)
                try? FileManager.default.removeItem(at: dest)
                if (try? FileManager.default.moveItem(at: tmp, to: dest)) != nil { file = dest }
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

struct InfoView: View {
    let photo: Photo
    var go: (Query) -> Void

    var body: some View {
        NavigationStack {
            List {
                Section {
                    row("calendar", Dates.long(photo.when), [Dates.clock(photo.when), photo.tz ?? ""].filter { !$0.isEmpty }.joined(separator: " · "))
                    if let place = photo.place {
                        Button { go(Query(picks: photo.city.map { [.city: [$0]] } ?? [:])) } label: {
                            row("mappin.and.ellipse", place, [photo.county ?? "", photo.placeInferred ? "from photos taken around then" : ""].filter { !$0.isEmpty }.joined(separator: " · "), link: true)
                        }
                    }
                    if let lat = photo.lat, let lon = photo.lon {
                        let at = CLLocationCoordinate2D(latitude: lat, longitude: lon)
                        Map(initialPosition: .region(MKCoordinateRegion(center: at, latitudinalMeters: 4000, longitudinalMeters: 4000)), interactionModes: []) {
                            Marker("", coordinate: at)
                        }
                        .frame(height: 150)
                        .listRowInsets(EdgeInsets())
                    }
                }
                if photo.event != nil || photo.trip != nil {
                    Section {
                        if let e = photo.event {
                            Button { go(Query(event: e)) } label: { row("sparkles.rectangle.stack", photo.eventTitle ?? "This event", "Photos taken around then", link: true) }
                        }
                        if let t = photo.trip {
                            Button { go(Query(picks: [.trip: [String(t)]])) } label: { row("airplane", photo.tripTitle ?? "This trip", "The whole trip", link: true) }
                        }
                    }
                }
                if !photo.people.isEmpty {
                    Section("People") {
                        ForEach(photo.people, id: \.self) { name in
                            Button { go(Query(picks: [.person: [name]])) } label: { row("person.crop.circle", name, "", link: true) }
                        }
                    }
                }
                if !photo.albums.isEmpty {
                    Section("Albums") {
                        ForEach(photo.albums, id: \.self) { name in
                            Button { go(Query(picks: [.album: [name]])) } label: { row("rectangle.stack", name, "", link: true) }
                        }
                    }
                }
                Section {
                    if let camera = photo.camera {
                        Button { go(Query(picks: [.camera: [camera]])) } label: { row("camera", camera, photo.lens ?? "", link: true) }
                    }
                    if !exposure.isEmpty { row("camera.aperture", exposure, "") }
                    row("doc", photo.filename, [photo.w.flatMap { w in photo.h.map { "\(w) × \($0)" } } ?? "", photo.desc ?? ""].filter { !$0.isEmpty }.joined(separator: " · "))
                }
            }
            .navigationTitle("Details")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private var exposure: String {
        var parts: [String] = []
        if let f = photo.focal35 ?? photo.focal { parts.append("\(f.formatted(.number.precision(.fractionLength(0...1)))) mm") }
        if let n = photo.fnumber { parts.append("ƒ/\(n.formatted(.number.precision(.fractionLength(0...1))))") }
        if let e = photo.exposure, e > 0 { parts.append(e < 1 ? "1/\(Int((1 / e).rounded())) s" : "\(e.formatted(.number.precision(.fractionLength(0...1)))) s") }
        if let i = photo.iso { parts.append("ISO \(Int(i))") }
        return parts.joined(separator: " · ")
    }

    private func row(_ symbol: String, _ title: String, _ detail: String, link: Bool = false) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).frame(width: 24).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).foregroundStyle(.primary)
                if !detail.isEmpty { Text(detail).font(.footnote).foregroundStyle(.secondary) }
            }
            Spacer()
            if link { Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary) }
        }
    }
}

// MARK: paging and zooming (UIKit)

struct PagerView: UIViewControllerRepresentable {
    let tiles: [Tile]
    @Binding var index: Int
    var onTap: () -> Void
    var onDrag: (CGFloat) -> Void
    var onClose: () -> Void
    var onInfo: () -> Void
    var chrome: AnyView         // the buttons, hosted above the pages so UIKit decides who gets a touch

    func makeUIViewController(context: Context) -> PagerController {
        let c = PagerController(tiles: tiles, index: index)
        c.onIndex = { i in DispatchQueue.main.async { index = i } }
        return c
    }

    func updateUIViewController(_ c: PagerController, context: Context) {
        c.onTap = onTap
        c.onDrag = onDrag
        c.onClose = onClose
        c.onInfo = onInfo
        c.chrome.rootView = chrome
    }
}

final class PagerController: UIPageViewController, UIPageViewControllerDataSource, UIPageViewControllerDelegate, UIGestureRecognizerDelegate {
    var onIndex: (Int) -> Void = { _ in }
    var onTap: () -> Void = {}
    var onDrag: (CGFloat) -> Void = { _ in }
    var onClose: () -> Void = {}
    var onInfo: () -> Void = {}
    let chrome = UIHostingController(rootView: AnyView(EmptyView()))
    private let tiles: [Tile]

    init(tiles: [Tile], index: Int) {
        self.tiles = tiles
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
        addChild(chrome)
        chrome.view.backgroundColor = .clear
        chrome.safeAreaRegions = []
        chrome.view.frame = view.bounds
        chrome.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(chrome.view)
        chrome.didMove(toParent: self)
    }

    private func page(_ i: Int) -> PhotoPage? {
        guard tiles.indices.contains(i) else { return nil }
        let p = PhotoPage(tile: tiles[i], index: i)
        p.onTap = { [weak self] in self?.onTap() }
        // Keep the neighbours' full-screen copies warm, so a swipe lands on a sharp photo.
        for j in [i + 1, i - 1] where tiles.indices.contains(j) { Task { _ = await ImageStore.shared.fetch(.preview, tiles[j].sha) } }
        return p
    }

    private var current: PhotoPage? { viewControllers?.first as? PhotoPage }

    func pageViewController(_ pvc: UIPageViewController, viewControllerBefore vc: UIViewController) -> UIViewController? {
        page((vc as! PhotoPage).index - 1)
    }

    func pageViewController(_ pvc: UIPageViewController, viewControllerAfter vc: UIViewController) -> UIViewController? {
        page((vc as! PhotoPage).index + 1)
    }

    func pageViewController(_ pvc: UIPageViewController, didFinishAnimating finished: Bool, previousViewControllers: [UIViewController], transitionCompleted completed: Bool) {
        if completed, let c = current { onIndex(c.index) }
    }

    // Pull down to close, flick up for details: only when the photo is not zoomed, and the pull is vertical.
    func gestureRecognizerShouldBegin(_ g: UIGestureRecognizer) -> Bool {
        guard let pan = g as? UIPanGestureRecognizer, let c = current, !c.isZoomed else { return false }
        let v = pan.velocity(in: view)
        return abs(v.y) > abs(v.x) * 1.3
    }

    @objc private func pulled(_ pan: UIPanGestureRecognizer) {
        guard let v = current?.view else { return }
        let t = pan.translation(in: view), vel = pan.velocity(in: view)
        switch pan.state {
        case .changed:
            let down = max(0, t.y)
            let k = 1 - min(down / 900, 0.35)
            v.transform = CGAffineTransform(translationX: t.x * (down > 0 ? 1 : 0), y: down).scaledBy(x: k, y: k)
            onDrag(min(down / 300, 1))
        case .ended, .cancelled:
            if pan.state == .ended, t.y > 110 || vel.y > 900 {
                onClose()
            } else {
                if pan.state == .ended, t.y < -60 || vel.y < -700 { onInfo() }
                UIView.animate(withDuration: 0.3, delay: 0, usingSpringWithDamping: 0.85, initialSpringVelocity: 0) { v.transform = .identity }
                onDrag(0)
            }
        default: break
        }
    }
}

final class PhotoPage: UIViewController, UIScrollViewDelegate {
    let tile: Tile
    let index: Int
    var onTap: () -> Void = {}
    private let scroll = UIScrollView()
    private let imageView = UIImageView()
    private var fitted = CGSize.zero
    private var task: Task<Void, Never>?
    private var large = false

    init(tile: Tile, index: Int) {
        self.tile = tile
        self.index = index
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

        imageView.image = ImageStore.shared.ready(tile.sha)
        let sha = tile.sha
        task = Task { [weak self] in
            if self?.imageView.image == nil, let t = await ImageStore.shared.thumb(sha), !Task.isCancelled { self?.imageView.image = t }
            if let p = await ImageStore.shared.image(.preview, sha), !Task.isCancelled, let self, !self.large {
                UIView.transition(with: self.imageView, duration: 0.15, options: .transitionCrossDissolve) { self.imageView.image = p }
            }
        }
    }

    deinit { task?.cancel() }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        guard scroll.bounds.size != fitted, scroll.bounds.width > 0 else { return }
        fitted = scroll.bounds.size
        scroll.zoomScale = 1
        let ar = CGFloat(max(tile.ar, 0.05))
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
    }

    private func centre() {
        let x = max((scroll.bounds.width - scroll.contentSize.width) / 2, 0), y = max((scroll.bounds.height - scroll.contentSize.height) / 2, 0)
        scroll.contentInset = UIEdgeInsets(top: y, left: x, bottom: y, right: x)
    }

    func viewForZooming(in scrollView: UIScrollView) -> UIView? { imageView }

    func scrollViewDidZoom(_ scrollView: UIScrollView) { centre() }

    func scrollViewDidEndZooming(_ scrollView: UIScrollView, with view: UIView?, atScale scale: CGFloat) {
        guard scale > 1.6, !large else { return }      // zoomed in far enough to see more than 1600 px holds
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
