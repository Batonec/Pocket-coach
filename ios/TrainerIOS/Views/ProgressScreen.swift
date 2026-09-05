import SwiftUI

// MARK: - Progress

struct ProgressTabScreen: View {
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
struct WeeklyReportSheet: View {
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
