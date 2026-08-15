import Foundation

/// Голосовой слой поверх стора: одна фраза в наушниках → сет в черновике.
///
/// Слой намеренно живёт между `VoiceSetParser` (грамматика) и `VoiceIntents`
/// (Siri/App Intents): здесь принимаются все продуктовые решения — что считать
/// началом тренировки, что делать с забытым черновиком, чем добивать
/// непроизнесённые вес и повторы — и здесь же собираются фразы ответа, чтобы
/// они проверялись тестами, а не Siri.
///
/// Каждый результат несёт язык ответа: телефон может быть англоязычным, а Siri
/// — русской (это независимые настройки), поэтому язык определяется по самой
/// произнесённой фразе и запоминается для команд без фразы.

// MARK: - Результаты команд

/// Откуда взялись не названные вслух вес и повторы.
enum VoiceSetFill: Equatable {
    case plan
    case history
}

struct VoiceLoggedSet: Equatable {
    var exerciseName: String
    var weight: Double
    var reps: Int
    var effort: SetEffort?
    /// Номер сета внутри упражнения в этой тренировке (1-based).
    var setNumber: Int
    /// Сколько сетов на это упражнение стоит в применённом плане тренера.
    var plannedSets: Int?
    var startedWorkout: Bool
    /// Вес и повторы не были названы и подставлены — и откуда именно.
    var filledFrom: VoiceSetFill?
    /// Дата черновика, если это не сегодня (правка старой тренировки).
    var otherDate: String?
    /// Вес резко расходится с планом — вероятная ошибка распознавания.
    var unusualWeight: Bool
    /// Вес назвали в фунтах и записали в килограммах.
    var convertedFromPounds: Bool
    var language: VoiceLanguage

    var spokenSummary: String {
        var parts: [String] = []
        if startedWorkout {
            parts.append(language == .ru ? "Начал тренировку." : "Started the workout.")
        }

        var line = "\(exerciseName), \(VoicePhrasing.set(weight: weight, reps: reps, in: language))"
        if let effort {
            line += ", \(VoiceWording.effort(effort, in: language))"
        }
        parts.append(line + ".")

        let ordinal = VoicePhrasing.capitalized(VoicePhrasing.ordinal(setNumber, in: language))
        if let plannedSets, setNumber <= plannedSets {
            let total = VoicePhrasing.countWord(plannedSets, in: language)
            parts.append(language == .ru
                ? "\(ordinal) подход из \(total)."
                : "\(ordinal) set of \(total).")
        } else {
            parts.append(language == .ru ? "\(ordinal) подход." : "\(ordinal) set.")
        }

        switch filledFrom {
        case .plan:
            parts.append(language == .ru
                ? "Взял вес и повторы из плана."
                : "Took weight and reps from the plan.")
        case .history:
            parts.append(language == .ru
                ? "Взял вес и повторы как в прошлый раз."
                : "Took weight and reps from last time.")
        case nil:
            break
        }
        if convertedFromPounds {
            parts.append(language == .ru
                ? "Пересчитал из фунтов."
                : "Converted from pounds.")
        }
        if unusualWeight {
            parts.append(language == .ru
                ? "Вес сильно отличается от плана — проверь."
                : "That weight is far off the plan — double-check.")
        }
        if let otherDate {
            let date = VoicePhrasing.date(otherDate, in: language)
            parts.append(language == .ru
                ? "Тренировка за \(date)."
                : "This workout is dated \(date).")
        }
        return parts.joined(separator: " ")
    }
}

struct VoiceUndoOutcome: Equatable {
    var exerciseName: String
    var weight: Double
    var reps: Int
    var remainingSets: Int
    var language: VoiceLanguage

    var spokenSummary: String {
        let set = VoicePhrasing.set(weight: weight, reps: reps, in: language)
        if language == .ru {
            let tail = remainingSets == 0
                ? "Тренировка снова пустая."
                : "Осталось \(VoicePhrasing.sets(remainingSets, in: language))."
            return "Убрал \(exerciseName), \(set). \(tail)"
        }
        let tail = remainingSets == 0
            ? "The workout is empty again."
            : "\(VoicePhrasing.sets(remainingSets, in: language)) left."
        return "Removed \(exerciseName), \(set). \(tail)"
    }
}

struct VoiceNextSet: Equatable {
    var exerciseName: String
    var weight: Double
    var reps: Int
    var setNumber: Int
    var plannedSets: Int?
    /// Сколько упражнений плана ещё не закрыто, включая текущее.
    var remainingExercises: Int
    var fromPlan: Bool
    var language: VoiceLanguage

    var spokenSummary: String {
        var line = "\(exerciseName), \(VoicePhrasing.set(weight: weight, reps: reps, in: language))"
        let ordinal = VoicePhrasing.ordinal(setNumber, in: language)
        if let plannedSets, setNumber <= plannedSets {
            let total = VoicePhrasing.countWord(plannedSets, in: language)
            line += language == .ru
                ? ", \(ordinal) подход из \(total)."
                : ", \(ordinal) set of \(total)."
        } else {
            line += language == .ru ? ", \(ordinal) подход." : ", \(ordinal) set."
        }
        if fromPlan, remainingExercises > 1 {
            let rest = VoicePhrasing.exercises(remainingExercises - 1, in: language)
            line += language == .ru
                ? " Дальше по плану ещё \(rest)."
                : " \(VoicePhrasing.capitalized(rest)) left in the plan after this."
        }
        return line
    }
}

struct VoiceFinishOutcome: Equatable {
    var exercises: Int
    var sets: Int
    var language: VoiceLanguage

    var spokenSummary: String {
        let body = "\(VoicePhrasing.exercises(exercises, in: language)), \(VoicePhrasing.sets(sets, in: language))"
        return language == .ru
            ? "Тренировка сохранена: \(body)."
            : "Workout saved: \(body)."
    }
}

// MARK: - Ошибки

/// Ошибка голосовой команды вместе с языком, на котором её надо произнести.
/// Язык хранится рядом с причиной, а не берётся из глобального состояния:
/// озвучка происходит уже за пределами стора, в интенте.
struct VoiceCommandError: Error, Equatable {
    enum Kind: Equatable {
        case unknownExercise(spoken: String)
        case ambiguousExercise(names: [String])
        case emptyPhrase
        /// В приложении лежит незакрытый черновик за другой день.
        case staleDraft(date: String)
        case nothingToUndo
        case nothingToFinish
        case noPlan
        case saveFailed(reason: String?)
    }

    var kind: Kind
    var language: VoiceLanguage

    init(_ kind: Kind, language: VoiceLanguage) {
        self.kind = kind
        self.language = language
    }

    /// Непонятую фразу Siri переспрашивает, остальное просто произносит.
    var needsRetry: Bool {
        switch kind {
        case .unknownExercise, .ambiguousExercise, .emptyPhrase: true
        default: false
        }
    }

    var spokenMessage: String {
        switch (kind, language) {
        case let (.unknownExercise(spoken), .ru):
            spoken.isEmpty
                ? "Не понял упражнение. Скажи, например: жим ногами 80 на 10."
                : "Не нашёл упражнение «\(spoken)». Скажи, например: жим ногами 80 на 10."
        case let (.unknownExercise(spoken), .en):
            spoken.isEmpty
                ? "I didn't catch the exercise. Try: leg press 80 by 10."
                : "I couldn't find “\(spoken)”. Try: leg press 80 by 10."
        case let (.ambiguousExercise(names), .ru):
            "Уточни: \(names.joined(separator: " или ")). Например: \(names.first ?? "") 80 на 10."
        case let (.ambiguousExercise(names), .en):
            "Which one: \(names.joined(separator: " or "))? For example: \(names.first ?? "") 80 by 10."
        case (.emptyPhrase, .ru):
            "Не расслышал. Скажи, например: жим ногами 80 на 10, тяжело."
        case (.emptyPhrase, .en):
            "I didn't catch that. Try: leg press 80 by 10, hard."
        case let (.staleDraft(date), .ru):
            "В приложении осталась незакрытая тренировка за \(VoicePhrasing.date(date, in: .ru)). Открой Pocket Coach и сохрани её."
        case let (.staleDraft(date), .en):
            "There's an unfinished workout from \(VoicePhrasing.date(date, in: .en)) in the app. Open Pocket Coach and save it first."
        case (.nothingToUndo, .ru):
            "В текущей тренировке ещё нет подходов."
        case (.nothingToUndo, .en):
            "There are no sets in the current workout yet."
        case (.nothingToFinish, .ru):
            "Сохранять пока нечего — в тренировке нет подходов."
        case (.nothingToFinish, .en):
            "Nothing to save yet — the workout has no sets."
        case (.noPlan, .ru):
            "Плана на сегодня нет — скажи упражнение, вес и повторы."
        case (.noPlan, .en):
            "There's no plan for today — say the exercise, weight and reps."
        case let (.saveFailed(reason), .ru):
            "Не удалось сохранить тренировку: \(reason ?? "нет связи с сервером"). Подходы остались в приложении."
        case let (.saveFailed(reason), .en):
            "Couldn't save the workout: \(reason ?? "no connection to the server"). The sets are still in the app."
        }
    }
}

enum VoiceWording {
    static func effort(_ effort: SetEffort, in language: VoiceLanguage) -> String {
        switch (effort, language) {
        case (.easy, .ru): "легко"
        case (.ok, .ru): "норм"
        case (.hard, .ru): "тяжело"
        case (.easy, .en): "easy"
        case (.ok, .en): "okay"
        case (.hard, .en): "hard"
        }
    }
}

// MARK: - Команды

extension TrainerStore {
    /// Верхние границы для голосового ввода: распознавание иногда слышит
    /// «сто восемьдесят» вместо «восемьдесят», и в базу это попасть не должно.
    static let voiceWeightLimit: Double = 500
    static let voiceRepsLimit = 100
    /// Во сколько раз вес должен разойтись с планом, чтобы про это сказать вслух.
    static let voiceWeightSuspicionFactor: Double = 2.5
    /// Насколько старым может быть последний сет, чтобы черновик всё ещё
    /// считался той же сессией (тренировка через полночь — нормально).
    static let voiceDraftFreshnessWindow: TimeInterval = 6 * 3600

    /// Язык последней голосовой команды. Стартует с языка телефона и
    /// переключается на язык произнесённой фразы: команды без фразы («отмени»,
    /// «что дальше») отвечают на том же языке, на котором с ними говорили.
    var voiceLanguage: VoiceLanguage {
        get {
            (voiceDefaults.string(forKey: VoiceKeys.language).flatMap(VoiceLanguage.init(rawValue:)))
                ?? .device
        }
        set { voiceDefaults.set(newValue.rawValue, forKey: VoiceKeys.language) }
    }

    /// Каталог для голосовых команд. Фоновый запуск ради одной фразы не
    /// проходит `boot()`, поэтому справочник берётся из бандла, если сети не было.
    @discardableResult
    func ensureVoiceCatalog() -> [ExerciseDefinition] {
        if exercises.isEmpty {
            exercises = Self.bundledExerciseCatalog
        }
        return exercises
    }

    static let bundledExerciseCatalog: [ExerciseDefinition] = {
        guard let url = Bundle.main.url(forResource: "exercises", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let payload = try? JSONDecoder().decode(ExerciseCatalogResponse.self, from: data) else {
            return []
        }
        return payload.exercises
    }()

    func voiceExerciseName(for exerciseID: Int, in language: VoiceLanguage) -> String? {
        exerciseDefinition(id: exerciseID).map { ExerciseVoiceNames.spoken(for: $0.name, in: language) }
    }

    /// Разбор продиктованной фразы целиком: «жим ногами 80 на 10, тяжело»
    /// или «leg press 80 by 10, hard».
    @discardableResult
    func logVoiceSet(phrase: String) throws -> VoiceLoggedSet {
        let catalog = ensureVoiceCatalog()
        let language = VoiceLanguage.detected(in: phrase, fallback: voiceLanguage)
        voiceLanguage = language

        guard !VoiceText.tokens(phrase).isEmpty else {
            throw VoiceCommandError(.emptyPhrase, language: language)
        }

        let parsed = VoiceSetParser.parse(phrase, catalog: catalog)
        guard let exerciseID = parsed.exerciseID else {
            let candidates = parsed.ambiguousIDs.compactMap { voiceExerciseName(for: $0, in: language) }
            throw candidates.isEmpty
                ? VoiceCommandError(.unknownExercise(spoken: parsed.spokenExercise), language: language)
                : VoiceCommandError(.ambiguousExercise(names: candidates), language: language)
        }

        return try logVoiceSet(
            exerciseID: exerciseID,
            weight: parsed.weight,
            reps: parsed.reps,
            effort: parsed.effort,
            convertedFromPounds: parsed.convertedFromPounds
        )
    }

    /// Записать подход. Не названные вес и повторы берутся оттуда же, откуда их
    /// берёт синяя кнопка «+»: план тренера → прошлое выполнение → дефолт.
    @discardableResult
    func logVoiceSet(
        exerciseID: Int,
        weight: Double?,
        reps: Int?,
        effort: SetEffort?,
        convertedFromPounds: Bool = false
    ) throws -> VoiceLoggedSet {
        ensureVoiceCatalog()
        let language = voiceLanguage
        guard exerciseDefinition(id: exerciseID) != nil else {
            throw VoiceCommandError(.unknownExercise(spoken: ""), language: language)
        }
        if let staleDate = staleVoiceDraftDate {
            throw VoiceCommandError(.staleDraft(date: staleDate), language: language)
        }

        let startedWorkout = !draft.hasRealSets && draft.editingWorkoutID == nil
        if startedWorkout {
            draft.workoutDate = DateTools.localTodayISO()
        }

        let planned = nextPlannedSet(exerciseID: exerciseID)
        let resolvedWeight = min(max(weight ?? planned.weight, 0), Self.voiceWeightLimit)
        let resolvedReps = min(max(reps ?? planned.reps, 1), Self.voiceRepsLimit)

        applySet(
            DraftSet(reps: resolvedReps, weight: resolvedWeight, effort: effort, notes: nil),
            exerciseID: exerciseID,
            setIndex: nil
        )

        let targets = voicePlanTargets(for: exerciseID)
        let performed = draft.exercises.first(where: { $0.exerciseID == exerciseID })?.sets.count ?? 1
        let reference = targets?.first?.weight ?? planned.weight

        return VoiceLoggedSet(
            exerciseName: voiceExerciseName(for: exerciseID, in: language) ?? "Упражнение",
            weight: resolvedWeight,
            reps: resolvedReps,
            effort: effort,
            setNumber: performed,
            plannedSets: targets?.count,
            startedWorkout: startedWorkout,
            filledFrom: (weight == nil && reps == nil) ? (targets == nil ? .history : .plan) : nil,
            otherDate: draft.workoutDate == DateTools.localTodayISO() ? nil : draft.workoutDate,
            unusualWeight: isUnusualVoiceWeight(resolvedWeight, reference: reference),
            convertedFromPounds: convertedFromPounds,
            language: language
        )
    }

    /// «Убери последний подход» — страховка от ложного срабатывания и от того,
    /// что распознавание услышало не то. Целится в последний добавленный сет
    /// независимо от того, каким способом его добавили.
    @discardableResult
    func undoLastVoiceSet() throws -> VoiceUndoOutcome {
        ensureVoiceCatalog()
        let language = voiceLanguage
        let stamped = draft.lastLoggedExerciseID.flatMap { id in
            draft.exercises.first(where: { $0.exerciseID == id && !$0.sets.isEmpty })
        }
        guard let target = stamped ?? draft.exercises.last(where: { !$0.sets.isEmpty }),
              let removed = target.sets.last else {
            throw VoiceCommandError(.nothingToUndo, language: language)
        }

        removeLastSet(exerciseID: target.exerciseID)

        return VoiceUndoOutcome(
            exerciseName: ExerciseVoiceNames.spoken(for: target.exerciseName, in: language),
            weight: removed.weight,
            reps: removed.reps,
            remainingSets: draft.exercises.reduce(0) { $0 + $1.sets.count },
            language: language
        )
    }

    /// «Что дальше?» — читает следующий подход, ничего не записывая.
    /// Без упражнения берёт первое незакрытое упражнение плана.
    func voiceNextSet(exerciseID: Int?) throws -> VoiceNextSet {
        ensureVoiceCatalog()
        let language = voiceLanguage
        let planExercises = draft.editingWorkoutID == nil ? (appliedPlan?.exercises ?? []) : []
        let pending = planExercises.filter { exercise in
            let done = draft.exercises.first(where: { $0.exerciseID == exercise.exerciseID })?.sets.count ?? 0
            return done < exercise.sets.count
        }

        guard let targetID = exerciseID ?? pending.first?.exerciseID else {
            throw VoiceCommandError(.noPlan, language: language)
        }
        guard let name = voiceExerciseName(for: targetID, in: language) else {
            throw VoiceCommandError(.unknownExercise(spoken: ""), language: language)
        }

        let planned = nextPlannedSet(exerciseID: targetID)
        let done = draft.exercises.first(where: { $0.exerciseID == targetID })?.sets.count ?? 0
        let targets = voicePlanTargets(for: targetID)

        return VoiceNextSet(
            exerciseName: name,
            weight: planned.weight,
            reps: planned.reps,
            setNumber: done + 1,
            plannedSets: targets?.count,
            remainingExercises: pending.count,
            fromPlan: targets != nil,
            language: language
        )
    }

    /// «Заверши тренировку» — отправка черновика на backend. Сессионная cookie
    /// может не пережить фоновый перезапуск процесса, поэтому сессия
    /// переоткрывается перед сохранением.
    func finishVoiceWorkout() async throws -> VoiceFinishOutcome {
        let language = voiceLanguage
        guard draft.hasRealSets else {
            throw VoiceCommandError(.nothingToFinish, language: language)
        }
        let sets = draft.exercises.reduce(0) { $0 + $1.sets.count }
        let exerciseCount = draft.exercises.filter { !$0.sets.isEmpty }.count

        _ = try? await APIClient(baseURLString: apiBaseURLString).resolveSession()
        guard await saveDraftWorkout() else {
            throw VoiceCommandError(.saveFailed(reason: toast), language: language)
        }
        return VoiceFinishOutcome(exercises: exerciseCount, sets: sets, language: language)
    }

    /// Незакрытый черновик за прошлый день: дописывать в него сегодняшний
    /// подход нельзя (данные уедут в чужую дату), молча стирать — тем более.
    var staleVoiceDraftDate: String? {
        guard draft.editingWorkoutID == nil,
              draft.hasRealSets,
              draft.workoutDate != DateTools.localTodayISO() else {
            return nil
        }
        guard let lastLoggedAt = draft.lastLoggedAt else {
            return draft.workoutDate
        }
        let age = Date().timeIntervalSince1970 - lastLoggedAt
        return age > Self.voiceDraftFreshnessWindow ? draft.workoutDate : nil
    }

    private func voicePlanTargets(for exerciseID: Int) -> [RecommendedSet]? {
        guard draft.editingWorkoutID == nil else { return nil }
        return appliedPlan?.targets(for: exerciseID)
    }

    private func isUnusualVoiceWeight(_ weight: Double, reference: Double) -> Bool {
        guard reference > 0, weight > 0 else { return false }
        let ratio = weight / reference
        return ratio >= Self.voiceWeightSuspicionFactor || ratio <= 1 / Self.voiceWeightSuspicionFactor
    }
}

private enum VoiceKeys {
    static let language = "trainer-ios-voice-language-v1"
}
