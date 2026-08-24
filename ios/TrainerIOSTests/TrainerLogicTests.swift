import XCTest

@testable import TrainerIOS

final class TrainerLogicTests: XCTestCase {
    func testSortWorkoutsUsesDateCreatedUpdatedAndIDFreshness() {
        let old = TestFixtures.workout(
            id: 1,
            date: "2026-05-01",
            createdAt: 100,
            updatedAt: 100,
            exercises: [
                TestFixtures.exercise(id: 8, name: "Жим ногами", sets: [TestFixtures.set()])
            ]
        )
        let newestSameDay = TestFixtures.workout(
            id: 4,
            date: "2026-05-03",
            createdAt: 100,
            updatedAt: 300,
            exercises: [TestFixtures.exercise(id: 12, name: "Трицепс", sets: [TestFixtures.set()])]
        )
        let createdLaterSameDay = TestFixtures.workout(
            id: 3,
            date: "2026-05-03",
            createdAt: 200,
            updatedAt: 100,
            exercises: [TestFixtures.exercise(id: 11, name: "Бицепс", sets: [TestFixtures.set()])]
        )
        let newerDate = TestFixtures.workout(
            id: 2,
            date: "2026-05-04",
            createdAt: 1,
            updatedAt: 1,
            exercises: [
                TestFixtures.exercise(id: 9, name: "Тяга верт.", sets: [TestFixtures.set()])
            ]
        )

        let sorted = TrainerLogic.sortWorkouts([old, newestSameDay, newerDate, createdLaterSameDay])

        XCTAssertEqual(sorted.compactMap(\.id), [2, 3, 4, 1])
    }

    func testSummaryGroupsConsecutiveSameWeightWithEffortAndRepRuns() {
        let summary = TrainerLogic.summarizeExerciseSets([
            TestFixtures.set(index: 1, reps: 14, weight: 60, effort: .easy),
            TestFixtures.set(index: 2, reps: 14, weight: 60, effort: .easy),
            TestFixtures.set(index: 3, reps: 13, weight: 60, effort: .easy, notes: "steady"),
            TestFixtures.set(index: 4, reps: 12, weight: 90, effort: .hard),
        ])

        XCTAssertEqual(summary.parts, ["60кг ×14×2, 13 🙂", "90кг ×12 😣"])
        XCTAssertEqual(summary.notes, ["steady"])
        XCTAssertEqual(summary.segments.map(\.editSetIndex), [0, 3])
    }

    func testPlanningContextUsesLatestExerciseAndBuildsPlusOnePlanWithoutCopyingEffortOrNotes() {
        let older = TestFixtures.workout(
            id: 1,
            date: "2026-05-01",
            exercises: [
                TestFixtures.exercise(
                    id: 8, name: "Жим ногами",
                    sets: [
                        TestFixtures.set(index: 1, reps: 10, weight: 70)
                    ])
            ]
        )
        let latest = TestFixtures.workout(
            id: 2,
            date: "2026-05-04",
            exercises: [
                TestFixtures.exercise(
                    id: 8, name: "Жим ногами",
                    sets: [
                        TestFixtures.set(
                            index: 1, reps: 15, weight: 80, effort: .hard, notes: "last"),
                        TestFixtures.set(index: 2, reps: 13, weight: 90, effort: .ok),
                    ])
            ]
        )

        let context = TrainerLogic.planningContext(workouts: [older, latest], exerciseID: 8)

        XCTAssertEqual(context?.workoutID, 2)
        XCTAssertEqual(context?.previousSummary.parts, ["80кг ×15 😣", "90кг ×13 😐"])
        XCTAssertEqual(context?.plannedSets.map(\.reps), [16, 14])
        XCTAssertEqual(context?.plannedSets.map(\.weight), [80, 90])
        XCTAssertEqual(context?.plannedSets.map(\.effort), [nil, nil])
        XCTAssertEqual(context?.plannedSets.map(\.notes), [nil, nil])
        XCTAssertEqual(context?.progressionParts.map(\.previousEffort), [.hard, .ok])
        XCTAssertEqual(context?.progressionParts.map(\.nextLabel), ["16", "14"])
    }

    func testPlannedSetFallsBackAndThenAdvancesThroughLatestPlanSequence() {
        let workout = TestFixtures.workout(
            exercises: [
                TestFixtures.exercise(
                    id: 8, name: "Жим ногами",
                    sets: [
                        TestFixtures.set(index: 1, reps: 15, weight: 80),
                        TestFixtures.set(index: 2, reps: 13, weight: 90),
                        TestFixtures.set(index: 3, reps: 12, weight: 90),
                    ])
            ]
        )

        let first = TrainerLogic.plannedSet(workouts: [workout], exerciseID: 8, draftSetIndex: 0)
        let second = TrainerLogic.plannedSet(workouts: [workout], exerciseID: 8, draftSetIndex: 1)
        let third = TrainerLogic.plannedSet(workouts: [workout], exerciseID: 8, draftSetIndex: 2)
        let beyondPlan = TrainerLogic.plannedSet(
            workouts: [workout], exerciseID: 8, draftSetIndex: 99)
        let fallback = TrainerLogic.plannedSet(workouts: [], exerciseID: 8, draftSetIndex: 0)

        XCTAssertEqual([first.reps, second.reps, third.reps, beyondPlan.reps], [16, 14, 13, 13])
        XCTAssertEqual(
            [first.weight, second.weight, third.weight, beyondPlan.weight], [80, 90, 90, 90])
        XCTAssertEqual(fallback.reps, 12)
        XCTAssertEqual(fallback.weight, 0)
    }

    func testExercisePickerBuildsPrimarySixFromHistoryAndKeepsBenchPressRare() {
        let workouts = popularHistory()
        let groups = TrainerLogic.exercisePickerGroups(
            available: TestFixtures.catalog,
            catalog: TestFixtures.catalog,
            workouts: workouts,
            draftExercises: []
        )

        XCTAssertEqual(
            groups.primary.map(\.name),
            [
                "Жим ногами",
                "Бабочка",
                "Тяга верт.",
                "Дельты",
                "Бицепс",
                "Трицепс",
            ])
        XCTAssertFalse(groups.primary.map(\.id).contains(1))
        XCTAssertTrue(groups.secondary.map(\.name).contains("Жим гор."))
        XCTAssertEqual(groups.primaryPoolTotal, 6)
    }

    func testDraftDisplayCardsStartAsPrimaryPreviewsAndActualCardsReplacePreview() {
        let workouts = popularHistory()
        let draft = [
            TestFixtures.draftExercise(
                id: 8,
                name: "Жим ногами",
                sets: [TestFixtures.draftSet(reps: 16, weight: 80)]
            ),
            TestFixtures.draftExercise(
                id: 1,
                name: "Жим гор.",
                sets: [TestFixtures.draftSet(reps: 12, weight: 50)]
            ),
        ]

        let cards = TrainerLogic.draftDisplayCards(
            exercises: TestFixtures.catalog,
            workouts: workouts,
            draftExercises: draft
        )

        XCTAssertEqual(
            cards.prefix(6).map(\.exerciseName),
            [
                "Жим ногами",
                "Бабочка",
                "Тяга верт.",
                "Дельты",
                "Бицепс",
                "Трицепс",
            ])
        XCTAssertEqual(cards.first?.isPreview, false)
        XCTAssertEqual(cards.first?.sets.count, 1)
        XCTAssertEqual(cards.last?.exerciseName, "Жим гор.")
        XCTAssertEqual(cards.last?.isPreview, false)
    }

    /// Регрессия: план тренера показывает свои упражнения, а «Добавить
    /// упражнение» обязано отдать ВСЁ остальное из каталога. Раньше список
    /// строился как дополнение к основной шестёрке, и частое упражнение вне
    /// плана (жим ногами) не появлялось ни карточкой, ни в каталоге — записать
    /// его было нечем.
    func testAddableCatalogOffersEveryExerciseMissingFromTheScreen() {
        let workouts = popularHistory()
        // На экране только план: тяга горизонт. + сгибания ног.
        let shown: Set<Int> = [10, 15]

        let addable = TrainerLogic.addableExercises(
            exercises: TestFixtures.catalog,
            workouts: workouts,
            shownExerciseIDs: shown
        )

        XCTAssertEqual(
            Set(addable.map(\.id)),
            Set(TestFixtures.catalog.map(\.id)).subtracting(shown)
        )
        XCTAssertTrue(addable.map(\.name).contains("Жим ногами"))
        XCTAssertTrue(addable.map(\.name).contains("Жим гор."))

        // Частое идёт выше того, чего в истории не было ни разу.
        let names = addable.map(\.name)
        XCTAssertLessThan(
            names.firstIndex(of: "Жим ногами") ?? .max,
            names.firstIndex(of: "Разгибания ног") ?? .max
        )
    }

    func testAddableCatalogIsEmptyWhenEveryExerciseIsAlreadyOnScreen() {
        let addable = TrainerLogic.addableExercises(
            exercises: TestFixtures.catalog,
            workouts: popularHistory(),
            shownExerciseIDs: Set(TestFixtures.catalog.map(\.id))
        )

        XCTAssertTrue(addable.isEmpty)
    }

    func testDraftProgressIsInvisibleUntilRealSetsAndCountsFractionalPlanProgress() {
        let workouts =
            popularHistory() + [
                TestFixtures.workout(
                    id: 99,
                    date: "2026-05-07",
                    exercises: [
                        TestFixtures.exercise(
                            id: 8, name: "Жим ногами",
                            sets: [
                                TestFixtures.set(index: 1, reps: 15, weight: 80),
                                TestFixtures.set(index: 2, reps: 13, weight: 90),
                                TestFixtures.set(index: 3, reps: 12, weight: 90),
                            ])
                    ]
                )
            ]

        XCTAssertEqual(
            TrainerLogic.draftProgressRatio(
                exercises: TestFixtures.catalog,
                workouts: workouts,
                draftExercises: [],
                editingWorkoutID: nil
            ),
            0
        )

        let oneSet = [
            TestFixtures.draftExercise(
                id: 8,
                name: "Жим ногами",
                sets: [TestFixtures.draftSet(reps: 16, weight: 80)]
            )
        ]
        let threeSets = [
            TestFixtures.draftExercise(
                id: 8,
                name: "Жим ногами",
                sets: [
                    TestFixtures.draftSet(reps: 16, weight: 80),
                    TestFixtures.draftSet(reps: 14, weight: 90),
                    TestFixtures.draftSet(reps: 13, weight: 90),
                ]
            )
        ]

        XCTAssertEqual(
            TrainerLogic.draftProgressRatio(
                exercises: TestFixtures.catalog,
                workouts: workouts,
                draftExercises: oneSet,
                editingWorkoutID: nil
            ),
            1.0 / 18.0,
            accuracy: 0.0001
        )
        XCTAssertEqual(
            TrainerLogic.draftProgressRatio(
                exercises: TestFixtures.catalog,
                workouts: workouts,
                draftExercises: threeSets,
                editingWorkoutID: nil
            ),
            1.0 / 6.0,
            accuracy: 0.0001
        )
    }

    func testProgressSeriesUsesHeaviestSetAndHighestRepSetPerWorkoutInChronologicalOrder() {
        let workouts = [
            TestFixtures.workout(
                id: 3,
                date: DateTools.iso(from: Date()),
                exercises: [
                    TestFixtures.exercise(
                        id: 16, name: "Разгибания ног",
                        sets: [
                            TestFixtures.set(index: 1, reps: 12, weight: 130),
                            TestFixtures.set(index: 2, reps: 10, weight: 150),
                        ])
                ]
            ),
            TestFixtures.workout(
                id: 1,
                date: DateTools.iso(
                    from: Calendar.current.date(byAdding: .day, value: -2, to: Date())!),
                exercises: [
                    TestFixtures.exercise(
                        id: 16, name: "Разгибания ног",
                        sets: [
                            TestFixtures.set(index: 1, reps: 10, weight: 120),
                            TestFixtures.set(index: 2, reps: 14, weight: 110),
                        ])
                ]
            ),
        ]

        let series = TrainerLogic.buildExerciseProgressSeries(
            workouts: workouts,
            range: .days7,
            exerciseID: 16
        )
        let summary = TrainerLogic.summarizeExerciseSeries(series)

        XCTAssertEqual(series.map(\.workoutID), [1, 3])
        XCTAssertEqual(series.first?.bestWeight, 120)
        XCTAssertEqual(series.first?.bestReps, 14)
        XCTAssertEqual(series.last?.bestWeight, 150)
        XCTAssertEqual(summary?.weightDelta, 30)
        XCTAssertEqual(summary?.repsDelta, -2)
    }

    func testBodyWeightSummarySortsOldestToNewestAndPreservesPreciseValues() {
        let entries = [
            TestFixtures.bodyWeight(id: 3, date: "2026-05-03", weight: 81.95),
            TestFixtures.bodyWeight(id: 1, date: "2026-05-01", weight: 82.4),
            TestFixtures.bodyWeight(id: 2, date: "2026-05-02", weight: 82.05),
        ]

        let sorted = TrainerLogic.sortBodyWeights(entries)
        let summary = TrainerLogic.summarizeBodyWeights(
            filteredEntries: sorted, allEntries: entries)

        XCTAssertEqual(sorted.map(\.entryDate), ["2026-05-01", "2026-05-02", "2026-05-03"])
        XCTAssertEqual(summary.totalEntries, 3)
        XCTAssertEqual(summary.latestEntry?.weight, 81.95)
        XCTAssertEqual(summary.delta, -0.45, accuracy: 0.0001)
        XCTAssertEqual(TrainerLogic.formatBodyWeight(81.95), "81,95")
    }

    // MARK: - Applied coach plan

    private func samplePlan() -> AppliedCoachPlan {
        AppliedCoachPlan(
            basedOnWorkoutID: 133,
            basedOnWorkoutCount: 56,
            model: "claude-opus-4-8",
            generatedAt: 1_781_200_000,
            appliedAt: "2026-06-12",
            focus: "Верх+низ",
            loadType: "medium",
            exercises: [
                RecommendedExercise(
                    exerciseID: 9, name: "Тяга верт.", note: nil,
                    sets: [RecommendedSet(reps: 12, weight: 70)]
                ),
                RecommendedExercise(
                    exerciseID: 8, name: "Жим ногами", note: "мягкий вход",
                    sets: [
                        RecommendedSet(reps: 12, weight: 90), RecommendedSet(reps: 10, weight: 95),
                    ]
                ),
            ]
        )
    }

    func testNextPlannedSetWalksPlanTargetsAndClampsOnExhaustion() {
        let targets = [RecommendedSet(reps: 12, weight: 90), RecommendedSet(reps: 10, weight: 95)]

        let first = TrainerLogic.nextPlannedSet(
            workouts: [], exerciseID: 8, draftSets: [], planTargets: targets)
        XCTAssertEqual(first.reps, 12)
        XCTAssertEqual(first.weight, 90)

        let second = TrainerLogic.nextPlannedSet(
            workouts: [], exerciseID: 8,
            draftSets: [TestFixtures.draftSet(reps: 12, weight: 90)],
            planTargets: targets
        )
        XCTAssertEqual(second.reps, 10)
        XCTAssertEqual(second.weight, 95)

        let exhausted = TrainerLogic.nextPlannedSet(
            workouts: [], exerciseID: 8,
            draftSets: [
                TestFixtures.draftSet(reps: 12, weight: 90),
                TestFixtures.draftSet(reps: 10, weight: 95),
            ],
            planTargets: targets
        )
        XCTAssertEqual(exhausted.reps, 10)
        XCTAssertEqual(exhausted.weight, 95)
    }

    func testNextPlannedSetRepeatsCustomSetAndIgnoresEffortWithWeightEpsilon() {
        let targets = [RecommendedSet(reps: 12, weight: 90), RecommendedSet(reps: 10, weight: 95)]

        let afterCustom = TrainerLogic.nextPlannedSet(
            workouts: [], exerciseID: 8,
            draftSets: [TestFixtures.draftSet(reps: 8, weight: 100)],
            planTargets: targets
        )
        XCTAssertEqual(afterCustom.reps, 8)
        XCTAssertEqual(afterCustom.weight, 100)

        // Effort/notes and sub-0.01 weight noise must NOT count as deviation.
        let annotated = TrainerLogic.nextPlannedSet(
            workouts: [], exerciseID: 8,
            draftSets: [TestFixtures.draftSet(reps: 12, weight: 90.001, effort: .hard, notes: "x")],
            planTargets: targets
        )
        XCTAssertEqual(annotated.reps, 10)
        XCTAssertEqual(annotated.weight, 95)
    }

    func testNextPlannedSetWithoutPlanContinuesFromCustomElseHistory() {
        let history = [
            TestFixtures.workout(
                exercises: [
                    TestFixtures.exercise(
                        id: 8, name: "Жим ногами",
                        sets: [
                            TestFixtures.set(index: 1, reps: 10, weight: 80)
                        ])
                ]
            )
        ]

        let fromHistory = TrainerLogic.nextPlannedSet(
            workouts: history, exerciseID: 8, draftSets: [], planTargets: nil)
        XCTAssertEqual(fromHistory.reps, 11)
        XCTAssertEqual(fromHistory.weight, 80)

        // The original complaint: a custom set then "+" must not snap back to
        // the history template — it continues from the custom set.
        let afterCustom = TrainerLogic.nextPlannedSet(
            workouts: history, exerciseID: 8,
            draftSets: [TestFixtures.draftSet(reps: 6, weight: 120)],
            planTargets: nil
        )
        XCTAssertEqual(afterCustom.reps, 6)
        XCTAssertEqual(afterCustom.weight, 120)

        let bare = TrainerLogic.nextPlannedSet(
            workouts: [], exerciseID: 8, draftSets: [], planTargets: nil)
        XCTAssertEqual(bare.reps, 12)
        XCTAssertEqual(bare.weight, 0)

        let bareCustom = TrainerLogic.nextPlannedSet(
            workouts: [], exerciseID: 8,
            draftSets: [TestFixtures.draftSet(reps: 9, weight: 45)],
            planTargets: nil
        )
        XCTAssertEqual(bareCustom.reps, 9)
        XCTAssertEqual(bareCustom.weight, 45)
    }

    func testPlanDisplayCardsKeepRecommendedOrderAndAppendExtras() {
        let draft = [
            TestFixtures.draftExercise(id: 8, name: "Жим ногами", sets: [TestFixtures.draftSet()]),
            TestFixtures.draftExercise(id: 11, name: "Бицепс", sets: [TestFixtures.draftSet()]),
        ]

        let cards = TrainerLogic.planDisplayCards(plan: samplePlan(), draftExercises: draft)

        XCTAssertEqual(cards.map(\.exerciseID), [9, 8, 11])
        XCTAssertTrue(cards[0].isPreview)  // plan exercise without sets yet
        XCTAssertFalse(cards[1].isPreview)  // plan exercise with logged sets
        XCTAssertFalse(cards[2].isPreview)  // off-plan extra appended last
    }

    func testPlanProgressRatioAveragesAgainstPlanTargets() {
        let plan = samplePlan()  // ex9: 1 target, ex8: 2 targets

        XCTAssertEqual(TrainerLogic.planProgressRatio(plan: plan, draftExercises: []), 0)

        let halfLegPress = [
            TestFixtures.draftExercise(id: 8, name: "Жим ногами", sets: [TestFixtures.draftSet()])
        ]
        XCTAssertEqual(
            TrainerLogic.planProgressRatio(plan: plan, draftExercises: halfLegPress),
            0.25,
            accuracy: 0.0001
        )

        let done = [
            TestFixtures.draftExercise(
                id: 8, name: "Жим ногами", sets: [TestFixtures.draftSet(), TestFixtures.draftSet()]),
            TestFixtures.draftExercise(id: 9, name: "Тяга верт.", sets: [TestFixtures.draftSet()]),
        ]
        XCTAssertEqual(TrainerLogic.planProgressRatio(plan: plan, draftExercises: done), 1)
    }

    func testPlanPlanningContextShowsPlanTargetWithWeight() {
        let context = TrainerLogic.planPlanningContext(
            workouts: [],
            exerciseID: 8,
            planExercise: samplePlan().exercises[1]
        )

        XCTAssertEqual(context.plannedSets.map(\.reps), [12, 10])
        XCTAssertEqual(context.progressionParts.first?.previousLabel, "—")
        XCTAssertEqual(context.progressionParts.first?.nextLabel, "90кг ×12")
    }

    func testWorkoutPayloadEncodesRecommendationSnapshotInSnakeCase() throws {
        let draft = TestFixtures.draft(
            exercises: [TestFixtures.draftExercise(sets: [TestFixtures.draftSet()])]
        )

        let payload = TrainerLogic.workoutPayload(
            from: draft, recommendation: samplePlan().snapshot)
        let data = try JSONEncoder().encode(payload)
        let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let snapshot = (object?["data"] as? [String: Any])?["recommendation"] as? [String: Any]

        XCTAssertNotNil(snapshot)
        XCTAssertEqual(snapshot?["schema"] as? Int, 1)
        XCTAssertEqual(snapshot?["source"] as? String, "coach")
        XCTAssertEqual(snapshot?["generated_at"] as? Int, 1_781_200_000)
        XCTAssertEqual(snapshot?["load_type"] as? String, "medium")
        let exercises = snapshot?["exercises"] as? [[String: Any]]
        XCTAssertEqual(exercises?.first?["exercise_id"] as? Int, 9)
        XCTAssertNil(exercises?.last?["note"])  // notes are stripped from snapshots

        // No plan applied → the key must be absent entirely.
        let plain = TrainerLogic.workoutPayload(from: draft)
        let plainData = try JSONEncoder().encode(plain)
        let plainObject = try JSONSerialization.jsonObject(with: plainData) as? [String: Any]
        XCTAssertNil((plainObject?["data"] as? [String: Any])?["recommendation"])
    }

    private func popularHistory() -> [Workout] {
        let core = [
            (8, "Жим ногами", 120.0, 15),
            (1, "Жим гор.", 50.0, 12),
            (9, "Тяга верт.", 60.0, 12),
            (13, "Дельты", 17.5, 15),
            (11, "Бицепс", 30.0, 12),
            (12, "Трицепс", 35.0, 12),
        ]

        let repeated = ["2026-05-01", "2026-05-03", "2026-05-05"].enumerated().map { offset, date in
            TestFixtures.workout(
                id: 10 + offset,
                clientID: "picker-core-\(offset)",
                date: date,
                exercises: core.map { item in
                    TestFixtures.exercise(
                        id: item.0,
                        name: item.1,
                        sets: [TestFixtures.set(reps: item.3, weight: item.2)]
                    )
                }
            )
        }

        let butterfly = TestFixtures.workout(
            id: 20,
            clientID: "picker-rare-butterfly",
            date: "2026-05-06",
            exercises: [
                TestFixtures.exercise(
                    id: 17,
                    name: "Бабочка",
                    sets: [TestFixtures.set(reps: 12, weight: 40)]
                )
            ]
        )

        return repeated + [butterfly]
    }

    // MARK: - История "next workout" card helpers

    func testRecommendationRepsLabelCollapsesUniformReps() {
        let sets = [
            RecommendedSet(reps: 12, weight: 90), RecommendedSet(reps: 12, weight: 90),
            RecommendedSet(reps: 12, weight: 90),
        ]
        XCTAssertEqual(TrainerLogic.recommendationRepsLabel(sets), "12 × 3")
    }

    func testRecommendationRepsLabelListsVaryingReps() {
        let sets = [
            RecommendedSet(reps: 12, weight: 60), RecommendedSet(reps: 12, weight: 60),
            RecommendedSet(reps: 10, weight: 60),
        ]
        XCTAssertEqual(TrainerLogic.recommendationRepsLabel(sets), "12, 12, 10")
    }

    func testRecommendationRepsLabelEmpty() {
        XCTAssertEqual(TrainerLogic.recommendationRepsLabel([]), "")
    }

    func testLatestWorkingWeightPicksMostRecentWorkoutAndHeaviestSet() {
        let workouts = [
            TestFixtures.workout(
                id: 1, clientID: "a", date: "2026-05-01",
                exercises: [
                    TestFixtures.exercise(
                        id: 8, name: "Жим ногами", sets: [TestFixtures.set(weight: 100)])
                ]
            ),
            TestFixtures.workout(
                id: 2, clientID: "b", date: "2026-05-20",
                exercises: [
                    TestFixtures.exercise(
                        id: 8, name: "Жим ногами",
                        sets: [
                            TestFixtures.set(weight: 110), TestFixtures.set(weight: 120),
                        ])
                ]
            ),
        ]
        XCTAssertEqual(TrainerLogic.latestWorkingWeight(in: workouts, exerciseID: 8), 120)
    }

    func testLatestWorkingWeightNilWhenNeverLogged() {
        let workouts = [
            TestFixtures.workout(
                id: 1, clientID: "a", date: "2026-05-01",
                exercises: [
                    TestFixtures.exercise(
                        id: 8, name: "Жим ногами", sets: [TestFixtures.set(weight: 100)])
                ]
            )
        ]
        XCTAssertNil(TrainerLogic.latestWorkingWeight(in: workouts, exerciseID: 99))
    }

    func testLatestWorkingWeightSkipsWorkoutsWithEmptySets() {
        let workouts = [
            TestFixtures.workout(
                id: 2, clientID: "b", date: "2026-05-20",
                exercises: [TestFixtures.exercise(id: 8, name: "Жим ногами", sets: [])]
            ),
            TestFixtures.workout(
                id: 1, clientID: "a", date: "2026-05-01",
                exercises: [
                    TestFixtures.exercise(
                        id: 8, name: "Жим ногами", sets: [TestFixtures.set(weight: 95)])
                ]
            ),
        ]
        XCTAssertEqual(TrainerLogic.latestWorkingWeight(in: workouts, exerciseID: 8), 95)
    }

    // MARK: - Weekly volume by muscle group

    func testWeeklyVolumeCountsPrimaryMuscleWithinWindow() {
        let today = DateTools.date(from: "2026-06-12")
        let workouts = [
            TestFixtures.workout(
                id: 1, clientID: "a", date: "2026-06-10",
                exercises: [
                    TestFixtures.exercise(
                        id: 18, name: "Жим",
                        sets: [TestFixtures.set(), TestFixtures.set(), TestFixtures.set()])
                ]),
            TestFixtures.workout(
                id: 2, clientID: "b", date: "2026-06-11",
                exercises: [
                    TestFixtures.exercise(
                        id: 8, name: "Жим ногами", sets: [TestFixtures.set(), TestFixtures.set()])
                ]),
            TestFixtures.workout(
                id: 3, clientID: "c", date: "2026-05-01",
                exercises: [  // outside 7d window
                    TestFixtures.exercise(id: 18, name: "Жим", sets: [TestFixtures.set()])
                ]),
        ]
        let vol = TrainerLogic.weeklyVolumeByGroup(workouts, today: today, days: 7)
        let chest = vol.first { $0.name == "Грудь" }!
        XCTAssertEqual(chest.count, 3)
        XCTAssertEqual(chest.status, .under)  // 3 < 10
        XCTAssertEqual(vol.first { $0.name == "Квадрицепс/ягод." }?.count, 2)
        XCTAssertEqual(vol.first { $0.name == "Бицепс бедра" }?.count, 0)
    }

    func testVolumeStatusThresholds() {
        var v = MuscleGroupVolume(name: "Бицепс", count: 6, minTarget: 4, maxTarget: 8)
        XCTAssertEqual(v.status, .onTarget)
        v.count = 2
        XCTAssertEqual(v.status, .under)
        v.count = 10
        XCTAssertEqual(v.status, .over)
    }

    // MARK: - Plan-vs-fact adherence

    func testAdherenceCapsDoneAtPlannedAndCountsSkips() {
        var w = TestFixtures.workout(
            id: 1, clientID: "a", date: DateTools.localTodayISO(),
            exercises: [
                // did 4 sets of ex 8 (one extra beyond the planned 3)
                TestFixtures.exercise(
                    id: 8, name: "Жим ногами",
                    sets: [
                        TestFixtures.set(), TestFixtures.set(), TestFixtures.set(),
                        TestFixtures.set(),
                    ])
                // ex 9 planned but never done → skipped
            ]
        )
        w.data.recommendation = RecommendationSnapshot(
            schema: 1, source: "coach", model: nil, generatedAt: nil, appliedAt: nil,
            basedOnWorkoutID: nil, basedOnWorkoutCount: nil, focus: nil, loadType: nil,
            exercises: [
                RecommendedExercise(
                    exerciseID: 8, name: "Жим ногами", note: nil,
                    sets: [
                        RecommendedSet(reps: 10, weight: 100),
                        RecommendedSet(reps: 10, weight: 100),
                        RecommendedSet(reps: 10, weight: 100),
                    ]),
                RecommendedExercise(
                    exerciseID: 9, name: "Тяга", note: nil,
                    sets: [RecommendedSet(reps: 10, weight: 60)]),
            ]
        )
        let s = TrainerLogic.adherenceSummary([w], range: .all)
        XCTAssertEqual(s.comparedWorkouts, 1)
        XCTAssertEqual(s.plannedSets, 4)  // 3 + 1
        XCTAssertEqual(s.doneSets, 3)  // min(4,3) + min(0,1)
        XCTAssertEqual(s.skippedExercises, 1)
        XCTAssertEqual(Int((s.ratio * 100).rounded()), 75)
        XCTAssertTrue(s.hasData)
    }

    func testAdherenceIgnoresWorkoutsWithoutSnapshot() {
        let w = TestFixtures.workout(
            id: 1, clientID: "a", date: DateTools.localTodayISO(),
            exercises: [
                TestFixtures.exercise(id: 8, name: "Жим ногами", sets: [TestFixtures.set()])
            ]
        )
        let s = TrainerLogic.adherenceSummary([w], range: .all)
        XCTAssertFalse(s.hasData)
        XCTAssertEqual(s.comparedWorkouts, 0)
    }

    // MARK: - Лента «Истории»: тренировки, события и подсказки в разрывах

    private func feedWorkout(id: Int, date: String) -> Workout {
        TestFixtures.workout(
            id: id,
            clientID: "client-\(id)",
            date: date,
            exercises: [
                TestFixtures.exercise(id: 8, name: "Жим ногами", sets: [TestFixtures.set()])
            ]
        )
    }

    func testHistoryFeedPutsGapPromptExactlyIntoTheHoleBetweenWorkouts() {
        let feed = TrainerLogic.historyFeed(
            workouts: [
                feedWorkout(id: 1, date: "2026-08-12"), feedWorkout(id: 2, date: "2026-08-21"),
            ],
            events: [],
            today: "2026-08-21"
        )

        XCTAssertEqual(feed.map(\.id), ["workout-server-2", "gap-2026-08-13", "workout-server-1"])
        guard case .gap(let gap) = feed[1] else { return XCTFail("Ожидалась подсказка в разрыве") }
        // Подсказка не может попасть на дату с тренировкой: она живёт строго
        // между ними, от следующего дня до предыдущего.
        XCTAssertEqual(gap.startDate, "2026-08-13")
        XCTAssertEqual(gap.endDate, "2026-08-20")
        XCTAssertEqual(gap.days, 8)
        XCTAssertFalse(gap.isRunning)
    }

    func testHistoryFeedKeepsShortBreaksSilent() {
        let feed = TrainerLogic.historyFeed(
            workouts: [
                feedWorkout(id: 1, date: "2026-08-12"), feedWorkout(id: 2, date: "2026-08-15"),
            ],
            events: [],
            today: "2026-08-15"
        )

        XCTAssertEqual(feed.map(\.id), ["workout-server-2", "workout-server-1"])
    }

    func testHistoryFeedPromptsTheOpenTailUpToToday() {
        let feed = TrainerLogic.historyFeed(
            workouts: [feedWorkout(id: 1, date: "2026-08-12")],
            events: [],
            today: "2026-08-20"
        )

        guard case .gap(let gap) = feed.first else { return XCTFail("Ожидалась подсказка сверху") }
        XCTAssertEqual(gap.startDate, "2026-08-13")
        XCTAssertEqual(gap.endDate, "2026-08-20")
        XCTAssertEqual(gap.days, 8)
        XCTAssertTrue(gap.isRunning)
    }

    func testHistoryFeedHidesPromptWhereAnEventAlreadyExplainsTheHole() {
        let feed = TrainerLogic.historyFeed(
            workouts: [
                feedWorkout(id: 1, date: "2026-08-12"), feedWorkout(id: 2, date: "2026-08-21"),
            ],
            events: [TestFixtures.event(id: 7, start: "2026-08-14", end: "2026-08-16")],
            today: "2026-08-21"
        )

        XCTAssertEqual(feed.map(\.id), ["workout-server-2", "event-7", "workout-server-1"])
    }

    func testHistoryFeedTreatsOpenEventAsCoveringEverythingUpToToday() {
        let feed = TrainerLogic.historyFeed(
            workouts: [feedWorkout(id: 1, date: "2026-08-12")],
            events: [TestFixtures.event(id: 3, start: "2026-08-13", end: nil)],
            today: "2026-08-20"
        )

        XCTAssertEqual(feed.map(\.id), ["event-3", "workout-server-1"])
    }

    func testHistoryFeedKeepsSameDayWorkoutOrderFromSortWorkouts() {
        let earlier = TestFixtures.workout(
            id: 1,
            clientID: "a",
            date: "2026-08-12",
            createdAt: 100,
            exercises: [
                TestFixtures.exercise(id: 8, name: "Жим ногами", sets: [TestFixtures.set()])
            ]
        )
        let later = TestFixtures.workout(
            id: 2,
            clientID: "b",
            date: "2026-08-12",
            createdAt: 200,
            exercises: [
                TestFixtures.exercise(id: 11, name: "Бицепс", sets: [TestFixtures.set()])
            ]
        )

        let feed = TrainerLogic.historyFeed(
            workouts: [earlier, later],
            events: [],
            today: "2026-08-12"
        )

        XCTAssertEqual(feed.map(\.id), ["workout-server-2", "workout-server-1"])
    }

    func testEventRailLabelsCollapseDaysAndOpenBothMonthsOnTheBoundary() {
        let single = TrainerLogic.eventRailLabels(
            TestFixtures.event(start: "2026-08-18", end: "2026-08-18"),
            today: "2026-08-23"
        )
        XCTAssertEqual(single.day, "18")
        XCTAssertEqual(single.month, "авг")

        let range = TrainerLogic.eventRailLabels(
            TestFixtures.event(start: "2026-08-05", end: "2026-08-09"),
            today: "2026-08-23"
        )
        XCTAssertEqual(range.day, "5–9")
        XCTAssertEqual(range.month, "авг")

        // «30–3» под одним «АПР» читалось бы как период внутри апреля.
        let crossMonth = TrainerLogic.eventRailLabels(
            TestFixtures.event(start: "2026-04-30", end: "2026-05-03"),
            today: "2026-08-23"
        )
        XCTAssertEqual(crossMonth.day, "30–3")
        XCTAssertEqual(crossMonth.month, "апр–май")

        let open = TrainerLogic.eventRailLabels(
            TestFixtures.event(start: "2026-08-21", end: nil),
            today: "2026-08-23"
        )
        XCTAssertEqual(open.day, "21")
        XCTAssertEqual(open.month, "авг")
    }

    func testFinishedWorkoutSummaryCountsExercisesAndSetsWithoutDeclension() {
        let workout = TestFixtures.workout(
            id: 1,
            exercises: [
                TestFixtures.exercise(
                    id: 8,
                    name: "Жим ногами",
                    sets: [TestFixtures.set(index: 1), TestFixtures.set(index: 2)]
                ),
                TestFixtures.exercise(id: 11, name: "Бицепс", sets: [TestFixtures.set(index: 1)]),
            ]
        )

        XCTAssertEqual(TrainerLogic.finishedWorkoutSummary(workout), "2 упр · 3 сет · 13 мин")
    }

    func testSetNotesLineCollapsesTheSameStagingRepeatedAcrossSets() {
        // Постановка одна на все подходы — и в карточке она одна строка.
        XCTAssertEqual(
            TrainerLogic.setNotesLine(["канат", "канат", "канат"]),
            "канат"
        )
        // Разные заметки остаются разными фактами, в порядке подходов.
        XCTAssertEqual(
            TrainerLogic.setNotesLine(["канат", "узкий хват", "канат"]),
            "канат · узкий хват"
        )
        XCTAssertEqual(TrainerLogic.setNotesLine([]), "")
    }
}
