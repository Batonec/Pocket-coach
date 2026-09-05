from __future__ import annotations

import unittest
from datetime import date, timedelta

import support  # noqa: F401 — adds backend to sys.path

from trainer.domain import coach_features, coach_state, prompt_builder

CATALOG = [
    {"id": 18, "name": "Жим в тренажере"},
    {"id": 1, "name": "Жим гор."},
    {"id": 9, "name": "Тяга верт."},
    {"id": 4, "name": "Гравитрон"},
    {"id": 8, "name": "Жим ногами"},
]

TODAY = date(2026, 8, 14)


def _workout(when: str, exercises: list[tuple[int, list[tuple[float, int]]]]) -> dict:
    return {
        "workout_date": when,
        "data": {
            "load_type": "medium",
            "exercises": [
                {
                    "exercise_id": exercise_id,
                    "name": f"#{exercise_id}",
                    "sets": [{"reps": reps, "weight": weight} for weight, reps in sets],
                }
                for exercise_id, sets in exercises
            ],
        },
    }


class AliasAndE1rmTests(unittest.TestCase):
    def test_canonical_id_maps_the_duplicate(self) -> None:
        self.assertEqual(coach_features.canonical_exercise_id(1), 18)
        self.assertEqual(coach_features.canonical_exercise_id(18), 18)
        self.assertEqual(coach_features.canonical_exercise_id("9"), 9)
        self.assertIsNone(coach_features.canonical_exercise_id("x"))

    def test_epley(self) -> None:
        self.assertAlmostEqual(coach_features.epley_e1rm(100, 10), 133.33, places=1)


class SummaryTests(unittest.TestCase):
    def test_summary_tracks_peak_pr_and_percent(self) -> None:
        workouts = [
            _workout("2026-08-10", [(18, [(47.5, 13)])]),  # newest-first order
            _workout("2026-03-31", [(18, [(55, 12), (55, 12)])]),
            _workout("2026-03-01", [(1, [(50, 12)])]),  # duplicate id merges in
        ]
        summaries = coach_features.exercise_summaries(workouts, CATALOG, TODAY)
        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary["exercise_id"], 18)
        self.assertEqual(summary["name"], "Жим в тренажере")
        self.assertEqual(summary["sessions_total"], 3)
        self.assertEqual(summary["top_weight"], 55)
        self.assertEqual(summary["last_pr_date"], "2026-03-31")
        self.assertEqual(summary["days_since_pr"], (TODAY - date(2026, 3, 31)).days)
        self.assertEqual(summary["current_weight"], 47.5)
        self.assertEqual(summary["pct_of_peak"], 86)

        text = prompt_builder.render_exercise_summaries(summaries)
        self.assertIn("пик 55×12", text)
        self.assertIn("86% пика", text)

    def test_gravitron_progress_is_inverted(self) -> None:
        workouts = [
            _workout("2026-08-01", [(4, [(28, 10)])]),
            _workout("2026-07-01", [(4, [(25, 12)])]),  # PR: more reps at 25
            _workout("2026-06-15", [(4, [(25, 10)])]),  # PR: lower counterweight
            _workout("2026-06-01", [(4, [(30, 10)])]),
        ]
        summary = coach_features.exercise_summaries(workouts, CATALOG, TODAY)[0]
        self.assertEqual(summary["top_weight"], 25)
        self.assertEqual(summary["top_reps"], 12)
        self.assertEqual(summary["last_pr_date"], "2026-07-01")
        self.assertEqual(summary["current_weight"], 28)
        self.assertEqual(summary["pct_of_peak"], 89)  # 25/28

        text = prompt_builder.render_exercise_summaries([summary])
        self.assertIn("противовес", text)
        self.assertIn("меньше = сильнее", text)


class WeeklyVolumeTests(unittest.TestCase):
    def test_direct_and_effective_sets(self) -> None:
        workouts = [
            _workout("2026-08-13", [(18, [(50, 10)] * 3), (9, [(60, 10)] * 2)]),
            _workout("2026-08-01", [(18, [(50, 10)] * 5)]),  # outside the 7-day window
        ]
        volume = coach_features.weekly_volume(workouts, TODAY)
        self.assertEqual(volume["грудь"]["direct"], 3)
        self.assertEqual(volume["грудь"]["effective"], 3.0)
        self.assertEqual(volume["спина"]["direct"], 2)
        self.assertEqual(volume["трицепс"]["effective"], 1.5)  # 3 presses × 0.5
        # Horizontal press hits the front delt, not the measured mid delt →
        # only a quarter-set credit per press.
        self.assertEqual(volume["дельты"]["effective"], 0.75)  # 3 presses × 0.25
        self.assertEqual(volume["бицепс"]["effective"], 1.0)  # 2 pulls × 0.5

    def test_duplicate_press_id_counts_into_chest(self) -> None:
        workouts = [_workout("2026-08-13", [(1, [(50, 10)] * 4)])]
        volume = coach_features.weekly_volume(workouts, TODAY)
        self.assertEqual(volume["грудь"]["direct"], 4)


class StallTests(unittest.TestCase):
    def _green_history(self) -> list[dict]:
        # 15 sessions inside the 6-week window, ~12.5 weekly sets for every big
        # group, and the same top weight all along → a textbook stall.
        workouts = []
        for index in range(15):
            when = (TODAY - timedelta(days=index * 2)).isoformat()
            workouts.append(
                _workout(
                    when,
                    [(18, [(60, 10)] * 5), (9, [(70, 10)] * 5), (8, [(100, 10)] * 5)],
                )
            )
        return workouts

    def test_green_preconditions_flag_the_stalled_lifts(self) -> None:
        workouts = self._green_history()
        summaries = coach_features.exercise_summaries(workouts, CATALOG, TODAY)
        report = coach_features.stall_report(workouts, summaries, 0.0, "lean_bulk", None, TODAY)
        self.assertTrue(report["preconditions_ok"])
        stalled_names = {s["name"] for s in report["stalled"]}
        self.assertIn("Жим в тренажере", stalled_names)

        text = prompt_builder.render_stall_report(report)
        self.assertIn("ЗАСТОЙ", text)
        self.assertIn("deload −10%", text)

    def test_red_preconditions_withhold_the_flag(self) -> None:
        workouts = [
            _workout((TODAY - timedelta(days=index * 10)).isoformat(), [(18, [(60, 10)] * 3)])
            for index in range(4)
        ]
        summaries = coach_features.exercise_summaries(workouts, CATALOG, TODAY)
        report = coach_features.stall_report(workouts, summaries, None, "lean_bulk", None, TODAY)
        self.assertFalse(report["preconditions_ok"])
        self.assertEqual(report["stalled"], [])
        text = prompt_builder.render_stall_report(report)
        self.assertIn("посещаемостью", text)

    def test_cut_requires_target_rate(self) -> None:
        workouts = self._green_history()
        summaries = coach_features.exercise_summaries(workouts, CATALOG, TODAY)
        report = coach_features.stall_report(
            workouts, summaries, +0.4, "cut_recomp", (-0.35, -0.25), TODAY
        )
        self.assertFalse(report["preconditions_ok"])
        self.assertTrue(any("темпа" in reason for reason in report["reasons"]))


class CurrentWorkingWeightTests(unittest.TestCase):
    def _sessions(self, workouts, exercise_id=8):
        return coach_features._iter_exercise_sessions(workouts)[exercise_id]

    def test_light_day_next_to_a_real_session_is_not_a_regression(self) -> None:
        workouts = [
            _workout("2026-07-22", [(8, [(40, 12)] * 3)]),  # лёгкий день
            _workout("2026-07-19", [(8, [(60, 12)] * 3)]),
        ]
        current = coach_features.current_working_weight(self._sessions(workouts), inverted=False)
        self.assertEqual(current, 60)

    def test_single_garbage_set_is_filtered(self) -> None:
        workouts = [
            _workout("2026-08-12", [(11, [(10, 12), (10, 12), (20, 3)])]),  # «20×3+»
            _workout("2026-08-09", [(11, [(10, 12)] * 3)]),
        ]
        sessions = coach_features._iter_exercise_sessions(workouts)[11]
        current = coach_features.current_working_weight(sessions, inverted=False)
        self.assertEqual(current, 10)

    def test_sessions_from_a_previous_era_are_ignored(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(80, 12)] * 3)]),  # после перерыва
            _workout("2026-04-20", [(8, [(120, 12)] * 3)]),  # прошлая эпоха
        ]
        current = coach_features.current_working_weight(self._sessions(workouts), inverted=False)
        self.assertEqual(current, 80)

    def test_summary_uses_the_filtered_metric(self) -> None:
        workouts = [
            _workout("2026-08-12", [(9, [(65, 12), (65, 12)])]),
            _workout("2026-08-09", [(9, [(75, 15), (80, 11)])]),
        ]
        summary = coach_features.exercise_summaries(workouts, CATALOG, TODAY)[0]
        # Peak = the real best-e1RM SET (75×15), never max-weight × alien reps.
        self.assertEqual(summary["top_weight"], 75)
        self.assertEqual(summary["top_reps"], 15)
        self.assertEqual(summary["current_weight"], 80)  # max working of last sessions


class RampInvariantTests(unittest.TestCase):
    def test_target_is_the_pre_break_working_weight_not_the_peak(self) -> None:
        # 40-day break between 07-01 and 08-10. The all-time e1RM peak is
        # 75×15, but the athlete WORKED with 80 right before the pause — the
        # ladder climbs back to that, not to a peak set at another time.
        workouts = [
            _workout("2026-08-10", [(9, [(65, 12)] * 3)]),
            _workout("2026-07-01", [(9, [(75, 15)] * 2)]),  # e1RM peak 112.5
            _workout("2026-06-20", [(9, [(80, 11)] * 2)]),  # heavier, pre-break working
            _workout("2026-06-10", [(9, [(70, 12)] * 2)]),
        ]
        items = coach_features.comeback_ramp_steps(workouts, CATALOG, TODAY)
        item = next(i for i in items if i["exercise_id"] == 9)
        self.assertEqual(item["target"], 80)
        self.assertEqual(item["current"], 65)
        self.assertEqual(
            (item["break_start"], item["break_end"]), (date(2026, 7, 1), date(2026, 8, 10))
        )
        # One machine step (5 kg here) per rung, up to the target.
        self.assertEqual(item["steps"], [70, 75, 80])
        text = prompt_builder.render_comeback_ramp(items)[0]
        self.assertIn("доперерывный рабочий 80", text)
        self.assertNotIn("пик", text)

    def test_degenerate_one_jump_ladder_is_rebuilt(self) -> None:
        # A machine whose only recorded step is huge (20 kg) would produce a
        # single 40→60 (+50%) jump — a lone leap is not a ladder, so it is
        # rebuilt as equal steps (three ~17% rungs still beat one jump).
        workouts = [
            _workout("2026-08-10", [(10, [(40, 12)] * 3)]),
            _workout("2026-07-05", [(10, [(60, 12)] * 3)]),
            _workout("2026-06-25", [(10, [(40, 12)] * 3)]),
            _workout("2026-06-15", [(10, [(60, 12)] * 3)]),
        ]
        catalog = [*CATALOG, {"id": 10, "name": "Тяга горизонт."}]
        items = coach_features.comeback_ramp_steps(workouts, catalog, TODAY)
        item = next(i for i in items if i["exercise_id"] == 10)
        self.assertGreaterEqual(len(item["steps"]), 3)  # no single 40→60 jump
        previous = item["current"]
        for step in item["steps"]:
            self.assertGreater(step, previous)  # strictly ascending
            self.assertLess(step - previous, 20)  # each rung < the raw gap
            previous = step
        self.assertEqual(item["steps"][-1], item["target"])

    def test_coarse_plates_keep_their_real_rungs(self) -> None:
        # 10-kg plates on an 80-kg leg press are 12.5% rungs — coarser than
        # the programme's 10%, but the only weights the machine can load. A
        # fabricated 82.5 would be worse data than an honest 90.
        workouts = [
            _workout("2026-08-10", [(8, [(80, 12)] * 3)]),
            _workout("2026-07-01", [(8, [(100, 10)] * 3)]),
            _workout("2026-06-24", [(8, [(90, 10)] * 3)]),
        ]
        item = coach_features.comeback_ramp_steps(workouts, CATALOG, TODAY)[0]
        self.assertEqual(item["steps"], [90, 100])

    def test_at_peak_means_no_ladder(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(120, 12)] * 3)]),
            _workout("2026-08-05", [(8, [(115, 12)] * 3)]),
        ]
        self.assertEqual(coach_features.comeback_ramp_steps(workouts, CATALOG, TODAY), [])

    def test_regained_weight_prints_nothing(self) -> None:
        # Back at (or above) the pre-break working weight → nothing to climb.
        workouts = [
            _workout("2026-08-12", [(8, [(100, 10)] * 3)]),
            _workout("2026-08-10", [(8, [(80, 12)] * 3)]),
            _workout("2026-07-01", [(8, [(100, 10)] * 3)]),
        ]
        self.assertEqual(coach_features.comeback_ramp_steps(workouts, CATALOG, TODAY), [])

    def test_no_ladder_while_still_on_the_break(self) -> None:
        # The comeback day itself: the pre-break weights are the reference
        # block, the entry weight is the model's call — no ladder.
        workouts = [
            _workout("2026-07-01", [(8, [(100, 10)] * 3)]),
            _workout("2026-05-01", [(8, [(80, 12)] * 3)]),
            _workout("2026-04-01", [(8, [(120, 12)] * 3)]),
        ]
        self.assertEqual(coach_features.comeback_ramp_steps(workouts, CATALOG, TODAY), [])

    def test_old_comeback_is_history_not_a_ramp(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(80, 12)] * 3)]),
            _workout("2026-05-01", [(8, [(80, 12)] * 3)]),  # comeback 105 days ago
            _workout("2026-03-01", [(8, [(120, 12)] * 3)]),
        ]
        self.assertEqual(coach_features.comeback_ramp_steps(workouts, CATALOG, TODAY), [])

    def test_gravitron_ladder_descends(self) -> None:
        workouts = [
            _workout("2026-08-10", [(4, [(32, 10)] * 3)]),
            _workout("2026-07-01", [(4, [(25, 12)] * 2)]),
            _workout("2026-06-20", [(4, [(27.5, 12)] * 2)]),
        ]
        items = coach_features.comeback_ramp_steps(workouts, CATALOG, TODAY)
        item = next(i for i in items if i["exercise_id"] == 4)
        self.assertTrue(item["inverted"])
        previous = item["current"]
        for step in item["steps"]:
            self.assertLess(step, previous)
            previous = step
        self.assertEqual(item["steps"][-1], 25)


class TrendValidityTests(unittest.TestCase):
    def test_gap_between_measurements_invalidates_the_trend(self) -> None:
        points = [(TODAY - timedelta(days=27), 77.25), (TODAY, 79.0)]
        self.assertIsNone(coach_features.weight_trend_per_week(points, TODAY))

    def test_phase_boundary_resets_the_base(self) -> None:
        phase_start = TODAY - timedelta(days=3)
        points = [
            (TODAY - timedelta(days=10), 77.0),  # прошлая фаза
            (TODAY - timedelta(days=2), 78.6),
            (TODAY, 79.0),
        ]
        self.assertIsNone(coach_features.weight_trend_per_week(points, TODAY, since=phase_start))
        # Without the boundary the same points give a number.
        self.assertIsNotNone(coach_features.weight_trend_per_week(points, TODAY))

    def test_dense_in_phase_measurements_yield_a_trend(self) -> None:
        points = [
            (TODAY - timedelta(days=7), 79.4),
            (TODAY - timedelta(days=3), 79.2),
            (TODAY, 79.0),
        ]
        trend = coach_features.weight_trend_per_week(points, TODAY)
        self.assertAlmostEqual(trend, -0.4, places=1)

    def test_matrix_asks_for_measurements_instead_of_advising(self) -> None:
        state = dict(
            coach_state.DEFAULT_STATE, phase_started=(TODAY - timedelta(days=1)).isoformat()
        )
        params = coach_state.phase_params(state)
        weights = [
            {"entry_date": (TODAY - timedelta(days=27)).isoformat(), "weight": 77.25},
            {"entry_date": TODAY.isoformat(), "weight": 79.0},
        ]
        result = coach_features.nutrition_matrix(state, params, weights, [], TODAY)
        self.assertIsNone(result["trend_per_week"])
        self.assertTrue(any("недостаточно" in line for line in result["lines"]))
        self.assertFalse(any("−100–150" in line for line in result["lines"]))
        self.assertFalse(any("тренд веса" in line for line in result["lines"]))


class CombackRampTests(unittest.TestCase):
    def test_steps_from_current_to_pre_break_use_the_machine_step(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(80, 12)])]),
            _workout("2026-04-20", [(8, [(120, 12)])]),
            _workout("2026-04-10", [(8, [(110, 12)])]),
            _workout("2026-04-01", [(8, [(100, 12)])]),
        ]
        lines = prompt_builder.comeback_ramp(workouts, CATALOG, TODAY)
        self.assertEqual(len(lines), 1)
        self.assertIn("Жим ногами: доперерывный рабочий 120, сейчас 80", lines[0])
        self.assertIn("Ступени: 90 → 100 → 110 → 120", lines[0])

    def test_no_ramp_when_current_is_at_peak(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(120, 12)])]),
            _workout("2026-08-01", [(8, [(115, 12)])]),
        ]
        self.assertEqual(prompt_builder.comeback_ramp(workouts, CATALOG, TODAY), [])


class NutritionMatrixTests(unittest.TestCase):
    def _state(self, **overrides):
        return dict(coach_state.DEFAULT_STATE, **overrides)

    def _params(self, state):
        return coach_state.phase_params(state)

    def _weights(self, value: float, days: int = 21) -> list[dict]:
        return [
            {"entry_date": (TODAY - timedelta(days=offset)).isoformat(), "weight": value}
            for offset in range(days, -1, -1)
        ]

    def test_cut_stall_with_waist_down_is_recomp_bonus(self) -> None:
        state = self._state()
        waists = [
            {"entry_date": (TODAY - timedelta(days=8)).isoformat(), "waist": 84.0},
            {"entry_date": (TODAY - timedelta(days=1)).isoformat(), "waist": 83.5},
        ]
        result = coach_features.nutrition_matrix(
            state, self._params(state), self._weights(79.0), waists, TODAY
        )
        self.assertTrue(any("рекомп-бонус" in line for line in result["lines"]))
        self.assertFalse(any("−100–150" in line for line in result["lines"]))

    def test_cut_stall_without_waist_drop_cuts_calories(self) -> None:
        state = self._state()
        result = coach_features.nutrition_matrix(
            state, self._params(state), self._weights(79.0), [], TODAY
        )
        self.assertTrue(any("−100–150 ккал" in line for line in result["lines"]))
        self.assertTrue(any("талии" in line for line in result["lines"]))  # ask to measure

    def test_cut_goal_reached_suggests_lean_bulk(self) -> None:
        state = self._state()
        result = coach_features.nutrition_matrix(
            state, self._params(state), self._weights(75.2), [], TODAY
        )
        self.assertIsNotNone(result["goal"])
        self.assertIn("lean_bulk", result["goal"])

    def test_bulk_waist_creep_pauses_the_surplus(self) -> None:
        state = self._state(phase="lean_bulk", waist_base_cm=84.0)
        weights = [
            {
                "entry_date": (TODAY - timedelta(days=offset)).isoformat(),
                "weight": 80.0 + (21 - offset) * 0.02,
            }
            for offset in range(21, -1, -1)
        ]
        waists = [
            {"entry_date": (TODAY - timedelta(days=8)).isoformat(), "waist": 85.2},
            {"entry_date": (TODAY - timedelta(days=1)).isoformat(), "waist": 85.4},
        ]
        result = coach_features.nutrition_matrix(state, self._params(state), weights, waists, TODAY)
        self.assertTrue(any("паузу набора" in line for line in result["lines"]))

    def test_bulk_waist_at_limit_is_a_hard_signal(self) -> None:
        state = self._state(phase="lean_bulk", waist_limit_cm=86.0)
        waists = [
            {"entry_date": (TODAY - timedelta(days=1)).isoformat(), "waist": 86.2},
        ]
        result = coach_features.nutrition_matrix(
            state, self._params(state), self._weights(80.0), waists, TODAY
        )
        self.assertIsNotNone(result["goal"])
        self.assertIn("мини-кат", result["goal"])

    def test_stale_weight_blocks_calorie_advice(self) -> None:
        state = self._state()
        old_weights = [
            {"entry_date": (TODAY - timedelta(days=20)).isoformat(), "weight": 79.0},
        ]
        result = coach_features.nutrition_matrix(state, self._params(state), old_weights, [], TODAY)
        self.assertTrue(any("устарел" in line for line in result["lines"]))
        self.assertFalse(any("ккал" in line for line in result["lines"] if "−100" in line))


class MeasurementRenderTests(unittest.TestCase):
    def test_drops_garbage_and_lists_waist(self) -> None:
        weights = [
            {"entry_date": "2026-08-01", "weight": 22.0},  # logging noise
            {"entry_date": "2026-08-10", "weight": 79.0},
        ]
        waists = [{"entry_date": "2026-08-10", "waist": 84.0}]
        lines = prompt_builder.render_measurements(weights, waists, TODAY)
        text = "\n".join(lines)
        self.assertIn("79кг", text)
        self.assertNotIn("22кг", text)
        self.assertIn("отброшено неправдоподобных записей: 1", text)
        self.assertIn("Талия: 2026-08-10: 84см", text)


class AdherenceStatsTests(unittest.TestCase):
    def _planned_workout(self, when: str, fact_first: int = 3, include_second: bool = False):
        workout = _workout("PLACEHOLDER", [])
        workout["workout_date"] = when
        workout["data"]["recommendation"] = {
            "schema": 1,
            "exercises": [
                {
                    "exercise_id": 18,
                    "name": "Жим в тренажере",
                    "sets": [{"reps": 10, "weight": 50}] * 3,
                },
                {
                    "exercise_id": 15,
                    "name": "Сгибания ног",
                    "sets": [{"reps": 12, "weight": 30}] * 2,
                },
            ],
        }
        exercises = []
        if fact_first:
            # Recorded under the duplicate id 1 — must match plan id 18 via alias.
            exercises.append(
                {
                    "exercise_id": 1,
                    "name": "Жим гор.",
                    "sets": [{"reps": 10, "weight": 50}] * fact_first,
                }
            )
        if include_second:
            exercises.append(
                {
                    "exercise_id": 15,
                    "name": "Сгибания ног",
                    "sets": [{"reps": 12, "weight": 30}] * 2,
                }
            )
        workout["data"]["exercises"] = exercises
        return workout

    def test_aggregates_pct_and_skips_with_alias_matching(self) -> None:
        workouts = [
            self._planned_workout((TODAY - timedelta(days=2)).isoformat()),
            self._planned_workout(
                (TODAY - timedelta(days=9)).isoformat(), fact_first=4, include_second=True
            ),
            self._planned_workout((TODAY - timedelta(days=60)).isoformat()),  # вне окна
        ]
        stats = coach_features.adherence_stats(workouts, TODAY)
        self.assertEqual(stats["sessions"], 2)
        self.assertEqual(stats["planned_sets"], 10)
        # 3 + min(4, 3) + 2 = 8; extra sets never inflate past the plan.
        self.assertEqual(stats["done_sets"], 8)
        self.assertEqual(stats["pct"], 80)
        self.assertEqual(stats["skipped"], [("Сгибания ног", 1)])

        line = prompt_builder.render_adherence_stats(stats)
        self.assertIn("8 из 10", line)
        self.assertIn("Сгибания ног ×1", line)

    def test_none_without_snapshots(self) -> None:
        workouts = [_workout(TODAY.isoformat(), [(18, [(50, 10)] * 3)])]
        self.assertIsNone(coach_features.adherence_stats(workouts, TODAY))


class PhaseSummaryTests(unittest.TestCase):
    def test_summary_derives_everything_from_the_date_range(self) -> None:
        started, ended = date(2026, 8, 1), date(2026, 8, 28)  # 4 weeks
        workouts = [
            _workout("2026-08-05", [(8, [(100, 10)] * 3)]),
            _workout("2026-08-12", [(8, [(105, 10)] * 3)]),  # PR in range
            _workout("2026-08-19", [(8, [(105, 10)] * 3)]),
            _workout("2026-07-01", [(8, [(90, 10)] * 3)]),  # before the phase
        ]
        weights = [
            {"entry_date": "2026-07-30", "weight": 79.0},
            {"entry_date": "2026-08-27", "weight": 77.6},
        ]
        waists = [
            {"entry_date": "2026-08-02", "waist": 86.0},
            {"entry_date": "2026-08-26", "waist": 84.5},
        ]
        summary = coach_features.phase_summary(
            workouts,
            weights,
            waists,
            CATALOG,
            phase="cut_recomp",
            started=started,
            ended=ended,
        )
        self.assertEqual(summary["workouts"], 3)
        self.assertEqual(summary["weight_start"], 79.0)
        self.assertEqual(summary["weight_end"], 77.6)
        self.assertAlmostEqual(summary["weight_rate_per_week"], -0.35, places=2)
        self.assertEqual(summary["waist_start"], 86.0)
        self.assertEqual(summary["waist_end"], 84.5)
        self.assertEqual(summary["pr_events"], 2)  # 100 and 105 both beat the July baseline
        self.assertEqual(summary["prs"][0]["name"], "Жим ногами")

        text = prompt_builder.render_phase_summary(summary)
        self.assertIn("Фаза cut_recomp", text)
        self.assertIn("79.0 → 77.6", text)
        self.assertIn("ПР за фазу: 2", text)

    def test_pr_dates_exclude_the_baseline_session(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(105, 10)])]),  # improvement
            _workout("2026-08-01", [(8, [(100, 10)])]),  # baseline
        ]
        summary = coach_features.exercise_summaries(workouts, CATALOG, TODAY)[0]
        self.assertEqual(summary["pr_dates"], ["2026-08-10"])


class GroupTargetTests(unittest.TestCase):
    def test_big_groups_follow_the_week_corridor(self) -> None:
        targets = coach_features.group_volume_targets((6, 8))
        self.assertEqual(targets["грудь"], (6, 8))
        self.assertEqual(targets["спина"], (6, 8))
        self.assertEqual(targets["квадрицепс/ягодичные"], (6, 8))
        self.assertEqual(targets["дельты"], (6, 12))  # small groups stay fixed
        self.assertEqual(targets["бицепс бедра"], (5, 10))
        self.assertEqual(set(targets), set(coach_features.MUSCLE_GROUPS))

    def test_maintenance_flattens_everything(self) -> None:
        targets = coach_features.group_volume_targets(None, maintenance_sets=(2, 3))
        self.assertTrue(all(target == (2, 3) for target in targets.values()))

    def test_missing_corridor_falls_back_to_policy_cap(self) -> None:
        self.assertEqual(coach_features.group_volume_targets(None)["грудь"], (10, 16))


class WeightRangeTests(unittest.TestCase):
    def test_eight_week_range_merges_the_duplicate_id(self) -> None:
        workouts = [
            _workout("2026-08-10", [(18, [(50, 10), (55, 8)])]),
            _workout("2026-07-20", [(1, [(45, 12)])]),
            _workout("2026-01-01", [(18, [(70, 10)])]),  # far outside 8 weeks
        ]
        self.assertEqual(coach_features.recent_weight_range(workouts, 18, TODAY), (45.0, 55.0))
        self.assertIsNone(coach_features.recent_weight_range(workouts, 9, TODAY))


if __name__ == "__main__":
    unittest.main()


class HoldingPhaseMatrixTests(unittest.TestCase):
    """Этап может просить ДЕРЖАТЬ вес. Тогда «вес стоит» — выполненная задача,
    а не повод срезать калории; захардкоженный коридор советовал бы дефицит
    ровно за правильное поведение."""

    def _points(self, *pairs):
        return [{"entry_date": d, "weight": w} for d, w in pairs]

    def _matrix(self, rate):
        state = dict(coach_state.DEFAULT_STATE, phase="cut_recomp", phase_started="2026-06-01")
        params = dict(coach_state.PHASE_DEFAULTS["cut_recomp"], rate_kg_per_week=rate)
        params["phase"] = "cut_recomp"
        weights = self._points(
            ("2026-07-25", 79.0),
            ("2026-07-28", 79.1),
            ("2026-08-01", 78.9),
            ("2026-08-05", 79.0),
            ("2026-08-09", 79.1),
            ("2026-08-13", 79.0),
            ("2026-08-15", 79.0),
        )
        return coach_features.nutrition_matrix(state, params, weights, [], date(2026, 8, 16))

    def test_holding_phase_does_not_ask_to_cut_calories(self):
        lines = " ".join(self._matrix((-0.10, 0.10))["lines"])
        self.assertIn("это и есть задача этапа", lines)
        self.assertNotIn("−100–150 ккал", lines)

    def test_cutting_phase_still_asks_to_cut_on_a_stall(self):
        lines = " ".join(self._matrix((-0.35, -0.25))["lines"])
        self.assertIn("−100–150 ккал", lines)

    def test_goal_reached_is_not_announced_while_holding(self):
        """target_weight_kg остаётся дефолтным на этапе удержания — объявлять
        «цель фазы достигнута» на нём нельзя."""
        self.assertIsNone(self._matrix((-0.10, 0.10))["goal"])


class ActiveWindowStallTests(unittest.TestCase):
    """Предусловия и застой считаются по АКТИВНОМУ окну: от старта блока, а не за
    шесть календарных недель, в которые попадает отпуск."""

    def _sessions(self, days_ago: list[int], weight: float = 60.0) -> list[dict]:
        # Every big group in every session, so only frequency and the window
        # length decide the verdict.
        return [
            _workout(
                (TODAY - timedelta(days=ago)).isoformat(),
                [(18, [(weight, 10)] * 5), (9, [(70, 10)] * 5), (8, [(100, 10)] * 5)],
            )
            for ago in days_ago
        ]

    def test_window_never_reaches_across_the_block_anchor(self) -> None:
        # Six sessions in the last two weeks after a return; the vacation before
        # it would read as «1.5/нед» over six weeks.
        workouts = self._sessions([1, 3, 5, 8, 10, 12])
        report = coach_features.stall_report(
            workouts, [], 0.0, "lean_bulk", None, TODAY, since=TODAY - timedelta(days=13)
        )
        self.assertEqual(report["window_days"], 14)
        self.assertAlmostEqual(report["frequency"], 3.0, places=2)
        self.assertTrue(report["too_short"])
        self.assertFalse(report["preconditions_ok"])
        text = prompt_builder.render_stall_report(report)
        self.assertIn("Активное окно 14 дн.", text)
        self.assertIn("пока не оцениваются", text)
        self.assertNotIn("НЕ выполнены", text)

    def test_group_targets_set_the_volume_threshold(self) -> None:
        # 15 sessions × 5 press sets over 42 days = 12.5 chest sets/week: enough
        # against the flat 10, not against a programme that asks 14–16.
        workouts = [
            _workout((TODAY - timedelta(days=index * 2)).isoformat(), [(18, [(60, 10)] * 5)])
            for index in range(15)
        ]
        loose = coach_features.stall_report(workouts, [], 0.0, "lean_bulk", None, TODAY)
        self.assertFalse(any("грудь" in reason for reason in loose["reasons"]))
        strict = coach_features.stall_report(
            workouts, [], 0.0, "lean_bulk", None, TODAY, group_targets={"грудь": (14, 16)}
        )
        self.assertTrue(any("грудь 12.5 < 14" in reason for reason in strict["reasons"]))
        self.assertIn("грудь 12.5 (порог 14)", prompt_builder.render_stall_report(strict))

    def test_stall_clock_runs_inside_the_window_not_from_the_all_time_pr(self) -> None:
        # Peak 70 set 100 days ago, then a break, then five weeks of climbing
        # 50 → 60: the all-time PR is old, but the lift improved 6 days ago.
        climb = [
            (34, 50),
            (32, 50),
            (30, 52.5),
            (27, 52.5),
            (25, 55),
            (23, 55),
            (20, 55),
            (18, 57.5),
            (16, 57.5),
            (13, 57.5),
            (11, 60),
            (9, 60),
            (6, 62.5),
            (4, 62.5),
            (2, 62.5),
        ]
        workouts = [
            _workout(
                (TODAY - timedelta(days=ago)).isoformat(),
                [(18, [(weight, 10)] * 4), (9, [(70, 10)] * 4), (8, [(100, 10)] * 4)],
            )
            for ago, weight in climb
        ]
        workouts.append(_workout((TODAY - timedelta(days=100)).isoformat(), [(18, [(70, 10)] * 4)]))
        summaries = coach_features.exercise_summaries(workouts, CATALOG, TODAY)
        self.assertGreaterEqual(summaries[0]["days_since_pr"], 100)  # all-time clock says «stalled»
        report = coach_features.stall_report(
            workouts, summaries, 0.0, "lean_bulk", None, TODAY, since=TODAY - timedelta(days=34)
        )
        self.assertTrue(report["preconditions_ok"])
        # The in-window clock says «6 days ago» for the press; the two lifts held
        # flat for the whole block are the ones that stalled.
        self.assertEqual([s["name"] for s in report["stalled"]], ["Тяга верт.", "Жим ногами"])

        flat = [
            _workout(
                (TODAY - timedelta(days=ago)).isoformat(),
                [(18, [(60, 10)] * 4), (9, [(70, 10)] * 4), (8, [(100, 10)] * 4)],
            )
            for ago, _ in climb
        ]
        report = coach_features.stall_report(
            flat,
            coach_features.exercise_summaries(flat, CATALOG, TODAY),
            0.0,
            "lean_bulk",
            None,
            TODAY,
            since=TODAY - timedelta(days=34),
        )
        self.assertEqual(
            [s["name"] for s in report["stalled"]], ["Жим в тренажере", "Тяга верт.", "Жим ногами"]
        )
        self.assertEqual(report["stalled"][0]["quiet_days"], 34)
        self.assertIn("без прироста 34 дн.", prompt_builder.render_stall_report(report))

    def test_young_window_withholds_the_stall_verdict(self) -> None:
        workouts = self._sessions([1, 3, 5, 8, 10, 12, 15, 17, 19, 21])
        report = coach_features.stall_report(
            workouts, [], 0.0, "lean_bulk", None, TODAY, since=TODAY - timedelta(days=21)
        )
        self.assertEqual(report["window_days"], 22)
        self.assertTrue(report["preconditions_ok"])
        self.assertIn("застой ещё не оценивается", prompt_builder.render_stall_report(report))

    def test_weight_reason_names_the_corridor_of_any_phase(self) -> None:
        workouts = self._sessions([1, 3, 5, 8, 10, 12, 15, 17, 19, 21, 24, 26, 28, 31, 33])
        holding = coach_features.stall_report(workouts, [], +0.35, "cut_recomp", (-0.1, 0.1), TODAY)
        self.assertTrue(any("при коридоре -0.10…+0.10" in r for r in holding["reasons"]))
        self.assertFalse(any("среза" in r for r in holding["reasons"]))
        gaining = coach_features.stall_report(workouts, [], +0.35, "lean_bulk", (0.1, 0.2), TODAY)
        self.assertFalse(
            any("вес" in r for r in gaining["reasons"])
        )  # gaining fast is not a red flag
        falling = coach_features.stall_report(workouts, [], -0.3, "lean_bulk", (0.1, 0.2), TODAY)
        self.assertTrue(any("вес падает" in r for r in falling["reasons"]))


class AttendanceTests(unittest.TestCase):
    def test_calendar_weeks_streaks_and_the_open_week(self) -> None:
        # TODAY is Friday 2026-08-14; the running week is Mon 10 → Sun 16.
        days = ["2026-07-20", "2026-07-27", "2026-07-29", "2026-07-31", "2026-08-01"]
        days += ["2026-08-03", "2026-08-05", "2026-08-07", "2026-08-11"]
        workouts = [_workout(day, [(8, [(100, 10)])]) for day in days]
        rows = coach_features.weekly_attendance(workouts, TODAY)
        self.assertEqual([row["sessions"] for row in rows], [0, 1, 4, 3, 1])
        self.assertEqual([row["closed"] for row in rows], [True, True, True, True, False])
        self.assertEqual(coach_features.attendance_streak(rows, 3), 2)
        self.assertEqual(coach_features.attendance_streak(rows, 4), 0)
        text = prompt_builder.render_weekly_attendance(rows, TODAY)
        self.assertIn("2026-07-27…2026-08-02: 4", text)
        self.assertIn("2026-08-10…2026-08-16 (текущая, по 2026-08-14): 1", text)

    def test_two_sessions_on_one_day_count_as_one_training_day(self) -> None:
        workouts = [_workout("2026-08-11", [(8, [(100, 10)])])] * 2
        rows = coach_features.weekly_attendance(workouts, TODAY)
        self.assertEqual(rows[-1]["sessions"], 1)


class TrendSlopeTests(unittest.TestCase):
    def test_slope_uses_every_point_not_the_end_points(self) -> None:
        # Flat week with one heavy final morning: the end points say +0.5/wk,
        # the least-squares line says ~+0.4 — one weigh-in no longer rules.
        points = [
            (TODAY - timedelta(days=14), 79.0),
            (TODAY - timedelta(days=10), 79.1),
            (TODAY - timedelta(days=7), 79.0),
            (TODAY - timedelta(days=3), 79.1),
            (TODAY, 80.0),
        ]
        trend = coach_features.weight_trend_per_week(points, TODAY)
        self.assertLess(trend, 0.45)
        self.assertGreater(trend, 0.3)

    def test_two_points_are_still_the_plain_slope(self) -> None:
        points = [(TODAY - timedelta(days=7), 80.0), (TODAY, 79.5)]
        self.assertAlmostEqual(coach_features.weight_trend_per_week(points, TODAY), -0.5, places=6)

    def test_weigh_in_count_is_reported_with_the_protocol_minimum(self) -> None:
        weights = [
            {"entry_date": (TODAY - timedelta(days=offset)).isoformat(), "weight": 79.0}
            for offset in (9, 5, 1)
        ]
        text = "\n".join(prompt_builder.render_measurements(weights, [], TODAY))
        self.assertIn("Замеров за последние 7 дней: 2 (для недельной средней нужно ≥4).", text)
        daily = [
            {"entry_date": (TODAY - timedelta(days=offset)).isoformat(), "weight": 79.0}
            for offset in range(6)
        ]
        text = "\n".join(prompt_builder.render_measurements(daily, [], TODAY))
        self.assertIn("Замеров за последние 7 дней: 6.", text)
        self.assertNotIn("нужно ≥", text)


class RateMatrixTests(unittest.TestCase):
    """Ветки матрицы строятся от коридора темпа фазы, а не от её имени."""

    def _matrix(self, phase: str, rate, weights, waists=None, **state_overrides):
        state = dict(
            coach_state.DEFAULT_STATE,
            phase=phase,
            phase_started=(TODAY - timedelta(days=30)).isoformat(),
            **state_overrides,
        )
        params = dict(coach_state.PHASE_DEFAULTS[phase], rate_kg_per_week=rate)
        params["phase"] = phase
        return coach_features.nutrition_matrix(state, params, weights, waists or [], TODAY)

    def _daily(self, start: float, per_day: float, days: int = 21) -> list[dict]:
        return [
            {
                "entry_date": (TODAY - timedelta(days=offset)).isoformat(),
                "weight": round(start + (days - offset) * per_day, 3),
            }
            for offset in range(days, -1, -1)
        ]

    def test_holding_phase_rising_weight_gets_a_confirmed_cut(self) -> None:
        result = self._matrix("cut_recomp", (-0.1, 0.1), self._daily(78.0, 0.07))
        line = " ".join(result["lines"])
        self.assertIn("выше коридора фазы (-0.10…+0.10 кг/нед", line)
        self.assertIn("по средним двух недель тоже", line)
        self.assertIn("−100–150 ккал", line)
        self.assertNotIn("паузу набора", line)

    def test_gaining_phase_adds_the_pause_option(self) -> None:
        result = self._matrix("lean_bulk", (0.1, 0.2), self._daily(78.0, 0.1))
        line = " ".join(result["lines"])
        self.assertIn("−100–150 ккал или паузу набора", line)

    def test_deviation_without_a_two_week_witness_is_not_acted_on(self) -> None:
        # Ten days of data only: the slope is valid, the means two weeks apart are not.
        weights = self._daily(79.0, 0.07, days=9)
        result = self._matrix("cut_recomp", (-0.1, 0.1), weights)
        line = " ".join(result["lines"])
        self.assertIn("выше коридора фазы", line)
        self.assertIn("ещё не подтверждают", line)
        self.assertNotIn("−100–150", line)

    def test_falling_weight_on_a_cut_offers_a_maintenance_week(self) -> None:
        result = self._matrix("cut_recomp", (-0.35, -0.25), self._daily(80.0, -0.1))
        line = " ".join(result["lines"])
        self.assertIn("ниже коридора фазы", line)
        self.assertIn("+100–150 ккал или неделю поддержки", line)

    def test_stalled_weight_on_a_gain_asks_for_more_food(self) -> None:
        result = self._matrix("lean_bulk", (0.1, 0.2), self._daily(80.0, 0.0))
        line = " ".join(result["lines"])
        self.assertIn("при плане набора", line)
        self.assertIn("+100–150 ккал", line)

    def test_in_corridor_says_so_for_any_phase(self) -> None:
        gain = " ".join(self._matrix("lean_bulk", (0.1, 0.2), self._daily(80.0, 0.02))["lines"])
        self.assertIn("в коридоре фазы (+0.10…+0.20 кг/нед", gain)
        cut = " ".join(
            self._matrix("cut_recomp", (-0.35, -0.25), self._daily(80.0, -0.04))["lines"]
        )
        self.assertIn("в коридоре фазы (-0.35…-0.25 кг/нед", cut)
        # A flat maintenance month is a two-week stall in a holding corridor:
        # the task of the phase, not a deviation.
        hold = " ".join(self._matrix("maintenance", (0.0, 0.0), self._daily(80.0, 0.0))["lines"])
        self.assertIn("задача этапа", hold)

    def test_maintenance_uses_its_own_corridor_not_the_phase_name(self) -> None:
        result = self._matrix("maintenance", (0.0, 0.0), self._daily(80.0, 0.06))
        line = " ".join(result["lines"])
        self.assertIn("выше коридора фазы (+0.00…+0.00 кг/нед", line)
        self.assertIn("−100–150 ккал", line)


class PositionTagTests(unittest.TestCase):
    def test_recent_sessions_carry_the_place_in_the_session(self) -> None:
        workouts = [
            _workout("2026-08-12", [(9, [(60, 12)]), (18, [(50, 12)]), (8, [(100, 10)])]),
            _workout("2026-08-10", [(8, [(100, 10)]), (18, [(50, 12)])]),
        ]
        summary = next(
            s
            for s in coach_features.exercise_summaries(workouts, CATALOG, TODAY)
            if s["exercise_id"] == 18
        )
        self.assertEqual(
            summary["recent_sessions"],
            [("2026-08-10", "[#2/2] 50×12"), ("2026-08-12", "[#2/3] 50×12")],
        )
        text = prompt_builder.render_exercise_summaries([summary])
        self.assertIn("2026-08-12: [#2/3] 50×12", text)
