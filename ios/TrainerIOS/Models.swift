import Foundation

enum TrainerTab: String, CaseIterable, Codable, Identifiable {
    case trainings
    case history
    case progress
    case weight

    var id: String { rawValue }

    var title: String {
        switch self {
        case .trainings: "Сегодня"
        case .history: "История"
        case .progress: "Прогресс"
        case .weight: "Замеры"
        }
    }

    var systemImage: String {
        switch self {
        case .trainings: "flame"
        case .history: "clock.arrow.circlepath"
        case .progress: "chart.line.uptrend.xyaxis"
        case .weight: "scalemass"
        }
    }
}

enum RangeOption: String, CaseIterable, Codable, Identifiable {
    case days7 = "DAYS_7"
    case days30 = "DAYS_30"
    case all = "ALL"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .days7: "7D"
        case .days30: "30D"
        case .all: "All"
        }
    }

    var days: Int? {
        switch self {
        case .days7: 7
        case .days30: 30
        case .all: nil
        }
    }
}

enum SetEffort: String, CaseIterable, Codable, Identifiable {
    case easy
    case ok
    case hard

    var id: String { rawValue }

    var icon: String {
        switch self {
        case .easy: "🙂"
        case .ok: "😐"
        case .hard: "😣"
        }
    }

    var label: String {
        switch self {
        case .easy: "Легко"
        case .ok: "Норм"
        case .hard: "Тяжело"
        }
    }

}

struct ExerciseCatalogResponse: Codable {
    var exercises: [ExerciseDefinition]
}

struct ExerciseDefinition: Codable, Hashable, Identifiable {
    var id: Int
    var name: String
}

struct TrainerUser: Codable, Hashable, Identifiable {
    var id: Int
    var authSource: String?
    var telegramUserID: Int?
    var username: String?
    var email: String?
    var firstName: String?
    var lastName: String?
    var debugAlias: String?
    var isDefaultDebugUser: Bool?
    var displayName: String?

    enum CodingKeys: String, CodingKey {
        case id
        case authSource = "auth_source"
        case telegramUserID = "telegram_user_id"
        case username
        case email
        case firstName = "first_name"
        case lastName = "last_name"
        case debugAlias = "debug_alias"
        case isDefaultDebugUser = "is_default_debug_user"
        case displayName = "display_name"
    }
}

struct Workout: Codable, Hashable, Identifiable {
    var id: Int?
    var clientID: String?
    var workoutDate: String
    var planID: Int?
    var createdAt: Int?
    var updatedAt: Int?
    var data: WorkoutData

    var stableID: String {
        if let id {
            return "server-\(id)"
        }
        return clientID ?? workoutDate
    }

    enum CodingKeys: String, CodingKey {
        case id
        case clientID = "client_id"
        case workoutDate = "workout_date"
        case planID = "plan_id"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case data
    }
}

struct WorkoutData: Codable, Hashable {
    var focus: String?
    var notes: String?
    var loadType: String?
    var exercises: [LoggedExercise]
    var recommendation: RecommendationSnapshot?

    enum CodingKeys: String, CodingKey {
        case focus
        case notes
        case loadType = "load_type"
        case exercises
        case recommendation
    }
}

struct LoggedExercise: Codable, Hashable, Identifiable {
    var exerciseID: Int
    var name: String
    var sets: [WorkoutSet]

    var id: Int { exerciseID }

    enum CodingKeys: String, CodingKey {
        case exerciseID = "exercise_id"
        case name
        case sets
    }
}

struct WorkoutSet: Codable, Hashable, Identifiable {
    var setIndex: Int?
    var reps: Int
    var weight: Double
    var effort: SetEffort?
    var notes: String?

    var id: Int { setIndex ?? reps.hashValue ^ weight.hashValue }

    enum CodingKeys: String, CodingKey {
        case setIndex = "set_index"
        case reps
        case weight
        case effort
        case notes
    }
}

struct WorkoutsResponse: Codable {
    var ok: Bool
    var user: TrainerUser?
    var workouts: [Workout]
}

struct WorkoutMutationResponse: Codable {
    var ok: Bool
    var created: Bool?
    var user: TrainerUser?
    var workout: Workout
}

struct BodyWeightsResponse: Codable {
    var ok: Bool
    var user: TrainerUser?
    var entries: [BodyWeightEntry]
}

struct BodyWeightMutationResponse: Codable {
    var ok: Bool
    var created: Bool?
    var deleted: Bool?
    var user: TrainerUser?
    var entry: BodyWeightEntry
}

struct BodyWeightEntry: Codable, Hashable, Identifiable {
    var id: Int
    var userID: Int?
    var entryDate: String
    var weight: Double
    var notes: String?
    var createdAt: Int?
    var updatedAt: Int?

    enum CodingKeys: String, CodingKey {
        case id
        case userID = "user_id"
        case entryDate = "entry_date"
        case weight
        case notes
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

struct SessionResolveResponse: Codable {
    var ok: Bool
    var user: TrainerUser?
    var authMode: String?
    var warning: String?
    var reason: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case user
        case authMode = "auth_mode"
        case warning
        case reason
    }
}

struct SessionLogoutResponse: Codable {
    var ok: Bool
}

struct APIReasonResponse: Codable {
    var ok: Bool?
    var reason: String?
}

// MARK: - Coach recommendation ("Совет тренера")

/// Response of GET /api/recommendations/next and POST /api/recommendations/refresh.
/// Decoding is intentionally lenient (no enums, optionals) so an unexpected field
/// never discards the whole payload.
struct RecommendationResponse: Codable {
    var ok: Bool?
    var status: String?            // none | pending | ready | failed
    var stale: Bool?
    var basedOnWorkoutID: Int?
    var basedOnWorkoutCount: Int?
    var model: String?
    var updatedAt: Int?            // generation identity (the row id is constant per user)
    var error: String?
    var recommendation: RecommendationPayload?

    enum CodingKeys: String, CodingKey {
        case ok, status, stale, model, error, recommendation
        case basedOnWorkoutID = "based_on_workout_id"
        case basedOnWorkoutCount = "based_on_workout_count"
        case updatedAt = "updated_at"
    }
}

struct RecommendationPayload: Codable, Hashable {
    var focus: String
    var loadType: String
    var rationale: String
    var exercises: [RecommendedExercise]
    // When the coach suggests doing this workout. restDays: 0 = today, 1 = tomorrow…
    // nextWorkoutDate: that resolved to an ISO date server-side. Optional — older
    // cached payloads predate these fields.
    var restDays: Int?
    var nextWorkoutDate: String?
    // Phase/cycle context computed server-side at generation time: powers the
    // phase badge and the CURRENT week's volume targets on the Progress screen.
    var coachContext: CoachContext?

    enum CodingKeys: String, CodingKey {
        case focus
        case loadType = "load_type"
        case rationale
        case exercises
        case restDays = "rest_days"
        case nextWorkoutDate = "next_workout_date"
        case coachContext = "coach_context"
    }
}

/// Server-computed preparation-phase context riding along with the coach
/// recommendation (see backend `recommender._coach_context`). All fields are
/// optional — older cached payloads predate it.
struct CoachContext: Codable, Hashable {
    var phase: String?             // cut_recomp | lean_bulk | maintenance
    var phaseTitle: String?        // human title, RU
    var blockWeek: Int?
    var deloadWeek: Bool?
    var returnFromBreak: Bool?
    var weeklyTarget: [Int]?       // big-group corridor for this block week
    var groupTargets: [String: [Int]]?  // backend group name → [min, max]
    var targetWeightKg: Double?    // phase weight goal (cut target / bulk ceiling)
    var waistLimitCm: Double?      // hard waist limit, when the athlete set one

    enum CodingKeys: String, CodingKey {
        case phase
        case phaseTitle = "phase_title"
        case blockWeek = "block_week"
        case deloadWeek = "deload_week"
        case returnFromBreak = "return_from_break"
        case weeklyTarget = "weekly_target"
        case groupTargets = "group_targets"
        case targetWeightKg = "target_weight_kg"
        case waistLimitCm = "waist_limit_cm"
    }
}

struct WeeklyReportResponse: Codable {
    var ok: Bool?
    var report: WeeklyReportEntry?
}

/// A cached coach weekly report (generated by the Sunday timer server-side;
/// the endpoint never spends tokens).
struct WeeklyReportEntry: Codable, Hashable {
    var periodEnd: String
    var days: Int?
    var report: String
    var createdAt: Int?

    enum CodingKeys: String, CodingKey {
        case periodEnd = "period_end"
        case days
        case report
        case createdAt = "created_at"
    }
}

struct RecommendedExercise: Codable, Hashable, Identifiable {
    var exerciseID: Int
    var name: String
    var note: String?
    var sets: [RecommendedSet]

    var id: Int { exerciseID }

    enum CodingKeys: String, CodingKey {
        case exerciseID = "exercise_id"
        case name
        case note
        case sets
    }
}

struct RecommendedSet: Codable, Hashable {
    var reps: Int
    var weight: Double
}

/// Which metric the Замеры screen shows; deep-links from coach signals pick one.
enum MeasureMetric: String {
    case weight
    case waist
}

/// A waist measurement (cm) — the second body-composition metric next to
/// weight; feeds the coach nutrition matrix.
struct WaistEntry: Codable, Hashable, Identifiable {
    var id: Int
    var entryDate: String
    var waist: Double
    var notes: String?
    var createdAt: Int?
    var updatedAt: Int?

    enum CodingKeys: String, CodingKey {
        case id
        case entryDate = "entry_date"
        case waist
        case notes
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

struct WaistsResponse: Codable {
    var ok: Bool?
    var user: TrainerUser?
    var entries: [WaistEntry]
}

struct WaistMutationResponse: Codable {
    var ok: Bool?
    var created: Bool?
    var user: TrainerUser?
    var entry: WaistEntry
}

/// A coach signal for the История banner (see ios/COACH_SIGNALS_BANNER_BRIEF.md).
/// The server computes, collapses, snooze-filters and sorts the list; the
/// client renders the first 1–2 and never invents texts. Unknown ids/severities
/// must still render as a generic info banner (server-driven taxonomy).
struct CoachSignal: Codable, Hashable, Identifiable {
    var signalID: String       // signal type, e.g. "measurements_due"
    var instanceKey: String    // episode identity — dismiss target
    var severity: String       // info | accent | warn | critical | positive
    var title: String
    var body: String
    var note: String?          // optional third line (italic, muted)
    var glyph: String?         // semantic token mapped to an SF Symbol by the client
    var action: CoachSignalAction?
    var snoozable: Bool?

    var id: String { instanceKey }

    /// Server glyphs are semantic tokens, not drawing instructions. Every
    /// banner renders a native SF Symbol so the iconography stays optically
    /// consistent with iOS and never falls back to hand-drawn paths.
    var systemImage: String { Self.systemImage(for: glyph) }

    static func systemImage(for glyph: String?) -> String {
        switch glyph {
        case "scale": "scalemass"
        case "nutrition": "fork.knife"
        case "tape": "ruler"
        case "back": "figure.strengthtraining.traditional"
        case "wave": "chart.line.downtrend.xyaxis"
        case "doc": "doc.text"
        case "check": "checkmark.circle"
        default: "info.circle"
        }
    }

    enum CodingKeys: String, CodingKey {
        case signalID = "id"
        case instanceKey = "instance_key"
        case severity
        case title
        case body
        case note
        case glyph
        case action
        case snoozable
    }
}

struct CoachSignalAction: Codable, Hashable {
    var type: String           // open_measurements | open_next_workout | refresh_recommendation | open_weekly_report | none
    var label: String?
    var target: String?        // e.g. "weight" | "waist" for open_measurements
}

struct CoachSignalsResponse: Codable {
    var ok: Bool?
    var generatedAt: Int?
    var signals: [CoachSignal]?

    enum CodingKeys: String, CodingKey {
        case ok
        case generatedAt = "generated_at"
        case signals
    }
}

struct CoachSignalDismissResponse: Codable {
    var ok: Bool?
}

struct WeeklyReportReadResponse: Codable {
    var ok: Bool?
    var read: Bool?
}

/// The coach recommendation the user applied as today's workout plan.
/// Captured AT APPLY TIME (the backend keeps one mutable recommendations row
/// per user, so the live row may be regenerated between apply and save).
/// Lives in TrainerStore (persisted in UserDefaults) until the workout is saved
/// or the plan is reset. Applying a plan must NOT create real draft sets.
struct AppliedCoachPlan: Codable, Hashable {
    var basedOnWorkoutID: Int?
    var basedOnWorkoutCount: Int?
    var model: String?
    var generatedAt: Int?
    var appliedAt: String
    var focus: String
    var loadType: String
    var exercises: [RecommendedExercise]

    func targets(for exerciseID: Int) -> [RecommendedSet]? {
        exercises.first(where: { $0.exerciseID == exerciseID })?.sets
    }

    /// Lean snapshot for the saved workout: per-exercise notes are display-only
    /// and intentionally stripped (stats only need targets).
    var snapshot: RecommendationSnapshot {
        RecommendationSnapshot(
            schema: 1,
            source: "coach",
            model: model,
            generatedAt: generatedAt,
            appliedAt: appliedAt,
            basedOnWorkoutID: basedOnWorkoutID,
            basedOnWorkoutCount: basedOnWorkoutCount,
            focus: focus,
            loadType: loadType,
            exercises: exercises.map {
                RecommendedExercise(exerciseID: $0.exerciseID, name: $0.name, note: nil, sets: $0.sets)
            }
        )
    }
}

/// Lean copy of the recommendation persisted inside a saved workout's payload
/// (`data.recommendation`) so stats can later compare actual vs recommended.
/// The recommendations table is one-overwritten-row-per-user, so a snapshot —
/// not an id reference — is the only stable link. Generation identity =
/// (based_on_workout_id, generated_at, model).
struct RecommendationSnapshot: Codable, Hashable {
    var schema: Int?
    var source: String?
    var model: String?
    var generatedAt: Int?
    var appliedAt: String?
    var basedOnWorkoutID: Int?
    var basedOnWorkoutCount: Int?
    var focus: String?
    var loadType: String?
    var exercises: [RecommendedExercise]?

    enum CodingKeys: String, CodingKey {
        case schema
        case source
        case model
        case generatedAt = "generated_at"
        case appliedAt = "applied_at"
        case basedOnWorkoutID = "based_on_workout_id"
        case basedOnWorkoutCount = "based_on_workout_count"
        case focus
        case loadType = "load_type"
        case exercises
    }
}

/// Weekly work-set count for one muscle group against the coaching landmarks.
enum VolumeStatus: Equatable { case under, onTarget, over }

struct MuscleGroupVolume: Identifiable {
    var name: String
    var count: Int
    var minTarget: Int
    var maxTarget: Int

    var id: String { name }
    var status: VolumeStatus {
        if count < minTarget { return .under }
        if count > maxTarget { return .over }
        return .onTarget
    }
    /// Bar fill 0…1 against the upper landmark.
    var fill: Double { maxTarget == 0 ? 0 : min(1, Double(count) / Double(maxTarget)) }
}

/// Plan-vs-performed adherence over a set of workouts that carried a coach
/// recommendation snapshot.
struct AdherenceSummary {
    var comparedWorkouts: Int
    var plannedSets: Int
    var doneSets: Int       // capped at planned per exercise
    var skippedExercises: Int
    // Exercises skipped OUTRIGHT (planned, zero sets done), most-skipped first —
    // the same signal the coach uses to adapt plans to real behaviour.
    var skippedByName: [(name: String, count: Int)] = []

    var ratio: Double { plannedSets == 0 ? 0 : Double(doneSets) / Double(plannedSets) }
    var hasData: Bool { comparedWorkouts > 0 && plannedSets > 0 }
}

struct DraftWorkout: Codable, Hashable {
    var workoutDate: String
    var exercises: [DraftExercise]
    var editingWorkoutID: Int?
    var editingClientID: String?
    /// Упражнение, в которое лёг последний добавленный сет — цель для отмены
    /// («убери последний подход») независимо от порядка карточек и суперсетов.
    var lastLoggedExerciseID: Int? = nil
    /// Когда добавили последний сет (unix seconds). По нему голосовой слой
    /// отличает живую сессию от забытого вчерашнего черновика.
    var lastLoggedAt: Double? = nil

    static var empty: DraftWorkout {
        DraftWorkout(
            workoutDate: DateTools.localTodayISO(),
            exercises: [],
            editingWorkoutID: nil,
            editingClientID: nil
        )
    }

    var hasAnyExercise: Bool {
        !exercises.isEmpty
    }

    var hasRealSets: Bool {
        exercises.contains { !$0.sets.isEmpty }
    }
}

struct DraftExercise: Codable, Hashable, Identifiable {
    var exerciseID: Int
    var exerciseName: String
    var sets: [DraftSet]

    var id: Int { exerciseID }
}

struct DraftSet: Codable, Hashable {
    var reps: Int
    var weight: Double
    var effort: SetEffort?
    var notes: String?

    func asWorkoutSet(index: Int) -> WorkoutSet {
        WorkoutSet(
            setIndex: index,
            reps: max(1, reps),
            weight: max(0, weight),
            effort: effort,
            notes: notes?.trimmingCharacters(in: .whitespacesAndNewlines).nilIfBlank
        )
    }
}

struct DraftDisplayExercise: Identifiable, Hashable {
    var exerciseID: Int
    var exerciseName: String
    var sets: [DraftSet]
    var isPreview: Bool

    var id: Int { exerciseID }
}

struct ExercisePlanningContext: Hashable {
    var workoutID: Int?
    var workoutDate: String
    var exerciseName: String
    var previousSets: [WorkoutSet]
    var plannedSets: [WorkoutSet]
    var previousSummary: ExerciseSetSummary
    var plannedSummary: ExerciseSetSummary
    var progressionParts: [ReferenceProgressionPart]
    var maxWeight: Double
}

struct ExerciseSetSummary: Hashable {
    var parts: [String]
    var notes: [String]
    var segments: [ExerciseSetSummarySegment]
}

struct ExerciseSetSummarySegment: Hashable {
    var label: String
    var editSetIndex: Int
    var effort: SetEffort?
    var notes: [String]
    var weight: Double
    var reps: [Int]
}

struct ReferenceProgressionPart: Hashable, Identifiable {
    var previousLabel: String
    var nextLabel: String
    var previousEffort: SetEffort?

    var id: String {
        "\(previousLabel)-\(nextLabel)-\(previousEffort?.rawValue ?? "none")"
    }
}

struct ExercisePickerGroups {
    var primary: [ExerciseDefinition]
    var secondary: [ExerciseDefinition]
    var primaryPoolExhausted: Bool
    var primaryPoolTotal: Int
    var completedPrimaryCount: Int
    var primaryPoolIDs: [Int]
}

struct ProgressPoint: Identifiable, Hashable {
    var workoutID: Int
    var workoutDate: String
    var bestWeight: Double
    var repsAtBestWeight: Int
    var bestReps: Int
    var weightAtBestReps: Double

    var id: String { "\(workoutID)-\(workoutDate)" }
}

struct ExerciseSeriesSummary {
    var firstPoint: ProgressPoint
    var latestPoint: ProgressPoint
    var weightDelta: Double
    var repsDelta: Int
}

struct BodyWeightSummary {
    var totalEntries: Int
    var latestOverallEntry: BodyWeightEntry?
    var latestEntry: BodyWeightEntry?
    var firstEntry: BodyWeightEntry?
    var delta: Double
}

enum BootState: Equatable {
    case idle
    case loading
    case loaded
    case needsSignIn(String?)
    case failed(String)
}

extension String {
    var nilIfBlank: String? {
        let trimmed = trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}

enum DateTools {
    static let isoFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = .current
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()

    static let longFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ru_RU")
        formatter.dateStyle = .long
        formatter.timeStyle = .none
        return formatter
    }()

    static let shortFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ru_RU")
        formatter.setLocalizedDateFormatFromTemplate("d MMM")
        return formatter
    }()

    static let weekdayFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "ru_RU")
        formatter.dateFormat = "EEEE"
        return formatter
    }()

    static func localTodayISO() -> String {
        isoFormatter.string(from: Date())
    }

    static func date(from iso: String) -> Date {
        isoFormatter.date(from: iso) ?? Date()
    }

    static func iso(from date: Date) -> String {
        isoFormatter.string(from: date)
    }

    static func long(_ iso: String) -> String {
        longFormatter.string(from: date(from: iso))
    }

    static func short(_ iso: String) -> String {
        shortFormatter.string(from: date(from: iso)).replacingOccurrences(of: ".", with: "")
    }

    /// Human-readable inclusive period ending at `endISO`, used by cached
    /// reports so the UI never guesses which week a server report covers.
    static func periodLabel(endingAt endISO: String, days: Int) -> String {
        guard let end = isoFormatter.date(from: endISO) else { return endISO }
        let safeDays = max(1, days)
        guard let start = Calendar(identifier: .gregorian).date(
            byAdding: .day,
            value: -(safeDays - 1),
            to: end
        ) else { return short(endISO) }
        return "\(short(iso(from: start))) – \(short(endISO))"
    }

    static func weekday(_ iso: String) -> String {
        let value = weekdayFormatter.string(from: date(from: iso))
        guard let first = value.first else { return value }
        return first.uppercased() + value.dropFirst()
    }
}
