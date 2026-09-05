"""Граничные случаи слоя domain: bool, nan, inf и дроби во входе тренировок,
снапшотов, параметров фазы, ответа модели и рендеров.
"""

from __future__ import annotations

import json
import math
import unittest
from copy import deepcopy
from datetime import date
from unittest import mock

import support  # noqa: F401 — кладёт backend в sys.path
from support import sample_workout_payload

from trainer.data.anthropic_client import RecommendationError
from trainer.domain import (
    coach_features,
    coach_signals,
    coach_state,
    plan_validator,
    prompt_builder,
    rules,
)

CATALOG = [{"id": 18, "name": "Жим в тренажере"}]


def _snapshot(sets: list[dict[str, object]]) -> dict[str, object]:
    """Снапшот совета с заданными подходами."""
    return {
        "focus": "Тест",
        "load_type": "medium",
        "exercises": [
            {
                "exercise_id": 18,
                "name": "Жим в тренажере",
                "sets": sets,
            }
        ],
    }


def _plan(sets: object, *, rest_days: object = 1) -> dict[str, object]:
    """Сырой ответ модели с заданными подходами и ``rest_days``."""
    return {
        "focus": "Тест",
        "load_type": "medium",
        "rest_days": rest_days,
        "rationale": "Причина",
        "exercises": [
            {
                "exercise_id": 18,
                "name": "подменённое имя",
                "note": None,
                "sets": sets,
            }
        ],
    }


class WorkoutRuleEdgeCaseTests(unittest.TestCase):
    """``rules``: форма тренировки и RIR."""

    def test_workout_requires_an_object_data_block(self) -> None:
        for invalid in (None, [], "payload", 7):
            payload = sample_workout_payload(client_id="invalid-data")
            payload["data"] = invalid
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(
                    ValueError,
                    "data must be an object",
                ),
            ):
                rules.normalize_workout_payload(payload)

    def test_workout_rejects_boolean_ids_reps_and_weights(self) -> None:
        cases = (("exercise_id", True), ("reps", True), ("weight", False))
        for field, value in cases:
            payload = sample_workout_payload(client_id=f"bool-{field}")
            exercise = payload["data"]["exercises"][0]
            if field == "exercise_id":
                exercise[field] = value
            else:
                exercise["sets"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                rules.normalize_workout_payload(payload)

    def test_workout_rejects_fractional_reps(self) -> None:
        payload = sample_workout_payload(client_id="fractional-reps")
        payload["data"]["exercises"][0]["sets"][0]["reps"] = 8.5

        with self.assertRaisesRegex(ValueError, "integer"):
            rules.normalize_workout_payload(payload)

    def test_workout_rejects_every_non_finite_weight(self) -> None:
        for weight in (math.nan, math.inf, -math.inf, "nan", "inf", "-inf"):
            payload = sample_workout_payload(client_id="non-finite", weight=weight)
            with self.subTest(weight=weight), self.assertRaisesRegex(ValueError, "finite"):
                rules.normalize_workout_payload(payload)

    def test_rir_rejects_fractional_and_non_finite_numbers(self) -> None:
        for value in (1.5, math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "integer"):
                rules.normalize_set_rir(value)


class SnapshotRuleEdgeCaseTests(unittest.TestCase):
    """Снапшот совета: не-числа и лимит размера в байтах."""

    def test_snapshot_drops_non_finite_boolean_and_fractional_sets(self) -> None:
        snapshot = _snapshot(
            [
                {"reps": 10, "weight": math.nan},
                {"reps": 10, "weight": math.inf},
                {"reps": True, "weight": 50},
                {"reps": 8.5, "weight": 50},
                {"reps": 10, "weight": False},
                {"reps": 9, "weight": 52.5},
            ]
        )

        normalized = rules.normalize_recommendation_snapshot(snapshot)
        assert normalized is not None

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["exercises"][0]["sets"], [{"reps": 9, "weight": 52.5}])

    def test_snapshot_with_only_non_finite_weights_is_discarded(self) -> None:
        for weight in (math.nan, math.inf, -math.inf):
            with self.subTest(weight=weight):
                self.assertIsNone(
                    rules.normalize_recommendation_snapshot(
                        _snapshot([{"reps": 10, "weight": weight}])
                    )
                )

    def test_snapshot_limit_counts_utf8_bytes_not_unicode_characters(self) -> None:
        snapshot = {
            "focus": "😀" * 200,
            "exercises": [
                {
                    "exercise_id": exercise_id,
                    "name": "😀" * 120,
                    "sets": [{"reps": 12, "weight": 100.5} for _ in range(12)],
                }
                for exercise_id in range(1, 11)
            ],
        }
        with mock.patch.object(rules, "MAX_RECOMMENDATION_SNAPSHOT_BYTES", 100_000):
            normalized = rules.normalize_recommendation_snapshot(snapshot)
        serialized = json.dumps(normalized, ensure_ascii=False)
        self.assertLess(len(serialized), rules.MAX_RECOMMENDATION_SNAPSHOT_BYTES)
        self.assertGreater(
            len(serialized.encode("utf-8")),
            rules.MAX_RECOMMENDATION_SNAPSHOT_BYTES,
        )

        self.assertIsNone(rules.normalize_recommendation_snapshot(snapshot))


class CoachStateEdgeCaseTests(unittest.TestCase):
    """Состояние: независимые дефолты, атомарность смены фазы, нормализация."""

    def test_default_state_returns_independent_nested_containers(self) -> None:
        first = coach_state.default_state()
        second = coach_state.default_state()

        first["phase_history"].append({"phase": "cut_recomp"})
        first["phase_params"]["cut_recomp"] = {"calories": [1, 2]}

        self.assertEqual(second["phase_history"], [])
        self.assertEqual(second["phase_params"], {})
        self.assertEqual(coach_state.DEFAULT_STATE["phase_history"], [])
        self.assertEqual(coach_state.DEFAULT_STATE["phase_params"], {})

    def test_invalid_phase_parameters_do_not_partially_mutate_state(self) -> None:
        state = coach_state.default_state()
        state["phase_started"] = "2026-08-01"
        before = deepcopy(state)

        with self.assertRaisesRegex(ValueError, "Неизвестный параметр"):
            coach_state.switch_phase(
                state,
                "lean_bulk",
                {"not_a_parameter": 1},
                today=date(2026, 9, 1),
            )

        self.assertEqual(state, before)

    def test_phase_overrides_reject_non_finite_scalars_and_ranges(self) -> None:
        bad_params = (
            {"target_weight_kg": math.nan},
            {"session_sets": [8, math.inf]},
            {"group_targets": {"спина": [8, -math.inf]}},
        )
        for params in bad_params:
            with self.subTest(params=params), self.assertRaises(ValueError):
                coach_state.switch_phase(coach_state.default_state(), "lean_bulk", params)

    def test_normalize_state_filters_bad_history_and_measurements(self) -> None:
        normalized = coach_state.normalize_state(
            {
                "phase": "unknown",
                "phase_started": "not-a-date",
                "phase_history": [
                    None,
                    {"phase": "unknown", "started": "2026-01-01", "ended": "2026-02-01"},
                    {"phase": "cut_recomp", "started": "bad", "ended": "2026-02-01"},
                ],
                "waist_limit_cm": True,
                "waist_base_cm": math.nan,
            }
        )

        self.assertEqual(normalized["phase"], "cut_recomp")
        self.assertIsNone(normalized["phase_started"])
        self.assertEqual(
            normalized["phase_history"],
            [{"phase": "cut_recomp", "started": None, "ended": "2026-02-01"}],
        )
        self.assertIsNone(normalized["waist_limit_cm"])
        self.assertIsNone(normalized["waist_base_cm"])


class PlanSanitizerEdgeCaseTests(unittest.TestCase):
    """Санитизация ответа модели и потолок сессии."""

    def test_validate_skips_non_finite_and_non_integer_sets(self) -> None:
        recommendation = plan_validator._validate(
            _plan(
                [
                    {"reps": 10, "weight": math.nan},
                    {"reps": 10, "weight": math.inf},
                    {"reps": True, "weight": 50},
                    {"reps": 8.5, "weight": 50},
                    {"reps": 9, "weight": 52.5},
                ]
            ),
            CATALOG,
        )

        self.assertEqual(recommendation["exercises"][0]["name"], "Жим в тренажере")
        self.assertEqual(
            recommendation["exercises"][0]["sets"],
            [{"reps": 9, "weight": 52.5}],
        )

    def test_validate_rejects_a_plan_with_only_non_finite_weights(self) -> None:
        with self.assertRaisesRegex(RecommendationError, "ни одного валидного"):
            plan_validator._validate(
                _plan([{"reps": 10, "weight": math.nan}]),
                CATALOG,
            )

    def test_validate_treats_malformed_collections_as_an_empty_plan(self) -> None:
        malformed = (
            {**_plan([]), "exercises": 7},
            _plan(7),
            _plan({"reps": 10, "weight": 50}),
        )
        for raw in malformed:
            with self.subTest(raw=raw), self.assertRaises(RecommendationError):
                plan_validator._validate(raw, CATALOG)

    def test_validate_defaults_overflowing_rest_days_without_crashing(self) -> None:
        recommendation = plan_validator._validate(
            _plan([{"reps": 10, "weight": 50}], rest_days=math.inf),
            CATALOG,
        )

        self.assertEqual(recommendation["rest_days"], 1)

    def test_session_cap_rejects_negative_non_finite_and_malformed_values(self) -> None:
        invalid = (
            {},
            {"session_sets": None},
            {"session_sets": [8]},
            {"session_sets": [8, -1]},
            {"session_sets": [8, math.inf]},
            {"session_sets": [8, "many"]},
        )
        for params in invalid:
            with self.subTest(params=params):
                self.assertIsNone(plan_validator._session_cap(params))
        self.assertEqual(plan_validator._session_cap({"session_sets": [8, "12"]}), 12)


class DefensiveRenderingEdgeCaseTests(unittest.TestCase):
    """Фичи, сериализация и отсрочки на битом входе."""

    def test_feature_sessions_ignore_malformed_and_non_finite_sets(self) -> None:
        workouts = [
            {
                "workout_date": "2026-09-01",
                "data": {
                    "exercises": [
                        {
                            "exercise_id": 18,
                            "sets": [
                                None,
                                {"reps": 10, "weight": math.nan},
                                {"reps": True, "weight": 50},
                                {"reps": 8, "weight": 52.5},
                            ],
                        }
                    ]
                },
            }
        ]

        sessions = coach_features._iter_exercise_sessions(workouts)

        self.assertEqual(
            sessions,
            {18: [(date(2026, 9, 1), [{"reps": 8, "weight": 52.5, "effort": None, "rir": None}])]},
        )

    def test_canonical_id_rejects_boolean_fractional_and_non_finite_values(self) -> None:
        for value in (True, 1.5, math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                self.assertIsNone(coach_features.canonical_exercise_id(value))

    def test_recent_weight_range_ignores_non_finite_legacy_values(self) -> None:
        workout = {
            "workout_date": "2026-09-01",
            "data": {
                "exercises": [
                    {
                        "exercise_id": 18,
                        "sets": [
                            {"reps": 10, "weight": math.nan},
                            {"reps": 10, "weight": math.inf},
                            {"reps": 10, "weight": 50},
                        ],
                    }
                ]
            },
        }

        self.assertEqual(
            coach_features.recent_weight_range([workout], 18, date(2026, 9, 5)),
            (50.0, 50.0),
        )

    def test_raw_history_omits_non_finite_and_malformed_sets(self) -> None:
        workout = {
            "workout_date": "2026-09-01",
            "data": {
                "exercises": [
                    {
                        "exercise_id": 18,
                        "name": "Жим",
                        "sets": [
                            None,
                            {"reps": 10, "weight": math.nan},
                            {"reps": 10, "weight": 50},
                        ],
                    }
                ]
            },
        }

        rendered = prompt_builder._serialize_workout(workout)

        self.assertNotIn("nan", rendered.lower())
        self.assertIn("50кг×10", rendered)

    def test_default_snooze_uses_severity_policy_and_unknown_is_ephemeral(self) -> None:
        now = 1_000
        self.assertEqual(coach_signals.default_snooze_until("info", now), now + 72 * 3600)
        self.assertEqual(coach_signals.default_snooze_until("warn", now), now + 48 * 3600)
        self.assertIsNone(coach_signals.default_snooze_until("accent", now))
        self.assertIsNone(coach_signals.default_snooze_until("critical", now))


if __name__ == "__main__":
    unittest.main()
