"""Недельный отчёт: какую неделю он описывает и когда её можно считать закрытой.

Раньше период отчёта был «последние 7 дней от сегодня», а таймер будил скрипт
в воскресенье вечером — и воскресная тренировка не попадала в отчёт о своей же
неделе. Теперь период — последняя ЗАКРЫТАЯ календарная неделя (пн–вс), и якорь
у всех вызывающих один: `coach_state.last_closed_week_end`.

Второй инвариант тут же: этим же якорем ищет кэш Coach MCP. Разъедутся —
ошибки не будет, инструмент просто промахнётся мимо кэша и сожжёт токены.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from support import sample_workout_payload

from infra.jobs import weekly_report
from trainer.data import backend_store
from trainer.domain import coach_state, recommender


class LastClosedWeekEndTests(unittest.TestCase):
    """Якорь последней закрытой недели."""

    def test_every_weekday_points_at_the_previous_sunday(self) -> None:
        # 2026-08-31 — понедельник; неделя 24–30 августа только что закрылась.
        expected = date(2026, 8, 30)
        monday = date(2026, 8, 31)
        for offset in range(7):  # пн 31.08 … вс 06.09
            today = date.fromordinal(monday.toordinal() + offset)
            with self.subTest(today=today.isoformat()):
                self.assertEqual(coach_state.last_closed_week_end(today), expected)

    def test_sunday_still_reports_the_week_before(self) -> None:
        """Главный смысл правки: в воскресенье неделя ЕЩЁ не закрыта."""
        sunday = date(2026, 8, 30)
        self.assertEqual(coach_state.last_closed_week_end(sunday), date(2026, 8, 23))
        # А в ночь на понедельник — уже она сама.
        self.assertEqual(coach_state.last_closed_week_end(date(2026, 8, 31)), sunday)

    def test_result_is_always_a_sunday_strictly_in_the_past(self) -> None:
        today = date(2026, 1, 1)
        for _ in range(400):
            end = coach_state.last_closed_week_end(today)
            self.assertEqual(end.weekday(), 6)
            self.assertLess(end, today)
            self.assertLessEqual((today - end).days, 7)
            today = date.fromordinal(today.toordinal() + 1)


class ReportPeriodTests(unittest.TestCase):
    """`weekly_report.run` — что уезжает в модель и подо что ложится в кэш."""

    def _store(self) -> tuple[backend_store.MiniAppStore, int]:
        """Стор с одной тренировкой."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = backend_store.MiniAppStore(Path(tmp.name) / "trainer.db")
        uid = int(store.ensure_debug_user("weekly-report")["id"])
        store.save_workout(uid, sample_workout_payload(client_id="w1"))
        return store, uid

    def _capture(self, seen: dict[str, object]):
        """Подменить ``generate_weekly_report`` захватом kwargs."""

        def capture(workouts, body_weights, waists, catalog, **kwargs):
            seen.update(kwargs)
            return "отчёт", {"input_tokens": 1, "output_tokens": 2}, "claude-test"

        original = recommender.generate_weekly_report
        recommender.generate_weekly_report = capture
        self.addCleanup(lambda: setattr(recommender, "generate_weekly_report", original))

    def test_monday_run_covers_the_week_that_just_closed(self) -> None:
        seen: dict[str, object] = {}
        store, uid = self._store()
        self._capture(seen)

        self.assertTrue(weekly_report.run(store, uid, today=date(2026, 8, 31)))

        # Окно данных для модели якорится на воскресенье, а не на сегодня —
        # иначе отчёт «за неделю» начинался бы во вторник.
        self.assertEqual(seen.get("today"), date(2026, 8, 30))
        stored = store.get_coach_report(uid, "2026-08-30", weekly_report.REPORT_DAYS)
        self.assertIsNotNone(stored)

    def test_sunday_evening_workout_is_not_reported_before_its_week_closes(self) -> None:
        seen: dict[str, object] = {}
        store, uid = self._store()
        self._capture(seen)

        weekly_report.run(store, uid, today=date(2026, 8, 30))

        self.assertEqual(seen.get("today"), date(2026, 8, 23))
        self.assertIsNone(store.get_coach_report(uid, "2026-08-30", weekly_report.REPORT_DAYS))

    def test_second_run_in_the_same_week_hits_the_cache(self) -> None:
        seen: dict[str, object] = {}
        store, uid = self._store()
        self._capture(seen)

        self.assertTrue(weekly_report.run(store, uid, today=date(2026, 8, 31)))
        # Вторник, та же закрытая неделя: догоняющий Persistent-запуск таймера
        # не должен переписывать отчёт и тратить токены.
        self.assertFalse(weekly_report.run(store, uid, today=date(2026, 9, 1)))
        self.assertTrue(weekly_report.run(store, uid, today=date(2026, 9, 1), force=True))


if __name__ == "__main__":
    unittest.main()
