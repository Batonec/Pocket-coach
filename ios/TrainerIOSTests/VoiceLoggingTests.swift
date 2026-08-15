import XCTest
@testable import TrainerIOS

@MainActor
final class VoiceLoggingTests: XCTestCase {
    private func makeStore(
        withPlan plan: Bool = false,
        language: VoiceLanguage = .ru
    ) -> TrainerStore {
        let store = TrainerStore(defaults: .isolatedTestDefaults())
        store.exercises = TestFixtures.catalog
        // Язык хоста тестов не должен решать, на каком языке отвечает стор.
        store.voiceLanguage = language
        if plan {
            store.appliedPlan = AppliedCoachPlan(
                basedOnWorkoutID: 1,
                basedOnWorkoutCount: 5,
                model: "test",
                generatedAt: 100,
                appliedAt: DateTools.localTodayISO(),
                focus: "Ноги",
                loadType: "heavy",
                exercises: [
                    RecommendedExercise(
                        exerciseID: 8,
                        name: "Жим ногами",
                        note: nil,
                        sets: Array(repeating: RecommendedSet(reps: 10, weight: 80), count: 4)
                    ),
                    RecommendedExercise(
                        exerciseID: 16,
                        name: "Разгибания ног",
                        note: nil,
                        sets: Array(repeating: RecommendedSet(reps: 12, weight: 45), count: 3)
                    )
                ]
            )
        }
        return store
    }

    // MARK: - Запись подхода

    func testPhraseStartsWorkoutAndLandsInDraft() throws {
        let store = makeStore()

        let logged = try store.logVoiceSet(phrase: "жим ногами 80 на 10 тяжело")

        XCTAssertTrue(logged.startedWorkout)
        XCTAssertEqual(logged.setNumber, 1)
        XCTAssertEqual(store.draft.workoutDate, DateTools.localTodayISO())
        XCTAssertEqual(store.draft.exercises.count, 1)
        XCTAssertEqual(store.draft.exercises[0].exerciseID, 8)
        XCTAssertEqual(store.draft.exercises[0].sets, [
            DraftSet(reps: 10, weight: 80, effort: .hard, notes: nil)
        ])
    }

    func testSecondSetContinuesTheSameWorkout() throws {
        let store = makeStore()

        _ = try store.logVoiceSet(phrase: "жим ногами 80 на 10")
        let second = try store.logVoiceSet(phrase: "жим ногами 80 на 9 тяжело")

        XCTAssertFalse(second.startedWorkout)
        XCTAssertEqual(second.setNumber, 2)
        XCTAssertEqual(store.draft.exercises[0].sets.count, 2)
    }

    func testMissingWeightAndRepsComeFromTheAppliedPlan() throws {
        let store = makeStore(withPlan: true)

        let logged = try store.logVoiceSet(exerciseID: 8, weight: nil, reps: nil, effort: nil)

        XCTAssertEqual(logged.weight, 80)
        XCTAssertEqual(logged.reps, 10)
        XCTAssertEqual(logged.plannedSets, 4)
        XCTAssertEqual(logged.filledFrom, .plan)
        XCTAssertTrue(logged.spokenSummary.contains("из плана"))
    }

    func testWithoutPlanTheSubstitutionIsAnnouncedAsHistory() throws {
        let store = makeStore()
        _ = try store.logVoiceSet(exerciseID: 11, weight: 20, reps: 12, effort: nil)

        let logged = try store.logVoiceSet(exerciseID: 11, weight: nil, reps: nil, effort: nil)

        XCTAssertEqual(logged.filledFrom, .history)
        XCTAssertTrue(logged.spokenSummary.contains("как в прошлый раз"))
    }

    func testSpokenValuesOverrideThePlan() throws {
        let store = makeStore(withPlan: true)

        let logged = try store.logVoiceSet(exerciseID: 8, weight: 85, reps: 8, effort: .hard)

        XCTAssertEqual(logged.weight, 85)
        XCTAssertEqual(logged.reps, 8)
        XCTAssertNil(logged.filledFrom)
        XCTAssertFalse(logged.unusualWeight)
    }

    func testWildlyDifferentWeightIsFlaggedForTheUser() throws {
        let store = makeStore(withPlan: true)

        let logged = try store.logVoiceSet(exerciseID: 8, weight: 800, reps: 10, effort: nil)

        XCTAssertTrue(logged.unusualWeight)
        XCTAssertTrue(logged.spokenSummary.contains("проверь"))
    }

    func testValuesAreClampedToSaneBounds() throws {
        let store = makeStore()

        let logged = try store.logVoiceSet(exerciseID: 8, weight: 9_000, reps: 900, effort: nil)

        XCTAssertEqual(logged.weight, TrainerStore.voiceWeightLimit)
        XCTAssertEqual(logged.reps, TrainerStore.voiceRepsLimit)
    }

    func testUnknownExerciseIsRejectedWithTheSpokenFragment() {
        let store = makeStore()

        XCTAssertThrowsError(try store.logVoiceSet(phrase: "становая тяга штанги 100 на 5")) { error in
            guard case let .unknownExercise(spoken) = (error as? VoiceCommandError)?.kind else {
                return XCTFail("Ожидалась unknownExercise, получено \(error)")
            }
            XCTAssertTrue(spoken.contains("становая"))
        }
        XCTAssertFalse(store.draft.hasRealSets, "Непонятая фраза не должна начинать тренировку")
    }

    func testAmbiguousExerciseAsksInsteadOfGuessing() {
        let store = makeStore()

        XCTAssertThrowsError(try store.logVoiceSet(phrase: "тяга 55 на 12")) { error in
            guard case let .ambiguousExercise(names) = (error as? VoiceCommandError)?.kind else {
                return XCTFail("Ожидалась ambiguousExercise, получено \(error)")
            }
            XCTAssertEqual(Set(names), ["Тяга вертикальная", "Тяга горизонтальная"])
        }
        XCTAssertFalse(store.draft.hasRealSets)
    }

    func testEmptyPhraseIsRejected() {
        let store = makeStore()

        XCTAssertThrowsError(try store.logVoiceSet(phrase: "  ")) { error in
            XCTAssertEqual((error as? VoiceCommandError)?.kind, .emptyPhrase)
        }
    }

    // MARK: - Забытый черновик

    func testForgottenDraftFromAnotherDayBlocksVoiceLogging() throws {
        let store = makeStore()
        store.draft = DraftWorkout(
            workoutDate: "2026-08-01",
            exercises: [DraftExercise(
                exerciseID: 8,
                exerciseName: "Жим ногами",
                sets: [DraftSet(reps: 10, weight: 80, effort: nil, notes: nil)]
            )],
            editingWorkoutID: nil,
            editingClientID: nil,
            lastLoggedExerciseID: 8,
            lastLoggedAt: Date().timeIntervalSince1970 - 48 * 3600
        )

        XCTAssertThrowsError(try store.logVoiceSet(phrase: "жим ногами 80 на 10")) { error in
            XCTAssertEqual((error as? VoiceCommandError)?.kind, .staleDraft(date: "2026-08-01"))
        }
        XCTAssertEqual(store.draft.exercises[0].sets.count, 1, "Чужой день не должен пополняться")
    }

    func testSessionCrossingMidnightKeepsWorkingOnTheSameDraft() throws {
        let store = makeStore()
        let yesterday = DateTools.iso(from: Date().addingTimeInterval(-24 * 3600))
        store.draft = DraftWorkout(
            workoutDate: yesterday,
            exercises: [DraftExercise(
                exerciseID: 8,
                exerciseName: "Жим ногами",
                sets: [DraftSet(reps: 10, weight: 80, effort: nil, notes: nil)]
            )],
            editingWorkoutID: nil,
            editingClientID: nil,
            lastLoggedExerciseID: 8,
            lastLoggedAt: Date().timeIntervalSince1970 - 600
        )

        let logged = try store.logVoiceSet(phrase: "жим ногами 80 на 10")

        XCTAssertEqual(logged.setNumber, 2)
        XCTAssertEqual(store.draft.workoutDate, yesterday)
        XCTAssertEqual(logged.otherDate, yesterday)
    }

    // MARK: - Отмена

    func testUndoRemovesTheLastLoggedSetAcrossExercises() throws {
        let store = makeStore()
        _ = try store.logVoiceSet(phrase: "жим ногами 80 на 10")
        _ = try store.logVoiceSet(phrase: "бицепс 20 на 12")
        _ = try store.logVoiceSet(phrase: "жим ногами 80 на 9")

        let undone = try store.undoLastVoiceSet()

        XCTAssertEqual(undone.exerciseName, "Жим ногами")
        XCTAssertEqual(undone.reps, 9)
        XCTAssertEqual(undone.remainingSets, 2)
        XCTAssertEqual(store.draft.exercises.first(where: { $0.exerciseID == 8 })?.sets.count, 1)
    }

    func testUndoDropsAnExerciseThatRunsOutOfSets() throws {
        let store = makeStore()
        _ = try store.logVoiceSet(phrase: "бицепс 20 на 12")

        let undone = try store.undoLastVoiceSet()

        XCTAssertEqual(undone.remainingSets, 0)
        XCTAssertFalse(store.draft.hasAnyExercise)
    }

    func testUndoOnEmptyWorkoutIsRejected() {
        let store = makeStore()

        XCTAssertThrowsError(try store.undoLastVoiceSet()) { error in
            XCTAssertEqual((error as? VoiceCommandError)?.kind, .nothingToUndo)
        }
    }

    // MARK: - Что дальше

    func testNextSetReadsThePlanWithoutLogging() throws {
        let store = makeStore(withPlan: true)
        _ = try store.logVoiceSet(exerciseID: 8, weight: nil, reps: nil, effort: nil)

        let next = try store.voiceNextSet(exerciseID: nil)

        XCTAssertEqual(next.exerciseName, "Жим ногами")
        XCTAssertEqual(next.setNumber, 2)
        XCTAssertEqual(next.plannedSets, 4)
        XCTAssertEqual(next.remainingExercises, 2)
        XCTAssertEqual(store.draft.exercises[0].sets.count, 1, "Вопрос не должен ничего записывать")
    }

    func testNextSetMovesToTheFollowingExerciseWhenOneIsDone() throws {
        let store = makeStore(withPlan: true)
        for _ in 0..<4 {
            _ = try store.logVoiceSet(exerciseID: 8, weight: nil, reps: nil, effort: nil)
        }

        let next = try store.voiceNextSet(exerciseID: nil)

        XCTAssertEqual(next.exerciseName, "Разгибания ног")
        XCTAssertEqual(next.setNumber, 1)
        XCTAssertEqual(next.remainingExercises, 1)
    }

    func testNextSetWithoutPlanIsRejected() {
        let store = makeStore()

        XCTAssertThrowsError(try store.voiceNextSet(exerciseID: nil)) { error in
            XCTAssertEqual((error as? VoiceCommandError)?.kind, .noPlan)
        }
    }

    // MARK: - Ответы Siri

    func testSpokenSummaryReadsBackWhatWasRecorded() throws {
        let store = makeStore(withPlan: true)

        let logged = try store.logVoiceSet(exerciseID: 8, weight: 80, reps: 10, effort: .hard)

        XCTAssertEqual(
            logged.spokenSummary,
            "Начал тренировку. Жим ногами, 80 на 10, тяжело. Первый подход из четырех."
        )
    }

    func testFinishWithoutSetsIsRejected() async {
        let store = makeStore()

        do {
            _ = try await store.finishVoiceWorkout()
            XCTFail("Пустую тренировку сохранять нечего")
        } catch {
            XCTAssertEqual((error as? VoiceCommandError)?.kind, .nothingToFinish)
        }
    }

    // MARK: - Английский язык

    func testEnglishPhraseAnswersInEnglish() throws {
        let store = makeStore(withPlan: true, language: .en)

        let logged = try store.logVoiceSet(phrase: "leg press 80 by 10, hard")

        XCTAssertEqual(logged.language, .en)
        XCTAssertEqual(store.draft.exercises[0].exerciseID, 8)
        XCTAssertEqual(
            logged.spokenSummary,
            "Started the workout. Leg press, 80 by 10, hard. First set of four."
        )
    }

    func testEnglishPhraseSwitchesTheAnswerLanguageOnARussianDevice() throws {
        let store = makeStore(language: .ru)

        let logged = try store.logVoiceSet(phrase: "biceps curl 20 by 12")

        XCTAssertEqual(logged.language, .en, "Язык фразы важнее прошлой настройки")
        XCTAssertEqual(store.voiceLanguage, .en)
        XCTAssertTrue(logged.spokenSummary.hasPrefix("Started the workout. Biceps curl, 20 by 12."))
    }

    func testCommandsWithoutAPhraseKeepTheLastSpokenLanguage() throws {
        let store = makeStore(language: .ru)
        _ = try store.logVoiceSet(phrase: "leg press 80 by 10")

        let undone = try store.undoLastVoiceSet()

        XCTAssertEqual(undone.language, .en)
        XCTAssertEqual(undone.spokenSummary, "Removed Leg press, 80 by 10. The workout is empty again.")
    }

    func testEnglishErrorsAreSpokenInEnglish() {
        let store = makeStore(language: .en)

        XCTAssertThrowsError(try store.logVoiceSet(phrase: "deadlift 100 by 5")) { error in
            let voiceError = error as? VoiceCommandError
            XCTAssertEqual(voiceError?.language, .en)
            XCTAssertTrue(voiceError?.spokenMessage.contains("Try: leg press 80 by 10") == true)
        }
    }

    func testEnglishNextSetReadsThePlan() throws {
        let store = makeStore(withPlan: true, language: .en)
        _ = try store.logVoiceSet(phrase: "leg press 80 by 10")

        let next = try store.voiceNextSet(exerciseID: nil)

        XCTAssertEqual(
            next.spokenSummary,
            "Leg press, 80 by 10, second set of four. 1 exercise left in the plan after this."
        )
    }
}
