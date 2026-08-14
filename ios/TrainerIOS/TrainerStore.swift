import Foundation
import SwiftUI

@MainActor
final class TrainerStore: ObservableObject {
    @Published var bootState: BootState = .idle
    @Published var currentTab: TrainerTab {
        didSet { defaults.set(currentTab.rawValue, forKey: Keys.currentTab) }
    }
    @Published var selectedRange: RangeOption {
        didSet { defaults.set(selectedRange.rawValue, forKey: Keys.progressRange) }
    }
    @Published var selectedBodyWeightRange: RangeOption {
        didSet { defaults.set(selectedBodyWeightRange.rawValue, forKey: Keys.bodyWeightRange) }
    }
    @Published var selectedProgressExerciseID: Int? {
        didSet {
            if let selectedProgressExerciseID {
                defaults.set(selectedProgressExerciseID, forKey: Keys.progressExercise)
            } else {
                defaults.removeObject(forKey: Keys.progressExercise)
            }
        }
    }
    @Published var apiBaseURLString: String {
        didSet { defaults.set(apiBaseURLString, forKey: Keys.apiBaseURL) }
    }

    @Published var currentUser: TrainerUser?
    @Published var exercises: [ExerciseDefinition] = []
    @Published var workouts: [Workout] = []
    @Published var bodyWeightEntries: [BodyWeightEntry] = []
    @Published var waistEntries: [WaistEntry] = []
    // Coach signals for the История banner: server-computed, snooze-filtered,
    // sorted. The client renders the first 1–2 and refetches on appear,
    // foreground and after relevant mutations.
    @Published var coachSignals: [CoachSignal] = []
    // Which segment the Замеры screen should open with (deep-link from banners).
    @Published var measurementsMetric: MeasureMetric = .weight
    @Published var draft: DraftWorkout {
        didSet { persistDraft() }
    }

    @Published var isWorkoutBuilderPresented = false
    @Published var isSavingWorkout = false
    @Published var isSavingBodyWeight = false
    @Published var bodyWeightDate: String
    @Published var bodyWeightValue: String = ""
    @Published var waistDate: String
    @Published var waistValue: String = ""
    @Published var isSavingWaist = false
    @Published var toast: String?

    @Published var recommendation: RecommendationResponse?
    @Published var isRefreshingRecommendation = false
    @Published var appliedPlan: AppliedCoachPlan? {
        didSet { persistAppliedPlan() }
    }

    private let defaults: UserDefaults
    private var toastTask: Task<Void, Never>?
    private var recommendationPollTask: Task<Void, Never>?
    private var recommendationPollGeneration = 0

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        self.currentTab = TrainerTab(rawValue: defaults.string(forKey: Keys.currentTab) ?? "") ?? .trainings
        // The Progress screen dropped its 7D/30D/All picker — everything there
        // (discipline, sparklines, exercise detail) now reads all-time.
        self.selectedRange = .all
        self.selectedBodyWeightRange = RangeOption(rawValue: defaults.string(forKey: Keys.bodyWeightRange) ?? "") ?? .days30
        self.selectedProgressExerciseID = defaults.object(forKey: Keys.progressExercise) as? Int
        self.apiBaseURLString = Self.normalizedBackendURL(defaults.string(forKey: Keys.apiBaseURL))
        self.draft = Self.readDraft(defaults: defaults)
        self.appliedPlan = Self.readAppliedPlan(defaults: defaults)
        self.bodyWeightDate = DateTools.localTodayISO()
        self.waistDate = DateTools.localTodayISO()
    }

    func boot() async {
        guard bootState == .idle else { return }
        bootState = .loading
        await reload(showSuccess: false)
    }

    func reload(showSuccess: Bool = true) async {
        // Cap the whole boot at 3 seconds. The splash/loading screen sits on
        // top of this; URLSession's default request timeout is 60s which feels
        // like a hang on flaky networks. After 3s we cancel the in-flight
        // work, drop into the error screen, and let the user retry.
        let baseURL = apiBaseURLString
        let workTask = Task<Void, Error> { [weak self] in
            guard let self else { return }
            let client = APIClient(baseURLString: baseURL)
            let session = try await client.resolveSession()
            try await self.loadAuthenticatedData(
                client: client,
                session: session,
                showSuccess: showSuccess
            )
        }
        let deadlineTask = Task {
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            workTask.cancel()
        }

        do {
            try await workTask.value
            deadlineTask.cancel()
        } catch {
            deadlineTask.cancel()
            // A cancellation that beat the work means we ran out of time.
            let isTimeout = workTask.isCancelled
                || error is CancellationError
                || (error as? URLError)?.code == .cancelled
            if isTimeout {
                handleLoadError(TrainerAPIError.timeout)
            } else {
                handleLoadError(error)
            }
        }
    }

    func reconnect() async {
        bootState = .loading
        await reload(showSuccess: true)
    }

    func signOut() async {
        recommendationPollTask?.cancel()
        recommendationPollTask = nil
        recommendationPollGeneration += 1
        isRefreshingRecommendation = false
        do {
            _ = try await APIClient(baseURLString: apiBaseURLString).logout()
        } catch {
            showToast(error.localizedDescription)
        }

        currentUser = nil
        workouts = []
        bodyWeightEntries = []
        waistEntries = []
        coachSignals = []
        bootState = .needsSignIn(nil)
    }

    func showToast(_ message: String) {
        toast = message
        toastTask?.cancel()
        toastTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_200_000_000)
            await MainActor.run {
                if self?.toast == message {
                    self?.toast = nil
                }
            }
        }
    }

    func openNewWorkout() {
        if !draft.hasAnyExercise && draft.editingWorkoutID == nil {
            draft.workoutDate = DateTools.localTodayISO()
        }
        isWorkoutBuilderPresented = true
    }

    func closeBuilder() {
        isWorkoutBuilderPresented = false
    }

    func resetDraft() {
        draft = .empty
    }

    func setDraftDate(_ date: Date) {
        draft.workoutDate = DateTools.iso(from: date)
    }

    func setBodyWeightDate(_ date: Date) {
        bodyWeightDate = DateTools.iso(from: date)
        syncBodyWeightComposer()
    }

    func setBodyWeightValue(_ value: String) {
        bodyWeightValue = TrainerLogic.normalizeBodyWeightInput(value)
    }

    func syncBodyWeightComposer(preserveValue: Bool = false) {
        guard !preserveValue else { return }
        if let existing = bodyWeightEntries.first(where: { $0.entryDate == bodyWeightDate }) {
            bodyWeightValue = TrainerLogic.formatBodyWeightInput(existing.weight)
            return
        }

        if let latest = bodyWeightEntries.last {
            bodyWeightValue = TrainerLogic.formatBodyWeightInput(latest.weight)
        } else {
            bodyWeightValue = ""
        }
    }

    func saveBodyWeight() async {
        let normalized = bodyWeightValue.replacingOccurrences(of: ",", with: ".")
        guard let value = Double(normalized), value > 0 else {
            showToast("Введи корректный вес тела")
            return
        }

        isSavingBodyWeight = true
        defer { isSavingBodyWeight = false }

        do {
            let response = try await APIClient(baseURLString: apiBaseURLString)
                .saveBodyWeight(entryDate: bodyWeightDate, weight: value)
            currentUser = response.user ?? currentUser
            bodyWeightEntries.removeAll { $0.entryDate == response.entry.entryDate }
            bodyWeightEntries.append(response.entry)
            bodyWeightEntries = TrainerLogic.sortBodyWeights(bodyWeightEntries)
            syncBodyWeightComposer()
            showToast(response.created == true ? "Вес тела сохранён" : "Вес тела обновлён")
            hideCoachSignals(withIDs: ["measurements_due", "measurements_overdue"])
            refreshCoachSignals()
            refreshRecommendationAfterMeasurement()
        } catch {
            showToast(error.localizedDescription)
        }
    }

    func deleteBodyWeight(_ entry: BodyWeightEntry) async {
        do {
            let response = try await APIClient(baseURLString: apiBaseURLString)
                .deleteBodyWeight(id: entry.id)
            currentUser = response.user ?? currentUser
            bodyWeightEntries.removeAll { $0.id == entry.id }
            syncBodyWeightComposer()
            showToast("Запись веса удалена")
            refreshCoachSignals()
            refreshRecommendationAfterMeasurement()
        } catch {
            showToast(error.localizedDescription)
        }
    }

    // MARK: - Waist measurements

    func setWaistDate(_ date: Date) {
        waistDate = DateTools.iso(from: date)
        syncWaistComposer()
    }

    func setWaistValue(_ value: String) {
        waistValue = TrainerLogic.normalizeBodyWeightInput(value)
    }

    func syncWaistComposer() {
        if let existing = waistEntries.first(where: { $0.entryDate == waistDate }) {
            waistValue = TrainerLogic.formatBodyWeightInput(existing.waist)
            return
        }
        if let latest = waistEntries.last {
            waistValue = TrainerLogic.formatBodyWeightInput(latest.waist)
        } else {
            waistValue = ""
        }
    }

    func saveWaist() async {
        let normalized = waistValue.replacingOccurrences(of: ",", with: ".")
        guard let value = Double(normalized), value > 0 else {
            showToast("Введи корректный обхват талии")
            return
        }

        isSavingWaist = true
        defer { isSavingWaist = false }

        do {
            let response = try await APIClient(baseURLString: apiBaseURLString)
                .saveWaist(entryDate: waistDate, waist: value)
            currentUser = response.user ?? currentUser
            waistEntries.removeAll { $0.entryDate == response.entry.entryDate }
            waistEntries.append(response.entry)
            waistEntries.sort { $0.entryDate < $1.entryDate }
            syncWaistComposer()
            showToast(response.created == true ? "Талия сохранена" : "Талия обновлена")
            hideCoachSignals(withIDs: [
                "measurements_due", "measurements_overdue", "waist_limit",
            ])
            refreshCoachSignals()
            refreshRecommendationAfterMeasurement()
        } catch {
            showToast(error.localizedDescription)
        }
    }

    func deleteWaist(_ entry: WaistEntry) async {
        do {
            let response = try await APIClient(baseURLString: apiBaseURLString)
                .deleteWaist(id: entry.id)
            currentUser = response.user ?? currentUser
            waistEntries.removeAll { $0.id == entry.id }
            syncWaistComposer()
            showToast("Замер талии удалён")
            refreshCoachSignals()
            refreshRecommendationAfterMeasurement()
        } catch {
            showToast(error.localizedDescription)
        }
    }

    // MARK: - Coach signals (the История banner)

    /// Silent refetch: the endpoint is cheap local SQLite on the server, so we
    /// call it on appear/foreground and right after relevant mutations.
    func loadCoachSignals() async {
        guard case .loaded = bootState else { return }
        if let response = try? await APIClient(baseURLString: apiBaseURLString).fetchCoachSignals(),
           let signals = response.signals {
            coachSignals = signals
        }
    }

    func refreshCoachSignals() {
        Task { [weak self] in await self?.loadCoachSignals() }
    }

    /// Optimistic hide by signal TYPE right after the user's own action —
    /// «взвесился → баннер погас в тот же кадр». The refetch that follows is
    /// the source of truth.
    func hideCoachSignals(withIDs ids: [String]) {
        coachSignals.removeAll { ids.contains($0.signalID) }
    }

    func dismissCoachSignal(_ signal: CoachSignal) {
        coachSignals.removeAll { $0.instanceKey == signal.instanceKey }
        Task { [weak self] in
            guard let self else { return }
            _ = try? await APIClient(baseURLString: self.apiBaseURLString)
                .dismissCoachSignal(instanceKey: signal.instanceKey)
            await self.loadCoachSignals()
        }
    }

    /// Server-side read receipt: prочтение — факт, не откладывание.
    func markWeeklyReportRead() {
        hideCoachSignals(withIDs: ["weekly_report_ready"])
        Task { [weak self] in
            guard let self else { return }
            _ = try? await APIClient(baseURLString: self.apiBaseURLString).markWeeklyReportRead()
            await self.loadCoachSignals()
        }
    }

    func startEditing(_ workout: Workout) {
        draft = DraftWorkout(
            workoutDate: workout.workoutDate,
            exercises: workout.data.exercises.map { exercise in
                DraftExercise(
                    exerciseID: exercise.exerciseID,
                    exerciseName: exercise.name,
                    sets: exercise.sets.map {
                        DraftSet(reps: $0.reps, weight: $0.weight, effort: $0.effort, notes: $0.notes)
                    }
                )
            },
            editingWorkoutID: workout.id,
            editingClientID: workout.clientID ?? workout.id.map { "workout-\($0)" }
        )
        isWorkoutBuilderPresented = true
        showToast("Тренировка открыта для редактирования")
    }

    func deleteWorkout(_ workout: Workout) async {
        guard let id = workout.id else { return }
        do {
            let response = try await APIClient(baseURLString: apiBaseURLString).deleteWorkout(id: id)
            currentUser = response.user ?? currentUser
            workouts.removeAll { $0.id == id }
            if draft.editingWorkoutID == id {
                resetDraft()
            }
            ensureSelectedProgressExercise()
            showToast("Тренировка удалена")
        } catch {
            showToast(error.localizedDescription)
        }
    }

    func saveDraftWorkout() async {
        // The applied plan is stamped onto NEW workouts only: editing an old
        // workout must neither claim today's plan nor consume it.
        let wasNewWorkout = draft.editingWorkoutID == nil
        let snapshot = wasNewWorkout ? appliedPlan?.snapshot : nil
        let payload = TrainerLogic.workoutPayload(from: draft, recommendation: snapshot)
        guard !payload.data.exercises.isEmpty else {
            showToast("Добавь хотя бы одно упражнение")
            return
        }

        isSavingWorkout = true
        defer { isSavingWorkout = false }

        do {
            let client = APIClient(baseURLString: apiBaseURLString)
            let response: WorkoutMutationResponse
            if let editingID = draft.editingWorkoutID {
                response = try await client.updateWorkout(id: editingID, workout: payload)
                showToast("Изменения в тренировке сохранены")
            } else {
                response = try await client.saveWorkout(payload)
                showToast(currentUser?.isDefaultDebugUser == true
                          ? "Тренировка сохранена для debug-user"
                          : "Тренировка сохранена")
            }

            currentUser = response.user ?? currentUser
            workouts.removeAll { $0.id == response.workout.id }
            workouts.append(response.workout)
            workouts = TrainerLogic.sortWorkouts(workouts)
            resetDraft()
            isWorkoutBuilderPresented = false
            ensureSelectedProgressExercise()
            hideCoachSignals(withIDs: ["return_soon", "return_mode", "deload_week"])
            refreshCoachSignals()
            if wasNewWorkout {
                // Plan consumed; backend regenerates the recommendation in the
                // background — pick up "pending" now and the fresh one later.
                appliedPlan = nil
                Task { [weak self] in await self?.loadRecommendation() }
                Task { [weak self] in
                    try? await Task.sleep(nanoseconds: 25_000_000_000)
                    await self?.loadRecommendation()
                }
            }
        } catch {
            showToast(error.localizedDescription)
        }
    }

    /// The cached coach weekly report (token-free endpoint: the Sunday timer
    /// generates it server-side). Nil when nothing is cached yet.
    func fetchWeeklyReport() async -> WeeklyReportEntry? {
        try? await requestWeeklyReport()
    }

    /// Same cached read with an observable failure for interactive UI. The
    /// progress screen uses this so it can stop its spinner and avoid opening
    /// a misleading empty sheet when the network request itself failed.
    func requestWeeklyReport() async throws -> WeeklyReportEntry? {
        return try await APIClient(baseURLString: apiBaseURLString).fetchWeeklyReport().report
    }

    // MARK: - Coach recommendation

    /// Instant cached read. Runs after boot reaches `.loaded`, outside the 3s
    /// reload deadline. Failures are silent — we keep whatever we already had.
    func loadRecommendation() async {
        do {
            recommendation = try await APIClient(baseURLString: apiBaseURLString).fetchRecommendation()
            autoApplyRecommendationIfReady()
        } catch {
            // ignore — the card just keeps its previous content (or stays hidden)
        }
    }

    /// A weight/waist mutation starts server-side generation in the background.
    /// Keep the existing payload visible while polling the cheap cached endpoint,
    /// then apply the refreshed targets only if today's workout has not started.
    private func refreshRecommendationAfterMeasurement() {
        guard !workouts.isEmpty else { return }

        recommendationPollGeneration += 1
        let generation = recommendationPollGeneration
        recommendationPollTask?.cancel()
        isRefreshingRecommendation = true

        let current = recommendation
        recommendation = RecommendationResponse(
            ok: current?.ok ?? true,
            status: "pending",
            stale: true,
            basedOnWorkoutID: current?.basedOnWorkoutID,
            basedOnWorkoutCount: current?.basedOnWorkoutCount,
            model: current?.model,
            updatedAt: current?.updatedAt,
            error: nil,
            recommendation: current?.recommendation
        )

        recommendationPollTask = Task { [weak self] in
            guard let self else { return }
            var lastError: Error?

            // Give the mutation handler time to expose server-side pending,
            // then poll the cached row; no LLM call is made by these GETs.
            for attempt in 0..<25 {
                do {
                    try await Task.sleep(
                        nanoseconds: attempt == 0 ? 600_000_000 : 2_000_000_000
                    )
                } catch {
                    return
                }
                guard !Task.isCancelled,
                      self.recommendationPollGeneration == generation else { return }

                do {
                    let response = try await APIClient(
                        baseURLString: self.apiBaseURLString
                    ).fetchRecommendation()
                    guard self.recommendationPollGeneration == generation else { return }
                    self.recommendation = response

                    if response.status == "ready" {
                        self.autoApplyRecommendationIfReady()
                        self.isRefreshingRecommendation = false
                        self.recommendationPollTask = nil
                        self.showToast("План обновлён по свежим замерам")
                        return
                    }
                    if response.status == "failed" {
                        self.isRefreshingRecommendation = false
                        self.recommendationPollTask = nil
                        self.showToast(response.error ?? "Не удалось обновить план")
                        return
                    }
                } catch {
                    lastError = error
                }
            }

            guard self.recommendationPollGeneration == generation else { return }
            self.isRefreshingRecommendation = false
            self.recommendationPollTask = nil
            self.showToast(
                lastError == nil
                    ? "Замер сохранён, план ещё обновляется"
                    : "Замер сохранён, план обновится после восстановления связи"
            )
        }
    }

    /// Force a new recommendation. Synchronous on the server (10–40s), so it runs
    /// on the long-running session and shows the pending overlay meanwhile.
    func refreshRecommendation() async {
        guard !isRefreshingRecommendation else { return }
        isRefreshingRecommendation = true
        defer { isRefreshingRecommendation = false }
        do {
            let response = try await APIClient(
                baseURLString: apiBaseURLString,
                session: APIClient.longRunningSession
            ).refreshRecommendation()
            recommendation = response
            if response.status == "ready" {
                autoApplyRecommendationIfReady()
                showToast("Совет обновлён")
            }
        } catch {
            recommendation = RecommendationResponse(
                ok: false,
                status: "failed",
                stale: false,
                basedOnWorkoutCount: recommendation?.basedOnWorkoutCount,
                model: recommendation?.model,
                error: error.localizedDescription,
                recommendation: recommendation?.recommendation
            )
            showToast(error.localizedDescription)
        }
    }

    /// Auto-apply the latest ready recommendation as today's plan — there is no
    /// "Применить" button; every generated workout becomes the plan on its own.
    /// Skips when editing a past workout (it must not claim today's plan) or when
    /// this recommendation is already the applied plan. Silent (no toast): it runs
    /// on every cached load and boot.
    func autoApplyRecommendationIfReady() {
        guard recommendation?.status == "ready",
              recommendation?.recommendation != nil,
              draft.editingWorkoutID == nil,
              !draft.hasRealSets,
              !isRecommendationApplied else { return }
        applyRecommendationAsPlan()
    }

    /// Apply the coach recommendation as today's PLAN: per-exercise targets that
    /// drive the preview cards, the quick "+" and progress. Deliberately does NOT
    /// create real draft sets — the workout starts only when the user logs a set.
    /// The plan is captured at apply time because the backend's recommendations
    /// row is mutable and may be regenerated before the workout is saved.
    func applyRecommendationAsPlan() {
        guard let response = recommendation,
              let payload = response.recommendation,
              !payload.exercises.isEmpty else { return }

        let planExercises = sanitizedPlanExercises(payload)
        guard !planExercises.isEmpty else { return }

        appliedPlan = AppliedCoachPlan(
            basedOnWorkoutID: response.basedOnWorkoutID,
            basedOnWorkoutCount: response.basedOnWorkoutCount,
            model: response.model,
            generatedAt: response.updatedAt,
            appliedAt: DateTools.localTodayISO(),
            focus: payload.focus,
            loadType: payload.loadType,
            exercises: planExercises
        )
    }

    /// Catalog filter (when the catalog is loaded), de-dupe by exercise id,
    /// drop zero-set entries, clamp reps/weight to sane bounds.
    private func sanitizedPlanExercises(_ payload: RecommendationPayload) -> [RecommendedExercise] {
        let knownIDs = Set(exercises.map(\.id))
        var seen = Set<Int>()
        return payload.exercises.compactMap { exercise in
            guard !exercise.sets.isEmpty,
                  seen.insert(exercise.exerciseID).inserted,
                  exercises.isEmpty || knownIDs.contains(exercise.exerciseID) else {
                return nil
            }
            return RecommendedExercise(
                exerciseID: exercise.exerciseID,
                name: exercise.name,
                note: exercise.note,
                sets: exercise.sets.map {
                    RecommendedSet(reps: max(1, $0.reps), weight: max(0, $0.weight))
                }
            )
        }
    }

    /// The coach's one-line note for an exercise in the applied plan (why this
    /// weight/reps) — shown on each plan card now that the expanded card is gone.
    func coachNote(for exerciseID: Int) -> String? {
        guard let note = appliedPlan?.exercises.first(where: { $0.exerciseID == exerciseID })?.note,
              !note.isEmpty else { return nil }
        return note
    }

    /// Remove a single (not yet started) exercise from the applied plan.
    /// Dropping the last exercise drops the whole plan.
    func removeFromPlan(exerciseID: Int) {
        guard var plan = appliedPlan else { return }
        plan.exercises.removeAll { $0.exerciseID == exerciseID }
        appliedPlan = plan.exercises.isEmpty ? nil : plan
    }

    /// True when the currently shown recommendation is the one already applied
    /// as the plan (drives the CoachCard button state). Compared by CONTENT:
    /// the backend keeps one recommendations row per user, so its id does not
    /// change across regenerations and can't tell old from new.
    var isRecommendationApplied: Bool {
        guard let appliedPlan, let payload = recommendation?.recommendation else { return false }
        return appliedPlan.focus == payload.focus
            && appliedPlan.exercises == sanitizedPlanExercises(payload)
    }

    /// A not-yet-started workout must not begin from targets that are being
    /// regenerated or whose replacement failed validation. Progress/history
    /// may keep rendering the previous payload, but Today hides its actionable
    /// plan until a fresh recommendation is ready.
    var isTodayPlanUnavailable: Bool {
        guard draft.editingWorkoutID == nil, !draft.hasRealSets else { return false }
        let status = recommendation?.status
        return isRefreshingRecommendation || status == "pending" || status == "failed"
    }

    func addPlannedSet(exerciseID: Int) {
        guard let exercise = exerciseDefinition(id: exerciseID) else { return }
        addSet(nextPlannedSet(exerciseID: exerciseID), to: exercise)
    }

    /// What the quick "+" / editor prefill proposes next for this exercise:
    /// custom-set continuation > applied-plan target > history+1 > fallback.
    func nextPlannedSet(exerciseID: Int) -> DraftSet {
        TrainerLogic.nextPlannedSet(
            workouts: workouts,
            exerciseID: exerciseID,
            draftSets: draft.exercises.first(where: { $0.exerciseID == exerciseID })?.sets ?? [],
            planTargets: activePlanTargets(for: exerciseID),
            excludeWorkoutID: draft.editingWorkoutID
        )
    }

    /// Plan targets apply only to today's draft — not when editing an old workout.
    private func activePlanTargets(for exerciseID: Int) -> [RecommendedSet]? {
        guard draft.editingWorkoutID == nil else { return nil }
        return appliedPlan?.targets(for: exerciseID)
    }

    func applySet(_ set: DraftSet, exerciseID: Int, setIndex: Int?) {
        guard let exercise = exerciseDefinition(id: exerciseID) else { return }
        if let setIndex {
            updateSet(set, exerciseID: exerciseID, setIndex: setIndex)
        } else {
            addSet(set, to: exercise)
        }
    }

    func removeLastSet(exerciseID: Int) {
        guard let index = draft.exercises.firstIndex(where: { $0.exerciseID == exerciseID }),
              !draft.exercises[index].sets.isEmpty else {
            return
        }

        draft.exercises[index].sets.removeLast()
        if draft.exercises[index].sets.isEmpty {
            draft.exercises.remove(at: index)
        }
    }

    func removeExercise(exerciseID: Int) {
        draft.exercises.removeAll { $0.exerciseID == exerciseID }
    }

    func displayCards() -> [DraftDisplayExercise] {
        if let appliedPlan, draft.editingWorkoutID == nil {
            return TrainerLogic.planDisplayCards(plan: appliedPlan, draftExercises: draft.exercises)
        }
        return TrainerLogic.draftDisplayCards(
            exercises: exercises,
            workouts: workouts,
            draftExercises: draft.exercises
        )
    }

    func rareExercises() -> [ExerciseDefinition] {
        TrainerLogic.availableRareExercises(
            exercises: exercises,
            workouts: workouts,
            draftExercises: draft.exercises
        )
    }

    func exerciseGroups() -> ExercisePickerGroups {
        TrainerLogic.exercisePickerGroups(
            available: exercises,
            catalog: exercises,
            workouts: workouts,
            draftExercises: draft.exercises
        )
    }

    func draftProgressRatio() -> Double {
        if let appliedPlan, draft.editingWorkoutID == nil {
            return TrainerLogic.planProgressRatio(plan: appliedPlan, draftExercises: draft.exercises)
        }
        return TrainerLogic.draftProgressRatio(
            exercises: exercises,
            workouts: workouts,
            draftExercises: draft.exercises,
            editingWorkoutID: draft.editingWorkoutID
        )
    }

    func planningContext(for exerciseID: Int) -> ExercisePlanningContext? {
        if draft.editingWorkoutID == nil,
           let planExercise = appliedPlan?.exercises.first(where: { $0.exerciseID == exerciseID }) {
            return TrainerLogic.planPlanningContext(
                workouts: workouts,
                exerciseID: exerciseID,
                planExercise: planExercise,
                excludeWorkoutID: nil
            )
        }
        return TrainerLogic.planningContext(
            workouts: workouts,
            exerciseID: exerciseID,
            excludeWorkoutID: draft.editingWorkoutID
        )
    }

    func plannedSetForEditor(exerciseID: Int) -> DraftSet {
        nextPlannedSet(exerciseID: exerciseID)
    }

    func exerciseDefinition(id: Int) -> ExerciseDefinition? {
        exercises.first { $0.id == id }
    }

    func progressExerciseOptions() -> [ExerciseDefinition] {
        TrainerLogic.progressExercises(catalog: exercises, workouts: workouts)
    }

    func progressSeries() -> [ProgressPoint] {
        guard let selectedProgressExerciseID else { return [] }
        return TrainerLogic.buildExerciseProgressSeries(
            workouts: workouts,
            range: selectedRange,
            exerciseID: selectedProgressExerciseID
        )
    }

    func bodyWeightEntriesForSelectedRange() -> [BodyWeightEntry] {
        TrainerLogic.bodyWeightEntriesInRange(bodyWeightEntries, range: selectedBodyWeightRange)
    }

    func bodyWeightSummary() -> BodyWeightSummary {
        TrainerLogic.summarizeBodyWeights(
            filteredEntries: bodyWeightEntriesForSelectedRange(),
            allEntries: bodyWeightEntries
        )
    }

    private func loadAuthenticatedData(
        client: APIClient,
        session: SessionResolveResponse,
        showSuccess: Bool
    ) async throws {
        async let exerciseResponse = client.fetchExercises()
        async let workoutsResponse = client.fetchWorkouts()
        async let weightsResponse = client.fetchBodyWeights()
        async let waistsResponse = client.fetchWaists()

        let (exercisePayload, workoutPayload, weightPayload, waistPayload) = try await (
            exerciseResponse,
            workoutsResponse,
            weightsResponse,
            waistsResponse
        )

        currentUser = workoutPayload.user ?? weightPayload.user ?? session.user ?? currentUser
        exercises = exercisePayload.exercises
        workouts = TrainerLogic.sortWorkouts(workoutPayload.workouts)
        bodyWeightEntries = TrainerLogic.sortBodyWeights(weightPayload.entries)
        waistEntries = waistPayload.entries.sorted { $0.entryDate < $1.entryDate }
        ensureSelectedProgressExercise()
        ensureDraftExerciseDefinitions()
        syncBodyWeightComposer()
        syncWaistComposer()
        bootState = .loaded
        if showSuccess {
            showToast("Данные обновлены")
        }
        // Fetch the coach recommendation off the boot path: a new unstructured Task
        // so the 3s reload deadline (which cancels the boot work task) never touches it.
        Task { [weak self] in await self?.loadRecommendation() }
        Task { [weak self] in await self?.loadCoachSignals() }
    }

    private func handleLoadError(_ error: Error) {
        let message = error.localizedDescription
        if (error as? TrainerAPIError)?.statusCode == 401 {
            bootState = .needsSignIn(message)
        } else {
            bootState = .failed(message)
        }
        showToast(message)
    }

    private func addSet(_ set: DraftSet, to exercise: ExerciseDefinition) {
        if let index = draft.exercises.firstIndex(where: { $0.exerciseID == exercise.id }) {
            draft.exercises[index].sets.append(set)
        } else {
            draft.exercises.append(
                DraftExercise(
                    exerciseID: exercise.id,
                    exerciseName: exercise.name,
                    sets: [set]
                )
            )
        }
    }

    private func updateSet(_ set: DraftSet, exerciseID: Int, setIndex: Int) {
        guard let exerciseIndex = draft.exercises.firstIndex(where: { $0.exerciseID == exerciseID }),
              draft.exercises[exerciseIndex].sets.indices.contains(setIndex) else {
            return
        }
        draft.exercises[exerciseIndex].sets[setIndex] = set
    }

    private func ensureSelectedProgressExercise() {
        let options = progressExerciseOptions()
        guard !options.isEmpty else {
            selectedProgressExerciseID = nil
            return
        }

        if let selectedProgressExerciseID,
           options.contains(where: { $0.id == selectedProgressExerciseID }) {
            return
        }

        selectedProgressExerciseID = options[0].id
    }

    private func ensureDraftExerciseDefinitions() {
        guard !exercises.isEmpty else { return }
        draft.exercises = draft.exercises.compactMap { draftExercise in
            guard let definition = exerciseDefinition(id: draftExercise.exerciseID) else {
                return nil
            }
            return DraftExercise(
                exerciseID: draftExercise.exerciseID,
                exerciseName: definition.name,
                sets: draftExercise.sets
            )
        }
    }

    private func persistDraft() {
        if !draft.hasAnyExercise && draft.editingWorkoutID == nil {
            defaults.removeObject(forKey: Keys.draft)
            return
        }

        if let data = try? JSONEncoder().encode(draft) {
            defaults.set(data, forKey: Keys.draft)
        }
    }

    private static func readDraft(defaults: UserDefaults) -> DraftWorkout {
        guard let data = defaults.data(forKey: Keys.draft),
              let draft = try? JSONDecoder().decode(DraftWorkout.self, from: data) else {
            return .empty
        }
        return draft
    }

    private func persistAppliedPlan() {
        guard let appliedPlan else {
            defaults.removeObject(forKey: Keys.appliedPlan)
            return
        }
        if let data = try? JSONEncoder().encode(appliedPlan) {
            defaults.set(data, forKey: Keys.appliedPlan)
        }
    }

    private static func readAppliedPlan(defaults: UserDefaults) -> AppliedCoachPlan? {
        guard let data = defaults.data(forKey: Keys.appliedPlan) else { return nil }
        return try? JSONDecoder().decode(AppliedCoachPlan.self, from: data)
    }

    private static func normalizedBackendURL(_ storedURL: String?) -> String {
        let localDevelopmentURL = "http://127.0.0.1:8080"
        let localHostURL = "http://localhost:8080"
        let legacyProductionURL = "https://trainer.89.124.83.32.nip.io:8443"
        let productionURL = "https://trainer.superbatonec.org"
        var value = storedURL?.trimmingCharacters(in: .whitespacesAndNewlines)
        while value?.hasSuffix("/") == true {
            value?.removeLast()
        }
        guard let value, !value.isEmpty,
              value != localDevelopmentURL,
              value != localHostURL,
              value != legacyProductionURL else {
            return productionURL
        }
        return value
    }
}

private enum Keys {
    static let apiBaseURL = "trainer-ios-api-base-url-v1"
    static let draft = "trainer-ios-draft-v1"
    static let appliedPlan = "trainer-ios-applied-plan-v1"
    static let currentTab = "trainer-ios-tab-v1"
    static let progressRange = "trainer-ios-progress-range-v1"
    static let bodyWeightRange = "trainer-ios-body-weight-range-v1"
    static let progressExercise = "trainer-ios-progress-exercise-v1"
}
