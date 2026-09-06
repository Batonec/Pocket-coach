import SwiftUI
import UIKit

// MARK: - Today screen

struct TodayScreen: View {
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
