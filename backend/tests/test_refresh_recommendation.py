from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import support  # noqa: F401
from support import sample_workout_payload

from jobs import refresh_recommendation as refresh
from trainer import backend_store  # support puts backend/ on sys.path
from trainer.coach import recommender

NOW = int(time.time())
HOUR = 3600


def rec_row(status: str, age_hours: float) -> dict:
    return {"status": status, "updated_at": NOW - int(age_hours * HOUR)}


class ShouldRefreshTests(unittest.TestCase):
    def test_missing_recommendation_refreshes(self) -> None:
        refresh_needed, _ = refresh.should_refresh(None, NOW)
        self.assertTrue(refresh_needed)

    def test_fresh_ready_skips_and_stale_ready_refreshes(self) -> None:
        self.assertFalse(refresh.should_refresh(rec_row("ready", 5), NOW)[0])
        self.assertTrue(refresh.should_refresh(rec_row("ready", 30), NOW)[0])

    def test_max_age_is_configurable(self) -> None:
        self.assertTrue(refresh.should_refresh(rec_row("ready", 5), NOW, max_age_hours=4)[0])
        self.assertFalse(refresh.should_refresh(rec_row("ready", 5), NOW, max_age_hours=6)[0])

    def test_pending_skipped_unless_stuck(self) -> None:
        self.assertFalse(refresh.should_refresh(rec_row("pending", 0.5), NOW)[0])
        self.assertTrue(refresh.should_refresh(rec_row("pending", 3), NOW)[0])

    def test_failed_refreshes(self) -> None:
        self.assertTrue(refresh.should_refresh(rec_row("failed", 0.1), NOW)[0])


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = backend_store.MiniAppStore(Path(tmp.name) / "trainer.db")
        user = self.store.ensure_debug_user("refresh-test-user")
        self.uid = int(user["id"])
        self.store.save_workout(self.uid, sample_workout_payload(client_id="w1"))

        self._orig_generate = recommender.generate
        self.addCleanup(lambda: setattr(recommender, "generate", self._orig_generate))
        recommender.generate = lambda *a, **k: (
            {
                "focus": "Авто-свежесть",
                "load_type": "medium",
                "rationale": "r",
                "exercises": [
                    {
                        "exercise_id": 1,
                        "name": "Bench Press",
                        "note": "n",
                        "sets": [{"reps": 10, "weight": 50}],
                    }
                ],
            },
            {"input_tokens": 1, "output_tokens": 2},
            "claude-test",
        )

    def test_run_generates_when_no_recommendation(self) -> None:
        did = refresh.run(self.store, self.uid)
        self.assertTrue(did)
        rec = self.store.get_recommendation(self.uid)
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(rec["recommendation"]["focus"], "Авто-свежесть")

    def test_run_skips_fresh_recommendation(self) -> None:
        refresh.run(self.store, self.uid)
        first = self.store.get_recommendation(self.uid)["updated_at"]
        did = refresh.run(self.store, self.uid)
        self.assertFalse(did)
        self.assertEqual(self.store.get_recommendation(self.uid)["updated_at"], first)

    def test_run_force_regenerates(self) -> None:
        refresh.run(self.store, self.uid)
        did = refresh.run(self.store, self.uid, force=True)
        self.assertTrue(did)

    def test_run_records_failure(self) -> None:
        def boom(*a, **k):
            raise recommender.RecommendationError("нет ключа")

        recommender.generate = boom
        did = refresh.run(self.store, self.uid)
        self.assertTrue(did)
        rec = self.store.get_recommendation(self.uid)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("ключа", rec["error"])

    def test_run_skips_user_without_workouts(self) -> None:
        empty_user = int(self.store.ensure_debug_user("no-workouts")["id"])
        did = refresh.run(self.store, empty_user)
        self.assertFalse(did)
        self.assertIsNone(self.store.get_recommendation(empty_user))


if __name__ == "__main__":
    unittest.main()
