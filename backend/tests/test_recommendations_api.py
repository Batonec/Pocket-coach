from __future__ import annotations

import tempfile
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from support import (
    JsonHttpClient,
    RunningMiniApp,
    running_miniapp_server,
    sample_workout_payload,
    temporary_env,
)

from trainer import backend_store  # support (imported below) puts backend on sys.path

FAKE_REC: dict[str, Any] = {
    "focus": "Тест",
    "load_type": "medium",
    "rationale": "обоснование",
    "exercises": [
        {"exercise_id": 1, "name": "Bench Press", "note": "n", "sets": [{"reps": 12, "weight": 80}]}
    ],
}


def _fake_generate(workouts, body_weights, catalog, **kwargs):
    return FAKE_REC, {"input_tokens": 100, "output_tokens": 50}, "claude-test"


class RecommendationsAPITests(unittest.TestCase):
    @contextmanager
    def _server(
        self,
        *,
        auto_trigger: bool = False,
        generate=None,
        raises: str | None = None,
    ) -> Iterator[RunningMiniApp]:
        # Drop the manual-refresh debounce so sequential calls in a test don't race it.
        with (
            temporary_env({"RECOMMENDATION_REFRESH_MIN_INTERVAL": "0"}),
            running_miniapp_server() as running,
        ):
            module = running.module
            orig_generate = module.recommender.generate
            orig_trigger = module.trigger_recommendation_async
            try:
                if not auto_trigger:
                    module.trigger_recommendation_async = lambda *a, **k: None
                if raises is not None:

                    def boom(*a, **k):
                        raise module.recommender.RecommendationError(raises)

                    module.recommender.generate = boom
                else:
                    module.recommender.generate = generate or _fake_generate
                yield running
            finally:
                module.recommender.generate = orig_generate
                module.trigger_recommendation_async = orig_trigger

    def test_next_returns_none_without_recommendation(self) -> None:
        with self._server() as running:
            client = JsonHttpClient(running.base_url)
            res = client.request_json("GET", "/api/recommendations/next")
            self.assertEqual(res.status, 200)
            self.assertEqual(res.payload["status"], "none")
            self.assertIsNone(res.payload["recommendation"])
            self.assertFalse(res.payload["stale"])

    def test_refresh_generates_then_next_reads_cached_ready(self) -> None:
        with self._server() as running:
            client = JsonHttpClient(running.base_url)
            client.request_json("POST", "/api/workouts", sample_workout_payload(client_id="w1"))

            res = client.request_json("POST", "/api/recommendations/refresh")
            self.assertEqual(res.status, 200)
            self.assertEqual(res.payload["status"], "ready")
            self.assertEqual(res.payload["recommendation"]["focus"], "Тест")
            self.assertFalse(res.payload["stale"])

            nxt = client.request_json("GET", "/api/recommendations/next")
            self.assertEqual(nxt.payload["status"], "ready")
            self.assertFalse(nxt.payload["stale"])
            self.assertEqual(nxt.payload["recommendation"]["exercises"][0]["exercise_id"], 1)

    def test_stale_flag_set_after_newer_workout(self) -> None:
        with self._server() as running:  # auto_trigger off → recommendation won't regenerate
            client = JsonHttpClient(running.base_url)
            client.request_json(
                "POST",
                "/api/workouts",
                sample_workout_payload(client_id="w1", workout_date="2026-03-20"),
            )
            client.request_json("POST", "/api/recommendations/refresh")
            client.request_json(
                "POST",
                "/api/workouts",
                sample_workout_payload(client_id="w2", workout_date="2026-03-28"),
            )

            nxt = client.request_json("GET", "/api/recommendations/next")
            self.assertEqual(nxt.payload["status"], "ready")
            self.assertTrue(nxt.payload["stale"])

    def test_refresh_failure_returns_502_with_reason(self) -> None:
        with self._server(raises="Claude API не ответил вовремя") as running:
            client = JsonHttpClient(running.base_url)
            client.request_json("POST", "/api/workouts", sample_workout_payload(client_id="w1"))
            res = client.request_json("POST", "/api/recommendations/refresh")
            self.assertEqual(res.status, 502)
            self.assertFalse(res.payload["ok"])
            self.assertIn("вовремя", res.payload["reason"])

    def test_failed_manual_refresh_can_be_retried_immediately(self) -> None:
        calls = 0

        def flaky_generate(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary generation failure")
            return _fake_generate(*args, **kwargs)

        with self._server(generate=flaky_generate) as running:
            running.module.REFRESH_MIN_INTERVAL = 10
            client = JsonHttpClient(running.base_url)
            client.request_json("POST", "/api/workouts", sample_workout_payload(client_id="w1"))

            first = client.request_json("POST", "/api/recommendations/refresh")
            second = client.request_json("POST", "/api/recommendations/refresh")

            self.assertEqual(first.status, 502)
            self.assertEqual(second.status, 200)
            self.assertEqual(second.payload["status"], "ready")
            self.assertEqual(calls, 2)

    def test_workout_save_auto_triggers_generation(self) -> None:
        with self._server(auto_trigger=True) as running:
            client = JsonHttpClient(running.base_url)
            client.request_json("POST", "/api/workouts", sample_workout_payload(client_id="w1"))

            status = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = client.request_json("GET", "/api/recommendations/next").payload.get(
                    "status"
                )
                if status == "ready":
                    break
                time.sleep(0.1)
            self.assertEqual(status, "ready")

    def test_workout_delete_regenerates_from_remaining_history(self) -> None:
        def history_generate(workouts, body_weights, catalog, **kwargs):
            recommendation = dict(FAKE_REC)
            recommendation["focus"] = f"workouts={len(workouts)}"
            return recommendation, {"input_tokens": 10, "output_tokens": 5}, "claude-test"

        with self._server(auto_trigger=True, generate=history_generate) as running:
            client = JsonHttpClient(running.base_url)
            client.request_json("POST", "/api/workouts", sample_workout_payload(client_id="w1"))
            second = client.request_json(
                "POST", "/api/workouts", sample_workout_payload(client_id="w2")
            ).payload["workout"]

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                payload = client.request_json("GET", "/api/recommendations/next").payload
                if payload.get("status") == "ready" and payload.get("based_on_workout_count") == 2:
                    break
                time.sleep(0.05)

            client.request_json("DELETE", f"/api/workouts/{second['id']}")

            result = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                payload = client.request_json("GET", "/api/recommendations/next").payload
                if payload.get("status") == "ready" and payload.get("based_on_workout_count") == 1:
                    result = payload
                    break
                time.sleep(0.05)

            self.assertIsNotNone(result)
            self.assertEqual(result["recommendation"]["focus"], "workouts=1")
            self.assertFalse(result["stale"])

    def test_deleting_last_workout_clears_cached_recommendation(self) -> None:
        with self._server(auto_trigger=True) as running:
            client = JsonHttpClient(running.base_url)
            workout = client.request_json(
                "POST", "/api/workouts", sample_workout_payload(client_id="only-workout")
            ).payload["workout"]

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                payload = client.request_json("GET", "/api/recommendations/next").payload
                if payload.get("status") == "ready":
                    break
                time.sleep(0.05)

            client.request_json("DELETE", f"/api/workouts/{workout['id']}")

            status = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = client.request_json("GET", "/api/recommendations/next").payload.get(
                    "status"
                )
                if status == "none":
                    break
                time.sleep(0.05)
            self.assertEqual(status, "none")

    def test_measurements_regenerate_once_more_with_latest_weight_and_waist(self) -> None:
        def measurement_generate(workouts, body_weights, catalog, **kwargs):
            # Keep the first measurement generation in flight long enough for
            # the waist mutation to request a coalesced follow-up pass.
            time.sleep(0.15)
            recommendation = dict(FAKE_REC)
            recommendation["focus"] = (
                f"weights={len(body_weights)},waists={len(kwargs.get('waists') or [])}"
            )
            return recommendation, {"input_tokens": 10, "output_tokens": 5}, "claude-test"

        with self._server(auto_trigger=True, generate=measurement_generate) as running:
            client = JsonHttpClient(running.base_url)
            client.request_json("POST", "/api/workouts", sample_workout_payload(client_id="w1"))

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                payload = client.request_json("GET", "/api/recommendations/next").payload
                if payload.get("status") == "ready":
                    break
                time.sleep(0.05)

            client.request_json(
                "POST",
                "/api/body-weights",
                {"entry_date": "2026-08-14", "weight": 79.0},
            )
            client.request_json(
                "POST",
                "/api/waists",
                {"entry_date": "2026-08-14", "waist": 84.0},
            )

            focus = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                payload = client.request_json("GET", "/api/recommendations/next").payload
                if payload.get("status") == "ready":
                    focus = payload["recommendation"]["focus"]
                    if focus == "weights=1,waists=1":
                        break
                time.sleep(0.05)
            self.assertEqual(focus, "weights=1,waists=1")


class RecommendationStoreTests(unittest.TestCase):
    def _store(self) -> tuple[backend_store.MiniAppStore, int]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = backend_store.MiniAppStore(Path(tmp.name) / "trainer.db")
        user = store.ensure_debug_user("rec-test-user")
        return store, int(user["id"])

    def test_recommendation_lifecycle(self) -> None:
        store, uid = self._store()
        self.assertIsNone(store.get_latest_workout_id(uid))
        self.assertIsNone(store.get_recommendation(uid))

        store.save_workout(uid, sample_workout_payload(client_id="w1", workout_date="2026-03-20"))
        workout_id = store.get_latest_workout_id(uid)
        self.assertIsNotNone(workout_id)

        store.set_recommendation_pending(uid)
        self.assertEqual(store.get_recommendation(uid)["status"], "pending")

        row = store.save_recommendation(uid, workout_id, 1, "model-x", FAKE_REC, 10, 5)
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["based_on_workout_id"], workout_id)
        self.assertEqual(row["recommendation"]["focus"], "Тест")
        self.assertEqual(row["input_tokens"], 10)

        store.fail_recommendation(uid, "boom")
        failed = store.get_recommendation(uid)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "boom")

        store.clear_recommendation(uid)
        self.assertIsNone(store.get_recommendation(uid))


if __name__ == "__main__":
    unittest.main()
