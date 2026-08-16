import XCTest

@testable import TrainerIOS

final class APIClientTests: XCTestCase {
    private var protocolType: MockURLProtocol.Type { MockURLProtocol.self }

    override func setUp() {
        super.setUp()
        protocolType.reset()
    }

    override func tearDown() {
        protocolType.reset()
        super.tearDown()
    }

    func testAPIClientUsesAllREADMEEndpointPathsAndHTTPMethods() async throws {
        let client = makeClient()

        protocolType.enqueue(
            json:
                #"{"ok":true,"user":{"id":1,"auth_source":"debug","telegram_user_id":null,"username":null,"first_name":"Browser","last_name":"Debug","debug_alias":"browser-default","is_default_debug_user":true,"display_name":"Browser Debug"},"auth_mode":"debug"}"#
        )
        _ = try await client.resolveSession()

        protocolType.enqueue(json: #"{"exercises":[]}"#)
        _ = try await client.fetchExercises()

        protocolType.enqueue(json: #"{"ok":true,"user":null,"workouts":[]}"#)
        _ = try await client.fetchWorkouts()

        protocolType.enqueue(json: #"{"ok":true,"user":null,"entries":[]}"#)
        _ = try await client.fetchBodyWeights()

        let workout = TestFixtures.workout(
            id: nil,
            clientID: "client-a",
            date: "2026-05-10",
            exercises: [
                TestFixtures.exercise(id: 8, name: "Жим ногами", sets: [TestFixtures.set()])
            ]
        )

        protocolType.enqueue(json: workoutMutationJSON(id: 10, created: true))
        _ = try await client.saveWorkout(workout)

        protocolType.enqueue(json: workoutMutationJSON(id: 10, created: nil))
        _ = try await client.updateWorkout(id: 10, workout: workout)

        protocolType.enqueue(json: workoutMutationJSON(id: 10, deleted: true))
        _ = try await client.deleteWorkout(id: 10)

        protocolType.enqueue(json: bodyWeightMutationJSON(id: 3, created: true))
        _ = try await client.saveBodyWeight(entryDate: "2026-05-10", weight: 81.9)

        protocolType.enqueue(json: bodyWeightMutationJSON(id: 3, deleted: true))
        _ = try await client.deleteBodyWeight(id: 3)

        protocolType.enqueue(
            json: #"{"ok":true,"status":"none","recommendation":null,"stale":false}"#)
        _ = try await client.fetchRecommendation()

        protocolType.enqueue(json: recommendationJSON())
        _ = try await client.refreshRecommendation()

        protocolType.enqueue(json: #"{"ok":true}"#)
        _ = try await client.logout()

        XCTAssertEqual(
            protocolType.requests.map(\.httpMethod),
            [
                "POST",
                "GET",
                "GET",
                "GET",
                "POST",
                "PUT",
                "DELETE",
                "POST",
                "DELETE",
                "GET",
                "POST",
                "POST",
            ])
        XCTAssertEqual(
            protocolType.requests.compactMap(\.url?.path),
            [
                "/api/session/resolve",
                "/data/exercises.json",
                "/api/workouts",
                "/api/body-weights",
                "/api/workouts",
                "/api/workouts/10",
                "/api/workouts/10",
                "/api/body-weights",
                "/api/body-weights/3",
                "/api/recommendations/next",
                "/api/recommendations/refresh",
                "/api/session/logout",
            ])
    }

    func testRefreshRecommendationDecodesReadyPayload() async throws {
        let client = makeClient()
        protocolType.enqueue(json: recommendationJSON())

        let response = try await client.refreshRecommendation()

        XCTAssertEqual(response.status, "ready")
        XCTAssertEqual(response.stale, false)
        XCTAssertEqual(response.basedOnWorkoutCount, 56)
        XCTAssertEqual(response.recommendation?.loadType, "medium")
        XCTAssertEqual(response.recommendation?.exercises.first?.exerciseID, 8)
        XCTAssertEqual(response.recommendation?.exercises.first?.note, "мягкий вход")
        XCTAssertEqual(response.recommendation?.exercises.first?.sets.first?.weight, 90)
        XCTAssertEqual(protocolType.requests.first?.httpMethod, "POST")
        XCTAssertEqual(protocolType.requests.first?.url?.path, "/api/recommendations/refresh")
    }

    func testAPIClientSendsIOSSessionResolveAndBodyWeightPayloads() async throws {
        let client = makeClient()

        protocolType.enqueue(json: #"{"ok":true,"user":null,"auth_mode":"debug"}"#)
        _ = try await client.resolveSession()
        protocolType.enqueue(json: bodyWeightMutationJSON(id: 1, created: true))
        _ = try await client.saveBodyWeight(entryDate: "2026-05-10", weight: 81.95)

        let sessionBody = try XCTUnwrap(protocolType.requests[0].httpBody)
        let sessionObject = try JSONSerialization.jsonObject(with: sessionBody) as? [String: Any]
        let weightBody = try XCTUnwrap(protocolType.requests[1].httpBody)
        let weightObject = try JSONSerialization.jsonObject(with: weightBody) as? [String: Any]

        XCTAssertEqual(sessionObject?["shell"] as? String, "ios")
        XCTAssertEqual(sessionObject?["native_user_id"] as? Int, 3)
        XCTAssertEqual(weightObject?["entry_date"] as? String, "2026-05-10")
        XCTAssertEqual(weightObject?["weight"] as? Double, 81.95)
        XCTAssertNil(weightObject?["notes"] as? String)
    }

    func testAPIClientSurfacesBackendReasonOnFailure() async throws {
        let client = makeClient()
        protocolType.enqueue(
            status: 400, json: #"{"ok":false,"reason":"Set effort must be one of easy, ok, hard"}"#)

        do {
            _ = try await client.fetchWorkouts() as WorkoutsResponse
            XCTFail("Expected backend error")
        } catch {
            XCTAssertEqual(error.localizedDescription, "Set effort must be one of easy, ok, hard")
        }
    }

    private func makeClient() -> APIClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockURLProtocol.self]
        let session = URLSession(configuration: config)
        return APIClient(baseURLString: "https://trainer.test", session: session)
    }

    private func workoutMutationJSON(id: Int, created: Bool? = nil, deleted: Bool? = nil) -> String
    {
        var fields = [#""ok":true"#]
        if let created {
            fields.append(#""created":\#(created)"#)
        }
        if let deleted {
            fields.append(#""deleted":\#(deleted)"#)
        }
        fields.append(
            #"""
            "workout":{
              "id":\#(id),
              "client_id":"client-a",
              "workout_date":"2026-05-10",
              "plan_id":null,
              "created_at":100,
              "updated_at":100,
              "data":{
                "focus":null,
                "notes":null,
                "load_type":"light",
                "exercises":[
                  {
                    "exercise_id":8,
                    "name":"Жим ногами",
                    "sets":[{"set_index":1,"reps":12,"weight":80,"effort":null,"notes":null}]
                  }
                ]
              }
            }
            """#
        )
        return "{\(fields.joined(separator: ","))}"
    }

    private func recommendationJSON() -> String {
        #"""
        {
          "ok": true,
          "status": "ready",
          "stale": false,
          "based_on_workout_count": 56,
          "model": "claude-opus-4-8",
          "error": null,
          "recommendation": {
            "focus": "Сбалансированная тренировка",
            "load_type": "medium",
            "rationale": "После перерыва беру среднюю нагрузку.",
            "exercises": [
              {"exercise_id": 8, "name": "Жим ногами", "note": "мягкий вход",
               "sets": [{"reps": 12, "weight": 90}, {"reps": 12, "weight": 90}]}
            ]
          }
        }
        """#
    }

    private func bodyWeightMutationJSON(id: Int, created: Bool? = nil, deleted: Bool? = nil)
        -> String
    {
        var fields = [#""ok":true"#]
        if let created {
            fields.append(#""created":\#(created)"#)
        }
        if let deleted {
            fields.append(#""deleted":\#(deleted)"#)
        }
        fields.append(
            #"""
            "entry":{
              "id":\#(id),
              "user_id":1,
              "entry_date":"2026-05-10",
              "weight":81.95,
              "notes":null,
              "created_at":100,
              "updated_at":100
            }
            """#
        )
        return "{\(fields.joined(separator: ","))}"
    }
}

private final class MockURLProtocol: URLProtocol {
    struct Response {
        var status: Int
        var data: Data
    }

    static var queuedResponses: [Response] = []
    static var requests: [URLRequest] = []

    static func enqueue(status: Int = 200, json: String) {
        queuedResponses.append(Response(status: status, data: Data(json.utf8)))
    }

    static func reset() {
        queuedResponses = []
        requests = []
    }

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        var capturedRequest = request
        if capturedRequest.httpBody == nil {
            capturedRequest.httpBody = Self.bodyData(from: request)
        }
        Self.requests.append(capturedRequest)
        let response =
            Self.queuedResponses.isEmpty
            ? Response(status: 500, data: Data(#"{"ok":false,"reason":"No mock response"}"#.utf8))
            : Self.queuedResponses.removeFirst()
        let http = HTTPURLResponse(
            url: request.url!,
            statusCode: response.status,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json; charset=utf-8"]
        )!
        client?.urlProtocol(self, didReceive: http, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: response.data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static func bodyData(from request: URLRequest) -> Data? {
        guard let stream = request.httpBodyStream else {
            return nil
        }

        stream.open()
        defer { stream.close() }

        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count < 0 {
                return nil
            }
            if count == 0 {
                break
            }
            data.append(buffer, count: count)
        }
        return data
    }
}
