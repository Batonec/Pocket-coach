from __future__ import annotations

import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

import support  # noqa: F401 — adds backend to sys.path
from support import sample_workout_payload

import coach_signals
import coach_state
from backend_store import MiniAppStore

TODAY = date(2026, 8, 14)
STATE = dict(coach_state.DEFAULT_STATE, phase_started="2026-08-01")


def _workout(when: str, exercise_id: int = 8, sets: int = 3) -> dict:
    return {
        "workout_date": when,
        "data": {
            "load_type": "medium",
            "exercises": [
                {"exercise_id": exercise_id, "name": "X",
                 "sets": [{"reps": 10, "weight": 60}] * sets}
            ],
        },
    }


def _weights(days_ago: int) -> list[dict]:
    return [{"entry_date": (TODAY - timedelta(days=days_ago)).isoformat(), "weight": 79.0}]


def _waists(days_ago: int) -> list[dict]:
    return [{"entry_date": (TODAY - timedelta(days=days_ago)).isoformat(), "waist": 84.0}]


class MeasurementsSignalTests(unittest.TestCase):
    def test_fresh_measurements_mean_no_signal(self) -> None:
        self.assertIsNone(
            coach_signals._measurements_signal(_weights(3), _waists(5), STATE, TODAY)
        )

    def test_due_stage_counts_down_to_the_deadline(self) -> None:
        signal = coach_signals._measurements_signal(_weights(11), _waists(2), STATE, TODAY)
        self.assertEqual(signal["id"], "measurements_due")
        self.assertEqual(signal["severity"], "info")
        self.assertEqual(signal["title"], "Обнови вес — талия свежая")
        self.assertIn("Через 4 дн.", signal["body"])
        self.assertEqual(signal["action"]["type"], "open_measurements")
        self.assertEqual(signal["action"]["target"], "weight")

    def test_overdue_and_never_measured_collapse_into_warn(self) -> None:
        signal = coach_signals._measurements_signal(_weights(27), [], STATE, TODAY)
        self.assertEqual(signal["id"], "measurements_overdue")
        self.assertEqual(signal["severity"], "warn")
        self.assertIn("вес и талию", signal["body"])

    def test_only_waist_due_targets_the_waist_segment(self) -> None:
        signal = coach_signals._measurements_signal(_weights(2), _waists(12), STATE, TODAY)
        self.assertEqual(signal["id"], "measurements_due")
        self.assertEqual(signal["title"], "Обнови талию — вес свежий")
        self.assertEqual(signal["action"]["target"], "waist")


class TrainingsSignalTests(unittest.TestCase):
    def test_recent_training_is_silent(self) -> None:
        workouts = [_workout((TODAY - timedelta(days=4)).isoformat())]
        self.assertIsNone(coach_signals._trainings_signal(workouts, TODAY))

    def test_return_soon_names_the_deadline(self) -> None:
        last = TODAY - timedelta(days=12)
        signal = coach_signals._trainings_signal([_workout(last.isoformat())], TODAY)
        self.assertEqual(signal["id"], "return_soon")
        self.assertEqual(signal["severity"], "warn")
        deadline = last + timedelta(days=13)
        self.assertIn(coach_signals._ru_date(deadline), signal["title"])

    def test_return_mode_is_supportive_accent(self) -> None:
        signal = coach_signals._trainings_signal(
            [_workout((TODAY - timedelta(days=20)).isoformat())], TODAY
        )
        self.assertEqual(signal["id"], "return_mode")
        self.assertEqual(signal["severity"], "accent")
        self.assertIn("Просто приди", signal["body"])


class DeloadSignalTests(unittest.TestCase):
    def _dense(self, start: date, count: int) -> list[dict]:
        return [_workout((start + timedelta(days=i * 3)).isoformat()) for i in range(count)]

    def test_deload_week_signal_until_first_session(self) -> None:
        start = date(2026, 5, 1)
        state = dict(coach_state.DEFAULT_STATE, phase_started=start.isoformat())
        workouts = self._dense(start, 14)          # 6 недель набора, week 7 = deload
        today = start + timedelta(days=43)         # день внутри 7-й недели без тренировки
        signal = coach_signals._deload_signal(state, workouts, today)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["severity"], "accent")

        trained = workouts + [_workout((start + timedelta(days=43)).isoformat())]
        self.assertIsNone(coach_signals._deload_signal(state, trained, today))


class WeekDoneSignalTests(unittest.TestCase):
    def _planned_workout(self, when: str, done: int, planned: int) -> dict:
        workout = _workout(when, sets=done)
        workout["data"]["recommendation"] = {
            "schema": 1,
            "exercises": [
                {"exercise_id": 8, "name": "X",
                 "sets": [{"reps": 10, "weight": 60}] * planned}
            ],
        }
        return workout

    def test_ninety_percent_week_earns_the_milestone(self) -> None:
        monday = date(2026, 8, 10)  # понедельник после закрытой недели 3–9 авг
        workouts = [
            self._planned_workout("2026-08-04", done=5, planned=5),
            self._planned_workout("2026-08-07", done=5, planned=5),
        ]
        signal = coach_signals._week_done_signal(workouts, monday)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["severity"], "positive")
        self.assertIn("100%", signal["title"])
        # По средам сигнал уже мёртв.
        self.assertIsNone(coach_signals._week_done_signal(workouts, date(2026, 8, 13)))

    def test_low_adherence_week_is_not_celebrated(self) -> None:
        monday = date(2026, 8, 10)
        workouts = [
            self._planned_workout("2026-08-04", done=2, planned=5),
            self._planned_workout("2026-08-07", done=2, planned=5),
        ]
        self.assertIsNone(coach_signals._week_done_signal(workouts, monday))


class ComputeSignalsIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = MiniAppStore(Path(self.temp_dir.name) / "trainer.db")
        self.user = self.store.ensure_debug_user("signals-tests")
        self.uid = self.user["id"]

    def _add_workout(self, when: str) -> None:
        payload = sample_workout_payload(client_id=f"w-{when}", workout_date=when)
        self.store.save_workout(self.uid, payload)

    def test_positive_is_suppressed_next_to_a_warn(self) -> None:
        today = date(2026, 8, 10)  # понедельник
        # Закрытая неделя с планом на 100%…
        for when in ("2026-08-04", "2026-08-07"):
            payload = sample_workout_payload(client_id=f"w-{when}", workout_date=when)
            payload["data"]["recommendation"] = {
                "schema": 1,
                "exercises": [{"exercise_id": 1, "name": "Bench Press",
                               "sets": [{"reps": 12, "weight": 80}]}],
            }
            self.store.save_workout(self.uid, payload)
        # …но замеров веса нет вовсе → warn по замерам подавляет позитив.
        signals = coach_signals.compute_signals(
            self.store, self.uid, dict(STATE), today=today
        )
        ids = [signal["id"] for signal in signals]
        self.assertIn("measurements_overdue", ids)
        self.assertNotIn("week_done", ids)

    def test_snooze_hides_episode_and_new_episode_revives(self) -> None:
        today = date(2026, 8, 14)
        self._add_workout("2026-08-02")  # 12 дней → return_soon
        self.store.save_body_weight(self.uid, {"entry_date": "2026-08-13", "weight": 79.0})
        self.store.save_waist(self.uid, {"entry_date": "2026-08-13", "waist": 84.0})

        signals = coach_signals.compute_signals(self.store, self.uid, dict(STATE), today=today)
        self.assertEqual([signal["id"] for signal in signals], ["return_soon"])
        key = signals[0]["instance_key"]

        # Эпизодный дисмисс прячет сигнал…
        self.store.save_signal_snooze(self.uid, key, None)
        self.assertEqual(
            coach_signals.compute_signals(self.store, self.uid, dict(STATE), today=today), []
        )
        # …но эскалация в return_mode — новый эпизод, снуз не действует.
        later = today + timedelta(days=3)
        self.store.save_body_weight(self.uid, {"entry_date": later.isoformat(), "weight": 79.0})
        self.store.save_waist(self.uid, {"entry_date": later.isoformat(), "waist": 84.0})
        revived = coach_signals.compute_signals(self.store, self.uid, dict(STATE), today=later)
        self.assertEqual([signal["id"] for signal in revived], ["return_mode"])

    def test_expired_timed_snooze_returns_the_signal(self) -> None:
        today = date(2026, 8, 14)
        self._add_workout("2026-08-02")
        self.store.save_body_weight(self.uid, {"entry_date": "2026-08-13", "weight": 79.0})
        self.store.save_waist(self.uid, {"entry_date": "2026-08-13", "waist": 84.0})
        signals = coach_signals.compute_signals(self.store, self.uid, dict(STATE), today=today)
        key = signals[0]["instance_key"]

        now = int(time.time())
        self.store.save_signal_snooze(self.uid, key, now + 3600)
        self.assertEqual(
            coach_signals.compute_signals(
                self.store, self.uid, dict(STATE), today=today, now_ts=now
            ),
            [],
        )
        self.assertEqual(
            [s["id"] for s in coach_signals.compute_signals(
                self.store, self.uid, dict(STATE), today=today, now_ts=now + 7200
            )],
            ["return_soon"],
        )

    def test_report_signal_dies_on_read(self) -> None:
        today = date(2026, 8, 14)
        self._add_workout(today.isoformat())
        self.store.save_body_weight(self.uid, {"entry_date": today.isoformat(), "weight": 79.0})
        self.store.save_waist(self.uid, {"entry_date": today.isoformat(), "waist": 84.0})
        self.store.save_coach_report(self.uid, today.isoformat(), 7, "**Итоги**", "m", 1, 2)

        signals = coach_signals.compute_signals(self.store, self.uid, dict(STATE), today=today)
        self.assertEqual([signal["id"] for signal in signals], ["weekly_report_ready"])

        self.assertTrue(self.store.mark_coach_report_read(self.uid))
        self.assertEqual(
            coach_signals.compute_signals(self.store, self.uid, dict(STATE), today=today), []
        )


if __name__ == "__main__":
    unittest.main()
