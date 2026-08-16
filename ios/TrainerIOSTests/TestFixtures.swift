import Foundation

@testable import TrainerIOS

enum TestFixtures {
    static let catalog: [ExerciseDefinition] = [
        ExerciseDefinition(id: 8, name: "Жим ногами"),
        ExerciseDefinition(id: 1, name: "Жим гор."),
        ExerciseDefinition(id: 9, name: "Тяга верт."),
        ExerciseDefinition(id: 13, name: "Дельты"),
        ExerciseDefinition(id: 11, name: "Бицепс"),
        ExerciseDefinition(id: 12, name: "Трицепс"),
        ExerciseDefinition(id: 16, name: "Разгибания ног"),
        ExerciseDefinition(id: 15, name: "Сгибания ног"),
        ExerciseDefinition(id: 10, name: "Тяга горизонт."),
        ExerciseDefinition(id: 17, name: "Бабочка"),
        ExerciseDefinition(id: 18, name: "Жим в тренажере"),
        ExerciseDefinition(id: 4, name: "Подтягивания грав."),
    ]

    static func workout(
        id: Int? = 1,
        clientID: String = "client-1",
        date: String = "2026-05-01",
        createdAt: Int = 100,
        updatedAt: Int = 100,
        loadType: String? = nil,
        exercises: [LoggedExercise]
    ) -> Workout {
        Workout(
            id: id,
            clientID: clientID,
            workoutDate: date,
            planID: nil,
            createdAt: createdAt,
            updatedAt: updatedAt,
            data: WorkoutData(
                focus: nil,
                notes: nil,
                loadType: loadType,
                exercises: exercises
            )
        )
    }

    static func exercise(
        id: Int,
        name: String,
        sets: [WorkoutSet]
    ) -> LoggedExercise {
        LoggedExercise(exerciseID: id, name: name, sets: sets)
    }

    static func set(
        index: Int = 1,
        reps: Int = 12,
        weight: Double = 80,
        effort: SetEffort? = nil,
        notes: String? = nil
    ) -> WorkoutSet {
        WorkoutSet(
            setIndex: index,
            reps: reps,
            weight: weight,
            effort: effort,
            notes: notes
        )
    }

    static func draft(
        date: String = "2026-05-10",
        editingWorkoutID: Int? = nil,
        editingClientID: String? = nil,
        exercises: [DraftExercise]
    ) -> DraftWorkout {
        DraftWorkout(
            workoutDate: date,
            exercises: exercises,
            editingWorkoutID: editingWorkoutID,
            editingClientID: editingClientID
        )
    }

    static func draftExercise(
        id: Int = 8,
        name: String = "Жим ногами",
        sets: [DraftSet]
    ) -> DraftExercise {
        DraftExercise(exerciseID: id, exerciseName: name, sets: sets)
    }

    static func draftSet(
        reps: Int = 12,
        weight: Double = 80,
        effort: SetEffort? = nil,
        notes: String? = nil
    ) -> DraftSet {
        DraftSet(reps: reps, weight: weight, effort: effort, notes: notes)
    }

    static func bodyWeight(
        id: Int,
        date: String,
        weight: Double,
        updatedAt: Int = 100
    ) -> BodyWeightEntry {
        BodyWeightEntry(
            id: id,
            userID: 1,
            entryDate: date,
            weight: weight,
            notes: nil,
            createdAt: updatedAt,
            updatedAt: updatedAt
        )
    }
}

extension UserDefaults {
    static func isolatedTestDefaults(file: StaticString = #filePath, line: UInt = #line)
        -> UserDefaults
    {
        let suiteName = "TrainerIOSTests.\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suiteName) else {
            fatalError("Failed to create isolated defaults", file: file, line: line)
        }
        defaults.removePersistentDomain(forName: suiteName)
        return defaults
    }
}
