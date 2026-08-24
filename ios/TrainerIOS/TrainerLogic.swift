import Foundation

enum TrainerLogic {
    static let rareOnlyBenchPressID = 1

    static func sortWorkouts(_ workouts: [Workout]) -> [Workout] {
        workouts.sorted { left, right in
            if left.workoutDate == right.workoutDate {
                let leftCreatedAt = left.createdAt ?? 0
                let rightCreatedAt = right.createdAt ?? 0
                if leftCreatedAt != rightCreatedAt {
                    return leftCreatedAt > rightCreatedAt
                }

                let leftUpdatedAt = left.updatedAt ?? 0
                let rightUpdatedAt = right.updatedAt ?? 0
                if leftUpdatedAt != rightUpdatedAt {
                    return leftUpdatedAt > rightUpdatedAt
                }

                return (left.id ?? 0) > (right.id ?? 0)
            }

            return right.workoutDate < left.workoutDate
        }
    }

    static func sortBodyWeights(_ entries: [BodyWeightEntry]) -> [BodyWeightEntry] {
        entries.sorted { left, right in
            if left.entryDate == right.entryDate {
                let updatedDiff = (left.updatedAt ?? 0) - (right.updatedAt ?? 0)
                if updatedDiff != 0 {
                    return updatedDiff < 0
                }
                return left.id < right.id
            }
            return left.entryDate < right.entryDate
        }
    }

    // MARK: - Лента «Истории»: тренировки, события и подсказки в разрывах

    /// Разрыв — сплошной ряд дней без тренировок между двумя соседними
    /// тренировками (или между последней тренировкой и сегодняшним днём).
    struct HistoryGap: Hashable {
        var startDate: String
        var endDate: String
        var days: Int
        /// Верхний разрыв упирается в сегодня — он единственный, который ещё
        /// может продолжаться, поэтому композер открывается из него «идёт».
        var isRunning: Bool
    }

    enum HistoryFeedItem: Identifiable, Hashable {
        case workout(Workout)
        case event(TrainingEvent)
        case gap(HistoryGap)

        var id: String {
            switch self {
            case .workout(let workout): "workout-\(workout.stableID)"
            case .event(let event): "event-\(event.id)"
            case .gap(let gap): "gap-\(gap.startDate)"
            }
        }
    }

    /// Короче трёх дней — это не перерыв, а обычный отдых между тренировками:
    /// объяснять там нечего, и подсказка только шумела бы в ленте.
    static let minPromptableGapDays = 3

    /// Лента истории одним списком. Тренировки остаются главным содержимым,
    /// событие и подсказка встают по дате начала — то есть ровно в ту дырку,
    /// которую объясняют.
    ///
    /// «Сегодня» приходит аргументом: функция обязана быть чистой, иначе тест
    /// на верхний разрыв зависел бы от даты прогона.
    static func historyFeed(
        workouts: [Workout],
        events: [TrainingEvent],
        today: String
    ) -> [HistoryFeedItem] {
        let sortedWorkouts = sortWorkouts(workouts)
        var entries: [(date: String, rank: Int, item: HistoryFeedItem)] = sortedWorkouts.map {
            ($0.workoutDate, 2, .workout($0))
        }
        entries += events.map { ($0.startDate, 1, .event($0)) }
        entries += promptableGaps(workouts: sortedWorkouts, events: events, today: today).map {
            ($0.startDate, 0, .gap($0))
        }

        // Sort в Swift не обещает стабильности, поэтому исходный индекс —
        // третий ключ: порядок нескольких тренировок одного дня уже выбран
        // sortWorkouts, и пересортировка не должна его перемешивать.
        return entries.enumerated()
            .sorted { left, right in
                if left.element.date != right.element.date {
                    return left.element.date > right.element.date
                }
                if left.element.rank != right.element.rank {
                    return left.element.rank > right.element.rank
                }
                return left.offset < right.offset
            }
            .map(\.element.item)
    }

    /// Разрывы, которые ещё некому объяснить: длиннее порога и не пересечённые
    /// ни одним событием. Подсказка живёт только МЕЖДУ тренировками — поэтому
    /// интерфейс физически не может предложить событие на дату с тренировкой.
    static func promptableGaps(
        workouts: [Workout],
        events: [TrainingEvent],
        today: String
    ) -> [HistoryGap] {
        // Даты канонические (YYYY-MM-DD), поэтому сравнение строк — это
        // сравнение дней, и весь расчёт обходится без разбора в Date.
        let dates = Set(workouts.map(\.workoutDate)).sorted()
        guard !dates.isEmpty else { return [] }

        var gaps: [HistoryGap] = []
        for (index, date) in dates.enumerated() {
            let isLast = index == dates.count - 1
            let start = DateTools.adding(days: 1, to: date)
            // Верхний разрыв включает сегодняшний день: тренировки на него ещё
            // нет, значит он такой же день без зала, как и предыдущие.
            let end = isLast ? today : DateTools.adding(days: -1, to: dates[index + 1])
            guard end >= start else { continue }

            let days = DateTools.daysBetween(start, end) + 1
            guard days >= minPromptableGapDays else { continue }
            gaps.append(
                HistoryGap(startDate: start, endDate: end, days: days, isRunning: isLast)
            )
        }

        return gaps.filter { gap in
            !events.contains { event in
                event.startDate <= gap.endDate && event.lastDay(today: today) >= gap.startDate
            }
        }
    }

    /// Подписи рельсы события: число (или диапазон чисел) и месяц. Период на
    /// стыке месяцев показывает оба — иначе «30–3» под «АПР» читается как
    /// период внутри апреля.
    static func eventRailLabels(_ event: TrainingEvent, today: String) -> (
        day: String, month: String
    ) {
        let startDay = DateTools.dayNumber(event.startDate)
        let startMonth = DateTools.monthShort(event.startDate)
        guard let endDate = event.endDate, endDate != event.startDate else {
            return (startDay, startMonth)
        }

        let endMonth = DateTools.monthShort(endDate)
        return (
            "\(startDay)–\(DateTools.dayNumber(endDate))",
            startMonth == endMonth ? startMonth : "\(startMonth)–\(endMonth)"
        )
    }

    /// Оценка длительности тренировки по числу сетов. Настоящего таймера в
    /// приложении нет, поэтому число одно и то же везде, где показывается.
    static func workoutDurationMinutes(_ workout: Workout) -> Int {
        let setCount = workout.data.exercises.reduce(0) { $0 + $1.sets.count }
        return max(8, setCount * 3 + workout.data.exercises.count * 2)
    }

    /// Строка заметок к подходам под упражнением в карточке истории.
    /// Одинаковый текст на нескольких подходах схлопывается: постановка одна,
    /// а «канат · канат · канат» — это шум, а не три разных факта.
    static func setNotesLine(_ notes: [String]) -> String {
        var seen = Set<String>()
        return notes.filter { seen.insert($0).inserted }.joined(separator: " · ")
    }

    /// Итог только что записанной тренировки для полоски «Тренировка записана».
    /// Слова не склоняются («14 сет», а не «14 сетов») — склонения числительных
    /// в проекте нет, и то же сокращение уже стоит в шапке «Сегодня».
    static func finishedWorkoutSummary(_ workout: Workout) -> String {
        let exercises = workout.data.exercises.count
        let sets = workout.data.exercises.reduce(0) { $0 + $1.sets.count }
        return "\(exercises) упр · \(sets) сет · \(workoutDurationMinutes(workout)) мин"
    }

    static func exercisePickerGroups(
        available: [ExerciseDefinition],
        catalog: [ExerciseDefinition],
        workouts: [Workout],
        draftExercises: [DraftExercise]
    ) -> ExercisePickerGroups {
        guard !available.isEmpty else {
            return ExercisePickerGroups(
                primary: [],
                secondary: [],
                primaryPoolExhausted: false,
                primaryPoolTotal: 0,
                completedPrimaryCount: 0,
                primaryPoolIDs: []
            )
        }

        let stats = exerciseUsageStats(workouts)
        let catalogSource = catalog.isEmpty ? available : catalog
        let rankedCatalog = catalogSource.enumerated().map { index, exercise in
            RankedExercise(
                exercise: exercise,
                count: stats[exercise.id]?.count ?? 0,
                averagePosition: stats[exercise.id]?.averagePosition ?? .infinity,
                latestWorkoutDate: stats[exercise.id]?.latestWorkoutDate ?? "",
                catalogIndex: index
            )
        }

        let rankedByImportance = rankedCatalog.sorted(by: compareByImportance)
        let suggestedPool = rankedByImportance.filter { $0.count > 0 }.prefix(6)
        let primaryPool =
            suggestedPool.isEmpty ? Array(rankedByImportance.prefix(6)) : Array(suggestedPool)
        var primaryIDs = Set(primaryPool.map { $0.exercise.id })
        let replacement = rankedByImportance.first {
            $0.exercise.id != rareOnlyBenchPressID && !primaryIDs.contains($0.exercise.id)
        }

        if primaryIDs.contains(rareOnlyBenchPressID) {
            primaryIDs.remove(rareOnlyBenchPressID)
            if let replacement {
                primaryIDs.insert(replacement.exercise.id)
            }
        }

        let rankedAvailable = available.enumerated().map { index, exercise in
            RankedExercise(
                exercise: exercise,
                count: stats[exercise.id]?.count ?? 0,
                averagePosition: stats[exercise.id]?.averagePosition ?? .infinity,
                latestWorkoutDate: stats[exercise.id]?.latestWorkoutDate ?? "",
                catalogIndex: index
            )
        }

        let primary =
            rankedAvailable
            .filter { primaryIDs.contains($0.exercise.id) }
            .sorted(by: comparePrimaryDisplay)
            .map(\.exercise)

        let completedIDs = Set(
            draftExercises
                .filter { !$0.sets.isEmpty }
                .map(\.exerciseID)
        )
        let completedPrimaryCount = primaryIDs.filter { completedIDs.contains($0) }.count

        let secondary =
            rankedAvailable
            .filter { !primaryIDs.contains($0.exercise.id) }
            .sorted(by: compareSecondaryDisplay)
            .map(\.exercise)

        return ExercisePickerGroups(
            primary: primary,
            secondary: secondary,
            primaryPoolExhausted: !primaryPool.isEmpty && primary.isEmpty && !secondary.isEmpty,
            primaryPoolTotal: primaryIDs.count,
            completedPrimaryCount: completedPrimaryCount,
            primaryPoolIDs: Array(primaryIDs)
        )
    }

    static func draftDisplayCards(
        exercises: [ExerciseDefinition],
        workouts: [Workout],
        draftExercises: [DraftExercise]
    ) -> [DraftDisplayExercise] {
        guard !exercises.isEmpty else {
            return draftExercises.map {
                DraftDisplayExercise(
                    exerciseID: $0.exerciseID,
                    exerciseName: $0.exerciseName,
                    sets: $0.sets,
                    isPreview: false
                )
            }
        }

        let groups = exercisePickerGroups(
            available: exercises,
            catalog: exercises,
            workouts: workouts,
            draftExercises: draftExercises
        )

        guard !groups.primary.isEmpty else {
            return draftExercises.map {
                DraftDisplayExercise(
                    exerciseID: $0.exerciseID,
                    exerciseName: $0.exerciseName,
                    sets: $0.sets,
                    isPreview: false
                )
            }
        }

        let actualByID = Dictionary(
            uniqueKeysWithValues: draftExercises.map { ($0.exerciseID, $0) })
        var usedActualIDs = Set<Int>()
        var cards: [DraftDisplayExercise] = groups.primary.map { exercise in
            if let actual = actualByID[exercise.id] {
                usedActualIDs.insert(actual.exerciseID)
                return DraftDisplayExercise(
                    exerciseID: actual.exerciseID,
                    exerciseName: actual.exerciseName,
                    sets: actual.sets,
                    isPreview: false
                )
            }

            return DraftDisplayExercise(
                exerciseID: exercise.id,
                exerciseName: exercise.name,
                sets: [],
                isPreview: true
            )
        }

        for actual in draftExercises where !usedActualIDs.contains(actual.exerciseID) {
            cards.append(
                DraftDisplayExercise(
                    exerciseID: actual.exerciseID,
                    exerciseName: actual.exerciseName,
                    sets: actual.sets,
                    isPreview: false
                )
            )
        }

        return cards
    }

    /// Display cards when a coach plan is applied: plan exercises in the
    /// recommended ORDER (merged with logged draft sets), then any extra
    /// exercises the user added outside the plan.
    static func planDisplayCards(
        plan: AppliedCoachPlan,
        draftExercises: [DraftExercise]
    ) -> [DraftDisplayExercise] {
        let actualByID = Dictionary(
            uniqueKeysWithValues: draftExercises.map { ($0.exerciseID, $0) })
        var usedActualIDs = Set<Int>()
        var cards: [DraftDisplayExercise] = plan.exercises.map { planned in
            if let actual = actualByID[planned.exerciseID] {
                usedActualIDs.insert(actual.exerciseID)
                return DraftDisplayExercise(
                    exerciseID: actual.exerciseID,
                    exerciseName: actual.exerciseName,
                    sets: actual.sets,
                    isPreview: false
                )
            }
            return DraftDisplayExercise(
                exerciseID: planned.exerciseID,
                exerciseName: planned.name,
                sets: [],
                isPreview: true
            )
        }

        for actual in draftExercises where !usedActualIDs.contains(actual.exerciseID) {
            cards.append(
                DraftDisplayExercise(
                    exerciseID: actual.exerciseID,
                    exerciseName: actual.exerciseName,
                    sets: actual.sets,
                    isPreview: false
                )
            )
        }

        return cards
    }

    /// Каталог под кнопкой «Добавить упражнение»: всё, чего сейчас нет на
    /// экране, от самого частого к самому редкому.
    ///
    /// Отсекаются именно ПОКАЗАННЫЕ карточки, а не «основная шестёрка». Пока
    /// список был её дополнением, он ломался тихо, как только карточки начинал
    /// задавать план тренера: частое упражнение вне плана пропадало отовсюду —
    /// ни карточки с «+», ни строчки в каталоге, — и добавить его в тренировку
    /// было нечем.
    static func addableExercises(
        exercises: [ExerciseDefinition],
        workouts: [Workout],
        shownExerciseIDs: Set<Int>
    ) -> [ExerciseDefinition] {
        let stats = exerciseUsageStats(workouts)
        return
            exercises
            .enumerated()
            .filter { !shownExerciseIDs.contains($0.element.id) }
            .map { index, exercise in
                RankedExercise(
                    exercise: exercise,
                    count: stats[exercise.id]?.count ?? 0,
                    averagePosition: stats[exercise.id]?.averagePosition ?? .infinity,
                    latestWorkoutDate: stats[exercise.id]?.latestWorkoutDate ?? "",
                    catalogIndex: index
                )
            }
            .sorted(by: compareSecondaryDisplay)
            .map(\.exercise)
    }

    static func draftProgressRatio(
        exercises: [ExerciseDefinition],
        workouts: [Workout],
        draftExercises: [DraftExercise],
        editingWorkoutID: Int?
    ) -> Double {
        guard draftExercises.contains(where: { !$0.sets.isEmpty }) else {
            return 0
        }

        let groups = exercisePickerGroups(
            available: exercises,
            catalog: exercises,
            workouts: workouts,
            draftExercises: draftExercises
        )
        guard groups.primaryPoolTotal > 0 else {
            return 0
        }

        let actualByID = Dictionary(
            uniqueKeysWithValues: draftExercises.map { ($0.exerciseID, $0) })
        let total = groups.primaryPoolIDs.reduce(0.0) { partial, exerciseID in
            let context = planningContext(
                workouts: workouts,
                exerciseID: exerciseID,
                excludeWorkoutID: editingWorkoutID
            )
            let targetCount = max(1, context?.plannedSets.count ?? 0)
            let actualCount = actualByID[exerciseID]?.sets.count ?? 0
            return partial + min(Double(actualCount), Double(targetCount)) / Double(targetCount)
        }

        return max(0, min(1, total / Double(groups.primaryPoolTotal)))
    }

    static func progressExercises(catalog: [ExerciseDefinition], workouts: [Workout])
        -> [ExerciseDefinition]
    {
        var lookup = Dictionary(uniqueKeysWithValues: catalog.map { ($0.id, $0) })
        for workout in sortWorkouts(workouts) {
            for exercise in workout.data.exercises where lookup[exercise.exerciseID] == nil {
                lookup[exercise.exerciseID] = ExerciseDefinition(
                    id: exercise.exerciseID, name: exercise.name)
            }
        }

        var result: [ExerciseDefinition] = []
        for exercise in catalog {
            if let value = lookup.removeValue(forKey: exercise.id) {
                result.append(value)
            }
        }
        result.append(
            contentsOf: lookup.values.sorted {
                $0.name.localizedCompare($1.name) == .orderedAscending
            })
        return result
    }

    static func getWorkoutsInRange(_ workouts: [Workout], range: RangeOption) -> [Workout] {
        let today = Calendar.current.startOfDay(for: Date())
        return
            workouts
            .map { workout in
                (workout, DateTools.date(from: workout.workoutDate))
            }
            .filter { _, date in
                inRange(date: date, rangeDays: range.days, today: today)
            }
            .sorted { left, right in
                left.1 > right.1
            }
            .map(\.0)
    }

    static func summarizeProgress(workouts: [Workout], range: RangeOption) -> Int {
        getWorkoutsInRange(workouts, range: range).count
    }

    static func buildExerciseProgressSeries(
        workouts: [Workout],
        range: RangeOption,
        exerciseID: Int
    ) -> [ProgressPoint] {
        return Array(
            getWorkoutsInRange(workouts, range: range)
                .compactMap { workout -> ProgressPoint? in
                    guard
                        let exercise = workout.data.exercises.first(where: {
                            $0.exerciseID == exerciseID
                        }),
                        let heaviest = pickHeaviestSet(exercise.sets),
                        let highestReps = pickHighestRepSet(exercise.sets)
                    else {
                        return nil
                    }

                    return ProgressPoint(
                        workoutID: workout.id ?? 0,
                        workoutDate: workout.workoutDate,
                        bestWeight: heaviest.weight,
                        repsAtBestWeight: heaviest.reps,
                        bestReps: highestReps.reps,
                        weightAtBestReps: highestReps.weight
                    )
                }
                .reversed())
    }

    static func summarizeExerciseSeries(_ series: [ProgressPoint]) -> ExerciseSeriesSummary? {
        guard let first = series.first, let latest = series.last else {
            return nil
        }

        return ExerciseSeriesSummary(
            firstPoint: first,
            latestPoint: latest,
            weightDelta: latest.bestWeight - first.bestWeight,
            repsDelta: latest.bestReps - first.bestReps
        )
    }

    static func bodyWeightEntriesInRange(_ entries: [BodyWeightEntry], range: RangeOption)
        -> [BodyWeightEntry]
    {
        let today = Calendar.current.startOfDay(for: Date())
        return sortBodyWeights(entries)
            .filter { entry in
                inRange(
                    date: DateTools.date(from: entry.entryDate), rangeDays: range.days, today: today
                )
            }
    }

    static func summarizeBodyWeights(
        filteredEntries: [BodyWeightEntry],
        allEntries: [BodyWeightEntry]
    ) -> BodyWeightSummary {
        let sortedAll = sortBodyWeights(allEntries)
        let sortedFiltered = sortBodyWeights(filteredEntries)
        let latestOverall = sortedAll.last
        let latest = sortedFiltered.last
        let first = sortedFiltered.first

        return BodyWeightSummary(
            totalEntries: sortedFiltered.count,
            latestOverallEntry: latestOverall,
            latestEntry: latest,
            firstEntry: first,
            delta: latest != nil && first != nil ? latest!.weight - first!.weight : 0
        )
    }

    static func planningContext(
        workouts: [Workout],
        exerciseID: Int,
        excludeWorkoutID: Int? = nil
    ) -> ExercisePlanningContext? {
        guard
            let source = latestExerciseSource(
                workouts: workouts,
                exerciseID: exerciseID,
                excludeWorkoutID: excludeWorkoutID
            )
        else {
            return nil
        }

        let previousSets = normalizedExerciseSets(source.exercise.sets, incrementReps: 0)
        guard !previousSets.isEmpty else {
            return nil
        }

        let plannedSets = normalizedExerciseSets(
            previousSets,
            incrementReps: 1,
            preserveNotes: false,
            preserveEffort: false
        )
        .enumerated()
        .map { index, set in
            WorkoutSet(
                setIndex: index + 1,
                reps: set.reps,
                weight: set.weight,
                effort: nil,
                notes: nil
            )
        }

        let previousSummary = summarizeExerciseSets(previousSets)
        let plannedSummary = summarizeExerciseSets(plannedSets)

        return ExercisePlanningContext(
            workoutID: source.workout.id,
            workoutDate: source.workout.workoutDate,
            exerciseName: source.exercise.name,
            previousSets: previousSets,
            plannedSets: plannedSets,
            previousSummary: previousSummary,
            plannedSummary: plannedSummary,
            progressionParts: referenceProgressionParts(
                previousSummary: previousSummary, plannedSummary: plannedSummary),
            maxWeight: previousSets.map(\.weight).max() ?? 0
        )
    }

    /// Planning context when the exercise's targets come from an applied coach
    /// plan: the green target shows the plan's weight AND reps (the coach may
    /// change the weight, unlike the history-based +1-rep plan). Never nil —
    /// the plan itself is the target even without past performances.
    static func planPlanningContext(
        workouts: [Workout],
        exerciseID: Int,
        planExercise: RecommendedExercise,
        excludeWorkoutID: Int? = nil
    ) -> ExercisePlanningContext {
        let source = latestExerciseSource(
            workouts: workouts,
            exerciseID: exerciseID,
            excludeWorkoutID: excludeWorkoutID
        )
        let previousSets =
            source.map { normalizedExerciseSets($0.exercise.sets, incrementReps: 0) } ?? []

        let plannedSets = planExercise.sets.enumerated().map { index, target in
            WorkoutSet(
                setIndex: index + 1,
                reps: target.reps,
                weight: target.weight,
                effort: nil,
                notes: nil
            )
        }

        let previousSummary = summarizeExerciseSets(previousSets)
        let plannedSummary = summarizeExerciseSets(plannedSets)

        let progressionParts = plannedSummary.segments.enumerated().map {
            index, segment -> ReferenceProgressionPart in
            let previousSegment =
                index < previousSummary.segments.count
                ? previousSummary.segments[index]
                : previousSummary.segments.last
            return ReferenceProgressionPart(
                previousLabel: previousSegment.map {
                    "\(formatWeight($0.weight))кг ×\(summarizeRepRuns($0.reps))"
                } ?? "—",
                nextLabel: "\(formatWeight(segment.weight))кг ×\(summarizeRepRuns(segment.reps))",
                previousEffort: previousSegment?.effort
            )
        }

        return ExercisePlanningContext(
            workoutID: source?.workout.id,
            workoutDate: source?.workout.workoutDate ?? "",
            exerciseName: planExercise.name,
            previousSets: previousSets,
            plannedSets: plannedSets,
            previousSummary: previousSummary,
            plannedSummary: plannedSummary,
            progressionParts: progressionParts,
            maxWeight: previousSets.map(\.weight).max() ?? 0
        )
    }

    /// Ring progress against an applied coach plan: fraction of target sets
    /// done per plan exercise, averaged over the plan.
    static func planProgressRatio(
        plan: AppliedCoachPlan,
        draftExercises: [DraftExercise]
    ) -> Double {
        guard draftExercises.contains(where: { !$0.sets.isEmpty }), !plan.exercises.isEmpty else {
            return 0
        }

        let actualByID = Dictionary(
            uniqueKeysWithValues: draftExercises.map { ($0.exerciseID, $0) })
        let total = plan.exercises.reduce(0.0) { partial, exercise in
            let targetCount = max(1, exercise.sets.count)
            let actualCount = actualByID[exercise.exerciseID]?.sets.count ?? 0
            return partial + min(Double(actualCount), Double(targetCount)) / Double(targetCount)
        }

        return max(0, min(1, total / Double(plan.exercises.count)))
    }

    static func plannedSet(
        workouts: [Workout],
        exerciseID: Int,
        draftSetIndex: Int,
        excludeWorkoutID: Int? = nil
    ) -> DraftSet {
        let index = max(0, draftSetIndex)
        guard
            let context = planningContext(
                workouts: workouts,
                exerciseID: exerciseID,
                excludeWorkoutID: excludeWorkoutID
            ), !context.plannedSets.isEmpty
        else {
            return DraftSet(reps: 12, weight: 0, effort: nil, notes: nil)
        }

        let template = context.plannedSets[min(index, context.plannedSets.count - 1)]
        return DraftSet(reps: template.reps, weight: template.weight, effort: nil, notes: nil)
    }

    /// Single source of truth for what the quick "+" (and the editor prefill)
    /// should propose next for an exercise.
    ///
    /// Priority:
    /// 1. The last logged draft set was CUSTOM (deviates from its template) →
    ///    continue from that set, not from the template.
    /// 2. An applied coach plan covers the exercise → its target for the next index.
    /// 3. History-based plan (last performance, +1 rep per set).
    /// 4. Fallback 12 × 0.
    static func nextPlannedSet(
        workouts: [Workout],
        exerciseID: Int,
        draftSets: [DraftSet],
        planTargets: [RecommendedSet]?,
        excludeWorkoutID: Int? = nil
    ) -> DraftSet {
        let templates = setTemplates(
            workouts: workouts,
            exerciseID: exerciseID,
            planTargets: planTargets,
            excludeWorkoutID: excludeWorkoutID
        )

        func template(at index: Int) -> DraftSet? {
            guard !templates.isEmpty else { return nil }
            let clamped = templates[min(max(0, index), templates.count - 1)]
            return DraftSet(reps: clamped.reps, weight: clamped.weight, effort: nil, notes: nil)
        }

        if let last = draftSets.last {
            let lastIndex = draftSets.count - 1
            // Effort/notes never count as deviation; weight gets an epsilon so
            // JSON doubles vs ±2.5 stepper arithmetic can't cause phantom drift.
            if let expected = template(at: lastIndex),
                expected.reps == last.reps, abs(expected.weight - last.weight) < 0.01
            {
                // On template — keep walking the plan.
                return template(at: draftSets.count)
                    ?? DraftSet(reps: last.reps, weight: last.weight, effort: nil, notes: nil)
            }
            // Custom set — repeat it instead of snapping back to the template.
            return DraftSet(reps: last.reps, weight: last.weight, effort: nil, notes: nil)
        }

        return template(at: 0) ?? DraftSet(reps: 12, weight: 0, effort: nil, notes: nil)
    }

    private static func setTemplates(
        workouts: [Workout],
        exerciseID: Int,
        planTargets: [RecommendedSet]?,
        excludeWorkoutID: Int?
    ) -> [DraftSet] {
        if let planTargets, !planTargets.isEmpty {
            return planTargets.map {
                DraftSet(reps: $0.reps, weight: $0.weight, effort: nil, notes: nil)
            }
        }

        guard
            let context = planningContext(
                workouts: workouts,
                exerciseID: exerciseID,
                excludeWorkoutID: excludeWorkoutID
            )
        else {
            return []
        }

        return context.plannedSets.map {
            DraftSet(reps: $0.reps, weight: $0.weight, effort: nil, notes: nil)
        }
    }

    static func summarizeExerciseSets(_ sets: [WorkoutSet]) -> ExerciseSetSummary {
        guard !sets.isEmpty else {
            return ExerciseSetSummary(parts: ["Пока нет сетов"], notes: [], segments: [])
        }

        var grouped: [MutableSummaryGroup] = []
        var current: MutableSummaryGroup?

        for (index, set) in sets.enumerated() {
            let note = set.notes?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if current?.weight == set.weight && current?.effort == set.effort {
                current?.reps.append(set.reps)
                if !note.isEmpty {
                    current?.notes.append(note)
                }
            } else {
                if let current {
                    grouped.append(current)
                }
                current = MutableSummaryGroup(
                    weight: set.weight,
                    reps: [set.reps],
                    effort: set.effort,
                    notes: note.isEmpty ? [] : [note],
                    editSetIndex: index
                )
            }
        }

        if let current {
            grouped.append(current)
        }

        let segments = grouped.map { group in
            let base = "\(formatWeight(group.weight))кг ×\(summarizeRepRuns(group.reps))"
            return ExerciseSetSummarySegment(
                label: base,
                editSetIndex: group.editSetIndex,
                effort: group.effort,
                notes: group.notes,
                weight: group.weight,
                reps: group.reps
            )
        }

        return ExerciseSetSummary(
            parts: segments.map { segment in
                segment.effort == nil ? segment.label : "\(segment.label) \(segment.effort!.icon)"
            },
            notes: sets.compactMap {
                $0.notes?.trimmingCharacters(in: .whitespacesAndNewlines).nilIfBlank
            },
            segments: segments
        )
    }

    static func summarizeDraftSets(_ sets: [DraftSet]) -> ExerciseSetSummary {
        summarizeExerciseSets(
            sets.enumerated().map { index, set in
                set.asWorkoutSet(index: index + 1)
            }
        )
    }

    static func workoutPayload(
        from draft: DraftWorkout,
        recommendation: RecommendationSnapshot? = nil
    ) -> Workout {
        let exercises = draft.exercises
            .filter { !$0.sets.isEmpty }
            .map { exercise in
                LoggedExercise(
                    exerciseID: exercise.exerciseID,
                    name: exercise.exerciseName,
                    sets: exercise.sets.enumerated().map { index, set in
                        set.asWorkoutSet(index: index + 1)
                    }
                )
            }

        return Workout(
            id: draft.editingWorkoutID,
            clientID: draft.editingClientID
                ?? "workout-\(Int(Date().timeIntervalSince1970 * 1000))",
            workoutDate: draft.workoutDate,
            planID: nil,
            createdAt: nil,
            updatedAt: nil,
            data: WorkoutData(
                focus: nil,
                notes: nil,
                // The coach's own label when the session followed an applied
                // plan; otherwise honestly unknown. The old tonnage heuristic
                // (>=3000 kg -> heavy) marked nearly every real session heavy.
                loadType: recommendation?.loadType,
                exercises: exercises,
                recommendation: recommendation
            )
        )
    }

    static func formatWeight(_ value: Double) -> String {
        let formatter = NumberFormatter()
        formatter.locale = Locale(identifier: "ru_RU")
        formatter.minimumFractionDigits = value.rounded() == value ? 0 : 1
        formatter.maximumFractionDigits = 1
        return formatter.string(from: NSNumber(value: value)) ?? "\(value)"
    }

    /// Compact reps label for a recommendation's sets, used by the História
    /// "next workout" card: uniform reps collapse to "12 × 3", otherwise the
    /// per-set reps are listed ("12, 12, 10").
    static func recommendationRepsLabel(_ sets: [RecommendedSet]) -> String {
        let reps = sets.map(\.reps)
        guard !reps.isEmpty else { return "" }
        if Set(reps).count == 1 { return "\(reps[0]) × \(sets.count)" }
        return reps.map(String.init).joined(separator: ", ")
    }

    // Muscle-group map + weekly set landmarks — mirrors the backend coaching
    // policy (coach_features.MUSCLE_GROUPS) so the Progress screen shows the
    // same accounting the coach reasons over. Counting is by the exercise's
    // primary muscle (same as the prompt's volume report). `key` is the
    // backend group name used by coach_context.group_targets; min/max are the
    // static fallbacks when no server targets are available yet.
    static let muscleGroupLandmarks: [(name: String, key: String, ids: [Int], min: Int, max: Int)] =
        [
            ("Грудь", "грудь", [18, 1, 17], 10, 16),
            ("Спина", "спина", [9, 4, 10], 10, 16),
            ("Квадрицепс/ягод.", "квадрицепс/ягодичные", [8, 16], 10, 16),
            ("Дельты", "дельты", [13], 6, 12),
            ("Задняя дельта", "задняя дельта", [19], 4, 8),
            ("Бицепс", "бицепс", [11], 4, 8),
            ("Трицепс", "трицепс", [12], 4, 8),
            ("Бицепс бедра", "бицепс бедра", [15], 5, 10),
        ]

    /// Work sets per muscle group over the last `days` (default 7) — the weekly
    /// volume the coach tracks. `targets` (from the recommendation's
    /// coach_context) overrides the static landmarks with the CURRENT block
    /// week's corridor, so the screen agrees with what the coach is ramping.
    static func weeklyVolumeByGroup(
        _ workouts: [Workout],
        targets: [String: [Int]]? = nil,
        today: Date = Date(),
        days: Int = 7
    ) -> [MuscleGroupVolume] {
        let cal = Calendar.current
        let end = cal.startOfDay(for: today)
        guard let start = cal.date(byAdding: .day, value: -(days - 1), to: end) else { return [] }

        var setsByID: [Int: Int] = [:]
        for workout in workouts {
            let d = cal.startOfDay(for: DateTools.date(from: workout.workoutDate))
            guard d >= start && d <= end else { continue }
            for ex in workout.data.exercises {
                setsByID[ex.exerciseID, default: 0] += ex.sets.count
            }
        }
        return muscleGroupLandmarks.map { group in
            let count = group.ids.reduce(0) { $0 + (setsByID[$1] ?? 0) }
            var minTarget = group.min
            var maxTarget = group.max
            if let target = targets?[group.key], target.count == 2, target[0] <= target[1] {
                minTarget = target[0]
                maxTarget = target[1]
            }
            return MuscleGroupVolume(
                name: group.name, count: count, minTarget: minTarget, maxTarget: maxTarget)
        }
    }

    /// Plan-vs-performed adherence across workouts in `range` that carried a
    /// recommendation snapshot. Done sets are capped at planned per exercise so
    /// extra work doesn't inflate adherence past 100%. Mirrors the backend's
    /// 30-day discipline aggregate, including WHICH exercises get skipped.
    static func adherenceSummary(_ workouts: [Workout], range: RangeOption) -> AdherenceSummary {
        var compared = 0
        var planned = 0
        var done = 0
        var skipped = 0
        var skipCounts: [String: Int] = [:]
        for workout in getWorkoutsInRange(workouts, range: range) {
            guard let plan = workout.data.recommendation?.exercises, !plan.isEmpty else { continue }
            compared += 1
            var doneByID: [Int: Int] = [:]
            for ex in workout.data.exercises {
                doneByID[ex.exerciseID, default: 0] += ex.sets.count
            }
            for plannedExercise in plan {
                let target = plannedExercise.sets.count
                let actual = doneByID[plannedExercise.exerciseID] ?? 0
                planned += target
                done += min(target, actual)
                if actual == 0 {
                    skipped += 1
                    skipCounts[plannedExercise.name, default: 0] += 1
                }
            }
        }
        let byName =
            skipCounts
            .sorted { $0.value == $1.value ? $0.key < $1.key : $0.value > $1.value }
            .map { (name: $0.key, count: $0.value) }
        return AdherenceSummary(
            comparedWorkouts: compared,
            plannedSets: planned,
            doneSets: done,
            skippedExercises: skipped,
            skippedByName: byName
        )
    }

    /// The most recently logged working weight (heaviest set) for an exercise
    /// across all history — the "было" half of the было→план delta. Nil if the
    /// exercise has never been logged.
    static func latestWorkingWeight(in workouts: [Workout], exerciseID: Int) -> Double? {
        let matching = workouts.filter { workout in
            workout.data.exercises.contains { $0.exerciseID == exerciseID && !$0.sets.isEmpty }
        }
        guard
            let latest = matching.max(by: {
                DateTools.date(from: $0.workoutDate) < DateTools.date(from: $1.workoutDate)
            })
        else { return nil }
        let sets = latest.data.exercises.first { $0.exerciseID == exerciseID }?.sets ?? []
        return sets.map(\.weight).max()
    }

    static func formatBodyWeight(_ value: Double) -> String {
        let formatter = NumberFormatter()
        formatter.locale = Locale(identifier: "ru_RU")
        formatter.usesGroupingSeparator = false
        formatter.minimumFractionDigits = 0
        formatter.maximumFractionDigits = 20
        return formatter.string(from: NSNumber(value: value)) ?? "\(value)"
    }

    static func formatBodyWeightInput(_ value: Double) -> String {
        value.rounded() == value
            ? String(Int(value))
            : String(format: "%.1f", locale: Locale(identifier: "en_US_POSIX"), value)
    }

    static func normalizeBodyWeightInput(_ value: String) -> String {
        let raw =
            value
            .filter { $0.isNumber || $0 == "." || $0 == "," }
            .map { $0 == "," ? "." : $0 }
        let text = String(raw)
        guard !text.isEmpty else { return "" }
        guard let firstDot = text.firstIndex(of: ".") else { return text }
        let integer = String(text[..<firstDot])
        let fractionStart = text.index(after: firstDot)
        let fraction = text[fractionStart...].filter { $0 != "." }
        if fraction.isEmpty && text.hasSuffix(".") {
            return "\(integer.isEmpty ? "0" : integer)."
        }
        return "\(integer.isEmpty ? "0" : integer).\(fraction)"
    }

    static func formatSignedWeight(_ value: Double) -> String {
        "\(value > 0 ? "+" : "")\(formatWeight(value)) кг"
    }

    static func formatSignedBodyWeight(_ value: Double) -> String {
        "\(value > 0 ? "+" : "")\(formatWeight(value)) кг"
    }

    static func formatSignedReps(_ value: Int) -> String {
        "\(value > 0 ? "+" : "")\(value) повт."
    }

    private static func latestExerciseSource(
        workouts: [Workout],
        exerciseID: Int,
        excludeWorkoutID: Int?
    ) -> (workout: Workout, exercise: LoggedExercise)? {
        for workout in sortWorkouts(workouts) {
            if let excludeWorkoutID, workout.id == excludeWorkoutID {
                continue
            }

            if let exercise = workout.data.exercises.first(where: { $0.exerciseID == exerciseID }),
                !exercise.sets.isEmpty
            {
                return (workout, exercise)
            }
        }

        return nil
    }

    private static func normalizedExerciseSets(
        _ sets: [WorkoutSet],
        incrementReps: Int,
        preserveNotes: Bool = true,
        preserveEffort: Bool = true
    ) -> [WorkoutSet] {
        sets.enumerated().compactMap { index, set in
            let reps = max(1, set.reps + incrementReps)
            guard reps > 0 else { return nil }
            return WorkoutSet(
                setIndex: (set.setIndex ?? 0) > 0 ? set.setIndex : index + 1,
                reps: reps,
                weight: max(0, set.weight),
                effort: preserveEffort ? set.effort : nil,
                notes: preserveNotes ? set.notes?.nilIfBlank : nil
            )
        }
    }

    private static func referenceProgressionParts(
        previousSummary: ExerciseSetSummary,
        plannedSummary: ExerciseSetSummary
    ) -> [ReferenceProgressionPart] {
        previousSummary.segments.enumerated().map { index, segment in
            let nextSegment =
                index < plannedSummary.segments.count ? plannedSummary.segments[index] : nil
            let nextReps =
                nextSegment?.reps.isEmpty == false
                ? nextSegment!.reps
                : segment.reps.map { max(1, $0 + 1) }

            return ReferenceProgressionPart(
                previousLabel:
                    "\(formatWeight(segment.weight))кг ×\(summarizeRepRuns(segment.reps))",
                nextLabel: summarizeRepRuns(nextReps),
                previousEffort: segment.effort
            )
        }
    }

    private static func summarizeRepRuns(_ reps: [Int]) -> String {
        guard let first = reps.first else {
            return "0"
        }

        var parts: [String] = []
        var current = first
        var count = 1

        for rep in reps.dropFirst() {
            if rep == current {
                count += 1
            } else {
                parts.append(count > 1 ? "\(current)×\(count)" : "\(current)")
                current = rep
                count = 1
            }
        }

        parts.append(count > 1 ? "\(current)×\(count)" : "\(current)")
        return parts.joined(separator: ", ")
    }

    private static func pickHeaviestSet(_ sets: [WorkoutSet]) -> WorkoutSet? {
        sets.max { left, right in
            if left.weight != right.weight {
                return left.weight < right.weight
            }
            if left.reps != right.reps {
                return left.reps < right.reps
            }
            return (left.setIndex ?? 0) < (right.setIndex ?? 0)
        }
    }

    private static func pickHighestRepSet(_ sets: [WorkoutSet]) -> WorkoutSet? {
        sets.max { left, right in
            if left.reps != right.reps {
                return left.reps < right.reps
            }
            if left.weight != right.weight {
                return left.weight < right.weight
            }
            return (left.setIndex ?? 0) < (right.setIndex ?? 0)
        }
    }

    private static func inRange(date: Date, rangeDays: Int?, today: Date) -> Bool {
        guard let rangeDays else {
            return true
        }

        let calendar = Calendar.current
        let start = calendar.date(byAdding: .day, value: -(rangeDays - 1), to: today) ?? today
        let target = calendar.startOfDay(for: date)
        return target >= start && target <= today
    }

    private static func exerciseUsageStats(_ workouts: [Workout]) -> [Int: ExerciseUsageStat] {
        var stats: [Int: ExerciseUsageStat] = [:]
        for workout in sortWorkouts(workouts) {
            for (index, exercise) in workout.data.exercises.enumerated() {
                var current = stats[exercise.exerciseID] ?? ExerciseUsageStat()
                current.count += 1
                current.totalPosition += index
                if current.latestWorkoutDate.isEmpty
                    || workout.workoutDate > current.latestWorkoutDate
                {
                    current.latestWorkoutDate = workout.workoutDate
                }
                stats[exercise.exerciseID] = current
            }
        }

        for (exerciseID, value) in stats {
            var next = value
            next.averagePosition =
                value.count > 0 ? Double(value.totalPosition) / Double(value.count) : .infinity
            stats[exerciseID] = next
        }
        return stats
    }

    private static func compareByImportance(_ left: RankedExercise, _ right: RankedExercise) -> Bool
    {
        if left.count != right.count {
            return left.count > right.count
        }
        if left.latestWorkoutDate != right.latestWorkoutDate {
            return left.latestWorkoutDate > right.latestWorkoutDate
        }
        if left.averagePosition != right.averagePosition {
            return left.averagePosition < right.averagePosition
        }
        return left.catalogIndex < right.catalogIndex
    }

    private static func comparePrimaryDisplay(_ left: RankedExercise, _ right: RankedExercise)
        -> Bool
    {
        if left.averagePosition != right.averagePosition {
            return left.averagePosition < right.averagePosition
        }
        if left.count != right.count {
            return left.count > right.count
        }
        if left.latestWorkoutDate != right.latestWorkoutDate {
            return left.latestWorkoutDate > right.latestWorkoutDate
        }
        return left.catalogIndex < right.catalogIndex
    }

    private static func compareSecondaryDisplay(_ left: RankedExercise, _ right: RankedExercise)
        -> Bool
    {
        if left.count != right.count {
            return left.count > right.count
        }
        if left.latestWorkoutDate != right.latestWorkoutDate {
            return left.latestWorkoutDate > right.latestWorkoutDate
        }
        return left.exercise.name.localizedCompare(right.exercise.name) == .orderedAscending
    }
}

private struct ExerciseUsageStat {
    var count: Int = 0
    var totalPosition: Int = 0
    var averagePosition: Double = .infinity
    var latestWorkoutDate: String = ""
}

private struct RankedExercise {
    var exercise: ExerciseDefinition
    var count: Int
    var averagePosition: Double
    var latestWorkoutDate: String
    var catalogIndex: Int
}

private struct MutableSummaryGroup {
    var weight: Double
    var reps: [Int]
    var effort: SetEffort?
    var notes: [String]
    var editSetIndex: Int
}
