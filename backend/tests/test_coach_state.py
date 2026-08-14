from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import support  # noqa: F401 — adds backend to sys.path

import coach_state


def _workout(when: str) -> dict:
    return {"workout_date": when, "data": {"exercises": []}}


class WeekdayParsingTests(unittest.TestCase):
    def test_accepts_int_en_and_ru(self) -> None:
        self.assertEqual(coach_state.parse_weekday(5), 5)
        self.assertEqual(coach_state.parse_weekday("sat"), 5)
        self.assertEqual(coach_state.parse_weekday("Суббота"), 5)
        self.assertEqual(coach_state.parse_weekday("СБ"), 5)
        self.assertEqual(coach_state.parse_weekday("воскресенье"), 6)

    def test_rejects_garbage(self) -> None:
        for bad in (9, -1, True, "someday"):
            with self.assertRaises(ValueError):
                coach_state.parse_weekday(bad)


class StateFileTests(unittest.TestCase):
    def test_missing_or_broken_file_falls_back_to_defaults(self) -> None:
        state = coach_state.load_state(None)
        self.assertEqual(state["phase"], "cut_recomp")
        self.assertEqual(state["injection_day"], 5)  # Saturday

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_state.json"
            self.assertEqual(coach_state.load_state(path)["phase"], "cut_recomp")
            path.write_text("{broken", "utf-8")
            self.assertEqual(coach_state.load_state(path)["phase"], "cut_recomp")

    def test_load_reads_valid_fields_and_ignores_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_state.json"
            path.write_text(
                json.dumps(
                    {
                        "phase": "maintenance",
                        "phase_started": "2026-08-01",
                        "waist_limit_cm": 86.5,
                        "waist_base_cm": 9000,     # implausible → ignored
                        "injection_day": "вс",
                    }
                ),
                "utf-8",
            )
            state = coach_state.load_state(path)
            self.assertEqual(state["phase"], "maintenance")
            self.assertEqual(state["phase_started"], "2026-08-01")
            self.assertEqual(state["waist_limit_cm"], 86.5)
            self.assertIsNone(state["waist_base_cm"])
            self.assertEqual(state["injection_day"], 6)

    def test_set_phase_writes_file_and_stamps_today(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_state.json"
            state = coach_state.set_phase(
                path, "lean_bulk", {"target_weight_kg": 80}, today=date(2026, 11, 1)
            )
            self.assertEqual(state["phase"], "lean_bulk")
            self.assertEqual(state["phase_started"], "2026-11-01")

            reloaded = coach_state.load_state(path)
            params = coach_state.phase_params(reloaded)
            self.assertEqual(params["phase"], "lean_bulk")
            self.assertEqual(params["target_weight_kg"], 80)
            # Defaults survive alongside the override.
            self.assertEqual(params["ramp_cap"], (10, 16))

    def test_set_phase_journals_the_closed_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_state.json"
            coach_state.set_phase(path, "cut_recomp", today=date(2026, 8, 14))
            state = coach_state.set_phase(path, "lean_bulk", today=date(2026, 10, 25))
            self.assertEqual(
                state["phase_history"],
                [{"phase": "cut_recomp", "started": "2026-08-14", "ended": "2026-10-25"}],
            )
            # And the journal survives a reload.
            self.assertEqual(len(coach_state.load_state(path)["phase_history"]), 1)

    def test_set_phase_without_start_date_journals_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_state.json"
            state = coach_state.set_phase(path, "maintenance", today=date(2026, 8, 14))
            self.assertEqual(state["phase_history"], [])

    def test_set_phase_validates_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_state.json"
            with self.assertRaises(ValueError):
                coach_state.set_phase(path, "bulk?!")
            with self.assertRaises(ValueError):
                coach_state.set_phase(path, "lean_bulk", {"unknown_key": 1})
            with self.assertRaises(ValueError):
                coach_state.set_phase(path, "lean_bulk", {"calories": True})


class BlockWeekTests(unittest.TestCase):
    def test_counts_weeks_from_phase_start(self) -> None:
        state = dict(coach_state.DEFAULT_STATE, phase_started="2026-08-01")
        workouts = [_workout("2026-08-12"), _workout("2026-08-08"), _workout("2026-08-02")]
        self.assertEqual(coach_state.block_week(state, workouts, date(2026, 8, 14)), 2)

    def test_long_gap_resets_the_anchor(self) -> None:
        state = dict(coach_state.DEFAULT_STATE, phase_started="2026-05-01")
        workouts = [  # a 30-day gap before 2026-07-01 starts a new block
            _workout("2026-07-15"),
            _workout("2026-07-08"),
            _workout("2026-07-01"),
            _workout("2026-06-01"),
        ]
        self.assertEqual(coach_state.block_week(state, workouts, date(2026, 7, 20)), 3)

    def test_currently_on_break_means_week_one(self) -> None:
        state = dict(coach_state.DEFAULT_STATE, phase_started="2026-05-01")
        workouts = [_workout("2026-07-01")]
        self.assertTrue(coach_state.is_return_from_break(workouts, date(2026, 8, 14)))
        self.assertEqual(coach_state.block_week(state, workouts, date(2026, 8, 14)), 1)

    def test_no_history_is_week_one(self) -> None:
        self.assertEqual(
            coach_state.block_week(dict(coach_state.DEFAULT_STATE), [], date(2026, 8, 14)), 1
        )


class VolumeRampTests(unittest.TestCase):
    def test_building_phase_ramps_to_the_cap(self) -> None:
        state = dict(coach_state.DEFAULT_STATE)  # cut_recomp: 6–8 → 10–14
        self.assertEqual(coach_state.weekly_volume_target(state, 1), (6, 8))
        self.assertEqual(coach_state.weekly_volume_target(state, 3), (8, 12))
        self.assertEqual(coach_state.weekly_volume_target(state, 6), (10, 14))

        bulk = dict(state, phase="lean_bulk")  # cap 10–16
        self.assertEqual(coach_state.weekly_volume_target(bulk, 6), (10, 16))

    def test_maintenance_has_no_ramp(self) -> None:
        state = dict(coach_state.DEFAULT_STATE, phase="maintenance")
        self.assertIsNone(coach_state.weekly_volume_target(state, 3))


class CyclePositionTests(unittest.TestCase):
    def _dense_workouts(self, start: date, count: int, step_days: int = 3) -> list[dict]:
        return [
            _workout((start + timedelta(days=index * step_days)).isoformat())
            for index in range(count)
        ]

    def test_deload_fires_on_week_seven_with_real_volume(self) -> None:
        start = date(2026, 5, 1)
        state = dict(coach_state.DEFAULT_STATE, phase_started=start.isoformat())
        workouts = self._dense_workouts(start, 15)          # every 3 days → 2.3/wk
        today = start + timedelta(days=42)                  # block week 7
        position = coach_state.cycle_position(state, workouts, today)
        self.assertEqual(position["block_week"], 7)
        self.assertEqual(position["cycle_week"], 7)
        self.assertTrue(position["deload_week"])

        # After the light week the cycle wraps and the ramp restarts.
        later = start + timedelta(days=49)                  # block week 8
        more = workouts + self._dense_workouts(start + timedelta(days=43), 2)
        position = coach_state.cycle_position(state, more, later)
        self.assertEqual(position["cycle_week"], 1)
        self.assertFalse(position["deload_week"])
        self.assertEqual(coach_state.weekly_volume_target(state, position["cycle_week"]), (6, 8))

    def test_deload_withheld_without_accumulated_work(self) -> None:
        start = date(2026, 5, 1)
        state = dict(coach_state.DEFAULT_STATE, phase_started=start.isoformat())
        workouts = self._dense_workouts(start, 7, step_days=7)   # 1/wk — no fatigue
        today = start + timedelta(days=42)
        position = coach_state.cycle_position(state, workouts, today)
        self.assertEqual(position["cycle_week"], 7)
        self.assertFalse(position["deload_week"])

    def test_maintenance_has_no_deload_cycle(self) -> None:
        start = date(2026, 5, 1)
        state = dict(coach_state.DEFAULT_STATE, phase="maintenance",
                     phase_started=start.isoformat())
        workouts = self._dense_workouts(start, 15)
        position = coach_state.cycle_position(state, workouts, start + timedelta(days=42))
        self.assertFalse(position["deload_week"])
        self.assertEqual(position["cycle_week"], position["block_week"])


class CycleTests(unittest.TestCase):
    def test_cycle_day_and_levels_for_saturday_injection(self) -> None:
        state = dict(coach_state.DEFAULT_STATE)  # injection sat
        friday = coach_state.cycle_info(state, date(2026, 8, 14))
        self.assertEqual(friday["day"], 7)
        self.assertIn("минимальный", friday["level"])

        saturday = coach_state.cycle_info(state, date(2026, 8, 15))
        self.assertEqual(saturday["day"], 1)
        self.assertIn("минимальный", saturday["level"])

        sunday = coach_state.cycle_info(state, date(2026, 8, 16))
        self.assertEqual(sunday["day"], 2)
        self.assertIn("пик", sunday["level"])

        wednesday = coach_state.cycle_info(state, date(2026, 8, 19))
        self.assertEqual(wednesday["day"], 5)
        self.assertIn("средний", wednesday["level"])

        self.assertEqual(friday["peak_days"], "вс–вт")
        self.assertEqual(friday["trough_days"], "пт–сб")

    def test_changing_injection_day_shifts_the_windows(self) -> None:
        state = dict(coach_state.DEFAULT_STATE, injection_day=2)  # Wednesday
        info = coach_state.cycle_info(state, date(2026, 8, 19))   # that Wednesday
        self.assertEqual(info["day"], 1)
        self.assertEqual(info["peak_days"], "чт–сб")
        self.assertEqual(info["trough_days"], "вт–ср")


if __name__ == "__main__":
    unittest.main()
