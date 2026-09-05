from __future__ import annotations

import unittest
from datetime import date

import support  # noqa: F401 — adds backend to sys.path

from trainer.domain import rules


class RetryAndEditSnapshotTests(unittest.TestCase):
    """Связка тренировка ↔ совет живёт снапшотом в payload, и правила её
    сохранности при ретрае и правке — здесь, а не в SQL."""

    def test_retry_backfills_a_missing_snapshot_only(self) -> None:
        existing = {"workout_date": "2026-09-05", "data": {"exercises": []}}
        incoming = {"workout_date": "2026-09-05", "data": {"recommendation": {"focus": "ноги"}}}

        patched = rules.retry_backfills_snapshot(existing, incoming)

        self.assertEqual(patched["data"]["recommendation"], {"focus": "ноги"})
        self.assertNotIn("recommendation", existing["data"], "исходная запись не мутирует")

    def test_retry_never_overwrites_an_existing_snapshot(self) -> None:
        existing = {"data": {"recommendation": {"focus": "старый"}}}
        incoming = {"data": {"recommendation": {"focus": "новый"}}}

        self.assertIsNone(rules.retry_backfills_snapshot(existing, incoming))

    def test_retry_without_snapshot_changes_nothing(self) -> None:
        self.assertIsNone(rules.retry_backfills_snapshot({"data": {}}, {"data": {"exercises": []}}))

    def test_edit_without_snapshot_keeps_the_stored_one(self) -> None:
        existing = {"data": {"recommendation": {"focus": "спина"}}}
        incoming = {"data": {"exercises": []}}

        merged = rules.edit_keeps_snapshot(existing, incoming)

        self.assertEqual(merged["data"]["recommendation"], {"focus": "спина"})

    def test_edit_with_snapshot_wins(self) -> None:
        existing = {"data": {"recommendation": {"focus": "спина"}}}
        incoming = {"data": {"recommendation": {"focus": "грудь"}}}

        self.assertEqual(
            rules.edit_keeps_snapshot(existing, incoming)["data"]["recommendation"],
            {"focus": "грудь"},
        )

    def test_edit_that_explicitly_drops_the_snapshot_keeps_the_drop(self) -> None:
        existing = {"data": {"recommendation": {"focus": "спина"}}}
        incoming = {"data": {"recommendation": None}}

        self.assertIsNone(rules.edit_keeps_snapshot(existing, incoming)["data"]["recommendation"])


class OpenEventTests(unittest.TestCase):
    def test_second_open_event_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rules.check_single_open_event(True)
        rules.check_single_open_event(False)

    def test_todays_new_workout_closes_the_open_event_with_yesterday(self) -> None:
        today = date(2026, 9, 5)
        self.assertEqual(
            rules.open_event_end_after_workout("2026-09-05", True, today), "2026-09-04"
        )

    def test_backdated_or_edited_workout_does_not_touch_the_event(self) -> None:
        today = date(2026, 9, 5)
        self.assertIsNone(rules.open_event_end_after_workout("2026-09-01", True, today))
        self.assertIsNone(rules.open_event_end_after_workout("2026-09-05", False, today))

    def test_event_that_started_today_closes_as_a_one_day_period(self) -> None:
        self.assertEqual(rules.closed_event_end("2026-09-05", "2026-09-04"), "2026-09-05")
        self.assertEqual(rules.closed_event_end("2026-08-30", "2026-09-04"), "2026-09-04")
