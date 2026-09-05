import Charts
import SwiftUI
import UIKit

// MARK: - Measurements tab

// «Замеры» — вес (кг) + талия (см), по макетам Claude Design
// (screens/measurements.jsx): сегмент с текущими значениями над hero-карточкой,
// график с reference-линией (цель веса / лимит талии), статы, последние записи
// с кнопкой удаления и пустое состояние талии.
struct BodyWeightScreen: View {
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
enum DecimalKeyboardPrewarmer {
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
