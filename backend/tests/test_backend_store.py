"""SQLite-стор и нормализация payload'ов: пользователи, тренировки (дедуп по
client_id, порядок), замеры веса, журнал генераций.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from support import MINIAPP_DIR, sample_body_weight_payload, sample_workout_payload

if str(MINIAPP_DIR) not in sys.path:
    sys.path.insert(0, str(MINIAPP_DIR))

from trainer.data.backend_store import MiniAppStore
from trainer.domain.rules import (
    normalize_body_weight_payload,
    normalize_waist_payload,
    normalize_workout_payload,
)


class MiniAppStoreTest(unittest.TestCase):
    """Стор на временной базе: пользователи, тренировки и взвешивания."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MiniAppStore(Path(self.temp_dir.name) / "trainer.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ensure_debug_user_is_stable_by_alias(self) -> None:
        first_user = self.store.ensure_debug_user("browser-default")
        second_user = self.store.ensure_debug_user("browser-default")

        self.assertEqual(first_user["id"], second_user["id"])
        self.assertTrue(first_user["is_default_debug_user"])
        self.assertEqual(first_user["auth_source"], "debug")

    def test_ensure_debug_user_uses_custom_names_for_new_alias(self) -> None:
        user = self.store.ensure_debug_user("local-qa", first_name="Local", last_name="QA")

        self.assertEqual(user["debug_alias"], "local-qa")
        self.assertEqual(user["display_name"], "Local QA")

    def test_upsert_telegram_user_updates_existing_record(self) -> None:
        first_user = self.store.upsert_telegram_user(
            {
                "id": "777000111",
                "first_name": "Telegram",
                "last_name": "User",
                "username": "first_name",
            },
            auth_source="telegram_unsafe",
        )
        second_user = self.store.upsert_telegram_user(
            {
                "id": 777000111,
                "first_name": "Updated",
                "last_name": "Name",
                "username": "updated_name",
            },
        )

        self.assertEqual(first_user["id"], second_user["id"])
        self.assertEqual(second_user["auth_source"], "telegram")
        self.assertEqual(second_user["username"], "updated_name")
        self.assertEqual(second_user["display_name"], "Updated Name")

    def test_upsert_telegram_user_normalizes_blank_names_and_username(self) -> None:
        user = self.store.upsert_telegram_user(
            {
                "id": 888000111,
                "first_name": "   ",
                "last_name": "   ",
                "username": "   ",
            },
            auth_source="telegram_unsafe",
        )

        self.assertEqual(user["first_name"], "Telegram")
        self.assertIsNone(user["last_name"])
        self.assertIsNone(user["username"])
        self.assertEqual(user["display_name"], "Telegram")

    def test_get_user_by_id_returns_none_for_unknown_user(self) -> None:
        self.assertIsNone(self.store.get_user_by_id(9999))

    def test_save_workout_is_idempotent_per_user_and_client_id(self) -> None:
        user = self.store.ensure_debug_user("browser-default")
        first_workout, first_created = self.store.save_workout(
            int(user["id"]),
            sample_workout_payload(client_id="client-a"),
        )
        second_workout, second_created = self.store.save_workout(
            int(user["id"]),
            sample_workout_payload(client_id="client-a"),
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_workout["id"], second_workout["id"])

    def test_save_workout_allows_same_client_id_for_different_users(self) -> None:
        first_user = self.store.ensure_debug_user("browser-default")
        second_user = self.store.upsert_telegram_user(
            {"id": 901001, "first_name": "Second", "last_name": "User"}
        )

        first_workout, first_created = self.store.save_workout(
            int(first_user["id"]),
            sample_workout_payload(client_id="shared-client-id"),
        )
        second_workout, second_created = self.store.save_workout(
            int(second_user["id"]),
            sample_workout_payload(client_id="shared-client-id"),
        )

        self.assertTrue(first_created)
        self.assertTrue(second_created)
        self.assertNotEqual(first_workout["id"], second_workout["id"])

    def test_list_workouts_orders_same_day_by_newest_id(self) -> None:
        user = self.store.ensure_debug_user("browser-default")
        first_workout, _ = self.store.save_workout(
            int(user["id"]),
            sample_workout_payload(
                client_id="client-a",
                exercise_name="Squat",
                exercise_id=6,
            ),
        )
        second_workout, _ = self.store.save_workout(
            int(user["id"]),
            sample_workout_payload(
                client_id="client-b",
                exercise_name="Pull Up",
                exercise_id=4,
            ),
        )

        workouts = self.store.list_workouts(int(user["id"]))

        self.assertEqual(
            [workouts[0]["id"], workouts[1]["id"]], [second_workout["id"], first_workout["id"]]
        )
        self.assertIn("created_at", workouts[0])
        self.assertIn("updated_at", workouts[0])

    def test_list_workouts_orders_newer_dates_before_older_dates(self) -> None:
        user = self.store.ensure_debug_user("browser-default")
        self.store.save_workout(
            int(user["id"]),
            sample_workout_payload(
                client_id="older-workout",
                workout_date="2026-03-20",
                exercise_name="Older",
                exercise_id=2,
            ),
        )
        self.store.save_workout(
            int(user["id"]),
            sample_workout_payload(
                client_id="newer-workout",
                workout_date="2026-03-28",
                exercise_name="Newer",
                exercise_id=3,
            ),
        )

        workouts = self.store.list_workouts(int(user["id"]))

        self.assertEqual(
            [workouts[0]["workout_date"], workouts[1]["workout_date"]],
            ["2026-03-28", "2026-03-20"],
        )

    def test_list_workouts_returns_empty_list_for_user_without_history(self) -> None:
        user = self.store.ensure_debug_user("browser-default")

        self.assertEqual(self.store.list_workouts(int(user["id"])), [])

    def test_save_workout_persists_normalized_notes_and_set_indexes(self) -> None:
        user = self.store.ensure_debug_user("browser-default")
        workout, created = self.store.save_workout(
            int(user["id"]),
            {
                "client_id": "normalized-workout",
                "workout_date": "2026-03-28",
                "plan_id": None,
                "data": {
                    "notes": "  Session note  ",
                    "load_type": None,
                    "exercises": [
                        {
                            "exercise_id": 5,
                            "name": "Cable Fly",
                            "sets": [
                                {"reps": 12, "weight": 25, "effort": "  easy  ", "notes": "  "},
                                {
                                    "reps": 10,
                                    "weight": 30,
                                    "effort": " HARD ",
                                    "notes": " Last hard set ",
                                },
                            ],
                        }
                    ],
                },
            },
        )

        self.assertTrue(created)
        self.assertEqual(workout["data"]["notes"], "Session note")
        self.assertEqual(
            [workout_set["set_index"] for workout_set in workout["data"]["exercises"][0]["sets"]],
            [1, 2],
        )
        self.assertIsNone(workout["data"]["exercises"][0]["sets"][0]["notes"])
        self.assertEqual(workout["data"]["exercises"][0]["sets"][0]["effort"], "easy")
        self.assertEqual(workout["data"]["exercises"][0]["sets"][1]["notes"], "Last hard set")
        self.assertEqual(workout["data"]["exercises"][0]["sets"][1]["effort"], "hard")

    def test_save_workout_rejects_invalid_set_effort(self) -> None:
        user = self.store.ensure_debug_user("browser-default")

        with self.assertRaisesRegex(ValueError, "Set effort must be one of easy, ok, hard"):
            self.store.save_workout(
                int(user["id"]),
                {
                    "client_id": "invalid-effort",
                    "workout_date": "2026-03-28",
                    "plan_id": None,
                    "data": {
                        "notes": None,
                        "load_type": None,
                        "exercises": [
                            {
                                "exercise_id": 5,
                                "name": "Cable Fly",
                                "sets": [
                                    {"reps": 12, "weight": 25, "effort": "monster"},
                                ],
                            }
                        ],
                    },
                },
            )

    def test_update_workout_rewrites_payload_and_bumps_updated_at(self) -> None:
        user = self.store.ensure_debug_user("browser-default")
        created_workout, _ = self.store.save_workout(
            int(user["id"]),
            sample_workout_payload(
                client_id="editable-workout",
                workout_date="2026-03-20",
                exercise_id=1,
                exercise_name="Bench Press",
                weight=80,
                reps=10,
            ),
        )

        updated_workout = self.store.update_workout(
            int(user["id"]),
            int(created_workout["id"]),
            sample_workout_payload(
                client_id="ignored-on-update",
                workout_date="2026-03-28",
                exercise_id=1,
                exercise_name="Bench Press",
                weight=92.5,
                reps=8,
                notes=" Updated set ",
            ),
        )
        assert updated_workout is not None

        self.assertIsNotNone(updated_workout)
        self.assertEqual(updated_workout["id"], created_workout["id"])
        self.assertEqual(updated_workout["client_id"], "editable-workout")
        self.assertEqual(updated_workout["workout_date"], "2026-03-28")
        self.assertGreaterEqual(updated_workout["updated_at"], created_workout["updated_at"])
        self.assertEqual(updated_workout["data"]["exercises"][0]["sets"][0]["weight"], 92.5)
        self.assertEqual(updated_workout["data"]["exercises"][0]["sets"][0]["reps"], 8)
        self.assertEqual(updated_workout["data"]["notes"], "Updated set")

    def test_update_workout_returns_none_for_other_user(self) -> None:
        first_user = self.store.ensure_debug_user("browser-default")
        second_user = self.store.upsert_telegram_user({"id": 901002, "first_name": "Other"})
        created_workout, _ = self.store.save_workout(
            int(first_user["id"]),
            sample_workout_payload(client_id="protected-workout"),
        )

        updated_workout = self.store.update_workout(
            int(second_user["id"]),
            int(created_workout["id"]),
            sample_workout_payload(client_id="protected-workout", weight=95),
        )

        self.assertIsNone(updated_workout)
        stored_workout = self.store.get_workout_by_id(
            int(first_user["id"]), int(created_workout["id"])
        )
        assert stored_workout is not None
        self.assertIsNotNone(stored_workout)
        self.assertEqual(stored_workout["data"]["exercises"][0]["sets"][0]["weight"], 80.0)

    def test_delete_workout_removes_existing_record(self) -> None:
        user = self.store.ensure_debug_user("browser-default")
        created_workout, _ = self.store.save_workout(
            int(user["id"]),
            sample_workout_payload(client_id="delete-me"),
        )

        deleted_workout = self.store.delete_workout(int(user["id"]), int(created_workout["id"]))
        assert deleted_workout is not None

        self.assertIsNotNone(deleted_workout)
        self.assertEqual(deleted_workout["id"], created_workout["id"])
        self.assertEqual(self.store.list_workouts(int(user["id"])), [])

    def test_delete_workout_returns_none_when_workout_belongs_to_other_user(self) -> None:
        first_user = self.store.ensure_debug_user("browser-default")
        second_user = self.store.upsert_telegram_user({"id": 901003, "first_name": "Other"})
        created_workout, _ = self.store.save_workout(
            int(first_user["id"]),
            sample_workout_payload(client_id="delete-protected"),
        )

        deleted_workout = self.store.delete_workout(
            int(second_user["id"]), int(created_workout["id"])
        )

        self.assertIsNone(deleted_workout)
        self.assertEqual(len(self.store.list_workouts(int(first_user["id"]))), 1)

    def test_save_body_weight_creates_new_entry(self) -> None:
        user = self.store.ensure_debug_user("browser-default")

        entry, created = self.store.save_body_weight(
            int(user["id"]),
            sample_body_weight_payload(entry_date="2026-03-27", weight=82.4),
        )

        self.assertTrue(created)
        self.assertEqual(entry["entry_date"], "2026-03-27")
        self.assertEqual(entry["weight"], 82.4)
        self.assertEqual(self.store.list_body_weights(int(user["id"])), [entry])

    def test_save_body_weight_upserts_same_date_for_same_user(self) -> None:
        user = self.store.ensure_debug_user("browser-default")
        first_entry, first_created = self.store.save_body_weight(
            int(user["id"]),
            sample_body_weight_payload(entry_date="2026-03-27", weight=82.4),
        )
        second_entry, second_created = self.store.save_body_weight(
            int(user["id"]),
            sample_body_weight_payload(entry_date="2026-03-27", weight=81.9, notes=" Updated "),
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_entry["id"], second_entry["id"])
        self.assertEqual(second_entry["weight"], 81.9)
        self.assertEqual(second_entry["notes"], "Updated")
        self.assertEqual(len(self.store.list_body_weights(int(user["id"]))), 1)

    def test_list_body_weights_orders_entries_from_oldest_to_newest(self) -> None:
        user = self.store.ensure_debug_user("browser-default")
        self.store.save_body_weight(
            int(user["id"]),
            sample_body_weight_payload(entry_date="2026-03-28", weight=82.0),
        )
        self.store.save_body_weight(
            int(user["id"]),
            sample_body_weight_payload(entry_date="2026-03-25", weight=83.0),
        )
        self.store.save_body_weight(
            int(user["id"]),
            sample_body_weight_payload(entry_date="2026-03-26", weight=82.7),
        )

        entries = self.store.list_body_weights(int(user["id"]))

        self.assertEqual(
            [entry["entry_date"] for entry in entries],
            ["2026-03-25", "2026-03-26", "2026-03-28"],
        )

    def test_delete_body_weight_removes_existing_entry(self) -> None:
        user = self.store.ensure_debug_user("browser-default")
        entry, _ = self.store.save_body_weight(
            int(user["id"]),
            sample_body_weight_payload(entry_date="2026-03-27", weight=82.4),
        )

        deleted_entry = self.store.delete_body_weight(int(user["id"]), int(entry["id"]))
        assert deleted_entry is not None

        self.assertIsNotNone(deleted_entry)
        self.assertEqual(deleted_entry["id"], entry["id"])
        self.assertEqual(self.store.list_body_weights(int(user["id"])), [])

    def test_delete_body_weight_returns_none_for_other_user(self) -> None:
        first_user = self.store.ensure_debug_user("browser-default")
        second_user = self.store.upsert_telegram_user({"id": 902001, "first_name": "Other"})
        entry, _ = self.store.save_body_weight(
            int(first_user["id"]),
            sample_body_weight_payload(entry_date="2026-03-27", weight=82.4),
        )

        deleted_entry = self.store.delete_body_weight(int(second_user["id"]), int(entry["id"]))

        self.assertIsNone(deleted_entry)
        self.assertEqual(len(self.store.list_body_weights(int(first_user["id"]))), 1)


class NormalizeWorkoutPayloadTest(unittest.TestCase):
    """``rules.normalize_workout_payload``: что принимается, что отвергается и с каким текстом."""

    def test_missing_load_type_stays_unknown(self) -> None:
        # Старый фолбэк по тоннажу (≥3000 кг → heavy) метил тяжёлой почти каждую
        # реальную сессию; отсутствующая метка теперь честно хранится как None.
        payload, client_id = normalize_workout_payload(
            {
                "client_id": "load-unknown",
                "workout_date": "2026-03-28",
                "plan_id": None,
                "data": {
                    "notes": None,
                    "load_type": None,
                    "exercises": [
                        {
                            "exercise_id": 1,
                            "name": "Bench Press",
                            "sets": [
                                {"reps": 10, "weight": 80, "notes": None},
                                {"reps": 10, "weight": 80, "notes": None},
                                {"reps": 10, "weight": 80, "notes": None},
                                {"reps": 10, "weight": 80, "notes": None},
                            ],
                        }
                    ],
                },
            }
        )

        self.assertEqual(client_id, "load-unknown")
        self.assertIsNone(payload["data"]["load_type"])

    def test_preserves_explicit_allowed_load_type(self) -> None:
        payload, _ = normalize_workout_payload(
            {
                "client_id": "load-deload",
                "workout_date": "2026-03-28",
                "plan_id": None,
                "data": {
                    "notes": None,
                    "load_type": "deload",
                    "exercises": [
                        {
                            "exercise_id": 1,
                            "name": "Bench Press",
                            "sets": [{"reps": 12, "weight": 20, "notes": None}],
                        }
                    ],
                },
            }
        )

        self.assertEqual(payload["data"]["load_type"], "deload")

    def test_unknown_load_type_value_is_dropped(self) -> None:
        payload, _ = normalize_workout_payload(
            {
                "client_id": "load-junk",
                "workout_date": "2026-03-28",
                "plan_id": None,
                "data": {
                    "notes": None,
                    "load_type": "extreme",
                    "exercises": [
                        {
                            "exercise_id": 1,
                            "name": "Bench Press",
                            "sets": [{"reps": 10, "weight": 50, "notes": None}],
                        }
                    ],
                },
            }
        )

        self.assertIsNone(payload["data"]["load_type"])

    def test_falls_back_to_payload_id_when_client_id_is_missing(self) -> None:
        payload, client_id = normalize_workout_payload(
            {
                "id": "legacy-workout-id",
                "workout_date": "2026-03-28",
                "plan_id": None,
                "data": {
                    "notes": None,
                    "load_type": None,
                    "exercises": [
                        {
                            "exercise_id": 1,
                            "name": "Bench Press",
                            "sets": [{"reps": 12, "weight": 80, "notes": None}],
                        }
                    ],
                },
            }
        )

        self.assertEqual(client_id, "legacy-workout-id")
        self.assertEqual(payload["workout_date"], "2026-03-28")

    def test_normalizes_workout_and_set_notes(self) -> None:
        payload, _ = normalize_workout_payload(
            {
                "client_id": "notes-normalized",
                "workout_date": "2026-03-28",
                "plan_id": None,
                "data": {
                    "notes": "  Workout note  ",
                    "load_type": None,
                    "exercises": [
                        {
                            "exercise_id": 1,
                            "name": "Bench Press",
                            "sets": [{"reps": 12, "weight": 80, "notes": "  "}],
                        }
                    ],
                },
            }
        )

        self.assertEqual(payload["data"]["notes"], "Workout note")
        self.assertIsNone(payload["data"]["exercises"][0]["sets"][0]["notes"])

    def test_assigns_set_indexes_sequentially(self) -> None:
        payload, _ = normalize_workout_payload(
            {
                "client_id": "indexed-sets",
                "workout_date": "2026-03-28",
                "plan_id": None,
                "data": {
                    "notes": None,
                    "load_type": None,
                    "exercises": [
                        {
                            "exercise_id": 1,
                            "name": "Bench Press",
                            "sets": [
                                {"reps": 12, "weight": 80, "notes": None},
                                {"reps": 10, "weight": 82.5, "notes": None},
                            ],
                        }
                    ],
                },
            }
        )

        self.assertEqual(
            [workout_set["set_index"] for workout_set in payload["data"]["exercises"][0]["sets"]],
            [1, 2],
        )

    def test_rejects_workout_without_exercises(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one exercise"):
            normalize_workout_payload(
                {
                    "client_id": "invalid",
                    "workout_date": "2026-03-28",
                    "plan_id": None,
                    "data": {"notes": None, "load_type": None, "exercises": []},
                }
            )

    def test_rejects_non_object_exercise_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "Exercise payload must be an object"):
            normalize_workout_payload(
                {
                    "client_id": "invalid-exercise-object",
                    "workout_date": "2026-03-28",
                    "plan_id": None,
                    "data": {"notes": None, "load_type": None, "exercises": ["not-an-object"]},
                }
            )

    def test_rejects_non_integer_exercise_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "exercise_id must be an integer"):
            normalize_workout_payload(
                {
                    "client_id": "invalid-exercise-id",
                    "workout_date": "2026-03-28",
                    "plan_id": None,
                    "data": {
                        "notes": None,
                        "load_type": None,
                        "exercises": [
                            {
                                "exercise_id": "1",
                                "name": "Bench",
                                "sets": [{"reps": 10, "weight": 80}],
                            }
                        ],
                    },
                }
            )

    def test_rejects_blank_exercise_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "Exercise name is required"):
            normalize_workout_payload(
                {
                    "client_id": "invalid-exercise-name",
                    "workout_date": "2026-03-28",
                    "plan_id": None,
                    "data": {
                        "notes": None,
                        "load_type": None,
                        "exercises": [
                            {"exercise_id": 1, "name": "   ", "sets": [{"reps": 10, "weight": 80}]}
                        ],
                    },
                }
            )

    def test_rejects_exercise_without_sets(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one set"):
            normalize_workout_payload(
                {
                    "client_id": "invalid-sets",
                    "workout_date": "2026-03-28",
                    "plan_id": None,
                    "data": {
                        "notes": None,
                        "load_type": None,
                        "exercises": [{"exercise_id": 1, "name": "Bench", "sets": []}],
                    },
                }
            )

    def test_rejects_non_numeric_set_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "Set reps must be an integer"):
            normalize_workout_payload(
                {
                    "client_id": "invalid-set-values",
                    "workout_date": "2026-03-28",
                    "plan_id": None,
                    "data": {
                        "notes": None,
                        "load_type": None,
                        "exercises": [
                            {
                                "exercise_id": 1,
                                "name": "Bench",
                                "sets": [{"reps": "abc", "weight": "xyz", "notes": None}],
                            }
                        ],
                    },
                }
            )

    def test_rejects_sets_with_zero_reps(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 1"):
            normalize_workout_payload(
                {
                    "client_id": "invalid-reps",
                    "workout_date": "2026-03-28",
                    "plan_id": None,
                    "data": {
                        "notes": None,
                        "load_type": None,
                        "exercises": [
                            {"exercise_id": 1, "name": "Bench", "sets": [{"reps": 0, "weight": 80}]}
                        ],
                    },
                }
            )

    def test_rejects_sets_with_negative_weight(self) -> None:
        with self.assertRaisesRegex(ValueError, "Set weight must be at least 0"):
            normalize_workout_payload(
                {
                    "client_id": "invalid-weight",
                    "workout_date": "2026-03-28",
                    "plan_id": None,
                    "data": {
                        "notes": None,
                        "load_type": None,
                        "exercises": [
                            {
                                "exercise_id": 1,
                                "name": "Bench",
                                "sets": [{"reps": 10, "weight": -5}],
                            }
                        ],
                    },
                }
            )

    def test_rejects_invalid_workout_date_format(self) -> None:
        # «20260328» в 3.11+ прошёл бы через date.fromisoformat, на VPS (3.10) — нет;
        # формат один для любого интерпретатора: только YYYY-MM-DD.
        for workout_date in ("28-03-2026", "20260328"):
            with (
                self.subTest(workout_date=workout_date),
                self.assertRaisesRegex(ValueError, "YYYY-MM-DD"),
            ):
                normalize_workout_payload(
                    {
                        "client_id": "invalid-date",
                        "workout_date": workout_date,
                        "plan_id": None,
                        "data": {
                            "notes": None,
                            "load_type": None,
                            "exercises": [
                                {
                                    "exercise_id": 1,
                                    "name": "Bench",
                                    "sets": [{"reps": 10, "weight": 80}],
                                }
                            ],
                        },
                    }
                )

    def test_requires_client_id_or_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "client_id is required"):
            normalize_workout_payload(
                {
                    "workout_date": "2026-03-28",
                    "plan_id": None,
                    "data": {
                        "notes": None,
                        "load_type": None,
                        "exercises": [
                            {
                                "exercise_id": 1,
                                "name": "Bench",
                                "sets": [{"reps": 10, "weight": 80}],
                            }
                        ],
                    },
                }
            )


class NormalizeBodyWeightPayloadTest(unittest.TestCase):
    """Нормализация взвешивания и замера талии: даты и правдоподобные границы."""

    def test_normalizes_valid_body_weight_payload(self) -> None:
        payload = normalize_body_weight_payload(
            {
                "entry_date": "2026-03-28",
                "weight": "82.4",
                "notes": "  Morning  ",
            }
        )

        self.assertEqual(payload["entry_date"], "2026-03-28")
        self.assertEqual(payload["weight"], 82.4)
        self.assertEqual(payload["notes"], "Morning")

    def test_rejects_invalid_body_weight_date(self) -> None:
        for entry_date in ("28-03-2026", "20260328"):
            with (
                self.subTest(entry_date=entry_date),
                self.assertRaisesRegex(ValueError, "YYYY-MM-DD"),
            ):
                normalize_body_weight_payload({"entry_date": entry_date, "weight": 82.4})

    def test_rejects_invalid_waist_date(self) -> None:
        for entry_date in ("28-03-2026", "20260328"):
            with (
                self.subTest(entry_date=entry_date),
                self.assertRaisesRegex(ValueError, "YYYY-MM-DD"),
            ):
                normalize_waist_payload({"entry_date": entry_date, "waist": 90.0})

    def test_rejects_non_positive_body_weight(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 30 and 400"):
            normalize_body_weight_payload(
                {
                    "entry_date": "2026-03-28",
                    "weight": 0,
                }
            )

    def test_rejects_implausible_body_weight(self) -> None:
        # Баг «22 кг: вес с тренажёра, вбитый в поле взвешивания».
        with self.assertRaisesRegex(ValueError, "between"):
            normalize_body_weight_payload({"entry_date": "2026-03-28", "weight": 22})
        with self.assertRaisesRegex(ValueError, "between"):
            normalize_body_weight_payload({"entry_date": "2026-03-28", "weight": 600})

    def test_accepts_plausible_edge_weights(self) -> None:
        self.assertEqual(
            normalize_body_weight_payload({"entry_date": "2026-03-28", "weight": 30})["weight"],
            30.0,
        )
        self.assertEqual(
            normalize_body_weight_payload({"entry_date": "2026-03-28", "weight": 400})["weight"],
            400.0,
        )


class RecommendationLogTest(unittest.TestCase):
    """Append-only журнал генераций рядом с перезаписываемым кэшем."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = MiniAppStore(Path(self.temp_dir.name) / "trainer.db")
        self.uid = int(self.store.ensure_debug_user("log-user")["id"])

    def _rec(self, focus: str) -> dict:
        """Минимальный совет с заданным фокусом."""
        return {
            "focus": focus,
            "load_type": "medium",
            "rationale": "r",
            "exercises": [
                {"exercise_id": 1, "name": "X", "note": "", "sets": [{"reps": 10, "weight": 50}]}
            ],
        }

    def test_save_appends_to_log_each_time(self) -> None:
        self.store.save_recommendation(self.uid, 1, 5, "m", self._rec("A"), 10, 20)
        self.store.save_recommendation(self.uid, 2, 6, "m", self._rec("B"), 11, 21)
        log = self.store.list_recommendation_log(self.uid)
        self.assertEqual([e["recommendation"]["focus"] for e in log], ["B", "A"])  # новые сверху
        self.assertEqual(log[0]["status"], "ready")
        self.assertEqual(log[0]["input_tokens"], 11)

    def test_failed_generation_is_logged(self) -> None:
        self.store.fail_recommendation(self.uid, "ANTHROPIC_API_KEY не настроен")
        log = self.store.list_recommendation_log(self.uid)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["status"], "failed")
        self.assertIn("ANTHROPIC_API_KEY", log[0]["error"])

    def test_pending_is_not_logged(self) -> None:
        self.store.set_recommendation_pending(self.uid)
        self.assertEqual(self.store.list_recommendation_log(self.uid), [])

    def test_log_is_isolated_per_user(self) -> None:
        other = int(self.store.ensure_debug_user("log-user-2")["id"])
        self.store.save_recommendation(self.uid, 1, 5, "m", self._rec("mine"), 1, 2)
        self.assertEqual(self.store.list_recommendation_log(other), [])


if __name__ == "__main__":
    unittest.main()
