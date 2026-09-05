"""Память карточки о себе: блок «Прошлая карточка тренера» в промпте плана и канал
``previous=`` у каждого живого вызывателя генерации — как у событий, не переданный
аргумент молча выключает блок, поэтому вызыватели проверяются поимённо.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

from support import (
    CATALOG_PATH,
    JsonHttpClient,
    running_miniapp_server,
    sample_workout_payload,
    temporary_env,
)

from infra.jobs import refresh_recommendation
from trainer.data import anthropic_client, backend_store, files
from trainer.domain import prompt_builder, recommender

CARD: dict[str, Any] = {
    "focus": "Грудь и руки",
    "load_type": "medium",
    "rest_days": 2,
    "rationale": (
        "**Почему так:** спина за круг набрала своё.\n"
        "**Когда:** послезавтра.\n"
        "**Совет:** следующая сессия — ноги и спина, жим ногами первым."
    ),
    "exercises": [
        {
            "exercise_id": 17,
            "name": "Бабочка",
            "note": "n",
            "sets": [{"reps": 12, "weight": 25}] * 3,
        },
        {
            "exercise_id": 12,
            "name": "Трицепс",
            "note": "n",
            "sets": [{"reps": 11, "weight": 15}] * 2,
        },
    ],
    "next_workout_date": "2026-09-07",
}


def _row(based_on: int) -> dict[str, Any]:
    """Строка кэша совета в форме ``store.get_recommendation``; собрана 2026-09-05 UTC."""
    return {
        "id": 1,
        "status": "pending",
        "based_on_workout_id": based_on,
        "recommendation": CARD,
        "created_at": 1_788_600_000,
        "updated_at": 1_788_600_000,
    }


def _workout(workout_id: int, when: str) -> dict[str, Any]:
    """Тренировка с id, как отдаёт стор (от новых к старым передаёт вызывающий)."""
    return {
        "id": workout_id,
        "workout_date": when,
        "data": {
            "load_type": "medium",
            "exercises": [{"exercise_id": 18, "name": "Жим", "sets": [{"reps": 10, "weight": 60}]}],
        },
    }


class PreviousCardBlockTests(unittest.TestCase):
    """Блок в user-промпте: что в нём есть и когда его нет."""

    def _prompt(self, previous: dict[str, Any] | None, workouts: list[dict[str, Any]]) -> str:
        return prompt_builder._build_user_prompt(
            workouts, [], date(2026, 9, 6), 10, previous=previous
        )

    def test_advice_is_the_sovet_item_of_the_rationale(self) -> None:
        self.assertEqual(
            prompt_builder.previous_advice(CARD["rationale"]),
            "следующая сессия — ноги и спина, жим ногами первым.",
        )
        self.assertIsNone(prompt_builder.previous_advice("**Когда:** завтра."))
        self.assertIsNone(prompt_builder.previous_advice(None))

    def test_block_names_focus_composition_and_promise(self) -> None:
        prompt = self._prompt(_row(based_on=154), [_workout(154, "2026-09-05")])
        self.assertIn(
            "Прошлая карточка тренера (собрана 2026-09-05, назначена на 2026-09-07; "
            "тренировок по ней ещё не было — ты пересобираешь ту же сессию)",
            prompt,
        )
        self.assertIn(
            "фокус «Грудь и руки», нагрузка medium, состав: Бабочка ×3, Трицепс ×2.", prompt
        )
        self.assertIn(
            "Обещание на следующую сессию из её rationale: «следующая сессия — ноги и спина, "
            "жим ногами первым.»",
            prompt,
        )
        # Блок стоит перед сырой историей: обещание читается до самих тренировок.
        self.assertLess(prompt.index("Прошлая карточка"), prompt.index("тренировок сырыми"))

    def test_a_newer_workout_marks_the_card_as_done(self) -> None:
        prompt = self._prompt(
            _row(based_on=154), [_workout(155, "2026-09-05"), _workout(154, "2026-09-03")]
        )
        self.assertIn("после неё записано тренировок: 1 — она уже отработана", prompt)

    def test_without_previous_or_payload_the_block_is_absent(self) -> None:
        for previous in (None, {"status": "none", "recommendation": None}):
            self.assertNotIn(
                "Прошлая карточка", self._prompt(previous, [_workout(1, "2026-09-05")])
            )


class PreviousCardReachesGenerationTests(unittest.TestCase):
    """Каждый живой вызыватель передаёт строку кэша; сама генерация кладёт её в промпт."""

    def _capture(self, seen: dict[str, Any]):
        """Подмена ``generate``, запоминающая kwargs."""

        def capture(workouts, body_weights, catalog, **kwargs):
            seen.update(kwargs)
            return CARD, {"input_tokens": 1, "output_tokens": 2}, "claude-test"

        return capture

    def test_backend_generation_passes_the_cached_card(self) -> None:
        seen: dict[str, Any] = {}
        with running_miniapp_server(allow_debug_user=True) as app:
            module = app.module
            client = JsonHttpClient(app.base_url)
            uid = int(client.request_json("POST", "/api/session/resolve", {}).payload["user"]["id"])
            client.request_json("POST", "/api/workouts", sample_workout_payload(client_id="w1"))
            module.STORE.save_recommendation(
                uid, module.STORE.get_latest_workout_id(uid), 1, "claude-test", CARD, 1, 2
            )
            original = module.recommender.generate
            module.recommender.generate = self._capture(seen)
            try:
                module._generate_and_store_recommendation(uid)
            finally:
                module.recommender.generate = original
        self.assertEqual(seen["previous"]["recommendation"]["focus"], "Грудь и руки")

    def test_scheduled_refresh_passes_the_cached_card(self) -> None:
        seen: dict[str, Any] = {}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = backend_store.MiniAppStore(Path(tmp.name) / "trainer.db")
        uid = int(store.ensure_debug_user("previous-card")["id"])
        store.save_workout(uid, sample_workout_payload(client_id="w1"))
        store.save_recommendation(
            uid, store.get_latest_workout_id(uid), 1, "claude-test", CARD, 1, 2
        )
        original = recommender.generate
        recommender.generate = self._capture(seen)
        self.addCleanup(lambda: setattr(recommender, "generate", original))

        self.assertTrue(refresh_recommendation.run(store, uid, force=True))
        self.assertEqual(seen["previous"]["recommendation"]["focus"], "Грудь и руки")

    def test_generate_with_trace_feeds_the_card_into_the_user_prompt(self) -> None:
        captured: dict[str, Any] = {}

        def fake_call(system, messages, schema, **kwargs):
            first = messages if isinstance(messages, str) else messages[0]["content"]
            captured.setdefault("user", first)
            return CARD, {"input_tokens": 1, "output_tokens": 1}

        original = anthropic_client._call_anthropic
        anthropic_client._call_anthropic = fake_call
        self.addCleanup(lambda: setattr(anthropic_client, "_call_anthropic", original))
        with temporary_env({"ANTHROPIC_API_KEY": "test"}):
            recommender.generate_with_trace(
                [_workout(154, "2026-09-05")],
                [],
                files.load_catalog(CATALOG_PATH),
                previous=_row(154),
                today=date(2026, 9, 6),
            )
        self.assertIn("Прошлая карточка тренера", captured["user"])
        self.assertIn("жим ногами первым", captured["user"])


if __name__ == "__main__":
    unittest.main()
