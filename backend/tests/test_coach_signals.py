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


class WaistLimitSignalTests(unittest.TestCase):
    def _state(self, **overrides) -> dict:
        state = dict(
            STATE,
            phase="lean_bulk",
            phase_started="2026-07-01",
            waist_limit_cm=88.0,
        )
        state.update(overrides)
        return state

    def _points(self, older: float, latest: float, latest_days_ago: int = 0) -> list[dict]:
        return [
            {
                "entry_date": (TODAY - timedelta(days=latest_days_ago + 7)).isoformat(),
                "waist": older,
            },
            {
                "entry_date": (TODAY - timedelta(days=latest_days_ago)).isoformat(),
                "waist": latest,
            },
        ]

    def test_two_consecutive_limit_readings_are_critical(self) -> None:
        signal = coach_signals._waist_limit_signal(
            self._points(88.0, 88.5), self._state(), TODAY
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal["id"], "waist_limit")
        self.assertEqual(signal["severity"], "critical")
        self.assertEqual(signal["glyph"], "tape")
        self.assertEqual(signal["action"]["target"], "waist")
        self.assertFalse(signal["snoozable"])

    def test_one_crossing_does_not_trigger_on_tape_noise(self) -> None:
        self.assertIsNone(
            coach_signals._waist_limit_signal(
                self._points(87.8, 88.4), self._state(), TODAY
            )
        )

    def test_signal_only_exists_in_lean_bulk(self) -> None:
        self.assertIsNone(
            coach_signals._waist_limit_signal(
                self._points(88.2, 88.4), self._state(phase="cut_recomp"), TODAY
            )
        )

    def test_stale_pair_does_not_leave_permanent_critical(self) -> None:
        self.assertIsNone(
            coach_signals._waist_limit_signal(
                self._points(88.2, 88.4, latest_days_ago=15), self._state(), TODAY
            )
        )


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
        self.assertEqual(signal["glyph"], "nutrition")
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

    def test_return_mode_ready_plan_is_supportive_accent(self) -> None:
        recommendation = {
            "status": "ready",
            "recommendation": {"coach_context": {"return_from_break": True}},
            "updated_at": 123,
        }
        signal = coach_signals._trainings_signal(
            [_workout((TODAY - timedelta(days=20)).isoformat())],
            TODAY,
            recommendation,
        )
        self.assertEqual(signal["id"], "return_mode")
        self.assertEqual(signal["severity"], "accent")
        self.assertEqual(signal["title"], "Возвратная тренировка готова")
        self.assertEqual(
            signal["body"],
            "~85–90% рабочих весов. Догонять пропущенное не надо",
        )
        self.assertEqual(signal["action"]["type"], "open_next_workout")

    def test_return_mode_pending_does_not_duplicate_loading_state(self) -> None:
        signal = coach_signals._trainings_signal(
            [_workout((TODAY - timedelta(days=20)).isoformat())],
            TODAY,
            {"status": "pending"},
        )
        self.assertIsNone(signal)

    def test_return_mode_failed_plan_offers_real_retry(self) -> None:
        signal = coach_signals._trainings_signal(
            [_workout((TODAY - timedelta(days=20)).isoformat())],
            TODAY,
            {"status": "failed", "updated_at": 456},
        )
        self.assertEqual(signal["title"], "После перерыва нужен облегчённый старт")
        self.assertEqual(signal["body"], "План пока не готов — повтори генерацию")
        self.assertEqual(signal["action"], {
            "type": "refresh_recommendation", "label": "Повторить",
        })
        self.assertIn("recommendation=failed:456", signal["instance_key"])

    def test_return_mode_outdated_ready_plan_requests_refresh(self) -> None:
        signal = coach_signals._trainings_signal(
            [_workout((TODAY - timedelta(days=20)).isoformat())],
            TODAY,
            {
                "status": "ready",
                "recommendation": {"coach_context": {"return_from_break": False}},
                "updated_at": 789,
            },
        )
        self.assertEqual(signal["body"], "Текущий план не учитывает перерыв — обнови его")
        self.assertEqual(signal["action"]["type"], "refresh_recommendation")


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

    def test_saving_missing_waist_clears_calorie_pause_signal(self) -> None:
        self.store.save_body_weight(
            self.uid, {"entry_date": TODAY.isoformat(), "weight": 79.0}
        )
        before = coach_signals.compute_signals(
            self.store, self.uid, dict(STATE), today=TODAY
        )
        self.assertEqual([signal["id"] for signal in before], ["measurements_overdue"])
        self.assertEqual(before[0]["action"]["target"], "waist")

        self.store.save_waist(
            self.uid, {"entry_date": TODAY.isoformat(), "waist": 84.0}
        )

        self.assertEqual(
            coach_signals.compute_signals(
                self.store, self.uid, dict(STATE), today=TODAY
            ),
            [],
        )

    def test_waist_limit_is_first_and_replaces_freshness_reminder(self) -> None:
        self._add_workout("2026-08-02")  # warn: return_soon
        self.store.save_body_weight(
            self.uid, {"entry_date": TODAY.isoformat(), "weight": 79.0}
        )
        self.store.save_waist(
            self.uid, {"entry_date": "2026-08-07", "waist": 88.0}
        )
        self.store.save_waist(
            self.uid, {"entry_date": TODAY.isoformat(), "waist": 88.5}
        )
        state = dict(
            STATE,
            phase="lean_bulk",
            phase_started="2026-08-01",
            waist_limit_cm=88.0,
        )

        signals = coach_signals.compute_signals(
            self.store, self.uid, state, today=TODAY
        )

        self.assertEqual([signal["id"] for signal in signals], [
            "waist_limit", "return_soon",
        ])

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

    def test_return_banner_tracks_cached_recommendation_status(self) -> None:
        today = date(2026, 8, 14)
        self._add_workout("2026-07-20")
        self.store.save_body_weight(
            self.uid, {"entry_date": today.isoformat(), "weight": 79.0}
        )
        self.store.save_waist(
            self.uid, {"entry_date": today.isoformat(), "waist": 84.0}
        )

        self.store.fail_recommendation(self.uid, "invalid plan")
        failed = coach_signals.compute_signals(
            self.store, self.uid, dict(STATE), today=today
        )
        self.assertEqual(failed[0]["action"]["type"], "refresh_recommendation")

        self.store.set_recommendation_pending(self.uid)
        pending = coach_signals.compute_signals(
            self.store, self.uid, dict(STATE), today=today
        )
        self.assertEqual(pending, [])

        self.store.save_recommendation(
            self.uid,
            self.store.get_latest_workout_id(self.uid),
            1,
            "claude-test",
            {"focus": "return", "coach_context": {"return_from_break": True}},
            1,
            1,
        )
        ready = coach_signals.compute_signals(
            self.store, self.uid, dict(STATE), today=today
        )
        self.assertEqual(ready[0]["title"], "Возвратная тренировка готова")
        self.assertEqual(ready[0]["action"]["type"], "open_next_workout")

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
