"""Обхваты кроме талии: нормализация и границы, стор, сводка для отчёта, HTTP API.

Метрики цели из vision (рука, плечи, грудь) приложение не хранило вовсе; теперь
они в таблице ``measurements`` и доходят до недельного отчёта. План их не читает —
совет они не обесценивают.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from support import CATALOG_PATH, JsonHttpClient, running_miniapp_server

from trainer.data import files
from trainer.data.backend_store import MiniAppStore
from trainer.domain import coach_features, coach_state, limits, prompt_builder, recommender, rules

TODAY = date(2026, 9, 6)


class MeasurementNormalizationTests(unittest.TestCase):
    """``normalize_measurement_payload``: вид из словаря, сантиметры в границах, дата."""

    def test_valid_payload(self) -> None:
        normalized = rules.normalize_measurement_payload(
            {
                "entry_date": "2026-09-06",
                "kind": "Arm_Flexed",
                "value_cm": "32.5",
                "notes": " утро ",
            }
        )
        self.assertEqual(normalized["kind"], "arm_flexed")
        self.assertEqual(normalized["value_cm"], 32.5)
        self.assertEqual(normalized["notes"], "утро")

    def test_rejects_unknown_kind_bounds_and_date(self) -> None:
        for payload in (
            {"entry_date": "2026-09-06", "kind": "waist", "value_cm": 90},  # талия — своя таблица
            {"entry_date": "2026-09-06", "kind": None, "value_cm": 32},
            {"entry_date": "2026-09-06", "kind": "neck", "value_cm": 5},
            {"entry_date": "2026-09-06", "kind": "neck", "value_cm": 250},
            {"entry_date": "06.09.2026", "kind": "neck", "value_cm": 39},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                rules.normalize_measurement_payload(payload)

    def test_every_kind_has_a_label_for_the_prompt(self) -> None:
        self.assertIn("arm_flexed", limits.MEASUREMENT_KINDS)
        self.assertTrue(all(label for label in limits.MEASUREMENT_KINDS.values()))
        self.assertNotIn("waist", limits.MEASUREMENT_KINDS)


class MeasurementStoreTests(unittest.TestCase):
    """Стор: один замер вида в день, список по виду, удаление, изоляция по пользователям."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = MiniAppStore(Path(self.temp_dir.name) / "trainer.db")
        self.user = self.store.ensure_debug_user("measurement-tests")

    def test_save_list_upsert_and_delete(self) -> None:
        uid = self.user["id"]
        entry, created = self.store.save_measurement(
            uid, {"entry_date": "2026-09-06", "kind": "arm_flexed", "value_cm": 32.0}
        )
        self.assertTrue(created)
        # Тот же вид за ту же дату — правка, другой вид за ту же дату — новая запись.
        updated, created_again = self.store.save_measurement(
            uid, {"entry_date": "2026-09-06", "kind": "arm_flexed", "value_cm": 32.5}
        )
        self.assertFalse(created_again)
        self.assertEqual(updated["id"], entry["id"])
        self.store.save_measurement(
            uid, {"entry_date": "2026-09-06", "kind": "shoulders", "value_cm": 120.0}
        )
        self.store.save_measurement(
            uid, {"entry_date": "2026-08-09", "kind": "arm_flexed", "value_cm": 31.5}
        )

        arms = self.store.list_measurements(uid, kind="arm_flexed")
        self.assertEqual([e["entry_date"] for e in arms], ["2026-08-09", "2026-09-06"])
        self.assertEqual(arms[-1]["value_cm"], 32.5)
        self.assertEqual(len(self.store.list_measurements(uid)), 3)

        deleted = self.store.delete_measurement(uid, entry["id"])
        assert deleted is not None
        self.assertEqual(deleted["kind"], "arm_flexed")
        self.assertIsNone(self.store.delete_measurement(uid, entry["id"]))
        self.assertEqual(len(self.store.list_measurements(uid)), 2)

    def test_entries_are_isolated_per_user(self) -> None:
        other = self.store.ensure_debug_user("someone-else")
        self.store.save_measurement(
            self.user["id"], {"entry_date": "2026-09-06", "kind": "neck", "value_cm": 39.0}
        )
        self.assertEqual(self.store.list_measurements(other["id"]), [])


class MeasurementOverviewTests(unittest.TestCase):
    """Сводка для отчёта: последний, дней с него, предыдущий; порядок словаря видов."""

    ENTRIES = [
        {"entry_date": "2026-07-12", "kind": "arm_flexed", "value_cm": 31.5},
        {"entry_date": "2026-08-15", "kind": "arm_flexed", "value_cm": 32.0},
        {"entry_date": "2026-08-15", "kind": "shoulders", "value_cm": 120.0},
        {"entry_date": "2026-08-15", "kind": "chest", "value_cm": 5.0},  # описка вне границ
        {"entry_date": "мусор", "kind": "neck", "value_cm": 39.0},
    ]

    def test_overview_rows(self) -> None:
        rows = coach_features.measurement_overview(self.ENTRIES, TODAY)
        self.assertEqual([row["kind"] for row in rows], ["arm_flexed", "shoulders"])
        arm = rows[0]
        self.assertEqual((arm["last_value"], arm["days_since"]), (32.0, 22))
        self.assertEqual((arm["previous_value"], arm["previous_date"]), (31.5, "2026-07-12"))
        self.assertIsNone(rows[1]["previous_value"])
        text = prompt_builder.render_measurement_overview(rows)
        self.assertIn(
            "рука в напряжении: 32 см (2026-08-15, 22 дн. назад; раньше 31.5 от 2026-07-12, +0.5)",
            text,
        )
        self.assertIn("плечи: 120 см (2026-08-15, 22 дн. назад)", text)
        self.assertEqual(coach_features.measurement_overview([], TODAY), [])

    def test_report_prompt_carries_the_overview_or_says_there_is_none(self) -> None:
        def build(measurements):
            return prompt_builder._build_report_prompt(
                [],
                [],
                [],
                files.load_catalog(CATALOG_PATH),
                coach_state.default_state(),
                TODAY,
                7,
                measurements=measurements,
            )

        with_rows = build(self.ENTRIES)
        self.assertIn("Обхваты кроме талии", with_rows)
        self.assertIn("рука в напряжении: 32 см", with_rows)
        self.assertNotIn("Обхватов (рука, плечи", with_rows)
        self.assertIn("Обхватов (рука, плечи, грудь, шея, бедро) ещё нет", build(None))

    def test_measurements_do_not_invalidate_the_advice(self) -> None:
        """План обхваты не читает — обесценивать готовый совет им незачем."""
        self.assertFalse(recommender.advice_invalidated_by("measurement"))
        self.assertTrue(recommender.advice_invalidated_by("waist"))


class MeasurementApiTests(unittest.TestCase):
    """HTTP: список с словарём видов, запись с валидацией, удаление с 404."""

    def test_round_trip_over_http(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            empty = client.request_json("GET", "/api/measurements")
            self.assertEqual(empty.status, 200)
            self.assertEqual(empty.payload["entries"], [])
            self.assertEqual(empty.payload["kinds"]["arm_flexed"], "рука в напряжении")

            created = client.request_json(
                "POST",
                "/api/measurements",
                {"entry_date": "2026-09-06", "kind": "arm_flexed", "value_cm": 32},
            )
            self.assertEqual(created.status, 201)
            entry_id = created.payload["entry"]["id"]
            again = client.request_json(
                "POST",
                "/api/measurements",
                {"entry_date": "2026-09-06", "kind": "arm_flexed", "value_cm": 32.5},
            )
            self.assertEqual(again.status, 200)
            self.assertEqual(again.payload["entry"]["id"], entry_id)

            bad = client.request_json(
                "POST",
                "/api/measurements",
                {"entry_date": "2026-09-06", "kind": "waist", "value_cm": 90},
            )
            self.assertEqual(bad.status, 400)
            self.assertIn("kind", bad.payload["reason"])

            listed = client.request_json("GET", "/api/measurements")
            self.assertEqual([e["value_cm"] for e in listed.payload["entries"]], [32.5])

            gone = client.request_json("DELETE", f"/api/measurements/{entry_id}")
            self.assertEqual(gone.status, 200)
            self.assertTrue(gone.payload["deleted"])
            self.assertEqual(
                client.request_json("DELETE", f"/api/measurements/{entry_id}").status, 404
            )


if __name__ == "__main__":
    unittest.main()
