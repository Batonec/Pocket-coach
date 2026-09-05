"""Состояние подготовки: файл и дефолты, смена фазы с журналом, неделя блока,
ramp объёма, разгрузка по позиции в цикле, переопределения целей по группам.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import support  # noqa: F401 — кладёт backend в sys.path

from trainer.data import files
from trainer.domain import coach_features, coach_state, prompt_builder


def _workout(when: str) -> dict:
    """Пустая тренировка на дату — для счёта недель хватает даты."""
    return {"workout_date": when, "data": {"exercises": []}}


class StateFileTests(unittest.TestCase):
    """Чтение, запись и смена фазы через ``files``."""

    def test_missing_or_broken_file_falls_back_to_defaults(self) -> None:
        state = coach_state.default_state()
        self.assertEqual(state["phase"], "cut_recomp")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_state.json"
            self.assertEqual(files.load_state(path)["phase"], "cut_recomp")
            path.write_text("{broken", "utf-8")
            self.assertEqual(files.load_state(path)["phase"], "cut_recomp")

    def test_load_reads_valid_fields_and_ignores_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_state.json"
            path.write_text(
                json.dumps(
                    {
                        "phase": "maintenance",
                        "phase_started": "2026-08-01",
                        "waist_limit_cm": 86.5,
                        "waist_base_cm": 9000,  # неправдоподобно → игнорируется
                        "injection_day": "вс",  # легаси-поле → игнорируется
                    }
                ),
                "utf-8",
            )
            state = files.load_state(path)
            self.assertEqual(state["phase"], "maintenance")
            self.assertEqual(state["phase_started"], "2026-08-01")
            self.assertEqual(state["waist_limit_cm"], 86.5)
            self.assertIsNone(state["waist_base_cm"])
            self.assertNotIn("injection_day", state)

    def test_set_phase_writes_file_and_stamps_today(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_state.json"
            state = files.set_phase(
                path, "lean_bulk", {"target_weight_kg": 80}, today=date(2026, 11, 1)
            )
            self.assertEqual(state["phase"], "lean_bulk")
            self.assertEqual(state["phase_started"], "2026-11-01")

            reloaded = files.load_state(path)
            params = coach_state.phase_params(reloaded)
            self.assertEqual(params["phase"], "lean_bulk")
            self.assertEqual(params["target_weight_kg"], 80)
            # Дефолты живут рядом с переопределением.
            self.assertEqual(params["ramp_cap"], (10, 16))

    def test_set_phase_journals_the_closed_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_state.json"
            files.set_phase(path, "cut_recomp", today=date(2026, 8, 14))
            state = files.set_phase(path, "lean_bulk", today=date(2026, 10, 25))
            self.assertEqual(
                state["phase_history"],
                [{"phase": "cut_recomp", "started": "2026-08-14", "ended": "2026-10-25"}],
            )
            # И журнал переживает перечитывание.
            self.assertEqual(len(files.load_state(path)["phase_history"]), 1)

    def test_set_phase_without_start_date_journals_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_state.json"
            state = files.set_phase(path, "maintenance", today=date(2026, 8, 14))
            self.assertEqual(state["phase_history"], [])

    def test_set_phase_validates_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_state.json"
            with self.assertRaises(ValueError):
                files.set_phase(path, "bulk?!")
            with self.assertRaises(ValueError):
                files.set_phase(path, "lean_bulk", {"unknown_key": 1})
            with self.assertRaises(ValueError):
                files.set_phase(path, "lean_bulk", {"calories": True})


class BlockWeekTests(unittest.TestCase):
    """Неделя блока от старта фазы или возврата."""

    def test_counts_weeks_from_phase_start(self) -> None:
        state = dict(coach_state.DEFAULT_STATE, phase_started="2026-08-01")
        workouts = [_workout("2026-08-12"), _workout("2026-08-08"), _workout("2026-08-02")]
        self.assertEqual(coach_state.block_week(state, workouts, date(2026, 8, 14)), 2)

    def test_long_gap_resets_the_anchor(self) -> None:
        state = dict(coach_state.DEFAULT_STATE, phase_started="2026-05-01")
        workouts = [  # разрыв 30 дней перед 2026-07-01 начинает новый блок
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
    """Коридор недельного объёма по неделе блока."""

    def test_building_phase_ramps_to_the_cap(self) -> None:
        state = dict(coach_state.DEFAULT_STATE)  # cut_recomp: 6–8 → 10–14
        self.assertEqual(coach_state.weekly_volume_target(state, 1), (6, 8))
        self.assertEqual(coach_state.weekly_volume_target(state, 3), (8, 12))
        self.assertEqual(coach_state.weekly_volume_target(state, 6), (10, 14))

        bulk = dict(state, phase="lean_bulk")  # потолок 10–16
        self.assertEqual(coach_state.weekly_volume_target(bulk, 6), (10, 16))

    def test_maintenance_has_no_ramp(self) -> None:
        state = dict(coach_state.DEFAULT_STATE, phase="maintenance")
        self.assertIsNone(coach_state.weekly_volume_target(state, 3))


class CyclePositionTests(unittest.TestCase):
    """Позиция в цикле и плановая разгрузка."""

    def _dense_workouts(self, start: date, count: int, step_days: int = 3) -> list[dict]:
        """``count`` тренировок с шагом ``step_days`` от ``start``."""
        return [
            _workout((start + timedelta(days=index * step_days)).isoformat())
            for index in range(count)
        ]

    def test_deload_fires_on_week_seven_with_real_volume(self) -> None:
        start = date(2026, 5, 1)
        state = dict(coach_state.DEFAULT_STATE, phase_started=start.isoformat())
        workouts = self._dense_workouts(start, 15)  # раз в 3 дня → 2.3/нед
        today = start + timedelta(days=42)  # неделя блока 7
        position = coach_state.cycle_position(state, workouts, today)
        self.assertEqual(position["block_week"], 7)
        self.assertEqual(position["cycle_week"], 7)
        self.assertTrue(position["deload_week"])

        # После лёгкой недели цикл замыкается, и ramp начинается заново.
        later = start + timedelta(days=49)  # неделя блока 8
        more = workouts + self._dense_workouts(start + timedelta(days=43), 2)
        position = coach_state.cycle_position(state, more, later)
        self.assertEqual(position["cycle_week"], 1)
        self.assertFalse(position["deload_week"])
        self.assertEqual(coach_state.weekly_volume_target(state, position["cycle_week"]), (6, 8))

    def test_deload_withheld_without_accumulated_work(self) -> None:
        start = date(2026, 5, 1)
        state = dict(coach_state.DEFAULT_STATE, phase_started=start.isoformat())
        workouts = self._dense_workouts(start, 7, step_days=7)  # 1/нед — усталости нет
        today = start + timedelta(days=42)
        position = coach_state.cycle_position(state, workouts, today)
        self.assertEqual(position["cycle_week"], 7)
        self.assertFalse(position["deload_week"])

    def test_maintenance_has_no_deload_cycle(self) -> None:
        start = date(2026, 5, 1)
        state = dict(
            coach_state.DEFAULT_STATE, phase="maintenance", phase_started=start.isoformat()
        )
        workouts = self._dense_workouts(start, 15)
        position = coach_state.cycle_position(state, workouts, start + timedelta(days=42))
        self.assertFalse(position["deload_week"])
        self.assertEqual(position["cycle_week"], position["block_week"])


if __name__ == "__main__":
    unittest.main()


class GroupTargetOverrideTests(unittest.TestCase):
    """Программа с приоритетами (спина 16, квадрицепс 9) не выражается одним
    коридором на все крупные группы — для этого и заведён group_targets."""

    def _path(self, tmp):
        """Путь к файлу состояния во временном каталоге."""
        return pathlib.Path(tmp) / "state.json"

    def test_group_targets_override_the_uniform_corridor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._path(tmp)
            files.set_phase(
                path,
                "cut_recomp",
                {"group_targets": {"спина": [16, 16], "квадрицепс/ягодичные": [9, 9]}},
                today=date(2026, 8, 16),
            )
            params = coach_state.phase_params(files.load_state(path))
            targets = coach_features.group_volume_targets((6, 9), None, params.get("group_targets"))
            self.assertEqual(targets["спина"], (16, 16))
            self.assertEqual(targets["квадрицепс/ягодичные"], (9, 9))
            # неупомянутая крупная группа остаётся на общем коридоре
            self.assertEqual(targets["грудь"], (6, 9))
            # малая группа — на своём потолке политики
            self.assertEqual(targets["бицепс"], coach_features.SMALL_GROUP_TARGETS["бицепс"])

    def test_unknown_group_is_rejected_on_write(self):
        """Опечатка в названии группы иначе молча не применилась бы."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                files.set_phase(
                    self._path(tmp),
                    "cut_recomp",
                    {"group_targets": {"спинв": [16, 16]}},
                    today=date(2026, 8, 16),
                )
            self.assertIn("Неизвестная группа", str(ctx.exception))

    def test_malformed_bounds_are_rejected_on_write(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            files.set_phase(
                self._path(tmp),
                "cut_recomp",
                {"group_targets": {"спина": 16}},
                today=date(2026, 8, 16),
            )

    def test_render_prints_the_group_goal_inline(self):
        volume = coach_features.weekly_volume([], date(2026, 8, 16))
        text = prompt_builder.render_weekly_volume(volume, (6, 9), None, {"спина": (16, 16)})
        # Цель стоит рядом с ПРЯМЫМ счётом — это колонка из таблицы программы, —
        # а эффективный счёт помечен как справочный.
        self.assertIn("спина: 0 прямых (цель 16–16) / 0 эффективных (справочно)", text)
        self.assertIn("ЗРЕЛОГО блока", text)
        self.assertIn("ПРЯМЫХ", text)
