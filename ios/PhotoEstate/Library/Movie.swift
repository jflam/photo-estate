import AVFoundation
import SwiftUI
import UIKit

/// Videos and Live Photos in the viewer. A video is never kept on the phone: it plays from the server's
/// `/video/<sha>`, which answers byte ranges, so AVPlayer streams it straight from the address. Its page
/// shows the poster (the video's preview, fetched like any photo's) until the picture comes, and the
/// controls are in the viewer's bottom bar, outside the pager, so dragging the playhead never turns the
/// page. A Live Photo's clip plays the same way, over its still, while the photo is held.

/// A player drawn in a view: the picture of a video, or of a Live Photo's clip, over its still. It lives
/// inside the page's image view, so it zooms and pans with it.
final class MovieView: UIView {
    override class var layerClass: AnyClass { AVPlayerLayer.self }
    private var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }
    private(set) var player: AVPlayer?
    private var watching: [NSKeyValueObservation] = []
    private var ticks: Any?
    private var ending: NSObjectProtocol?

    var onReady: () -> Void = {}                  // it can play
    var onFailed: () -> Void = {}                 // it can't: the server is away, or the file is not a video it can read
    var onWaiting: (Bool) -> Void = { _ in }      // asked to play and waiting for the network
    var onPlaying: (Bool) -> Void = { _ in }
    var onTime: (Double) -> Void = { _ in }
    var onSize: (CGSize) -> Void = { _ in }       // the picture's own shape, once known
    var onEnd: () -> Void = {}

    /// `fill`: a Live Photo's clip covers its still exactly, as Photos draws it, even when the two were
    /// saved at slightly different shapes. A video is fitted whole.
    init(fill: Bool = false) {
        super.init(frame: .zero)
        playerLayer.videoGravity = fill ? .resizeAspectFill : .resizeAspect
        isUserInteractionEnabled = false
        backgroundColor = .clear
    }

    required init?(coder: NSCoder) { fatalError() }

    var isReady: Bool { player?.currentItem?.status == .readyToPlay }
    var length: Double { player?.currentItem.map { $0.duration.seconds }.flatMap { $0.isFinite ? $0 : nil } ?? 0 }

    func load(_ item: AVPlayerItem) {
        release()
        // A video is something asked to play, so it sounds even with the phone's ring switch on silent, as
        // in Photos: an app's default audio session is silenced by that switch. A Live Photo's clip (`fill`)
        // leaves the session as it is.
        if playerLayer.videoGravity == .resizeAspect {
            try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
        }
        let p = AVPlayer(playerItem: item)
        p.actionAtItemEnd = .pause
        player = p
        playerLayer.player = p
        watching = [
            item.observe(\.status) { [weak self] item, _ in
                let status = item.status
                Task { @MainActor in
                    if status == .readyToPlay { self?.onReady() } else if status == .failed { self?.onFailed() }
                }
            },
            item.observe(\.presentationSize) { [weak self] item, _ in
                let size = item.presentationSize
                Task { @MainActor in if size.width > 0, size.height > 0 { self?.onSize(size) } }
            },
            p.observe(\.timeControlStatus) { [weak self] p, _ in
                let status = p.timeControlStatus
                Task { @MainActor in
                    self?.onPlaying(status == .playing)
                    self?.onWaiting(status == .waitingToPlayAtSpecifiedRate)
                }
            },
        ]
        ticks = p.addPeriodicTimeObserver(forInterval: CMTime(value: 1, timescale: 10), queue: .main) { [weak self] t in
            MainActor.assumeIsolated { self?.onTime(t.seconds) }
        }
        ending = NotificationCenter.default.addObserver(forName: AVPlayerItem.didPlayToEndTimeNotification, object: item, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.onEnd() }
        }
    }

    /// Stop, and let go of the player and what it has loaded.
    func release() {
        player?.pause()
        if let ticks { player?.removeTimeObserver(ticks) }
        if let ending { NotificationCenter.default.removeObserver(ending) }
        watching.forEach { $0.invalidate() }
        watching = []
        ticks = nil
        ending = nil
        playerLayer.player = nil
        player = nil
    }
}

/// The video on the page that is up, as its controls in the bottom bar see it. The page attaches its
/// player when it becomes the current page and detaches it when it goes.
@MainActor
final class Playback: ObservableObject {
    @Published private(set) var sha: String?
    @Published private(set) var playing = false
    @Published private(set) var time: Double = 0
    @Published private(set) var length: Double = 0
    @Published private(set) var ready = false
    private weak var movie: MovieView?
    private var scrubbing = false

    func attach(_ sha: String, _ movie: MovieView, length: Double) {
        self.sha = sha
        self.movie = movie
        self.length = length
        time = 0
        playing = false
        ready = false
    }

    func detach(_ sha: String) {
        guard self.sha == sha else { return }
        self.sha = nil
        movie = nil
        playing = false
    }

    // what the page reports
    func update(_ sha: String, playing: Bool? = nil, time: Double? = nil, length: Double? = nil, ready: Bool? = nil) {
        guard self.sha == sha else { return }
        if let playing, playing != self.playing { self.playing = playing }
        if let time, !scrubbing, abs(time - self.time) > 0.05 { self.time = time }
        if let length, length > 0, length != self.length { self.length = length }
        if let ready, ready != self.ready { self.ready = ready }
    }

    // what the controls ask
    func toggle() {
        guard let p = movie?.player else { return }
        if p.timeControlStatus != .paused {
            p.pause()
        } else {
            if length > 0, time >= length - 0.25 { p.seek(to: .zero) }      // from the start again, as Photos does
            p.play()
        }
    }

    func scrub(to t: Double, done: Bool) {
        scrubbing = !done
        time = t
        movie?.player?.seek(to: CMTime(seconds: t, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
    }
}

/// A video's controls: play or pause, how far in, and a playhead to drag.
struct VideoBar: View {
    @ObservedObject var playback: Playback
    let sha: String
    var dur: Float          // the length the catalog gives, until the player knows

    var body: some View {
        let mine = playback.sha == sha
        let length = mine && playback.length > 0 ? playback.length : Double(dur)
        let time = mine ? min(playback.time, length) : 0
        HStack(spacing: 10) {
            Button { playback.toggle() } label: {
                Image(systemName: mine && playback.playing ? "pause.fill" : "play.fill")
                    .font(.system(size: 17, weight: .semibold)).frame(width: 34, height: 34).contentShape(Rectangle())
                    .contentTransition(.symbolEffect(.replace))
            }
            .accessibilityLabel(mine && playback.playing ? "Pause" : "Play")
            Text(Tile.clock(Float(time))).font(.caption.monospacedDigit()).frame(minWidth: 34)
            Slider(value: Binding(get: { time }, set: { playback.scrub(to: $0, done: false) }), in: 0...max(length, 0.1)) { editing in
                if !editing { playback.scrub(to: playback.time, done: true) }
            }
            .accessibilityLabel("Playhead")
            Text("-" + Tile.clock(Float(max(length - time, 0)))).font(.caption.monospacedDigit()).frame(minWidth: 40)
        }
        .disabled(!mine || !playback.ready)
        .padding(.horizontal, 12)
        .frame(width: 340, height: 46)
        .glassEffect(.regular.interactive(), in: .capsule)
        .foregroundStyle(.white)
    }
}

/// A short line over the photo when a video can't play: what is wrong, plainly. No spinner forever.
final class MovieNote: UIView {
    private let glass = UIVisualEffectView(effect: UIGlassEffect())
    private let label = UILabel()
    private let icon = UIImageView(image: UIImage(systemName: "wifi.slash", withConfiguration: UIImage.SymbolConfiguration(pointSize: 15, weight: .semibold)))

    var text = "" { didSet { label.text = text; setNeedsLayout() } }

    override init(frame: CGRect) {
        super.init(frame: frame)
        isUserInteractionEnabled = false
        glass.clipsToBounds = true
        addSubview(glass)
        label.font = .systemFont(ofSize: 15, weight: .medium)
        label.textColor = .white
        label.numberOfLines = 0
        label.textAlignment = .center
        icon.tintColor = Estate.sunLit
        icon.contentMode = .center
        glass.contentView.addSubview(icon)
        glass.contentView.addSubview(label)
    }

    required init?(coder: NSCoder) { fatalError() }

    override func sizeThatFits(_ size: CGSize) -> CGSize {
        let s = label.sizeThatFits(CGSize(width: min(size.width, 320) - 32, height: .greatestFiniteMagnitude))
        return CGSize(width: min(size.width, 320), height: ceil(s.height) + 52)
    }

    override func layoutSubviews() {
        glass.frame = bounds
        glass.layer.cornerRadius = 22
        icon.frame = CGRect(x: 0, y: 12, width: bounds.width, height: 18)
        label.frame = CGRect(x: 16, y: 36, width: bounds.width - 32, height: bounds.height - 48)
    }
}

extension Server {
    /// Where a video (or a Live Photo's clip, by the clip's own sha) streams from.
    static func video(_ sha: String) -> URL { baseURL.appendingPathComponent("video/\(sha)") }
}
