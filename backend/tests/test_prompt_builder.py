from __future__ import annotations

import unittest

import support  # noqa: F401 — adds backend to sys.path

import plan_validator
import prompt_builder


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
        self.assertIn("3/5", report)  # 3 of 5 planned sets done
        self.assertIn("пропущено", report)  # hamstring exercise skipped

    def test_plan_adherence_none_without_snapshots(self) -> None:
        self.assertIsNone(
            prompt_builder._plan_adherence_report([self._workout("2026-06-10", 18, 3)])
        )


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
        text = prompt_builder._serialize_history(workouts, 20, catalog)
        self.assertIn("Жим в тренажере", text)  # renamed onto the canonical id
        self.assertNotIn("Жим гор.", text)
        self.assertIn("50кг×10@2", text)  # RIR shown as @N

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
        out = plan_validator._validate(raw, catalog)
        self.assertEqual(out["exercises"][0]["exercise_id"], 18)
        self.assertEqual(out["exercises"][0]["name"], "Жим в тренажере")
