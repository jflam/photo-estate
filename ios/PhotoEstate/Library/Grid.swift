import SwiftUI
import UIKit

/// The justified grid, as in the web app (chapter 13 of the book): rows as wide as the screen, every
/// photo at its own shape. Each row is one collection-view cell, so a library of 100,000 photos is some
/// 30,000 cells of known height: the layout is arithmetic, and scrolling never waits on it.
struct GridView: UIViewControllerRepresentable {
    let data: GridData
    let rowHeight: CGFloat
    var onOpen: (Int) -> Void
    var onNearEnd: () -> Void
    var onRefresh: () async -> Void
    let reveal: Int?            // keep this photo on screen (the viewer moved to it)
    var topInset: CGFloat = 0   // the filter bar drawn over the top of the grid

    func makeUIViewController(context: Context) -> GridController { GridController() }

    func updateUIViewController(_ c: GridController, context: Context) {
        c.onOpen = onOpen
        c.onNearEnd = onNearEnd
        c.onRefresh = onRefresh
        c.setTopInset(topInset)
        c.show(data, rowHeight: rowHeight)
        if let reveal { c.reveal(reveal) }
    }
}

private struct RowLayout {
    let start: Int               // index of the row's first photo in `tiles`
    let widths: [CGFloat]
    let height: CGFloat
}

private struct SectionLayout {
    let key: String
    let places: String
    let count: Int
    var rows: [RowLayout]
}

final class GridController: UIViewController, UICollectionViewDataSource, UICollectionViewDelegateFlowLayout {
    var onOpen: (Int) -> Void = { _ in }
    var onNearEnd: () -> Void = {}
    var onRefresh: () async -> Void = {}

    private static let gap: CGFloat = 2
    private var data = GridData()
    private var rowHeight: CGFloat = 128
    private var sections: [SectionLayout] = []
    private var width: CGFloat = 0
    private var revealed: Int?
    private lazy var collection: UICollectionView = {
        let layout = UICollectionViewFlowLayout()
        layout.minimumLineSpacing = GridController.gap
        layout.minimumInteritemSpacing = 0
        layout.sectionHeadersPinToVisibleBounds = false
        let v = UICollectionView(frame: .zero, collectionViewLayout: layout)
        v.backgroundColor = .systemBackground
        v.dataSource = self
        v.delegate = self
        v.alwaysBounceVertical = true
        v.register(RowCell.self, forCellWithReuseIdentifier: "row")
        v.register(HeaderView.self, forSupplementaryViewOfKind: UICollectionView.elementKindSectionHeader, withReuseIdentifier: "head")
        let refresh = UIRefreshControl()
        refresh.addAction(UIAction { [weak self] _ in
            Task { await self?.onRefresh(); refresh.endRefreshing() }
        }, for: .valueChanged)
        v.refreshControl = refresh
        return v
    }()

    override func loadView() { view = collection }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        if abs(collection.bounds.width - width) > 0.5 {
            width = collection.bounds.width
            layout()
            collection.reloadData()
        }
    }

    func show(_ new: GridData, rowHeight h: CGFloat) {
        guard new.id != data.id || h != rowHeight else { return }
        let reset = new.reset != data.reset
        let grew = !reset && h == rowHeight && new.grain == 0 && data.grain == 0 && new.tiles.count > data.tiles.count
        // Refreshed in place (a sync, or a setting): keep looking at the same photo.
        let anchor = reset || grew ? nil : topPhoto()
        data = new
        rowHeight = h
        guard width > 0 else { return }
        layout()
        collection.reloadData()
        if reset {
            collection.setContentOffset(CGPoint(x: 0, y: -collection.adjustedContentInset.top), animated: false)
        } else if let anchor, let path = path(ofPhoto: anchor.sha) {
            collection.layoutIfNeeded()
            if let attr = collection.layoutAttributesForItem(at: path) {
                collection.setContentOffset(CGPoint(x: 0, y: max(-collection.adjustedContentInset.top, attr.frame.minY - anchor.offset)), animated: false)
            }
        }
    }

    func setTopInset(_ inset: CGFloat) {
        guard abs(collection.contentInset.top - inset) > 0.5 else { return }
        let atTop = collection.contentOffset.y <= -collection.adjustedContentInset.top + 1
        collection.contentInset.top = inset
        collection.verticalScrollIndicatorInsets.top = inset
        if atTop { collection.setContentOffset(CGPoint(x: 0, y: -collection.adjustedContentInset.top), animated: false) }
    }

    /// Scroll just enough to bring a photo on screen.
    func reveal(_ index: Int) {
        guard revealed != index, width > 0 else { return }
        revealed = index
        guard let path = path(ofIndex: index), !collection.indexPathsForVisibleItems.contains(path) else { return }
        collection.scrollToItem(at: path, at: .centeredVertically, animated: false)
    }

    // MARK: layout

    private func layout() {
        let gap = GridController.gap, W = width
        let H = min(rowHeight, max(96, W / 2.4))
        var out: [SectionLayout] = []
        let tiles = data.tiles
        var i = 0
        while i < tiles.count {
            // one section: photos sharing the first `grain` characters of their time
            let key = data.grain == 0 ? "" : String(tiles[i].when.prefix(data.grain))
            var j = i
            var cities: [Substring: Int] = [:]
            while j < tiles.count, data.grain == 0 || tiles[j].when.prefix(data.grain) == key {
                if data.grain > 0, let c = tiles[j].city?.split(separator: ",").first { cities[c, default: 0] += 1 }
                j += 1
            }
            var rows: [RowLayout] = []
            var start = i, sum: CGFloat = 0
            for k in i..<j {
                sum += CGFloat(min(max(tiles[k].ar, 0.5), 2.6))
                let n = k - start + 1
                let gaps = CGFloat(n - 1) * gap
                let last = k == j - 1
                if sum * H + gaps >= W || last {
                    // a full row shrinks to fit exactly; a short last row stays at H unless it is nearly full
                    let fits = sum * H + gaps >= W * (last ? 0.9 : 1)
                    let h = fits ? (W - gaps) / sum : H
                    var widths = (start...k).map { (CGFloat(min(max(tiles[$0].ar, 0.5), 2.6)) * h).rounded() }
                    if fits {      // rounding left a pixel or two: give them to the widest photos
                        var spare = W - gaps - widths.reduce(0, +)
                        var m = 0
                        while abs(spare) >= 1, !widths.isEmpty {
                            widths[m % widths.count] += spare > 0 ? 1 : -1
                            spare += spare > 0 ? -1 : 1
                            m += 1
                        }
                    }
                    rows.append(RowLayout(start: start, widths: widths, height: h.rounded()))
                    start = k + 1
                    sum = 0
                }
            }
            let top = cities.sorted { $0.value > $1.value }.prefix(3).map { String($0.key) }.joined(separator: " · ")
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

    // MARK: collection view

    func numberOfSections(in collectionView: UICollectionView) -> Int { sections.count }

    func collectionView(_ collectionView: UICollectionView, numberOfItemsInSection section: Int) -> Int { sections[section].rows.count }

    func collectionView(_ collectionView: UICollectionView, cellForItemAt indexPath: IndexPath) -> UICollectionViewCell {
        let cell = collectionView.dequeueReusableCell(withReuseIdentifier: "row", for: indexPath) as! RowCell
        let row = sections[indexPath.section].rows[indexPath.item]
        cell.show(row.widths, Array(data.tiles[row.start..<row.start + row.widths.count]), first: row.start, height: row.height, gap: GridController.gap) { [weak self] i in
            self?.onOpen(i)
        }
        if indexPath.section == sections.count - 1, indexPath.item >= sections[indexPath.section].rows.count - 12 { onNearEnd() }
        return cell
    }

    func collectionView(_ collectionView: UICollectionView, layout: UICollectionViewLayout, sizeForItemAt indexPath: IndexPath) -> CGSize {
        CGSize(width: width, height: sections[indexPath.section].rows[indexPath.item].height)
    }

    func collectionView(_ collectionView: UICollectionView, layout: UICollectionViewLayout, referenceSizeForHeaderInSection section: Int) -> CGSize {
        data.grain == 0 ? .zero : CGSize(width: width, height: 54)
    }

    func collectionView(_ collectionView: UICollectionView, layout: UICollectionViewLayout, insetForSectionAt section: Int) -> UIEdgeInsets {
        UIEdgeInsets(top: 0, left: 0, bottom: data.grain == 0 ? 0 : 10, right: 0)
    }

    func collectionView(_ collectionView: UICollectionView, viewForSupplementaryElementOfKind kind: String, at indexPath: IndexPath) -> UICollectionReusableView {
        let head = collectionView.dequeueReusableSupplementaryView(ofKind: kind, withReuseIdentifier: "head", for: indexPath) as! HeaderView
        let sec = sections[indexPath.section]
        head.show(title: Dates.heading(sec.key), places: sec.places, count: sec.count)
        return head
    }
}

private final class HeaderView: UICollectionReusableView {
    private let title = UILabel(), detail = UILabel()

    override init(frame: CGRect) {
        super.init(frame: frame)
        title.font = .preferredFont(forTextStyle: .headline)
        detail.font = .preferredFont(forTextStyle: .footnote)
        detail.textColor = .secondaryLabel
        let stack = UIStackView(arrangedSubviews: [title, detail])
        stack.axis = .vertical
        stack.spacing = 1
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -14),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -7),
        ])
    }

    required init?(coder: NSCoder) { fatalError() }

    func show(title t: String, places: String, count: Int) {
        title.text = t
        detail.text = [places, "\(count.formatted()) \(count == 1 ? "photo" : "photos")"].filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

/// One photo in a row.
private final class TileView: UIImageView {
    private var sha = ""
    private var task: Task<Void, Never>?
    private let heart = UIImageView(image: UIImage(systemName: "heart.fill"))
    var index = 0

    override init(frame: CGRect) {
        super.init(frame: frame)
        contentMode = .scaleAspectFill
        clipsToBounds = true
        backgroundColor = .secondarySystemBackground
        isUserInteractionEnabled = true
        isAccessibilityElement = true
        accessibilityTraits = [.image, .button]
        heart.tintColor = .white
        heart.layer.shadowOpacity = 0.5
        heart.layer.shadowRadius = 2
        heart.layer.shadowOffset = .zero
        heart.frame = CGRect(x: 5, y: 5, width: 14, height: 13)
        heart.contentMode = .scaleAspectFit
        addSubview(heart)
    }

    required init?(coder: NSCoder) { fatalError() }

    func show(_ tile: Tile, index i: Int) {
        index = i
        heart.isHidden = !tile.fav
        accessibilityLabel = [Dates.long(tile.when), tile.city?.split(separator: ",").first.map(String.init)].compactMap { $0 }.joined(separator: ", ")
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
            UIView.transition(with: self, duration: 0.18, options: [.transitionCrossDissolve, .allowUserInteraction]) { self.image = img }
        }
    }

    func clear() {
        task?.cancel()
    }
}

private final class RowCell: UICollectionViewCell {
    private var tiles: [TileView] = []
    private var open: (Int) -> Void = { _ in }

    func show(_ widths: [CGFloat], _ photos: [Tile], first: Int, height: CGFloat, gap: CGFloat, open: @escaping (Int) -> Void) {
        self.open = open
        while tiles.count < widths.count {
            let t = TileView(frame: .zero)
            t.addGestureRecognizer(UITapGestureRecognizer(target: self, action: #selector(tapped(_:))))
            contentView.addSubview(t)
            tiles.append(t)
        }
        var x: CGFloat = 0
        for (i, t) in tiles.enumerated() {
            t.isHidden = i >= widths.count
            guard i < widths.count else { t.clear(); continue }
            t.frame = CGRect(x: x, y: 0, width: widths[i], height: height)
            t.show(photos[i], index: first + i)
            x += widths[i] + gap
        }
    }

    @objc private func tapped(_ g: UITapGestureRecognizer) {
        if let t = g.view as? TileView { open(t.index) }
    }

    override func prepareForReuse() {
        super.prepareForReuse()
        tiles.forEach { $0.clear() }
    }
}
