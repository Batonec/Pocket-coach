from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from support import MINIAPP_DIR, sample_workout_payload

if str(MINIAPP_DIR) not in sys.path:
    sys.path.insert(0, str(MINIAPP_DIR))

from backend_store import (  # noqa: E402
    MiniAppStore,
    normalize_set_rir,
    normalize_waist_payload,
    normalize_workout_payload,
)


class WaistNormalizationTests(unittest.TestCase):
    def test_valid_payload(self) -> None:
        normalized = normalize_waist_payload(
            {"entry_date": "2026-08-14", "waist": "84.5", "notes": " утро "}
        )
        self.assertEqual(normalized["waist"], 84.5)
        self.assertEqual(normalized["notes"], "утро")

    def test_rejects_bad_date_and_bounds(self) -> None:
        with self.assertRaises(ValueError):
            normalize_waist_payload({"entry_date": "14.08.2026", "waist": 84})
        with self.assertRaises(ValueError):
            normalize_waist_payload({"entry_date": "2026-08-14", "waist": 20})
        with self.assertRaises(ValueError):
            normalize_waist_payload({"entry_date": "2026-08-14", "waist": "мало"})


class WaistStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = MiniAppStore(Path(self.temp_dir.name) / "trainer.db")
        self.user = self.store.ensure_debug_user("waist-tests")

    def test_save_list_upsert_and_delete(self) -> None:
        entry, created = self.store.save_waist(
            self.user["id"], {"entry_date": "2026-08-14", "waist": 84.5}
        )
        self.assertTrue(created)

        updated, created_again = self.store.save_waist(
            self.user["id"], {"entry_date": "2026-08-14", "waist": 84.0}
        )
        self.assertFalse(created_again)
        self.assertEqual(updated["id"], entry["id"])

        self.store.save_waist(self.user["id"], {"entry_date": "2026-08-07", "waist": 85.0})
        entries = self.store.list_waists(self.user["id"])
        self.assertEqual([e["entry_date"] for e in entries], ["2026-08-07", "2026-08-14"])
        self.assertEqual(entries[-1]["waist"], 84.0)

        deleted = self.store.delete_waist(self.user["id"], entry["id"])
        self.assertEqual(deleted["entry_date"], "2026-08-14")
        self.assertIsNone(self.store.delete_waist(self.user["id"], entry["id"]))
        self.assertEqual(len(self.store.list_waists(self.user["id"])), 1)

    def test_waists_are_isolated_per_user(self) -> None:
        other = self.store.ensure_debug_user("someone-else")
        self.store.save_waist(self.user["id"], {"entry_date": "2026-08-14", "waist": 84.5})
        self.assertEqual(self.store.list_waists(other["id"]), [])


class SetRirTests(unittest.TestCase):
    def test_normalize_set_rir_values(self) -> None:
        self.assertIsNone(normalize_set_rir(None))
        self.assertIsNone(normalize_set_rir(""))
        self.assertEqual(normalize_set_rir(0), 0)
        self.assertEqual(normalize_set_rir("3"), 3)
        for bad in (5, -1, True, "полтора"):
            with self.assertRaises(ValueError):
                normalize_set_rir(bad)

    def test_workout_payload_keeps_rir(self) -> None:
        payload = sample_workout_payload(client_id="w-rir")
        payload["data"]["exercises"][0]["sets"][0]["rir"] = 2
        normalized, _client_id = normalize_workout_payload(payload)
        self.assertEqual(normalized["data"]["exercises"][0]["sets"][0]["rir"], 2)

        payload["data"]["exercises"][0]["sets"][0]["rir"] = 7
        with self.assertRaises(ValueError):
            normalize_workout_payload(payload)

    def test_rir_round_trips_through_the_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MiniAppStore(Path(tmp) / "trainer.db")
            user = store.ensure_debug_user("rir-tests")
            payload = sample_workout_payload(client_id="w-1")
            payload["data"]["exercises"][0]["sets"][0]["rir"] = 1
            saved, _created = store.save_workout(user["id"], payload)
            self.assertEqual(saved["data"]["exercises"][0]["sets"][0]["rir"], 1)


if __name__ == "__main__":
    unittest.main()
