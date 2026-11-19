import SwiftUI
import UIKit

/// A photo open in the viewer: which list it came from and where the viewer is in it. The grid reads
/// `index` to know which tile the photo should shrink back into.
final class ViewerSession: ObservableObject {
    let tiles: [Tile]
    @Published var index: Int
    var zoomed = false                    // pinched in: a drag pans the photo, it doesn't close the viewer
    var close: () -> Void = {}

    init(tiles: [Tile], index: Int) {
        self.tiles = tiles
        self.index = index
    }

    var tile: Tile { tiles[min(max(index, 0), tiles.count - 1)] }
}

/// The justified grid, as in the web app (chapter 13 of the book): rows as wide as the screen, every
/// photo at its own shape. Each row is one collection-view cell, so a library of 100,000 photos is some
/// 30,000 cells of known height: the layout is arithmetic, and scrolling never waits on it.
struct GridView: UIViewControllerRepresentable {
    let data: GridData
    let rowHeight: CGFloat
    var title: String? = nil            // a large heading that scrolls away with the photos
    var subtitle = ""
    var topInset: CGFloat = 0           // a bar drawn over the top of the grid
    var jump = Jump()                   // scroll to this month (or day) when it changes
    var onNearEnd: () -> Void = {}
    var onRefresh: () async -> Void = {}
    var onRowHeight: (Double) -> Void = { _ in }
    var onTop: (String) -> Void = { _ in }       // the heading of the section now at the top, "" above the first
    var footer: GridFooter? = nil       // under the last photo: the months either side of the one showing
    var onFooter: (String) -> Void = { _ in }
    var viewer: (ViewerSession) -> AnyView

    func makeUIViewController(context: Context) -> GridController { GridController() }

    func updateUIViewController(_ c: GridController, context: Context) {
        c.onNearEnd = onNearEnd
        c.onRefresh = onRefresh
        c.onRowHeight = onRowHeight
        c.onTop = onTop
        c.viewer = viewer
        c.onFooter = onFooter
        c.setHeading(title, subtitle, topInset: topInset)
        c.setFooter(footer)
        c.show(data, rowHeight: rowHeight)
        c.go(jump)
    }
}

/// What sits under a month's photos: the month before it and the month after, as keys ("2019-06").
struct GridFooter: Equatable {
    var before: String?
    var after: String?
}

private struct RowLayout {
    let start: Int               // index of the row's first photo in `tiles`
    let widths: [CGFloat]
    let height: CGFloat
    let inset: CGFloat           // a short row that could not fill the width is centred
}

private struct SectionLayout {
    let key: String
    let places: String
    let count: Int
    var rows: [RowLayout]
}

final class GridController: UIViewController, UICollectionViewDataSource, UICollectionViewDelegateFlowLayout, UIGestureRecognizerDelegate {
    var onNearEnd: () -> Void = {}
    var onRefresh: () async -> Void = {}
    var onRowHeight: (Double) -> Void = { _ in }
    var onTop: (String) -> Void = { _ in }
    var onFooter: (String) -> Void = { _ in }
    var viewer: (ViewerSession) -> AnyView = { _ in AnyView(EmptyView()) }

    private var data = GridData()
    private var rowHeight: CGFloat = 128
    private var sections: [SectionLayout] = []
    private var width: CGFloat = 0
    private var topInset: CGFloat = 0
    private var topKey: String? = nil
    private var jump = Jump()
    private let heading = HeadingView()
    private let foot = MonthNav()
    private var footer: GridFooter?
    /// Sections have captions: months or days of a list in time order, or an album's own sections.
    private var headed: Bool { data.grain > 0 || data.runs != nil }
    private func caption(_ key: String) -> String { data.runs != nil ? key : Dates.heading(key) }
    private let scrubber = Scrubber()
    private let scrim = UIView()                 // paper fading out below the status bar, so captions don't run into the clock
    private let scrimMask = CAGradientLayer()
    private var scrubHide: DispatchWorkItem?
    private lazy var collection: UICollectionView = {
        let layout = UICollectionViewFlowLayout()
        layout.minimumLineSpacing = Estate.gap
        layout.minimumInteritemSpacing = 0
        let v = UICollectionView(frame: .zero, collectionViewLayout: layout)
        v.backgroundColor = Estate.paper
        v.dataSource = self
        v.delegate = self
        v.alwaysBounceVertical = true
        v.showsVerticalScrollIndicator = false          // the scrubber is the indicator
        v.register(RowCell.self, forCellWithReuseIdentifier: "row")
        v.register(HeaderView.self, forSupplementaryViewOfKind: UICollectionView.elementKindSectionHeader, withReuseIdentifier: "head")
        let refresh = UIRefreshControl()
        refresh.tintColor = Estate.sun
        refresh.addAction(UIAction { [weak self] _ in
            Task { await self?.onRefresh(); refresh.endRefreshing() }
        }, for: .valueChanged)
        v.refreshControl = refresh
        let pinch = UIPinchGestureRecognizer(target: self, action: #selector(pinched(_:)))
        v.addGestureRecognizer(pinch)
        return v
    }()

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = Estate.paper
        collection.frame = view.bounds
        collection.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(collection)
        setContentScrollView(collection)
        collection.addSubview(heading)
        foot.isHidden = true
        foot.go = { [weak self] key in self?.onFooter(key) }
        collection.addSubview(foot)
        scrim.backgroundColor = Estate.paper
        scrim.isUserInteractionEnabled = false
        scrimMask.colors = [UIColor.black.cgColor, UIColor.black.withAlphaComponent(0.85).cgColor, UIColor.clear.cgColor]
        scrim.layer.mask = scrimMask
        view.addSubview(scrim)
        view.addSubview(scrubber)
        scrubber.alpha = 0
        scrubber.addGestureRecognizer(UIPanGestureRecognizer(target: self, action: #selector(scrubbed(_:))))
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        if abs(collection.bounds.width - width) > 0.5 {
            width = collection.bounds.width
            layout()
            collection.reloadData()
            placeHeading()
        }
        let bar = view.window?.safeAreaInsets.top ?? 0
        scrim.frame = CGRect(x: 0, y: 0, width: view.bounds.width, height: bar + 26)
        scrimMask.frame = scrim.bounds
        scrimMask.locations = [0, NSNumber(value: Double(bar * 0.7 / max(bar + 26, 1))), 1]
        placeFooter()
        placeScrubber()
    }

    // MARK: what is shown

    func setHeading(_ title: String?, _ subtitle: String, topInset inset: CGFloat) {
        let changed = heading.show(title, subtitle) || abs(inset - topInset) > 0.5
        topInset = inset
        if changed, width > 0 { placeHeading() }
    }

    /// The heading sits in the scroll view above the first photo, so it leaves with them.
    private func placeHeading() {
        let h = heading.isHidden ? 0 : heading.height(for: width)
        let atTop = collection.contentOffset.y <= -collection.adjustedContentInset.top + 1
        heading.frame = CGRect(x: 0, y: -h, width: width, height: h)
        collection.contentInset.top = topInset + h
        if atTop { collection.setContentOffset(CGPoint(x: 0, y: -collection.adjustedContentInset.top), animated: false) }
    }

    func setFooter(_ f: GridFooter?) {
        guard f != footer else { return }
        footer = f
        foot.show(f)
        if width > 0 { placeFooter() }
    }

    /// The footer sits in the scroll view under the last photo, as the heading sits above the first.
    private func placeFooter() {
        let h: CGFloat = foot.isHidden ? 0 : 76
        if abs(collection.contentInset.bottom - h) > 0.5 { collection.contentInset.bottom = h }
        guard !foot.isHidden else { return }
        collection.layoutIfNeeded()
        foot.frame = CGRect(x: 0, y: collection.collectionViewLayout.collectionViewContentSize.height, width: width, height: h)
    }

    func show(_ new: GridData, rowHeight h: CGFloat) {
        guard new.id != data.id || h != rowHeight else { return }
        let reset = new.reset != data.reset
        let grew = !reset && h == rowHeight && new.grain == 0 && data.grain == 0 && new.tiles.count > data.tiles.count
        // Refreshed in place (a sync, or a new size): keep looking at the same photo.
        let anchor = reset || grew ? nil : topPhoto()
        let resized = h != rowHeight && new.id == data.id
        data = new
        rowHeight = h
        guard width > 0 else { return }
        layout()
        if resized {
            UIView.transition(with: collection, duration: 0.22, options: [.transitionCrossDissolve, .allowUserInteraction]) { self.collection.reloadData() }
        } else {
            collection.reloadData()
        }
        if reset {
            collection.setContentOffset(CGPoint(x: 0, y: -collection.adjustedContentInset.top), animated: false)
        } else if let anchor, let path = path(ofPhoto: anchor.sha) {
            collection.layoutIfNeeded()
            if let attr = collection.layoutAttributesForItem(at: path) {
                collection.setContentOffset(CGPoint(x: 0, y: max(-collection.adjustedContentInset.top, attr.frame.minY - anchor.offset)), animated: false)
            }
        }
        placeFooter()
        reportTop()
    }

    /// Put the first section whose key starts with `j.key` just under the bar.
    func go(_ j: Jump) {
        guard j.id != jump.id, width > 0 else { return }
        jump = j
        guard let s = sections.firstIndex(where: { $0.key.hasPrefix(j.key) }) else { return }
        collection.layoutIfNeeded()
        guard let attr = collection.layoutAttributesForSupplementaryElement(ofKind: UICollectionView.elementKindSectionHeader,
                                                                            at: IndexPath(item: 0, section: s)) else { return }
        let top = -collection.adjustedContentInset.top
        let bottom = max(top, collection.contentSize.height + collection.adjustedContentInset.bottom - collection.bounds.height)
        let y = s == 0 ? top : min(max(top, attr.frame.minY - view.safeAreaInsets.top - topInset), bottom)
        collection.setContentOffset(CGPoint(x: 0, y: y), animated: false)
        reportTop()
    }

    // MARK: the viewer

    private func open(_ index: Int) {
        let session = ViewerSession(tiles: data.tiles, index: index)
        let host = UIHostingController(rootView: viewer(session))
        host.view.backgroundColor = .black
        host.safeAreaRegions = []                 // the photo has the whole screen; the bars keep clear of the edges themselves
        host.modalPresentationStyle = .fullScreen
        session.close = { [weak host] in host?.dismiss(animated: true) }
        // The photo grows out of its tile and shrinks back into whichever tile the viewer ended on;
        // pulling it down, or pinching it shut, does the same under the finger.
        let options = UIViewController.Transition.ZoomOptions()
        // Sideways belongs to the pager, and a zoomed photo pans: only a pull down (or a pinch shut) closes.
        options.interactiveDismissShouldBegin = { context in
            !session.zoomed && abs(context.velocity.dy) >= abs(context.velocity.dx)
        }
        options.alignmentRectProvider = { context in
            let b = context.zoomedViewController.view.bounds
            let ar = CGFloat(max(session.tile.ar, 0.05))
            let size = b.width / b.height > ar ? CGSize(width: b.height * ar, height: b.height) : CGSize(width: b.width, height: b.width / ar)
            return CGRect(x: (b.width - size.width) / 2, y: (b.height - size.height) / 2, width: size.width, height: size.height)
        }
        host.preferredTransition = .zoom(options: options) { [weak self] _ in self?.tileView(session.index) }
        Haptics.tap()
        present(host, animated: true)
    }

    /// The tile showing a photo, scrolling it into view if it is not on screen.
    private func tileView(_ index: Int) -> UIView? {
        guard let path = path(ofIndex: index) else { return nil }
        let clear = collection.bounds.inset(by: collection.adjustedContentInset)
        if let frame = collection.layoutAttributesForItem(at: path)?.frame, !clear.contains(frame) {
            collection.scrollToItem(at: path, at: .centeredVertically, animated: false)
            collection.layoutIfNeeded()
        }
        return (collection.cellForItem(at: path) as? RowCell)?.tile(index)
    }

    // MARK: layout

    private func layout() {
        let gap = Estate.gap, W = width
        let H = max(56, rowHeight)
        var out: [SectionLayout] = []
        let tiles = data.tiles
        let shape = { (i: Int) in CGFloat(min(max(tiles[i].ar, 0.5), 2.6)) }
        var i = 0, run = 0
        while i < tiles.count {
            // one section: the next of the list's own runs (an album's sections), or the photos sharing the
            // first `grain` characters of their time
            var key = data.grain == 0 ? "" : String(tiles[i].when.prefix(data.grain))
            var j = i
            var cities: [Substring: Int] = [:]
            if let runs = data.runs {
                key = run < runs.count ? runs[run].title : ""
                j = run + 1 < runs.count ? min(i + max(runs[run].count, 1), tiles.count) : tiles.count   // the last takes what is left
                run += 1
            } else {
                while j < tiles.count, data.grain == 0 || tiles[j].when.prefix(data.grain) == key {
                    if data.grain > 0, let c = tiles[j].city?.split(separator: ",").first { cities[c, default: 0] += 1 }
                    j += 1
                }
            }
            var rows: [RowLayout] = []
            var start = i, sum: CGFloat = 0
            for k in i..<j {
                sum += shape(k)
                let gaps = CGFloat(k - start) * gap
                let last = k == j - 1
                guard sum * H + gaps >= W || last else { continue }
                // A full row shrinks to fit exactly. A section's last row grows to fill the width if that
                // does not make it too tall (one landscape photo alone becomes a full-width picture, at any
                // photo size); if it can't, it grows as far as it may and sits in the middle.
                let fill = (W - gaps) / sum
                let fits = fill <= (last ? max(H * 2.6, min(W * 0.75, 300)) : H) + 0.5
                let h = fits ? fill : min(fill, max(H * 2.2, 240))
                var widths = (start...k).map { (shape($0) * h).rounded() }
                if fits {      // rounding left a pixel or two: spread them
                    var spare = W - gaps - widths.reduce(0, +), m = 0
                    while abs(spare) >= 1 {
                        widths[m % widths.count] += spare > 0 ? 1 : -1
                        spare += spare > 0 ? -1 : 1
                        m += 1
                    }
                }
                rows.append(RowLayout(start: start, widths: widths, height: h.rounded(),
                                      inset: fits ? 0 : ((W - gaps - widths.reduce(0, +)) / 2).rounded()))
                start = k + 1
                sum = 0
            }
            let top = cities.sorted { ($0.value, $1.key) > ($1.value, $0.key) }.prefix(3).map { String($0.key) }.joined(separator: " · ")
            out.append(SectionLayout(key: key, places: top, count: j - i, rows: rows))
            i = j
        }
        sections = out
    }

    private func path(ofIndex index: Int) -> IndexPath? {
        for (s, sec) in sections.enumerated() {
            guard let first = sec.rows.first, index >= first.start, index < first.start + sec.count else { continue }
            var lo = 0, hi = sec.rows.count - 1
            while lo < hi {
                let mid = (lo + hi + 1) / 2
                if sec.rows[mid].start <= index { lo = mid } else { hi = mid - 1 }
            }
            return IndexPath(item: lo, section: s)
        }
        return nil
    }

    private func path(ofPhoto sha: String) -> IndexPath? {
        data.tiles.firstIndex { $0.sha == sha }.flatMap(path(ofIndex:))
    }

    /// The first photo of the topmost visible row, and how far below the top of the screen it is.
    private func topPhoto() -> (sha: String, offset: CGFloat)? {
        guard let path = collection.indexPathsForVisibleItems.min(), let attr = collection.layoutAttributesForItem(at: path),
              path.section < sections.count, path.item < sections[path.section].rows.count else { return nil }
        let i = sections[path.section].rows[path.item].start
        guard i < data.tiles.count else { return nil }
        return (data.tiles[i].sha, attr.frame.minY - collection.contentOffset.y)
    }

    // MARK: where we are: the heading in the bar, and the scrubber

    private func reportTop() {
        // the section of the first row that reaches below the bar
        let edge = collection.contentOffset.y + view.safeAreaInsets.top + topInset + 8
        var key = ""
        if collection.contentOffset.y + collection.adjustedContentInset.top > 8, headed {
            for path in collection.indexPathsForVisibleItems.sorted() {
                guard let frame = collection.layoutAttributesForItem(at: path)?.frame, frame.maxY > edge else { continue }
                if path.section < sections.count { key = sections[path.section].key }
                break
            }
        }
        guard key != topKey else { return }
        topKey = key
        onTop(key.isEmpty ? "" : caption(key))
        scrubber.label = key.isEmpty ? "" : data.runs != nil ? String(key.prefix(18)) : Dates.short(key)
        if scrubber.dragging { Haptics.tick() }
    }

    private var scrollable: CGFloat {
        collection.contentSize.height + collection.adjustedContentInset.top + collection.adjustedContentInset.bottom - collection.bounds.height
    }

    private var track: (top: CGFloat, height: CGFloat) {
        let top = view.safeAreaInsets.top + topInset + 12
        return (top, max(1, view.bounds.height - top - view.safeAreaInsets.bottom - 24 - scrubber.bounds.height))
    }

    private func placeScrubber() {
        let size = scrubber.sizeThatFits(.zero)
        let fraction = scrollable > 0 ? min(max((collection.contentOffset.y + collection.adjustedContentInset.top) / scrollable, 0), 1) : 0
        scrubber.bounds.size = size
        scrubber.center = CGPoint(x: view.bounds.width - size.width / 2 - 6, y: track.top + track.height * fraction + size.height / 2)
    }

    private func showScrubber() {
        guard scrollable > view.bounds.height * 2, headed else { return }      // short lists don't need one
        scrubHide?.cancel()
        if scrubber.alpha < 1 { UIView.animate(withDuration: 0.15) { self.scrubber.alpha = 1 } }
        let hide = DispatchWorkItem { [weak self] in
            guard let self, !self.scrubber.dragging else { return }
            UIView.animate(withDuration: 0.4) { self.scrubber.alpha = 0 }
        }
        scrubHide = hide
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.4, execute: hide)
    }

    func scrollViewDidScroll(_ scrollView: UIScrollView) {
        reportTop()
        placeScrubber()
        if scrollView.isDragging || scrollView.isDecelerating || scrubber.dragging { showScrubber() }
    }

    @objc private func scrubbed(_ pan: UIPanGestureRecognizer) {
        switch pan.state {
        case .began:
            scrubber.dragging = true
            Haptics.tap()
        case .changed:
            let y = pan.location(in: view).y - scrubber.bounds.height / 2
            let fraction = min(max((y - track.top) / track.height, 0), 1)
            collection.setContentOffset(CGPoint(x: 0, y: fraction * scrollable - collection.adjustedContentInset.top), animated: false)
        default:
            scrubber.dragging = false
            showScrubber()
        }
    }

    /// Pinch to make the photos smaller or larger, a step at a time.
    @objc private func pinched(_ pinch: UIPinchGestureRecognizer) {
        guard pinch.state == .changed, pinch.scale > 1.3 || pinch.scale < 0.77 else { return }
        let steps = Estate.densities
        let at = steps.enumerated().min { abs($0.element - rowHeight) < abs($1.element - rowHeight) }?.offset ?? 2
        let next = min(max(at + (pinch.scale > 1 ? 1 : -1), 0), steps.count - 1)
        pinch.scale = 1
        guard next != at else { return }
        Haptics.tap()
        onRowHeight(steps[next])
    }

    // MARK: collection view

    func numberOfSections(in collectionView: UICollectionView) -> Int { sections.count }

    func collectionView(_ collectionView: UICollectionView, numberOfItemsInSection section: Int) -> Int { sections[section].rows.count }

    func collectionView(_ collectionView: UICollectionView, cellForItemAt indexPath: IndexPath) -> UICollectionViewCell {
        let cell = collectionView.dequeueReusableCell(withReuseIdentifier: "row", for: indexPath) as! RowCell
        let row = sections[indexPath.section].rows[indexPath.item]
        cell.show(row.widths, Array(data.tiles[row.start..<row.start + row.widths.count]), first: row.start, height: row.height, inset: row.inset) { [weak self] i in
            self?.open(i)
        }
        if indexPath.section == sections.count - 1, indexPath.item >= sections[indexPath.section].rows.count - 12 { onNearEnd() }
        return cell
    }

    func collectionView(_ collectionView: UICollectionView, layout: UICollectionViewLayout, sizeForItemAt indexPath: IndexPath) -> CGSize {
        CGSize(width: width, height: sections[indexPath.section].rows[indexPath.item].height)
    }

    func collectionView(_ collectionView: UICollectionView, layout: UICollectionViewLayout, referenceSizeForHeaderInSection section: Int) -> CGSize {
        !headed || (data.runs != nil && sections[section].key.isEmpty) ? .zero : CGSize(width: width, height: 62)
    }

    func collectionView(_ collectionView: UICollectionView, layout: UICollectionViewLayout, insetForSectionAt section: Int) -> UIEdgeInsets {
        UIEdgeInsets(top: 0, left: 0, bottom: headed ? 22 : 0, right: 0)
    }

    func collectionView(_ collectionView: UICollectionView, viewForSupplementaryElementOfKind kind: String, at indexPath: IndexPath) -> UICollectionReusableView {
        let head = collectionView.dequeueReusableSupplementaryView(ofKind: kind, withReuseIdentifier: "head", for: indexPath) as! HeaderView
        let sec = sections[indexPath.section]
        head.show(title: caption(sec.key), places: sec.places, count: sec.count)
        return head
    }
}

/// The heading above the first photo: "Library", or what was asked ("white porsche"), and how much is
/// in it. Only a little larger than a month's caption, so a long question wraps onto a second line
/// instead of shrinking or being cut off.
private final class HeadingView: UIView {
    private let title = UILabel(), detail = UILabel()
    private let inset: CGFloat = 16, top: CGFloat = 12, gap: CGFloat = 3, bottom: CGFloat = 8

    override init(frame: CGRect) {
        super.init(frame: frame)
        title.font = Estate.title(28, .bold)
        title.numberOfLines = 2
        title.lineBreakMode = .byTruncatingTail
        detail.font = .preferredFont(forTextStyle: .subheadline)
        detail.textColor = .secondaryLabel
        detail.numberOfLines = 5          // an album says what was asked for under its count
        addSubview(title)
        addSubview(detail)
        isHidden = true
    }

    required init?(coder: NSCoder) { fatalError() }

    /// True if anything changed.
    func show(_ t: String?, _ sub: String) -> Bool {
        guard t != title.text || sub != detail.text || isHidden != (t == nil) else { return false }
        title.text = t
        detail.text = sub
        isHidden = t == nil
        setNeedsLayout()
        return true
    }

    private func sizes(_ width: CGFloat) -> (CGFloat, CGFloat) {
        let w = max(width - inset * 2, 1)
        let t = ceil(title.sizeThatFits(CGSize(width: w, height: .greatestFiniteMagnitude)).height)
        let d = detail.text?.isEmpty ?? true ? 0 : ceil(detail.sizeThatFits(CGSize(width: w, height: .greatestFiniteMagnitude)).height)
        return (t, d)
    }

    func height(for width: CGFloat) -> CGFloat {
        let (t, d) = sizes(width)
        return top + t + (d > 0 ? gap + d : 0) + bottom
    }

    override func layoutSubviews() {
        let (t, d) = sizes(bounds.width)
        title.frame = CGRect(x: inset, y: top, width: bounds.width - inset * 2, height: t)
        detail.frame = CGRect(x: inset + 1, y: top + t + gap, width: bounds.width - inset * 2 - 2, height: d)
    }
}

/// Under a month's photos: the month before on the left, the month after on the right. Each takes this
/// month's place, so Back is still the Months.
private final class MonthNav: UIView {
    var go: (String) -> Void = { _ in }
    private let before = UIButton(type: .system), after = UIButton(type: .system)
    private var keys: (String?, String?) = (nil, nil)

    override init(frame: CGRect) {
        super.init(frame: frame)
        for (b, back) in [(before, true), (after, false)] {
            var c = UIButton.Configuration.gray()
            c.cornerStyle = .capsule
            c.image = UIImage(systemName: back ? "chevron.backward" : "chevron.forward",
                              withConfiguration: UIImage.SymbolConfiguration(pointSize: 12, weight: .bold))
            c.imagePlacement = back ? .leading : .trailing
            c.imagePadding = 7
            c.baseForegroundColor = .label
            c.contentInsets = NSDirectionalEdgeInsets(top: 11, leading: 16, bottom: 11, trailing: 16)
            b.configuration = c
            b.addAction(UIAction { [weak self] _ in
                guard let self, let key = back ? self.keys.0 : self.keys.1 else { return }
                Haptics.tap()
                self.go(key)
            }, for: .touchUpInside)
            addSubview(b)
        }
    }

    required init?(coder: NSCoder) { fatalError() }

    func show(_ f: GridFooter?) {
        keys = (f?.before, f?.after)
        isHidden = f == nil || (f?.before == nil && f?.after == nil)
        for (b, key) in [(before, keys.0), (after, keys.1)] {
            b.isHidden = key == nil
            b.configuration?.attributedTitle = key.map { AttributedString(Dates.heading($0), attributes: AttributeContainer([.font: Estate.title(15)])) }
        }
        setNeedsLayout()
    }

    override func layoutSubviews() {
        let a = before.sizeThatFits(bounds.size), b = after.sizeThatFits(bounds.size)
        before.frame = CGRect(x: 14, y: 18, width: a.width, height: a.height)
        after.frame = CGRect(x: bounds.width - 14 - b.width, y: 18, width: b.width, height: b.height)
    }
}

/// A section's caption: the month (or day), then where and how many.
private final class HeaderView: UICollectionReusableView {
    private let title = UILabel(), detail = UILabel()

    override init(frame: CGRect) {
        super.init(frame: frame)
        title.font = Estate.title(22)
        detail.font = .systemFont(ofSize: 13, weight: .regular)
        detail.textColor = .secondaryLabel
        let stack = UIStackView(arrangedSubviews: [title, detail])
        stack.axis = .vertical
        stack.spacing = 2
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 16),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -16),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -10),
        ])
    }

    required init?(coder: NSCoder) { fatalError() }

    func show(title t: String, places: String, count: Int) {
        title.text = t
        detail.text = [places, "\(count.formatted()) \(count == 1 ? "photo" : "photos")"].filter { !$0.isEmpty }.joined(separator: "  ·  ")
    }
}

/// The handle at the right edge: where you are in the library, and a way to get anywhere in it.
private final class Scrubber: UIView {
    private let text = UILabel()
    private let grip = UIImageView(image: UIImage(systemName: "chevron.up.chevron.down",
                                                  withConfiguration: UIImage.SymbolConfiguration(pointSize: 11, weight: .bold)))
    private let glass = UIVisualEffectView(effect: UIGlassEffect())

    var label = "" { didSet { if label != oldValue { text.text = label; resize() } } }
    var dragging = false { didSet { if dragging != oldValue { resize() } } }

    override init(frame: CGRect) {
        super.init(frame: frame)
        glass.clipsToBounds = true
        addSubview(glass)
        text.font = Estate.title(15)
        text.textColor = .label
        grip.tintColor = .secondaryLabel
        grip.contentMode = .center
        glass.contentView.addSubview(text)
        glass.contentView.addSubview(grip)
        isAccessibilityElement = true
        accessibilityLabel = "Scroll through the library"
        accessibilityTraits = .adjustable
    }

    required init?(coder: NSCoder) { fatalError() }

    override func sizeThatFits(_ size: CGSize) -> CGSize {
        let w = label.isEmpty ? 0 : ceil(text.intrinsicContentSize.width) + 18
        return CGSize(width: 36 + w, height: dragging ? 44 : 38)
    }

    private func resize() {
        UIView.animate(withDuration: 0.18) {
            self.bounds.size = self.sizeThatFits(.zero)
            self.superview?.setNeedsLayout()
            self.superview?.layoutIfNeeded()
            self.layoutSubviews()
        }
    }

    override func layoutSubviews() {
        glass.frame = bounds
        glass.layer.cornerRadius = bounds.height / 2
        text.frame = CGRect(x: 15, y: 0, width: max(0, bounds.width - 42), height: bounds.height)
        grip.frame = CGRect(x: bounds.width - 30, y: 0, width: 26, height: bounds.height)
    }

    // generous to the thumb
    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool { bounds.insetBy(dx: -14, dy: -10).contains(point) }
}

/// The count on the top photo of a stack.
private final class StackBadge: UIView {
    private let icon = UIImageView(image: UIImage(systemName: "square.stack.fill",
                                                  withConfiguration: UIImage.SymbolConfiguration(pointSize: 9, weight: .semibold)))
    private let label = UILabel()
    var count = 0 { didSet { label.text = String(count) } }

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = UIColor.black.withAlphaComponent(0.55)
        isUserInteractionEnabled = false
        icon.tintColor = .white
        icon.contentMode = .center
        label.textColor = .white
        label.font = .monospacedDigitSystemFont(ofSize: 11, weight: .semibold)
        addSubview(icon)
        addSubview(label)
    }

    required init?(coder: NSCoder) { fatalError() }

    override func sizeThatFits(_ size: CGSize) -> CGSize {
        CGSize(width: 24 + ceil(label.intrinsicContentSize.width), height: 18)
    }

    override func layoutSubviews() {
        layer.cornerRadius = bounds.height / 2
        icon.frame = CGRect(x: 4, y: 0, width: 14, height: bounds.height)
        label.frame = CGRect(x: 19, y: 0, width: max(0, bounds.width - 23), height: bounds.height)
    }
}

/// One photo in a row.
private final class TileView: UIImageView {
    private var sha = ""
    private var task: Task<Void, Never>?
    private let heart = UIImageView(image: UIImage(systemName: "heart.fill"))
    private let mine = UIImageView(image: UIImage(systemName: "arrow.up.circle.fill"))   // on this iPhone, not yet on the server
    private let stack = StackBadge()      // the top of a stack: how many shots it holds
    var index = 0

    override init(frame: CGRect) {
        super.init(frame: frame)
        contentMode = .scaleAspectFill
        clipsToBounds = true
        backgroundColor = Estate.well
        isUserInteractionEnabled = true
        isAccessibilityElement = true
        accessibilityTraits = [.image, .button]
        heart.tintColor = Estate.sunLit
        heart.layer.shadowOpacity = 0.45
        heart.layer.shadowRadius = 2
        heart.layer.shadowOffset = .zero
        heart.contentMode = .scaleAspectFit
        addSubview(heart)
        mine.tintColor = .white
        mine.layer.shadowOpacity = 0.5
        mine.layer.shadowRadius = 2
        mine.layer.shadowOffset = .zero
        mine.contentMode = .scaleAspectFit
        addSubview(mine)
        addSubview(stack)
    }

    required init?(coder: NSCoder) { fatalError() }

    override func layoutSubviews() {
        super.layoutSubviews()
        heart.frame = CGRect(x: 6, y: bounds.height - 19, width: 14, height: 13)
        mine.frame = CGRect(x: bounds.width - 20, y: bounds.height - 20, width: 15, height: 15)
        let s = stack.sizeThatFits(.zero)
        stack.frame = CGRect(x: 5, y: 5, width: s.width, height: s.height)
    }

    func show(_ tile: Tile, index i: Int) {
        index = i
        heart.isHidden = !tile.fav
        mine.isHidden = !tile.isLocal
        stack.isHidden = tile.stack < 2
        if !stack.isHidden { stack.count = Int(tile.stack); setNeedsLayout() }
        accessibilityLabel = [Dates.long(tile.when), tile.city?.split(separator: ",").first.map(String.init), tile.fav ? "favourite" : nil,
                              stack.isHidden ? nil : "stack of \(tile.stack)", tile.isLocal ? "not yet on the server" : nil]
            .compactMap { $0 }.joined(separator: ", ")
        guard tile.sha != sha || image == nil else { return }
        task?.cancel()
        sha = tile.sha
        if let ready = ImageStore.shared.ready(tile.sha) {
            image = ready
            return
        }
        image = nil
        let want = tile.sha
        task = Task { [weak self] in
            let img = await ImageStore.shared.thumb(want)
            guard !Task.isCancelled, let self, self.sha == want, let img else { return }
            UIView.transition(with: self, duration: 0.2, options: [.transitionCrossDissolve, .allowUserInteraction]) { self.image = img }
        }
    }

    func clear() { task?.cancel() }

    // a photo gives a little under the finger
    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        super.touchesBegan(touches, with: event)
        UIView.animate(withDuration: 0.12, delay: 0, options: [.allowUserInteraction, .beginFromCurrentState]) { self.alpha = 0.82 }
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        super.touchesEnded(touches, with: event)
        UIView.animate(withDuration: 0.25, delay: 0, options: [.allowUserInteraction, .beginFromCurrentState]) { self.alpha = 1 }
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        super.touchesCancelled(touches, with: event)
        UIView.animate(withDuration: 0.25, delay: 0, options: [.allowUserInteraction, .beginFromCurrentState]) { self.alpha = 1 }
    }
}

private final class RowCell: UICollectionViewCell {
    private var tiles: [TileView] = []
    private var used = 0
    private var open: (Int) -> Void = { _ in }

    func show(_ widths: [CGFloat], _ photos: [Tile], first: Int, height: CGFloat, inset: CGFloat, open: @escaping (Int) -> Void) {
        self.open = open
        used = widths.count
        while tiles.count < widths.count {
            let t = TileView(frame: .zero)
            t.addGestureRecognizer(UITapGestureRecognizer(target: self, action: #selector(tapped(_:))))
            contentView.addSubview(t)
            tiles.append(t)
        }
        var x = inset
        for (i, t) in tiles.enumerated() {
            t.isHidden = i >= widths.count
            guard i < widths.count else { t.clear(); continue }
            t.alpha = 1
            t.frame = CGRect(x: x, y: 0, width: widths[i], height: height)
            t.show(photos[i], index: first + i)
            x += widths[i] + Estate.gap
        }
    }

    func tile(_ index: Int) -> UIView? { tiles.prefix(used).first { $0.index == index } }

    @objc private func tapped(_ g: UITapGestureRecognizer) {
        if let t = g.view as? TileView { open(t.index) }
    }

    override func prepareForReuse() {
        super.prepareForReuse()
        tiles.forEach { $0.clear() }
    }
}
