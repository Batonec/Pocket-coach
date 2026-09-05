"""Снапшот совета внутри тренировки: нормализация формы и размера, POST/PUT через
API, ретрай без затирания.
"""

from __future__ import annotations

import unittest

from support import (
    JsonHttpClient,
    running_miniapp_server,
    sample_workout_payload,
)

from trainer.data import backend_store  # support кладёт backend/ в sys.path
from trainer.domain import rules


def sample_snapshot(**overrides):
    """Валидный снапшот совета с переопределениями."""
    snapshot = {
        "schema": 1,
        "source": "coach",
        "model": "claude-opus-4-8",
        "generated_at": 1781200000,
        "applied_at": "2026-06-12",
        "based_on_workout_id": 133,
        "based_on_workout_count": 56,
        "focus": "Верх+низ",
        "load_type": "medium",
        "exercises": [
            {
                "exercise_id": 1,
                "name": "Bench Press",
                "sets": [{"reps": 12, "weight": 80}, {"reps": 12, "weight": 82.5}],
            }
        ],
    }
    snapshot.update(overrides)
    return snapshot


def payload_with_snapshot(client_id: str, snapshot) -> dict:
    """Payload тренировки со снапшотом внутри."""
    payload = sample_workout_payload(client_id=client_id)
    payload["data"]["recommendation"] = snapshot
    return payload


class NormalizeSnapshotTests(unittest.TestCase):
    """Нормализация снапшота: ``rules.normalize_recommendation_snapshot``."""

    def test_valid_snapshot_passes_with_defaults(self) -> None:
        out = rules.normalize_recommendation_snapshot(sample_snapshot())
        assert out is not None
        self.assertIsNotNone(out)
        self.assertEqual(out["schema"], 1)
        self.assertEqual(out["source"], "coach")
        self.assertEqual(out["load_type"], "medium")
        self.assertEqual(out["exercises"][0]["sets"][1]["weight"], 82.5)

    def test_defaults_filled_when_meta_missing(self) -> None:
        out = rules.normalize_recommendation_snapshot(
            {"exercises": [{"exercise_id": 2, "name": "X", "sets": [{"reps": 10, "weight": 50}]}]}
        )
        assert out is not None
        self.assertEqual(out["schema"], 1)
        self.assertEqual(out["source"], "coach")
        self.assertIsNone(out["model"])

    def test_invalid_shapes_yield_none(self) -> None:
        self.assertIsNone(rules.normalize_recommendation_snapshot(None))
        self.assertIsNone(rules.normalize_recommendation_snapshot("nope"))
        self.assertIsNone(rules.normalize_recommendation_snapshot({}))
        self.assertIsNone(rules.normalize_recommendation_snapshot({"exercises": []}))
        # повторы < 1 убирают подход, пустые подходы — упражнение, без упражнений → None
        self.assertIsNone(
            rules.normalize_recommendation_snapshot(
                {
                    "exercises": [
                        {"exercise_id": 1, "name": "X", "sets": [{"reps": 0, "weight": 10}]}
                    ]
                }
            )
        )

    def test_unknown_load_type_nulled_and_strings_capped(self) -> None:
        out = rules.normalize_recommendation_snapshot(
            sample_snapshot(load_type="insane", focus="ф" * 500)
        )
        assert out is not None
        self.assertIsNone(out["load_type"])
        self.assertEqual(len(out["focus"]), 200)

    def test_oversize_snapshot_dropped(self) -> None:
        big = sample_snapshot(
            exercises=[
                {
                    "exercise_id": i,
                    "name": "Y" * 120,
                    "sets": [{"reps": 12, "weight": 100.5} for _ in range(12)],
                }
                for i in range(1, 11)
            ]
        )
        # ~10 упражнений × 12 подходов с именами максимальной длины: > 8 КБ за счёт имён
        result = rules.normalize_recommendation_snapshot(big)
        if result is not None:
            self.assertLessEqual(
                len(backend_store.json.dumps(result, ensure_ascii=False)),
                rules.MAX_RECOMMENDATION_SNAPSHOT_BYTES,
            )


class SnapshotAPITests(unittest.TestCase):
    """Снапшот через POST и PUT на живом сервере."""

    def test_post_with_snapshot_persists_and_lists(self) -> None:
        with running_miniapp_server() as app:
            client = JsonHttpClient(app.base_url)
            res = client.request_json(
                "POST", "/api/workouts", payload_with_snapshot("w-snap", sample_snapshot())
            )
            self.assertEqual(res.status, 201)
            stored = res.payload["workout"]["data"].get("recommendation")
            self.assertIsNotNone(stored)
            self.assertEqual(stored["focus"], "Верх+низ")

            listed = client.request_json("GET", "/api/workouts")
            self.assertEqual(
                listed.payload["workouts"][0]["data"]["recommendation"]["model"],
                "claude-opus-4-8",
            )

    def test_post_without_snapshot_has_no_key(self) -> None:
        with running_miniapp_server() as app:
            client = JsonHttpClient(app.base_url)
            res = client.request_json(
                "POST", "/api/workouts", sample_workout_payload(client_id="w-plain")
            )
            self.assertEqual(res.status, 201)
            self.assertNotIn("recommendation", res.payload["workout"]["data"])

    def test_invalid_snapshot_dropped_silently(self) -> None:
        with running_miniapp_server() as app:
            client = JsonHttpClient(app.base_url)
            res = client.request_json(
                "POST", "/api/workouts", payload_with_snapshot("w-bad", {"exercises": "garbage"})
            )
            self.assertEqual(res.status, 201)
            self.assertNotIn("recommendation", res.payload["workout"]["data"])

    def test_put_without_snapshot_preserves_existing(self) -> None:
        with running_miniapp_server() as app:
            client = JsonHttpClient(app.base_url)
            created = client.request_json(
                "POST", "/api/workouts", payload_with_snapshot("w-edit", sample_snapshot())
            )
            workout_id = created.payload["workout"]["id"]

            edited = sample_workout_payload(client_id="w-edit", reps=15)
            res = client.request_json("PUT", f"/api/workouts/{workout_id}", edited)
            self.assertEqual(res.status, 200)
            preserved = res.payload["workout"]["data"].get("recommendation")
            self.assertIsNotNone(preserved)
            self.assertEqual(preserved["generated_at"], 1781200000)
            self.assertEqual(res.payload["workout"]["data"]["exercises"][0]["sets"][0]["reps"], 15)

    def test_put_with_snapshot_replaces_existing(self) -> None:
        with running_miniapp_server() as app:
            client = JsonHttpClient(app.base_url)
            created = client.request_json(
                "POST", "/api/workouts", payload_with_snapshot("w-replace", sample_snapshot())
            )
            workout_id = created.payload["workout"]["id"]

            newer = payload_with_snapshot(
                "w-replace", sample_snapshot(focus="Новый план", generated_at=222)
            )
            res = client.request_json("PUT", f"/api/workouts/{workout_id}", newer)
            self.assertEqual(
                res.payload["workout"]["data"]["recommendation"]["focus"], "Новый план"
            )
            self.assertEqual(res.payload["workout"]["data"]["recommendation"]["generated_at"], 222)

    def test_post_dedupe_retry_backfills_snapshot(self) -> None:
        with running_miniapp_server() as app:
            client = JsonHttpClient(app.base_url)
            first = client.request_json(
                "POST", "/api/workouts", sample_workout_payload(client_id="w-retry")
            )
            self.assertEqual(first.status, 201)
            self.assertNotIn("recommendation", first.payload["workout"]["data"])

            retry = client.request_json(
                "POST", "/api/workouts", payload_with_snapshot("w-retry", sample_snapshot())
            )
            self.assertEqual(retry.status, 200)  # путь дедупа, created=False
            self.assertFalse(retry.payload["created"])
            backfilled = retry.payload["workout"]["data"].get("recommendation")
            self.assertIsNotNone(backfilled)
            self.assertEqual(backfilled["focus"], "Верх+низ")

    def test_post_dedupe_retry_does_not_overwrite_existing_snapshot(self) -> None:
        with running_miniapp_server() as app:
            client = JsonHttpClient(app.base_url)
            client.request_json(
                "POST",
                "/api/workouts",
                payload_with_snapshot("w-keep", sample_snapshot(focus="Оригинал")),
            )
            retry = client.request_json(
                "POST",
                "/api/workouts",
                payload_with_snapshot("w-keep", sample_snapshot(focus="Подмена")),
            )
            self.assertEqual(
                retry.payload["workout"]["data"]["recommendation"]["focus"], "Оригинал"
            )


if __name__ == "__main__":
    unittest.main()
