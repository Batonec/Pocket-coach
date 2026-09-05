"""Сборка промпта: отчёт «факт против плана», сериализация истории с RIR и
каноническими именами, состав user-промпта недельного отчёта."""

from __future__ import annotations

import unittest
from datetime import date

import support  # noqa: F401 — кладёт backend в sys.path
from support import CATALOG_PATH

from trainer.data import files
from trainer.domain import coach_state, prompt_builder, rules


class CoachContextTests(unittest.TestCase):
    """Отчёт дисциплины по снапшотам."""

    def _workout(self, when: str, exercise_id: int, sets: int, with_snapshot: bool = False):
        """Тренировка с ``sets`` подходами и, по флагу, снапшотом плана из двух упражнений."""
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
                    {
                        "exercise_id": exercise_id,
                        "name": "X",
                        "sets": [{"reps": 10, "weight": 50}] * 3,
                    },
                    {
                        "exercise_id": 15,
                        "name": "Сгибания ног",
                        "sets": [{"reps": 12, "weight": 40}] * 2,
                    },
                ],
            }
        return workout

    def test_plan_adherence_report_compares_fact_vs_plan(self) -> None:
        workouts = [self._workout("2026-06-10", 18, 3, with_snapshot=True)]
        report = prompt_builder._plan_adherence_report(workouts)
        assert report is not None
        self.assertIn("3/5", report)  # 3 из 5 плановых сетов выполнены
        self.assertIn("пропущено", report)  # сгибания ног пропущены

    def test_plan_adherence_none_without_snapshots(self) -> None:
        self.assertIsNone(
            prompt_builder._plan_adherence_report([self._workout("2026-06-10", 18, 3)])
        )


class SerializationTests(unittest.TestCase):
    """Сериализация истории и ремап дубля в валидаторе."""

    def test_serialize_history_shows_rir_and_canonical_name(self) -> None:
        catalog = [{"id": 18, "name": "Жим в тренажере"}, {"id": 1, "name": "Жим гор."}]
        workouts = [
            {
                "workout_date": "2026-06-10",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 1,  # дублирующий id из старой истории
                            "name": "Жим гор.",
                            "sets": [
                                {"reps": 10, "weight": 50, "effort": None, "rir": 2},
                            ],
                        }
                    ],
                },
            }
        ]
        text = prompt_builder._serialize_history(workouts, 20, catalog)
        self.assertIn("Жим в тренажере", text)  # переименовано на канонический id
        self.assertNotIn("Жим гор.", text)
        self.assertIn("50кг×10@2", text)  # RIR показан как @N

    def test_validate_remaps_duplicate_id_to_canonical(self) -> None:
        catalog = [{"id": 18, "name": "Жим в тренажере"}, {"id": 1, "name": "Жим гор."}]
        raw = {
            "focus": "x",
            "load_type": "medium",
            "rationale": "r",
            "exercises": [
                {
                    "exercise_id": 1,
                    "name": "Жим гор.",
                    "note": "n",
                    "sets": [{"reps": 10, "weight": 50}],
                }
            ],
        }
        out = rules.normalize_model_plan(raw, catalog)
        self.assertEqual(out["exercises"][0]["exercise_id"], 18)
        self.assertEqual(out["exercises"][0]["name"], "Жим в тренажере")


class ReportPromptTests(unittest.TestCase):
    """User-промпт недельного отчёта: что в нём есть кроме тренировок недели."""

    TODAY = date(2026, 6, 14)  # воскресенье закрытой недели 8–14 июня

    def _workout(self, when: str, weight: float) -> dict:
        """Жим ногами тремя подходами на одном весе."""
        return {
            "workout_date": when,
            "data": {
                "load_type": "medium",
                "exercises": [
                    {
                        "exercise_id": 8,
                        "name": "Жим ногами",
                        "sets": [{"reps": 10, "weight": weight, "effort": "hard"}] * 3,
                    }
                ],
            },
        }

    def _report(self, workouts: list[dict]) -> str:
        """Промпт отчёта на настоящем каталоге и состоянии по умолчанию."""
        return prompt_builder._build_report_prompt(
            workouts,
            [],
            [],
            files.load_catalog(CATALOG_PATH),
            coach_state.default_state(),
            self.TODAY,
            7,
        )

    def test_summaries_give_the_report_a_baseline_beyond_the_week(self) -> None:
        """Отчёт видит одну неделю сырых подходов; «движение весов» и силовой гейт
        подтверждаются только сводкой по тренажёрам — той же, что читает план."""
        prompt = self._report([self._workout("2026-06-12", 105), self._workout("2026-06-03", 100)])
        self.assertIn("Сводка по тренажёрам за ВСЮ историю", prompt)
        self.assertIn("Жим ногами: пик 105×10", prompt)
        # Легенда стоит у сырых строк недели — метка «+» иначе читалась бы наугад.
        self.assertIn("Тренировки за период (1), от старых к новым. Формат строки", prompt)
        self.assertIn("105кг×10+", prompt)

    def test_single_session_history_has_no_summary_block(self) -> None:
        prompt = self._report([self._workout("2026-06-12", 105)])
        self.assertNotIn("Сводка по тренажёрам", prompt)
