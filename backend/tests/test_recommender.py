from __future__ import annotations

import io
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
        self.assertIn("День гормонального цикла", prompt)
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
        # 2×7 sets → inside the 14–20 building corridor, so no reprompt fires.
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

        rec, _usage, _model = recommender.generate(
            [{"workout_date": "2026-06-01", "data": {"exercises": []}}],
            [],
            CATALOG,
            today=date(2026, 6, 12),
        )
        self.assertEqual(rec["rest_days"], 2)
        self.assertEqual(rec["next_workout_date"], "2026-06-14")


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
    CATALOG = [
        {"id": 8, "name": "Жим ногами"},
        {"id": 9, "name": "Тяга верт."},
        {"id": 4, "name": "Гравитрон"},
    ]

    def _history(self, when: str = "2026-06-10", load_type: str = "medium"):
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
                    ],
                },
            }
        ]

    def _rec(self, weight: float = 100.0, sets: int = 14, load_type: str = "medium",
             rest_days: int = 1, focus: str = "f", rationale: str = "r"):
        # Two exercises so the per-exercise 12-set clamp never distorts the
        # session total the corridor check sees.
        first = sets - sets // 2
        return {
            "focus": focus,
            "load_type": load_type,
            "rest_days": rest_days,
            "rationale": rationale,
            "exercises": [
                {"exercise_id": 8, "name": "Жим ногами", "note": "n",
                 "sets": [{"reps": 10, "weight": weight}] * first},
                {"exercise_id": 9, "name": "Тяга верт.", "note": "n",
                 "sets": [{"reps": 12, "weight": 60}] * (sets // 2)},
            ],
        }

    def _violations(self, rec, raw=None, workouts=None, today=None, state=None):
        from datetime import date as _date

        import coach_state

        return recommender._semantic_violations(
            rec,
            raw if raw is not None else rec,
            self.CATALOG,
            workouts if workouts is not None else self._history(),
            today or _date(2026, 6, 12),
            state or coach_state.load_state(None),
        )

    def test_clean_plan_has_no_violations(self) -> None:
        rec = recommender._validate(self._rec(weight=105, sets=14), self.CATALOG)
        self.assertEqual(self._violations(rec), [])

    def test_weight_out_of_range_is_flagged(self) -> None:
        rec = recommender._validate(self._rec(weight=140, sets=14), self.CATALOG)  # +40%
        violations = self._violations(rec)
        self.assertEqual(len(violations), 7)  # every set of the offending exercise
        self.assertTrue(all("выше" in v for v in violations))

    def test_low_weight_allowed_on_return_from_break(self) -> None:
        rec = recommender._validate(self._rec(weight=80, sets=12), self.CATALOG)  # −20%
        # 40 days since the last session → return-from-break, low side waived,
        # and the session corridor becomes 10–14.
        from datetime import date as _date

        violations = self._violations(
            rec, workouts=self._history("2026-05-03"), today=_date(2026, 6, 12)
        )
        self.assertEqual(violations, [])

    def test_low_weight_without_return_is_flagged_unless_deload(self) -> None:
        rec = recommender._validate(self._rec(weight=80, sets=14), self.CATALOG)
        self.assertTrue(any("ниже" in v for v in self._violations(rec)))
        deload = recommender._validate(
            self._rec(weight=80, sets=14, rationale="**Совет:** разгрузочная неделя"),
            self.CATALOG,
        )
        self.assertEqual(self._violations(deload), [])

    def test_session_set_corridor_by_phase(self) -> None:
        import coach_state

        too_few = recommender._validate(self._rec(sets=5), self.CATALOG)
        self.assertTrue(any("коридор" in v for v in self._violations(too_few)))

        maintenance = dict(coach_state.load_state(None), phase="maintenance")
        ok_maintenance = recommender._validate(self._rec(sets=10), self.CATALOG)
        self.assertEqual(self._violations(ok_maintenance, state=maintenance), [])
        too_many = recommender._validate(self._rec(sets=14), self.CATALOG)
        self.assertTrue(
            any("8–12" in v for v in self._violations(too_many, state=maintenance))
        )

    def test_rest_days_and_double_heavy_are_flagged(self) -> None:
        rec = recommender._validate(self._rec(sets=14, rest_days=6), self.CATALOG)
        self.assertTrue(any("rest_days" in v for v in self._violations(rec)))

        heavy = recommender._validate(self._rec(sets=14, load_type="heavy"), self.CATALOG)
        violations = self._violations(heavy, workouts=self._history(load_type="heavy"))
        self.assertTrue(any("две heavy" in v for v in violations))

    def test_name_mismatch_is_flagged(self) -> None:
        raw = self._rec(sets=14)
        raw["exercises"][0]["name"] = "Придуманное имя"
        rec = recommender._validate(raw, self.CATALOG)
        violations = self._violations(rec, raw=raw)
        self.assertTrue(any("дословно" in v for v in violations))

    def test_gravitron_bounds_are_inverted(self) -> None:
        workouts = [
            {
                "workout_date": "2026-06-10",
                "data": {"load_type": "medium", "exercises": [
                    {"exercise_id": 4, "name": "Гравитрон",
                     "sets": [{"reps": 10, "weight": 30}] * 3},
                ]},
            }
        ]
        raw = {
            "focus": "f", "load_type": "medium", "rest_days": 1, "rationale": "r",
            "exercises": [
                {"exercise_id": 4, "name": "Гравитрон", "note": "n",
                 "sets": [{"reps": 10, "weight": 20}] * 14},  # −33% counterweight
            ],
        }
        rec = recommender._validate(raw, self.CATALOG)
        violations = self._violations(rec, raw=raw, workouts=workouts)
        self.assertTrue(any("противовес" in v for v in violations))


class GenerateRepromptTests(unittest.TestCase):
    CATALOG = [{"id": 8, "name": "Жим ногами"}, {"id": 9, "name": "Тяга верт."}]

    def setUp(self) -> None:
        import os

        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        self.addCleanup(lambda: os.environ.pop("ANTHROPIC_API_KEY", None))
        self._orig = recommender._call_anthropic
        self.addCleanup(lambda: setattr(recommender, "_call_anthropic", self._orig))

    def _history(self):
        return [
            {
                "workout_date": "2026-06-10",
                "data": {"load_type": "medium", "exercises": [
                    {"exercise_id": 8, "name": "Жим ногами",
                     "sets": [{"reps": 10, "weight": 100}] * 3},
                    {"exercise_id": 9, "name": "Тяга верт.",
                     "sets": [{"reps": 12, "weight": 60}] * 3},
                ]},
            }
        ]

    def _raw(self, weight: float):
        return {
            "focus": "f", "load_type": "medium", "rest_days": 1, "rationale": "r",
            "exercises": [
                {"exercise_id": 8, "name": "Жим ногами", "note": "n",
                 "sets": [{"reps": 10, "weight": weight}] * 7},
                {"exercise_id": 9, "name": "Тяга верт.", "note": "n",
                 "sets": [{"reps": 12, "weight": 60}] * 7},
            ],
        }

    def test_violating_answer_triggers_one_reprompt_then_succeeds(self) -> None:
        from datetime import date as _date

        answers = [self._raw(140), self._raw(105)]  # +40% → fixed on retry
        calls: list[list[dict]] = []

        def fake_call(system, user, schema, **kwargs):
            calls.append(user if isinstance(user, list) else [{"role": "user", "content": user}])
            return answers[len(calls) - 1], {"input_tokens": 10, "output_tokens": 5}

        recommender._call_anthropic = fake_call
        rec, usage, _model, trace = recommender.generate_with_trace(
            self._history(), [], self.CATALOG, today=_date(2026, 6, 12)
        )
        self.assertEqual(len(trace), 2)
        self.assertTrue(trace[0]["violations"])
        self.assertEqual(trace[1]["violations"], [])
        self.assertEqual(rec["exercises"][0]["sets"][0]["weight"], 105)
        self.assertEqual(usage, {"input_tokens": 20, "output_tokens": 10})
        # The reprompt continues the same conversation and lists the violations.
        self.assertEqual(len(calls[1]), 3)
        self.assertIn("нарушает ограничения", calls[1][2]["content"])

    def test_second_violation_raises_with_details_and_trace(self) -> None:
        from datetime import date as _date

        recommender._call_anthropic = lambda *a, **k: (self._raw(140), {"input_tokens": 1, "output_tokens": 1})
        with self.assertRaises(recommender.RecommendationError) as ctx:
            recommender.generate_with_trace(
                self._history(), [], self.CATALOG, today=_date(2026, 6, 12)
            )
        self.assertIn("дважды", str(ctx.exception))
        self.assertEqual(len(ctx.exception.trace), 2)


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
