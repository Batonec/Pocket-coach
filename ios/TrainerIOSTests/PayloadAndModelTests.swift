import XCTest
@testable import TrainerIOS

final class PayloadAndModelTests: XCTestCase {
    func testWeeklyReportPeriodLabelUsesInclusiveServerWindow() {
        XCTAssertEqual(
            DateTools.periodLabel(endingAt: "2026-08-10", days: 7),
            "4 авг – 10 авг"
        )
        XCTAssertEqual(
            DateTools.periodLabel(endingAt: "2026-08-10", days: 1),
            "10 авг – 10 авг"
        )
        XCTAssertEqual(
            DateTools.periodLabel(endingAt: "broken", days: 7),
            "broken"
        )
    }

    func testRecommendationResponseDecodesSnakeCaseContract() throws {
        let json = #"""
        {
          "ok": true,
          "status": "ready",
          "stale": true,
          "based_on_workout_count": 56,
          "model": "claude-opus-4-8",
          "error": null,
          "recommendation": {
            "focus": "Верх+низ",
            "load_type": "medium",
            "rationale": "После перерыва — средняя нагрузка.",
            "exercises": [
              {"exercise_id": 8, "name": "Жим ногами", "note": "мягкий вход",
               "sets": [{"reps": 12, "weight": 90}, {"reps": 10, "weight": 90}]}
            ]
          }
        }
        """#

        let decoded = try JSONDecoder().decode(RecommendationResponse.self, from: Data(json.utf8))

        XCTAssertEqual(decoded.status, "ready")
        XCTAssertEqual(decoded.stale, true)
        XCTAssertEqual(decoded.basedOnWorkoutCount, 56)
        XCTAssertEqual(decoded.model, "claude-opus-4-8")
        XCTAssertNil(decoded.error)
        let exercise = try XCTUnwrap(decoded.recommendation?.exercises.first)
        XCTAssertEqual(decoded.recommendation?.loadType, "medium")
        XCTAssertEqual(exercise.exerciseID, 8)
        XCTAssertEqual(exercise.note, "мягкий вход")
        XCTAssertEqual(exercise.sets.map(\.reps), [12, 10])
        XCTAssertEqual(exercise.sets.first?.weight, 90)
    }

    func testRecommendationResponseDecodesEmptyState() throws {
        let json = #"{"ok":true,"status":"none","recommendation":null,"stale":false}"#
        let decoded = try JSONDecoder().decode(RecommendationResponse.self, from: Data(json.utf8))
        XCTAssertEqual(decoded.status, "none")
        XCTAssertNil(decoded.recommendation)
        XCTAssertEqual(decoded.stale, false)
    }

    func testWorkoutPayloadMatchesBackendContractAndAssignsSequentialSetIndexes() throws {
        let draft = TestFixtures.draft(
            date: "2026-05-10",
            exercises: [
                TestFixtures.draftExercise(
                    id: 8,
                    name: "Жим ногами",
                    sets: [
                        TestFixtures.draftSet(reps: 15, weight: 80, effort: .hard, notes: "  good  "),
                        TestFixtures.draftSet(reps: 13, weight: 90, effort: nil, notes: "   ")
                    ]
                ),
                TestFixtures.draftExercise(id: 9, name: "Тяга верт.", sets: [])
            ]
        )

        let payload = TrainerLogic.workoutPayload(from: draft)
        let data = try JSONEncoder().encode(payload)
        let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let payloadData = object?["data"] as? [String: Any]
        let exercises = payloadData?["exercises"] as? [[String: Any]]
        let sets = exercises?.first?["sets"] as? [[String: Any]]

        XCTAssertEqual(object?["workout_date"] as? String, "2026-05-10")
        XCTAssertNotNil(object?["client_id"] as? String)
        XCTAssertEqual(payloadData?["load_type"] as? String, "medium")
        XCTAssertEqual(exercises?.count, 1)
        XCTAssertEqual(exercises?.first?["exercise_id"] as? Int, 8)
        XCTAssertEqual(sets?.map { $0["set_index"] as? Int }, [1, 2])
        XCTAssertEqual(sets?.first?["effort"] as? String, "hard")
        XCTAssertEqual(sets?.first?["notes"] as? String, "good")
        XCTAssertNil(sets?[1]["notes"] as? String)
    }

    func testWorkoutPayloadPreservesEditingClientIDAndInfersLoadTypeThresholds() {
        let editingDraft = TestFixtures.draft(
            date: "2026-05-11",
            editingWorkoutID: 42,
            editingClientID: "editable-workout",
            exercises: [
                TestFixtures.draftExercise(
                    sets: [TestFixtures.draftSet(reps: 12, weight: 20)]
                )
            ]
        )

        let light = TrainerLogic.workoutPayload(from: editingDraft)
        let medium = TrainerLogic.workoutPayload(from: TestFixtures.draft(exercises: [
            TestFixtures.draftExercise(sets: [
                TestFixtures.draftSet(reps: 10, weight: 50),
                TestFixtures.draftSet(reps: 10, weight: 50),
                TestFixtures.draftSet(reps: 10, weight: 50),
                TestFixtures.draftSet(reps: 10, weight: 50)
            ])
        ]))
        let heavy = TrainerLogic.workoutPayload(from: TestFixtures.draft(exercises: [
            TestFixtures.draftExercise(sets: [
                TestFixtures.draftSet(reps: 10, weight: 80),
                TestFixtures.draftSet(reps: 10, weight: 80),
                TestFixtures.draftSet(reps: 10, weight: 80),
                TestFixtures.draftSet(reps: 10, weight: 80)
            ])
        ]))

        XCTAssertEqual(light.id, 42)
        XCTAssertEqual(light.clientID, "editable-workout")
        XCTAssertEqual(light.data.loadType, "light")
        XCTAssertEqual(medium.data.loadType, "medium")
        XCTAssertEqual(heavy.data.loadType, "heavy")
    }

    func testAPIModelsDecodeServerSnakeCaseResponses() throws {
        let json = """
        {
          "ok": true,
          "user": {
            "id": 1,
            "auth_source": "debug",
            "telegram_user_id": null,
            "username": null,
            "email": null,
            "first_name": "Browser",
            "last_name": "Debug",
            "debug_alias": "browser-default",
            "is_default_debug_user": true,
            "display_name": "Browser Debug"
          },
          "workouts": [
            {
              "id": 10,
              "client_id": "client-a",
              "workout_date": "2026-05-10",
              "plan_id": null,
              "created_at": 100,
              "updated_at": 110,
              "data": {
                "focus": null,
                "notes": null,
                "load_type": "medium",
                "exercises": [
                  {
                    "exercise_id": 8,
                    "name": "Жим ногами",
                    "sets": [
                      {
                        "set_index": 1,
                        "reps": 15,
                        "weight": 80,
                        "effort": "hard",
                        "notes": null
                      }
                    ]
                  }
                ]
              }
            }
          ]
        }
        """

        let response = try JSONDecoder().decode(WorkoutsResponse.self, from: Data(json.utf8))

        XCTAssertTrue(response.ok)
        XCTAssertEqual(response.user?.authSource, "debug")
        XCTAssertNil(response.user?.email)
        XCTAssertEqual(response.user?.isDefaultDebugUser, true)
        XCTAssertEqual(response.workouts.first?.clientID, "client-a")
        XCTAssertEqual(response.workouts.first?.data.loadType, "medium")
        XCTAssertEqual(response.workouts.first?.data.exercises.first?.exerciseID, 8)
        XCTAssertEqual(response.workouts.first?.data.exercises.first?.sets.first?.effort, .hard)
    }

    func testBodyWeightResponseDecodesUpsertShape() throws {
        let json = """
        {
          "ok": true,
          "created": false,
          "entry": {
            "id": 3,
            "user_id": 1,
            "entry_date": "2026-05-10",
            "weight": 81.95,
            "notes": "Morning",
            "created_at": 100,
            "updated_at": 120
          }
        }
        """

        let response = try JSONDecoder().decode(BodyWeightMutationResponse.self, from: Data(json.utf8))

        XCTAssertEqual(response.created, false)
        XCTAssertEqual(response.entry.entryDate, "2026-05-10")
        XCTAssertEqual(response.entry.weight, 81.95)
        XCTAssertEqual(response.entry.notes, "Morning")
    }

    func testBundledExerciseCatalogMatchesMiniAppREADMESource() throws {
        let bundle = Bundle(for: Self.self)
        let url = try XCTUnwrap(bundle.url(forResource: "exercises", withExtension: "json"))
        let data = try Data(contentsOf: url)
        let catalog = try JSONDecoder().decode(ExerciseCatalogResponse.self, from: data)

        XCTAssertEqual(catalog.exercises.count, 12)
        XCTAssertEqual(catalog.exercises.first?.name, "Жим ногами")
        XCTAssertTrue(catalog.exercises.contains(ExerciseDefinition(id: 1, name: "Жим гор.")))
        XCTAssertTrue(catalog.exercises.contains(ExerciseDefinition(id: 4, name: "Подтягивания грав.")))
    }
}
