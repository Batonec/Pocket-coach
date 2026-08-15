import XCTest
import SwiftUI
import UIKit
@testable import TrainerIOS

@MainActor
final class TrainerStoreTests: XCTestCase {
    func testMeasurementFieldBecomesFirstResponderWhenAttachedToWindow() async {
        let field = ImmediateDecimalTextField(frame: CGRect(x: 20, y: 20, width: 200, height: 56))
        let controller = UIViewController()
        controller.view.addSubview(field)
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = controller
        let beganEditing = XCTNSNotificationExpectation(
            name: UITextField.textDidBeginEditingNotification,
            object: field
        )

        window.makeKeyAndVisible()
        await fulfillment(of: [beganEditing], timeout: 1)

        XCTAssertTrue(field.isFirstResponder)
        field.resignFirstResponder()
        window.isHidden = true
    }

    func testMeasurementInputBuffersFirstKeyBeforeRelayingItToSwiftUI() async {
        var relayedText = ""
        let didRelay = expectation(description: "Debounced text reached SwiftUI")
        let buffer = DecimalDraftBuffer(value: "")
        let input = ImmediateDecimalInput(
            text: Binding(
                get: { relayedText },
                set: {
                    relayedText = $0
                    didRelay.fulfill()
                }
            ),
            buffer: buffer,
            accessibilityLabel: "Вес"
        )
        let coordinator = input.makeCoordinator()
        let field = UITextField()
        field.text = "8"

        coordinator.valueChanged(field)

        XCTAssertEqual(buffer.value, "8")
        XCTAssertEqual(relayedText, "", "The key must not synchronously rebuild SwiftUI")
        await fulfillment(of: [didRelay], timeout: 1)
        XCTAssertEqual(relayedText, "8")
    }

    func testStoreDefaultsMatchREADMEInitialState() {
        let store = TrainerStore(defaults: .isolatedTestDefaults())

        XCTAssertEqual(store.currentTab, .trainings)
        XCTAssertEqual(store.selectedRange, .all)
        XCTAssertEqual(store.selectedBodyWeightRange, .days30)
        XCTAssertEqual(store.apiBaseURLString, "https://trainer.superbatonec.org")
        XCTAssertFalse(store.draft.hasAnyExercise)
        XCTAssertFalse(store.isWorkoutBuilderPresented)
    }

    private func readyRecommendation() -> RecommendationResponse {
        RecommendationResponse(
            ok: true,
            status: "ready",
            stale: false,
            basedOnWorkoutID: 133,
            basedOnWorkoutCount: 10,
            model: "claude-opus-4-8",
            updatedAt: 1781200000,
            error: nil,
            recommendation: RecommendationPayload(
                focus: "Верх+низ",
                loadType: "medium",
                rationale: "...",
                exercises: [
                    RecommendedExercise(
                        exerciseID: 8, name: "Жим ногами", note: "n",
                        sets: [RecommendedSet(reps: 12, weight: 90), RecommendedSet(reps: 10, weight: 95)]
                    ),
                    RecommendedExercise(
                        exerciseID: 9, name: "Тяга верт.", note: nil,
                        sets: [RecommendedSet(reps: 12, weight: 70)]
                    ),
                    RecommendedExercise(
                        exerciseID: 999, name: "Выдумка", note: nil,
                        sets: [RecommendedSet(reps: 5, weight: 5)]
                    )
                ]
            )
        )
    }

    func testApplyRecommendationAsPlanDoesNotStartWorkoutAndFiltersCatalog() {
        let store = TrainerStore(defaults: .isolatedTestDefaults())
        store.exercises = TestFixtures.catalog
        store.recommendation = readyRecommendation()

        store.applyRecommendationAsPlan()

        // Plan captured (unknown id 999 dropped), but the draft is untouched:
        // applying a plan must not look like a started workout.
        XCTAssertEqual(store.appliedPlan?.exercises.map(\.exerciseID), [8, 9])
        XCTAssertEqual(store.appliedPlan?.generatedAt, 1781200000)
        XCTAssertTrue(store.draft.exercises.isEmpty)
        XCTAssertFalse(store.draft.hasRealSets)
        XCTAssertTrue(store.isRecommendationApplied)

        // Display cards follow the recommended order as previews.
        let cards = store.displayCards()
        XCTAssertEqual(cards.map(\.exerciseID), [8, 9])
        XCTAssertTrue(cards.allSatisfy(\.isPreview))
    }

    func testAutoApplyAppliesReadyRecommendationAndIsIdempotent() {
        let store = TrainerStore(defaults: .isolatedTestDefaults())
        store.exercises = TestFixtures.catalog
        store.recommendation = readyRecommendation()
        XCTAssertNil(store.appliedPlan)

        store.autoApplyRecommendationIfReady()
        XCTAssertEqual(store.appliedPlan?.exercises.map(\.exerciseID), [8, 9])
        XCTAssertTrue(store.isRecommendationApplied)

        // Re-running (e.g. another cached load of the same rec) changes nothing.
        store.autoApplyRecommendationIfReady()
        XCTAssertEqual(store.appliedPlan?.exercises.map(\.exerciseID), [8, 9])
    }

    func testAutoApplySkipsWhileEditingPastWorkout() {
        let store = TrainerStore(defaults: .isolatedTestDefaults())
        store.exercises = TestFixtures.catalog
        store.startEditing(TestFixtures.workout(
            id: 42,
            exercises: [TestFixtures.exercise(id: 8, name: "Жим ногами", sets: [TestFixtures.set()])]
        ))
        store.recommendation = readyRecommendation()

        store.autoApplyRecommendationIfReady()
        XCTAssertNil(store.appliedPlan)
    }

    func testAutoApplyReplacesAppliedPlanBeforeWorkoutStarts() {
        let store = TrainerStore(defaults: .isolatedTestDefaults())
        store.exercises = TestFixtures.catalog
        store.recommendation = readyRecommendation()
        store.applyRecommendationAsPlan()

        var refreshed = readyRecommendation()
        refreshed.recommendation?.focus = "Новый план по свежим замерам"
        refreshed.recommendation?.exercises[0].sets[0].reps = 8
        store.recommendation = refreshed

        store.autoApplyRecommendationIfReady()

        XCTAssertEqual(store.appliedPlan?.focus, "Новый план по свежим замерам")
        XCTAssertEqual(store.appliedPlan?.exercises[0].sets[0].reps, 8)
        XCTAssertFalse(store.draft.hasRealSets)
    }

    func testAutoApplyDoesNotReplacePlanAfterWorkoutHasStarted() {
        let store = TrainerStore(defaults: .isolatedTestDefaults())
        store.exercises = TestFixtures.catalog
        store.recommendation = readyRecommendation()
        store.applyRecommendationAsPlan()
        store.addPlannedSet(exerciseID: 8)

        var refreshed = readyRecommendation()
        refreshed.recommendation?.focus = "Новый план по свежим замерам"
        refreshed.recommendation?.exercises[0].sets[0].reps = 8
        store.recommendation = refreshed

        store.autoApplyRecommendationIfReady()

        XCTAssertEqual(store.appliedPlan?.focus, "Верх+низ")
        XCTAssertEqual(store.appliedPlan?.exercises[0].sets[0].reps, 12)
        XCTAssertTrue(store.draft.hasRealSets)
    }

    func testTodayHidesRefreshingPlanOnlyUntilWorkoutStarts() {
        let store = TrainerStore(defaults: .isolatedTestDefaults())
        store.exercises = TestFixtures.catalog
        store.recommendation = readyRecommendation()
        store.applyRecommendationAsPlan()

        store.isRefreshingRecommendation = true
        XCTAssertTrue(store.isTodayPlanUnavailable)

        store.addPlannedSet(exerciseID: 8)
        XCTAssertFalse(store.isTodayPlanUnavailable)
    }

    func testTodayHidesPendingAndFailedPlansWithoutLocalSpinner() {
        let store = TrainerStore(defaults: .isolatedTestDefaults())
        var pending = readyRecommendation()
        pending.status = "pending"
        store.recommendation = pending

        XCTAssertTrue(store.isTodayPlanUnavailable)

        var failed = readyRecommendation()
        failed.status = "failed"
        failed.error = "Модель нарушила ограничения"
        store.recommendation = failed

        XCTAssertTrue(store.isTodayPlanUnavailable)
    }

    func testFreshMeasurementOptimisticallyRemovesMeasurementSignalsOnly() {
        let store = TrainerStore(defaults: .isolatedTestDefaults())
        store.coachSignals = [
            CoachSignal(
                signalID: "measurements_overdue",
                instanceKey: "measurements_overdue:waist=none",
                severity: "warn",
                title: "Советы по калориям на паузе",
                body: "Внеси талию — вернутся",
                note: nil,
                glyph: "nutrition",
                action: CoachSignalAction(
                    type: "open_measurements", label: "Замеры", target: "waist"
                ),
                snoozable: true
            ),
            CoachSignal(
                signalID: "return_soon",
                instanceKey: "return_soon:last_workout=2026-08-02",
                severity: "warn",
                title: "Потренируйся",
                body: "Возвратный режим близко",
                note: nil,
                glyph: "back",
                action: CoachSignalAction(
                    type: "open_next_workout", label: "План", target: nil
                ),
                snoozable: true
            ),
        ]

        store.hideCoachSignals(withIDs: [
            "measurements_due", "measurements_overdue", "waist_limit",
        ])

        XCTAssertEqual(store.coachSignals.map(\.signalID), ["return_soon"])
    }

    func testPendingRecommendationSuppressesEveryPresentableCoachSignal() {
        let store = TrainerStore(defaults: .isolatedTestDefaults())
        store.coachSignals = [
            CoachSignal(
                signalID: "weekly_report_ready",
                instanceKey: "weekly_report_ready:period=2026-08-10",
                severity: "info",
                title: "Готов отчёт недели",
                body: "Итоги недели",
                note: nil,
                glyph: "doc",
                action: CoachSignalAction(
                    type: "open_weekly_report", label: "Отчёт", target: nil
                ),
                snoozable: true
            ),
        ]

        var pending = readyRecommendation()
        pending.status = "pending"
        store.recommendation = pending
        XCTAssertTrue(store.presentableCoachSignals.isEmpty)

        store.recommendation = readyRecommendation()
        XCTAssertEqual(store.presentableCoachSignals.map(\.signalID), ["weekly_report_ready"])

        store.isRefreshingRecommendation = true
        XCTAssertTrue(store.presentableCoachSignals.isEmpty)
    }

    func testDeletedWorkoutCannotAcceptRecommendationFromOldHistorySnapshot() {
        var oldRecommendation = readyRecommendation()
        oldRecommendation.basedOnWorkoutID = 133
        oldRecommendation.basedOnWorkoutCount = 10

        XCTAssertFalse(TrainerStore.recommendation(
            oldRecommendation,
            matchesWorkoutCount: 9,
            latestWorkoutID: 132
        ))

        oldRecommendation.basedOnWorkoutID = 132
        oldRecommendation.basedOnWorkoutCount = 9
        XCTAssertTrue(TrainerStore.recommendation(
            oldRecommendation,
            matchesWorkoutCount: 9,
            latestWorkoutID: 132
        ))

        oldRecommendation.stale = true
        XCTAssertFalse(TrainerStore.recommendation(
            oldRecommendation,
            matchesWorkoutCount: 9,
            latestWorkoutID: 132
        ))
    }

    func testAutoApplySkipsNonReadyRecommendation() {
        let store = TrainerStore(defaults: .isolatedTestDefaults())
        store.exercises = TestFixtures.catalog
        store.recommendation = RecommendationResponse(
            ok: true, status: "pending", stale: false,
            basedOnWorkoutID: nil, basedOnWorkoutCount: nil, model: nil,
            updatedAt: nil, error: nil, recommendation: nil
        )

        store.autoApplyRecommendationIfReady()
        XCTAssertNil(store.appliedPlan)
    }

    func testQuickAddFollowsAppliedPlanThenContinuesFromCustomSet() {
        let store = TrainerStore(defaults: .isolatedTestDefaults())
        store.exercises = TestFixtures.catalog
        store.recommendation = readyRecommendation()
        store.applyRecommendationAsPlan()

        // First quick "+" takes the plan's first target.
        store.addPlannedSet(exerciseID: 8)
        XCTAssertEqual(store.draft.exercises.first?.sets.last?.reps, 12)
        XCTAssertEqual(store.draft.exercises.first?.sets.last?.weight, 90)

        // On-plan set logged → next quick "+" walks to target #2.
        store.addPlannedSet(exerciseID: 8)
        XCTAssertEqual(store.draft.exercises.first?.sets.last?.reps, 10)
        XCTAssertEqual(store.draft.exercises.first?.sets.last?.weight, 95)

        // Custom set → the next "+" repeats it instead of snapping back.
        store.applySet(TestFixtures.draftSet(reps: 8, weight: 100, effort: .hard), exerciseID: 8, setIndex: nil)
        store.addPlannedSet(exerciseID: 8)
        XCTAssertEqual(store.draft.exercises.first?.sets.last?.reps, 8)
        XCTAssertEqual(store.draft.exercises.first?.sets.last?.weight, 100)
        XCTAssertNil(store.draft.exercises.first?.sets.last?.effort)
    }

    func testRemoveFromPlanDropsExerciseAndClearsWhenEmpty() {
        let store = TrainerStore(defaults: .isolatedTestDefaults())
        store.exercises = TestFixtures.catalog
        store.recommendation = readyRecommendation()
        store.applyRecommendationAsPlan()

        store.removeFromPlan(exerciseID: 8)
        XCTAssertEqual(store.appliedPlan?.exercises.map(\.exerciseID), [9])

        // Dropping the last exercise drops the plan entirely.
        store.removeFromPlan(exerciseID: 9)
        XCTAssertNil(store.appliedPlan)
    }

    func testAppliedPlanPersistsAcrossStoreInstances() {
        let defaults = UserDefaults.isolatedTestDefaults()
        let store = TrainerStore(defaults: defaults)
        store.exercises = TestFixtures.catalog
        store.recommendation = readyRecommendation()
        store.applyRecommendationAsPlan()

        let restored = TrainerStore(defaults: defaults)
        XCTAssertEqual(restored.appliedPlan?.exercises.map(\.exerciseID), [8, 9])
        XCTAssertEqual(restored.appliedPlan?.focus, "Верх+низ")
    }

    func testStoreMigratesLegacyLocalBackendURLToProduction() {
        let defaults = UserDefaults.isolatedTestDefaults()
        defaults.set("http://127.0.0.1:8080/", forKey: "trainer-ios-api-base-url-v1")

        let store = TrainerStore(defaults: defaults)

        XCTAssertEqual(store.apiBaseURLString, "https://trainer.superbatonec.org")
    }

    func testAddPlannedSetCreatesRealDraftAndPersistsAcrossStoreInstances() {
        let defaults = UserDefaults.isolatedTestDefaults()
        let store = TrainerStore(defaults: defaults)
        store.exercises = TestFixtures.catalog
        store.workouts = [
            TestFixtures.workout(
                exercises: [
                    TestFixtures.exercise(id: 8, name: "Жим ногами", sets: [
                        TestFixtures.set(index: 1, reps: 15, weight: 80, effort: .hard)
                    ])
                ]
            )
        ]

        store.addPlannedSet(exerciseID: 8)
        let restored = TrainerStore(defaults: defaults)

        XCTAssertTrue(store.draft.hasRealSets)
        XCTAssertEqual(store.draft.exercises.first?.sets.first?.reps, 16)
        XCTAssertEqual(store.draft.exercises.first?.sets.first?.weight, 80)
        XCTAssertNil(store.draft.exercises.first?.sets.first?.effort)
        XCTAssertEqual(restored.draft.exercises.first?.exerciseName, "Жим ногами")
        XCTAssertEqual(restored.draft.exercises.first?.sets.first?.reps, 16)
    }

    func testApplySetCanEditLatestSetAndRemoveLastSetDropsEmptyExercise() {
        let store = configuredStore()

        store.applySet(TestFixtures.draftSet(reps: 12, weight: 70), exerciseID: 8, setIndex: nil)
        store.applySet(TestFixtures.draftSet(reps: 13, weight: 75, effort: .ok), exerciseID: 8, setIndex: nil)
        store.applySet(TestFixtures.draftSet(reps: 11, weight: 77.5, effort: .hard), exerciseID: 8, setIndex: 1)

        XCTAssertEqual(store.draft.exercises.first?.sets.map(\.reps), [12, 11])
        XCTAssertEqual(store.draft.exercises.first?.sets.last?.weight, 77.5)
        XCTAssertEqual(store.draft.exercises.first?.sets.last?.effort, .hard)

        store.removeLastSet(exerciseID: 8)
        store.removeLastSet(exerciseID: 8)

        XCTAssertTrue(store.draft.exercises.isEmpty)
        XCTAssertFalse(store.draft.hasRealSets)
    }

    func testResetDraftClearsPersistedDraft() {
        let defaults = UserDefaults.isolatedTestDefaults()
        let store = configuredStore(defaults: defaults)

        store.addPlannedSet(exerciseID: 8)
        XCTAssertTrue(TrainerStore(defaults: defaults).draft.hasRealSets)

        store.resetDraft()

        XCTAssertFalse(store.draft.hasAnyExercise)
        XCTAssertFalse(TrainerStore(defaults: defaults).draft.hasAnyExercise)
    }

    func testStartEditingPreservesServerIDClientIDDateAndSets() {
        let store = configuredStore()
        let workout = TestFixtures.workout(
            id: 77,
            clientID: "editable-client",
            date: "2026-05-03",
            exercises: [
                TestFixtures.exercise(id: 8, name: "Жим ногами", sets: [
                    TestFixtures.set(index: 1, reps: 12, weight: 90, effort: .easy)
                ])
            ]
        )

        store.startEditing(workout)

        XCTAssertTrue(store.isWorkoutBuilderPresented)
        XCTAssertEqual(store.draft.editingWorkoutID, 77)
        XCTAssertEqual(store.draft.editingClientID, "editable-client")
        XCTAssertEqual(store.draft.workoutDate, "2026-05-03")
        XCTAssertEqual(store.draft.exercises.first?.sets.first?.effort, .easy)
    }

    func testBodyWeightComposerPrefillsExistingSelectedDateThenLatestOverall() {
        let store = configuredStore()
        store.bodyWeightEntries = TrainerLogic.sortBodyWeights([
            TestFixtures.bodyWeight(id: 1, date: "2026-05-01", weight: 82.4),
            TestFixtures.bodyWeight(id: 2, date: "2026-05-03", weight: 81.9)
        ])

        store.bodyWeightDate = "unchanged"
        store.bodyWeightValue = "unchanged"
        XCTAssertEqual(store.bodyWeightComposerValue(for: "2026-05-01"), "82.4")
        XCTAssertEqual(store.bodyWeightDate, "unchanged")
        XCTAssertEqual(store.bodyWeightValue, "unchanged")

        store.bodyWeightDate = "2026-05-01"
        store.syncBodyWeightComposer()
        XCTAssertEqual(store.bodyWeightValue, "82.4")

        store.bodyWeightDate = "2026-05-02"
        store.syncBodyWeightComposer()
        XCTAssertEqual(store.bodyWeightValue, "81.9")

        store.setBodyWeightValue("82,45 кг")
        XCTAssertEqual(store.bodyWeightValue, "82.45")
    }

    func testWaistComposerSkipsLegacyValuesTheCoachCannotUse() async {
        let store = configuredStore()
        store.waistEntries = [
            WaistEntry(
                id: 1, entryDate: "2026-08-13", waist: 84,
                notes: nil, createdAt: nil, updatedAt: nil
            ),
            WaistEntry(
                id: 2, entryDate: "2026-08-14", waist: 231,
                notes: nil, createdAt: nil, updatedAt: nil
            ),
        ]
        store.waistDate = "2026-08-14"

        store.waistValue = "unchanged"
        XCTAssertEqual(store.waistComposerValue(for: "2026-08-14"), "84")
        XCTAssertEqual(store.waistValue, "unchanged")

        store.syncWaistComposer()

        XCTAssertEqual(store.waistValue, "84")
        store.setWaistValue("230")
        let didSave = await store.saveWaist()
        XCTAssertFalse(didSave)
        XCTAssertEqual(store.toast, "Талия должна быть от 50 до 160 см")
    }

    func testSelectedProgressExerciseFallsBackToRealHistoryAndPersists() {
        let defaults = UserDefaults.isolatedTestDefaults()
        let store = TrainerStore(defaults: defaults)
        store.exercises = []
        store.workouts = [
            TestFixtures.workout(
                exercises: [
                    TestFixtures.exercise(id: 777, name: "История", sets: [TestFixtures.set()])
                ]
            )
        ]

        XCTAssertEqual(store.progressExerciseOptions().map(\.name), ["История"])
        store.selectedProgressExerciseID = 777

        let restored = TrainerStore(defaults: defaults)
        XCTAssertEqual(restored.selectedProgressExerciseID, 777)
    }

    // MARK: - Refresh that outlives the client's patience

    /// The refresh POST is synchronous, but the server finishes the generation
    /// and saves it even after the client stops waiting. A timeout must
    /// therefore keep the pending overlay and pick the plan up from the cheap
    /// cached endpoint — never paint the error card over a ready plan.
    func testRefreshTimeoutWaitsForThePlanTheServerSavesAnyway() async throws {
        let store = stubbedStore()
        store.recommendation = readyRecommendation()
        RecommendationStub.enqueueTimeout("/api/recommendations/refresh")
        RecommendationStub.enqueue("/api/recommendations/next", json: pollJSON(status: "pending"))
        RecommendationStub.enqueue("/api/recommendations/next", json: pollJSON(status: "ready"))

        await store.refreshRecommendation()

        // Handed over to polling: still busy, still pending, no error anywhere,
        // and the previous payload survives for the "старый план скрыт" card.
        XCTAssertTrue(store.isRefreshingRecommendation)
        XCTAssertEqual(store.recommendation?.status, "pending")
        XCTAssertNil(store.recommendation?.error)
        XCTAssertNotNil(store.recommendation?.recommendation)
        XCTAssertNil(store.toast)
        XCTAssertTrue(store.isTodayPlanUnavailable)

        try await waitUntil("polling picks up the generated plan") {
            !store.isRefreshingRecommendation
        }

        XCTAssertEqual(store.recommendation?.status, "ready")
        XCTAssertEqual(store.recommendation?.recommendation?.focus, "Догенерация после таймаута")
        XCTAssertEqual(store.appliedPlan?.exercises.map(\.exerciseID), [8, 9])
        XCTAssertFalse(store.isTodayPlanUnavailable)
        XCTAssertEqual(store.toast, "Совет обновлён — генерация была дольше обычного")
        XCTAssertEqual(RecommendationStub.requestedPaths, [
            "/api/recommendations/refresh",
            "/api/recommendations/next",
            "/api/recommendations/next",
        ])
    }

    /// A failed generation that the server did answer stays a failure: error
    /// card, error toast, no background polling of a row that will not change.
    func testRefreshKeepsTheErrorCardForFailuresThatAreNotTimeouts() async throws {
        let store = stubbedStore()
        RecommendationStub.enqueue(
            "/api/recommendations/refresh",
            status: 502,
            json: #"{"ok":false,"reason":"Модель нарушила ограничения"}"#
        )

        await store.refreshRecommendation()

        XCTAssertFalse(store.isRefreshingRecommendation)
        XCTAssertEqual(store.recommendation?.status, "failed")
        XCTAssertEqual(store.recommendation?.error, "Модель нарушила ограничения")
        XCTAssertEqual(store.toast, "Модель нарушила ограничения")

        // Nothing may start polling behind the error card.
        try await Task.sleep(nanoseconds: 900_000_000)
        XCTAssertEqual(RecommendationStub.requestedPaths, ["/api/recommendations/refresh"])
    }

    func testOnlyTimeoutsMeanTheGenerationIsStillRunning() {
        XCTAssertTrue(TrainerStore.isTimeoutError(TrainerAPIError.timeout))
        XCTAssertTrue(TrainerStore.isTimeoutError(URLError(.timedOut)))

        XCTAssertFalse(TrainerStore.isTimeoutError(URLError(.notConnectedToInternet)))
        XCTAssertFalse(TrainerStore.isTimeoutError(URLError(.cancelled)))
        XCTAssertFalse(TrainerStore.isTimeoutError(TrainerAPIError.server(status: 502, reason: "no")))
        XCTAssertFalse(TrainerStore.isTimeoutError(TrainerAPIError.decoding("no")))
        XCTAssertFalse(TrainerStore.isTimeoutError(TrainerAPIError.invalidResponse))
    }

    /// The default 50s window assumes a generation that started long before the
    /// poll; after a timeout the client has waited ~90s of a worst case that is
    /// twice ANTHROPIC_TIMEOUT (semantic-validator repromt), so the window has
    /// to cover the remaining ~150s.
    func testTimeoutPollWindowOutlastsTheBackendWorstCase() {
        XCTAssertGreaterThanOrEqual(TrainerStore.refreshTimeoutPollWindow, 150)
    }

    private func stubbedStore() -> TrainerStore {
        RecommendationStub.reset()
        addTeardownBlock { RecommendationStub.reset() }
        let store = TrainerStore(
            defaults: .isolatedTestDefaults(),
            urlSession: RecommendationStub.session()
        )
        store.exercises = TestFixtures.catalog
        return store
    }

    private func waitUntil(
        _ what: String,
        timeout: TimeInterval = 10,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ isDone: () -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !isDone() {
            if Date() >= deadline {
                XCTFail("Timed out waiting until \(what)", file: file, line: line)
                return
            }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
    }

    /// What GET /api/recommendations/next returns while (and after) the server
    /// finishes the generation the refresh POST no longer waits for. A pending
    /// row keeps the previous payload, exactly like the backend's cached row.
    private func pollJSON(status: String) -> String {
        #"""
        {
          "ok": true,
          "status": "\#(status)",
          "stale": \#(status == "pending" ? "true" : "false"),
          "based_on_workout_id": 133,
          "based_on_workout_count": 10,
          "model": "claude-opus-4-8",
          "updated_at": 1781200500,
          "error": null,
          "recommendation": {
            "focus": "\#(status == "pending" ? "Верх+низ" : "Догенерация после таймаута")",
            "load_type": "medium",
            "rationale": "Вторая попытка модели уложилась в серверный таймаут.",
            "exercises": [
              {"exercise_id": 8, "name": "Жим ногами", "note": "мягкий вход",
               "sets": [{"reps": 12, "weight": 90}]},
              {"exercise_id": 9, "name": "Тяга верт.", "note": null,
               "sets": [{"reps": 12, "weight": 70}]}
            ]
          }
        }
        """#
    }

    private func configuredStore(defaults: UserDefaults = .isolatedTestDefaults()) -> TrainerStore {
        let store = TrainerStore(defaults: defaults)
        store.exercises = TestFixtures.catalog
        store.workouts = [
            TestFixtures.workout(
                exercises: [
                    TestFixtures.exercise(id: 8, name: "Жим ногами", sets: [
                        TestFixtures.set(index: 1, reps: 15, weight: 80)
                    ])
                ]
            )
        ]
        return store
    }
}

/// Scripts the store's network per endpoint: the refresh POST and the cached
/// reads that follow it need different answers in the same test. The last stub
/// queued for a path sticks, so an extra poll never falls off the script.
private final class RecommendationStub: URLProtocol {
    struct Stub {
        var status = 200
        var json = "{}"
        var error: Error?
    }

    static var stubs: [String: [Stub]] = [:]
    static var requestedPaths: [String] = []

    static func reset() {
        stubs = [:]
        requestedPaths = []
    }

    static func enqueue(_ path: String, status: Int = 200, json: String) {
        stubs[path, default: []].append(Stub(status: status, json: json))
    }

    static func enqueueTimeout(_ path: String) {
        stubs[path, default: []].append(Stub(error: URLError(.timedOut)))
    }

    static func session() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [RecommendationStub.self]
        return URLSession(configuration: config)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let path = request.url?.path ?? ""
        Self.requestedPaths.append(path)
        let stub = Self.next(for: path)

        if let error = stub.error {
            client?.urlProtocol(self, didFailWithError: error)
            return
        }

        let http = HTTPURLResponse(
            url: request.url!,
            statusCode: stub.status,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json; charset=utf-8"]
        )!
        client?.urlProtocol(self, didReceive: http, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(stub.json.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static func next(for path: String) -> Stub {
        guard let queued = stubs[path], let stub = queued.first else {
            return Stub(status: 500, json: #"{"ok":false,"reason":"No stub for \#(path)"}"#)
        }
        if queued.count > 1 {
            stubs[path] = Array(queued.dropFirst())
        }
        return stub
    }
}
