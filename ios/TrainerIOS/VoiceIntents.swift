import AppIntents
import Foundation

/// Siri поверх тренировки: телефон лежит в кармане, наушники в ушах, подход
/// диктуется голосом.
///
/// Интенты объявлены в таргете приложения, поэтому выполняются в его процессе
/// (система поднимает его в фоне, если приложение закрыто) и работают с тем же
/// `TrainerStore.shared`, что и UI. `openAppWhenRun = false` — экран не
/// разблокируется и приложение не выходит на передний план.
///
/// Вся продуктовая логика — в `TrainerStoreVoice.swift`; здесь только
/// объявление команд, фраз и озвучка результата.

// MARK: - Озвучка ошибок

extension VoiceCommandError: CustomLocalizedStringResourceConvertible {
    var localizedStringResource: LocalizedStringResource {
        LocalizedStringResource(stringLiteral: spokenMessage)
    }
}

private func voiceDialog(_ text: String) -> IntentDialog {
    IntentDialog(stringLiteral: text)
}

// MARK: - Сущности

struct ExerciseEntity: AppEntity {
    static var typeDisplayRepresentation = TypeDisplayRepresentation(name: "Упражнение")
    static var defaultQuery = ExerciseEntityQuery()

    var id: Int
    /// Как это называют вслух («Жим горизонтальный»), а не как в каталоге.
    var spokenName: String
    /// Название из каталога — по нему считается совпадение и синонимы.
    var catalogName: String

    /// На каком языке Siri слышит это упражнение.
    var language: VoiceLanguage

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(
            title: "\(spokenName)",
            synonyms: ExerciseVoiceMatcher.spokenSynonyms(for: catalogName, in: language)
                .map { LocalizedStringResource(stringLiteral: $0) }
        )
    }
}

struct ExerciseEntityQuery: EntityStringQuery {
    func entities(for identifiers: [Int]) async throws -> [ExerciseEntity] {
        let catalog = await voiceCatalog()
        return identifiers.compactMap { id in catalog.first { $0.id == id } }
    }

    /// Siri и Shortcuts ищут упражнение по произнесённой строке — отдаём им тот
    /// же матчер, что разбирает свободную фразу.
    func entities(matching string: String) async throws -> [ExerciseEntity] {
        let catalog = await voiceCatalog()
        let tokens = VoiceText.tokens(string)
        guard !tokens.isEmpty else { return catalog }

        let ranked = catalog
            .map { (entity: $0, score: ExerciseVoiceMatcher.score(tokens: tokens, for: $0.catalogName)) }
            .filter { $0.score >= ExerciseVoiceMatcher.acceptThreshold }
            .sorted { $0.score > $1.score }
        return ranked.isEmpty ? catalog : ranked.map(\.entity)
    }

    func suggestedEntities() async throws -> [ExerciseEntity] {
        await voiceCatalog()
    }

    private func voiceCatalog() async -> [ExerciseEntity] {
        await MainActor.run {
            let store = TrainerStore.shared
            let language = store.voiceLanguage
            return store.ensureVoiceCatalog().map {
                ExerciseEntity(
                    id: $0.id,
                    spokenName: ExerciseVoiceNames.spoken(for: $0.name, in: language),
                    catalogName: $0.name,
                    language: language
                )
            }
        }
    }
}

enum SetEffortEntity: String, AppEnum {
    case easy
    case ok
    case hard

    static var typeDisplayRepresentation = TypeDisplayRepresentation(name: "Как далось")

    static var caseDisplayRepresentations: [SetEffortEntity: DisplayRepresentation] = [
        .easy: DisplayRepresentation(title: "Легко", synonyms: ["легко", "просто", "изи", "easy", "light"]),
        .ok: DisplayRepresentation(title: "Норм", synonyms: ["нормально", "норм", "средне", "okay", "normal"]),
        .hard: DisplayRepresentation(title: "Тяжело", synonyms: ["тяжело", "тяжко", "на пределе", "hard", "heavy"])
    ]

    var effort: SetEffort { SetEffort(rawValue: rawValue) ?? .ok }
}

// MARK: - Записать подход одной фразой

struct LogSetByPhraseIntent: AppIntent {
    static var title: LocalizedStringResource = "Записать подход"
    static var description = IntentDescription(
        "Разбирает фразу вида «жим ногами 80 на 10, тяжело» и добавляет подход в текущую тренировку. Если тренировка ещё не начата — начинает её.",
        categoryName: "Тренировка",
        searchKeywords: ["подход", "сет", "тренировка", "жим", "тяга"]
    )
    static var openAppWhenRun = false
    /// Смысл фичи в том, что телефон заблокирован и лежит в кармане.
    static var authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @Parameter(
        title: "Подход",
        description: "Упражнение, вес и повторы одной фразой",
        requestValueDialog: "Что записать? Например: жим ногами 80 на 10"
    )
    var phrase: String

    static var parameterSummary: some ParameterSummary {
        Summary("Записать подход: \(\.$phrase)")
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        do {
            let logged = try TrainerStore.shared.logVoiceSet(phrase: phrase)
            return .result(dialog: voiceDialog(logged.spokenSummary))
        } catch let error as VoiceCommandError where error.needsRetry {
            // Не понял именно фразу — переспрашиваем её же, а не падаем.
            throw $phrase.needsValueError(voiceDialog(error.spokenMessage))
        }
    }
}

// MARK: - Засчитать подход по плану

struct LogPlannedSetIntent: AppIntent {
    static var title: LocalizedStringResource = "Засчитать подход по плану"
    static var description = IntentDescription(
        "Добавляет подход в названное упражнение. Вес и повторы, если их не назвать, берутся из плана тренера или из прошлого выполнения.",
        categoryName: "Тренировка"
    )
    static var openAppWhenRun = false
    static var authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @Parameter(title: "Упражнение")
    var exercise: ExerciseEntity

    @Parameter(title: "Вес, кг")
    var weight: Double?

    @Parameter(title: "Повторы")
    var reps: Int?

    @Parameter(title: "Как далось")
    var effort: SetEffortEntity?

    static var parameterSummary: some ParameterSummary {
        Summary("Записать подход: \(\.$exercise)") {
            \.$weight
            \.$reps
            \.$effort
        }
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let logged = try TrainerStore.shared.logVoiceSet(
            exerciseID: exercise.id,
            weight: weight,
            reps: reps,
            effort: effort?.effort
        )
        return .result(dialog: voiceDialog(logged.spokenSummary))
    }
}

// MARK: - Отмена

struct UndoLastSetIntent: AppIntent {
    static var title: LocalizedStringResource = "Убрать последний подход"
    static var description = IntentDescription(
        "Удаляет последний добавленный подход из текущей тренировки.",
        categoryName: "Тренировка"
    )
    static var openAppWhenRun = false
    static var authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let undone = try TrainerStore.shared.undoLastVoiceSet()
        return .result(dialog: voiceDialog(undone.spokenSummary))
    }
}

// MARK: - Что дальше

struct NextSetIntent: AppIntent {
    static var title: LocalizedStringResource = "Следующий подход"
    static var description = IntentDescription(
        "Проговаривает следующий подход по плану, ничего не записывая.",
        categoryName: "Тренировка"
    )
    static var openAppWhenRun = false
    static var authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @Parameter(title: "Упражнение")
    var exercise: ExerciseEntity?

    static var parameterSummary: some ParameterSummary {
        Summary("Следующий подход: \(\.$exercise)")
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let next = try TrainerStore.shared.voiceNextSet(exerciseID: exercise?.id)
        return .result(dialog: voiceDialog(next.spokenSummary))
    }
}

// MARK: - Завершить тренировку

struct FinishWorkoutIntent: AppIntent {
    static var title: LocalizedStringResource = "Завершить тренировку"
    static var description = IntentDescription(
        "Сохраняет текущую тренировку на сервер — так же, как кнопка «Сохранить» в приложении.",
        categoryName: "Тренировка"
    )
    static var openAppWhenRun = false
    static var authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let store = TrainerStore.shared
        store.ensureVoiceCatalog()
        let language = store.voiceLanguage
        guard store.draft.hasRealSets else {
            throw VoiceCommandError(.nothingToFinish, language: language)
        }

        // Единственная команда, которая уходит на сервер и «съедает» план дня,
        // поэтому она подтверждается вслух. requestConfirmation с диалогом
        // появился в iOS 18; на 17 команда выполняется сразу.
        let sets = store.draft.exercises.reduce(0) { $0 + $1.sets.count }
        let exercises = store.draft.exercises.filter { !$0.sets.isEmpty }.count
        if #available(iOS 18.0, *) {
            let body = "\(VoicePhrasing.exercises(exercises, in: language)), \(VoicePhrasing.sets(sets, in: language))"
            try await requestConfirmation(
                dialog: voiceDialog(language == .ru
                    ? "Сохранить тренировку: \(body)?"
                    : "Save the workout: \(body)?")
            )
        }

        let outcome = try await store.finishVoiceWorkout()
        return .result(dialog: voiceDialog(outcome.spokenSummary))
    }
}

// MARK: - Фразы для Siri

/// Каждая фраза обязана содержать `\(.applicationName)` — фраза без имени
/// приложения молча выбрасывается из NLU-модели при сборке. Обойти это нельзя,
/// но имя выбираем мы: короткое «Зал» (`INAlternativeAppNames` в Info.plist)
/// превращает обязательное имя в кодовое слово.
///
/// Отсюда два регистра фраз у каждой команды: длинный человеческий («Записать
/// подход в Покет Коуч») и телеграфный для зала («Зал подход», «Зал жим
/// ногами»). Второй — рабочий: одна короткая фраза, и подход засчитан.
struct TrainerAppShortcuts: AppShortcutsProvider {
    static var shortcutTileColor: ShortcutTileColor { .orange }

    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: LogSetByPhraseIntent(),
            phrases: [
                "Записать подход в \(.applicationName)",
                "Добавь подход в \(.applicationName)",
                "Новый подход в \(.applicationName)",
                "Запиши подход в \(.applicationName)",
                "\(.applicationName) подход"
            ],
            shortTitle: "Записать подход",
            systemImageName: "plus.circle.fill"
        )
        AppShortcut(
            intent: LogPlannedSetIntent(),
            phrases: [
                // Главная рабочая фраза: кодовое слово плюс упражнение, больше
                // ничего. Вес и повторы приезжают из плана тренера.
                "\(.applicationName) \(\.$exercise)",
                "Засчитай \(\.$exercise) в \(.applicationName)",
                "Сделал \(\.$exercise) в \(.applicationName)",
                "Закрой \(\.$exercise) в \(.applicationName)"
            ],
            shortTitle: "Засчитать по плану",
            systemImageName: "checkmark.circle.fill"
        )
        AppShortcut(
            intent: UndoLastSetIntent(),
            phrases: [
                "Убери последний подход в \(.applicationName)",
                "Отмени последний подход в \(.applicationName)",
                "Удали последний подход в \(.applicationName)",
                "\(.applicationName) отмена"
            ],
            shortTitle: "Убрать подход",
            systemImageName: "arrow.uturn.backward"
        )
        AppShortcut(
            intent: NextSetIntent(),
            phrases: [
                "Что дальше в \(.applicationName)",
                "Следующий подход в \(.applicationName)",
                "Какой следующий подход в \(.applicationName)",
                "\(.applicationName) дальше"
            ],
            shortTitle: "Следующий подход",
            systemImageName: "list.bullet.rectangle"
        )
        AppShortcut(
            intent: FinishWorkoutIntent(),
            phrases: [
                "Заверши тренировку в \(.applicationName)",
                "Сохрани тренировку в \(.applicationName)",
                "Закончил тренировку в \(.applicationName)",
                "\(.applicationName) финиш"
            ],
            shortTitle: "Завершить тренировку",
            systemImageName: "checkmark.seal.fill"
        )
    }
}
