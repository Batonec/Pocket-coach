import Charts
import SwiftUI

// MARK: - Exercise detail

struct ExerciseDetailScreen: View {
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
