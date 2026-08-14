from __future__ import annotations

import io
import json
import unittest
import urllib.error

import support  # noqa: F401 — adds backend to sys.path
from support import STATIC_DIR

import recommender


CATALOG = [
    {"id": 8, "name": "Жим ногами"},
    {"id": 9, "name": "Тяга верт."},
    {"id": 1, "name": "Жим гор."},
]


class RecommenderTests(unittest.TestCase):
    def test_load_catalog_reads_web_data(self) -> None:
        catalog = recommender.load_catalog(STATIC_DIR)
        self.assertTrue(catalog)
        self.assertTrue(all("id" in item and "name" in item for item in catalog))

    def test_build_schema_enum_lists_catalog_ids_and_requires_note(self) -> None:
        schema = recommender._build_schema(CATALOG)
        item = schema["properties"]["exercises"]["items"]
        # id 1 is the catalog duplicate of 18 — never offered to the model.
        self.assertEqual(item["properties"]["exercise_id"]["enum"], [8, 9])
        self.assertIn("note", item["required"])
        # The model never writes names — the server injects catalog names.
        self.assertNotIn("name", item["properties"])
        self.assertNotIn("name", item["required"])

    def test_validate_drops_unknown_id_clamps_and_uses_catalog_name(self) -> None:
        raw = {
            "focus": "Ноги",
            "load_type": "medium",
            "rationale": "...",
            "exercises": [
                {
                    "exercise_id": 8,
                    "name": "что-то своё",
                    "note": "+вес",
                    "sets": [
                        {"reps": 11, "weight": 120},
                        {"reps": 0, "weight": 50},        # reps < 1 → dropped
                        {"reps": 10, "weight": 99999},    # weight clamped
                    ],
                },
                {  # hallucinated id → dropped
                    "exercise_id": 999,
                    "name": "выдумка",
                    "note": "n",
                    "sets": [{"reps": 5, "weight": 5}],
                },
            ],
        }
        out = recommender._validate(raw, CATALOG)
        self.assertEqual(len(out["exercises"]), 1)
        exercise = out["exercises"][0]
        self.assertEqual(exercise["exercise_id"], 8)
        self.assertEqual(exercise["name"], "Жим ногами")  # catalog name, not model echo
        self.assertEqual(exercise["note"], "+вес")
        self.assertEqual([s["reps"] for s in exercise["sets"]], [11, 10])
        self.assertEqual(exercise["sets"][1]["weight"], recommender.MAX_WEIGHT)

    def test_validate_normalizes_unknown_load_type(self) -> None:
        raw = {
            "focus": "x",
            "load_type": "crazy",
            "rationale": "r",
            "exercises": [
                {"exercise_id": 8, "name": "x", "note": "n", "sets": [{"reps": 10, "weight": 50}]}
            ],
        }
        self.assertEqual(recommender._validate(raw, CATALOG)["load_type"], "medium")

    def test_validate_raises_without_valid_exercises(self) -> None:
        raw = {
            "focus": "x",
            "load_type": "light",
            "rationale": "r",
            "exercises": [
                {"exercise_id": 999, "name": "x", "note": "n", "sets": [{"reps": 10, "weight": 50}]}
            ],
        }
        with self.assertRaises(recommender.RecommendationError):
            recommender._validate(raw, CATALOG)

    def test_serialize_history_is_oldest_first_with_effort_marks(self) -> None:
        # list_workouts() returns newest-first; the serializer flips to oldest-first.
        workouts = [
            {
                "workout_date": "2026-05-29",
                "data": {
                    "load_type": "heavy",
                    "exercises": [
                        {"name": "Жим ногами", "sets": [{"reps": 10, "weight": 120, "effort": "hard"}]}
                    ],
                },
            },
            {
                "workout_date": "2026-05-26",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {"name": "Жим гор.", "sets": [{"reps": 8, "weight": 50, "effort": "easy"}]}
                    ],
                },
            },
        ]
        text = recommender._serialize_history(workouts, 20)
        self.assertTrue(text.splitlines()[0].startswith("2026-05-26"))
        self.assertIn("120кг×10+", text)  # hard → '+'
        self.assertIn("50кг×8-", text)    # easy → '-'

    def test_generate_requires_history(self) -> None:
        with self.assertRaises(recommender.RecommendationError):
            recommender.generate([], [], CATALOG)


class CoachContextTests(unittest.TestCase):
    def _workout(self, when: str, exercise_id: int, sets: int, with_snapshot: bool = False):
        workout = {
            "workout_date": when,
            "data": {
                "load_type": "medium",
                "exercises": [
                    {
                        "exercise_id": exercise_id,
                        "name": "X",
                        "sets": [{"reps": 10, "weight": 50} for _ in range(sets)],
                    }
                ],
            },
        }
        if with_snapshot:
            workout["data"]["recommendation"] = {
                "schema": 1,
                "exercises": [
                    {"exercise_id": exercise_id, "name": "X", "sets": [{"reps": 10, "weight": 50}] * 3},
                    {"exercise_id": 15, "name": "Сгибания ног", "sets": [{"reps": 12, "weight": 40}] * 2},
                ],
            }
        return workout

    def test_plan_adherence_report_compares_fact_vs_plan(self) -> None:
        workouts = [self._workout("2026-06-10", 18, 3, with_snapshot=True)]
        report = recommender._plan_adherence_report(workouts)
        self.assertIn("3/5", report)            # 3 of 5 planned sets done
        self.assertIn("пропущено", report)      # hamstring exercise skipped

    def test_plan_adherence_none_without_snapshots(self) -> None:
        self.assertIsNone(recommender._plan_adherence_report([self._workout("2026-06-10", 18, 3)]))


class ProfileTests(unittest.TestCase):
    def test_load_profile_reads_valid_file(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_profile.json"
            path.write_text(
                '{"schema":1,"blocks":{"Цель":"lean bulk до 84"}}', "utf-8"
            )
            profile = recommender.load_profile(path)
            self.assertIsNotNone(profile)
            self.assertIn("Цель", profile["blocks"])

    def test_load_profile_tolerates_missing_or_garbage(self) -> None:
        import tempfile
        from pathlib import Path

        self.assertIsNone(recommender.load_profile(None))
        self.assertIsNone(recommender.load_profile("/nonexistent/coach_profile.json"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", "utf-8")
            self.assertIsNone(recommender.load_profile(path))
            path.write_text('{"blocks":{}}', "utf-8")
            self.assertIsNone(recommender.load_profile(path))

    def test_system_prompt_embeds_profile_semantics_and_policy(self) -> None:
        profile = {"schema": 1, "blocks": {"Цель": "lean bulk, потолок 84 кг"}}
        prompt = recommender._build_system_prompt(CATALOG, profile)
        self.assertIn("lean bulk, потолок 84 кг", prompt)
        self.assertIn("широчайшие", prompt)              # catalog semantics
        self.assertIn("ТРЕНЕРСКАЯ ПОЛИТИКА", prompt)
        self.assertIn("rationale", prompt)
        # The policy is explicitly defaults; the hard bounds are named apart.
        self.assertIn("ориентиры по умолчанию", prompt)
        self.assertIn("ЖЁСТКИЕ ГРАНИЦЫ", prompt)
        # Planning no longer schedules around the injection cycle.
        self.assertNotIn("гормональный цикл", prompt.lower())
        # The medical boundary must survive the cycle removal.
        self.assertIn("зона лечащего врача", prompt)

    def test_system_prompt_without_profile_uses_fallback(self) -> None:
        prompt = recommender._build_system_prompt(CATALOG)
        self.assertIn("Профиль атлета не настроен", prompt)

    def test_user_prompt_contains_context_volumes_and_weekday(self) -> None:
        from datetime import date

        workouts = [
            {
                "workout_date": "2026-06-10",
                "data": {
                    "load_type": "heavy",
                    "exercises": [
                        {"exercise_id": 8, "name": "Жим ногами", "sets": [{"reps": 10, "weight": 100}]}
                    ],
                },
            }
        ]
        prompt = recommender._build_user_prompt(workouts, [], date(2026, 6, 12), 20)
        self.assertTrue(prompt.startswith("=== КОНТЕКСТ ==="))
        self.assertIn("пятница", prompt)
        self.assertNotIn("гормонального цикла", prompt)
        self.assertIn("Фаза: cut_recomp", prompt)
        self.assertIn("Объём за последние 7 дней", prompt)
        self.assertIn("квадрицепс/ягодичные: 1 прямых / 1 эффективных", prompt)
        self.assertIn("Дней с последней тренировки: 2", prompt)

    def test_user_prompt_flags_return_after_break(self) -> None:
        from datetime import date

        workouts = [
            {
                "workout_date": "2026-05-01",
                "data": {"load_type": "medium", "exercises": [
                    {"exercise_id": 8, "name": "Жим ногами", "sets": [{"reps": 10, "weight": 100}]}
                ]},
            }
        ]
        prompt = recommender._build_user_prompt(workouts, [], date(2026, 6, 12), 20)
        self.assertIn("ВОЗВРАТ ПОСЛЕ ПЕРЕРЫВА", prompt)
        self.assertIn("неделя блока 1", prompt)
        # The comeback methodology is delegated: the context names the single
        # hard bound and prescribes no percentages or set counts.
        self.assertIn("не выше доперерывных", prompt)
        self.assertNotIn("85–90%", prompt)
        self.assertNotIn("10–14 подходов", prompt)


class RestDaysTests(unittest.TestCase):
    def _raw(self, **extra):
        base = {
            "focus": "f",
            "load_type": "medium",
            "rationale": "r",
            "exercises": [
                {"exercise_id": 8, "name": "Жим ногами", "note": "n", "sets": [{"reps": 10, "weight": 100}]}
            ],
        }
        base.update(extra)
        return base

    def test_validate_defaults_rest_days_when_missing(self) -> None:
        self.assertEqual(recommender._validate(self._raw(), CATALOG)["rest_days"], 1)

    def test_validate_clamps_and_coerces_rest_days(self) -> None:
        self.assertEqual(recommender._validate(self._raw(rest_days=99), CATALOG)["rest_days"], recommender.MAX_REST_DAYS)
        self.assertEqual(recommender._validate(self._raw(rest_days=-3), CATALOG)["rest_days"], 0)
        self.assertEqual(recommender._validate(self._raw(rest_days="2"), CATALOG)["rest_days"], 2)

    def test_schema_requires_rest_days(self) -> None:
        schema = recommender._build_schema(CATALOG)
        self.assertIn("rest_days", schema["properties"])
        self.assertIn("rest_days", schema["required"])

    def test_generate_resolves_next_workout_date(self) -> None:
        import os
        from datetime import date

        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        self.addCleanup(lambda: os.environ.pop("ANTHROPIC_API_KEY", None))
        orig = recommender._call_anthropic
        self.addCleanup(lambda: setattr(recommender, "_call_anthropic", orig))
        # Fullbody-ish plan over fresh history → no hard-bound violations.
        raw = self._raw(
            rest_days=2,
            exercises=[
                {"exercise_id": 8, "name": "Жим ногами", "note": "n",
                 "sets": [{"reps": 10, "weight": 100}] * 7},
                {"exercise_id": 9, "name": "Тяга верт.", "note": "n",
                 "sets": [{"reps": 12, "weight": 60}] * 7},
            ],
        )
        recommender._call_anthropic = lambda *a, **k: (raw, {"input_tokens": 1, "output_tokens": 1})

        # Recent history touches every coverage group, so the plan above is clean.
        history = [{
            "workout_date": "2026-06-05",
            "data": {"exercises": [
                {"exercise_id": 8, "name": "Жим ногами", "sets": [{"reps": 10, "weight": 100}] * 2},
                {"exercise_id": 9, "name": "Тяга верт.", "sets": [{"reps": 12, "weight": 60}] * 2},
                {"exercise_id": 18, "name": "Жим в тренажере", "sets": [{"reps": 12, "weight": 50}] * 2},
                {"exercise_id": 15, "name": "Сгибания ног", "sets": [{"reps": 12, "weight": 30}] * 2},
            ]},
        }]
        rec, _usage, _model = recommender.generate(
            history,
            [],
            CATALOG,
            today=date(2026, 6, 12),
        )
        self.assertEqual(rec["rest_days"], 2)
        self.assertEqual(rec["next_workout_date"], "2026-06-14")
        # Phase/cycle context ships with the payload for the iOS client.
        context = rec["coach_context"]
        self.assertEqual(context["phase"], "cut_recomp")
        self.assertEqual(context["block_week"], 2)  # anchored on the 06-05 workout
        self.assertFalse(context["deload_week"])
        self.assertEqual(context["weekly_target"], [7, 10])
        self.assertEqual(context["group_targets"]["грудь"], [7, 10])
        self.assertEqual(context["group_targets"]["бицепс"], [4, 8])


class SerializationTests(unittest.TestCase):
    def test_serialize_history_shows_rir_and_canonical_name(self) -> None:
        catalog = [{"id": 18, "name": "Жим в тренажере"}, {"id": 1, "name": "Жим гор."}]
        workouts = [
            {
                "workout_date": "2026-06-10",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 1,  # duplicate id from old history
                            "name": "Жим гор.",
                            "sets": [
                                {"reps": 10, "weight": 50, "effort": None, "rir": 2},
                            ],
                        }
                    ],
                },
            }
        ]
        text = recommender._serialize_history(workouts, 20, catalog)
        self.assertIn("Жим в тренажере", text)   # renamed onto the canonical id
        self.assertNotIn("Жим гор.", text)
        self.assertIn("50кг×10@2", text)          # RIR shown as @N

    def test_validate_remaps_duplicate_id_to_canonical(self) -> None:
        catalog = [{"id": 18, "name": "Жим в тренажере"}, {"id": 1, "name": "Жим гор."}]
        raw = {
            "focus": "x", "load_type": "medium", "rationale": "r",
            "exercises": [
                {"exercise_id": 1, "name": "Жим гор.", "note": "n",
                 "sets": [{"reps": 10, "weight": 50}]}
            ],
        }
        out = recommender._validate(raw, catalog)
        self.assertEqual(out["exercises"][0]["exercise_id"], 18)
        self.assertEqual(out["exercises"][0]["name"], "Жим в тренажере")


class SemanticValidatorTests(unittest.TestCase):
    """The validator owns exactly two hard bounds (comeback ceiling, group
    coverage). Everything the old validator policed — weight bands, session
    corridors, rep waves, rest_days, load sequencing — is now the model's
    coaching judgement, and these tests pin the freedom down so it doesn't
    silently regrow."""

    CATALOG = [
        {"id": 8, "name": "Жим ногами"},
        {"id": 9, "name": "Тяга верт."},
        {"id": 4, "name": "Гравитрон"},
    ]

    def _history(self, when: str = "2026-06-10", load_type: str = "medium"):
        # Covers every coverage-rule group (chest/back/quads/hamstrings) so the
        # tests below exercise exactly the rule they are about.
        return [
            {
                "workout_date": when,
                "data": {
                    "load_type": load_type,
                    "exercises": [
                        {"exercise_id": 8, "name": "Жим ногами",
                         "sets": [{"reps": 10, "weight": 100}] * 3},
                        {"exercise_id": 9, "name": "Тяга верт.",
                         "sets": [{"reps": 12, "weight": 60}] * 3},
                        {"exercise_id": 18, "name": "Жим в тренажере",
                         "sets": [{"reps": 12, "weight": 50}] * 2},
                        {"exercise_id": 15, "name": "Сгибания ног",
                         "sets": [{"reps": 12, "weight": 30}] * 2},
                    ],
                },
            }
        ]

    def _rec(self, weight: float = 100.0, sets: int = 14, load_type: str = "medium",
             rest_days: int = 1, reps: int = 10, focus: str = "f", rationale: str = "r"):
        first = sets - sets // 2
        return {
            "focus": focus,
            "load_type": load_type,
            "rest_days": rest_days,
            "rationale": rationale,
            "exercises": [
                {"exercise_id": 8, "name": "Жим ногами", "note": "n",
                 "sets": [{"reps": reps, "weight": weight}] * first},
                {"exercise_id": 9, "name": "Тяга верт.", "note": "n",
                 "sets": [{"reps": 12, "weight": 60}] * (sets // 2)},
            ],
        }

    def _violations(self, rec, workouts=None, today=None):
        from datetime import date as _date

        return recommender._semantic_violations(
            rec,
            self.CATALOG,
            workouts if workouts is not None else self._history(),
            today or _date(2026, 6, 12),
        )

    def test_clean_plan_has_no_violations(self) -> None:
        rec = recommender._validate(self._rec(weight=105, sets=14), self.CATALOG)
        self.assertEqual(self._violations(rec), [])

    def test_weight_jumps_are_the_models_call(self) -> None:
        # +40% over the recent range used to be rejected; the model may now
        # judge it (the prompt carries the progression defaults instead).
        rec = recommender._validate(self._rec(weight=140, sets=14), self.CATALOG)
        self.assertEqual(self._violations(rec), [])
        low = recommender._validate(self._rec(weight=60, sets=14), self.CATALOG)
        self.assertEqual(self._violations(low), [])

    def test_session_size_is_the_models_call(self) -> None:
        tiny = recommender._validate(self._rec(sets=5), self.CATALOG)
        self.assertEqual(self._violations(tiny), [])
        big = recommender._validate(self._rec(sets=24), self.CATALOG)
        self.assertEqual(self._violations(big), [])

    def test_rep_ranges_and_load_sequencing_are_the_models_call(self) -> None:
        pump_heavy = recommender._validate(
            self._rec(sets=14, load_type="heavy", reps=14), self.CATALOG
        )
        violations = self._violations(
            pump_heavy, workouts=self._history(load_type="heavy")
        )
        self.assertEqual(violations, [])

    def test_rest_days_are_clamped_not_flagged(self) -> None:
        rec = recommender._validate(self._rec(sets=14, rest_days=6), self.CATALOG)
        self.assertEqual(rec["rest_days"], recommender.MAX_REST_DAYS)
        self.assertEqual(self._violations(rec), [])


class CoverageAndDeloadValidatorTests(unittest.TestCase):
    CATALOG = [
        {"id": 8, "name": "Жим ногами"},
        {"id": 9, "name": "Тяга верт."},
        {"id": 18, "name": "Жим в тренажере"},
        {"id": 15, "name": "Сгибания ног"},
    ]

    def _fullbody(self, when: str, sets_each: int = 2):
        return {
            "workout_date": when,
            "data": {"load_type": "medium", "exercises": [
                {"exercise_id": eid, "name": f"#{eid}",
                 "sets": [{"reps": 10, "weight": 60}] * sets_each}
                for eid in (8, 9, 18, 15)
            ]},
        }

    def _plan(self, ids_sets: list[tuple[int, int]], rationale: str = "r"):
        names = {item["id"]: item["name"] for item in self.CATALOG}
        return {
            "focus": "f", "load_type": "medium", "rest_days": 1,
            "rationale": rationale,
            "exercises": [
                {"exercise_id": eid, "name": names[eid], "note": "n",
                 "sets": [{"reps": 10, "weight": 60}] * count}
                for eid, count in ids_sets
            ],
        }

    def test_dry_group_missing_from_plan_is_flagged(self) -> None:
        from datetime import date as _date

        # Hamstrings (id 15) last trained 12 days ago → dry; the plan skips them.
        workouts = [
            self._fullbody("2026-06-10", sets_each=2),
            {"workout_date": "2026-05-31", "data": {"load_type": "medium", "exercises": [
                {"exercise_id": 15, "name": "Сгибания ног",
                 "sets": [{"reps": 10, "weight": 60}] * 2},
            ]}},
        ]
        workouts[0]["data"]["exercises"] = [
            ex for ex in workouts[0]["data"]["exercises"] if ex["exercise_id"] != 15
        ]
        raw = self._plan([(8, 5), (9, 5), (18, 4)])
        rec = recommender._validate(raw, self.CATALOG)
        violations = recommender._semantic_violations(
            rec, self.CATALOG, workouts, _date(2026, 6, 12)
        )
        self.assertTrue(any("бицепс бедра" in v for v in violations))

        covered = self._plan([(8, 4), (9, 4), (18, 4), (15, 2)])
        rec = recommender._validate(covered, self.CATALOG)
        violations = recommender._semantic_violations(
            rec, self.CATALOG, workouts, _date(2026, 6, 12)
        )
        self.assertEqual(violations, [])

    def test_planned_deload_week_no_longer_constrains_the_plan(self) -> None:
        from datetime import date as _date, timedelta as _timedelta

        import coach_state

        start = _date(2026, 5, 1)
        state = dict(coach_state.DEFAULT_STATE, phase_started=start.isoformat())
        workouts = [
            self._fullbody((start + _timedelta(days=index * 3)).isoformat())
            for index in range(15)
        ]
        today = start + _timedelta(days=42)  # block week 7 → planned deload
        # The flag still reaches the prompt via the context block…
        self.assertTrue(coach_state.cycle_position(state, workouts, today)["deload_week"])

        # …but the validator no longer polices deload volume or weights: how
        # to build the light week is the model's call.
        heavy_volume = self._plan([(8, 5), (9, 5), (18, 4), (15, 2)])  # 16 sets
        rec = recommender._validate(heavy_volume, self.CATALOG)
        violations = recommender._semantic_violations(
            rec, self.CATALOG, workouts, today
        )
        self.assertEqual(violations, [])


class ReturnLadderIsDataOnlyTests(unittest.TestCase):
    """The comeback ladder used to be a validation bound (first rung + one
    step). It is data-only now: on a return the single hard ceiling for EVERY
    movement is the pre-break working weight; the ladder in the prompt guides
    the sessions after it."""

    CATALOG = [{"id": 8, "name": "Жим ногами"}, {"id": 9, "name": "Тяга верт."},
               {"id": 18, "name": "Жим в тренажере"}, {"id": 15, "name": "Сгибания ног"}]

    def _history(self):
        # 40-day break; leg-press peak 120, last working weight 80 → the ladder
        # (90 → 100 → 110 → 120) exists as prompt data, but the return session
        # itself is capped at the pre-break 80.
        return [
            {"workout_date": "2026-05-03", "data": {"load_type": "medium", "exercises": [
                {"exercise_id": 8, "name": "Жим ногами", "sets": [{"reps": 12, "weight": 80}] * 3},
                {"exercise_id": 9, "name": "Тяга верт.", "sets": [{"reps": 12, "weight": 60}] * 3},
                {"exercise_id": 18, "name": "Жим в тренажере", "sets": [{"reps": 12, "weight": 50}] * 2},
                {"exercise_id": 15, "name": "Сгибания ног", "sets": [{"reps": 12, "weight": 30}] * 2},
            ]}},
            {"workout_date": "2026-04-15", "data": {"load_type": "medium", "exercises": [
                {"exercise_id": 8, "name": "Жим ногами", "sets": [{"reps": 12, "weight": 120}] * 3},
            ]}},
            {"workout_date": "2026-04-08", "data": {"load_type": "medium", "exercises": [
                {"exercise_id": 8, "name": "Жим ногами", "sets": [{"reps": 12, "weight": 110}] * 3},
            ]}},
            {"workout_date": "2026-04-01", "data": {"load_type": "medium", "exercises": [
                {"exercise_id": 8, "name": "Жим ногами", "sets": [{"reps": 12, "weight": 100}] * 3},
            ]}},
        ]

    def _plan(self, leg_press_weight: float):
        return {
            "focus": "возврат", "load_type": "medium", "rest_days": 0, "rationale": "r",
            "exercises": [
                {"exercise_id": 8, "name": "Жим ногами", "note": "n",
                 "sets": [{"reps": 12, "weight": leg_press_weight}] * 4},
                {"exercise_id": 9, "name": "Тяга верт.", "note": "n",
                 "sets": [{"reps": 12, "weight": 47.5}] * 4},
                {"exercise_id": 18, "name": "Жим в тренажере", "note": "n",
                 "sets": [{"reps": 12, "weight": 40}] * 2},
                {"exercise_id": 15, "name": "Сгибания ног", "note": "n",
                 "sets": [{"reps": 12, "weight": 22.5}] * 2},
            ],
        }

    def _violations(self, plan):
        from datetime import date as _date

        rec = recommender._validate(plan, self.CATALOG)
        return recommender._semantic_violations(
            rec, self.CATALOG, self._history(), _date(2026, 6, 12)
        )

    def test_ladder_rungs_above_the_pre_break_weight_are_rejected(self) -> None:
        violations = self._violations(self._plan(leg_press_weight=90))
        self.assertTrue(any("не место для прибавки" in v for v in violations))
        violations = self._violations(self._plan(leg_press_weight=120))
        self.assertTrue(any("не место для прибавки" in v for v in violations))

    def test_pre_break_weight_and_below_pass(self) -> None:
        self.assertEqual(self._violations(self._plan(leg_press_weight=80)), [])
        self.assertEqual(self._violations(self._plan(leg_press_weight=60)), [])


class ReturnCeilingTests(unittest.TestCase):
    """After a break EVERY exercise is capped at its pre-break working weight
    — including the ones the athlete left at their peak."""

    CATALOG = [{"id": 8, "name": "Жим ногами"}, {"id": 9, "name": "Тяга верт."},
               {"id": 18, "name": "Жим в тренажере"}, {"id": 15, "name": "Сгибания ног"}]

    def _history(self, last: str = "2026-05-22"):
        # Two identical sessions at the same weights: nothing is below peak,
        # so comeback_ramp_steps yields NO ladder for any movement.
        return [
            {"workout_date": last, "data": {"load_type": "medium", "exercises": [
                {"exercise_id": 8, "name": "Жим ногами", "sets": [{"reps": 12, "weight": 100}] * 3},
                {"exercise_id": 9, "name": "Тяга верт.", "sets": [{"reps": 12, "weight": 60}] * 3},
                {"exercise_id": 18, "name": "Жим в тренажере", "sets": [{"reps": 12, "weight": 50}] * 2},
                {"exercise_id": 15, "name": "Сгибания ног", "sets": [{"reps": 12, "weight": 30}] * 2},
            ]}},
            {"workout_date": "2026-05-19", "data": {"load_type": "medium", "exercises": [
                {"exercise_id": 8, "name": "Жим ногами", "sets": [{"reps": 12, "weight": 100}] * 3},
                {"exercise_id": 9, "name": "Тяга верт.", "sets": [{"reps": 12, "weight": 60}] * 3},
                {"exercise_id": 18, "name": "Жим в тренажере", "sets": [{"reps": 12, "weight": 50}] * 2},
                {"exercise_id": 15, "name": "Сгибания ног", "sets": [{"reps": 12, "weight": 30}] * 2},
            ]}},
        ]

    def _plan(self, leg_press: float):
        return {
            "focus": "возврат", "load_type": "medium", "rest_days": 0, "rationale": "r",
            "exercises": [
                {"exercise_id": 8, "name": "Жим ногами", "note": "n",
                 "sets": [{"reps": 12, "weight": leg_press}] * 4},
                {"exercise_id": 9, "name": "Тяга верт.", "note": "n",
                 "sets": [{"reps": 12, "weight": 50}] * 4},
                {"exercise_id": 18, "name": "Жим в тренажере", "note": "n",
                 "sets": [{"reps": 12, "weight": 42.5}] * 2},
                {"exercise_id": 15, "name": "Сгибания ног", "note": "n",
                 "sets": [{"reps": 12, "weight": 25}] * 2},
            ],
        }

    def _violations(self, plan, today):
        rec = recommender._validate(plan, self.CATALOG)
        return recommender._semantic_violations(
            rec, self.CATALOG, self._history(), today
        )

    def test_progression_after_a_break_is_rejected(self) -> None:
        from datetime import date as _date

        # 21 days off and the model still adds weight to the pre-break 100.
        violations = self._violations(self._plan(leg_press=105), _date(2026, 6, 12))
        self.assertTrue(any("не место для прибавки" in v for v in violations))

    def test_below_pre_break_weight_passes(self) -> None:
        from datetime import date as _date

        # How far below is the coach's call — the server only blocks the plus.
        self.assertEqual(self._violations(self._plan(leg_press=85), _date(2026, 6, 12)), [])
        self.assertEqual(self._violations(self._plan(leg_press=100), _date(2026, 6, 12)), [])

    def test_no_ceilings_outside_a_break(self) -> None:
        from datetime import date as _date

        # Trained 3 days ago: normal progression rules, no comeback guardrail.
        rec = recommender._validate(self._plan(leg_press=105), self.CATALOG)
        violations = recommender._semantic_violations(
            rec, self.CATALOG, self._history(last="2026-06-09"), _date(2026, 6, 12)
        )
        self.assertFalse(any("не место для прибавки" in v for v in violations))

    def test_prompt_states_facts_without_prescribing_numbers(self) -> None:
        import coach_features

        text = coach_features.render_pre_break_weights(
            coach_features.pre_break_working_weights(self._history(), self.CATALOG), 21
        )
        self.assertIn("21 дн.", text)
        self.assertIn("Жим ногами: 100", text)
        # The server states data and defers the judgement; no percentages, no
        # physiology lecture baked into the algorithmic layer.
        self.assertIn("решай сам", text)
        self.assertNotIn("%", text)
        self.assertNotIn("связк", text)


class RequestModelCachingTests(unittest.TestCase):
    def test_body_carries_cache_control_and_optional_schema(self) -> None:
        captured: dict = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({
                    "content": [{"type": "text", "text": "привет"}],
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                }).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _Resp()

        orig = recommender.urllib.request.urlopen
        self.addCleanup(lambda: setattr(recommender.urllib.request, "urlopen", orig))
        recommender.urllib.request.urlopen = fake_urlopen

        text, usage = recommender._request_model(
            "system text", "user text", schema=None,
            model="m", max_tokens=10, api_key="k", timeout=1,
        )
        self.assertEqual(text, "привет")
        body = captured["body"]
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        first_content = body["messages"][0]["content"]
        self.assertEqual(first_content[0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("output_config", body)

        recommender._request_model(
            "system text", "user text", schema={"type": "object"},
            model="m", max_tokens=10, api_key="k", timeout=1,
        )
        self.assertIn("output_config", captured["body"])


class WeeklyReportTests(unittest.TestCase):
    def test_report_prompt_assembles_and_returns_text(self) -> None:
        import os
        from datetime import date as _date

        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        self.addCleanup(lambda: os.environ.pop("ANTHROPIC_API_KEY", None))
        seen: dict = {}

        def fake_request(system, user, **kwargs):
            seen["system"], seen["user"] = system, user
            return "**Итоги недели** — всё по плану.", {"input_tokens": 3, "output_tokens": 4}

        orig = recommender._request_model
        self.addCleanup(lambda: setattr(recommender, "_request_model", orig))
        recommender._request_model = fake_request

        workouts = [{
            "workout_date": "2026-06-10",
            "data": {"load_type": "medium", "exercises": [
                {"exercise_id": 8, "name": "Жим ногами", "sets": [{"reps": 10, "weight": 100}] * 3},
            ]},
        }]
        report, usage, model = recommender.generate_weekly_report(
            workouts, [], [], CATALOG, today=_date(2026, 6, 12)
        )
        self.assertIn("Итоги недели", report)
        self.assertEqual(usage["output_tokens"], 4)
        self.assertIs(seen["system"], recommender.REPORT_SYSTEM_PROMPT)
        self.assertIn("Период отчёта", seen["user"])
        self.assertIn("Тренировки за период (1)", seen["user"])
        self.assertIn("Объём за 7 дней", seen["user"])
        self.assertIn("Новых ПР за период нет.", seen["user"])

    def test_report_requires_api_key(self) -> None:
        import os

        os.environ.pop("ANTHROPIC_API_KEY", None)
        with self.assertRaises(recommender.RecommendationError):
            recommender.generate_weekly_report([], [], [], CATALOG)


class GenerateRepromptTests(unittest.TestCase):
    CATALOG = [
        {"id": 8, "name": "Жим ногами"},
        {"id": 9, "name": "Тяга верт."},
        {"id": 18, "name": "Жим в тренажере"},
        {"id": 15, "name": "Сгибания ног"},
    ]

    def setUp(self) -> None:
        import os

        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        self.addCleanup(lambda: os.environ.pop("ANTHROPIC_API_KEY", None))
        self._orig = recommender._call_anthropic
        self.addCleanup(lambda: setattr(recommender, "_call_anthropic", self._orig))

    def _history(self, when: str = "2026-06-10"):
        return [
            {
                "workout_date": when,
                "data": {"load_type": "medium", "exercises": [
                    {"exercise_id": 8, "name": "Жим ногами",
                     "sets": [{"reps": 10, "weight": 100}] * 3},
                    {"exercise_id": 9, "name": "Тяга верт.",
                     "sets": [{"reps": 12, "weight": 60}] * 3},
                    {"exercise_id": 18, "name": "Жим в тренажере",
                     "sets": [{"reps": 12, "weight": 50}] * 2},
                    {"exercise_id": 15, "name": "Сгибания ног",
                     "sets": [{"reps": 12, "weight": 30}] * 2},
                ]},
            }
        ]

    def _fullbody_raw(self, leg_press: float = 100.0, with_hamstrings: bool = True):
        exercises = [
            {"exercise_id": 8, "note": "n",
             "sets": [{"reps": 10, "weight": leg_press}] * 4},
            {"exercise_id": 9, "note": "n",
             "sets": [{"reps": 12, "weight": 60}] * 4},
            {"exercise_id": 18, "note": "n",
             "sets": [{"reps": 12, "weight": 50}] * 3},
        ]
        if with_hamstrings:
            exercises.append(
                {"exercise_id": 15, "note": "n",
                 "sets": [{"reps": 12, "weight": 30}] * 2}
            )
        return {
            "focus": "f", "load_type": "medium", "rest_days": 1, "rationale": "r",
            "exercises": exercises,
        }

    def _dry_hamstrings_history(self):
        # Hamstrings last trained 12 days ago → the coverage rule demands them.
        workouts = self._history()
        workouts[0]["data"]["exercises"] = [
            ex for ex in workouts[0]["data"]["exercises"] if ex["exercise_id"] != 15
        ]
        workouts.append({
            "workout_date": "2026-05-31",
            "data": {"load_type": "medium", "exercises": [
                {"exercise_id": 15, "name": "Сгибания ног",
                 "sets": [{"reps": 12, "weight": 30}] * 2},
            ]},
        })
        return workouts

    def test_violating_answer_triggers_one_reprompt_then_succeeds(self) -> None:
        from datetime import date as _date

        answers = [
            self._fullbody_raw(with_hamstrings=False),  # dry group missing
            self._fullbody_raw(with_hamstrings=True),   # fixed on retry
        ]
        calls: list[list[dict]] = []

        def fake_call(system, user, schema, **kwargs):
            calls.append(user if isinstance(user, list) else [{"role": "user", "content": user}])
            return answers[len(calls) - 1], {"input_tokens": 10, "output_tokens": 5}

        recommender._call_anthropic = fake_call
        rec, usage, _model, trace = recommender.generate_with_trace(
            self._dry_hamstrings_history(), [], self.CATALOG, today=_date(2026, 6, 12)
        )
        self.assertEqual(len(trace), 2)
        self.assertTrue(any("бицепс бедра" in v for v in trace[0]["violations"]))
        self.assertEqual(trace[1]["violations"], [])
        self.assertEqual(rec["exercises"][-1]["exercise_id"], 15)
        self.assertEqual(usage, {"input_tokens": 20, "output_tokens": 10})
        # The reprompt continues the same conversation and lists the violations.
        self.assertEqual(len(calls[1]), 3)
        self.assertIn("жёсткие границы", calls[1][2]["content"])

    def test_clean_plan_is_served_as_is_without_corridor_trimming(self) -> None:
        from datetime import date as _date

        import coach_state

        calls = 0

        def fake_call(*args, **kwargs):  # noqa: ANN002, ANN003
            nonlocal calls
            calls += 1
            return self._fullbody_raw(), {"input_tokens": 10, "output_tokens": 5}

        recommender._call_anthropic = fake_call
        # 13 sets in maintenance (old corridor was 8–12): served untouched —
        # session size is the model's call now.
        state = dict(coach_state.load_state(None), phase="maintenance")
        rec, _usage, _model, trace = recommender.generate_with_trace(
            self._history(), [], self.CATALOG,
            today=_date(2026, 6, 12), state=state,
        )

        self.assertEqual(calls, 1)
        self.assertEqual(sum(len(exercise["sets"]) for exercise in rec["exercises"]), 13)
        self.assertEqual(trace[0]["adjustments"], [])
        self.assertEqual(trace[0]["violations"], [])
        self.assertNotIn("Проверка методики", rec["rationale"])

    def test_unresolved_coverage_is_served_with_an_honest_note(self) -> None:
        from datetime import date as _date

        # The model ignores the dry hamstrings twice → the plan is still
        # served, with the unmet bound surfaced in the rationale.
        recommender._call_anthropic = lambda *a, **k: (
            self._fullbody_raw(with_hamstrings=False),
            {"input_tokens": 1, "output_tokens": 1},
        )
        rec, usage, _model, trace = recommender.generate_with_trace(
            self._dry_hamstrings_history(), [], self.CATALOG, today=_date(2026, 6, 12)
        )
        self.assertEqual(len(trace), 2)
        self.assertTrue(trace[1]["violations"])
        self.assertIn("Проверка методики", rec["rationale"])
        self.assertIn("бицепс бедра", rec["rationale"])
        self.assertEqual(usage, {"input_tokens": 2, "output_tokens": 2})

    def test_comeback_overshoot_is_clamped_after_a_failed_reprompt(self) -> None:
        from datetime import date as _date

        # 21 days off; the model insists on 105 over the pre-break 100 twice →
        # the server clamps the offending sets and says so in the rationale.
        recommender._call_anthropic = lambda *a, **k: (
            self._fullbody_raw(leg_press=105),
            {"input_tokens": 1, "output_tokens": 1},
        )
        rec, _usage, _model, trace = recommender.generate_with_trace(
            self._history("2026-05-22"), [], self.CATALOG, today=_date(2026, 6, 12)
        )
        self.assertEqual(len(trace), 2)
        self.assertTrue(trace[1]["adjustments"])
        leg_press = next(e for e in rec["exercises"] if e["exercise_id"] == 8)
        self.assertTrue(all(s["weight"] == 100 for s in leg_press["sets"]))
        self.assertIn("Проверка методики", rec["rationale"])
        self.assertIn("доперерывным", rec["rationale"])


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "msg", None, io.BytesIO(b"detail"))


class FetchRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = recommender.urllib.request.urlopen
        self.addCleanup(lambda: setattr(recommender.urllib.request, "urlopen", self._orig))
        self.slept: list[float] = []

    def _fetch(self, max_retries: int = 2):
        return recommender._fetch_anthropic(
            object(),
            timeout=1,
            max_retries=max_retries,
            backoff=0.5,
            sleep=self.slept.append,
        )

    def _patch(self, sequence) -> list[int]:
        calls = {"n": 0}
        it = iter(sequence)

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            item = next(it)
            if isinstance(item, Exception):
                raise item
            return _FakeResponse(item)

        recommender.urllib.request.urlopen = fake_urlopen
        return calls

    def test_retries_transient_then_succeeds(self) -> None:
        calls = self._patch([_http_error(503), b"ok"])
        self.assertEqual(self._fetch(), "ok")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.slept, [0.5])  # one backoff before the 2nd try

    def test_permanent_error_is_not_retried(self) -> None:
        calls = self._patch([_http_error(400)])
        with self.assertRaises(recommender.RecommendationError):
            self._fetch()
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.slept, [])

    def test_exhausts_retries_on_persistent_transient(self) -> None:
        calls = self._patch([_http_error(529), _http_error(529), _http_error(529)])
        with self.assertRaisesRegex(recommender.RecommendationError, "529"):
            self._fetch(max_retries=2)
        self.assertEqual(calls["n"], 3)  # initial + 2 retries
        self.assertEqual(self.slept, [0.5, 1.0])  # exponential backoff

    def test_url_error_retried_then_raised(self) -> None:
        calls = self._patch([urllib.error.URLError("conn reset"), b"ok"])
        self.assertEqual(self._fetch(), "ok")
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
