from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
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
