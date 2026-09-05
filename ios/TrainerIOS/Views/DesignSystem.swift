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
