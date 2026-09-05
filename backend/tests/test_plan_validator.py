from __future__ import annotations

import unittest

import support  # noqa: F401 — adds backend to sys.path

import plan_validator

CATALOG = [
    {"id": 8, "name": "Жим ногами"},
    {"id": 9, "name": "Тяга верт."},
    {"id": 1, "name": "Жим гор."},
]


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
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 10, "weight": 100}] * 3,
                        },
                        {
                            "exercise_id": 9,
                            "name": "Тяга верт.",
                            "sets": [{"reps": 12, "weight": 60}] * 3,
                        },
                        {
                            "exercise_id": 18,
                            "name": "Жим в тренажере",
                            "sets": [{"reps": 12, "weight": 50}] * 2,
                        },
                        {
                            "exercise_id": 15,
                            "name": "Сгибания ног",
                            "sets": [{"reps": 12, "weight": 30}] * 2,
                        },
                    ],
                },
            }
        ]

    def _rec(
        self,
        weight: float = 100.0,
        sets: int = 14,
        load_type: str = "medium",
        rest_days: int = 1,
        reps: int = 10,
        focus: str = "f",
        rationale: str = "r",
    ):
        first = sets - sets // 2
        return {
            "focus": focus,
            "load_type": load_type,
            "rest_days": rest_days,
            "rationale": rationale,
            "exercises": [
                {
                    "exercise_id": 8,
                    "name": "Жим ногами",
                    "note": "n",
                    "sets": [{"reps": reps, "weight": weight}] * first,
                },
                {
                    "exercise_id": 9,
                    "name": "Тяга верт.",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 60}] * (sets // 2),
                },
            ],
        }

    def _violations(self, rec, workouts=None, today=None):
        from datetime import date as _date

        return plan_validator._semantic_violations(
            rec,
            self.CATALOG,
            workouts if workouts is not None else self._history(),
            today or _date(2026, 6, 12),
        )

    def test_clean_plan_has_no_violations(self) -> None:
        rec = plan_validator._validate(self._rec(weight=105, sets=14), self.CATALOG)
        self.assertEqual(self._violations(rec), [])

    def test_weight_jumps_are_the_models_call(self) -> None:
        # +40% over the recent range used to be rejected; the model may now
        # judge it (the prompt carries the progression defaults instead).
        rec = plan_validator._validate(self._rec(weight=140, sets=14), self.CATALOG)
        self.assertEqual(self._violations(rec), [])
        low = plan_validator._validate(self._rec(weight=60, sets=14), self.CATALOG)
        self.assertEqual(self._violations(low), [])

    def test_session_size_is_capped_by_the_phase_but_never_padded(self) -> None:
        from datetime import date as _date

        tiny = plan_validator._validate(self._rec(sets=5), self.CATALOG)
        big = plan_validator._validate(self._rec(sets=24), self.CATALOG)
        # Without a cap (no phase parameters) the size is the model's call.
        self.assertEqual(self._violations(tiny), [])
        self.assertEqual(self._violations(big), [])
        # With the phase cap only the UPPER bound is enforced: a short session
        # can be a decision, an oversized one is not.
        capped = lambda rec: plan_validator._semantic_violations(  # noqa: E731
            rec, self.CATALOG, self._history(), _date(2026, 6, 12), session_cap=20
        )
        self.assertEqual(capped(tiny), [])
        violations = capped(big)
        self.assertEqual(len(violations), 1)
        self.assertIn("24 рабочих подходов при потолке сессии 20", violations[0])
        self.assertIn("начиная с изоляции", violations[0])

    def test_rep_ranges_and_load_sequencing_are_the_models_call(self) -> None:
        pump_heavy = plan_validator._validate(
            self._rec(sets=14, load_type="heavy", reps=14), self.CATALOG
        )
        violations = self._violations(pump_heavy, workouts=self._history(load_type="heavy"))
        self.assertEqual(violations, [])

    def test_rest_days_are_clamped_not_flagged(self) -> None:
        rec = plan_validator._validate(self._rec(sets=14, rest_days=6), self.CATALOG)
        self.assertEqual(rec["rest_days"], plan_validator.MAX_REST_DAYS)
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
            "data": {
                "load_type": "medium",
                "exercises": [
                    {
                        "exercise_id": eid,
                        "name": f"#{eid}",
                        "sets": [{"reps": 10, "weight": 60}] * sets_each,
                    }
                    for eid in (8, 9, 18, 15)
                ],
            },
        }

    def _plan(self, ids_sets: list[tuple[int, int]], rationale: str = "r"):
        names = {item["id"]: item["name"] for item in self.CATALOG}
        return {
            "focus": "f",
            "load_type": "medium",
            "rest_days": 1,
            "rationale": rationale,
            "exercises": [
                {
                    "exercise_id": eid,
                    "name": names[eid],
                    "note": "n",
                    "sets": [{"reps": 10, "weight": 60}] * count,
                }
                for eid, count in ids_sets
            ],
        }

    def test_dry_group_missing_from_plan_is_flagged(self) -> None:
        from datetime import date as _date

        # Hamstrings (id 15) last trained 12 days ago → dry; the plan skips them.
        workouts = [
            self._fullbody("2026-06-10", sets_each=2),
            {
                "workout_date": "2026-05-31",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 15,
                            "name": "Сгибания ног",
                            "sets": [{"reps": 10, "weight": 60}] * 2,
                        },
                    ],
                },
            },
        ]
        workouts[0]["data"]["exercises"] = [
            ex for ex in workouts[0]["data"]["exercises"] if ex["exercise_id"] != 15
        ]
        raw = self._plan([(8, 5), (9, 5), (18, 4)])
        rec = plan_validator._validate(raw, self.CATALOG)
        violations = plan_validator._semantic_violations(
            rec, self.CATALOG, workouts, _date(2026, 6, 12)
        )
        self.assertTrue(any("бицепс бедра" in v for v in violations))

        covered = self._plan([(8, 4), (9, 4), (18, 4), (15, 2)])
        rec = plan_validator._validate(covered, self.CATALOG)
        violations = plan_validator._semantic_violations(
            rec, self.CATALOG, workouts, _date(2026, 6, 12)
        )
        self.assertEqual(violations, [])

    def test_planned_deload_week_no_longer_constrains_the_plan(self) -> None:
        from datetime import date as _date
        from datetime import timedelta as _timedelta

        import coach_state

        start = _date(2026, 5, 1)
        state = dict(coach_state.DEFAULT_STATE, phase_started=start.isoformat())
        workouts = [
            self._fullbody((start + _timedelta(days=index * 3)).isoformat()) for index in range(15)
        ]
        today = start + _timedelta(days=42)  # block week 7 → planned deload
        # The flag still reaches the prompt via the context block…
        self.assertTrue(coach_state.cycle_position(state, workouts, today)["deload_week"])

        # …but the validator no longer polices deload volume or weights: how
        # to build the light week is the model's call.
        heavy_volume = self._plan([(8, 5), (9, 5), (18, 4), (15, 2)])  # 16 sets
        rec = plan_validator._validate(heavy_volume, self.CATALOG)
        violations = plan_validator._semantic_violations(rec, self.CATALOG, workouts, today)
        self.assertEqual(violations, [])


class ReturnLadderIsDataOnlyTests(unittest.TestCase):
    """The comeback ladder used to be a validation bound (first rung + one
    step). It is data-only now: on a return the single hard ceiling for EVERY
    movement is the pre-break working weight; the ladder in the prompt guides
    the sessions after it."""

    CATALOG = [
        {"id": 8, "name": "Жим ногами"},
        {"id": 9, "name": "Тяга верт."},
        {"id": 18, "name": "Жим в тренажере"},
        {"id": 15, "name": "Сгибания ног"},
    ]

    def _history(self):
        # 40-day break; leg-press peak 120, last working weight 80 → the ladder
        # (90 → 100 → 110 → 120) exists as prompt data, but the return session
        # itself is capped at the pre-break 80.
        return [
            {
                "workout_date": "2026-05-03",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 12, "weight": 80}] * 3,
                        },
                        {
                            "exercise_id": 9,
                            "name": "Тяга верт.",
                            "sets": [{"reps": 12, "weight": 60}] * 3,
                        },
                        {
                            "exercise_id": 18,
                            "name": "Жим в тренажере",
                            "sets": [{"reps": 12, "weight": 50}] * 2,
                        },
                        {
                            "exercise_id": 15,
                            "name": "Сгибания ног",
                            "sets": [{"reps": 12, "weight": 30}] * 2,
                        },
                    ],
                },
            },
            {
                "workout_date": "2026-04-15",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 12, "weight": 120}] * 3,
                        },
                    ],
                },
            },
            {
                "workout_date": "2026-04-08",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 12, "weight": 110}] * 3,
                        },
                    ],
                },
            },
            {
                "workout_date": "2026-04-01",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 12, "weight": 100}] * 3,
                        },
                    ],
                },
            },
        ]

    def _plan(self, leg_press_weight: float):
        return {
            "focus": "возврат",
            "load_type": "medium",
            "rest_days": 0,
            "rationale": "r",
            "exercises": [
                {
                    "exercise_id": 8,
                    "name": "Жим ногами",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": leg_press_weight}] * 4,
                },
                {
                    "exercise_id": 9,
                    "name": "Тяга верт.",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 47.5}] * 4,
                },
                {
                    "exercise_id": 18,
                    "name": "Жим в тренажере",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 40}] * 2,
                },
                {
                    "exercise_id": 15,
                    "name": "Сгибания ног",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 22.5}] * 2,
                },
            ],
        }

    def _violations(self, plan):
        from datetime import date as _date

        rec = plan_validator._validate(plan, self.CATALOG)
        return plan_validator._semantic_violations(
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

    CATALOG = [
        {"id": 8, "name": "Жим ногами"},
        {"id": 9, "name": "Тяга верт."},
        {"id": 18, "name": "Жим в тренажере"},
        {"id": 15, "name": "Сгибания ног"},
    ]

    def _history(self, last: str = "2026-05-22"):
        # Two identical sessions at the same weights: nothing is below peak,
        # so comeback_ramp_steps yields NO ladder for any movement.
        return [
            {
                "workout_date": last,
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 12, "weight": 100}] * 3,
                        },
                        {
                            "exercise_id": 9,
                            "name": "Тяга верт.",
                            "sets": [{"reps": 12, "weight": 60}] * 3,
                        },
                        {
                            "exercise_id": 18,
                            "name": "Жим в тренажере",
                            "sets": [{"reps": 12, "weight": 50}] * 2,
                        },
                        {
                            "exercise_id": 15,
                            "name": "Сгибания ног",
                            "sets": [{"reps": 12, "weight": 30}] * 2,
                        },
                    ],
                },
            },
            {
                "workout_date": "2026-05-19",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 12, "weight": 100}] * 3,
                        },
                        {
                            "exercise_id": 9,
                            "name": "Тяга верт.",
                            "sets": [{"reps": 12, "weight": 60}] * 3,
                        },
                        {
                            "exercise_id": 18,
                            "name": "Жим в тренажере",
                            "sets": [{"reps": 12, "weight": 50}] * 2,
                        },
                        {
                            "exercise_id": 15,
                            "name": "Сгибания ног",
                            "sets": [{"reps": 12, "weight": 30}] * 2,
                        },
                    ],
                },
            },
        ]

    def _plan(self, leg_press: float):
        return {
            "focus": "возврат",
            "load_type": "medium",
            "rest_days": 0,
            "rationale": "r",
            "exercises": [
                {
                    "exercise_id": 8,
                    "name": "Жим ногами",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": leg_press}] * 4,
                },
                {
                    "exercise_id": 9,
                    "name": "Тяга верт.",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 50}] * 4,
                },
                {
                    "exercise_id": 18,
                    "name": "Жим в тренажере",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 42.5}] * 2,
                },
                {
                    "exercise_id": 15,
                    "name": "Сгибания ног",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 25}] * 2,
                },
            ],
        }

    def _violations(self, plan, today):
        rec = plan_validator._validate(plan, self.CATALOG)
        return plan_validator._semantic_violations(rec, self.CATALOG, self._history(), today)

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
        rec = plan_validator._validate(self._plan(leg_press=105), self.CATALOG)
        violations = plan_validator._semantic_violations(
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
