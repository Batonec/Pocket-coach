import SwiftUI

// MARK: - History tab

struct HistoryScreen: View {
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

func historyLoadChip(_ type: String) -> (label: String, color: Color) {
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
