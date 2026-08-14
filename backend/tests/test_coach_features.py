from __future__ import annotations

import unittest
from datetime import date, timedelta

import support  # noqa: F401 — adds backend to sys.path

import coach_features
import coach_state


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
            _workout("2026-08-10", [(18, [(47.5, 13)])]),   # newest-first order
            _workout("2026-03-31", [(18, [(55, 12), (55, 12)])]),
            _workout("2026-03-01", [(1, [(50, 12)])]),      # duplicate id merges in
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

        text = coach_features.render_exercise_summaries(summaries)
        self.assertIn("пик 55×12", text)
        self.assertIn("86% пика", text)

    def test_gravitron_progress_is_inverted(self) -> None:
        workouts = [
            _workout("2026-08-01", [(4, [(28, 10)])]),
            _workout("2026-07-01", [(4, [(25, 12)])]),   # PR: more reps at 25
            _workout("2026-06-15", [(4, [(25, 10)])]),   # PR: lower counterweight
            _workout("2026-06-01", [(4, [(30, 10)])]),
        ]
        summary = coach_features.exercise_summaries(workouts, CATALOG, TODAY)[0]
        self.assertEqual(summary["top_weight"], 25)
        self.assertEqual(summary["top_reps"], 12)
        self.assertEqual(summary["last_pr_date"], "2026-07-01")
        self.assertEqual(summary["current_weight"], 28)
        self.assertEqual(summary["pct_of_peak"], 89)  # 25/28

        text = coach_features.render_exercise_summaries([summary])
        self.assertIn("противовес", text)
        self.assertIn("меньше = сильнее", text)


class WeeklyVolumeTests(unittest.TestCase):
    def test_direct_and_effective_sets(self) -> None:
        workouts = [
            _workout("2026-08-13", [(18, [(50, 10)] * 3), (9, [(60, 10)] * 2)]),
            _workout("2026-08-01", [(18, [(50, 10)] * 5)]),   # outside the 7-day window
        ]
        volume = coach_features.weekly_volume(workouts, TODAY)
        self.assertEqual(volume["грудь"]["direct"], 3)
        self.assertEqual(volume["грудь"]["effective"], 3.0)
        self.assertEqual(volume["спина"]["direct"], 2)
        self.assertEqual(volume["трицепс"]["effective"], 1.5)   # 3 presses × 0.5
        self.assertEqual(volume["дельты"]["effective"], 1.5)
        self.assertEqual(volume["бицепс"]["effective"], 1.0)    # 2 pulls × 0.5

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
        report = coach_features.stall_report(
            workouts, summaries, 0.0, "lean_bulk", None, TODAY
        )
        self.assertTrue(report["preconditions_ok"])
        stalled_names = {s["name"] for s in report["stalled"]}
        self.assertIn("Жим в тренажере", stalled_names)

        text = coach_features.render_stall_report(report)
        self.assertIn("ЗАСТОЙ", text)
        self.assertIn("deload −10%", text)

    def test_red_preconditions_withhold_the_flag(self) -> None:
        workouts = [
            _workout((TODAY - timedelta(days=index * 10)).isoformat(), [(18, [(60, 10)] * 3)])
            for index in range(4)
        ]
        summaries = coach_features.exercise_summaries(workouts, CATALOG, TODAY)
        report = coach_features.stall_report(
            workouts, summaries, None, "lean_bulk", None, TODAY
        )
        self.assertFalse(report["preconditions_ok"])
        self.assertEqual(report["stalled"], [])
        text = coach_features.render_stall_report(report)
        self.assertIn("посещаемостью", text)

    def test_cut_requires_target_rate(self) -> None:
        workouts = self._green_history()
        summaries = coach_features.exercise_summaries(workouts, CATALOG, TODAY)
        report = coach_features.stall_report(
            workouts, summaries, +0.4, "cut_recomp", (-0.35, -0.25), TODAY
        )
        self.assertFalse(report["preconditions_ok"])
        self.assertTrue(any("темпа" in reason for reason in report["reasons"]))


class CombackRampTests(unittest.TestCase):
    def test_steps_from_current_to_peak_use_the_machine_step(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(80, 12)])]),
            _workout("2026-04-20", [(8, [(120, 12)])]),
            _workout("2026-04-10", [(8, [(110, 12)])]),
            _workout("2026-04-01", [(8, [(100, 12)])]),
        ]
        lines = coach_features.comeback_ramp(workouts, CATALOG, TODAY)
        self.assertEqual(len(lines), 1)
        self.assertIn("Жим ногами", lines[0])
        self.assertIn("Ступени: 90 → 100 → 110 → 120", lines[0])

    def test_no_ramp_when_current_is_at_peak(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(120, 12)])]),
            _workout("2026-08-01", [(8, [(115, 12)])]),
        ]
        self.assertEqual(coach_features.comeback_ramp(workouts, CATALOG, TODAY), [])


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
            {"entry_date": (TODAY - timedelta(days=offset)).isoformat(),
             "weight": 80.0 + (21 - offset) * 0.02}
            for offset in range(21, -1, -1)
        ]
        waists = [
            {"entry_date": (TODAY - timedelta(days=8)).isoformat(), "waist": 85.2},
            {"entry_date": (TODAY - timedelta(days=1)).isoformat(), "waist": 85.4},
        ]
        result = coach_features.nutrition_matrix(
            state, self._params(state), weights, waists, TODAY
        )
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
        result = coach_features.nutrition_matrix(
            state, self._params(state), old_weights, [], TODAY
        )
        self.assertTrue(any("устарел" in line for line in result["lines"]))
        self.assertFalse(any("ккал" in line for line in result["lines"] if "−100" in line))


class MeasurementRenderTests(unittest.TestCase):
    def test_drops_garbage_and_lists_waist(self) -> None:
        weights = [
            {"entry_date": "2026-08-01", "weight": 22.0},   # logging noise
            {"entry_date": "2026-08-10", "weight": 79.0},
        ]
        waists = [{"entry_date": "2026-08-10", "waist": 84.0}]
        lines = coach_features.render_measurements(weights, waists, TODAY)
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
                {"exercise_id": 18, "name": "Жим в тренажере",
                 "sets": [{"reps": 10, "weight": 50}] * 3},
                {"exercise_id": 15, "name": "Сгибания ног",
                 "sets": [{"reps": 12, "weight": 30}] * 2},
            ],
        }
        exercises = []
        if fact_first:
            # Recorded under the duplicate id 1 — must match plan id 18 via alias.
            exercises.append({"exercise_id": 1, "name": "Жим гор.",
                              "sets": [{"reps": 10, "weight": 50}] * fact_first})
        if include_second:
            exercises.append({"exercise_id": 15, "name": "Сгибания ног",
                              "sets": [{"reps": 12, "weight": 30}] * 2})
        workout["data"]["exercises"] = exercises
        return workout

    def test_aggregates_pct_and_skips_with_alias_matching(self) -> None:
        workouts = [
            self._planned_workout((TODAY - timedelta(days=2)).isoformat()),
            self._planned_workout((TODAY - timedelta(days=9)).isoformat(),
                                  fact_first=4, include_second=True),
            self._planned_workout((TODAY - timedelta(days=60)).isoformat()),  # вне окна
        ]
        stats = coach_features.adherence_stats(workouts, TODAY)
        self.assertEqual(stats["sessions"], 2)
        self.assertEqual(stats["planned_sets"], 10)
        # 3 + min(4, 3) + 2 = 8; extra sets never inflate past the plan.
        self.assertEqual(stats["done_sets"], 8)
        self.assertEqual(stats["pct"], 80)
        self.assertEqual(stats["skipped"], [("Сгибания ног", 1)])

        line = coach_features.render_adherence_stats(stats)
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
            _workout("2026-08-12", [(8, [(105, 10)] * 3)]),   # PR in range
            _workout("2026-08-19", [(8, [(105, 10)] * 3)]),
            _workout("2026-07-01", [(8, [(90, 10)] * 3)]),    # before the phase
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
            workouts, weights, waists, CATALOG,
            phase="cut_recomp", started=started, ended=ended,
        )
        self.assertEqual(summary["workouts"], 3)
        self.assertEqual(summary["weight_start"], 79.0)
        self.assertEqual(summary["weight_end"], 77.6)
        self.assertAlmostEqual(summary["weight_rate_per_week"], -0.35, places=2)
        self.assertEqual(summary["waist_start"], 86.0)
        self.assertEqual(summary["waist_end"], 84.5)
        self.assertEqual(summary["pr_events"], 2)  # 100 and 105 both beat the July baseline
        self.assertEqual(summary["prs"][0]["name"], "Жим ногами")

        text = coach_features.render_phase_summary(summary)
        self.assertIn("Фаза cut_recomp", text)
        self.assertIn("79.0 → 77.6", text)
        self.assertIn("ПР за фазу: 2", text)

    def test_pr_dates_exclude_the_baseline_session(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(105, 10)])]),   # improvement
            _workout("2026-08-01", [(8, [(100, 10)])]),   # baseline
        ]
        summary = coach_features.exercise_summaries(workouts, CATALOG, TODAY)[0]
        self.assertEqual(summary["pr_dates"], ["2026-08-10"])


class GroupTargetTests(unittest.TestCase):
    def test_big_groups_follow_the_week_corridor(self) -> None:
        targets = coach_features.group_volume_targets((6, 8))
        self.assertEqual(targets["грудь"], (6, 8))
        self.assertEqual(targets["спина"], (6, 8))
        self.assertEqual(targets["квадрицепс/ягодичные"], (6, 8))
        self.assertEqual(targets["дельты"], (6, 12))     # small groups stay fixed
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
            _workout("2026-01-01", [(18, [(70, 10)])]),   # far outside 8 weeks
        ]
        self.assertEqual(
            coach_features.recent_weight_range(workouts, 18, TODAY), (45.0, 55.0)
        )
        self.assertIsNone(coach_features.recent_weight_range(workouts, 9, TODAY))


if __name__ == "__main__":
    unittest.main()
