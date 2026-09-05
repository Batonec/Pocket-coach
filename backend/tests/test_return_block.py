"""Приёмка на реальном блоке возврата (см. return_block_fixture).

Каждый тест здесь — один пункт разбора от 05.09.2026 с числами, которые сервер
теперь обязан отдавать модели вместо искажённых.
"""

from __future__ import annotations

import unittest

import return_block_fixture as fx
import support  # noqa: F401 — adds backend to sys.path

from trainer.domain import coach_features, coach_state, plan_validator, prompt_builder


class ActiveWindowTests(unittest.TestCase):
    """П.1 разбора: окно частоты и объёма не должно включать отпуск."""

    def setUp(self) -> None:
        self.workouts = fx.workouts()
        self.params = coach_state.phase_params(fx.STATE)
        self.summaries = coach_features.exercise_summaries(self.workouts, fx.CATALOG, fx.TODAY)
        self.matrix = coach_features.nutrition_matrix(
            fx.STATE, self.params, fx.BODY_WEIGHTS, fx.WAISTS, fx.TODAY
        )
        self.report = coach_features.stall_report(
            self.workouts,
            self.summaries,
            self.matrix["trend_per_week"],
            "cut_recomp",
            self.params["rate_kg_per_week"],
            fx.TODAY,
            since=coach_state._block_anchor(fx.STATE, self.workouts, fx.TODAY),
            group_targets=self.params["group_targets"],
        )

    def test_window_starts_at_the_return_not_six_weeks_back(self) -> None:
        self.assertEqual(self.report["window_start"].isoformat(), "2026-08-14")
        self.assertEqual(self.report["window_days"], 23)

    def test_frequency_is_the_real_one(self) -> None:
        # 9 sessions in 23 days ≈ 2.7/week — not «1.5/нед за 6 недель».
        self.assertAlmostEqual(self.report["frequency"], 9 / (23 / 7), places=2)
        self.assertFalse(any("частота" in reason for reason in self.report["reasons"]))

    def test_volume_is_judged_against_the_phase_targets(self) -> None:
        per_week = self.report["volume_per_week"]
        self.assertEqual(per_week["квадрицепс/ягодичные"][1], 8)  # not a flat 10
        self.assertEqual(per_week["грудь"][1], 12)
        text = coach_features.render_stall_report(self.report)
        self.assertIn("Активное окно 23 дн. (с 2026-08-14", text)
        self.assertIn("частота 2.7/нед", text)
        self.assertIn("грудь 10.7 (порог 12)", text)
        self.assertNotIn("за 6 недель", text)
        self.assertNotIn("среза", text)


class ReturnLadderTests(unittest.TestCase):
    """П.3 разбора: при закрытом возврате лесенка к пикам не печатается."""

    def test_no_ladder_once_the_pre_break_weights_are_back(self) -> None:
        # Press 60 ≥ 55, vertical row 65 ≥ 65, leg press 100 ≥ 80 → nothing to climb.
        self.assertEqual(
            coach_features.comeback_ramp_steps(fx.workouts(), fx.CATALOG, fx.TODAY), []
        )

    def test_ladder_on_the_second_session_targets_the_pre_break_weight(self) -> None:
        # The morning after the comeback session (14.08 at 50): 52.5 → 55, not 65 → 67.5.
        history = [w for w in fx.workouts() if w["workout_date"] <= "2026-08-14"]
        items = coach_features.comeback_ramp_steps(
            history, fx.CATALOG, fx.TODAY.replace(month=8, day=15)
        )
        press = next(item for item in items if item["exercise_id"] == 18)
        self.assertEqual((press["current"], press["target"]), (50, 55))
        # The rung is the machine step as the HISTORY shows it: with only two
        # sessions on record (55 → 50) that is 5 kg, hence one rung to 55; the
        # live history has 2.5-kg changes and yields 52.5 → 55.
        self.assertEqual(press["steps"][-1], 55)
        self.assertTrue(all(step - 50 <= 5 for step in press["steps"]))
        vertical = next(item for item in items if item["exercise_id"] == 9)
        self.assertEqual((vertical["current"], vertical["target"]), (60, 65))
        # The leg press is already back at its pre-break 80 → not listed.
        self.assertEqual({item["exercise_id"] for item in items}, {18, 9})


class NutritionByRateTests(unittest.TestCase):
    """П.7 разбора: ветка матрицы строится от коридора темпа, а не от имени фазы."""

    def test_weight_above_the_holding_corridor_gets_a_direction(self) -> None:
        params = coach_state.phase_params(fx.STATE)
        result = coach_features.nutrition_matrix(
            fx.STATE, params, fx.BODY_WEIGHTS, fx.WAISTS, fx.TODAY
        )
        line = " ".join(result["lines"])
        self.assertIn("выше коридора фазы", line)
        self.assertIn("−100–150 ккал", line)
        self.assertNotIn("сверь с целевым темпом", line)
        self.assertGreater(result["trend_per_week"], 0.25)

    def test_measurement_line_counts_the_week(self) -> None:
        text = "\n".join(coach_features.render_measurements(fx.BODY_WEIGHTS, fx.WAISTS, fx.TODAY))
        self.assertIn("Замеров за последние 7 дней: 2 (для недельной средней нужно ≥4)", text)


class AttendanceTests(unittest.TestCase):
    """П.2 разбора: явка по календарным неделям — факт для гейта и для каркаса."""

    def test_calendar_weeks_and_streaks(self) -> None:
        rows = coach_features.weekly_attendance(fx.workouts(), fx.TODAY)
        text = coach_features.render_weekly_attendance(rows, fx.TODAY)
        self.assertIn("2026-08-10…2026-08-16: 2", text)
        self.assertIn("2026-08-17…2026-08-23: 1", text)
        self.assertIn("2026-08-24…2026-08-30: 4", text)
        self.assertIn("2026-08-31…2026-09-06 (текущая, по 2026-09-05): 2", text)
        self.assertEqual(coach_features.attendance_streak(rows, 3), 1)
        self.assertEqual(coach_features.attendance_streak(rows, 4), 1)


class PositionInSessionTests(unittest.TestCase):
    """П.10 разбора: позиция упражнения в сессии стоит в сводке, а не выводится из ленты."""

    def test_row_summary_shows_first_and_sixth_place(self) -> None:
        summaries = coach_features.exercise_summaries(fx.workouts(), fx.CATALOG, fx.TODAY)
        row = next(s for s in summaries if s["exercise_id"] == 10)
        recent = dict(row["recent_sessions"])
        self.assertTrue(recent["2026-08-28"].startswith("[#6/6] 40×12"))
        self.assertTrue(recent["2026-08-30"].startswith("[#1/5] 60×10"))


class SessionCapTests(unittest.TestCase):
    """П.4 разбора: размер сессии — жёсткая граница фазы, не пожелание."""

    def _plan(self, per_exercise: list[int]) -> dict:
        ids = [18, 9, 8, 13, 11, 12, 17, 15, 16, 19]
        return plan_validator._validate(
            {
                "focus": "f",
                "load_type": "medium",
                "rest_days": 1,
                "rationale": "r",
                "exercises": [
                    {
                        "exercise_id": ids[index],
                        "note": "n",
                        "sets": [{"reps": 12, "weight": 20}] * count,
                    }
                    for index, count in enumerate(per_exercise)
                ],
            },
            fx.CATALOG,
        )

    def test_twenty_two_sets_violate_the_phase_cap(self) -> None:
        cap = plan_validator._session_cap(coach_state.phase_params(fx.STATE))
        self.assertEqual(cap, 20)
        plan = self._plan([4, 4, 4, 3, 3, 2, 2])  # 22 sets, like the card of 24.08
        violations = plan_validator._semantic_violations(
            plan, fx.CATALOG, fx.workouts(), fx.TODAY, session_cap=cap
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("22 рабочих подходов при потолке сессии 20", violations[0])

    def test_trim_cuts_from_the_tail_and_keeps_every_exercise(self) -> None:
        plan = self._plan([4, 4, 4, 3, 3, 2, 2])
        removed = plan_validator._trim_to_cap(plan, 20)
        self.assertEqual(plan_validator._planned_sets(plan), 20)
        # Tail first, one set per exercise per pass: the two last (isolation)
        # movements each lose a set; the compounds at the front are untouched.
        self.assertEqual([len(e["sets"]) for e in plan["exercises"]], [4, 4, 4, 3, 3, 1, 1])
        self.assertEqual(removed, ["Бабочка −1", "Трицепс −1"])


class AssembledPromptTests(unittest.TestCase):
    def test_plan_prompt_carries_the_fixed_blocks(self) -> None:
        prompt = prompt_builder._build_user_prompt(
            fx.workouts(),
            fx.BODY_WEIGHTS,
            fx.TODAY,
            20,
            catalog=fx.CATALOG,
            state=fx.STATE,
            waists=fx.WAISTS,
            events=fx.EVENTS,
        )
        self.assertIn("Тренировки по календарным неделям", prompt)
        self.assertIn("Активное окно 23 дн.", prompt)
        self.assertIn("бицепс: 5 прямых (цель 10–12) / 10.5 эффективных (справочно)", prompt)
        self.assertIn("выше коридора фазы", prompt)
        self.assertIn("[#6/6] 40×12", prompt)
        self.assertIn("2026-08-24 [без плана]", prompt)
        self.assertNotIn("Ступени", prompt)
        self.assertNotIn("пиковым", prompt)
        self.assertNotIn("[?]", prompt)

    def test_report_prompt_carries_attendance_and_the_window(self) -> None:
        prompt = prompt_builder._build_report_prompt(
            fx.workouts(),
            fx.BODY_WEIGHTS,
            fx.WAISTS,
            fx.CATALOG,
            fx.STATE,
            fx.TODAY,
            7,
            events=fx.EVENTS,
        )
        self.assertIn("Тренировки по календарным неделям", prompt)
        self.assertIn("Активное окно", prompt)


if __name__ == "__main__":
    unittest.main()
