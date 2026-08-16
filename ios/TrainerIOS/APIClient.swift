import Foundation

enum TrainerAPIError: LocalizedError {
    case invalidBaseURL
    case invalidResponse
    case server(status: Int, reason: String)
    case decoding(String)
    case timeout

    var statusCode: Int? {
        switch self {
        case .server(let status, _):
            status
        default:
            nil
        }
    }

    var errorDescription: String? {
        switch self {
        case .invalidBaseURL:
            "Некорректный адрес backend"
        case .invalidResponse:
            "Backend вернул неожиданный ответ"
        case .server(let status, let reason):
            reason.isEmpty ? "Ошибка backend: \(status)" : reason
        case .decoding(let message):
            "Не удалось прочитать ответ backend: \(message)"
        case .timeout:
            "Сервер не ответил вовремя"
        }
    }
}

final class APIClient {
    private let baseURLString: String
    private let session: URLSession
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    init(baseURLString: String, session: URLSession? = nil) {
        self.baseURLString = baseURLString
        self.session = session ?? Self.defaultSession
        self.decoder = JSONDecoder()
        self.encoder = JSONEncoder()
    }

    /// Default session for app traffic. Per-request timeout is intentionally
    /// short (3s) so a stalled backend doesn't park the splash screen on the
    /// loading state for the URLSession default of 60s.
    private static let defaultSession: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 3
        config.timeoutIntervalForResource = 6
        config.waitsForConnectivity = false
        return URLSession(configuration: config)
    }()

    /// Long-running session for the recommendation refresh, which blocks server-side
    /// on a Claude generation. Pass it into a dedicated APIClient instance so it
    /// never touches the aggressive 3s default used by the rest of the app.
    ///
    /// Both values must stay ABOVE the backend's own `ANTHROPIC_TIMEOUT` (120s):
    /// the server keeps generating after the client gives up and stores the
    /// result, so timing out early only costs the user a false error card.
    /// `timeoutIntervalForRequest` is the wait for the first byte — the whole
    /// generation happens before it, so it has to cover the full server call,
    /// not just a network stall.
    static let longRunningSession: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 150
        config.timeoutIntervalForResource = 180
        config.waitsForConnectivity = false
        return URLSession(configuration: config)
    }()

    func resolveSession() async throws -> SessionResolveResponse {
        try await post(
            "/api/session/resolve",
            body: NativeSessionResolveRequest(shell: "ios", nativeUserID: NativeSession.userID)
        )
    }

    func logout() async throws -> SessionLogoutResponse {
        try await post("/api/session/logout", body: EmptyRequest())
    }

    func fetchExercises() async throws -> ExerciseCatalogResponse {
        try await get("/data/exercises.json")
    }

    func fetchWorkouts() async throws -> WorkoutsResponse {
        try await get("/api/workouts")
    }

    func fetchBodyWeights() async throws -> BodyWeightsResponse {
        try await get("/api/body-weights")
    }

    func saveWorkout(_ workout: Workout) async throws -> WorkoutMutationResponse {
        try await post("/api/workouts", body: workout)
    }

    func updateWorkout(id: Int, workout: Workout) async throws -> WorkoutMutationResponse {
        try await put("/api/workouts/\(id)", body: workout)
    }

    func deleteWorkout(id: Int) async throws -> WorkoutMutationResponse {
        try await delete("/api/workouts/\(id)")
    }

    func saveBodyWeight(entryDate: String, weight: Double) async throws
        -> BodyWeightMutationResponse
    {
        try await post(
            "/api/body-weights",
            body: BodyWeightSaveRequest(entryDate: entryDate, weight: weight, notes: nil)
        )
    }

    func deleteBodyWeight(id: Int) async throws -> BodyWeightMutationResponse {
        try await delete("/api/body-weights/\(id)")
    }

    func fetchRecommendation() async throws -> RecommendationResponse {
        try await get("/api/recommendations/next")
    }

    func fetchWeeklyReport() async throws -> WeeklyReportResponse {
        try await get("/api/reports/weekly")
    }

    func markWeeklyReportRead() async throws -> WeeklyReportReadResponse {
        try await request("/api/reports/weekly/read", method: "POST", body: Optional<Data>.none)
    }

    func fetchWaists() async throws -> WaistsResponse {
        try await get("/api/waists")
    }

    func saveWaist(entryDate: String, waist: Double) async throws -> WaistMutationResponse {
        try await post("/api/waists", body: WaistSaveRequest(entryDate: entryDate, waist: waist))
    }

    func deleteWaist(id: Int) async throws -> WaistMutationResponse {
        try await delete("/api/waists/\(id)")
    }

    func fetchCoachSignals() async throws -> CoachSignalsResponse {
        try await get("/api/coach/signals")
    }

    func dismissCoachSignal(instanceKey: String) async throws -> CoachSignalDismissResponse {
        try await post(
            "/api/coach/signals/dismiss", body: SignalDismissRequest(instanceKey: instanceKey))
    }

    /// Force-regenerate. Synchronous on the server (10–40s); construct this client
    /// with `APIClient.longRunningSession` so the call isn't cut at 3s.
    func refreshRecommendation() async throws -> RecommendationResponse {
        try await request("/api/recommendations/refresh", method: "POST", body: Optional<Data>.none)
    }

    private func get<Response: Decodable>(_ path: String) async throws -> Response {
        try await request(path, method: "GET", body: Optional<Data>.none)
    }

    private func post<Body: Encodable, Response: Decodable>(_ path: String, body: Body) async throws
        -> Response
    {
        try await request(path, method: "POST", body: encoder.encode(body))
    }

    private func put<Body: Encodable, Response: Decodable>(_ path: String, body: Body) async throws
        -> Response
    {
        try await request(path, method: "PUT", body: encoder.encode(body))
    }

    private func delete<Response: Decodable>(_ path: String) async throws -> Response {
        try await request(path, method: "DELETE", body: Optional<Data>.none)
    }

    private func request<Response: Decodable>(
        _ path: String,
        method: String,
        body: Data?
    ) async throws -> Response {
        guard let url = makeURL(path) else {
            throw TrainerAPIError.invalidBaseURL
        }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.httpBody = body
            request.setValue("application/json; charset=utf-8", forHTTPHeaderField: "Content-Type")
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch let urlError as URLError where urlError.code == .timedOut {
            throw TrainerAPIError.timeout
        }
        guard let http = response as? HTTPURLResponse else {
            throw TrainerAPIError.invalidResponse
        }

        guard (200..<300).contains(http.statusCode) else {
            let reason = (try? decoder.decode(APIReasonResponse.self, from: data).reason) ?? ""
            throw TrainerAPIError.server(status: http.statusCode, reason: reason)
        }

        do {
            return try decoder.decode(Response.self, from: data)
        } catch {
            throw TrainerAPIError.decoding(error.localizedDescription)
        }
    }

    private func makeURL(_ path: String) -> URL? {
        let trimmedBase = baseURLString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var base = URL(string: trimmedBase), !trimmedBase.isEmpty else {
            return nil
        }

        if base.path.hasSuffix("/") {
            base.deleteLastPathComponent()
        }

        return base.appendingPathComponent(
            path.trimmingCharacters(in: CharacterSet(charactersIn: "/")))
    }
}

private enum NativeSession {
    static let userID = 3
}

private struct NativeSessionResolveRequest: Encodable {
    var shell: String
    var nativeUserID: Int

    enum CodingKeys: String, CodingKey {
        case shell
        case nativeUserID = "native_user_id"
    }
}

private struct EmptyRequest: Encodable {}

private struct BodyWeightSaveRequest: Encodable {
    var entryDate: String
    var weight: Double
    var notes: String?

    enum CodingKeys: String, CodingKey {
        case entryDate = "entry_date"
        case weight
        case notes
    }
}

private struct WaistSaveRequest: Encodable {
    var entryDate: String
    var waist: Double

    enum CodingKeys: String, CodingKey {
        case entryDate = "entry_date"
        case waist
    }
}

private struct SignalDismissRequest: Encodable {
    var instanceKey: String

    enum CodingKeys: String, CodingKey {
        case instanceKey = "instance_key"
    }
}
