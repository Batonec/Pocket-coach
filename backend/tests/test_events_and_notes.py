"""События (периоды без тренировок с причиной) и заметки атлета.

Инвариант фичи один и проверяется здесь с трёх сторон: из событий и заметок
не считается НИ ОДНОГО числа — они доезжают до модели текстом и только текстом.
Поэтому тесты смотрят на нормализацию (что вообще можно записать), на
хранилище (одно открытое событие, автозакрытие тренировкой) и на промпт
(куда именно встают строки и что при этом не меняется).
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from support import JsonHttpClient, running_miniapp_server, sample_workout_payload

from infra.jobs import refresh_recommendation, weekly_report
from trainer.data import backend_store, coach_prompts
from trainer.domain import coach_state, prompt_builder, recommender, rules

CATALOG = [
    {"id": 8, "name": "Жим ногами"},
    {"id": 9, "name": "Тяга верт."},
]

TODAY = date(2026, 6, 12)


def _workout(
    when: str,
    *,
    exercise_id: int = 8,
    name: str = "Жим ногами",
    sets: list[dict[str, Any]] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Тренировка на дату с одним упражнением и заметкой."""
    return {
        "workout_date": when,
        "data": {
            "load_type": "medium",
            "notes": notes,
            "exercises": [
                {
                    "exercise_id": exercise_id,
                    "name": name,
                    "sets": sets or [{"reps": 10, "weight": 100}],
                }
            ],
        },
    }


class EventNormalizationTests(unittest.TestCase):
    """``rules.normalize_event_payload``: даты, период, будущее, текст."""

    def test_dates_are_strict_iso_on_every_python_and_text_is_trimmed(self) -> None:
        # В 3.11+ date.fromisoformat принимает и «20260801», на VPS (3.10) — нет.
        # Формат один для любого интерпретатора: только YYYY-MM-DD, иначе периоды
        # перестали бы сравниваться как строки.
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            rules.normalize_event_payload(
                {"start_date": "20260801", "end_date": "2026-08-05", "text": "болел"}
            )
        normalized = rules.normalize_event_payload(
            {"start_date": " 2026-08-01 ", "end_date": "2026-08-05", "text": "  болел  "}
        )
        self.assertEqual(normalized["start_date"], "2026-08-01")
        self.assertEqual(normalized["end_date"], "2026-08-05")
        self.assertEqual(normalized["text"], "болел")

    def test_missing_end_date_means_the_event_is_still_running(self) -> None:
        for payload in (
            {"start_date": "2026-08-01", "text": "болею"},
            {"start_date": "2026-08-01", "end_date": None, "text": "болею"},
            {"start_date": "2026-08-01", "end_date": "   ", "text": "болею"},
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(rules.normalize_event_payload(payload)["end_date"])

    def test_one_day_event_is_a_valid_period(self) -> None:
        normalized = rules.normalize_event_payload(
            {"start_date": "2026-08-01", "end_date": "2026-08-01", "text": "отравился"}
        )
        self.assertEqual(normalized["end_date"], normalized["start_date"])

    def test_rejects_broken_dates_and_a_reversed_period(self) -> None:
        for payload in (
            {"start_date": "01.08.2026", "text": "болел"},
            {"start_date": None, "text": "болел"},
            {"start_date": "2026-08-01", "end_date": "2026-02-30", "text": "болел"},
            {"start_date": "2026-08-05", "end_date": "2026-08-01", "text": "болел"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                rules.normalize_event_payload(payload)

    def test_rejects_future_dates(self) -> None:
        """Планирование отложено: событие описывает то, что уже случилось."""
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):
            rules.normalize_event_payload({"start_date": tomorrow, "text": "отпуск"})
        with self.assertRaises(ValueError):
            rules.normalize_event_payload(
                {"start_date": "2026-08-01", "end_date": tomorrow, "text": "отпуск"}
            )
        # Сегодня — уже не будущее: событие можно завести в день его начала.
        today = date.today().isoformat()
        self.assertEqual(
            rules.normalize_event_payload({"start_date": today, "text": "заболел"})["start_date"],
            today,
        )

    def test_text_is_required(self) -> None:
        """Событие без причины — это просто дырка в датах, она и так видна."""
        for text in (None, "", "   "):
            with self.subTest(text=text), self.assertRaises(ValueError):
                rules.normalize_event_payload({"start_date": "2026-08-01", "text": text})


class EventStoreTests(unittest.TestCase):
    """События в сторе: порядок, отдельные записи, одно открытое, закрытие."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = backend_store.MiniAppStore(Path(self.temp_dir.name) / "trainer.db")
        self.user = self.store.ensure_debug_user("event-tests")

    def _save(self, start: str, end: str | None, text: str) -> dict[str, Any]:
        """Записать событие через стор."""
        return self.store.save_event(
            self.user["id"], {"start_date": start, "end_date": end, "text": text}
        )

    def test_list_is_newest_first(self) -> None:
        old = self._save("2026-08-01", "2026-08-03", "командировка")
        same_day = self._save("2026-08-01", "2026-08-01", "отравился")
        fresh = self._save("2026-08-10", None, "болею")

        listed = self.store.list_events(self.user["id"])
        # Событие читают рядом с дыркой, которую оно объясняет, поэтому сверху
        # свежее; при равной дате начала новее то, что записано позже.
        self.assertEqual([e["id"] for e in listed], [fresh["id"], same_day["id"], old["id"]])

    def test_events_with_the_same_start_date_are_separate_records(self) -> None:
        """Ключа апсерта у события нет: болезнь и командировка могут начаться
        в один день, и это две разные записи."""
        first = self._save("2026-08-01", "2026-08-03", "командировка")
        second = self._save("2026-08-01", "2026-08-05", "болел")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.list_events(self.user["id"])), 2)

    def test_update_and_delete_round_trip(self) -> None:
        event = self._save("2026-08-01", None, "болею")

        updated = self.store.update_event(
            self.user["id"],
            event["id"],
            {"start_date": "2026-08-02", "end_date": "2026-08-06", "text": "  болел, теперь ок  "},
        )
        assert updated is not None
        self.assertEqual(updated["id"], event["id"])
        self.assertEqual(updated["start_date"], "2026-08-02")
        self.assertEqual(updated["end_date"], "2026-08-06")
        self.assertEqual(updated["text"], "болел, теперь ок")

        deleted = self.store.delete_event(self.user["id"], event["id"])
        assert deleted is not None
        self.assertEqual(deleted["id"], event["id"])
        self.assertEqual(self.store.list_events(self.user["id"]), [])

    def test_unknown_id_is_not_found_rather_than_an_error(self) -> None:
        payload = {"start_date": "2026-08-01", "end_date": "2026-08-02", "text": "болел"}
        self.assertIsNone(self.store.update_event(self.user["id"], 999, payload))
        self.assertIsNone(self.store.delete_event(self.user["id"], 999))

    def test_only_one_event_stays_open(self) -> None:
        """Открытое событие — состояние «сейчас не тренируюсь», и оно одно:
        иначе автозакрытие не смогло бы выбрать, какой период закрывать."""
        open_event = self._save("2026-08-10", None, "болею")
        closed = self._save("2026-08-01", "2026-08-03", "командировка")

        with self.assertRaises(ValueError):
            self._save("2026-08-12", None, "ещё одно")
        with self.assertRaises(ValueError):
            self.store.update_event(
                self.user["id"], closed["id"], {"start_date": "2026-08-01", "text": "открыть"}
            )

        # Правка самого открытого события открытым его и оставляет — это не
        # «второе открытое», а то же самое.
        still_open = self.store.update_event(
            self.user["id"], open_event["id"], {"start_date": "2026-08-09", "text": "болею дальше"}
        )
        assert still_open is not None
        self.assertIsNone(still_open["end_date"])

    def test_close_open_event_is_idempotent(self) -> None:
        self._save("2026-08-01", "2026-08-03", "командировка")
        opened = self._save("2026-08-10", None, "болею")

        closed = self.store.close_open_event(self.user["id"], "2026-08-15")
        assert closed is not None
        self.assertEqual(closed["id"], opened["id"])
        self.assertEqual(closed["end_date"], "2026-08-15")
        # Вызывается на каждой созданной тренировке — второй раз закрывать нечего.
        self.assertIsNone(self.store.close_open_event(self.user["id"], "2026-08-16"))

    def test_close_never_ends_the_event_before_it_started(self) -> None:
        """«Заболел утром, вечером всё же потренировался»: автозакрытие ставит
        вчера, а событие началось сегодня — выходит однодневный период."""
        today = date.today()
        self.store.save_event(self.user["id"], {"start_date": today.isoformat(), "text": "заболел"})

        closed = self.store.close_open_event(
            self.user["id"], (today - timedelta(days=1)).isoformat()
        )
        assert closed is not None
        self.assertEqual(closed["start_date"], today.isoformat())
        self.assertEqual(closed["end_date"], today.isoformat())

    def test_events_are_isolated_per_user(self) -> None:
        other = self.store.ensure_debug_user("someone-else")
        mine = self._save("2026-08-10", None, "болею")

        self.assertEqual(self.store.list_events(other["id"]), [])
        self.assertIsNone(self.store.delete_event(other["id"], mine["id"]))
        # Чужое открытое событие не мешает завести своё.
        self.assertIsNotNone(
            self.store.save_event(other["id"], {"start_date": "2026-08-10", "text": "и я болею"})
        )


class EventsApiTests(unittest.TestCase):
    """REST события: CRUD, ошибки, сессия."""

    def test_events_endpoint_creates_lists_updates_and_deletes(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            created = client.request_json(
                "POST", "/api/events", {"start_date": "2026-08-01", "text": "болею"}
            )
            self.assertEqual(created.status, 201)
            event_id = created.payload["event"]["id"]
            self.assertIsNone(created.payload["event"]["end_date"])

            listed = client.request_json("GET", "/api/events")
            self.assertEqual(listed.status, 200)
            self.assertEqual([e["id"] for e in listed.payload["events"]], [event_id])

            updated = client.request_json(
                "PUT",
                f"/api/events/{event_id}",
                {"start_date": "2026-08-01", "end_date": "2026-08-04", "text": "болел"},
            )
            self.assertEqual(updated.status, 200)
            self.assertEqual(updated.payload["event"]["end_date"], "2026-08-04")

            deleted = client.request_json("DELETE", f"/api/events/{event_id}")
            self.assertEqual(deleted.status, 200)
            self.assertTrue(deleted.payload["deleted"])
            self.assertEqual(client.request_json("GET", "/api/events").payload["events"], [])

    def test_events_endpoint_reports_bad_payloads_and_unknown_ids(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})

            tomorrow = (date.today() + timedelta(days=1)).isoformat()
            future = client.request_json(
                "POST", "/api/events", {"start_date": tomorrow, "text": "x"}
            )
            self.assertEqual(future.status, 400)

            no_text = client.request_json("POST", "/api/events", {"start_date": "2026-08-01"})
            self.assertEqual(no_text.status, 400)

            client.request_json(
                "POST", "/api/events", {"start_date": "2026-08-01", "text": "болею"}
            )
            second_open = client.request_json(
                "POST", "/api/events", {"start_date": "2026-08-02", "text": "и ещё"}
            )
            self.assertEqual(second_open.status, 400)

            missing = client.request_json(
                "PUT", "/api/events/999", {"start_date": "2026-08-01", "text": "нет такого"}
            )
            self.assertEqual(missing.status, 404)
            self.assertEqual(client.request_json("DELETE", "/api/events/999").status, 404)

    def test_events_endpoint_requires_a_session(self) -> None:
        with running_miniapp_server(allow_debug_user=False) as app:
            client = JsonHttpClient(app.base_url)
            self.assertEqual(client.request_json("GET", "/api/events").status, 401)
            self.assertEqual(
                client.request_json(
                    "POST", "/api/events", {"start_date": "2026-08-01", "text": "болею"}
                ).status,
                401,
            )


class WorkoutClosesOpenEventTests(unittest.TestCase):
    """Состояние «сейчас не тренируюсь» переключает только новая сегодняшняя
    тренировка: правка истории — это правка истории, а не выход из перерыва."""

    def _open_event(self, client: JsonHttpClient) -> dict[str, Any]:
        """Открыть событие 10 дней назад через API."""
        started = (date.today() - timedelta(days=10)).isoformat()
        response = client.request_json(
            "POST", "/api/events", {"start_date": started, "text": "болею"}
        )
        self.assertEqual(response.status, 201)
        return response.payload["event"]

    def _current(self, client: JsonHttpClient, event_id: int) -> dict[str, Any]:
        """Текущее состояние события по id через API."""
        events = client.request_json("GET", "/api/events").payload["events"]
        return next(event for event in events if event["id"] == event_id)

    def test_todays_first_workout_closes_the_open_event_with_yesterday(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})
            event = self._open_event(client)

            today = date.today()
            created = client.request_json(
                "POST",
                "/api/workouts",
                sample_workout_payload(client_id="today-1", workout_date=today.isoformat()),
            )
            self.assertEqual(created.status, 201)

            # Перерыв кончился в тот день, когда атлет снова пришёл в зал.
            self.assertEqual(
                self._current(client, event["id"])["end_date"],
                (today - timedelta(days=1)).isoformat(),
            )

    def test_backdated_workout_leaves_the_event_open(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})
            event = self._open_event(client)

            yesterday = (date.today() - timedelta(days=1)).isoformat()
            created = client.request_json(
                "POST",
                "/api/workouts",
                sample_workout_payload(client_id="backdated-1", workout_date=yesterday),
            )
            self.assertEqual(created.status, 201)
            self.assertIsNone(self._current(client, event["id"])["end_date"])

    def test_editing_a_workout_leaves_the_event_open(self) -> None:
        with running_miniapp_server(allow_debug_user=True) as app:
            client = JsonHttpClient(app.base_url)
            client.request_json("POST", "/api/session/resolve", {})
            event = self._open_event(client)

            yesterday = (date.today() - timedelta(days=1)).isoformat()
            today = date.today().isoformat()
            created = client.request_json(
                "POST",
                "/api/workouts",
                sample_workout_payload(client_id="editable-1", workout_date=yesterday),
            )
            workout_id = created.payload["workout"]["id"]

            # Дата тренировки переехала на сегодня правкой — состояние прежнее.
            updated = client.request_json(
                "PUT",
                f"/api/workouts/{workout_id}",
                sample_workout_payload(client_id="editable-1", workout_date=today),
            )
            self.assertEqual(updated.status, 200)
            self.assertIsNone(self._current(client, event["id"])["end_date"])

            # Повторный POST того же client_id — тоже правка (created=false).
            again = client.request_json(
                "POST",
                "/api/workouts",
                sample_workout_payload(client_id="editable-1", workout_date=today),
            )
            self.assertEqual(again.status, 200)
            self.assertFalse(again.payload["created"])
            self.assertIsNone(self._current(client, event["id"])["end_date"])


class AthleteNotesInPromptTests(unittest.TestCase):
    """Заметка — дословные слова атлета, и модель обязана видеть, к чему они
    относятся: к подходу (вес под ней не сравним с соседними) или ко всей сессии."""

    def test_set_note_stands_next_to_its_own_set(self) -> None:
        line = prompt_builder._serialize_workout(
            _workout(
                "2026-06-10",
                sets=[
                    {"reps": 10, "weight": 100, "effort": "hard", "notes": "канат вместо ручки"},
                    {"reps": 8, "weight": 90},
                ],
            )
        )
        self.assertIn("100кг×10+ «канат вместо ручки», 90кг×8", line)

    def test_session_note_closes_the_line(self) -> None:
        line = prompt_builder._serialize_workout(
            _workout("2026-06-10", notes="спал 5 часов, тяжело шло")
        )
        self.assertTrue(line.endswith(" — «спал 5 часов, тяжело шло»"), line)

    def test_note_stays_on_one_line(self) -> None:
        """Одна тренировка — одна строка хроники: заметка в две строки разорвала
        бы формат, по которому модель читает историю."""
        line = prompt_builder._serialize_workout(
            _workout(
                "2026-06-10",
                sets=[{"reps": 10, "weight": 100, "notes": "новая\n  скамья"}],
                notes="спал  мало\nустал",
            )
        )
        self.assertNotIn("\n", line)
        self.assertIn("«новая скамья»", line)
        self.assertIn("«спал мало устал»", line)

    def test_empty_note_leaves_no_empty_quotes(self) -> None:
        line = prompt_builder._serialize_workout(
            _workout("2026-06-10", sets=[{"reps": 10, "weight": 100, "notes": "   "}], notes=None)
        )
        self.assertNotIn("«", line)
        self.assertTrue(line.endswith("100кг×10"), line)

    def test_notes_reach_the_assembled_prompt(self) -> None:
        prompt = prompt_builder._build_user_prompt(
            [
                _workout(
                    "2026-06-10",
                    sets=[{"reps": 10, "weight": 100, "notes": "узкая ручка"}],
                    notes="колено ныло",
                )
            ],
            [],
            TODAY,
            20,
            catalog=CATALOG,
        )
        self.assertIn("«узкая ручка»", prompt)
        self.assertIn("«колено ныло»", prompt)


class EventPromptTests(unittest.TestCase):
    """События в промпте: контекст, хроника, легенда, обрезка, ни одного числа."""

    WORKOUTS = [  # list_workouts() отдаёт новые сверху
        _workout("2026-06-10"),
        _workout("2026-06-01", exercise_id=9, name="Тяга верт."),
    ]
    EVENTS = [
        {"start_date": "2026-05-20", "end_date": "2026-05-20", "text": "отравился"},
        {"start_date": "2026-06-03", "end_date": "2026-06-08", "text": "грипп"},
        {"start_date": "2026-06-11", "end_date": None, "text": "командировка"},
    ]

    def _prompt(self, events: list[dict[str, Any]] | None) -> str:
        """User-промпт на фиксированной истории с заданными событиями."""
        return prompt_builder._build_user_prompt(
            self.WORKOUTS, [], TODAY, 20, catalog=CATALOG, events=events
        )

    def test_open_event_lands_in_the_context_block(self) -> None:
        prompt = self._prompt(self.EVENTS)
        context = prompt.split("\n\n", 1)[0]
        self.assertIn("СОБЫТИЕ ИДЁТ ПРЯМО СЕЙЧАС", context)
        # День начала — первый: 11-е и 12-е это два дня.
        self.assertIn("с 2026-06-11, 2 дн.", context)
        self.assertIn("«командировка»", context)
        # Событие стоит сразу за днями простоя — решение о возврате принимается
        # здесь, а без причины дырка в датах неотличима от лени.
        self.assertLess(context.index("Дней с последней"), context.index("СОБЫТИЕ ИДЁТ"))

    def test_closed_events_do_not_reach_the_context_block(self) -> None:
        closed = [event for event in self.EVENTS if event["end_date"]]
        self.assertNotIn("СОБЫТИЕ ИДЁТ ПРЯМО СЕЙЧАС", self._prompt(closed))
        self.assertNotIn("СОБЫТИЕ ИДЁТ ПРЯМО СЕЙЧАС", self._prompt(None))

    def test_the_newest_open_event_wins(self) -> None:
        """Хранилище держит открытое событие единственным; если в данных их всё
        же несколько, в контекст идёт самое свежее."""
        picked = prompt_builder._open_event(
            [
                {"start_date": "2026-06-01", "end_date": None, "text": "старое"},
                {"start_date": "2026-06-11", "end_date": None, "text": "свежее"},
            ]
        )
        assert picked is not None
        self.assertEqual(picked["text"], "свежее")

    def test_chronicle_stands_between_the_workouts(self) -> None:
        lines = prompt_builder._serialize_history(
            self.WORKOUTS, 10, CATALOG, self.EVENTS
        ).splitlines()
        self.assertEqual(
            [line.split(" [")[0] for line in lines],
            [
                "2026-05-20",
                "2026-06-01",
                "2026-06-03 — 2026-06-08",
                "2026-06-10",
                "2026-06-11 — идёт",
            ],
        )
        self.assertEqual(lines[0], "2026-05-20 [событие] «отравился»")
        self.assertIn("[событие] «грипп»", lines[2])

    def test_event_on_a_workout_day_comes_first(self) -> None:
        """Сначала обстоятельство, потом сессия — иначе строка события читается
        как комментарий к уже прошедшей тренировке."""
        lines = prompt_builder._serialize_history(
            [_workout("2026-06-10")],
            10,
            CATALOG,
            [{"start_date": "2026-06-10", "end_date": "2026-06-10", "text": "температура"}],
        ).splitlines()
        self.assertIn("[событие]", lines[0])
        self.assertIn("[medium]", lines[1])

    def test_legend_appears_only_with_events(self) -> None:
        legend = coach_prompts.fragments("user_blocks")["raw_history_events"]
        self.assertIn(legend, self._prompt(self.EVENTS))
        self.assertNotIn(legend, self._prompt(None))

    def test_clipped_chronicle_says_so(self) -> None:
        """Урезанная хроника, прочитанная как полная, врёт про причины пауз."""
        extra = 5
        events = [
            {
                "start_date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
                "end_date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
                "text": f"событие {i}",
            }
            for i in range(prompt_builder.MAX_EVENT_LINES + extra)
        ]
        prompt = self._prompt(events)

        self.assertIn("ХРОНИКА СОБЫТИЙ НЕПОЛНАЯ", prompt)
        self.assertIn(
            f"показаны последние {prompt_builder.MAX_EVENT_LINES} из {len(events)}", prompt
        )
        # Режутся самые старые: свежее событие объясняет разрыв, до которого
        # модель ещё дойдёт.
        self.assertNotIn("«событие 0»", prompt)
        self.assertIn(f"«событие {len(events) - 1}»", prompt)
        chronicle = [
            line for line in prompt.splitlines() if "[событие]" in line and line[:4].isdigit()
        ]
        self.assertEqual(len(chronicle), prompt_builder.MAX_EVENT_LINES)

    def test_chronicle_at_the_ceiling_is_not_flagged_as_clipped(self) -> None:
        events = [
            {
                "start_date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
                "end_date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
                "text": f"событие {i}",
            }
            for i in range(prompt_builder.MAX_EVENT_LINES)
        ]
        self.assertNotIn("ХРОНИКА СОБЫТИЙ НЕПОЛНАЯ", self._prompt(events))

    def test_events_only_add_lines_and_change_no_number(self) -> None:
        """Главный инвариант фичи: из событий не считается ни одного числа.
        Выкинь из промпта строки событий — и получится ровно тот промпт, что
        собрался бы без них: ни объёмы, ни дни простоя, ни фазу они не двигают.
        """
        blocks = coach_prompts.fragments("user_blocks")
        legend = blocks["raw_history_events"]
        context_prefix = blocks["context_open_event"].split("{{")[0]

        with_events = self._prompt(self.EVENTS)
        kept = [
            line
            for line in with_events.splitlines()
            if "[событие]" not in line and line != legend and not line.startswith(context_prefix)
        ]
        self.assertEqual("\n".join(kept), self._prompt(None))


class WeeklyReportEventTests(unittest.TestCase):
    """События в промпте недельного отчёта."""

    DAYS = 7  # период отчёта: 2026-06-06 … 2026-06-12

    def _report(self, events: list[dict[str, Any]] | None) -> str:
        """Промпт отчёта на одной тренировке с заданными событиями."""
        return prompt_builder._build_report_prompt(
            [_workout("2026-06-10")],
            [],
            [],
            CATALOG,
            coach_state.default_state(),
            TODAY,
            self.DAYS,
            events=events,
        )

    def test_events_crossing_the_period_reach_the_report(self) -> None:
        report = self._report(
            [
                {"start_date": "2026-05-01", "end_date": "2026-05-03", "text": "старое"},
                {"start_date": "2026-06-04", "end_date": "2026-06-06", "text": "хвост гриппа"},
                {"start_date": "2026-06-08", "end_date": "2026-06-08", "text": "отравился"},
                {"start_date": "2026-06-11", "end_date": None, "text": "командировка"},
            ]
        )
        self.assertIn("События, пересекающиеся с периодом", report)
        # Событие, начавшееся до периода и кончившееся в нём, — тоже контекст
        # его пустых дней; идущее считается идущим до конца периода.
        self.assertIn("2026-06-04 — 2026-06-06 [событие] «хвост гриппа»", report)
        self.assertIn("2026-06-08 [событие] «отравился»", report)
        self.assertIn("2026-06-11 — идёт [событие] «командировка»", report)
        self.assertNotIn("«старое»", report)

    def test_event_ending_the_day_before_the_period_is_left_out(self) -> None:
        report = self._report(
            [{"start_date": "2026-06-01", "end_date": "2026-06-05", "text": "давняя простуда"}]
        )
        self.assertNotIn("«давняя простуда»", report)
        self.assertIn("Событий за период нет", report)

    def test_report_says_out_loud_when_there_were_no_events(self) -> None:
        """«Событий нет» значит, что пропуски ничем не объяснены, и это другой
        разговор с атлетом — блок обязан быть даже пустым."""
        for events in (None, []):
            with self.subTest(events=events):
                self.assertIn("Событий за период нет", self._report(events))


class EventsReachGenerationTests(unittest.TestCase):
    """Канал «события → промпт» включается на стороне ВЫЗЫВАЮЩЕГО: не переданный
    `events=` молча выключает и хронику, и открытое событие, а промпт при этом
    собирается без единой ошибки. Поэтому каждый живой вызыватель модели
    проверяется здесь поимённо — иначе фича тихо не доедет до атлета."""

    RECOMMENDATION = {
        "focus": "Ноги",
        "load_type": "medium",
        "rationale": "r",
        "exercises": [
            {
                "exercise_id": 1,
                "name": "Bench Press",
                "note": "n",
                "sets": [{"reps": 10, "weight": 50}],
            }
        ],
    }

    def _store_with_event(self) -> tuple[backend_store.MiniAppStore, int]:
        """Стор с одной тренировкой и открытым событием."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = backend_store.MiniAppStore(Path(tmp.name) / "trainer.db")
        uid = int(store.ensure_debug_user("events-generation")["id"])
        store.save_workout(uid, sample_workout_payload(client_id="w1"))
        store.save_event(uid, {"start_date": "2026-06-01", "text": "болел"})
        return store, uid

    def _capture_generate(self, seen: dict[str, Any]):
        """Подмена ``generate``, запоминающая kwargs."""

        def capture(workouts, body_weights, catalog, **kwargs):
            seen.update(kwargs)
            return self.RECOMMENDATION, {"input_tokens": 1, "output_tokens": 2}, "claude-test"

        return capture

    def _texts(self, seen: dict[str, Any]) -> list[str]:
        """Тексты событий, дошедших до генерации."""
        return [str(event["text"]) for event in seen.get("events") or []]

    def test_backend_generation_passes_events(self) -> None:
        """Главный путь: генерация, которую видит iOS."""
        seen: dict[str, Any] = {}
        with running_miniapp_server(allow_debug_user=True) as app:
            module = app.module
            client = JsonHttpClient(app.base_url)
            uid = int(client.request_json("POST", "/api/session/resolve", {}).payload["user"]["id"])
            client.request_json("POST", "/api/workouts", sample_workout_payload(client_id="w1"))
            client.request_json(
                "POST", "/api/events", {"start_date": "2026-06-01", "text": "болел"}
            )

            original = module.recommender.generate
            module.recommender.generate = self._capture_generate(seen)
            try:
                module._generate_and_store_recommendation(uid)
            finally:
                module.recommender.generate = original

        self.assertEqual(self._texts(seen), ["болел"])

    def test_scheduled_refresh_passes_events(self) -> None:
        seen: dict[str, Any] = {}
        store, uid = self._store_with_event()
        original = recommender.generate
        recommender.generate = self._capture_generate(seen)
        self.addCleanup(lambda: setattr(recommender, "generate", original))

        self.assertTrue(refresh_recommendation.run(store, uid))
        self.assertEqual(self._texts(seen), ["болел"])

    def test_weekly_report_passes_events(self) -> None:
        seen: dict[str, Any] = {}
        store, uid = self._store_with_event()

        def capture(workouts, body_weights, waists, catalog, **kwargs):
            seen.update(kwargs)
            return "отчёт", {"input_tokens": 1, "output_tokens": 2}, "claude-test"

        original = recommender.generate_weekly_report
        recommender.generate_weekly_report = capture
        self.addCleanup(lambda: setattr(recommender, "generate_weekly_report", original))

        self.assertTrue(weekly_report.run(store, uid))
        self.assertEqual(self._texts(seen), ["болел"])


if __name__ == "__main__":
    unittest.main()
