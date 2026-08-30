import Charts
import SwiftUI
import UIKit

// MARK: - Left-edge swipe-back overlay for screens with hidden nav bar.
//
// SwiftUI's NavigationStack is backed by a UINavigationController, but when
// we hide the navigation bar (`.toolbar(.hidden, for: .navigationBar)`) UIKit
// silently disables the interactive pop gesture, AND a wrapping ScrollView
// eagerly grabs every pan inside the content area, so any attempt to swap
// the `interactivePopGestureRecognizer`'s delegate ends up losing to the
// scroll view's pan.
//
// Cleanest reliable workaround: stamp a thin invisible UIKit overlay along
// the very left edge of the screen. The overlay hit-tests `nil` for touches
// outside the edge strip (so scrolling and taps go through), and owns a
// `UIScreenEdgePanGestureRecognizer` that triggers `dismiss()` on swipe.
//
// Apply with `.swipeBackOverlay { dismiss() }` on any pushed screen.

private final class EdgeStripView: UIView {
    var edgeWidth: CGFloat = 24
    override func hitTest(_ point: CGPoint, with event: UIEvent?) -> UIView? {
        // Only own touches that originate within the leftmost edgeWidth pt.
        // Everything else is invisible to UIKit so the scroll view / buttons
        // underneath behave normally.
        point.x < edgeWidth ? super.hitTest(point, with: event) : nil
    }
}

private struct SwipeBackOverlay: UIViewRepresentable {
    let onTrigger: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onTrigger: onTrigger) }

    func makeUIView(context: Context) -> EdgeStripView {
        let view = EdgeStripView()
        view.backgroundColor = .clear
        let pan = UIScreenEdgePanGestureRecognizer(
            target: context.coordinator,
            action: #selector(Coordinator.handle(_:))
        )
        pan.edges = .left
        view.addGestureRecognizer(pan)
        return view
    }

    func updateUIView(_ uiView: EdgeStripView, context: Context) {
        context.coordinator.onTrigger = onTrigger
    }

    final class Coordinator: NSObject {
        var onTrigger: () -> Void
        private var didFire = false
        init(onTrigger: @escaping () -> Void) { self.onTrigger = onTrigger }

        @objc func handle(_ g: UIScreenEdgePanGestureRecognizer) {
            switch g.state {
            case .began:
                didFire = false
            case .changed:
                // Fire as soon as the swipe travels past a clear threshold so
                // it feels immediate (matches UIKit's pop animation timing).
                if !didFire {
                    let dx = g.translation(in: g.view).x
                    if dx > 40 {
                        didFire = true
                        onTrigger()
                    }
                }
            default:
                break
            }
        }
    }
}

extension View {
    /// Adds an invisible left-edge swipe-back affordance. The closure runs
    /// when the user drags inward from the left screen edge — typically
    /// `dismiss()` from the screen's `@Environment(\.dismiss)`.
    func swipeBackOverlay(_ action: @escaping () -> Void) -> some View {
        overlay(alignment: .leading) {
            SwipeBackOverlay(onTrigger: action)
                .frame(width: 24)
                .frame(maxHeight: .infinity)
                .ignoresSafeArea()
        }
    }
}

// MARK: - Design System

enum DesignPalette {
    // Ink ramp — cooler, graphite
    static let ink = Color(red: 0.055, green: 0.059, blue: 0.071)  // #0E0F12
    static let ink2 = Color(red: 0.180, green: 0.192, blue: 0.220)  // #2E3138
    static let ink3 = Color(red: 0.431, green: 0.447, blue: 0.482)  // #6E727B
    static let ink4 = Color(red: 0.659, green: 0.675, blue: 0.706)  // #A8ACB4
    static let ink5 = Color(red: 0.839, green: 0.847, blue: 0.867)  // #D6D8DD

    // Paper — cool off-white
    static let paper = Color(red: 0.949, green: 0.937, blue: 0.910)  // #F2F0EC
    static let paper2 = Color(red: 0.910, green: 0.902, blue: 0.882)  // #E8E6E1

    // Signals
    static let ok = Color(red: 0.122, green: 0.616, blue: 0.420)  // #1F9D6B
    static let warn = Color(red: 0.847, green: 0.576, blue: 0.141)  // #D89324
    static let bad = Color(red: 0.863, green: 0.282, blue: 0.282)  // #DC4848
    static let sep = Color.black.opacity(0.08)

    static let effortEasy = Color(red: 0.851, green: 0.957, blue: 0.871)
    static let effortOk = Color(red: 0.984, green: 0.945, blue: 0.839)
    static let effortHard = Color(red: 0.980, green: 0.839, blue: 0.839)

    // Accent (slightly deeper than before)
    static let accent = Color(red: 1.0, green: 0.302, blue: 0.122)  // #FF4D1F
    static let accentSoft = Color(red: 1.0, green: 0.910, blue: 0.871)  // #FFE8DE
    static let accentDeep = Color(red: 0.784, green: 0.212, blue: 0.039)  // #C8360A
}

// One consistent press feedback for action buttons across the app: a quick
// spring scale-down with a touch of dimming. Use via `.buttonStyle(.pressable)`.
// A short tap holds the pressed look for a minimum time so it's actually
// visible (otherwise isPressed flips back before the spring travels).
struct PressableScaleStyle: ButtonStyle {
    var scale: CGFloat = 0.9
    func makeBody(configuration: Configuration) -> some View {
        PressBody(configuration: configuration, scale: scale)
    }

    private struct PressBody: View {
        let configuration: Configuration
        let scale: CGFloat
        @State private var held = false
        private var down: Bool { configuration.isPressed || held }

        var body: some View {
            configuration.label
                .scaleEffect(down ? scale : 1)
                .opacity(down ? 0.92 : 1)
                .animation(.spring(response: 0.22, dampingFraction: 0.55), value: down)
                .onChange(of: configuration.isPressed) { _, pressed in
                    if pressed {
                        held = true
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.14) { held = false }
                    }
                }
        }
    }
}

extension ButtonStyle where Self == PressableScaleStyle {
    static var pressable: PressableScaleStyle { PressableScaleStyle() }
    static func pressable(scale: CGFloat) -> PressableScaleStyle {
        PressableScaleStyle(scale: scale)
    }
}

// A button that fires once on tap and then auto-repeats while held (with a short
// initial delay, then accelerating) — for the weight/reps steppers so you can
// hold instead of tapping many times. Also gives the same press scale feedback.
struct HoldRepeatButton<Label: View>: View {
    var scale: CGFloat = 0.86
    var action: () -> Void
    @ViewBuilder var label: Label

    @State private var pressed = false
    @State private var holdTask: Task<Void, Never>?

    init(scale: CGFloat = 0.86, action: @escaping () -> Void, @ViewBuilder label: () -> Label) {
        self.scale = scale
        self.action = action
        self.label = label()
    }

    var body: some View {
        label
            .scaleEffect(pressed ? scale : 1)
            .animation(.spring(response: 0.28, dampingFraction: 0.58), value: pressed)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { _ in if holdTask == nil { begin() } }
                    .onEnded { _ in end() }
            )
    }

    private func begin() {
        pressed = true
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        holdTask = Task { @MainActor in
            action()  // immediate single step
            try? await Task.sleep(nanoseconds: 350_000_000)  // hold threshold before repeat
            var interval: UInt64 = 110_000_000
            while !Task.isCancelled {
                action()
                try? await Task.sleep(nanoseconds: interval)
                if interval > 50_000_000 { interval -= 10_000_000 }  // accelerate
            }
        }
    }

    private func end() {
        pressed = false
        holdTask?.cancel()
        holdTask = nil
    }
}

struct WarmWallpaper: View {
    var dim: Bool = false

    var body: some View {
        ZStack {
            LinearGradient(
                colors: [
                    Color(red: 0.957, green: 0.949, blue: 0.933),  // #F4F2EE
                    Color(red: 0.918, green: 0.906, blue: 0.882),  // #EAE7E1
                ],
                startPoint: .top,
                endPoint: .bottom
            )

            GeometryReader { geo in
                let w = geo.size.width
                let h = geo.size.height

                // Single restrained accent wash from the top-right corner.
                Circle()
                    .fill(
                        RadialGradient(
                            colors: [
                                Color(red: 1.0, green: 0.353, blue: 0.157).opacity(
                                    dim ? 0.18 : 0.34),
                                .clear,
                            ],
                            center: .center,
                            startRadius: 0,
                            endRadius: w * 0.55
                        )
                    )
                    .frame(width: w * 1.1, height: w * 1.1)
                    .position(x: w * 0.86, y: h * 0.04)
                    .blur(radius: 32)

                // Faint cool wash from bottom-left for depth.
                if !dim {
                    Circle()
                        .fill(
                            RadialGradient(
                                colors: [
                                    Color(red: 0.118, green: 0.176, blue: 0.275).opacity(0.09),
                                    .clear,
                                ],
                                center: .center,
                                startRadius: 0,
                                endRadius: w * 0.4
                            )
                        )
                        .frame(width: w * 0.9, height: w * 0.9)
                        .position(x: w * 0.06, y: h * 0.96)
                        .blur(radius: 28)
                }
            }
        }
        .ignoresSafeArea()
    }
}

struct GlassBackground: View {
    var radius: CGFloat = 24
    var thick: Bool = false

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .fill(.ultraThinMaterial)
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .fill(Color.white.opacity(thick ? 0.30 : 0.18))
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .stroke(Color.white.opacity(0.55), lineWidth: 0.5)
        }
        .shadow(color: .black.opacity(0.025), radius: 1, y: 1)
        .shadow(color: .black.opacity(0.10), radius: 12, y: 8)
    }
}

struct LiquidGlassBackground: View {
    var radius: CGFloat = 28

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .fill(.ultraThinMaterial)
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .fill(
                    LinearGradient(
                        colors: [
                            Color.white.opacity(0.58),
                            Color.white.opacity(0.34),
                            Color.white.opacity(0.46),
                        ],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .stroke(Color.white.opacity(0.70), lineWidth: 0.5)
            // Bottom rim — replaces the screen-blend sheen that turned into a stripe
            // artifact on shorter surfaces like the tab bar.
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .stroke(Color.black.opacity(0.05), lineWidth: 0.5)
                .blendMode(.multiply)
                .allowsHitTesting(false)
        }
        .shadow(color: .black.opacity(0.025), radius: 1, y: 1)
        .shadow(color: .black.opacity(0.18), radius: 18, y: 14)
    }
}

extension View {
    @ViewBuilder
    func glassCard(radius: CGFloat = 24, thick: Bool = false) -> some View {
        if #available(iOS 26.0, *) {
            self.glassEffect(
                .regular, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
        } else {
            self.background(GlassBackground(radius: radius, thick: thick))
                .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
        }
    }

    @ViewBuilder
    func liquidGlass(radius: CGFloat = 28) -> some View {
        if #available(iOS 26.0, *) {
            self.glassEffect(
                .regular, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
        } else {
            self.background(LiquidGlassBackground(radius: radius))
                .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
        }
    }

    @ViewBuilder
    func chipBackground() -> some View {
        if #available(iOS 26.0, *) {
            self.glassEffect(.regular, in: Capsule())
        } else {
            self.background(
                ZStack {
                    Capsule().fill(.ultraThinMaterial)
                    Capsule().fill(Color.white.opacity(0.20))
                    Capsule().stroke(Color.white.opacity(0.65), lineWidth: 0.5)
                }
            )
        }
    }
}

// MARK: - Свободный текст
//
// Единственный примитив поля свободного текста в приложении: событие, заметка
// к тренировке, заметка к подходу. Системный TextEditor приезжает вообще без
// оформления, поэтому вся оболочка своя — стекло, радиус, плейсхолдер и
// каретка акцентом; три размера отличаются только минимальной высотой.
//
// Автофокуса нет намеренно: клавиатура поверх CTA — это закрытая кнопка, а
// автофокус в проекте уже дал флакающий тест. Вместо него — «Готово» в
// тулбаре клавиатуры.

struct MonoTextArea: View {
    var placeholder: String
    @Binding var text: String
    var minHeight: CGFloat = 76
    var radius: CGFloat = 18
    /// Надпись внутри блока — нужна там, где поле стоит без своего заголовка
    /// (заметка к подходу в шите быстрого ввода).
    var label: String?

    @FocusState private var isFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let label {
                Text(label)
                    .tLabel(size: 9)
            }

            ZStack(alignment: .topLeading) {
                if text.isEmpty {
                    Text(placeholder)
                        .font(.jbm(13.5))
                        .foregroundStyle(DesignPalette.ink4)
                        // Подгон под внутренние инсеты UITextView: без него
                        // плейсхолдер стоит на пару точек выше и левее текста.
                        .padding(.top, 8)
                        .padding(.leading, 5)
                        .allowsHitTesting(false)
                }

                // Высота фиксирована по макету, а не растёт с текстом: в
                // ScrollView композера TextEditor забирает всё предложенное
                // место, а карточка «на пол-экрана» ради двух фраз не нужна.
                // Что не влезло — скроллится внутри поля.
                TextEditor(text: $text)
                    .font(.jbm(13.5))
                    .foregroundStyle(DesignPalette.ink)
                    .tint(DesignPalette.accent)
                    .scrollContentBackground(.hidden)
                    .focused($isFocused)
                    .frame(height: minHeight)
            }
        }
        // 14/12 по макету минус собственные инсеты TextEditor.
        .padding(.horizontal, 9)
        .padding(.vertical, 4)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassCard(radius: radius)
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Готово") { isFocused = false }
                    .font(.jbm(15, weight: .semibold))
                    .foregroundStyle(DesignPalette.ink)
            }
        }
    }
}

/// Перенос чипов по строкам: пять пресетов не влезают в ширину SE одной
/// строкой, а горизонтальный скролл спрятал бы половину списка.
struct WrapLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout Void) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var rowWidth: CGFloat = 0
        var rowHeight: CGFloat = 0
        var total = CGSize.zero

        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if rowWidth > 0, rowWidth + spacing + size.width > maxWidth {
                total.width = max(total.width, rowWidth)
                total.height += rowHeight + spacing
                rowWidth = 0
                rowHeight = 0
            }
            rowWidth += (rowWidth > 0 ? spacing : 0) + size.width
            rowHeight = max(rowHeight, size.height)
        }

        total.width = max(total.width, rowWidth)
        total.height += rowHeight
        return total
    }

    func placeSubviews(
        in bounds: CGRect,
        proposal: ProposedViewSize,
        subviews: Subviews,
        cache: inout Void
    ) {
        var x = bounds.minX
        var y = bounds.minY
        var rowHeight: CGFloat = 0

        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}

/// Вертикальный пунктир — граница рельсы у карточки события.
struct VerticalDashedLine: View {
    var color: Color

    var body: some View {
        GeometryReader { geo in
            Path { path in
                path.move(to: CGPoint(x: 0.5, y: 0))
                path.addLine(to: CGPoint(x: 0.5, y: geo.size.height))
            }
            .stroke(style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
            .foregroundStyle(color)
        }
        .frame(width: 1)
    }
}

// MARK: - Effort

extension SetEffort {
    fileprivate var dotIndex: Int {
        switch self {
        case .easy: 0
        case .ok: 1
        case .hard: 2
        }
    }

    fileprivate var emoji: String {
        switch self {
        case .easy: "🙂"
        case .ok: "😐"
        case .hard: "😣"
        }
    }

    fileprivate var bubbleColor: Color {
        switch self {
        case .easy: DesignPalette.effortEasy
        case .ok: DesignPalette.effortOk
        case .hard: DesignPalette.effortHard
        }
    }
}

struct EffortBubble: View {
    var effort: SetEffort
    var size: CGFloat = 22
    var selected: Bool = false

    var body: some View {
        Text(effort.emoji)
            .font(.system(size: size * 0.55))
            .frame(width: size, height: size)
            .background(effort.bubbleColor, in: Circle())
            .overlay(
                Circle().stroke(
                    selected ? DesignPalette.ink : Color.clear,
                    lineWidth: selected ? 2.5 : 0
                )
            )
            .scaleEffect(selected ? 1.05 : 1)
            .animation(.spring(response: 0.2, dampingFraction: 0.7), value: selected)
            .accessibilityLabel(effort.label)
    }
}

// MARK: - Glyphs

enum ExerciseGlyph: String {
    case bench, legs, lat, delts, biceps, triceps, row, fly, legext, legcurl, pullup
    case other

    static func resolve(id: Int) -> ExerciseGlyph {
        switch id {
        case 1, 18: return .bench
        case 8: return .legs
        case 9: return .lat
        case 13: return .delts
        case 11: return .biceps
        case 12: return .triceps
        case 10: return .row
        case 17: return .fly
        case 16: return .legext
        case 15: return .legcurl
        case 4: return .pullup
        default: return .other
        }
    }

    static func muscle(id: Int) -> String {
        switch id {
        case 1, 17, 18: return "Грудь"
        case 8, 15, 16: return "Ноги"
        case 9, 10, 4: return "Спина"
        case 13: return "Плечи"
        case 11, 12: return "Руки"
        default: return "Другое"
        }
    }

    static func short(name: String) -> String {
        if name.count <= 9 { return name }
        return String(name.prefix(8)) + "."
    }
}

struct ExerciseGlyphView: View {
    var glyph: ExerciseGlyph
    var size: CGFloat = 28
    var lineWidth: CGFloat = 1.6

    var body: some View {
        Canvas { ctx, _ in
            let s: CGFloat = 36
            let path = Self.path(for: glyph, in: CGRect(x: 0, y: 0, width: s, height: s))
            ctx.stroke(
                path,
                with: .color(.primary),
                style: StrokeStyle(
                    lineWidth: lineWidth * (36 / size), lineCap: .round, lineJoin: .round)
            )
        }
        .frame(width: size, height: size)
        .scaleEffect(size / 36 * (36 / size))
    }

    static func path(for glyph: ExerciseGlyph, in rect: CGRect) -> Path {
        var p = Path()
        switch glyph {
        case .bench:
            p.addRoundedRect(
                in: CGRect(x: 13, y: 9, width: 10, height: 3),
                cornerSize: CGSize(width: 1.2, height: 1.2))
            p.move(to: CGPoint(x: 18, y: 12))
            p.addLine(to: CGPoint(x: 18, y: 21))
            p.move(to: CGPoint(x: 8, y: 21))
            p.addLine(to: CGPoint(x: 28, y: 21))
            p.move(to: CGPoint(x: 11, y: 21))
            p.addLine(to: CGPoint(x: 11, y: 27))
            p.move(to: CGPoint(x: 25, y: 21))
            p.addLine(to: CGPoint(x: 25, y: 27))
            p.addEllipse(in: CGRect(x: 4, y: 14, width: 4, height: 4))
            p.addEllipse(in: CGRect(x: 28, y: 14, width: 4, height: 4))
        case .legs:
            p.move(to: CGPoint(x: 9, y: 8))
            p.addLine(to: CGPoint(x: 17, y: 19))
            p.addLine(to: CGPoint(x: 15, y: 28))
            p.move(to: CGPoint(x: 18, y: 8))
            p.addLine(to: CGPoint(x: 15, y: 19))
            p.addLine(to: CGPoint(x: 20, y: 28))
            p.move(to: CGPoint(x: 26, y: 8))
            p.addLine(to: CGPoint(x: 23, y: 17))
            p.addLine(to: CGPoint(x: 24, y: 28))
            p.addEllipse(in: CGRect(x: 3.5, y: 3.5, width: 5, height: 5))
        case .lat:
            p.move(to: CGPoint(x: 6, y: 7))
            p.addLine(to: CGPoint(x: 30, y: 7))
            for x in [10, 16, 22, 28] {
                p.move(to: CGPoint(x: CGFloat(x), y: 7))
                p.addLine(to: CGPoint(x: CGFloat(x), y: x == 16 || x == 28 ? 16 : 13))
            }
            p.move(to: CGPoint(x: 14, y: 16))
            p.addLine(to: CGPoint(x: 24, y: 16))
            p.addLine(to: CGPoint(x: 24, y: 20))
            p.addLine(to: CGPoint(x: 19, y: 28))
            p.addLine(to: CGPoint(x: 14, y: 20))
            p.closeSubpath()
        case .delts:
            p.addEllipse(in: CGRect(x: 15, y: 6, width: 6, height: 6))
            p.move(to: CGPoint(x: 11, y: 16))
            p.addLine(to: CGPoint(x: 18, y: 14))
            p.addLine(to: CGPoint(x: 25, y: 16))
            p.move(to: CGPoint(x: 9, y: 22))
            p.addLine(to: CGPoint(x: 12, y: 16))
            p.move(to: CGPoint(x: 27, y: 22))
            p.addLine(to: CGPoint(x: 24, y: 16))
            p.move(to: CGPoint(x: 11, y: 22))
            p.addLine(to: CGPoint(x: 25, y: 22))
            p.addLine(to: CGPoint(x: 23, y: 29))
            p.addLine(to: CGPoint(x: 13, y: 29))
            p.closeSubpath()
        case .biceps:
            p.move(to: CGPoint(x: 7, y: 24))
            p.addCurve(
                to: CGPoint(x: 16, y: 15),
                control1: CGPoint(x: 7, y: 18), control2: CGPoint(x: 12, y: 15))
            p.addCurve(
                to: CGPoint(x: 21, y: 9),
                control1: CGPoint(x: 20, y: 15), control2: CGPoint(x: 21, y: 12))
            p.move(to: CGPoint(x: 16, y: 15))
            p.addCurve(
                to: CGPoint(x: 23, y: 19),
                control1: CGPoint(x: 17, y: 18), control2: CGPoint(x: 20, y: 19))
            p.move(to: CGPoint(x: 21, y: 9))
            p.addLine(to: CGPoint(x: 25, y: 6))
            p.move(to: CGPoint(x: 28, y: 17))
            p.addLine(to: CGPoint(x: 30, y: 17))
        case .triceps:
            p.move(to: CGPoint(x: 28, y: 12))
            p.addCurve(
                to: CGPoint(x: 19, y: 21),
                control1: CGPoint(x: 28, y: 18), control2: CGPoint(x: 23, y: 21))
            p.addCurve(
                to: CGPoint(x: 14, y: 27),
                control1: CGPoint(x: 15, y: 21), control2: CGPoint(x: 14, y: 24))
            p.move(to: CGPoint(x: 19, y: 21))
            p.addCurve(
                to: CGPoint(x: 12, y: 17),
                control1: CGPoint(x: 18, y: 18), control2: CGPoint(x: 15, y: 17))
            p.move(to: CGPoint(x: 15, y: 27))
            p.addLine(to: CGPoint(x: 11, y: 30))
            p.move(to: CGPoint(x: 7, y: 19))
            p.addLine(to: CGPoint(x: 5, y: 19))
        case .row:
            p.move(to: CGPoint(x: 4, y: 18))
            p.addLine(to: CGPoint(x: 32, y: 18))
            p.addEllipse(in: CGRect(x: 4.5, y: 15.5, width: 5, height: 5))
            p.addEllipse(in: CGRect(x: 26.5, y: 15.5, width: 5, height: 5))
            p.move(to: CGPoint(x: 14, y: 12))
            p.addLine(to: CGPoint(x: 12, y: 18))
            p.addLine(to: CGPoint(x: 14, y: 24))
            p.move(to: CGPoint(x: 22, y: 12))
            p.addLine(to: CGPoint(x: 24, y: 18))
            p.addLine(to: CGPoint(x: 22, y: 24))
        case .fly:
            p.move(to: CGPoint(x: 18, y: 8))
            p.addLine(to: CGPoint(x: 18, y: 28))
            p.move(to: CGPoint(x: 18, y: 14))
            p.addCurve(
                to: CGPoint(x: 9, y: 12),
                control1: CGPoint(x: 15, y: 11), control2: CGPoint(x: 12, y: 11))
            p.move(to: CGPoint(x: 18, y: 14))
            p.addCurve(
                to: CGPoint(x: 27, y: 12),
                control1: CGPoint(x: 21, y: 11), control2: CGPoint(x: 24, y: 11))
            p.move(to: CGPoint(x: 18, y: 22))
            p.addCurve(
                to: CGPoint(x: 9, y: 23),
                control1: CGPoint(x: 15, y: 24), control2: CGPoint(x: 12, y: 24))
            p.move(to: CGPoint(x: 18, y: 22))
            p.addCurve(
                to: CGPoint(x: 27, y: 23),
                control1: CGPoint(x: 21, y: 24), control2: CGPoint(x: 24, y: 24))
        case .legext:
            p.move(to: CGPoint(x: 8, y: 26))
            p.addLine(to: CGPoint(x: 14, y: 26))
            p.addLine(to: CGPoint(x: 14, y: 18))
            p.addLine(to: CGPoint(x: 24, y: 18))
            p.addLine(to: CGPoint(x: 28, y: 26))
            p.addEllipse(in: CGRect(x: 19, y: 11, width: 6, height: 6))
            p.move(to: CGPoint(x: 14, y: 18))
            p.addLine(to: CGPoint(x: 11, y: 14))
        case .legcurl:
            p.move(to: CGPoint(x: 8, y: 12))
            p.addLine(to: CGPoint(x: 22, y: 12))
            p.addLine(to: CGPoint(x: 22, y: 20))
            p.addLine(to: CGPoint(x: 28, y: 20))
            p.addLine(to: CGPoint(x: 24, y: 26))
            p.addEllipse(in: CGRect(x: 19, y: 23, width: 6, height: 6))
            p.move(to: CGPoint(x: 22, y: 20))
            p.addLine(to: CGPoint(x: 22, y: 23))
        case .pullup:
            p.move(to: CGPoint(x: 5, y: 7))
            p.addLine(to: CGPoint(x: 31, y: 7))
            p.move(to: CGPoint(x: 11, y: 7))
            p.addLine(to: CGPoint(x: 11, y: 11))
            p.move(to: CGPoint(x: 25, y: 7))
            p.addLine(to: CGPoint(x: 25, y: 11))
            p.addEllipse(in: CGRect(x: 15.5, y: 11.5, width: 5, height: 5))
            p.move(to: CGPoint(x: 18, y: 16))
            p.addLine(to: CGPoint(x: 18, y: 24))
            p.move(to: CGPoint(x: 14, y: 19))
            p.addLine(to: CGPoint(x: 18, y: 17))
            p.addLine(to: CGPoint(x: 22, y: 19))
            p.move(to: CGPoint(x: 14, y: 27))
            p.addLine(to: CGPoint(x: 18, y: 24))
            p.addLine(to: CGPoint(x: 22, y: 27))
        case .other:
            p.addEllipse(in: CGRect(x: 8, y: 8, width: 20, height: 20))
        }
        // Path is drawn in canvas; scale to bounds is implicit when 36x36.
        return p
    }
}

// Wrapper that scales the 36-unit canvas to the requested size.
struct GlyphIcon: View {
    var glyph: ExerciseGlyph
    var size: CGFloat
    var lineWidth: CGFloat = 1.6
    var tint: Color = DesignPalette.ink

    var body: some View {
        let scale = size / 36
        Canvas { ctx, _ in
            ctx.scaleBy(x: scale, y: scale)
            ctx.stroke(
                ExerciseGlyphView.path(for: glyph, in: CGRect(x: 0, y: 0, width: 36, height: 36)),
                with: .color(tint),
                style: StrokeStyle(lineWidth: lineWidth, lineCap: .round, lineJoin: .round)
            )
        }
        .frame(width: size, height: size)
    }
}

// MARK: - Typography helpers
//
// All-mono direction: JetBrains Mono everywhere, matching the design mockups.
// The TTFs are bundled under TrainerIOS/Resources and registered through
// Info.plist's UIAppFonts. AppearanceFonts.bootstrap() runs at app launch and
// logs an assertion if any PS name is missing so we catch a broken bundle.

enum AppFont {
    static let regular = "JetBrainsMono-Regular"
    static let medium = "JetBrainsMono-Medium"
    static let semibold = "JetBrainsMono-SemiBold"
    static let bold = "JetBrainsMono-Bold"

    static func name(for weight: Font.Weight) -> String {
        switch weight {
        case .ultraLight, .thin, .light, .regular: return regular
        case .medium: return medium
        case .semibold: return semibold
        case .bold, .heavy, .black: return bold
        default: return regular
        }
    }
}

extension Font {
    static func jbm(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .custom(AppFont.name(for: weight), size: size)
    }
}

extension View {
    func display(size: CGFloat, weight: Font.Weight = .bold) -> some View {
        font(.jbm(size, weight: weight))
            .tracking(-size * 0.04)
            .monospacedDigit()
    }

    func mono(_ size: CGFloat, weight: Font.Weight = .regular) -> some View {
        font(.jbm(size, weight: weight))
            .monospacedDigit()
    }

    /// Цвет — параметр, а не второй `.foregroundStyle` поверх: у Text выигрывает
    /// БЛИЖАЙШИЙ к нему стиль, поэтому `.tLabel().foregroundStyle(accent)`
    /// молча оставляет надпись серой.
    func tLabel(size: CGFloat = 10.5, color: Color = DesignPalette.ink3) -> some View {
        font(.jbm(size, weight: .semibold))
            .tracking(0.6)
            .textCase(.uppercase)
            .foregroundStyle(color)
    }
}

// MARK: - Pieces shared across screens

struct TopPills: View {
    struct Pill: Identifiable {
        let id = UUID()
        var icon: AnyView?
        var label: String
        var tone: Tone = .neutral
        var action: (() -> Void)?

        enum Tone {
            case neutral
            case accent
        }
    }

    var pills: [Pill]
    var trailing: AnyView?

    var body: some View {
        HStack(spacing: 6) {
            ForEach(pills) { pill in
                HStack(spacing: 6) {
                    if let icon = pill.icon { icon }
                    Text(pill.label)
                }
                .font(.jbm(13.5, weight: .semibold))
                .foregroundStyle(pill.tone == .accent ? DesignPalette.accent : DesignPalette.ink2)
                .padding(.horizontal, 12)
                .padding(.vertical, 7)
                .chipBackground()
            }

            Spacer(minLength: 0)

            if let trailing { trailing }
        }
    }
}

struct TopTitle: View {
    var sub: String?
    var title: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let sub {
                Text(sub.uppercased())
                    .font(.jbm(13, weight: .semibold))
                    .tracking(0.4)
                    .foregroundStyle(DesignPalette.ink.opacity(0.5))
            }
            Text(title)
                .display(size: 34, weight: .heavy)
                .foregroundStyle(DesignPalette.ink)
        }
    }
}

// MARK: - Finish workout button

struct ProgressRingArc: View {
    var progress: Double

    var body: some View {
        ZStack {
            Circle()
                .stroke(Color.black.opacity(0.08), lineWidth: 3.5)
                .padding(5)

            Circle()
                .trim(from: 0, to: max(0.001, min(1, progress)))
                .stroke(
                    AngularGradient(
                        gradient: Gradient(stops: [
                            .init(color: Color(red: 1.0, green: 0.0, blue: 0.251), location: 0),
                            .init(color: Color(red: 1.0, green: 0.831, blue: 0.0), location: 0.5),
                            .init(color: Color(red: 0.0, green: 0.902, blue: 0.463), location: 1),
                        ]),
                        center: .center,
                        startAngle: .degrees(-90),
                        endAngle: .degrees(270)
                    ),
                    style: StrokeStyle(lineWidth: 3.5, lineCap: .round)
                )
                .rotationEffect(.degrees(-90))
                .padding(5)
                .shadow(color: Color(red: 0.0, green: 0.902, blue: 0.463).opacity(0.6), radius: 6)
                .animation(.spring(response: 0.4, dampingFraction: 0.85), value: progress)
        }
    }
}

// MARK: - ContentView shell

struct ContentView: View {
    @EnvironmentObject private var store: TrainerStore
    @State private var noteWorkout: Workout?

    var body: some View {
        ZStack(alignment: .top) {
            switch store.bootState {
            case .idle, .loading:
                ZStack {
                    WarmWallpaper(dim: true)
                    LoadingScreen()
                }
            case .loaded:
                MainShellView()
            case .needsSignIn(let message):
                ZStack {
                    WarmWallpaper(dim: true)
                    SignInScreen(message: message)
                }
            case .failed(let message):
                ZStack {
                    WarmWallpaper(dim: true)
                    ErrorScreen(message: message)
                }
            }

            if let toast = store.toast {
                ToastView(message: toast)
                    .padding(.top, 60)
                    .transition(.move(edge: .top).combined(with: .opacity))
                    .frame(maxWidth: .infinity, alignment: .top)
                    .allowsHitTesting(false)
            }

            // Полоска живёт над таб-баром и поверх любого экрана: тренировку
            // могли записать голосом, а заметка предлагается к факту, а не к
            // месту в интерфейсе.
            if let finished = store.finishedWorkout {
                FinishWorkoutStrip(summary: finished.summary) {
                    noteWorkout = store.workouts.first { $0.id == finished.id }
                    store.dismissFinishedWorkout()
                }
                .padding(.horizontal, 14)
                .padding(.bottom, 96)
                .transition(.move(edge: .bottom).combined(with: .opacity))
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
            }
        }
        .sheet(item: $noteWorkout) { workout in
            WorkoutNoteSheet(workout: workout)
                .environmentObject(store)
        }
        .animation(.spring(response: 0.28, dampingFraction: 0.86), value: store.toast)
        .animation(.spring(response: 0.30, dampingFraction: 0.86), value: store.finishedWorkout)
        .animation(.spring(response: 0.32, dampingFraction: 0.85), value: store.currentTab)
        .animation(.spring(response: 0.32, dampingFraction: 0.85), value: store.draft.hasRealSets)
        // Decimal-pad startup is surprisingly expensive on a cold process.
        // Pay that one-time cost behind the loading screen, not after the user
        // taps "+" on Measurements.
        .onAppear { DecimalKeyboardPrewarmer.warmUp() }
    }
}

private struct MainShellView: View {
    @EnvironmentObject private var store: TrainerStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var isShowingSettings = false

    // Per the design refresh, the Progress tab is gone — Progress is reachable
    // only by tapping the streak strip on History. Tabs are: История, Сегодня, Замеры.
    var body: some View {
        TabView(selection: tabBinding) {
            HistoryScreen(openSettings: { isShowingSettings = true })
                .tabItem {
                    Label(TrainerTab.history.title, systemImage: TrainerTab.history.systemImage)
                }
                .tag(TrainerTab.history)

            TodayScreen(openSettings: { isShowingSettings = true })
                .tabItem {
                    Label(TrainerTab.trainings.title, systemImage: TrainerTab.trainings.systemImage)
                }
                .tag(TrainerTab.trainings)

            BodyWeightScreen()
                .tabItem {
                    Label(TrainerTab.weight.title, systemImage: TrainerTab.weight.systemImage)
                }
                .tag(TrainerTab.weight)
        }
        .tint(DesignPalette.accent)
        .sheet(isPresented: $isShowingSettings) {
            SettingsSheet()
                .environmentObject(store)
        }
        .onChange(of: scenePhase) { _, newPhase in
            if newPhase == .active {
                store.refreshCoachSignals()
                // События пишутся не только отсюда: запись из чата с тренером
                // (MCP) — штатный сценарий, а сегодняшняя тренировка закрывает
                // открытое событие на сервере молча. Запрос такой же дешёвый,
                // как за сигналами, — локальный SQLite на той же машине.
                Task { await store.reloadEvents() }
            }
        }
    }

    // Old persisted .progress value should land on History (the new entry point).
    private var tabBinding: Binding<TrainerTab> {
        Binding(
            get: { store.currentTab == .progress ? .history : store.currentTab },
            set: { store.currentTab = $0 }
        )
    }
}

// MARK: - Today screen

// MARK: - Coach recommendation card ("Совет тренера")

// CoachCard now renders ONLY the transient states (pending / failed / none).
// The ready recommendation is no longer a separate card — its content lives in
// the "План от тренера" section: per-exercise notes on each plan card, and the
// rationale behind a "?" in the section header. So the ready branch is empty.
struct CoachCard: View {
    @EnvironmentObject private var store: TrainerStore

    var body: some View {
        if let rec = store.recommendation {
            card(for: rec)
        }
    }

    @ViewBuilder
    private func card(for rec: RecommendationResponse) -> some View {
        let status = rec.status ?? "none"
        let busy = store.isRefreshingRecommendation
        if busy || status == "pending" {
            pendingCard(hasPreviousPlan: rec.recommendation != nil)
        } else if status == "failed" {
            failedCard(rec)
        } else if rec.recommendation != nil {
            EmptyView()  // ready → shown inline in the plan section
        } else {
            noneCard
        }
    }
    // MARK: pending

    private func pendingCard(hasPreviousPlan: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            header(basedOn: nil)
            HStack(spacing: 11) {
                ProgressView().tint(DesignPalette.accent)
                VStack(alignment: .leading, spacing: 1) {
                    Text(hasPreviousPlan ? "Обновляю план…" : "ИИ составляет план…")
                        .font(.jbm(13.5, weight: .bold))
                        .foregroundStyle(DesignPalette.ink)
                    Text(hasPreviousPlan ? "старый план временно скрыт" : "обычно 15–20 секунд")
                        .font(.jbm(10.5))
                        .foregroundStyle(DesignPalette.ink3)
                }
            }
            .padding(.top, 16)
        }
        .padding(16)
        .liquidGlass(radius: 26)
    }

    // MARK: none / empty

    private var noneCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            header(basedOn: nil)
            VStack(spacing: 0) {
                ZStack {
                    Circle().fill(DesignPalette.accent.opacity(0.12)).frame(width: 52, height: 52)
                        .overlay(
                            Circle().stroke(DesignPalette.accent.opacity(0.20), lineWidth: 0.5))
                    Image(systemName: "sparkles").font(.system(size: 22)).foregroundStyle(
                        DesignPalette.accent)
                }
                .padding(.bottom, 14)
                Text("Совет ещё не сгенерирован")
                    .font(.jbm(15, weight: .bold)).tracking(-0.3)
                    .foregroundStyle(DesignPalette.ink).multilineTextAlignment(.center)
                Text(
                    "Построю план следующей тренировки по твоей истории — с весами, повторами и обоснованием."
                )
                .font(.jbm(12)).foregroundStyle(DesignPalette.ink3)
                .multilineTextAlignment(.center).lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 6)
                Button {
                    Task { await store.refreshRecommendation() }
                } label: {
                    HStack(spacing: 9) {
                        Image(systemName: "sparkles").font(.system(size: 16, weight: .semibold))
                        Text("Сгенерировать совет").font(.jbm(14.5, weight: .bold))
                    }
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity).frame(height: 48)
                    .background(DesignPalette.accent, in: Capsule())
                }
                .buttonStyle(.pressable(scale: 0.96))
                .padding(.top, 16)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 14)
            .padding(.horizontal, 6)
        }
        .padding(16)
        .liquidGlass(radius: 26)
    }

    // MARK: failed

    private func failedCard(_ rec: RecommendationResponse) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            header(basedOn: nil)
            HStack(alignment: .top, spacing: 12) {
                ZStack {
                    Circle().fill(DesignPalette.bad.opacity(0.10)).frame(width: 40, height: 40)
                        .overlay(Circle().stroke(DesignPalette.bad.opacity(0.22), lineWidth: 0.5))
                    Image(systemName: "exclamationmark.triangle").font(.system(size: 18))
                        .foregroundStyle(DesignPalette.bad)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text("Не удалось подготовить план")
                        .font(.jbm(14.5, weight: .bold)).tracking(-0.3).foregroundStyle(
                            DesignPalette.ink)
                    Text(failureMessage(rec.error))
                        .font(.jbm(12)).foregroundStyle(DesignPalette.ink3)
                        .lineSpacing(2).fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.top, 14)
            Button {
                Task { await store.refreshRecommendation() }
            } label: {
                HStack(spacing: 9) {
                    Image(systemName: "arrow.triangle.2.circlepath").font(
                        .system(size: 14, weight: .semibold))
                    Text("Повторить").font(.jbm(14, weight: .bold))
                }
                .foregroundStyle(DesignPalette.ink2)
                .frame(maxWidth: .infinity).frame(height: 46)
                .background(DesignPalette.ink.opacity(0.05), in: Capsule())
                .overlay(Capsule().stroke(DesignPalette.ink.opacity(0.10), lineWidth: 0.5))
            }
            .buttonStyle(.plain)
            .padding(.top, 16)
        }
        .padding(16)
        .liquidGlass(radius: 26)
    }

    private func failureMessage(_ error: String?) -> String {
        guard let error, !error.isEmpty else {
            return "Старый план скрыт. Попробуй ещё раз."
        }
        if error.localizedCaseInsensitiveContains("ограничения методики") {
            return
                "План не прошёл автоматическую проверку нагрузки. Старый план скрыт — попробуй ещё раз."
        }
        return "\(error)\nСтарый план скрыт — попробуй ещё раз."
    }

    // MARK: shared bits

    private func header(basedOn: Int?) -> some View {
        HStack {
            HStack(spacing: 8) {
                Image(systemName: "sparkles").font(.system(size: 14)).foregroundStyle(
                    DesignPalette.accent)
                Text("Совет тренера")
                    .font(.jbm(10.5, weight: .semibold)).tracking(0.6)
                    .textCase(.uppercase).foregroundStyle(DesignPalette.ink)
            }
            Spacer()
            if let basedOn {
                Text("по \(basedOn) трен.")
                    .font(.jbm(10.5, weight: .semibold))
                    .foregroundStyle(DesignPalette.ink4)
            }
        }
    }
}

// Compact preparation-phase chip built from the recommendation's server-side
// coach context: «ДЕФИЦИТ · Н2», «НАБОР · Н5» or a warn-tinted «РАЗГРУЗКА».
struct CoachPhaseChip: View {
    var label: String
    var tint: Color

    var body: some View {
        Text(label)
            .font(.jbm(9.5, weight: .bold)).tracking(0.5)
            .foregroundStyle(tint)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(tint.opacity(0.12), in: Capsule())
            .overlay(Capsule().stroke(tint.opacity(0.22), lineWidth: 0.5))
            .lineLimit(1)
            .fixedSize()
    }

    static func make(_ context: CoachContext?) -> CoachPhaseChip? {
        guard let context else { return nil }
        if context.deloadWeek == true {
            return CoachPhaseChip(label: "РАЗГРУЗКА", tint: DesignPalette.warn)
        }
        // The athlete names the stage in coach_state (phase_params.title) — e.g.
        // «Ф0 · возврат», where the engine code is still cut_recomp. Rendering
        // the engine code would print «ДЕФИЦИТ» on a stage whose whole point is
        // NOT to lose weight. The switch stays as the fallback for a plan
        // generated before the title existed.
        let name: String
        if let title = context.phaseTitle?.trimmingCharacters(in: .whitespacesAndNewlines),
            !title.isEmpty
        {
            name = title.uppercased()
        } else {
            guard let phase = context.phase else { return nil }
            switch phase {
            case "cut_recomp": name = "ДЕФИЦИТ"
            case "lean_bulk": name = "НАБОР"
            case "maintenance": name = "ПОДДЕРЖАНИЕ"
            default: name = phase.uppercased()
            }
        }
        if let week = context.blockWeek {
            return CoachPhaseChip(label: "\(name) · Н\(week)", tint: DesignPalette.ink3)
        }
        return CoachPhaseChip(label: name, tint: DesignPalette.ink3)
    }
}

// The "почему так" sheet behind the "?" in the plan header — focus + load + the
// full rationale text that used to live (collapsed) inside the expanded card.
private struct CoachRationaleSheet: View {
    var focus: String?
    var loadType: String?
    var rationale: String
    var coachContext: CoachContext?

    var body: some View {
        ZStack {
            WarmWallpaper()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    HStack(spacing: 8) {
                        Image(systemName: "sparkles")
                            .font(.system(size: 15))
                            .foregroundStyle(DesignPalette.accent)
                        Text("Почему так")
                            .font(.jbm(11, weight: .bold)).tracking(0.6)
                            .textCase(.uppercase).foregroundStyle(DesignPalette.ink2)
                        Spacer()
                    }
                    if let focus, !focus.isEmpty {
                        Text(focus)
                            .font(.jbm(18, weight: .bold)).tracking(-0.4)
                            .foregroundStyle(DesignPalette.ink)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    HStack(spacing: 8) {
                        if let loadType {
                            let chip = historyLoadChip(loadType)
                            HStack(spacing: 6) {
                                Circle().fill(chip.color).frame(width: 6, height: 6)
                                Text("\(chip.label) нагрузка".uppercased())
                                    .font(.jbm(10, weight: .bold)).tracking(0.6)
                                    .foregroundStyle(chip.color)
                            }
                            .padding(.horizontal, 10).padding(.vertical, 5)
                            .background(chip.color.opacity(0.13), in: Capsule())
                            .overlay(Capsule().stroke(chip.color.opacity(0.24), lineWidth: 0.5))
                        }
                        if let phaseChip = CoachPhaseChip.make(coachContext) {
                            phaseChip
                        }
                    }
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(Array(paragraphs.enumerated()), id: \.offset) { _, para in
                            Text(markdown(para))
                                .font(.jbm(13))
                                .foregroundStyle(DesignPalette.ink2)
                                .lineSpacing(4)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(.top, 2)
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    // Split the rationale into readable paragraphs (the model emits one logical
    // point per line); blank lines are dropped.
    private var paragraphs: [String] {
        rationale
            .components(separatedBy: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    // Render **bold** inline; fall back to plain text if markdown can't parse.
    private func markdown(_ line: String) -> AttributedString {
        (try? AttributedString(
            markdown: line,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )) ?? AttributedString(line)
    }
}

private struct TodayScreen: View {
    @EnvironmentObject private var store: TrainerStore
    var openSettings: () -> Void
    @State private var editor: SetEditorState?
    @State private var pendingActionExercise: DraftDisplayExercise?
    @State private var isConfirmingReset = false
    @State private var showAddCatalog = false
    @State private var showRationale = false
    @State private var confirmRegen = false
    @State private var eventComposer: EventComposerMode?

    var body: some View {
        ZStack(alignment: .bottom) {
            WarmWallpaper()
            scrollContent
            actionBar
                .padding(.horizontal, 14)
                .padding(.bottom, 12)
        }
    }

    private var scrollContent: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                topPillsRow

                // Открытое событие — состояние, а не упрёк, и оно совместимо с
                // начатой сессией: плашка остаётся на месте, пока событие идёт.
                if let openEvent = store.openEvent {
                    TodayEventStrip(
                        event: openEvent,
                        today: DateTools.localTodayISO(),
                        onTap: { eventComposer = .edit(openEvent) },
                        onClose: { Task { await store.closeEvent(openEvent) } }
                    )
                }

                if store.draft.editingWorkoutID == nil {
                    CoachCard()
                }

                if !store.isTodayPlanUnavailable {
                    if store.draft.editingWorkoutID != nil {
                        sectionHeader("Редактируем", right: sessionSummary)
                    } else if store.draft.hasRealSets {
                        sectionHeader("Упражнения", right: sessionSummary)
                    } else if store.appliedPlan != nil {
                        coachPlanHeader
                    } else {
                        sectionHeader("План тренировки", right: nil)
                    }

                    LazyVStack(spacing: 10) {
                        ForEach(store.displayCards()) { card in
                            TodayExerciseCard(
                                card: card,
                                planningContext: store.planningContext(for: card.exerciseID),
                                coachNote: store.coachNote(for: card.exerciseID),
                                onAdd: {
                                    withAnimation(.spring(response: 0.28, dampingFraction: 0.86)) {
                                        store.addPlannedSet(exerciseID: card.exerciseID)
                                    }
                                },
                                onManual: {
                                    openEditor(exerciseID: card.exerciseID, setIndex: nil)
                                },
                                onEditLast: {
                                    if !card.sets.isEmpty {
                                        openEditor(
                                            exerciseID: card.exerciseID,
                                            setIndex: card.sets.count - 1)
                                    }
                                },
                                onLongPress: { pendingActionExercise = card }
                            )
                        }
                    }

                    AddExerciseButton(isExpanded: $showAddCatalog)

                    if showAddCatalog {
                        AddExerciseCatalog(
                            exercises: store.addableExercises(),
                            onSelect: { exercise in
                                openEditor(exerciseID: exercise.id, setIndex: nil)
                                withAnimation { showAddCatalog = false }
                            }
                        )
                    }
                }

            }
            .padding(.horizontal, 14)
            .padding(.top, 8)
            .padding(.bottom, store.draft.hasRealSets ? 90 : 86)
        }
        .scrollIndicators(.hidden)
        .sheet(item: $editor) { state in
            QuickAddSheet(state: state) { nextState in
                store.applySet(
                    DraftSet(
                        reps: nextState.reps,
                        weight: nextState.weight,
                        effort: nextState.effort,
                        notes: nextState.notes.nilIfBlank
                    ),
                    exerciseID: nextState.exerciseID,
                    setIndex: nextState.setIndex
                )
            }
        }
        .sheet(item: $eventComposer) { mode in
            EventComposerSheet(mode: mode)
                .environmentObject(store)
        }
        .sheet(isPresented: $showRationale) {
            CoachRationaleSheet(
                focus: store.recommendation?.recommendation?.focus,
                loadType: store.recommendation?.recommendation?.loadType,
                rationale: store.recommendation?.recommendation?.rationale ?? "",
                coachContext: store.recommendation?.recommendation?.coachContext
            )
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
        .confirmationDialog(
            "Перегенерировать совет?",
            isPresented: $confirmRegen,
            titleVisibility: .visible
        ) {
            Button("Обновить совет") {
                Task { await store.refreshRecommendation() }
            }
            Button("Отмена", role: .cancel) {}
        } message: {
            Text("ИИ построит новый план тренировки. Это занимает 15–20 секунд.")
        }
        .confirmationDialog(
            pendingActionExercise?.exerciseName ?? "Упражнение",
            isPresented: actionDialogBinding,
            titleVisibility: .visible
        ) {
            if let pendingActionExercise,
                pendingActionExercise.sets.isEmpty,
                store.draft.editingWorkoutID == nil,
                store.appliedPlan?.targets(for: pendingActionExercise.exerciseID) != nil
            {
                Button("Убрать из плана", role: .destructive) {
                    withAnimation {
                        store.removeFromPlan(exerciseID: pendingActionExercise.exerciseID)
                    }
                    self.pendingActionExercise = nil
                }
            }

            Button("Удалить последний сет", role: .destructive) {
                if let pendingActionExercise {
                    withAnimation {
                        store.removeLastSet(exerciseID: pendingActionExercise.exerciseID)
                    }
                }
                pendingActionExercise = nil
            }
            .disabled(pendingActionExercise?.sets.isEmpty ?? true)

            Button("Удалить упражнение", role: .destructive) {
                if let pendingActionExercise {
                    withAnimation {
                        store.removeExercise(exerciseID: pendingActionExercise.exerciseID)
                    }
                }
                pendingActionExercise = nil
            }

            Button("Отмена", role: .cancel) {
                pendingActionExercise = nil
            }
        }
        .alert("Отменить тренировку?", isPresented: $isConfirmingReset) {
            Button("Отменить", role: .destructive) {
                withAnimation { store.resetDraft() }
            }
            Button("Назад", role: .cancel) {}
        } message: {
            Text("Все записанные сеты будут удалены.")
        }
    }

    @ViewBuilder
    private var actionBar: some View {
        if store.draft.hasRealSets {
            HStack(spacing: 8) {
                Button {
                    isConfirmingReset = true
                } label: {
                    Image(systemName: "xmark")
                        .font(.jbm(16, weight: .bold))
                        .foregroundStyle(DesignPalette.bad)
                        .frame(width: 52, height: 52)
                        .background(
                            Circle()
                                .fill(DesignPalette.bad.opacity(0.06))
                        )
                        .overlay(
                            Circle()
                                .stroke(DesignPalette.bad.opacity(0.20), lineWidth: 0.5)
                        )
                }
                .buttonStyle(.pressable)
                .accessibilityLabel("Отменить тренировку")

                Button {
                    Task { await store.saveDraftWorkout() }
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "checkmark")
                            .font(.jbm(15, weight: .heavy))
                        Text("Завершить тренировку")
                            .font(.jbm(15.5, weight: .heavy))
                            .tracking(-0.3)
                    }
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(height: 52)
                    .background(DesignPalette.accent, in: Capsule())
                    .shadow(color: DesignPalette.accent.opacity(0.35), radius: 14, y: 6)
                    .overlay(
                        Capsule()
                            .stroke(Color.white.opacity(0.3), lineWidth: 0.5)
                            .blendMode(.plusLighter)
                    )
                }
                .buttonStyle(.pressable(scale: 0.97))
                .disabled(store.isSavingWorkout)
            }
        } else if !store.isTodayPlanUnavailable,
            let first = store.displayCards().first
        {
            Button {
                withAnimation(.spring(response: 0.32, dampingFraction: 0.85)) {
                    store.addPlannedSet(exerciseID: first.exerciseID)
                }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "play.fill")
                        .font(.jbm(13, weight: .heavy))
                    Text("Начать тренировку")
                        .font(.jbm(16, weight: .heavy))
                        .tracking(-0.3)
                }
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity)
                .frame(height: 52)
                .background(DesignPalette.accent, in: Capsule())
                .shadow(color: DesignPalette.accent.opacity(0.35), radius: 14, y: 6)
                .overlay(
                    Capsule()
                        .stroke(Color.white.opacity(0.3), lineWidth: 0.5)
                        .blendMode(.plusLighter)
                )
            }
            .buttonStyle(.pressable(scale: 0.97))
        }
    }

    private var topPillsRow: some View {
        HStack(spacing: 6) {
            HStack(spacing: 6) {
                Circle().fill(DesignPalette.ok).frame(width: 6, height: 6)
                Text("UID \(store.currentUser?.id ?? 0)")
            }
            .mono(13, weight: .semibold)
            .foregroundStyle(DesignPalette.ink2)
            .padding(.horizontal, 11)
            .padding(.vertical, 6)
            .chipBackground()

            HStack(spacing: 4) {
                Text("\(DateTools.short(store.draft.workoutDate)) · \(weekdayShort)")
            }
            .mono(13, weight: .semibold)
            .foregroundStyle(DesignPalette.accent)
            .padding(.horizontal, 11)
            .padding(.vertical, 6)
            .chipBackground()

            if store.draft.hasRealSets {
                SessionPill()
            }

            Spacer()

            Button(action: openSettings) {
                Image(systemName: "ellipsis")
                    .font(.jbm(15, weight: .bold))
                    .foregroundStyle(DesignPalette.ink2)
                    .frame(width: 34, height: 34)
                    .chipBackground()
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Настройки")
        }
        .padding(.top, 4)
    }

    private var weekdayShort: String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ru_RU")
        formatter.dateFormat = "EE"
        let value = formatter.string(from: DateTools.date(from: store.draft.workoutDate))
        guard let first = value.first else { return value }
        return first.uppercased() + value.dropFirst()
    }

    private var sessionSummary: AnyView {
        let totalExercises = store.displayCards().filter { !$0.sets.isEmpty }.count
        let totalSets = store.draft.exercises.reduce(0) { $0 + $1.sets.count }
        let label: String
        if store.draft.editingWorkoutID == nil,
            let plan = store.appliedPlan
        {
            // Against an applied coach plan show progress vs the plan's volume.
            let planTotal = plan.exercises.reduce(0) { $0 + $1.sets.count }
            label = "\(totalExercises) упр · \(min(totalSets, planTotal))/\(planTotal) сет"
        } else {
            label = "\(totalExercises) упр · \(totalSets) сет"
        }
        return AnyView(
            Text(label)
                .font(.jbm(12, weight: .semibold))
                .foregroundStyle(DesignPalette.ink3)
        )
    }

    private func sectionHeader(_ title: String, right: AnyView? = nil) -> some View {
        HStack {
            Text(title.uppercased())
                .font(.jbm(13, weight: .bold))
                .tracking(0.4)
                .foregroundStyle(DesignPalette.ink3)
            Spacer()
            if let right { right }
        }
        .padding(.horizontal, 4)
        .padding(.top, 4)
    }

    // Header for the coach plan: spark mark (it's AI) + a "?" that reveals the
    // rationale ("почему так") in a sheet — the only surviving bit of the old
    // expanded card besides the per-exercise notes now on each plan card.
    private var coachPlanHeader: some View {
        HStack(spacing: 7) {
            Image(systemName: "sparkles")
                .font(.system(size: 13))
                .foregroundStyle(DesignPalette.accent)
            Text("План от тренера".uppercased())
                .font(.jbm(13, weight: .bold))
                .tracking(0.4)
                .foregroundStyle(DesignPalette.ink3)
            Spacer()
            HStack(spacing: 14) {
                Button {
                    confirmRegen = true
                } label: {
                    Group {
                        if store.isRefreshingRecommendation {
                            ProgressView().controlSize(.small)
                        } else {
                            Image(systemName: "arrow.triangle.2.circlepath")
                                .font(.system(size: 16, weight: .regular))
                        }
                    }
                    .foregroundStyle(DesignPalette.ink3)
                }
                .buttonStyle(.plain)
                .disabled(store.isRefreshingRecommendation)
                .accessibilityLabel("Перегенерировать совет")

                if let rationale = store.recommendation?.recommendation?.rationale,
                    !rationale.isEmpty
                {
                    Button {
                        showRationale = true
                    } label: {
                        Image(systemName: "questionmark.circle")
                            .font(.system(size: 17, weight: .regular))
                            .foregroundStyle(DesignPalette.ink3)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Почему такой план")
                }
            }
        }
        .padding(.horizontal, 4)
        .padding(.top, 4)
    }

    private var completedCount: Int {
        store.displayCards().filter { !$0.sets.isEmpty }.count
    }

    private var plannedTotal: Int {
        max(store.exerciseGroups().primaryPoolTotal, max(store.displayCards().count, 1))
    }

    private var actionDialogBinding: Binding<Bool> {
        Binding(
            get: { pendingActionExercise != nil },
            set: { if !$0 { pendingActionExercise = nil } }
        )
    }

    private func openEditor(exerciseID: Int, setIndex: Int?) {
        let exerciseName = store.exerciseDefinition(id: exerciseID)?.name ?? "Упражнение"
        let draftExercise = store.draft.exercises.first(where: { $0.exerciseID == exerciseID })
        let draftSet: DraftSet
        if let setIndex, let existing = draftExercise?.sets[safe: setIndex] {
            draftSet = existing
        } else {
            draftSet = store.plannedSetForEditor(exerciseID: exerciseID)
        }

        editor = SetEditorState(
            exerciseID: exerciseID,
            exerciseName: exerciseName,
            setIndex: setIndex,
            reps: draftSet.reps,
            weight: draftSet.weight,
            effort: draftSet.effort,
            previousLabel: previousLabel(for: exerciseID),
            targetLabel: targetLabel(for: exerciseID),
            currentSetIndex: (draftExercise?.sets.count ?? 0) + 1,
            notes: draftSet.notes ?? ""
        )
    }

    private func previousLabel(for exerciseID: Int) -> String {
        guard let context = store.planningContext(for: exerciseID) else { return "—" }
        return context.previousSummary.segments
            .map { "\(TrainerLogic.formatWeight($0.weight))кг ×\(repsRunString($0.reps))" }
            .joined(separator: " · ")
    }

    private func targetLabel(for exerciseID: Int) -> String {
        guard let context = store.planningContext(for: exerciseID) else { return "—" }
        return context.plannedSummary.segments
            .map { "\(repsRunString($0.reps))" }
            .joined(separator: ", ")
    }
}

// MARK: Session pill
//
// Compact inline indicator that lives in the top-pills row alongside other chips.
// Single live accent dot with a soft halo + elapsed time. The ring + completed/
// total counter were dropped because they were too micro to read and the active
// card on the list already implies progress.
struct SessionPill: View {
    @State private var elapsed: TimeInterval = 0
    @State private var ticker: Timer?

    var body: some View {
        HStack(spacing: 7) {
            ZStack {
                Circle()
                    .fill(DesignPalette.accent.opacity(0.15))
                    .frame(width: 13, height: 13)
                Circle()
                    .fill(DesignPalette.accent)
                    .frame(width: 7, height: 7)
            }

            Text(timeString)
                .mono(13, weight: .bold)
                .foregroundStyle(DesignPalette.ink)
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 6)
        .chipBackground()
        .onAppear { startTicker() }
        .onDisappear { ticker?.invalidate() }
    }

    private var timeString: String {
        let m = Int(elapsed) / 60
        let s = Int(elapsed) % 60
        return String(format: "%02d:%02d", m, s)
    }

    private func startTicker() {
        ticker?.invalidate()
        ticker = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
            DispatchQueue.main.async { elapsed += 1 }
        }
    }
}

private func repsRunString(_ reps: [Int]) -> String {
    guard let first = reps.first else { return "0" }
    var parts: [String] = []
    var current = first
    var count = 1
    for r in reps.dropFirst() {
        if r == current {
            count += 1
        } else {
            parts.append(count > 1 ? "\(current)×\(count)" : "\(current)")
            current = r
            count = 1
        }
    }
    parts.append(count > 1 ? "\(current)×\(count)" : "\(current)")
    return parts.joined(separator: ", ")
}

// MARK: Today exercise card (active)

private struct TodayExerciseCard: View {
    var card: DraftDisplayExercise
    var planningContext: ExercisePlanningContext?
    var coachNote: String? = nil
    var onAdd: () -> Void
    var onManual: () -> Void
    var onEditLast: () -> Void
    var onLongPress: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(card.exerciseName)
                    .font(.jbm(16, weight: .heavy))
                    .tracking(-0.3)
                    .foregroundStyle(DesignPalette.ink)
                    .lineLimit(1)

                referenceLine

                if !card.sets.isEmpty {
                    setsLine
                }

                // Coach's reasoning for this target — kept visible even after
                // logging sets, so the "почему такой вес" context never vanishes.
                if let coachNote {
                    Text(coachNote)
                        .font(.jbm(10.5, weight: .medium))
                        .foregroundStyle(DesignPalette.ink3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 3)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            plusButton
        }
        .padding(EdgeInsets(top: 10, leading: 14, bottom: 10, trailing: 12))
        .glassCard(radius: 20)
        .contentShape(RoundedRectangle(cornerRadius: 20))
        .onLongPressGesture(minimumDuration: 0.55, perform: onLongPress)
    }

    private var referenceLine: some View {
        Group {
            if let parts = planningContext?.progressionParts, !parts.isEmpty {
                HStack(spacing: 6) {
                    Text(parts.first?.previousLabel ?? "")
                        .mono(12.5, weight: .semibold)
                        .foregroundStyle(DesignPalette.ink4)
                    if let effort = parts.first?.previousEffort {
                        EffortBubble(effort: effort, size: 13)
                    }
                    Text("→")
                        .mono(12.5, weight: .bold)
                        .foregroundStyle(DesignPalette.ink5)
                    Text(parts.first?.nextLabel ?? "")
                        .mono(12.5, weight: .heavy)
                        .foregroundStyle(DesignPalette.ok)
                }
                .lineLimit(1)
            } else {
                Text("Нет прошлого выполнения")
                    .mono(12, weight: .semibold)
                    .foregroundStyle(DesignPalette.ink4)
            }
        }
    }

    private var setsLine: some View {
        // Group consecutive same (weight + effort) sets and compress the rep
        // run, matching the history card and the design spec ("120кг ×10×3"
        // instead of "120×10, 120×10, 120×10").
        let summary = TrainerLogic.summarizeDraftSets(card.sets)
        return Button(action: onEditLast) {
            HStack(spacing: 0) {
                ForEach(Array(summary.segments.enumerated()), id: \.offset) { index, seg in
                    let isLast = index == summary.segments.count - 1
                    HStack(spacing: 4) {
                        Text(seg.label)
                            .mono(13, weight: .heavy)
                            .foregroundStyle(DesignPalette.accent)
                        if let effort = seg.effort {
                            EffortBubble(effort: effort, size: 13)
                        }
                    }
                    if !isLast {
                        Text(",")
                            .mono(13, weight: .heavy)
                            .foregroundStyle(DesignPalette.accent)
                            .padding(.trailing, 6)
                    }
                }
            }
            .padding(.top, 2)
        }
        .buttonStyle(.plain)
    }

    // Tap = add a set; long-press = open the manual editor. A real Button gives
    // reliable tap + press animation inside the ScrollView; the long-press is a
    // `highPriorityGesture` so it deterministically wins over the card's own
    // long-press and the scroll's pan (the old tap+longPress+card-longPress mix
    // arbitrated unpredictably — opening the editor late or not at all).
    private var plusButton: some View {
        Button {
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            onAdd()
        } label: {
            ZStack {
                Circle()
                    .fill(DesignPalette.accent)
                    .frame(width: 42, height: 42)
                    .shadow(color: DesignPalette.accent.opacity(0.33), radius: 10, y: 5)
                    .overlay(
                        Circle()
                            .stroke(Color.white.opacity(0.35), lineWidth: 0.5)
                            .blendMode(.plusLighter)
                    )
                Image(systemName: "plus")
                    .font(.jbm(18, weight: .bold))
                    .foregroundStyle(.white)
            }
            // Visual circle stays 42pt; the tap target is a generous 64pt square.
            .frame(width: 64, height: 64)
            .contentShape(Rectangle())
        }
        .buttonStyle(.pressable(scale: 0.84))
        .accessibilityLabel("Добавить подход")
        .accessibilityHint("Долгое нажатие — свой вес и повторы")
        .highPriorityGesture(
            LongPressGesture(minimumDuration: 0.32, maximumDistance: 18)
                .onEnded { _ in
                    UIImpactFeedbackGenerator(style: .medium).impactOccurred()
                    onManual()
                }
        )
    }
}

// MARK: Add exercise + catalog

private struct AddExerciseButton: View {
    @Binding var isExpanded: Bool

    var body: some View {
        Button {
            withAnimation(.spring(response: 0.32, dampingFraction: 0.85)) {
                isExpanded.toggle()
            }
        } label: {
            HStack(spacing: 8) {
                Image(systemName: isExpanded ? "chevron.up" : "plus")
                    .font(.jbm(12, weight: .bold))
                Text(isExpanded ? "Скрыть каталог" : "Добавить упражнение")
                    .font(.jbm(14, weight: .semibold))
                    .tracking(-0.2)
            }
            .foregroundStyle(DesignPalette.ink2)
            .frame(maxWidth: .infinity)
            .frame(height: 52)
            .background(
                RoundedRectangle(cornerRadius: 26, style: .continuous)
                    .strokeBorder(style: StrokeStyle(lineWidth: 1.5, dash: [4, 4]))
                    .foregroundStyle(Color.black.opacity(0.18))
            )
            .background(
                RoundedRectangle(cornerRadius: 26, style: .continuous)
                    .fill(Color.black.opacity(0.03))
            )
        }
        .buttonStyle(.pressable(scale: 0.97))
        .padding(.top, 6)
    }
}

/// Полный каталог за вычетом того, что уже стоит карточкой на экране:
/// добавить можно любое упражнение из базы, а не только «редкое».
private struct AddExerciseCatalog: View {
    var exercises: [ExerciseDefinition]
    var onSelect: (ExerciseDefinition) -> Void

    private let columns = [GridItem(.adaptive(minimum: 150), spacing: 8)]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if exercises.isEmpty {
                Text("Все упражнения уже на экране")
                    .mono(13)
                    .foregroundStyle(DesignPalette.ink3)
                    .padding(.vertical, 12)
                    .frame(maxWidth: .infinity)
            } else {
                LazyVGrid(columns: columns, spacing: 8) {
                    ForEach(exercises) { ex in
                        Button {
                            onSelect(ex)
                        } label: {
                            HStack(spacing: 8) {
                                Text(ex.name)
                                    .font(.jbm(13, weight: .semibold))
                                    .tracking(-0.2)
                                    .foregroundStyle(DesignPalette.ink)
                                    .lineLimit(2)
                                    .multilineTextAlignment(.leading)
                                Spacer(minLength: 0)
                            }
                            .padding(EdgeInsets(top: 12, leading: 14, bottom: 12, trailing: 12))
                            .glassCard(radius: 16)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
    }
}

// MARK: - Quick Add Sheet

struct SetEditorState: Identifiable, Equatable {
    let id = UUID()
    var exerciseID: Int
    var exerciseName: String
    var setIndex: Int?
    var reps: Int
    var weight: Double
    var effort: SetEffort?
    var previousLabel: String
    var targetLabel: String
    var currentSetIndex: Int
    /// Заметка к подходу: «канат вместо прямой ручки». Вес сопоставим только
    /// внутри одной постановки, и объясняет её этот текст, а не число.
    var notes: String = ""
}

struct QuickAddSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var state: SetEditorState
    @State private var isNoteExpanded: Bool
    @State private var detent: PresentationDetent
    var onApply: (SetEditorState) -> Void

    private static let compactDetent = PresentationDetent.height(520)

    init(state: SetEditorState, onApply: @escaping (SetEditorState) -> Void) {
        _state = State(initialValue: state)
        _isNoteExpanded = State(initialValue: !state.notes.isEmpty)
        _detent = State(
            initialValue: state.notes.isEmpty ? Self.compactDetent : .large
        )
        self.onApply = onApply
    }

    var body: some View {
        ZStack {
            WarmWallpaper()

            VStack(spacing: 0) {
                exerciseHeader

                VStack(spacing: 0) {
                    Text("Вес, кг")
                        .tLabel()
                        .padding(.top, 6)

                    Stepper(
                        value: TrainerLogic.formatWeight(state.weight),
                        suffix: "",
                        big: true,
                        onMinus: { state.weight = max(0, state.weight - 2.5) },
                        onPlus: { state.weight += 2.5 }
                    )

                    Rectangle()
                        .fill(DesignPalette.sep)
                        .frame(height: 0.5)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)

                    Text("Повторений")
                        .tLabel()
                        .padding(.top, 4)

                    Stepper(
                        value: "\(state.reps)",
                        suffix: "",
                        big: false,
                        onMinus: { state.reps = max(1, state.reps - 1) },
                        onPlus: { state.reps += 1 }
                    )

                    Rectangle()
                        .fill(DesignPalette.sep)
                        .frame(height: 0.5)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 8)

                    Text("Как ощущения?")
                        .tLabel()
                        .padding(.bottom, 4)

                    HStack(spacing: 14) {
                        ForEach(SetEffort.allCases) { effort in
                            Button {
                                state.effort = state.effort == effort ? nil : effort
                            } label: {
                                EffortBubble(
                                    effort: effort, size: 60, selected: state.effort == effort)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.top, 4)

                    noteBlock
                        .padding(.top, 16)
                }
                .padding(.horizontal, 24)
                .padding(.top, 8)

                Spacer(minLength: 12)

                Button {
                    onApply(state)
                    dismiss()
                } label: {
                    Text("Сохранить сет")
                        .font(.jbm(17, weight: .heavy))
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .frame(height: 56)
                        .background(
                            DesignPalette.ink,
                            in: RoundedRectangle(cornerRadius: 28, style: .continuous)
                        )
                        .shadow(color: DesignPalette.ink.opacity(0.35), radius: 18, y: 8)
                }
                .buttonStyle(.pressable(scale: 0.96))
                .padding(.horizontal, 24)
                .padding(.bottom, 28)
            }
            .padding(.top, 8)
        }
        // Раскрытая заметка означает клавиатуру поверх CTA, поэтому шит
        // переезжает на полный детент, а не сжимает содержимое.
        .presentationDetents([Self.compactDetent, .large], selection: $detent)
        .presentationDragIndicator(.visible)
    }

    /// Свёрнутая заметка — пунктирная строка: она не занимает место в колонке
    /// шита и не мешает основному сценарию «вес, повторы, ощущения».
    @ViewBuilder
    private var noteBlock: some View {
        if isNoteExpanded {
            MonoTextArea(
                placeholder: "канат, узкий хват, другая скамья",
                text: $state.notes,
                minHeight: 44,
                radius: 16,
                label: "Заметка к подходу"
            )
        } else {
            Button {
                expandNote()
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "pencil")
                        .font(.jbm(11, weight: .semibold))
                    Text("канат, узкий хват, другая скамья")
                        .font(.jbm(12.5, weight: .semibold))
                        .lineLimit(1)
                }
                .foregroundStyle(DesignPalette.ink3)
                .frame(maxWidth: .infinity)
                .frame(height: 42)
                .background(
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
                        .foregroundStyle(DesignPalette.ink.opacity(0.16))
                )
            }
            .buttonStyle(.pressable(scale: 0.98))
        }
    }

    private func expandNote() {
        withAnimation(.spring(response: 0.3, dampingFraction: 0.86)) {
            isNoteExpanded = true
            detent = .large
        }
    }

    private var exerciseHeader: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(state.exerciseName)
                        .font(.jbm(16, weight: .heavy))
                        .tracking(-0.3)
                        .foregroundStyle(DesignPalette.ink)
                        .lineLimit(1)
                    if !state.previousLabel.isEmpty && state.previousLabel != "—" {
                        HStack(spacing: 4) {
                            Text(state.previousLabel)
                                .mono(12)
                                .foregroundStyle(DesignPalette.ink3)
                            Text("→ \(state.targetLabel)")
                                .mono(12, weight: .heavy)
                                .foregroundStyle(DesignPalette.accent)
                        }
                        .lineLimit(1)
                    }
                }

                Spacer()

                Text("СЕТ \(state.currentSetIndex)")
                    .font(.jbm(10.5, weight: .heavy))
                    .tracking(0.4)
                    .foregroundStyle(DesignPalette.ink3)
                    .padding(.horizontal, 9)
                    .padding(.vertical, 4)
                    .chipBackground()

                // Второй вход в заметку: из шапки видно, есть она уже или нет.
                Button {
                    if isNoteExpanded {
                        withAnimation(.spring(response: 0.3, dampingFraction: 0.86)) {
                            isNoteExpanded = false
                        }
                    } else {
                        expandNote()
                    }
                } label: {
                    Image(systemName: "pencil")
                        .font(.jbm(12, weight: .semibold))
                        .foregroundStyle(
                            state.notes.isEmpty ? DesignPalette.ink2 : Color.white
                        )
                        .frame(width: 30, height: 30)
                        .background(
                            Circle()
                                .fill(
                                    state.notes.isEmpty
                                        ? DesignPalette.ink.opacity(0.05)
                                        : DesignPalette.ink
                                )
                        )
                }
                .buttonStyle(.pressable(scale: 0.92))
                .accessibilityLabel("Заметка к подходу")
            }
            .padding(.horizontal, 24)
            .padding(.top, 12)
            .padding(.bottom, 14)

            Rectangle()
                .fill(DesignPalette.sep)
                .frame(height: 0.5)
                .padding(.horizontal, 16)
        }
    }
}

private struct Stepper: View {
    var value: String
    var suffix: String
    var big: Bool
    var onMinus: () -> Void
    var onPlus: () -> Void

    var body: some View {
        HStack(spacing: 14) {
            HoldRepeatButton(action: onMinus) {
                ZStack {
                    Circle().fill(Color.black.opacity(0.06))
                    Image(systemName: "minus")
                        .font(.jbm(18, weight: .heavy))
                        .foregroundStyle(DesignPalette.ink)
                }
                .frame(width: 62, height: 62)
            }

            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(value)
                    .display(size: big ? 72 : 56, weight: .heavy)
                    .foregroundStyle(DesignPalette.ink)
                Text(suffix)
                    .font(.jbm(18, weight: .semibold))
                    .foregroundStyle(DesignPalette.ink3)
            }
            .frame(maxWidth: .infinity)

            HoldRepeatButton(action: onPlus) {
                ZStack {
                    Circle().fill(DesignPalette.accent)
                    Image(systemName: "plus")
                        .font(.jbm(22, weight: .bold))
                        .foregroundStyle(.white)
                }
                .frame(width: 62, height: 62)
                .shadow(color: DesignPalette.accent.opacity(0.35), radius: 16, y: 6)
            }
        }
        .padding(.vertical, 6)
    }
}

// MARK: - History tab

// Coach-signal glyphs use native SF Symbols only. The server sends semantic
// tokens; CoachSignal maps every supported token to a system image and unknown
// future tokens to info.circle.
struct SignalGlyph: View {
    var systemName: String
    var color: Color
    var size: CGFloat = 17

    var body: some View {
        Image(systemName: systemName)
            .font(.system(size: size, weight: .semibold))
            .symbolRenderingMode(.monochrome)
            .foregroundStyle(color)
            .frame(width: size, height: size)
    }
}

// One coach signal banner, styled after the Claude Design mockups
// (screens/signals.jsx). History owns the visibility cap so every banner is an
// independent List row and gets the native trailing swipe action.
struct SignalBannerView: View {
    var signal: CoachSignal
    var onAction: (CoachSignal) -> Void

    var body: some View {
        banner(signal)
    }

    private struct Tone {
        var glyph: Color
        var glyphBackground: Color
        var edge: Color
    }

    private static func tone(for severity: String) -> Tone {
        switch severity {
        case "warn":
            return Tone(
                glyph: Color(red: 0.72, green: 0.48, blue: 0.07),  // #B87A12
                glyphBackground: DesignPalette.warn.opacity(0.16),
                edge: DesignPalette.warn.opacity(0.38)
            )
        case "accent":
            return Tone(
                glyph: DesignPalette.accent,
                glyphBackground: DesignPalette.accent.opacity(0.12),
                edge: DesignPalette.accent.opacity(0.24)
            )
        case "positive":
            return Tone(
                glyph: DesignPalette.ok,
                glyphBackground: DesignPalette.ok.opacity(0.15),
                edge: DesignPalette.ok.opacity(0.34)
            )
        default:  // info + unknown severities
            return Tone(
                glyph: DesignPalette.ink3,
                glyphBackground: DesignPalette.ink.opacity(0.055),
                edge: DesignPalette.ink.opacity(0.10)
            )
        }
    }

    private func banner(_ signal: CoachSignal) -> some View {
        let critical = signal.severity == "critical"
        let tone = Self.tone(for: signal.severity)
        let ink: Color = critical ? .white : DesignPalette.ink
        let sub: Color = critical ? .white.opacity(0.80) : DesignPalette.ink3
        let hasAction = signal.action != nil && signal.action?.type != "none"
        let ctaColor: Color =
            critical
            ? .white
            : (signal.severity == "info" ? DesignPalette.ink2 : tone.glyph)

        return Button {
            onAction(signal)
        } label: {
            HStack(alignment: .top, spacing: 10) {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(critical ? Color.white.opacity(0.20) : tone.glyphBackground)
                    .frame(width: 30, height: 30)
                    .overlay(
                        SignalGlyph(
                            systemName: signal.systemImage,
                            color: critical ? .white : tone.glyph
                        )
                    )
                    .padding(.top, 1)
                VStack(alignment: .leading, spacing: 2) {
                    Text(signal.title)
                        // Regular weight on purpose: the banner should read as
                        // a coach's line, not shout — hierarchy comes from the
                        // size and ink vs the muted body.
                        .font(.jbm(13)).tracking(-0.2)
                        .foregroundStyle(ink)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                    if !signal.body.isEmpty {
                        Text(signal.body)
                            .font(.jbm(11.5))
                            .foregroundStyle(sub)
                            .lineSpacing(2.5)
                            .multilineTextAlignment(.leading)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let note = signal.note, !note.isEmpty {
                        Text(note)
                            .font(.jbm(10.5))
                            .italic()
                            .foregroundStyle(critical ? .white.opacity(0.62) : DesignPalette.ink4)
                            .padding(.top, 1)
                    }
                }
                Spacer(minLength: 2)
                if hasAction {
                    VStack(spacing: 3) {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 12, weight: .bold))
                            .foregroundStyle(ctaColor)
                        if let label = signal.action?.label, !label.isEmpty {
                            Text(label.uppercased())
                                .font(.jbm(8.5, weight: .bold)).tracking(0.4)
                                .foregroundStyle(ctaColor)
                        }
                    }
                    // Match the natural width of the 8-letter «ПРОГРЕСС» CTA
                    // below, so shorter labels keep their chevron on the same
                    // vertical guide instead of drifting toward the edge.
                    .frame(width: 45)
                    .frame(maxHeight: .infinity, alignment: .center)
                    .padding(.leading, 2)
                }
            }
            .padding(.leading, 12)
            .padding(.trailing, 14)
            .padding(.vertical, 11)
            .frame(maxWidth: .infinity, minHeight: 58, alignment: .leading)
            .background {
                if critical {
                    RoundedRectangle(cornerRadius: 20, style: .continuous)
                        .fill(
                            LinearGradient(
                                colors: [
                                    Color(red: 0.878, green: 0.322, blue: 0.322),  // #E05252
                                    Color(red: 0.824, green: 0.247, blue: 0.247),  // #D23F3F
                                ],
                                startPoint: .top, endPoint: .bottom
                            )
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 20, style: .continuous)
                                .stroke(Color.white.opacity(0.22), lineWidth: 0.5)
                        )
                        .shadow(color: DesignPalette.bad.opacity(0.55), radius: 15, y: 8)
                }
            }
            .modifier(SignalCardChrome(critical: critical, edge: tone.edge))
        }
        .buttonStyle(.pressable(scale: 0.985))
    }
}

// Non-critical banners sit on liquid glass with a severity-tinted hairline;
// the critical one paints its own red gradient instead.
private struct SignalCardChrome: ViewModifier {
    var critical: Bool
    var edge: Color

    func body(content: Content) -> some View {
        if critical {
            content
        } else {
            content
                .glassCard(radius: 20)
                .overlay(
                    RoundedRectangle(cornerRadius: 20, style: .continuous)
                        .stroke(edge, lineWidth: 0.5)
                )
        }
    }
}

private struct HistoryScreen: View {
    @EnvironmentObject private var store: TrainerStore
    var openSettings: () -> Void
    @State private var pendingDeleteWorkout: Workout?
    @State private var pendingDeleteEvent: TrainingEvent?
    @State private var eventComposer: EventComposerMode?
    @State private var noteWorkout: Workout?
    @State private var isShowingProgress = false
    @State private var isShowingWeeklyReport = false
    // Keep the internal backend switcher reachable in code without exposing
    // implementation details (UID / server URL) in the product UI.
    private let showsDeveloperHeader = false

    var body: some View {
        NavigationStack {
            ZStack {
                WarmWallpaper()
                List {
                    Section {
                        if showsDeveloperHeader {
                            headerPills
                                .listRowBackground(Color.clear)
                                .listRowSeparator(.hidden)
                                .listRowInsets(
                                    EdgeInsets(top: 8, leading: 14, bottom: 0, trailing: 14))
                        }

                        TopTitle(sub: "Тренировки · \(store.workouts.count)", title: "История")
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                            .listRowInsets(EdgeInsets(top: 6, leading: 18, bottom: 4, trailing: 18))

                        // Attention always wins over retrospective stats: every
                        // visible server signal is placed before the streak.
                        // Separate rows make the gesture match workout cards.
                        ForEach(Array(visibleCoachSignals.enumerated()), id: \.element.id) {
                            index, signal in
                            SignalBannerView(signal: signal, onAction: handleSignalAction)
                                .listRowBackground(Color.clear)
                                .listRowSeparator(.hidden)
                                .listRowInsets(
                                    EdgeInsets(
                                        top: index == 0 ? 8 : 4,
                                        leading: 14,
                                        bottom: 0,
                                        trailing: 14
                                    )
                                )
                                .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                    if signal.snoozable == true {
                                        Button(role: .destructive) {
                                            store.dismissCoachSignal(signal)
                                        } label: {
                                            Label("Удалить", systemImage: "trash")
                                        }
                                    }
                                }
                        }

                        // Use a Button + navigationDestination instead of a
                        // NavigationLink in the List — a List's NavigationLink
                        // forces a system gray disclosure chevron that
                        // duplicates the accent chevron + "Прогресс" label
                        // baked into streakStrip.
                        Button {
                            isShowingProgress = true
                        } label: {
                            streakStrip
                        }
                        .buttonStyle(.plain)
                        .listRowBackground(Color.clear)
                        .listRowSeparator(.hidden)
                        .listRowInsets(EdgeInsets(top: 8, leading: 14, bottom: 2, trailing: 14))

                        // Compact AI recommendation — the next workout, below the
                        // stats strip. Hidden when there's nothing to show.
                        if showsCoachStrip {
                            HistoryNextWorkoutCard()
                                .listRowBackground(Color.clear)
                                .listRowSeparator(.hidden)
                                .listRowInsets(
                                    EdgeInsets(top: 8, leading: 14, bottom: 6, trailing: 14))
                        }
                    }

                    Section {
                        // Лента — тренировки, события и подсказки в разрывах
                        // одним списком: событие стоит ровно в той дырке,
                        // которую объясняет.
                        ForEach(feedItems) { item in
                            feedRow(item)
                        }

                        if store.workouts.isEmpty && store.events.isEmpty {
                            EmptyStateCard(
                                glyph: .other,
                                title: "История пуста",
                                subtitle: "Первая тренировка появится здесь после сохранения."
                            )
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                            .listRowInsets(
                                EdgeInsets(top: 16, leading: 14, bottom: 16, trailing: 14))
                        }
                    }
                }
                .listStyle(.plain)
                .listSectionSpacing(.compact)
                .scrollContentBackground(.hidden)
                .background(Color.clear)
                .scrollIndicators(.hidden)
                .refreshable {
                    await store.refreshServerData()
                }
            }
            .toolbar(.hidden, for: .navigationBar)
            .navigationDestination(isPresented: $isShowingProgress) {
                ProgressTabScreen()
            }
        }
        .onAppear { store.refreshCoachSignals() }
        .sheet(isPresented: $isShowingWeeklyReport) {
            WeeklyReportSheet()
                .environmentObject(store)
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
        }
        .sheet(item: $eventComposer) { mode in
            EventComposerSheet(mode: mode)
                .environmentObject(store)
        }
        .sheet(item: $noteWorkout) { workout in
            WorkoutNoteSheet(workout: workout)
                .environmentObject(store)
        }
        .alert("Удалить событие?", isPresented: deleteEventBinding) {
            Button("Удалить", role: .destructive) {
                if let pendingDeleteEvent {
                    Task { await store.deleteEvent(pendingDeleteEvent) }
                }
                pendingDeleteEvent = nil
            }
            Button("Отмена", role: .cancel) {
                pendingDeleteEvent = nil
            }
        } message: {
            if let pendingDeleteEvent {
                Text("Тренер перестанет видеть причину этого перерыва.")
                    .accessibilityLabel(pendingDeleteEvent.text)
            }
        }
        .alert("Удалить тренировку?", isPresented: deleteWorkoutBinding) {
            Button("Удалить", role: .destructive) {
                if let pendingDeleteWorkout {
                    Task { await store.deleteWorkout(pendingDeleteWorkout) }
                }
                pendingDeleteWorkout = nil
            }
            Button("Отмена", role: .cancel) {
                pendingDeleteWorkout = nil
            }
        } message: {
            if let pendingDeleteWorkout {
                Text(
                    "Тренировка от \(DateTools.long(pendingDeleteWorkout.workoutDate)) будет удалена."
                )
            }
        }
    }

    private var feedItems: [TrainerLogic.HistoryFeedItem] {
        TrainerLogic.historyFeed(
            workouts: store.workouts,
            events: store.events,
            today: DateTools.localTodayISO()
        )
    }

    @ViewBuilder
    private func feedRow(_ item: TrainerLogic.HistoryFeedItem) -> some View {
        switch item {
        case .workout(let workout):
            HistoryCard(workout: workout)
                .listRowBackground(Color.clear)
                .listRowSeparator(.hidden)
                .listRowInsets(EdgeInsets(top: 5, leading: 14, bottom: 5, trailing: 14))
                .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                    Button(role: .destructive) {
                        pendingDeleteWorkout = workout
                    } label: {
                        Label("Удалить", systemImage: "trash")
                    }

                    Button {
                        store.startEditing(workout)
                        store.currentTab = .trainings
                    } label: {
                        Label("Изменить", systemImage: "pencil")
                    }
                    .tint(DesignPalette.accent)

                    // Поздний вход в заметку: полоска после сохранения уезжает
                    // сама, и другого способа дописать её потом нет.
                    Button {
                        noteWorkout = workout
                    } label: {
                        Label("Заметка", systemImage: "note.text")
                    }
                    .tint(DesignPalette.ink3)
                }

        case .event(let event):
            EventCard(
                event: event,
                today: DateTools.localTodayISO(),
                onClose: { Task { await store.closeEvent(event) } }
            )
            .contentShape(Rectangle())
            // Тап — правка: только там текст события виден целиком, и только
            // там правятся уехавшие даты.
            .onTapGesture { eventComposer = .edit(event) }
            .listRowBackground(Color.clear)
            .listRowSeparator(.hidden)
            .listRowInsets(EdgeInsets(top: 5, leading: 14, bottom: 5, trailing: 14))
            .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                Button(role: .destructive) {
                    pendingDeleteEvent = event
                } label: {
                    Label("Удалить", systemImage: "trash")
                }
            }

        case .gap(let gap):
            EventGapPromptRow(gap: gap) {
                eventComposer = .new(
                    start: gap.startDate,
                    end: gap.isRunning ? nil : gap.endDate
                )
            }
            .listRowBackground(Color.clear)
            .listRowSeparator(.hidden)
            .listRowInsets(EdgeInsets(top: 5, leading: 14, bottom: 5, trailing: 14))
        }
    }

    private var headerPills: some View {
        HStack(spacing: 6) {
            HStack(spacing: 6) {
                Circle().fill(DesignPalette.ok).frame(width: 6, height: 6)
                Text("UID \(store.currentUser?.id ?? 0)")
            }
            .mono(13, weight: .semibold)
            .foregroundStyle(DesignPalette.ink2)
            .padding(.horizontal, 11)
            .padding(.vertical, 6)
            .chipBackground()

            Spacer()

            Button(action: openSettings) {
                Image(systemName: "ellipsis")
                    .font(.jbm(15, weight: .bold))
                    .foregroundStyle(DesignPalette.ink2)
                    .frame(width: 34, height: 34)
                    .chipBackground()
            }
            .buttonStyle(.plain)
        }
    }

    /// Default is one banner. A critical first item opens one additional slot,
    /// matching the design taxonomy without letting History become an inbox.
    private var visibleCoachSignals: [CoachSignal] {
        let signals = store.presentableCoachSignals
        guard let first = signals.first else { return [] }
        return Array(signals.prefix(first.severity == "critical" ? 2 : 1))
    }

    private var streakStrip: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("\(workoutsInLast28Days)")
                        .display(size: 26, weight: .heavy)
                        .foregroundStyle(DesignPalette.ink)
                    Text("тренировок")
                        .mono(13, weight: .semibold)
                        .foregroundStyle(DesignPalette.ink3)
                }
                Text("За последние 4 недели")
                    .mono(12)
                    .foregroundStyle(DesignPalette.ink3)
            }
            Spacer(minLength: 8)
            // 28-day heatmap (7 cols x 4 rows)
            VStack(alignment: .trailing, spacing: 3) {
                let dots = recentHeatmap
                ForEach(0..<4, id: \.self) { row in
                    HStack(spacing: 3) {
                        ForEach(0..<7, id: \.self) { col in
                            let idx = row * 7 + col
                            RoundedRectangle(cornerRadius: 2)
                                .fill(dots[idx] ? DesignPalette.accent : Color.black.opacity(0.08))
                                .frame(width: 8, height: 8)
                        }
                    }
                }
            }
            VStack(alignment: .center, spacing: 3) {
                Image(systemName: "chevron.right")
                    .font(.jbm(12, weight: .heavy))
                    .foregroundStyle(DesignPalette.accent)
                Text("Прогресс")
                    .font(.jbm(8.5, weight: .heavy))
                    .tracking(0.6)
                    .textCase(.uppercase)
                    .foregroundStyle(DesignPalette.accent)
            }
            .padding(.leading, 2)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .liquidGlass(radius: 20)
    }

    // Mirror HistoryNextWorkoutCard's own state machine so we don't reserve an
    // empty List row (with insets) when the card renders nothing.
    private var showsCoachStrip: Bool {
        guard let rec = store.recommendation else { return false }
        if store.isRefreshingRecommendation { return true }
        switch rec.status ?? "none" {
        case "failed": return false
        case "ready": return rec.recommendation != nil
        default: return true
        }
    }

    // Deep-links from the signal banner. Unknown action types do nothing —
    // the banner text still stands on its own (server-driven taxonomy).
    private func handleSignalAction(_ signal: CoachSignal) {
        switch signal.action?.type {
        case "open_measurements":
            store.measurementsMetric = signal.action?.target == "waist" ? .waist : .weight
            store.currentTab = .weight
        case "open_next_workout":
            store.currentTab = .trainings
        case "refresh_recommendation":
            store.currentTab = .trainings
            Task { await store.refreshRecommendation() }
        case "open_weekly_report":
            isShowingWeeklyReport = true
        default:
            break
        }
    }

    private var deleteEventBinding: Binding<Bool> {
        Binding(
            get: { pendingDeleteEvent != nil },
            set: { if !$0 { pendingDeleteEvent = nil } }
        )
    }

    private var deleteWorkoutBinding: Binding<Bool> {
        Binding(
            get: { pendingDeleteWorkout != nil },
            set: { if !$0 { pendingDeleteWorkout = nil } }
        )
    }

    private var workoutsInLast28Days: Int {
        let cal = Calendar.current
        let today = cal.startOfDay(for: Date())
        guard let start = cal.date(byAdding: .day, value: -27, to: today) else {
            return store.workouts.count
        }
        return store.workouts.filter { w in
            let d = cal.startOfDay(for: DateTools.date(from: w.workoutDate))
            return d >= start && d <= today
        }.count
    }

    private var recentHeatmap: [Bool] {
        let cal = Calendar.current
        let today = cal.startOfDay(for: Date())
        let workoutDates = Set(
            store.workouts.compactMap { w -> Date? in
                cal.startOfDay(for: DateTools.date(from: w.workoutDate))
            })
        return (0..<28).map { offset in
            guard let d = cal.date(byAdding: .day, value: -(27 - offset), to: today) else {
                return false
            }
            return workoutDates.contains(d)
        }
    }
}

private func historyLoadChip(_ type: String) -> (label: String, color: Color) {
    switch type {
    case "heavy": return ("Тяжёлая", DesignPalette.bad)
    case "light": return ("Лёгкая", DesignPalette.ok)
    default: return ("Средняя", DesignPalette.warn)
    }
}

// Compact "следующая тренировка" card — the AI recommendation rendered as a
// FUTURE workout in the same date-rail family as HistoryCard, sitting near the
// top of История just below the stats strip. Tap drills into the full CoachCard on the
// «Тренировка» tab. Mirrors the Claude Design `CoachCompact` (ready/pending/none);
// `failed` is owned by the full card, so История stays calm and shows nothing.
private struct HistoryNextWorkoutCard: View {
    @EnvironmentObject private var store: TrainerStore

    var body: some View {
        if let rec = store.recommendation {
            content(for: rec)
        }
    }

    @ViewBuilder
    private func content(for rec: RecommendationResponse) -> some View {
        let status = rec.status ?? "none"
        if store.isRefreshingRecommendation || status == "pending" {
            pendingRow
        } else if let payload = rec.recommendation, status != "failed" {
            readyCard(payload)
        } else if status == "failed" {
            EmptyView()
        } else {
            noneRow
        }
    }

    // MARK: ready

    private func readyCard(_ payload: RecommendationPayload) -> some View {
        Button {
            store.currentTab = .trainings
        } label: {
            HStack(spacing: 0) {
                dateRail(payload)
                rightSide(payload)
            }
            .background(
                RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .fill(Color(red: 0.984, green: 0.980, blue: 0.969))  // #FBFAF7
            )
            .overlay(
                RoundedRectangle(cornerRadius: 20, style: .continuous)
                    .stroke(Color.black.opacity(0.08), lineWidth: 0.5)
            )
            .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
            .shadow(color: .black.opacity(0.02), radius: 1, y: 1)
            .shadow(color: .black.opacity(0.06), radius: 10, y: 6)
        }
        .buttonStyle(.plain)
    }

    // Accent-tinted rail showing the planned (next) session date the coach
    // picked — mirrors the history date rail, but warm instead of grey to read
    // as "upcoming". Bottom label is the relative day when known (СЕГОДНЯ/ЗАВТРА).
    private func dateRail(_ payload: RecommendationPayload) -> some View {
        let date = plannedDate(payload)
        return VStack {
            VStack(spacing: 2) {
                Text(ruDate("d", date))
                    .font(.jbm(28, weight: .heavy))
                    .tracking(-0.04 * 28)
                    .foregroundStyle(DesignPalette.ink)
                Text(ruDate("LLL", date).uppercased())
                    .tLabel()
            }
            Rectangle()
                .fill(DesignPalette.accent.opacity(0.30))
                .frame(width: 22, height: 0.5)
                .padding(.vertical, 4)
            VStack(spacing: 2) {
                Text(ruDate("EE", date).uppercased())
                    .tLabel()
                    .foregroundStyle(DesignPalette.accent)
                Text(planLabel(payload))
                    .tLabel(size: 9.5)
            }
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 14)
        .frame(width: 64)
        .frame(maxHeight: .infinity)
        .background(DesignPalette.accent.opacity(0.05))
        .overlay(alignment: .trailing) {
            Rectangle()
                .fill(DesignPalette.accent.opacity(0.13))
                .frame(width: 0.5)
        }
    }

    private func rightSide(_ payload: RecommendationPayload) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 7) {
                Image(systemName: "sparkles")
                    .font(.system(size: 13))
                    .foregroundStyle(DesignPalette.accent)
                Text("След. тренировка")
                    .tLabel()
                Spacer(minLength: 6)
                if let phaseChip = CoachPhaseChip.make(payload.coachContext) {
                    phaseChip
                }
                loadBadge(payload.loadType)
            }
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(payload.exercises.enumerated()), id: \.element.exerciseID) {
                    idx, ex in
                    if idx > 0 {
                        Rectangle().fill(Color.black.opacity(0.07)).frame(height: 0.5)
                    }
                    exerciseRow(ex)
                }
            }
            .padding(.top, 9)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func loadBadge(_ type: String) -> some View {
        let chip = historyLoadChip(type)
        return HStack(spacing: 4) {
            Circle().fill(chip.color).frame(width: 5, height: 5)
            Text(chip.label.uppercased())
                .font(.jbm(9, weight: .bold))
                .tracking(0.4)
                .foregroundStyle(chip.color)
        }
        .fixedSize()
    }

    private func exerciseRow(_ ex: RecommendedExercise) -> some View {
        let plan = ex.sets.map(\.weight).max() ?? 0
        let prev = TrainerLogic.latestWorkingWeight(in: store.workouts, exerciseID: ex.exerciseID)
        let up = (prev ?? plan) < plan
        return HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(ExerciseGlyph.short(name: ex.name))
                .font(.jbm(12.5, weight: .semibold))
                .tracking(-0.15)
                .foregroundStyle(DesignPalette.ink)
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            deltaText(
                prev: prev, plan: plan, reps: TrainerLogic.recommendationRepsLabel(ex.sets), up: up
            )
            .font(.jbm(11.5, weight: .semibold))
            .monospacedDigit()
            .fixedSize()
        }
        .padding(.vertical, 5)
    }

    // "было → план": previous working weight in grey, planned weight in
    // progress-green when it's a step up, ink otherwise.
    private func deltaText(prev: Double?, plan: Double, reps: String, up: Bool) -> Text {
        let planPart = Text("\(TrainerLogic.formatWeight(plan))кг")
            .foregroundColor(up ? DesignPalette.ok : DesignPalette.ink)
            .fontWeight(.bold)
        let repsPart = Text(" · \(reps)").foregroundColor(DesignPalette.ink4)
        if let prev {
            return Text(TrainerLogic.formatWeight(prev)).foregroundColor(DesignPalette.ink4)
                + Text(" → ").foregroundColor(DesignPalette.ink5)
                + planPart + repsPart
        }
        return planPart + repsPart
    }

    // MARK: pending / none (glass rows, like the stats strip)

    private var pendingRow: some View {
        Button {
            store.currentTab = .trainings
        } label: {
            HStack(spacing: 12) {
                ZStack {
                    Circle().fill(DesignPalette.accent.opacity(0.08))
                        .overlay(
                            Circle().stroke(DesignPalette.accent.opacity(0.18), lineWidth: 0.5))
                    ProgressView().controlSize(.small).tint(DesignPalette.accent)
                }
                .frame(width: 34, height: 34)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Совет тренера").tLabel().foregroundStyle(DesignPalette.ink4)
                    Text("ИИ обновляет план…")
                        .font(.jbm(13, weight: .semibold))
                        .foregroundStyle(DesignPalette.ink2)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 13)
            .liquidGlass(radius: 20)
        }
        .buttonStyle(.plain)
    }

    private var noneRow: some View {
        Button {
            Task { await store.refreshRecommendation() }
        } label: {
            HStack(spacing: 12) {
                ZStack {
                    Circle().fill(DesignPalette.accent.opacity(0.12))
                        .overlay(
                            Circle().stroke(DesignPalette.accent.opacity(0.20), lineWidth: 0.5))
                    Image(systemName: "sparkles").font(.system(size: 16)).foregroundStyle(
                        DesignPalette.accent)
                }
                .frame(width: 34, height: 34)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Совет тренера").tLabel().foregroundStyle(DesignPalette.ink4)
                    Text("Сгенерировать совет")
                        .font(.jbm(13, weight: .semibold))
                        .foregroundStyle(DesignPalette.ink)
                }
                Spacer(minLength: 8)
                Text("Создать")
                    .font(.jbm(12, weight: .bold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 12)
                    .frame(height: 30)
                    .background(DesignPalette.accent, in: Capsule())
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 13)
            .liquidGlass(radius: 20)
        }
        .buttonStyle(.plain)
    }

    // MARK: planned (next) session date — from the coach, falling back to today

    private func plannedDate(_ payload: RecommendationPayload) -> Date {
        if let iso = payload.nextWorkoutDate, !iso.isEmpty {
            return DateTools.date(from: iso)
        }
        return Date()
    }

    private func planLabel(_ payload: RecommendationPayload) -> String {
        switch payload.restDays {
        case 0: return "СЕГОДНЯ"
        case 1: return "ЗАВТРА"
        default: return "ПЛАН"
        }
    }

    private func ruDate(_ format: String, _ date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "ru_RU")
        f.dateFormat = format
        return f.string(from: date).replacingOccurrences(of: ".", with: "")
    }
}

// Date-rail HistoryCard: left 64px column with day number + month label +
// accent weekday + duration; right side a compact list of exercises (short
// name + grouped set string). Light bg, no intensity bars, no "LATEST" chip.
private struct HistoryCard: View {
    var workout: Workout

    private var workoutDate: Date { DateTools.date(from: workout.workoutDate) }

    private var dayNumber: String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "ru_RU")
        f.dateFormat = "d"
        return f.string(from: workoutDate)
    }

    private var monthShort: String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "ru_RU")
        f.dateFormat = "LLL"
        return f.string(from: workoutDate)
            .replacingOccurrences(of: ".", with: "")
    }

    private var weekdayShort: String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "ru_RU")
        f.dateFormat = "EE"
        return f.string(from: workoutDate)
            .replacingOccurrences(of: ".", with: "")
    }

    private var durationLabel: String {
        "\(TrainerLogic.workoutDurationMinutes(workout)) МИН"
    }

    var body: some View {
        HStack(spacing: 0) {
            dateRail
            exerciseList
        }
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(Color(red: 0.984, green: 0.980, blue: 0.969))  // #FBFAF7
        )
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(Color.black.opacity(0.08), lineWidth: 0.5)
        )
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(color: .black.opacity(0.02), radius: 1, y: 1)
        .shadow(color: .black.opacity(0.06), radius: 10, y: 6)
    }

    private var dateRail: some View {
        VStack {
            VStack(spacing: 2) {
                Text(dayNumber)
                    .font(.jbm(28, weight: .heavy))
                    .tracking(-0.04 * 28)
                    .foregroundStyle(DesignPalette.ink)
                Text(monthShort.uppercased())
                    .tLabel()
            }

            Rectangle()
                .fill(Color.black.opacity(0.10))
                .frame(width: 22, height: 0.5)
                .padding(.vertical, 4)

            VStack(spacing: 2) {
                Text(weekdayShort.uppercased())
                    .tLabel()
                    .foregroundStyle(DesignPalette.accent)
                Text(durationLabel)
                    .tLabel(size: 9.5)
            }
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 14)
        .frame(width: 64)
        .frame(maxHeight: .infinity)
        .background(Color.black.opacity(0.045))
        .overlay(alignment: .trailing) {
            Rectangle()
                .fill(Color.black.opacity(0.08))
                .frame(width: 0.5)
        }
    }

    private var exerciseList: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(workout.data.exercises.enumerated()), id: \.element.exerciseID) {
                idx, ex in
                if idx > 0 {
                    Rectangle().fill(Color.black.opacity(0.07)).frame(height: 0.5)
                }
                HistoryExerciseRow(exercise: ex)
            }

            // Заметку к тренировке видно здесь и больше нигде: текст, который
            // нельзя перечитать, незачем и вводить.
            if let note = workout.data.notes?.nilIfBlank {
                Rectangle().fill(Color.black.opacity(0.07)).frame(height: 0.5)
                HStack(alignment: .top, spacing: 7) {
                    Image(systemName: "text.quote")
                        .font(.jbm(10, weight: .semibold))
                        .foregroundStyle(DesignPalette.ink4)
                        .padding(.top, 1)
                    Text(note)
                        .font(.jbm(11))
                        .italic()
                        .foregroundStyle(DesignPalette.ink3)
                        .lineLimit(4)
                        .multilineTextAlignment(.leading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.top, 7)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// One row inside HistoryCard: short exercise name on the left, grouped set
// string mono-right. No glyph icon — typography only.
private struct HistoryExerciseRow: View {
    var exercise: LoggedExercise

    var body: some View {
        let summary = TrainerLogic.summarizeExerciseSets(exercise.sets)
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(ExerciseGlyph.short(name: exercise.name))
                    .font(.jbm(13, weight: .semibold))
                    .tracking(-0.2)
                    .foregroundStyle(DesignPalette.ink)
                    .frame(width: 78, alignment: .leading)
                    .lineLimit(1)

                HStack(spacing: 3) {
                    ForEach(Array(summary.segments.enumerated()), id: \.offset) { i, seg in
                        Text(seg.label)
                            .mono(12, weight: .regular)
                            .foregroundStyle(DesignPalette.ink2)
                        if let effort = seg.effort, effort == .hard {
                            Text("😣").font(.jbm(11))
                        }
                        if i != summary.segments.count - 1 {
                            Text(",")
                                .mono(12)
                                .foregroundStyle(DesignPalette.ink2)
                        }
                    }
                }
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .trailing)
            }

            // Заметка к подходу объясняет вес: «канат вместо прямой ручки» —
            // это другая постановка, а не откат силы.
            let notesLine = TrainerLogic.setNotesLine(summary.notes)
            if !notesLine.isEmpty {
                Text(notesLine)
                    .font(.jbm(10.5))
                    .italic()
                    .foregroundStyle(DesignPalette.ink3)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.vertical, 6.5)
    }
}

// MARK: - События (периоды без тренировок)
//
// Событие — это текст с датами и ничего больше: ни одного числа из него не
// считается, ни один график его не видит. В интерфейсе оно живёт ровно там,
// где объясняет дырку в ленте, плюс плашкой на «Сегодня», пока идёт.

/// Что открыло композер: подсказка из разрыва (даты уже известны) или правка
/// существующего события.
enum EventComposerMode: Identifiable {
    case new(start: String, end: String?)
    case edit(TrainingEvent)

    var id: String {
        switch self {
        case .new(let start, let end): "new-\(start)-\(end ?? "open")"
        case .edit(let event): "edit-\(event.id)"
        }
    }
}

/// Карточка события в ленте «Истории». Та же порода, что карточка тренировки:
/// тот же радиус, та же рельса 64 pt. Но не бумага, а незалитый блок с
/// пунктиром — в ленте это буквально дырка, на которой оставили подпись.
/// Тренировки остаются главным содержимым.
struct EventCard: View {
    var event: TrainingEvent
    var today: String
    var onClose: () -> Void

    private var isOpen: Bool { event.isOpen }

    private var strokeColor: Color {
        isOpen ? DesignPalette.accent.opacity(0.35) : DesignPalette.ink.opacity(0.20)
    }

    var body: some View {
        HStack(spacing: 0) {
            rail
            VStack(alignment: .leading, spacing: 8) {
                // Длинный текст режется: лента рассчитана на короткие строки.
                // Целиком его видно в правке — тап по карточке её и открывает.
                Text(event.text)
                    .font(.jbm(13.5))
                    .lineSpacing(4)
                    .foregroundStyle(DesignPalette.ink2)
                    .lineLimit(4)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)

                if isOpen {
                    Button(action: onClose) {
                        Text("Закончилось")
                            .font(.jbm(11.5, weight: .bold))
                            .foregroundStyle(DesignPalette.ink)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 6)
                            .chipBackground()
                    }
                    .buttonStyle(.pressable(scale: 0.96))
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(isOpen ? DesignPalette.accent.opacity(0.04) : Color.clear)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
                .foregroundStyle(strokeColor)
        )
    }

    private var rail: some View {
        let labels = TrainerLogic.eventRailLabels(event, today: today)
        let isRange = labels.day.contains("–")

        return VStack(spacing: 0) {
            VStack(spacing: 3) {
                if isOpen {
                    HStack(alignment: .firstTextBaseline, spacing: 0) {
                        Text("с ")
                            .font(.jbm(12))
                            .foregroundStyle(DesignPalette.ink3)
                        Text(labels.day)
                            .font(.jbm(19, weight: .bold))
                            .tracking(-0.5)
                            .foregroundStyle(DesignPalette.ink)
                    }
                } else {
                    Text(labels.day)
                        .font(.jbm(isRange ? 16 : 28, weight: .bold))
                        .tracking(isRange ? -0.6 : -0.04)
                        .foregroundStyle(DesignPalette.ink)
                }

                Text(labels.month)
                    .tLabel(size: labels.month.contains("–") ? 9 : 10.5)
            }

            Spacer(minLength: 4)

            Rectangle()
                .fill(DesignPalette.ink.opacity(0.10))
                .frame(width: 22, height: 0.5)
                .padding(.vertical, 4)

            HStack(spacing: 4) {
                if isOpen {
                    Circle()
                        .fill(DesignPalette.accent)
                        .frame(width: 5, height: 5)
                        .overlay(
                            Circle()
                                .stroke(DesignPalette.accent.opacity(0.15), lineWidth: 3)
                        )
                }
                Text(isOpen ? "идёт" : "\(event.dayCount(today: today)) дн.")
                    .tLabel(
                        size: 9,
                        color: isOpen ? DesignPalette.accent : DesignPalette.ink4
                    )
            }
        }
        .padding(.horizontal, 5)
        .padding(.vertical, 13)
        .frame(width: 64)
        .frame(maxHeight: .infinity)
        .overlay(alignment: .trailing) {
            VerticalDashedLine(color: DesignPalette.ink.opacity(0.18))
        }
    }
}

/// Подсказка в самой дырке — единственная точка входа в событие. Свайп влево
/// на «Истории» занят удалением, свободных целей на экране нет, поэтому вход
/// контекстный: строка появляется ровно там, где разрыв между тренировками
/// длиннее порога. Побочный эффект и есть главное свойство — интерфейс
/// физически не может предложить событие на дату, где уже есть тренировка.
struct EventGapPromptRow: View {
    var gap: TrainerLogic.HistoryGap
    var onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 8) {
                Text("\(gap.days) дн. без тренировок")
                    .tLabel(size: 9.5, color: DesignPalette.ink4)

                Spacer(minLength: 8)

                Text("отметить событие")
                    .font(.jbm(11.5, weight: .bold))
                    .foregroundStyle(DesignPalette.accent)

                Image(systemName: "chevron.right")
                    .font(.jbm(9, weight: .bold))
                    .foregroundStyle(DesignPalette.accent)
            }
            .padding(.horizontal, 14)
            .frame(maxWidth: .infinity)
            .frame(height: 42)
            .background(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
                    .foregroundStyle(DesignPalette.ink.opacity(0.16))
            )
        }
        .buttonStyle(.pressable(scale: 0.98))
    }
}

/// Плашка открытого события на «Сегодня». Состояние, а не упрёк: нейтральные
/// чернила, без красного и без слова «пропущено».
struct TodayEventStrip: View {
    var event: TrainingEvent
    var today: String
    var onTap: () -> Void
    var onClose: () -> Void

    private var headline: String {
        let firstLine = event.text.split(separator: "\n").first.map(String.init) ?? event.text
        return firstLine.trimmingCharacters(in: .whitespaces)
    }

    var body: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(DesignPalette.accent)
                .frame(width: 7, height: 7)
                .overlay(Circle().stroke(DesignPalette.accent.opacity(0.15), lineWidth: 3))

            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 4) {
                    Text(headline)
                        .font(.jbm(13.5, weight: .bold))
                        .tracking(-0.2)
                        .foregroundStyle(DesignPalette.ink)
                        .lineLimit(1)
                    Text("· \(event.dayCount(today: today)) дн.")
                        .mono(13.5, weight: .semibold)
                        .foregroundStyle(DesignPalette.ink3)
                        .lineLimit(1)
                        .layoutPriority(1)
                }
                Text("План сегодня легче обычного")
                    .font(.jbm(11))
                    .foregroundStyle(DesignPalette.ink3)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
            .onTapGesture(perform: onTap)

            Button(action: onClose) {
                Text("Закончилась?")
                    .font(.jbm(11.5, weight: .bold))
                    .foregroundStyle(DesignPalette.ink)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 7)
                    .chipBackground()
            }
            .buttonStyle(.pressable(scale: 0.96))
        }
        .padding(EdgeInsets(top: 9, leading: 13, bottom: 9, trailing: 10))
        .liquidGlass(radius: 18)
    }
}

/// Переключатель «ещё идёт». Системный Toggle в моно-язык не приведён, а этот
/// нужен ровно в одном месте — поэтому он и живёт рядом с композером.
private struct EventRunningSwitch: View {
    var isOn: Bool

    var body: some View {
        Capsule()
            .fill(isOn ? DesignPalette.accent : DesignPalette.ink.opacity(0.14))
            .frame(width: 46, height: 28)
            .overlay(alignment: isOn ? .trailing : .leading) {
                Circle()
                    .fill(Color.white)
                    .frame(width: 22, height: 22)
                    .shadow(color: DesignPalette.ink.opacity(0.28), radius: 1.5, y: 1)
                    .padding(.horizontal, 3)
            }
            .animation(.spring(response: 0.24, dampingFraction: 0.8), value: isOn)
    }
}

private struct EventDateField: View {
    var label: String
    var value: String
    var muted: Bool
    var onTap: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .tLabel(size: 9)

            Button(action: onTap) {
                Text(value)
                    .font(.jbm(15, weight: .bold))
                    .tracking(-0.2)
                    .foregroundStyle(muted ? DesignPalette.ink4 : DesignPalette.ink)
                    .frame(maxWidth: .infinity)
                    .frame(height: 46)
                    .background(fieldBackground)
            }
            .buttonStyle(.pressable(scale: 0.97))
        }
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private var fieldBackground: some View {
        if muted {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
                .foregroundStyle(DesignPalette.ink.opacity(0.14))
        } else {
            ZStack {
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(Color.white.opacity(0.6))
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .stroke(DesignPalette.ink.opacity(0.10), lineWidth: 0.5)
            }
        }
    }
}

struct EventComposerSheet: View {
    var mode: EventComposerMode

    @EnvironmentObject private var store: TrainerStore
    @Environment(\.dismiss) private var dismiss

    @State private var startDate: Date
    @State private var endDate: Date
    @State private var isRunning: Bool
    @State private var text: String
    @State private var openPicker: PickedField?
    @State private var isConfirmingDelete = false
    @State private var detent: PresentationDetent = .height(560)

    private enum PickedField { case start, end }

    /// Пресеты подставляют текст, а не категорию: категории в данных нет и не
    /// будет — это миграция и ещё одно место синхронизации ради иконки.
    private static let presets = ["Болезнь", "Травма", "Поездка", "Не спал", "Зал закрыт"]

    init(mode: EventComposerMode) {
        self.mode = mode
        switch mode {
        case .new(let start, let end):
            _startDate = State(initialValue: DateTools.date(from: start))
            _endDate = State(initialValue: DateTools.date(from: end ?? start))
            _isRunning = State(initialValue: end == nil)
            _text = State(initialValue: "")
        case .edit(let event):
            _startDate = State(initialValue: DateTools.date(from: event.startDate))
            _endDate = State(initialValue: DateTools.date(from: event.endDate ?? event.startDate))
            _isRunning = State(initialValue: event.isOpen)
            _text = State(initialValue: event.text)
        }
    }

    private var isEditing: Bool {
        if case .edit = mode { return true }
        return false
    }

    private var editedEvent: TrainingEvent? {
        if case .edit(let event) = mode { return event }
        return nil
    }

    /// Открытое событие одно. Если оно уже есть и правим мы не его — «ещё идёт»
    /// недоступно: иначе backend откажет уже на сохранении.
    private var canRun: Bool {
        guard let open = store.openEvent else { return true }
        return open.id == editedEvent?.id
    }

    private var dayCount: Int {
        let start = DateTools.iso(from: startDate)
        let end = isRunning ? DateTools.localTodayISO() : DateTools.iso(from: endDate)
        return max(1, DateTools.daysBetween(start, max(end, start)) + 1)
    }

    private var canSave: Bool {
        text.nilIfBlank != nil && !store.isSavingEvent
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header

            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    datesRow
                        .padding(.top, 14)

                    if let openPicker {
                        datePicker(for: openPicker)
                            .padding(.top, 10)
                    }

                    runningRow
                        .padding(.top, 10)

                    presetsRow
                        .padding(.top, 12)

                    MonoTextArea(placeholder: "болел, температура", text: $text)
                        .padding(.top, 10)

                    // Отказ сервера — второе открытое событие, будущая дата,
                    // нет связи — строкой под полем: тост рисуется ПОД шитом,
                    // и на этом экране его физически не видно.
                    if let eventError = store.eventError {
                        Text(eventError)
                            .font(.jbm(11.5, weight: .semibold))
                            .foregroundStyle(DesignPalette.bad)
                            .multilineTextAlignment(.leading)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.top, 8)
                    }
                }
            }
            .scrollIndicators(.hidden)

            actionsRow
                .padding(.top, 14)

            Text("Тренер перечитает план — как после нового замера")
                .font(.jbm(11))
                .foregroundStyle(DesignPalette.ink4)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)
                .padding(.top, 9)
        }
        .padding(.horizontal, 16)
        .padding(.top, 16)
        .padding(.bottom, 26)
        .background(WarmWallpaper())
        .presentationDetents([.height(560), .large], selection: $detent)
        .presentationDragIndicator(.visible)
        .interactiveDismissDisabled(store.isSavingEvent)
        .onAppear { store.eventError = nil }
        .onChange(of: startDate) { _, newValue in
            // Конец раньше начала невозможен по построению: поле конца
            // подтягивается за началом, а его пикер ограничен снизу.
            if endDate < newValue { endDate = newValue }
        }
        .confirmationDialog(
            "Удалить событие?",
            isPresented: $isConfirmingDelete,
            titleVisibility: .visible
        ) {
            Button("Удалить", role: .destructive) {
                guard let event = editedEvent else { return }
                Task {
                    if await store.deleteEvent(event) { dismiss() }
                }
            }
            Button("Отмена", role: .cancel) {}
        }
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 2) {
                Text(isEditing ? "Правка события" : "Новое событие")
                    .tLabel()
                Text("Дни без тренировок")
                    .font(.jbm(17, weight: .bold))
                    .tracking(-0.3)
                    .foregroundStyle(DesignPalette.ink)
            }

            Spacer()

            Button {
                dismiss()
            } label: {
                Image(systemName: "xmark")
                    .font(.jbm(11, weight: .bold))
                    .foregroundStyle(DesignPalette.ink3)
                    .frame(width: 34, height: 34)
                    .chipBackground()
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Закрыть")
        }
    }

    private var datesRow: some View {
        HStack(alignment: .bottom, spacing: 8) {
            EventDateField(
                label: "Начало",
                value: DateTools.short(DateTools.iso(from: startDate)),
                muted: false,
                onTap: { toggle(.start) }
            )

            Text("—")
                .font(.jbm(13))
                .foregroundStyle(DesignPalette.ink4)
                .padding(.bottom, 15)

            EventDateField(
                label: "Конец",
                value: isRunning ? "идёт" : DateTools.short(DateTools.iso(from: endDate)),
                muted: isRunning,
                onTap: {
                    // Пока событие «идёт», конца нет и выбирать нечего —
                    // сначала переключатель.
                    guard !isRunning else { return }
                    toggle(.end)
                }
            )

            VStack(spacing: 2) {
                Text("\(dayCount)")
                    .display(size: 20, weight: .bold)
                    .foregroundStyle(isRunning ? DesignPalette.accent : DesignPalette.ink)
                Text("дн.")
                    .tLabel(size: 9)
            }
            .frame(width: 52)
            .padding(.bottom, 4)
        }
    }

    @ViewBuilder
    private func datePicker(for field: PickedField) -> some View {
        // Системный DatePicker — уже принятый прецедент (композер замеров);
        // будущее он запрещает так же, как backend.
        DatePicker(
            "",
            selection: field == .start ? $startDate : $endDate,
            in: (field == .start ? Date.distantPast : startDate)...Date(),
            displayedComponents: .date
        )
        .datePickerStyle(.graphical)
        .labelsHidden()
        .tint(DesignPalette.accent)
        .padding(.horizontal, 6)
        .padding(.vertical, 4)
        .glassCard(radius: 18)
    }

    private var runningRow: some View {
        Button {
            guard canRun else { return }
            withAnimation(.spring(response: 0.26, dampingFraction: 0.85)) {
                isRunning.toggle()
                if isRunning { openPicker = nil }
            }
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 1) {
                    Text("Ещё идёт")
                        .font(.jbm(14, weight: .bold))
                        .tracking(-0.2)
                        .foregroundStyle(DesignPalette.ink)
                    Text(
                        canRun
                            ? "Закроется само, когда запишешь тренировку"
                            : "Одно открытое событие уже есть"
                    )
                    .font(.jbm(11))
                    .foregroundStyle(DesignPalette.ink3)
                    .multilineTextAlignment(.leading)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                EventRunningSwitch(isOn: isRunning)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .glassCard(radius: 16)
        }
        .buttonStyle(.plain)
        .opacity(canRun ? 1 : 0.55)
    }

    private var presetsRow: some View {
        WrapLayout(spacing: 6) {
            ForEach(Self.presets, id: \.self) { preset in
                Button {
                    apply(preset: preset)
                } label: {
                    presetChip(
                        preset,
                        isPicked: text.trimmingCharacters(in: .whitespacesAndNewlines) == preset
                    )
                }
                .buttonStyle(.pressable(scale: 0.96))
            }
        }
    }

    @ViewBuilder
    private func presetChip(_ preset: String, isPicked: Bool) -> some View {
        let label =
            Text(preset)
            .font(.jbm(12.5, weight: .bold))
            .tracking(-0.15)
            .foregroundStyle(isPicked ? Color.white : DesignPalette.ink2)
            .padding(.horizontal, 13)
            .padding(.vertical, 8)

        if isPicked {
            label.background(DesignPalette.ink, in: Capsule())
        } else {
            label.chipBackground()
        }
    }

    private var actionsRow: some View {
        HStack(spacing: 8) {
            if isEditing {
                Button {
                    isConfirmingDelete = true
                } label: {
                    Image(systemName: "trash")
                        .font(.jbm(16, weight: .semibold))
                        .foregroundStyle(DesignPalette.bad)
                        .frame(width: 56, height: 54)
                        .background(
                            RoundedRectangle(cornerRadius: 27, style: .continuous)
                                .fill(DesignPalette.bad.opacity(0.06))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 27, style: .continuous)
                                .stroke(DesignPalette.bad.opacity(0.20), lineWidth: 0.5)
                        )
                }
                .buttonStyle(.pressable(scale: 0.96))
                .accessibilityLabel("Удалить событие")
            }

            Button {
                Task { await save() }
            } label: {
                HStack(spacing: 8) {
                    if store.isSavingEvent {
                        ProgressView().tint(.white)
                    }
                    Text(isEditing ? "Сохранить" : "Добавить событие")
                        .font(.jbm(16, weight: .bold))
                        .tracking(-0.2)
                }
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity)
                .frame(height: 54)
                .background(
                    DesignPalette.ink,
                    in: RoundedRectangle(cornerRadius: 27, style: .continuous)
                )
                .shadow(color: DesignPalette.ink.opacity(0.30), radius: 12, y: 6)
            }
            .buttonStyle(.pressable(scale: 0.97))
            .disabled(!canSave)
            .opacity(canSave ? 1 : 0.5)
        }
    }

    private func toggle(_ field: PickedField) {
        withAnimation(.spring(response: 0.28, dampingFraction: 0.86)) {
            openPicker = openPicker == field ? nil : field
            // Календарь высокий — на маленьком детенте он не помещается.
            if openPicker != nil { detent = .large }
        }
    }

    /// Пресет — это подстановка текста. Пустое поле и поле ровно с другим
    /// пресетом заменяются целиком; написанное руками не затирается — пресет
    /// дописывается в конец. Повторный тап по выбранному очищает поле, так что
    /// промах всегда обратим.
    private func apply(preset: String) {
        let current = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if current == preset {
            text = ""
        } else if current.isEmpty || Self.presets.contains(current) {
            text = preset
        } else {
            text = "\(current), \(preset.lowercased())"
        }
    }

    private func save() async {
        let start = DateTools.iso(from: startDate)
        let end = isRunning ? nil : DateTools.iso(from: endDate)
        let saved: Bool
        if let event = editedEvent {
            saved = await store.updateEvent(event, startDate: start, endDate: end, text: text)
        } else {
            saved = await store.saveEvent(startDate: start, endDate: end, text: text)
        }
        if saved { dismiss() }
    }
}

// MARK: - Заметка к тренировке

/// Полоска после сохранения тренировки. Завершение остаётся одним нажатием:
/// тренировка уже записана, заметку предлагает полоска, которая сама уедет, —
/// её можно просто не заметить.
struct FinishWorkoutStrip: View {
    var summary: String
    var onNote: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "checkmark")
                .font(.jbm(12, weight: .heavy))
                .foregroundStyle(DesignPalette.ok)
                .frame(width: 26, height: 26)
                .background(DesignPalette.ok.opacity(0.15), in: Circle())

            VStack(alignment: .leading, spacing: 1) {
                Text("Тренировка записана")
                    .font(.jbm(13.5, weight: .bold))
                    .tracking(-0.2)
                    .foregroundStyle(DesignPalette.ink)
                Text(summary)
                    .mono(11)
                    .foregroundStyle(DesignPalette.ink3)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Button(action: onNote) {
                HStack(spacing: 6) {
                    Image(systemName: "pencil")
                        .font(.jbm(11, weight: .semibold))
                    Text("Заметка")
                        .font(.jbm(12, weight: .bold))
                }
                .foregroundStyle(DesignPalette.ink)
                .padding(.horizontal, 13)
                .padding(.vertical, 8)
                .chipBackground()
            }
            .buttonStyle(.pressable(scale: 0.96))
        }
        .padding(EdgeInsets(top: 10, leading: 14, bottom: 10, trailing: 10))
        .glassCard(radius: 20, thick: true)
        .shadow(color: DesignPalette.ink.opacity(0.18), radius: 14, y: 8)
    }
}

/// Открывается только если полоску тронули — либо поздним входом со свайпа
/// карточки в «Истории».
struct WorkoutNoteSheet: View {
    var workout: Workout

    @EnvironmentObject private var store: TrainerStore
    @Environment(\.dismiss) private var dismiss
    @State private var text: String
    @State private var isSaving = false

    init(workout: Workout) {
        self.workout = workout
        _text = State(initialValue: workout.data.notes ?? "")
    }

    private var title: String {
        let date = DateTools.short(workout.workoutDate)
        if let focus = workout.data.focus?.nilIfBlank {
            return "\(date) · \(focus)"
        }
        return "\(date) · \(DateTools.weekday(workout.workoutDate))"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Заметка к тренировке")
                        .tLabel()
                    Text(title)
                        .font(.jbm(17, weight: .bold))
                        .tracking(-0.3)
                        .foregroundStyle(DesignPalette.ink)
                }

                Spacer()

                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark")
                        .font(.jbm(11, weight: .bold))
                        .foregroundStyle(DesignPalette.ink3)
                        .frame(width: 34, height: 34)
                        .chipBackground()
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Закрыть")
            }
            .padding(.bottom, 12)

            MonoTextArea(placeholder: "Как прошло, что мешало", text: $text)

            Button {
                Task {
                    guard let id = workout.id else { return }
                    isSaving = true
                    let saved = await store.saveWorkoutNote(workoutID: id, text: text)
                    isSaving = false
                    if saved { dismiss() }
                }
            } label: {
                HStack(spacing: 8) {
                    if isSaving {
                        ProgressView().tint(.white)
                    }
                    Text("Сохранить")
                        .font(.jbm(16, weight: .bold))
                        .tracking(-0.2)
                }
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity)
                .frame(height: 54)
                .background(
                    DesignPalette.ink,
                    in: RoundedRectangle(cornerRadius: 27, style: .continuous)
                )
                .shadow(color: DesignPalette.ink.opacity(0.30), radius: 12, y: 6)
            }
            .buttonStyle(.pressable(scale: 0.97))
            .padding(.top, 12)
            .disabled(isSaving)

            Text("Уедет тренеру вместе с весами")
                .font(.jbm(11))
                .foregroundStyle(DesignPalette.ink4)
                .frame(maxWidth: .infinity)
                .padding(.top, 9)

            Spacer(minLength: 0)
        }
        .padding(.horizontal, 16)
        .padding(.top, 16)
        .padding(.bottom, 26)
        .background(WarmWallpaper())
        .presentationDetents([.height(320), .large])
        .presentationDragIndicator(.visible)
        .interactiveDismissDisabled(isSaving)
    }
}

// MARK: - Progress

private struct ProgressTabScreen: View {
    @EnvironmentObject private var store: TrainerStore
    @Environment(\.dismiss) private var dismiss
    @State private var showWeeklyReport = false
    @State private var latestWeeklyReport: WeeklyReportEntry?
    @State private var weeklyReportForSheet: WeeklyReportEntry?
    @State private var isFetchingWeeklyReport = false
    @State private var isOpeningWeeklyReport = false
    @State private var didFinishWeeklyReportRequest = false

    var body: some View {
        ZStack {
            WarmWallpaper()
            content
        }
        .toolbar(.hidden, for: .navigationBar)
        .swipeBackOverlay { dismiss() }
        .sheet(isPresented: $showWeeklyReport) {
            WeeklyReportSheet(
                prefetchedEntry: weeklyReportForSheet,
                fetchesOnAppear: false
            )
            .environmentObject(store)
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
        .task {
            // This prefetch only names the exact period. If the user taps while
            // it is still running, the same request drives the card spinner and
            // presents the sheet on completion — no duplicate fetch.
            await loadWeeklyReport()
        }
    }

    private var content: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                headerPills
                TopTitle(sub: nil, title: "Прогресс")
                    .padding(.horizontal, 4)

                weeklySummarySection
                disciplineSection
                weeklyReportSection
                weeklyVolumeSection

                sectionHeader

                let options = store.progressExerciseOptions()
                LazyVStack(spacing: 8) {
                    ForEach(options) { ex in
                        NavigationLink {
                            ExerciseDetailScreen(exerciseID: ex.id, exerciseName: ex.name)
                        } label: {
                            ProgressExerciseRow(exerciseID: ex.id, name: ex.name, store: store)
                        }
                        .buttonStyle(.plain)
                    }

                    if options.isEmpty {
                        EmptyStateCard(
                            glyph: .other,
                            title: "Нет точек прогресса",
                            subtitle: "Сохрани несколько тренировок, чтобы увидеть динамику."
                        )
                    }
                }
            }
            .padding(.horizontal, 14)
            .padding(.top, 8)
            .padding(.bottom, 24)
        }
        .scrollIndicators(.hidden)
    }

    private var headerPills: some View {
        HStack(spacing: 6) {
            Button {
                dismiss()
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.left")
                        .font(.jbm(12, weight: .heavy))
                    Text("История")
                }
                .mono(13, weight: .semibold)
                .foregroundStyle(DesignPalette.ink2)
                .padding(.horizontal, 11)
                .padding(.vertical, 6)
                .chipBackground()
            }
            .buttonStyle(.plain)

            Spacer()
        }
    }

    private var sectionHeader: some View {
        HStack {
            Text("УПРАЖНЕНИЯ")
                .font(.jbm(13, weight: .bold))
                .tracking(0.4)
                .foregroundStyle(DesignPalette.ink3)
            Spacer()
            Text(store.selectedRange.label)
                .font(.jbm(12, weight: .semibold))
                .foregroundStyle(DesignPalette.ink3)
        }
        .padding(.horizontal, 4)
        .padding(.top, 6)
    }

    private func miniHeader(_ title: String, _ trailing: String) -> some View {
        HStack {
            Text(title)
                .font(.jbm(13, weight: .bold)).tracking(0.4)
                .foregroundStyle(DesignPalette.ink3)
            Spacer()
            Text(trailing)
                .font(.jbm(11, weight: .semibold))
                .foregroundStyle(DesignPalette.ink4)
        }
        .padding(.horizontal, 4)
        .padding(.top, 6)
    }

    // MARK: rolling seven-day progress

    /// Rolling algorithmic summary. Its exact per-group breakdown lives in a
    /// separate section below the closed-week report to keep the first screen
    /// focused on the higher-level story.
    private var weeklySummarySection: some View {
        let context = store.recommendation?.recommendation?.coachContext
        let rows = TrainerLogic.weeklyVolumeByGroup(
            store.workouts,
            targets: context?.groupTargets
        )
        let adherence = TrainerLogic.adherenceSummary(store.workouts, range: .days7)
        return VStack(alignment: .leading, spacing: 8) {
            miniHeader("НЕДЕЛЬНЫЙ ПРОГРЕСС", "7 дней")

            WeekCoachSummaryCard(
                rows: rows,
                adherence: adherence,
                context: context,
                hasPlan: store.recommendation?.recommendation != nil,
                basedOnWorkoutCount: store.recommendation?.basedOnWorkoutCount,
                loadType: store.recommendation?.recommendation?.loadType
            )
        }
    }

    private var weeklyVolumeSection: some View {
        let context = store.recommendation?.recommendation?.coachContext
        let rows = TrainerLogic.weeklyVolumeByGroup(
            store.workouts,
            targets: context?.groupTargets
        )
        return VStack(alignment: .leading, spacing: 8) {
            miniHeader("ОБЪЁМ ПО ГРУППАМ", volumeTrailing(context))

            // Targets come from the coach's current block week when available
            // (ramp/deload-aware); static policy ranges are the fallback.
            VStack(spacing: 0) {
                ForEach(Array(rows.enumerated()), id: \.element.id) { idx, row in
                    if idx > 0 {
                        Rectangle().fill(DesignPalette.ink.opacity(0.06)).frame(height: 0.5)
                    }
                    VolumeRow(row: row)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 4)
            .glassCard(radius: 20)
        }
    }

    private func volumeTrailing(_ context: CoachContext?) -> String {
        guard let context else { return "7 дней" }
        if context.deloadWeek == true { return "разгрузка · 7 дней" }
        if let week = context.blockWeek { return "неделя \(week) · 7 дней" }
        return "7 дней"
    }

    // MARK: discipline (plan vs fact)

    private var disciplineSection: some View {
        // Fixed 30-day window — the same one the coach reads server-side when
        // adapting plans to real behaviour; all-time adherence says nothing.
        let summary = TrainerLogic.adherenceSummary(store.workouts, range: .days30)
        return VStack(alignment: .leading, spacing: 8) {
            miniHeader("ДИСЦИПЛИНА", "30 дней")
            DisciplineCard(summary: summary)
        }
    }

    // MARK: weekly coach report (cached server-side by the Monday-midnight timer)

    private var weeklyReportSection: some View {
        Button {
            openWeeklyReport()
        } label: {
            HStack(spacing: 11) {
                ZStack {
                    Circle().fill(DesignPalette.accent.opacity(0.12)).frame(width: 36, height: 36)
                        .overlay(
                            Circle().stroke(DesignPalette.accent.opacity(0.20), lineWidth: 0.5))
                    if isOpeningWeeklyReport {
                        ProgressView()
                            .controlSize(.small)
                            .tint(DesignPalette.accent)
                            .transition(.opacity.combined(with: .scale(scale: 0.8)))
                    } else {
                        Image(systemName: "doc.text")
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(DesignPalette.accent)
                            .transition(.opacity.combined(with: .scale(scale: 0.8)))
                    }
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("Отчёт прошлой недели")
                        .font(.jbm(13.5, weight: .bold))
                        .foregroundStyle(DesignPalette.ink)
                    Text(weeklyReportPeriodLabel)
                        .font(.jbm(10.5, weight: .semibold))
                        .foregroundStyle(DesignPalette.ink3)
                    Text("итоги · ПР · вес и питание · фокус")
                        .font(.jbm(10.5))
                        .foregroundStyle(DesignPalette.ink4)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.jbm(11, weight: .heavy))
                    .foregroundStyle(DesignPalette.ink4)
                    .opacity(isOpeningWeeklyReport ? 0.35 : 1)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .glassCard(radius: 20)
        }
        .buttonStyle(.pressable(scale: 0.98))
        .animation(.easeInOut(duration: 0.16), value: isOpeningWeeklyReport)
        .accessibilityLabel(
            isOpeningWeeklyReport
                ? "Загружаю отчёт прошлой недели"
                : "Открыть отчёт прошлой недели"
        )
    }

    private var weeklyReportPeriodLabel: String {
        if isOpeningWeeklyReport { return "загружаю отчёт…" }
        guard let report = latestWeeklyReport else { return "пока не сформирован" }
        return DateTools.periodLabel(endingAt: report.periodEnd, days: report.days ?? 7)
    }

    private func openWeeklyReport() {
        guard !isOpeningWeeklyReport else { return }
        if didFinishWeeklyReportRequest {
            weeklyReportForSheet = latestWeeklyReport
            showWeeklyReport = true
            return
        }

        // Synchronous state change gives the tap immediate visual feedback.
        // loadWeeklyReport() either starts the request or joins the prefetch
        // already in flight; that request presents the sheet when it finishes.
        isOpeningWeeklyReport = true
        Task { await loadWeeklyReport() }
    }

    @MainActor
    private func loadWeeklyReport() async {
        guard !didFinishWeeklyReportRequest, !isFetchingWeeklyReport else { return }
        isFetchingWeeklyReport = true
        do {
            let report = try await store.requestWeeklyReport()
            latestWeeklyReport = report
            weeklyReportForSheet = report
            didFinishWeeklyReportRequest = true
            isFetchingWeeklyReport = false
            if isOpeningWeeklyReport {
                isOpeningWeeklyReport = false
                showWeeklyReport = true
            }
        } catch is CancellationError {
            isFetchingWeeklyReport = false
            isOpeningWeeklyReport = false
        } catch {
            isFetchingWeeklyReport = false
            if isOpeningWeeklyReport {
                isOpeningWeeklyReport = false
                store.showToast("Не удалось загрузить отчёт. Попробуй ещё раз.")
            }
        }
    }
}

/// High-contrast rolling-seven-day summary from the Claude Design Week screen.
/// It is intentionally static: the exact volume rows later on this screen are
/// its detail, while the separately labelled report button opens the
/// closed-week LLM retrospective.
private struct WeekCoachSummaryCard: View {
    var rows: [MuscleGroupVolume]
    var adherence: AdherenceSummary
    var context: CoachContext?
    var hasPlan: Bool
    var basedOnWorkoutCount: Int?
    var loadType: String?

    private var under: [MuscleGroupVolume] { rows.filter { $0.status == .under } }
    private var over: [MuscleGroupVolume] { rows.filter { $0.status == .over } }
    private var onTargetCount: Int { rows.filter { $0.status == .onTarget }.count }
    private var hasVolume: Bool { rows.contains { $0.count > 0 } }

    private var headline: String {
        if !hasVolume { return "За 7 дней подходов пока нет" }
        if under.isEmpty && over.isEmpty { return "Все группы в рабочем диапазоне" }
        return "\(onTargetCount) из \(rows.count) групп в диапазоне"
    }

    private var explanation: String {
        if !hasVolume {
            return hasPlan
                ? "Тренировок пока нет. План готов, коридоры начнут заполняться после первой сессии."
                : "Тренировок пока нет. После первой сессии здесь появится распределение объёма."
        }
        var facts: [String] = []
        if !under.isEmpty {
            facts.append("Ниже коридора: \(groupNames(under)).")
        }
        if !over.isEmpty {
            facts.append("Выше коридора: \(groupNames(over)).")
        }
        if facts.isEmpty {
            return "За последние 7 дней все группы попали в текущие рабочие коридоры."
        }
        return facts.joined(separator: " ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(DesignPalette.accent)
                    .frame(width: 26, height: 26)
                    .overlay(
                        Image(systemName: "sparkles")
                            .font(.system(size: 11, weight: .bold))
                            .foregroundStyle(.white)
                    )
                Text("СВОДКА ПОСЛЕДНИХ 7 ДНЕЙ")
                    .font(.jbm(9.5, weight: .bold))
                    .tracking(0.7)
                    .foregroundStyle(.white.opacity(0.62))
                Spacer(minLength: 0)
            }

            VStack(alignment: .leading, spacing: 5) {
                Text(headline)
                    .font(.jbm(16, weight: .bold))
                    .tracking(-0.3)
                    .foregroundStyle(.white)
                    .fixedSize(horizontal: false, vertical: true)
                Text(explanation)
                    .font(.jbm(11.5, weight: .medium))
                    .foregroundStyle(.white.opacity(0.70))
                    .lineSpacing(3)
                    .fixedSize(horizontal: false, vertical: true)
            }

            HStack(spacing: 7) {
                if let basedOnWorkoutCount {
                    footerChip("ПО \(basedOnWorkoutCount) ТРЕН.")
                } else if let week = context?.blockWeek {
                    footerChip("НЕДЕЛЯ \(week)")
                }
                if adherence.hasData {
                    footerChip("\(Int((adherence.ratio * 100).rounded()))% ПЛАНА")
                }
                if let loadType {
                    footerChip(historyLoadChip(loadType).label.uppercased())
                }
            }
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: 22, style: .continuous)
                .fill(DesignPalette.ink)
                .overlay(
                    RoundedRectangle(cornerRadius: 22, style: .continuous)
                        .stroke(Color.white.opacity(0.08), lineWidth: 0.5)
                )
                .shadow(color: DesignPalette.ink.opacity(0.24), radius: 18, y: 9)
        )
    }

    private func footerChip(_ text: String) -> some View {
        Text(text)
            .font(.jbm(8.5, weight: .bold))
            .tracking(0.45)
            .foregroundStyle(.white.opacity(0.68))
            .lineLimit(1)
            .padding(.horizontal, 7)
            .padding(.vertical, 5)
            .background(Color.white.opacity(0.08), in: Capsule())
    }

    private func groupNames(_ values: [MuscleGroupVolume]) -> String {
        let visible = values.prefix(3).map(\.name).joined(separator: ", ")
        let hidden = values.count - min(3, values.count)
        return hidden > 0 ? "\(visible) и ещё \(hidden)" : visible
    }
}

// Sheet with the coach's weekly retrospective, rendered from cached Markdown.
// The report is generated by the server timer in the night from Sunday to
// Monday, so opening this costs nothing; before the first one it shows a
// friendly empty state.
private struct WeeklyReportSheet: View {
    @EnvironmentObject private var store: TrainerStore
    @State private var entry: WeeklyReportEntry?
    @State private var isLoading: Bool
    private let fetchesOnAppear: Bool

    init(
        prefetchedEntry: WeeklyReportEntry? = nil,
        fetchesOnAppear: Bool = true
    ) {
        _entry = State(initialValue: prefetchedEntry)
        _isLoading = State(initialValue: fetchesOnAppear)
        self.fetchesOnAppear = fetchesOnAppear
    }

    var body: some View {
        ZStack {
            WarmWallpaper()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    HStack(spacing: 8) {
                        Image(systemName: "doc.text")
                            .font(.system(size: 15))
                            .foregroundStyle(DesignPalette.accent)
                        Text("Отчёт недели")
                            .font(.jbm(11, weight: .bold)).tracking(0.6)
                            .textCase(.uppercase).foregroundStyle(DesignPalette.ink2)
                        Spacer()
                        if let entry {
                            Text("по \(entry.periodEnd)")
                                .font(.jbm(10.5, weight: .semibold))
                                .foregroundStyle(DesignPalette.ink4)
                        }
                    }
                    if isLoading {
                        HStack(spacing: 11) {
                            ProgressView().tint(DesignPalette.accent)
                            Text("Загружаю отчёт…")
                                .font(.jbm(12.5)).foregroundStyle(DesignPalette.ink3)
                        }
                        .padding(.top, 8)
                    } else if let entry {
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(Array(paragraphs(entry.report).enumerated()), id: \.offset) {
                                _, para in
                                Text(markdown(para))
                                    .font(.jbm(13))
                                    .foregroundStyle(DesignPalette.ink2)
                                    .lineSpacing(4)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .padding(.top, 2)
                    } else {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Отчёта пока нет")
                                .font(.jbm(15, weight: .bold)).tracking(-0.3)
                                .foregroundStyle(DesignPalette.ink)
                            Text(
                                "Тренер собирает итоги недели сам — в ночь на понедельник. Загляни утром."
                            )
                            .font(.jbm(12)).foregroundStyle(DesignPalette.ink3)
                            .lineSpacing(3)
                            .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(.top, 8)
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .task {
            if fetchesOnAppear {
                entry = await store.fetchWeeklyReport()
                isLoading = false
            }
            if entry != nil {
                // Reading is a fact, not a snooze: the server-side receipt
                // kills the weekly_report_ready signal for every client.
                store.markWeeklyReportRead()
            }
        }
    }

    private func paragraphs(_ text: String) -> [String] {
        text
            .components(separatedBy: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    private func markdown(_ line: String) -> AttributedString {
        (try? AttributedString(
            markdown: line,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )) ?? AttributedString(line)
    }
}

// One muscle-group volume row: name, set count vs landmark, a fill bar with a
// tick at the lower landmark (where "достаточно" begins).
private struct VolumeRow: View {
    var row: MuscleGroupVolume

    private var color: Color {
        switch row.status {
        case .under: return DesignPalette.ink4
        case .onTarget: return DesignPalette.ok
        case .over: return DesignPalette.warn
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                Text(row.name)
                    .font(.jbm(12.5, weight: .semibold))
                    .foregroundStyle(DesignPalette.ink2)
                    .lineLimit(1)
                Spacer(minLength: 6)
                Text("\(row.count) / \(row.minTarget)–\(row.maxTarget)")
                    .font(.jbm(11, weight: .semibold))
                    .monospacedDigit()
                    .foregroundStyle(color)
            }
            GeometryReader { geo in
                let w = geo.size.width
                ZStack(alignment: .leading) {
                    Capsule().fill(DesignPalette.ink.opacity(0.07)).frame(height: 6)
                    Capsule().fill(color).frame(width: max(6, w * row.fill), height: 6)
                    Rectangle()
                        .fill(DesignPalette.ink.opacity(0.28))
                        .frame(width: 1, height: 11)
                        .offset(
                            x: w * min(1, Double(row.minTarget) / Double(max(1, row.maxTarget))))
                }
            }
            .frame(height: 11)
        }
        .padding(.vertical, 8)
    }
}

// Adherence summary: big percentage + a fill bar + context (workouts compared,
// skipped exercises). Empty hint when nothing was done against a coach plan yet.
private struct DisciplineCard: View {
    var summary: AdherenceSummary

    private var color: Color {
        if summary.ratio >= 0.8 { return DesignPalette.ok }
        if summary.ratio >= 0.5 { return DesignPalette.warn }
        return DesignPalette.bad
    }

    var body: some View {
        Group {
            if summary.hasData {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("\(Int((summary.ratio * 100).rounded()))%")
                            .font(.jbm(28, weight: .heavy)).tracking(-0.5)
                            .foregroundStyle(DesignPalette.ink)
                        Text("подходов из планов тренера")
                            .font(.jbm(12, weight: .semibold))
                            .foregroundStyle(DesignPalette.ink3)
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                    }
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Capsule().fill(DesignPalette.ink.opacity(0.07)).frame(height: 7)
                            Capsule().fill(color).frame(
                                width: max(7, geo.size.width * summary.ratio), height: 7)
                        }
                    }
                    .frame(height: 7)
                    Text(
                        "\(summary.doneSets) из \(summary.plannedSets) плановых подходов · \(summary.comparedWorkouts) трен. по плану"
                    )
                    .font(.jbm(10.5, weight: .medium))
                    .foregroundStyle(DesignPalette.ink3)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                    if !summary.skippedByName.isEmpty {
                        Text("чаще пропускаешь: \(skippedLabel)")
                            .font(.jbm(10.5, weight: .medium))
                            .foregroundStyle(DesignPalette.warn)
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(14)
                .glassCard(radius: 20)
            } else {
                HStack(spacing: 11) {
                    Image(systemName: "checklist")
                        .font(.system(size: 18))
                        .foregroundStyle(DesignPalette.ink4)
                    Text(
                        "За последние 30 дней не было тренировок по плану тренера — дисциплину считать не по чему."
                    )
                    .font(.jbm(12, weight: .medium))
                    .foregroundStyle(DesignPalette.ink3)
                    .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .padding(14)
                .glassCard(radius: 20)
            }
        }
    }

    // «Сгибания ног ×2, Дельты» — the top of the skip list, the actionable part.
    private var skippedLabel: String {
        summary.skippedByName.prefix(3)
            .map { $0.count > 1 ? "\($0.name) ×\($0.count)" : $0.name }
            .joined(separator: ", ")
    }
}

private struct ProgressExerciseRow: View {
    var exerciseID: Int
    var name: String
    @ObservedObject var store: TrainerStore

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 1) {
                Text(name)
                    .font(.jbm(15, weight: .heavy))
                    .tracking(-0.3)
                    .foregroundStyle(DesignPalette.ink)
                    .lineLimit(1)
                if let delta = formattedDelta {
                    Text(delta)
                        .mono(12, weight: .heavy)
                        .foregroundStyle(deltaTint)
                }
            }

            Spacer()

            sparkline
                .frame(width: 76, height: 34)

            Image(systemName: "chevron.right")
                .font(.jbm(12, weight: .heavy))
                .foregroundStyle(DesignPalette.ink3.opacity(0.6))
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .glassCard(radius: 22)
    }

    private var series: [ProgressPoint] {
        TrainerLogic.buildExerciseProgressSeries(
            workouts: store.workouts,
            range: store.selectedRange,
            exerciseID: exerciseID
        )
    }

    private var formattedDelta: String? {
        guard let summary = TrainerLogic.summarizeExerciseSeries(series),
            summary.firstPoint.bestWeight > 0
        else { return nil }
        let pct =
            (summary.latestPoint.bestWeight - summary.firstPoint.bestWeight)
            / summary.firstPoint.bestWeight * 100
        let sign = pct >= 0 ? "+" : ""
        return "\(sign)\(Int(pct.rounded()))%"
    }

    private var deltaTint: Color {
        guard let summary = TrainerLogic.summarizeExerciseSeries(series) else {
            return DesignPalette.ink3
        }
        return summary.latestPoint.bestWeight >= summary.firstPoint.bestWeight
            ? DesignPalette.ok : DesignPalette.bad
    }

    private var sparkline: some View {
        GeometryReader { geo in
            let pts = series.map(\.bestWeight)
            if pts.count >= 2 {
                let mx = pts.max() ?? 1
                let mn = pts.min() ?? 0
                let range = max(mx - mn, 0.0001)
                let stepX = geo.size.width / CGFloat(pts.count - 1)
                let toPoint: (Int) -> CGPoint = { i in
                    CGPoint(
                        x: CGFloat(i) * stepX,
                        y: geo.size.height - CGFloat((pts[i] - mn) / range) * (geo.size.height - 4)
                            - 2
                    )
                }
                Path { p in
                    p.move(to: toPoint(0))
                    for i in 1..<pts.count {
                        p.addLine(to: toPoint(i))
                    }
                }
                .stroke(
                    DesignPalette.accent,
                    style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round))

                Path { p in
                    p.move(to: toPoint(0))
                    for i in 1..<pts.count { p.addLine(to: toPoint(i)) }
                    p.addLine(to: CGPoint(x: geo.size.width, y: geo.size.height))
                    p.addLine(to: CGPoint(x: 0, y: geo.size.height))
                    p.closeSubpath()
                }
                .fill(
                    LinearGradient(
                        colors: [
                            DesignPalette.accent.opacity(0.3), DesignPalette.accent.opacity(0),
                        ],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )
            } else if pts.count == 1 {
                Path { p in
                    p.move(to: CGPoint(x: 0, y: geo.size.height / 2))
                    p.addLine(to: CGPoint(x: geo.size.width, y: geo.size.height / 2))
                }
                .stroke(
                    DesignPalette.accent.opacity(0.4),
                    style: StrokeStyle(lineWidth: 1.5, dash: [3, 3]))
            }
        }
    }
}

// MARK: - Exercise detail

private struct ExerciseDetailScreen: View {
    var exerciseID: Int
    var exerciseName: String
    @EnvironmentObject private var store: TrainerStore
    @Environment(\.dismiss) private var dismiss
    @State private var metric: Metric = .topSet

    enum Metric: String, CaseIterable, Identifiable {
        case topSet = "Топ-сет"
        case weight = "Вес"
        case reps = "Повт."
        case workouts = "Сессий"

        var id: String { rawValue }
    }

    var body: some View {
        ZStack {
            WarmWallpaper()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    topBar

                    headerRow

                    chartCard

                    statsGrid

                    Text("ПОСЛЕДНИЕ СЕТЫ")
                        .tLabel(size: 12)
                        .padding(.horizontal, 4)
                        .padding(.top, 6)

                    recentSets
                }
                .padding(.horizontal, 14)
                .padding(.top, 8)
                .padding(.bottom, 32)
            }
            .scrollIndicators(.hidden)
        }
        .toolbar(.hidden, for: .navigationBar)
        .swipeBackOverlay { dismiss() }
    }

    private var topBar: some View {
        HStack(spacing: 6) {
            Button {
                dismiss()
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.left")
                        .font(.jbm(12, weight: .heavy))
                    Text("Прогресс")
                        .mono(13, weight: .semibold)
                }
                .foregroundStyle(DesignPalette.ink2)
                .padding(.horizontal, 11)
                .padding(.vertical, 6)
                .chipBackground()
            }
            .buttonStyle(.plain)

            Text(ExerciseGlyph.muscle(id: exerciseID))
                .mono(13, weight: .semibold)
                .foregroundStyle(DesignPalette.accent)
                .padding(.horizontal, 11)
                .padding(.vertical, 6)
                .chipBackground()

            Spacer()
        }
    }

    private var headerRow: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(exerciseName)
                .display(size: 28, weight: .bold)
                .foregroundStyle(DesignPalette.ink)
                .lineLimit(2)
            Text("\(series.count) тренировок · \(rangeSubtitle)")
                .mono(13)
                .foregroundStyle(DesignPalette.ink3)
        }
        .padding(.horizontal, 4)
        .padding(.top, 6)
        .padding(.bottom, 4)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var chartCard: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(latestValueText)
                    .display(size: 38, weight: .heavy)
                    .foregroundStyle(DesignPalette.ink)
                if !deltaText.isEmpty {
                    Text(deltaText)
                        .font(.jbm(11, weight: .heavy))
                        .foregroundStyle(DesignPalette.ok)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(
                            DesignPalette.ok.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
                }
                Spacer()
                if let last = series.last {
                    Text("\(last.bestReps) повт.")
                        .mono(13, weight: .regular)
                        .foregroundStyle(DesignPalette.ink3)
                }
            }
            Text("Лучший сет в диапазоне")
                .font(.jbm(13))
                .foregroundStyle(DesignPalette.ink3)
                .padding(.bottom, 4)

            if series.count >= 2 {
                Chart {
                    ForEach(series) { p in
                        AreaMark(
                            x: .value("Дата", DateTools.date(from: p.workoutDate)),
                            y: .value(metricLabel, valueFor(p))
                        )
                        .foregroundStyle(
                            LinearGradient(
                                colors: [
                                    DesignPalette.accent.opacity(0.35),
                                    DesignPalette.accent.opacity(0),
                                ],
                                startPoint: .top,
                                endPoint: .bottom
                            )
                        )

                        LineMark(
                            x: .value("Дата", DateTools.date(from: p.workoutDate)),
                            y: .value(metricLabel, valueFor(p))
                        )
                        .foregroundStyle(DesignPalette.accent)
                        .interpolationMethod(.monotone)

                        PointMark(
                            x: .value("Дата", DateTools.date(from: p.workoutDate)),
                            y: .value(metricLabel, valueFor(p))
                        )
                        .foregroundStyle(DesignPalette.accent)
                        .symbolSize(p.id == series.last?.id ? 60 : 14)
                    }
                }
                .frame(height: 160)
                .chartXAxis { AxisMarks(values: .automatic(desiredCount: 4)) }
            } else {
                Text("Недостаточно точек для графика")
                    .font(.jbm(12))
                    .foregroundStyle(DesignPalette.ink3)
                    .frame(maxWidth: .infinity)
                    .frame(height: 160)
            }
        }
        .padding(16)
        .liquidGlass(radius: 26)
    }

    private var statsGrid: some View {
        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 8) {
            statTile(label: "Рабочий", value: workingWeight, unit: "кг")
            statTile(label: "Топ повт.", value: topReps, unit: "")
            statTile(label: "Дельта", value: deltaWeightString, unit: "")
            statTile(label: "Сетов", value: "\(totalSets)", unit: "")
        }
    }

    private func statTile(label: String, value: String, unit: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label.uppercased())
                .font(.jbm(11, weight: .heavy))
                .tracking(0.4)
                .foregroundStyle(DesignPalette.ink3)
            HStack(alignment: .firstTextBaseline, spacing: 2) {
                Text(value)
                    .display(size: 22, weight: .heavy)
                    .foregroundStyle(DesignPalette.ink)
                if !unit.isEmpty {
                    Text(unit)
                        .font(.jbm(12, weight: .semibold))
                        .foregroundStyle(DesignPalette.ink3)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .glassCard(radius: 18)
    }

    private var recentSets: some View {
        VStack(spacing: 0) {
            let entries = recentEntries
            ForEach(Array(entries.enumerated()), id: \.offset) { idx, entry in
                HStack {
                    Text(entry.date)
                        .font(.jbm(13))
                        .foregroundStyle(DesignPalette.ink3)
                        .frame(width: 70, alignment: .leading)
                    Text(entry.label)
                        .mono(14, weight: .heavy)
                        .foregroundStyle(DesignPalette.ink)
                    Spacer()
                    if let effort = entry.effort {
                        EffortBubble(effort: effort, size: 22)
                    }
                }
                .padding(.vertical, 10)
                if idx < entries.count - 1 {
                    Rectangle().fill(DesignPalette.sep).frame(height: 0.5)
                }
            }

            if recentEntries.isEmpty {
                Text("Нет записей")
                    .font(.jbm(13))
                    .foregroundStyle(DesignPalette.ink3)
                    .padding(.vertical, 14)
            }
        }
        .padding(.horizontal, 16)
        .glassCard(radius: 20)
    }

    private var series: [ProgressPoint] {
        TrainerLogic.buildExerciseProgressSeries(
            workouts: store.workouts,
            range: store.selectedRange,
            exerciseID: exerciseID
        )
    }

    private var rangeSubtitle: String {
        if let last = series.last { return "последняя \(DateTools.short(last.workoutDate))" }
        return "—"
    }

    private var latestValueText: String {
        guard let last = series.last else { return "—" }
        switch metric {
        case .topSet:
            return "\(TrainerLogic.formatWeight(last.bestWeight)) кг × \(last.repsAtBestWeight)"
        case .weight:
            return "\(TrainerLogic.formatWeight(last.bestWeight)) кг"
        case .reps:
            return "\(last.bestReps)"
        case .workouts:
            return "\(series.count)"
        }
    }

    private var metricLabel: String { metric.rawValue }

    private func valueFor(_ p: ProgressPoint) -> Double {
        switch metric {
        case .topSet: return p.bestWeight * Double(p.repsAtBestWeight)
        case .weight: return p.bestWeight
        case .reps: return Double(p.bestReps)
        case .workouts: return 1
        }
    }

    private var deltaText: String {
        guard let summary = TrainerLogic.summarizeExerciseSeries(series),
            summary.firstPoint.bestWeight > 0
        else { return "" }
        let pct =
            (summary.latestPoint.bestWeight - summary.firstPoint.bestWeight)
            / summary.firstPoint.bestWeight * 100
        let sign = pct >= 0 ? "↑" : "↓"
        return "\(sign) \(abs(Int(pct.rounded())))% за \(store.selectedRange.label)"
    }

    private var workingWeight: String {
        guard let last = series.last else { return "—" }
        return TrainerLogic.formatWeight(last.bestWeight)
    }

    private var topReps: String {
        if let max = series.map(\.bestReps).max() { return "\(max)" }
        return "—"
    }

    private var deltaWeightString: String {
        guard let summary = TrainerLogic.summarizeExerciseSeries(series) else { return "—" }
        let delta = summary.latestPoint.bestWeight - summary.firstPoint.bestWeight
        let sign = delta >= 0 ? "+" : ""
        return "\(sign)\(TrainerLogic.formatWeight(delta))"
    }

    private var totalSets: Int {
        store.workouts.reduce(0) { p, w in
            p + (w.data.exercises.first { $0.exerciseID == exerciseID }?.sets.count ?? 0)
        }
    }

    private struct RecentEntry {
        var date: String
        var label: String
        var effort: SetEffort?
    }

    private var recentEntries: [RecentEntry] {
        var collected: [RecentEntry] = []
        for w in store.workouts {
            guard let ex = w.data.exercises.first(where: { $0.exerciseID == exerciseID }) else {
                continue
            }
            let top = ex.sets.max { left, right in
                left.weight < right.weight
                    || (left.weight == right.weight && left.reps < right.reps)
            }
            if let top {
                collected.append(
                    RecentEntry(
                        date: DateTools.short(w.workoutDate),
                        label: "\(TrainerLogic.formatWeight(top.weight)) кг × \(top.reps)",
                        effort: top.effort
                    ))
            }
            if collected.count >= 5 { break }
        }
        return collected
    }
}

// MARK: - Measurements tab

// «Замеры» — вес (кг) + талия (см), по макетам Claude Design
// (screens/measurements.jsx): сегмент с текущими значениями над hero-карточкой,
// график с reference-линией (цель веса / лимит талии), статы, последние записи
// с кнопкой удаления и пустое состояние талии.
private struct BodyWeightScreen: View {
    @EnvironmentObject private var store: TrainerStore
    @State private var metric: MeasureMetric = .weight
    @State private var pendingDeleteWeight: BodyWeightEntry?
    @State private var pendingDeleteWaist: WaistEntry?
    @State private var showComposer = false
    @State private var composerInitialDate = Date()
    @State private var composerInitialValue = ""
    // Preserve the internal user marker for diagnostics without reserving
    // product-screen space for it.
    private let showsDeveloperHeader = false

    // Both metrics render through one chart/list shape.
    private struct MeasurePoint: Identifiable {
        var id: Int
        var entryDate: String
        var value: Double
    }

    private var points: [MeasurePoint] {
        switch metric {
        case .weight:
            return store.bodyWeightEntries.map {
                MeasurePoint(id: $0.id, entryDate: $0.entryDate, value: $0.weight)
            }
        case .waist:
            return store.waistEntries.map {
                MeasurePoint(id: $0.id, entryDate: $0.entryDate, value: $0.waist)
            }
        }
    }

    private var unit: String { metric == .weight ? "кг" : "см" }

    // Reference line from the coach context: the phase weight goal on the
    // weight chart, the hard waist limit on the waist chart.
    private var referenceLine: (value: Double, label: String, tone: Color)? {
        let context = store.recommendation?.recommendation?.coachContext
        switch metric {
        case .weight:
            guard let target = context?.targetWeightKg else { return nil }
            return (target, "ЦЕЛЬ \(Self.format1dp(target))", DesignPalette.ok)
        case .waist:
            guard let limit = context?.waistLimitCm else { return nil }
            return (limit, "ЛИМИТ \(Self.format1dp(limit))", DesignPalette.bad)
        }
    }

    var body: some View {
        ZStack {
            WarmWallpaper()
            content
        }
        // The keyboard belongs to the measurement composer. Keep the chart-heavy
        // screen underneath at its existing size while the sheet gains focus;
        // otherwise the first keyboard presentation needlessly lays out the
        // entire measurements screen again.
        .ignoresSafeArea(.keyboard, edges: .bottom)
        .onAppear {
            metric = store.measurementsMetric
            // Fallback for the rare case where ContentView appeared before its
            // UIWindow became key and could not prewarm the input system yet.
            DecimalKeyboardPrewarmer.warmUp()
        }
        .onChange(of: store.measurementsMetric) { _, newValue in metric = newValue }
        .sheet(isPresented: $showComposer) {
            MeasureComposerSheet(
                metric: metric,
                initialDate: composerInitialDate,
                initialValue: composerInitialValue
            )
            .environmentObject(store)
            .presentationDetents([.medium])
            .presentationDragIndicator(.visible)
        }
        .alert("Удалить запись веса?", isPresented: deleteWeightBinding) {
            Button("Удалить", role: .destructive) {
                if let pendingDeleteWeight {
                    Task { await store.deleteBodyWeight(pendingDeleteWeight) }
                }
                pendingDeleteWeight = nil
            }
            Button("Отмена", role: .cancel) { pendingDeleteWeight = nil }
        } message: {
            if let pendingDeleteWeight {
                Text(
                    "\(TrainerLogic.formatBodyWeight(pendingDeleteWeight.weight)) кг от \(DateTools.long(pendingDeleteWeight.entryDate))"
                )
            }
        }
        .alert("Удалить замер талии?", isPresented: deleteWaistBinding) {
            Button("Удалить", role: .destructive) {
                if let pendingDeleteWaist {
                    Task { await store.deleteWaist(pendingDeleteWaist) }
                }
                pendingDeleteWaist = nil
            }
            Button("Отмена", role: .cancel) { pendingDeleteWaist = nil }
        } message: {
            if let pendingDeleteWaist {
                Text(
                    "\(Self.format1dp(pendingDeleteWaist.waist)) см от \(DateTools.long(pendingDeleteWaist.entryDate))"
                )
            }
        }
    }

    private var content: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if showsDeveloperHeader {
                    headerPills
                }
                TopTitle(sub: "Вес и талия", title: "Замеры")
                    .padding(.horizontal, 4)

                metricSegment

                if metric == .waist && store.waistEntries.isEmpty {
                    emptyWaistCard
                } else {
                    heroCard
                    statsRow

                    Text("ПОСЛЕДНИЕ ЗАПИСИ")
                        .font(.jbm(13, weight: .bold))
                        .tracking(0.4)
                        .foregroundStyle(DesignPalette.ink3)
                        .padding(.horizontal, 4)
                        .padding(.top, 6)

                    recentEntries
                }
            }
            .padding(.horizontal, 14)
            .padding(.top, 8)
            .padding(.bottom, 24)
        }
        .scrollIndicators(.hidden)
        .refreshable {
            await store.refreshServerData()
        }
    }

    private var headerPills: some View {
        HStack(spacing: 6) {
            HStack(spacing: 6) {
                Circle().fill(DesignPalette.ok).frame(width: 6, height: 6)
                Text("UID \(store.currentUser?.id ?? 0)")
            }
            .mono(13, weight: .semibold)
            .foregroundStyle(DesignPalette.ink2)
            .padding(.horizontal, 11)
            .padding(.vertical, 6)
            .chipBackground()

            Spacer()
        }
        .padding(.top, 4)  // Match Today screen's topPillsRow inset.
    }

    // MARK: metric segment («Вес 79.0 | Талия 84.0»)

    private var metricSegment: some View {
        HStack(spacing: 3) {
            segmentItem(.weight, label: "Вес", value: store.bodyWeightEntries.last?.weight)
            segmentItem(.waist, label: "Талия", value: store.waistEntries.last?.waist)
        }
        .padding(3)
        .background(DesignPalette.ink.opacity(0.05), in: Capsule())
        .overlay(Capsule().stroke(DesignPalette.ink.opacity(0.08), lineWidth: 0.5))
    }

    private func segmentItem(_ target: MeasureMetric, label: String, value: Double?) -> some View {
        let on = metric == target
        return Button {
            metric = target
            store.measurementsMetric = target
        } label: {
            HStack(spacing: 6) {
                Text(label)
                    .font(.jbm(13, weight: .bold)).tracking(-0.15)
                Text(value.map(Self.format1dp) ?? "—")
                    .mono(11, weight: .semibold)
                    .opacity(on ? 0.66 : 0.5)
            }
            .foregroundStyle(on ? .white : DesignPalette.ink2)
            .frame(maxWidth: .infinity)
            .frame(height: 38)
            .background(on ? DesignPalette.ink : .clear, in: Capsule())
        }
        .buttonStyle(.plain)
    }

    // MARK: hero card (значение + дельта за 90 дней + график)

    private var heroCard: some View {
        let delta = ninetyDayDelta
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: 10) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    if let last = points.last {
                        Text(Self.format1dp(last.value))
                            .display(size: 40, weight: .bold)
                            .foregroundStyle(DesignPalette.ink)
                        Text(unit)
                            .font(.jbm(15, weight: .semibold))
                            .foregroundStyle(DesignPalette.ink3)
                    } else {
                        Text("—")
                            .display(size: 40, weight: .bold)
                            .foregroundStyle(DesignPalette.ink)
                    }

                    if let delta {
                        VStack(alignment: .leading, spacing: 0) {
                            Text(deltaText(delta))
                                .font(.jbm(13, weight: .heavy))
                                .foregroundStyle(deltaTint(delta))
                            Text("за 90 дней")
                                .font(.jbm(10.5))
                                .foregroundStyle(DesignPalette.ink3)
                        }
                        .padding(.leading, 4)
                    }
                }

                Spacer()

                addButton
            }

            if points.count >= 2 {
                measureChart
                    .frame(height: 170)
                    .padding(.top, 8)
            } else {
                Text("Добавь несколько записей, чтобы увидеть динамику")
                    .font(.jbm(12))
                    .foregroundStyle(DesignPalette.ink3)
                    .frame(maxWidth: .infinity)
                    .frame(height: 170)
            }
        }
        .padding(18)
        .liquidGlass(radius: 28)
    }

    private var addButton: some View {
        Button {
            openComposer()
        } label: {
            ZStack {
                Circle().fill(DesignPalette.accent)
                Image(systemName: "plus")
                    .font(.jbm(18, weight: .heavy))
                    .foregroundStyle(.white)
            }
            .frame(width: 44, height: 44)
            .shadow(color: DesignPalette.accent.opacity(0.35), radius: 14, y: 6)
        }
        .buttonStyle(.pressable(scale: 0.86))
    }

    private var measureChart: some View {
        let domain = yDomain
        let yMin = domain.lowerBound
        return Chart {
            if let referenceLine {
                RuleMark(y: .value("Ориентир", referenceLine.value))
                    .foregroundStyle(referenceLine.tone)
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 4]))
                    .annotation(position: .top, alignment: .trailing) {
                        Text(referenceLine.label)
                            .font(.jbm(9, weight: .bold))
                            .foregroundStyle(referenceLine.tone)
                    }
            }
            ForEach(points) { point in
                AreaMark(
                    x: .value("Дата", DateTools.date(from: point.entryDate)),
                    yStart: .value("Низ", yMin),
                    yEnd: .value("Значение", point.value)
                )
                .foregroundStyle(
                    LinearGradient(
                        colors: [
                            DesignPalette.accent.opacity(0.32), DesignPalette.accent.opacity(0),
                        ],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )

                LineMark(
                    x: .value("Дата", DateTools.date(from: point.entryDate)),
                    y: .value("Значение", point.value)
                )
                .foregroundStyle(DesignPalette.accent)
                .interpolationMethod(.monotone)

                PointMark(
                    x: .value("Дата", DateTools.date(from: point.entryDate)),
                    y: .value("Значение", point.value)
                )
                .foregroundStyle(point.id == points.last?.id ? Color.white : DesignPalette.accent)
                .symbolSize(point.id == points.last?.id ? 80 : 14)
            }
        }
        .chartOverlay { proxy in
            GeometryReader { geo in
                Rectangle()
                    .fill(.clear)
                    .contentShape(Rectangle())
                    .onTapGesture { location in
                        let frame = geo[proxy.plotAreaFrame]
                        let x = location.x - frame.origin.x
                        guard let date: Date = proxy.value(atX: x) else { return }
                        requestDelete(nearest(to: date))
                    }
            }
        }
        .chartYScale(domain: domain)
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 4))
        }
    }

    // MARK: stats («Средний» + «Минимум» / «Лимит фазы»)

    private var statsRow: some View {
        let values = points.map(\.value)
        let average = values.isEmpty ? nil : values.reduce(0, +) / Double(values.count)
        return LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 8) {
            statTile(label: "Средний", value: average.map(Self.format1dp) ?? "—")
            if metric == .waist {
                statTile(
                    label: "Лимит фазы",
                    value: referenceLine.map { Self.format1dp($0.value) } ?? "—"
                )
            } else {
                statTile(label: "Минимум", value: values.min().map(Self.format1dp) ?? "—")
            }
        }
    }

    private func statTile(label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label.uppercased())
                .font(.jbm(10, weight: .heavy))
                .tracking(0.4)
                .foregroundStyle(DesignPalette.ink3)
            HStack(alignment: .firstTextBaseline, spacing: 2) {
                Text(value)
                    .display(size: 18, weight: .heavy)
                    .foregroundStyle(DesignPalette.ink)
                Text(unit)
                    .font(.jbm(11, weight: .semibold))
                    .foregroundStyle(DesignPalette.ink3)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .glassCard(radius: 18)
    }

    // MARK: recent entries («дата · значение · удалить»)

    private var recentEntries: some View {
        let visible = Array(points.reversed())
        return VStack(spacing: 0) {
            ForEach(Array(visible.enumerated()), id: \.element.id) { idx, point in
                HStack(spacing: 10) {
                    Text(DateTools.short(point.entryDate))
                        .font(.jbm(13))
                        .foregroundStyle(DesignPalette.ink3)
                        .frame(width: 70, alignment: .leading)
                    Text("\(Self.format1dp(point.value)) \(unit)")
                        .mono(14, weight: .heavy)
                        .foregroundStyle(DesignPalette.ink)
                    Spacer(minLength: 0)
                    Button {
                        requestDelete(point)
                    } label: {
                        Text("удалить")
                            .font(.jbm(12, weight: .semibold))
                            .foregroundStyle(DesignPalette.ink3)
                    }
                    .buttonStyle(.plain)
                }
                .padding(.vertical, 11)
                if idx < visible.count - 1 {
                    Rectangle().fill(DesignPalette.sep).frame(height: 0.5)
                }
            }
            if visible.isEmpty {
                Text("Нет записей")
                    .font(.jbm(13))
                    .foregroundStyle(DesignPalette.ink3)
                    .padding(.vertical, 14)
            }
        }
        .padding(.horizontal, 16)
        .glassCard(radius: 20)
    }

    // MARK: waist empty state («Первый замер станет базой фазы»)

    private var emptyWaistCard: some View {
        VStack(spacing: 0) {
            RoundedRectangle(cornerRadius: 15, style: .continuous)
                .fill(DesignPalette.ink.opacity(0.05))
                .frame(width: 46, height: 46)
                .overlay(SignalGlyph(systemName: "ruler", color: DesignPalette.ink3, size: 22))
                .padding(.bottom, 12)
            Text("Первый замер станет базой фазы")
                .font(.jbm(15, weight: .bold)).tracking(-0.2)
                .foregroundStyle(DesignPalette.ink)
                .multilineTextAlignment(.center)
            Text("Талия — второй контур набора: по ней тренер видит, куда идёт вес.")
                .font(.jbm(12))
                .foregroundStyle(DesignPalette.ink3)
                .multilineTextAlignment(.center)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 5)
            Text("утром натощак, по пупку")
                .font(.jbm(11.5))
                .foregroundStyle(DesignPalette.ink4)
                .padding(.top, 8)
            Button {
                openComposer()
            } label: {
                Text("Внести талию")
                    .font(.jbm(14, weight: .bold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(height: 48)
                    .background(DesignPalette.accent, in: Capsule())
                    .shadow(color: DesignPalette.accent.opacity(0.30), radius: 10, y: 4)
            }
            .buttonStyle(.pressable(scale: 0.96))
            .padding(.top, 16)
        }
        .frame(maxWidth: .infinity)
        .padding(EdgeInsets(top: 30, leading: 22, bottom: 22, trailing: 22))
        .liquidGlass(radius: 28)
    }

    // MARK: helpers

    private func openComposer() {
        let today = DateTools.localTodayISO()
        composerInitialDate = DateTools.date(from: today)
        composerInitialValue =
            metric == .weight
            ? store.bodyWeightComposerValue(for: today)
            : store.waistComposerValue(for: today)
        showComposer = true
    }

    private var ninetyDayDelta: Double? {
        let cal = Calendar.current
        guard let cutoff = cal.date(byAdding: .day, value: -90, to: Date()) else { return nil }
        let window = points.filter {
            DateTools.date(from: $0.entryDate) >= cal.startOfDay(for: cutoff)
        }
        guard let first = window.first, let last = window.last, window.count >= 2 else {
            return nil
        }
        return last.value - first.value
    }

    private func deltaText(_ value: Double) -> String {
        let arrow = value <= 0 ? "↓" : "↑"
        return "\(arrow) \(Self.format1dp(abs(value))) \(unit)"
    }

    // Going down is good in a cut for both metrics; going up is «bad» for
    // weight and «warn» for the waist (mockup tones).
    private func deltaTint(_ value: Double) -> Color {
        if value <= 0 { return DesignPalette.ok }
        return metric == .waist ? DesignPalette.warn : DesignPalette.bad
    }

    private func requestDelete(_ point: MeasurePoint?) {
        guard let point else { return }
        switch metric {
        case .weight:
            pendingDeleteWeight = store.bodyWeightEntries.first { $0.id == point.id }
        case .waist:
            pendingDeleteWaist = store.waistEntries.first { $0.id == point.id }
        }
    }

    private func nearest(to date: Date) -> MeasurePoint? {
        points.min { left, right in
            abs(DateTools.date(from: left.entryDate).timeIntervalSince(date))
                < abs(DateTools.date(from: right.entryDate).timeIntervalSince(date))
        }
    }

    private var deleteWeightBinding: Binding<Bool> {
        Binding(
            get: { pendingDeleteWeight != nil },
            set: { if !$0 { pendingDeleteWeight = nil } }
        )
    }

    private var deleteWaistBinding: Binding<Bool> {
        Binding(
            get: { pendingDeleteWaist != nil },
            set: { if !$0 { pendingDeleteWaist = nil } }
        )
    }

    private var yDomain: ClosedRange<Double> {
        var values = points.map(\.value)
        if let referenceLine {
            values.append(referenceLine.value)
        }
        guard let min = values.min(), let max = values.max(), min != max else {
            let v = values.first ?? 80
            return (v - 1)...(v + 1)
        }
        return (min - 0.5)...(max + 0.5)
    }

    static func format1dp(_ value: Double) -> String {
        String(format: "%.1f", locale: Locale(identifier: "ru_RU"), value)
    }
}

/// SwiftUI's `defaultFocus` is not reliable for a text field presented inside a
/// sheet: on some iOS versions it updates the focus graph without making the
/// underlying text input first responder. This UIKit field requests first
/// responder status as soon as it actually belongs to a window, so the sheet
/// and decimal keyboard start their animations together.
@MainActor
private enum DecimalKeyboardPrewarmer {
    private static var didWarmUp = false

    static func warmUp() {
        guard ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil,
            !didWarmUp,
            let window = UIApplication.shared.connectedScenes
                .compactMap({ $0 as? UIWindowScene })
                .flatMap(\.windows)
                .first(where: \.isKeyWindow)
        else { return }

        let field = UITextField(frame: CGRect(x: -2, y: -2, width: 1, height: 1))
        field.alpha = 0.01
        field.keyboardType = .decimalPad
        window.addSubview(field)

        // A synchronous become/resign cycle initializes KeyboardServices but
        // is completed before Core Animation commits a visible keyboard frame.
        didWarmUp = field.becomeFirstResponder()
        field.resignFirstResponder()
        field.removeFromSuperview()
    }
}

final class ImmediateDecimalTextField: UITextField {
    private var didRequestInitialFocus = false

    override func didMoveToWindow() {
        super.didMoveToWindow()
        guard window != nil, !didRequestInitialFocus else { return }
        didRequestInitialFocus = true

        // Waiting one main-loop turn lets the sheet finish attaching its view
        // hierarchy while remaining inside the same presentation transition.
        DispatchQueue.main.async { [weak self] in
            guard let self, self.window != nil else { return }
            self.becomeFirstResponder()
        }
    }
}

final class DecimalDraftBuffer {
    var value: String

    init(value: String) {
        self.value = value
    }
}

struct ImmediateDecimalInput: UIViewRepresentable {
    @Binding var text: String
    var buffer: DecimalDraftBuffer
    var accessibilityLabel: String

    func makeCoordinator() -> Coordinator {
        Coordinator(text: $text, buffer: buffer)
    }

    func makeUIView(context: Context) -> ImmediateDecimalTextField {
        let field = ImmediateDecimalTextField()
        field.keyboardType = .decimalPad
        field.placeholder = "0.0"
        field.font = UIFont(name: AppFont.bold, size: 26)
        field.textColor = UIColor(DesignPalette.ink)
        field.tintColor = UIColor(DesignPalette.accent)
        field.backgroundColor = .clear
        field.adjustsFontForContentSizeCategory = true
        field.setContentHuggingPriority(.defaultLow, for: .horizontal)
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        field.accessibilityLabel = accessibilityLabel
        field.addTarget(
            context.coordinator,
            action: #selector(Coordinator.valueChanged(_:)),
            for: .editingChanged
        )
        return field
    }

    func updateUIView(_ uiView: ImmediateDecimalTextField, context: Context) {
        context.coordinator.text = $text
        context.coordinator.buffer = buffer
        uiView.accessibilityLabel = accessibilityLabel
        // While editing, UIKit is the source of truth. The SwiftUI binding is
        // intentionally debounced so its validation pass cannot delay the key
        // that the user has just pressed.
        if !uiView.isFirstResponder, uiView.text != text {
            uiView.text = text
        }
    }

    static func dismantleUIView(_ uiView: ImmediateDecimalTextField, coordinator: Coordinator) {
        coordinator.cancelPendingUpdate()
        uiView.resignFirstResponder()
    }

    final class Coordinator: NSObject {
        var text: Binding<String>
        var buffer: DecimalDraftBuffer
        private var pendingTextUpdate: DispatchWorkItem?

        init(text: Binding<String>, buffer: DecimalDraftBuffer) {
            self.text = text
            self.buffer = buffer
        }

        @objc func valueChanged(_ sender: UITextField) {
            let value = sender.text ?? ""
            buffer.value = value
            pendingTextUpdate?.cancel()

            let update = DispatchWorkItem { [weak self] in
                self?.text.wrappedValue = value
            }
            pendingTextUpdate = update
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.12, execute: update)
        }

        func cancelPendingUpdate() {
            pendingTextUpdate?.cancel()
            pendingTextUpdate = nil
        }
    }
}

// One composer for both metrics: «НОВЫЙ ЗАМЕР · ВЕС/ТАЛИЯ», date, value with
// the unit, the waist hint, and an accent save button (mockup MeasureEntrySheet;
// the system decimal pad replaces the custom keypad deliberately).
private struct MeasureComposerSheet: View {
    var metric: MeasureMetric
    @EnvironmentObject private var store: TrainerStore
    @Environment(\.dismiss) private var dismiss
    @State private var draftDate: Date
    @State private var draftValue: String
    private let draftBuffer: DecimalDraftBuffer

    init(metric: MeasureMetric, initialDate: Date, initialValue: String) {
        self.metric = metric
        _draftDate = State(initialValue: initialDate)
        _draftValue = State(initialValue: initialValue)
        draftBuffer = DecimalDraftBuffer(value: initialValue)
    }

    private var isSaving: Bool {
        metric == .weight ? store.isSavingBodyWeight : store.isSavingWaist
    }

    private var normalizedDraftValue: String {
        TrainerLogic.normalizeBodyWeightInput(draftValue)
    }

    private var parsedDraftValue: Double? {
        Double(normalizedDraftValue)
    }

    private var isDraftValid: Bool {
        guard let value = parsedDraftValue else { return false }
        return metric == .weight
            ? TrainerStore.validBodyWeightRange.contains(value)
            : TrainerStore.validWaistRange.contains(value)
    }

    private var validationMessage: String? {
        guard !draftValue.isEmpty, !isDraftValid else { return nil }
        return metric == .weight
            ? "Вес должен быть от 30 до 400 кг"
            : "Талия должна быть от 50 до 160 см"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("НОВЫЙ ЗАМЕР · \(metric == .weight ? "ВЕС" : "ТАЛИЯ")")
                .font(.jbm(11, weight: .bold)).tracking(0.6)
                .foregroundStyle(DesignPalette.ink3)

            DatePicker(
                "Дата",
                selection: $draftDate,
                in: ...Date(),
                displayedComponents: .date
            )

            HStack {
                // Keep keystrokes local to the sheet. Publishing every digit
                // through the shared store rebuilt the chart-heavy screen
                // underneath and also disturbed the decimal-pad caret.
                ImmediateDecimalInput(
                    text: $draftValue,
                    buffer: draftBuffer,
                    accessibilityLabel: metric == .weight ? "Вес" : "Талия"
                )
                .padding(.horizontal, 14)
                .frame(maxWidth: .infinity)
                .frame(height: 56)
                .background(Color.white.opacity(0.6), in: RoundedRectangle(cornerRadius: 14))

                Text(metric == .weight ? "кг" : "см")
                    .font(.jbm(16, weight: .semibold))
                    .foregroundStyle(DesignPalette.ink3)
            }

            if let validationMessage {
                Text(validationMessage)
                    .font(.jbm(11.5, weight: .semibold))
                    .foregroundStyle(DesignPalette.bad)
            }

            if metric == .waist {
                Text("утром натощак, по пупку")
                    .font(.jbm(11.5))
                    .foregroundStyle(DesignPalette.ink3)
            }

            Button {
                Task {
                    let currentValue = TrainerLogic.normalizeBodyWeightInput(draftBuffer.value)
                    let saved: Bool
                    if metric == .weight {
                        store.setBodyWeightDate(draftDate)
                        store.setBodyWeightValue(currentValue)
                        saved = await store.saveBodyWeight()
                    } else {
                        store.setWaistDate(draftDate)
                        store.setWaistValue(currentValue)
                        saved = await store.saveWaist()
                    }
                    if saved {
                        dismiss()
                    }
                }
            } label: {
                HStack {
                    if isSaving {
                        ProgressView().tint(.white)
                    }
                    Text(isSaving ? "Сохраняем…" : "Сохранить")
                        .font(.jbm(17, weight: .heavy))
                        .foregroundStyle(.white)
                }
                .frame(maxWidth: .infinity)
                .frame(height: 54)
                .background(DesignPalette.accent, in: RoundedRectangle(cornerRadius: 27))
                .shadow(color: DesignPalette.accent.opacity(0.30), radius: 10, y: 4)
            }
            .buttonStyle(.plain)
            .disabled(isSaving || !isDraftValid)
            .opacity(isDraftValid ? 1 : 0.5)
        }
        .padding(22)
        .background(WarmWallpaper())
        .interactiveDismissDisabled(isSaving)
    }
}

// MARK: - Settings / Sign-in / Loading / Error / Toast / Empty

private struct SettingsSheet: View {
    @EnvironmentObject private var store: TrainerStore
    @Environment(\.dismiss) private var dismiss
    @State private var draftURL = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Backend") {
                    TextField("URL", text: $draftURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Text(
                        "Production: https://trainer.superbatonec.org. Локально: http://127.0.0.1:8080."
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }

                if let user = store.currentUser {
                    Section("Аккаунт") {
                        LabeledContent("Пользователь", value: user.displayName ?? "Trainer")
                        Button("Выйти", role: .destructive) {
                            Task {
                                await store.signOut()
                                dismiss()
                            }
                        }
                    }
                }

                Section {
                    Button("Сохранить и переподключиться") {
                        store.apiBaseURLString = draftURL
                        Task { await store.reconnect() }
                        dismiss()
                    }
                    Button("Обновить данные") {
                        Task { await store.refreshServerData() }
                        dismiss()
                    }
                }
            }
            .navigationTitle("Настройки")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Готово") { dismiss() }
                }
            }
            .onAppear { draftURL = store.apiBaseURLString }
        }
    }
}

private struct SignInScreen: View {
    @EnvironmentObject private var store: TrainerStore
    @State private var showSettings = false
    var message: String?

    var body: some View {
        VStack(spacing: 18) {
            Spacer()
            ZStack {
                Circle().fill(DesignPalette.accent.opacity(0.12)).frame(width: 110, height: 110)
                GlyphIcon(glyph: .delts, size: 56, lineWidth: 2.4, tint: DesignPalette.accent)
            }
            VStack(spacing: 6) {
                Text("Trainer").display(size: 36, weight: .heavy)
                Text("Подключаемся к серверу")
                    .font(.jbm(14))
                    .foregroundStyle(DesignPalette.ink3)
            }
            if let message, !message.isEmpty {
                Text(message)
                    .font(.jbm(12))
                    .foregroundStyle(DesignPalette.ink3)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 32)
            }
            Button {
                Task { await store.reconnect() }
            } label: {
                Text("Повторить")
                    .font(.jbm(16, weight: .heavy))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 32)
                    .frame(height: 52)
                    .background(DesignPalette.accent, in: Capsule())
                    .shadow(color: DesignPalette.accent.opacity(0.4), radius: 16, y: 6)
            }
            .buttonStyle(.pressable(scale: 0.96))

            Button {
                showSettings = true
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "gear")
                    Text("Backend")
                }
                .font(.jbm(14, weight: .semibold))
                .foregroundStyle(DesignPalette.ink2)
                .padding(.horizontal, 18)
                .frame(height: 42)
                .chipBackground()
            }
            .buttonStyle(.plain)
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .sheet(isPresented: $showSettings) {
            SettingsSheet().environmentObject(store)
        }
    }
}

private struct LoadingScreen: View {
    var body: some View {
        VStack(spacing: 14) {
            GlyphIcon(glyph: .delts, size: 48, lineWidth: 2.2, tint: DesignPalette.accent)
            Text("Trainer").display(size: 32, weight: .heavy)
            ProgressView().tint(DesignPalette.accent)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct ErrorScreen: View {
    @EnvironmentObject private var store: TrainerStore
    var message: String

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "wifi.exclamationmark")
                .font(.jbm(40, weight: .heavy))
                .foregroundStyle(DesignPalette.warn)
            Text("Не удалось загрузить Trainer").display(size: 22, weight: .heavy)
                .multilineTextAlignment(.center)
            Text(message)
                .font(.jbm(13))
                .foregroundStyle(DesignPalette.ink3)
                .multilineTextAlignment(.center)
                .padding(.horizontal)
            Button {
                Task { await store.reconnect() }
            } label: {
                Text("Повторить")
                    .font(.jbm(16, weight: .heavy))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 28)
                    .frame(height: 50)
                    .background(DesignPalette.accent, in: Capsule())
            }
            .buttonStyle(.pressable(scale: 0.96))
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
    }
}

private struct ToastView: View {
    var message: String

    var body: some View {
        Text(message)
            .font(.jbm(14, weight: .heavy))
            .foregroundStyle(.white)
            .lineLimit(2)
            .multilineTextAlignment(.center)
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(DesignPalette.ink.opacity(0.92), in: Capsule())
            .padding(.horizontal, 20)
    }
}

private struct EmptyStateCard: View {
    var glyph: ExerciseGlyph
    var title: String
    var subtitle: String

    var body: some View {
        VStack(spacing: 8) {
            Text(title)
                .font(.jbm(16, weight: .heavy))
                .tracking(-0.3)
                .foregroundStyle(DesignPalette.ink)
            Text(subtitle)
                .mono(13)
                .foregroundStyle(DesignPalette.ink3)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(24)
        .glassCard(radius: 22)
    }
}

// MARK: - Helpers

extension Collection {
    fileprivate subscript(safe index: Index) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
