import SwiftUI
import UIKit

/// The app's look, in one place. It comes from the icon: ink, paper, and a low sun.
///
/// - Photos are the interface, so surfaces are quiet: warm paper in the light, near-black ink in the dark.
/// - One colour, the sun, marks what is chosen or alive. Nothing else is coloured.
/// - Words that name a time or a place (a month, a trip, a date) are set in the serif, like captions
///   under prints. Controls stay in the system face.
enum Estate {
    static let paper = UIColor { $0.userInterfaceStyle == .dark ? UIColor(red: 0.035, green: 0.035, blue: 0.04, alpha: 1)
                                                               : UIColor(red: 0.972, green: 0.964, blue: 0.945, alpha: 1) }
    static let well = UIColor { $0.userInterfaceStyle == .dark ? UIColor(white: 1, alpha: 0.06) : UIColor(red: 0.2, green: 0.15, blue: 0.05, alpha: 0.06) }
    /// The sun: amber on ink, and burnt a little on paper so it still reads as text.
    static let sun = UIColor { $0.userInterfaceStyle == .dark ? UIColor(red: 1, green: 0.69, blue: 0.38, alpha: 1)
                                                             : UIColor(red: 0.78, green: 0.41, blue: 0.08, alpha: 1) }
    static let sunLit = UIColor(red: 1, green: 0.69, blue: 0.38, alpha: 1)      // the sun itself, on any ground
    static let gap: CGFloat = 3
    static let densities: [Double] = [64, 96, 128, 180, 260]                   // row heights, pinch steps between them

    static func serif(_ size: CGFloat, _ weight: UIFont.Weight = .semibold) -> UIFont {
        let base = UIFont.systemFont(ofSize: size, weight: weight)
        return UIFont(descriptor: base.fontDescriptor.withDesign(.serif) ?? base.fontDescriptor, size: size)
    }
}

extension Color {
    static let paper = Color(uiColor: Estate.paper)
    static let well = Color(uiColor: Estate.well)
    static let sun = Color(uiColor: Estate.sun)
    static let sunLit = Color(uiColor: Estate.sunLit)
}

extension Font {
    /// The caption face: for months, trips, dates, places.
    static func serif(_ size: CGFloat, _ weight: Font.Weight = .semibold) -> Font { .system(size: size, weight: weight, design: .serif) }
}

/// The icon's picture, drawn: hills under a low sun. Used where there are no photos to show.
struct EstateMark: View {
    var progress: Double? = nil        // 0...1: the sun rises as a fetch goes on

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width, h = geo.size.height
            ZStack {
                Circle()
                    .fill(Color.sunLit)
                    .frame(width: w * 0.2, height: w * 0.2)
                    .position(x: w * 0.68, y: h * (0.62 - 0.36 * (progress ?? 1)))
                    .animation(.easeOut(duration: 0.6), value: progress)
                Path { p in
                    p.move(to: CGPoint(x: 0, y: h))
                    p.addLine(to: CGPoint(x: w * 0.33, y: h * 0.38))
                    p.addLine(to: CGPoint(x: w * 0.56, y: h * 0.74))
                    p.addLine(to: CGPoint(x: w * 0.69, y: h * 0.56))
                    p.addLine(to: CGPoint(x: w, y: h))
                    p.closeSubpath()
                }
                .fill(Color.primary)
            }
        }
        .aspectRatio(1.5, contentMode: .fit)
        .clipped()
        .accessibilityHidden(true)
    }
}

/// A round control on glass, for use over photos.
struct GlassCircle: ViewModifier {
    func body(content: Content) -> some View {
        content
            .font(.system(size: 17, weight: .semibold))
            .frame(width: 46, height: 46)
            .contentShape(Circle())
            .glassEffect(.regular.interactive(), in: .circle)
    }
}

enum Haptics {
    static func tick() { UISelectionFeedbackGenerator().selectionChanged() }
    static func tap() { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
}
