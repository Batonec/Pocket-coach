"""HTTP API через живой сервер: health, сессии (debug и iOS), тренировки и замеры
по REST, недельный отчёт из кэша, таблица маршрутов.
"""

from __future__ import annotations

import unittest

from support import (
    JsonHttpClient,
    running_miniapp_server,
    sample_body_weight_payload,
    sample_workout_payload,
)


class ServerApiTest(unittest.TestCase):
    """Сессии, тренировки и взвешивания через REST."""

    def test_health_endpoint_reports_runtime_flags(self) -> None:
        with running_miniapp_server(allow_debug_user=True, dev_mode=True) as app:
            client = JsonHttpClient(app.base_url)

            response = client.request_json("GET", "/api/health")

            self.assertEqual(response.status, 200)
            self.assertTrue(response.payload["ok"])
            self.assertTrue(response.payload["debug_user_enabled"])
            self.assertIn("trainer.db", response.payload["db_path"])

    def test_session_resolve_returns_debug_user_when_debug_is_enabled(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)

            response = client.request_json("POST", "/api/session/resolve", {})

            self.assertEqual(response.status, 200)
            self.assertEqual(response.payload["auth_mode"], "debug")
            self.assertTrue(response.payload["user"]["is_default_debug_user"])
            self.assertIn("trainer_session=", response.headers.get("Set-Cookie", ""))

    def test_session_resolve_requires_auth_when_debug_is_disabled(self) -> None:
        with running_miniapp_server(allow_debug_user=False) as app:
            client = JsonHttpClient(app.base_url)

            response = client.request_json("POST", "/api/session/resolve", {})

            self.assertEqual(response.status, 401)
            self.assertIn("No active session", response.payload["reason"])

    def test_session_resolve_reuses_existing_cookie_when_initdata_is_missing(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)

            first_response = client.request_json("POST", "/api/session/resolve", {})
            second_response = client.request_json("POST", "/api/session/resolve", {})

            self.assertEqual(first_response.status, 200)
            self.assertEqual(second_response.status, 200)
            self.assertEqual(
                first_response.payload["user"]["id"], second_response.payload["user"]["id"]
            )
            self.assertNotIn("auth_mode", second_response.payload)

    def test_session_resolve_ios_can_bind_to_fixed_native_user_id(self) -> None:
        with running_miniapp_server(allow_debug_user=False) as app:
            app.module.STORE.ensure_debug_user("first-local-user", "First", "Local")
            app.module.STORE.ensure_debug_user("second-local-user", "Second", "Local")
            fixed_user = app.module.STORE.ensure_debug_user("ios-fixed-user", "Native", "Fixed")
            self.assertEqual(fixed_user["id"], 3)
            client = JsonHttpClient(app.base_url)

            response = client.request_json(
                "POST",
                "/api/session/resolve",
                {"shell": "ios", "native_user_id": 3},
            )
            workouts_response = client.request_json("GET", "/api/workouts")

            self.assertEqual(response.status, 200)
            self.assertEqual(response.payload["auth_mode"], "ios_fixed_user")
            self.assertEqual(response.payload["user"]["id"], 3)
            self.assertEqual(response.payload["user"]["display_name"], "Native Fixed")
            self.assertIn("trainer_session=", response.headers.get("Set-Cookie", ""))
            self.assertEqual(workouts_response.status, 200)
            self.assertEqual(workouts_response.payload["user"]["id"], 3)

    def test_session_resolve_ios_reports_missing_fixed_native_user(self) -> None:
        with running_miniapp_server(allow_debug_user=False) as app:
            client = JsonHttpClient(app.base_url)

            response = client.request_json(
                "POST",
                "/api/session/resolve",
                {"shell": "ios", "native_user_id": 3},
            )

            self.assertEqual(response.status, 401)
            self.assertIn("user #3", response.payload["reason"])

    def test_get_workouts_uses_debug_fallback_cookie_when_enabled(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)

            response = client.request_json("GET", "/api/workouts")

            self.assertEqual(response.status, 200)
            self.assertEqual(response.payload["user"]["auth_source"], "debug")
            self.assertIn("trainer_session=", response.headers.get("Set-Cookie", ""))

    def test_get_workouts_requires_session_when_debug_is_disabled(self) -> None:
        with running_miniapp_server(allow_debug_user=False) as app:
            client = JsonHttpClient(app.base_url)

            response = client.request_json("GET", "/api/workouts")

            self.assertEqual(response.status, 401)
            self.assertIn("No active session", response.payload["reason"])

    def test_get_body_weights_uses_debug_fallback_cookie_when_enabled(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)

            response = client.request_json("GET", "/api/body-weights")

            self.assertEqual(response.status, 200)
            self.assertEqual(response.payload["user"]["auth_source"], "debug")
            self.assertEqual(response.payload["entries"], [])

    def test_get_body_weights_requires_session_when_debug_is_disabled(self) -> None:
        with running_miniapp_server(allow_debug_user=False) as app:
            client = JsonHttpClient(app.base_url)

            response = client.request_json("GET", "/api/body-weights")

            self.assertEqual(response.status, 401)
            self.assertIn("No active session", response.payload["reason"])

    def test_workouts_endpoint_rejects_invalid_payload(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            response = client.request_json(
                "POST",
                "/api/workouts",
                {
                    "client_id": "invalid-workout",
                    "workout_date": "2026-03-28",
                    "plan_id": None,
                    "data": {"notes": None, "load_type": None, "exercises": []},
                },
            )

            self.assertEqual(response.status, 400)
            self.assertIn("at least one exercise", response.payload["reason"])

    def test_workouts_endpoint_is_idempotent_via_api_for_same_client_id(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            first_response = client.request_json(
                "POST",
                "/api/workouts",
                sample_workout_payload(client_id="api-idempotent"),
            )
            second_response = client.request_json(
                "POST",
                "/api/workouts",
                sample_workout_payload(client_id="api-idempotent"),
            )
            workouts_response = client.request_json("GET", "/api/workouts")

            self.assertEqual(first_response.status, 201)
            self.assertEqual(second_response.status, 200)
            self.assertFalse(second_response.payload["created"])
            self.assertEqual(
                first_response.payload["workout"]["id"], second_response.payload["workout"]["id"]
            )
            self.assertEqual(len(workouts_response.payload["workouts"]), 1)

    def test_workouts_endpoint_updates_existing_workout_via_put(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            created_response = client.request_json(
                "POST",
                "/api/workouts",
                sample_workout_payload(
                    client_id="editable-api-workout",
                    workout_date="2026-03-18",
                    exercise_id=4,
                    exercise_name="Pull Up",
                    weight=10,
                    reps=8,
                ),
            )

            updated_response = client.request_json(
                "PUT",
                f"/api/workouts/{created_response.payload['workout']['id']}",
                sample_workout_payload(
                    client_id="ignored-client-id",
                    workout_date="2026-03-27",
                    exercise_id=4,
                    exercise_name="Pull Up",
                    weight=20,
                    reps=10,
                    effort="hard",
                    notes=" Updated pull-up day ",
                ),
            )

            workouts_response = client.request_json("GET", "/api/workouts")

            self.assertEqual(updated_response.status, 200)
            self.assertEqual(
                updated_response.payload["workout"]["client_id"], "editable-api-workout"
            )
            self.assertEqual(updated_response.payload["workout"]["workout_date"], "2026-03-27")
            self.assertEqual(
                updated_response.payload["workout"]["data"]["notes"], "Updated pull-up day"
            )
            self.assertEqual(
                workouts_response.payload["workouts"][0]["data"]["exercises"][0]["sets"][0][
                    "weight"
                ],
                20,
            )
            self.assertEqual(
                workouts_response.payload["workouts"][0]["data"]["exercises"][0]["sets"][0][
                    "effort"
                ],
                "hard",
            )

    def test_workouts_endpoint_rejects_invalid_set_effort(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            response = client.request_json(
                "POST",
                "/api/workouts",
                sample_workout_payload(
                    client_id="invalid-effort-api-workout",
                    exercise_id=4,
                    exercise_name="Pull Up",
                    effort="impossible",
                ),
            )

            self.assertEqual(response.status, 400)
            self.assertIn("Set effort must be one of easy, ok, hard", response.payload["reason"])

    def test_workouts_endpoint_returns_not_found_for_missing_update(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            response = client.request_json(
                "PUT",
                "/api/workouts/9999",
                sample_workout_payload(client_id="missing-update"),
            )

            self.assertEqual(response.status, 404)
            self.assertIn("Workout not found", response.payload["reason"])

    def test_workouts_endpoint_deletes_existing_workout_via_delete(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            created_response = client.request_json(
                "POST",
                "/api/workouts",
                sample_workout_payload(
                    client_id="delete-api-workout",
                    exercise_id=6,
                    exercise_name="Squat",
                ),
            )

            delete_response = client.request_json(
                "DELETE",
                f"/api/workouts/{created_response.payload['workout']['id']}",
            )
            workouts_response = client.request_json("GET", "/api/workouts")

            self.assertEqual(delete_response.status, 200)
            self.assertTrue(delete_response.payload["deleted"])
            self.assertEqual(
                delete_response.payload["workout"]["id"], created_response.payload["workout"]["id"]
            )
            self.assertEqual(workouts_response.payload["workouts"], [])

    def test_workouts_endpoint_returns_not_found_for_missing_delete(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            response = client.request_json("DELETE", "/api/workouts/9999")

            self.assertEqual(response.status, 404)
            self.assertIn("Workout not found", response.payload["reason"])

    def test_body_weights_endpoint_saves_and_updates_entries_via_api(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            created_response = client.request_json(
                "POST",
                "/api/body-weights",
                sample_body_weight_payload(entry_date="2026-03-27", weight=82.4),
            )
            updated_response = client.request_json(
                "POST",
                "/api/body-weights",
                sample_body_weight_payload(entry_date="2026-03-27", weight=81.9, notes=" Updated "),
            )
            list_response = client.request_json("GET", "/api/body-weights")

            self.assertEqual(created_response.status, 201)
            self.assertTrue(created_response.payload["created"])
            self.assertEqual(updated_response.status, 200)
            self.assertFalse(updated_response.payload["created"])
            self.assertEqual(updated_response.payload["entry"]["weight"], 81.9)
            self.assertEqual(updated_response.payload["entry"]["notes"], "Updated")
            self.assertEqual(len(list_response.payload["entries"]), 1)

    def test_body_weights_endpoint_rejects_invalid_payload(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            response = client.request_json(
                "POST",
                "/api/body-weights",
                {
                    "entry_date": "2026-03-28",
                    "weight": 0,
                },
            )

            self.assertEqual(response.status, 400)
            self.assertIn("greater than 0", response.payload["reason"])

    def test_body_weights_endpoint_deletes_entry_via_api(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            created_response = client.request_json(
                "POST",
                "/api/body-weights",
                sample_body_weight_payload(entry_date="2026-03-27", weight=82.4),
            )
            delete_response = client.request_json(
                "DELETE",
                f"/api/body-weights/{created_response.payload['entry']['id']}",
            )
            list_response = client.request_json("GET", "/api/body-weights")

            self.assertEqual(delete_response.status, 200)
            self.assertTrue(delete_response.payload["deleted"])
            self.assertEqual(delete_response.payload["entry"]["entry_date"], "2026-03-27")
            self.assertEqual(list_response.payload["entries"], [])

    def test_body_weights_endpoint_returns_not_found_for_missing_delete(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            response = client.request_json("DELETE", "/api/body-weights/999999")

            self.assertEqual(response.status, 404)
            self.assertIn("not found", response.payload["reason"].lower())

    def test_workouts_endpoint_orders_same_day_entries_from_newest_to_oldest(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            first_save = client.request_json(
                "POST",
                "/api/workouts",
                sample_workout_payload(
                    client_id="workout-a",
                    exercise_id=6,
                    exercise_name="Squat",
                ),
            )
            second_save = client.request_json(
                "POST",
                "/api/workouts",
                sample_workout_payload(
                    client_id="workout-b",
                    exercise_id=4,
                    exercise_name="Pull Up",
                ),
            )
            workouts_response = client.request_json("GET", "/api/workouts")

            self.assertEqual(first_save.status, 201)
            self.assertEqual(second_save.status, 201)
            self.assertEqual(workouts_response.status, 200)
            self.assertEqual(
                [
                    workout["data"]["exercises"][0]["name"]
                    for workout in workouts_response.payload["workouts"]
                ],
                ["Pull Up", "Squat"],
            )


if __name__ == "__main__":
    unittest.main()


class WeeklyReportEndpointTest(unittest.TestCase):
    """Эндпоинт недельного отчёта отдаёт только кэш."""

    def test_weekly_report_serves_cache_only(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            empty = client.request_json("GET", "/api/reports/weekly")
            self.assertEqual(empty.status, 200)
            self.assertTrue(empty.payload["ok"])
            self.assertIsNone(empty.payload["report"])

            user = app.module.STORE.ensure_debug_user("browser-default")
            app.module.STORE.save_coach_report(
                user["id"], "2026-08-09", 7, "**Итоги** старая", "m", 1, 2
            )
            app.module.STORE.save_coach_report(
                user["id"], "2026-08-16", 7, "**Итоги** свежая", "m", 1, 2
            )

            cached = client.request_json("GET", "/api/reports/weekly")
            self.assertEqual(cached.status, 200)
            self.assertEqual(cached.payload["report"]["report"], "**Итоги** свежая")
            self.assertEqual(cached.payload["report"]["period_end"], "2026-08-16")

    def test_weekly_report_requires_session(self) -> None:
        with running_miniapp_server(allow_debug_user=False) as app:
            client = JsonHttpClient(app.base_url)
            response = client.request_json("GET", "/api/reports/weekly")
            self.assertEqual(response.status, 401)


class RoutingTest(unittest.TestCase):
    """Диспетчер по таблице маршрутов: то, что раньше держали хвосты цепочек
    if — 404 на чужой путь, на id не-число и на лишний сегмент — обязано
    остаться на месте."""

    def test_unknown_api_path_is_not_found_for_every_method(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)

            for method, payload in (("GET", None), ("POST", {}), ("PUT", {}), ("DELETE", None)):
                response = client.request_json(method, "/api/nowhere", payload)

                self.assertEqual(response.status, 404, method)
                self.assertEqual(response.payload, {"ok": False, "reason": "Not found"}, method)

    def test_id_routes_reject_non_numeric_and_nested_ids(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)

            self.assertEqual(client.request_json("PUT", "/api/workouts/abc", {}).status, 404)
            self.assertEqual(client.request_json("DELETE", "/api/workouts/1/extra").status, 404)
            self.assertEqual(client.request_json("DELETE", "/api/events/").status, 404)

    def test_id_routes_exist_only_for_the_methods_that_declare_them(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)

            # У GET нет эндпоинтов по id, а PUT есть только у тренировок и событий.
            self.assertEqual(client.request_json("GET", "/api/workouts/1").status, 404)
            self.assertEqual(client.request_json("PUT", "/api/waists/1", {}).status, 404)

    def test_exercise_catalog_is_served_on_the_legacy_url(self) -> None:
        """Файл переехал в resources/, адрес остался: он зашит в iOS-клиент. Другой
        статики сервер больше не отдаёт — веб-версии нет с июня 2026."""
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)

            catalog = client.request_json("GET", "/data/exercises.json")

            self.assertEqual(catalog.status, 200)
            self.assertTrue(catalog.payload["exercises"])
            self.assertEqual(client.request_json("GET", "/index.html").status, 404)
