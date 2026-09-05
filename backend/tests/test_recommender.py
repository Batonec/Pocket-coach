"""Точка входа генерации: каталог и JSON-схема, санитизация ответа, системный и
user-промпт, недельный отчёт, жизненный цикл совета, репромпт по нарушениям
валидатора и детерминированное разрешение.
"""

from __future__ import annotations

import unittest

import support  # noqa: F401 — кладёт backend в sys.path
from support import CATALOG_PATH

from trainer.data import anthropic_client, files
from trainer.domain import limits, prompt_builder, recommender, rules

CATALOG = [
    {"id": 8, "name": "Жим ногами"},
    {"id": 9, "name": "Тяга верт."},
    {"id": 1, "name": "Жим гор."},
]


class RecommenderTests(unittest.TestCase):
    """Каталог, схема, санитизация, сериализация истории."""

    def test_load_catalog_reads_the_resources_file(self) -> None:
        catalog = files.load_catalog(CATALOG_PATH)
        self.assertTrue(catalog)
        self.assertTrue(all("id" in item and "name" in item for item in catalog))

    def test_build_schema_enum_lists_catalog_ids_and_requires_note(self) -> None:
        schema = prompt_builder._build_schema(CATALOG)
        item = schema["properties"]["exercises"]["items"]
        # id 1 — дубль 18 в каталоге, модели не предлагается никогда.
        self.assertEqual(item["properties"]["exercise_id"]["enum"], [8, 9])
        self.assertIn("note", item["required"])
        # Модель никогда не пишет имена — их подставляет сервер из каталога.
        self.assertNotIn("name", item["properties"])
        self.assertNotIn("name", item["required"])

    def test_validate_drops_unknown_id_clamps_and_uses_catalog_name(self) -> None:
        raw = {
            "focus": "Ноги",
            "load_type": "medium",
            "rationale": "...",
            "exercises": [
                {
                    "exercise_id": 8,
                    "name": "что-то своё",
                    "note": "+вес",
                    "sets": [
                        {"reps": 11, "weight": 120},
                        {"reps": 0, "weight": 50},  # повторы < 1 → отброшен
                        {"reps": 10, "weight": 99999},  # вес зажат
                    ],
                },
                {  # выдуманный id → отброшен
                    "exercise_id": 999,
                    "name": "выдумка",
                    "note": "n",
                    "sets": [{"reps": 5, "weight": 5}],
                },
            ],
        }
        out = rules.normalize_model_plan(raw, CATALOG)
        self.assertEqual(len(out["exercises"]), 1)
        exercise = out["exercises"][0]
        self.assertEqual(exercise["exercise_id"], 8)
        self.assertEqual(exercise["name"], "Жим ногами")  # имя из каталога, а не эхо модели
        self.assertEqual(exercise["note"], "+вес")
        self.assertEqual([s["reps"] for s in exercise["sets"]], [11, 10])
        self.assertEqual(exercise["sets"][1]["weight"], limits.MAX_WEIGHT)

    def test_validate_normalizes_unknown_load_type(self) -> None:
        raw = {
            "focus": "x",
            "load_type": "crazy",
            "rationale": "r",
            "exercises": [
                {"exercise_id": 8, "name": "x", "note": "n", "sets": [{"reps": 10, "weight": 50}]}
            ],
        }
        self.assertEqual(rules.normalize_model_plan(raw, CATALOG)["load_type"], "medium")

    def test_validate_raises_without_valid_exercises(self) -> None:
        raw = {
            "focus": "x",
            "load_type": "light",
            "rationale": "r",
            "exercises": [
                {"exercise_id": 999, "name": "x", "note": "n", "sets": [{"reps": 10, "weight": 50}]}
            ],
        }
        # Форма ответа — забота rules, и говорит она на языке rules: ValueError.
        # В RecommendationError его переводит уже generate_with_trace (см. ниже).
        with self.assertRaisesRegex(ValueError, "ни одного валидного"):
            rules.normalize_model_plan(raw, CATALOG)

    def test_serialize_history_is_oldest_first_with_effort_marks(self) -> None:
        # list_workouts() отдаёт новые сверху; сериализатор переворачивает к старым сверху.
        workouts = [
            {
                "workout_date": "2026-05-29",
                "data": {
                    "load_type": "heavy",
                    "exercises": [
                        {
                            "name": "Жим ногами",
                            "sets": [{"reps": 10, "weight": 120, "effort": "hard"}],
                        }
                    ],
                },
            },
            {
                "workout_date": "2026-05-26",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {"name": "Жим гор.", "sets": [{"reps": 8, "weight": 50, "effort": "easy"}]}
                    ],
                },
            },
        ]
        text = prompt_builder._serialize_history(workouts, 20)
        self.assertTrue(text.splitlines()[0].startswith("2026-05-26"))
        self.assertIn("120кг×10+", text)  # hard → «+»
        self.assertIn("50кг×8-", text)  # easy → «-»

    def test_generate_requires_history(self) -> None:
        with self.assertRaises(recommender.RecommendationError):
            recommender.generate([], [], CATALOG)


class ProfileTests(unittest.TestCase):
    """Профиль атлета в файле и в системном промпте; контекст user-промпта."""

    def test_load_profile_reads_valid_file(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_profile.json"
            path.write_text('{"schema":1,"blocks":{"Цель":"lean bulk до 84"}}', "utf-8")
            profile = files.load_profile(path)
            assert profile is not None
            self.assertIn("Цель", profile["blocks"])

    def test_load_profile_tolerates_missing_or_garbage(self) -> None:
        import tempfile
        from pathlib import Path

        self.assertIsNone(files.load_profile(None))
        self.assertIsNone(files.load_profile("/nonexistent/coach_profile.json"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", "utf-8")
            self.assertIsNone(files.load_profile(path))
            path.write_text('{"blocks":{}}', "utf-8")
            self.assertIsNone(files.load_profile(path))

    def test_update_profile_block_replaces_deletes_and_backs_up(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_profile.json"
            path.write_text(
                '{"schema":1,"updated":"2026-01-01","blocks":{"Цель":"старый текст","Атлет":"а"}}',
                "utf-8",
            )
            updated = files.update_profile_block(path, "Цель", "новый текст")
            self.assertEqual(updated["blocks"]["Цель"], "новый текст")
            reloaded = files.load_profile(path)
            assert reloaded is not None
            self.assertEqual(reloaded["blocks"]["Цель"], "новый текст")
            self.assertEqual(reloaded["blocks"]["Атлет"], "а")  # не тронут
            backups = list(Path(tmp).glob("coach_profile.json.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("старый текст", backups[0].read_text("utf-8"))

            # Пустой текст удаляет блок.
            files.update_profile_block(path, "Атлет", "")
            after_delete = files.load_profile(path)
            assert after_delete is not None
            self.assertNotIn("Атлет", after_delete["blocks"])

    def test_update_profile_block_rejects_missing_file_and_unknown_delete(self) -> None:
        import tempfile
        from pathlib import Path

        with self.assertRaises(recommender.RecommendationError):
            files.update_profile_block(None, "Цель", "x")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coach_profile.json"
            with self.assertRaises(recommender.RecommendationError):
                files.update_profile_block(path, "Цель", "x")
            path.write_text('{"schema":1,"blocks":{"Цель":"т"}}', "utf-8")
            with self.assertRaises(recommender.RecommendationError):
                files.update_profile_block(path, "Нет такого", "")

    def test_system_prompt_embeds_profile_semantics_and_policy(self) -> None:
        profile = {"schema": 1, "blocks": {"Цель": "lean bulk, потолок 84 кг"}}
        prompt = prompt_builder._build_system_prompt(CATALOG, profile)
        self.assertIn("lean bulk, потолок 84 кг", prompt)
        self.assertIn("широчайшие", prompt)  # семантика каталога
        self.assertIn("ТРЕНЕРСКАЯ ПОЛИТИКА", prompt)
        self.assertIn("rationale", prompt)
        # Политика — явно дефолты; жёсткие границы названы отдельно.
        self.assertIn("ориентиры по умолчанию", prompt)
        self.assertIn("ЖЁСТКИЕ ГРАНИЦЫ", prompt)
        # Планирование больше не строится вокруг цикла инъекций.
        self.assertNotIn("гормональный цикл", prompt.lower())
        # Медицинская граница обязана пережить удаление цикла.
        self.assertIn("зона лечащего врача", prompt)

    def test_system_prompt_without_profile_uses_fallback(self) -> None:
        prompt = prompt_builder._build_system_prompt(CATALOG)
        self.assertIn("Профиль атлета не настроен", prompt)

    def test_context_line_uses_athlete_overrides(self) -> None:
        """Ориентиры фазы в КОНТЕКСТЕ рендерятся из phase_params, а не из дефолтов,
        и без машинного имени фазы: модель читает название этапа стратегии, а
        cut_recomp / lean_bulk ей не нужны и в rationale им не место.
        """
        from datetime import date

        from trainer.domain import coach_state

        state = coach_state.default_state()
        state["phase"] = "cut_recomp"
        state["phase_params"] = {"cut_recomp": {"title": "Ф0 · возврат", "calories": [2450, 2550]}}
        workouts = [
            {
                "workout_date": "2026-06-10",
                "data": {
                    "load_type": "heavy",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 10, "weight": 100}],
                        }
                    ],
                },
            }
        ]
        prompt = prompt_builder._build_user_prompt(workouts, [], date(2026, 6, 12), 20, state=state)
        self.assertIn(
            "Фаза: «Ф0 · возврат», неделя блока 1. Ориентиры фазы: 2450–2550 ккал", prompt
        )
        self.assertNotIn("2100–2200", prompt)
        self.assertNotIn("cut_recomp", prompt)

    def test_context_line_survives_a_malformed_override(self) -> None:
        """Ключ-диапазон, переопределённый скаляром, не должен ронять сборку промпта:
        генерация никогда не падает из-за методики.
        """
        from datetime import date

        from trainer.domain import coach_state

        state = coach_state.default_state()
        state["phase"] = "maintenance"
        state["phase_params"] = {"maintenance": {"protein_g": 150, "session_sets": "восемь"}}
        workouts = [
            {
                "workout_date": "2026-06-10",
                "data": {
                    "load_type": "heavy",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 10, "weight": 100}],
                        }
                    ],
                },
            }
        ]
        prompt = prompt_builder._build_user_prompt(workouts, [], date(2026, 6, 12), 20, state=state)
        self.assertIn("белок 150 г, сессия восемь рабочих подходов", prompt)

    def test_user_prompt_contains_context_volumes_and_weekday(self) -> None:
        from datetime import date

        workouts = [
            {
                "workout_date": "2026-06-10",
                "data": {
                    "load_type": "heavy",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 10, "weight": 100}],
                        }
                    ],
                },
            }
        ]
        prompt = prompt_builder._build_user_prompt(workouts, [], date(2026, 6, 12), 20)
        self.assertTrue(prompt.startswith("=== КОНТЕКСТ ==="))
        self.assertIn("пятница", prompt)
        self.assertNotIn("гормонального цикла", prompt)
        self.assertIn("Фаза: «лёгкий дефицит-рекомп», неделя блока 1", prompt)
        self.assertNotIn("cut_recomp", prompt)
        self.assertIn("Объём за последние 7 дней", prompt)
        self.assertIn("квадрицепс/ягодичные: 1 прямых / 1 эффективных", prompt)
        self.assertIn("Дней с последней тренировки: 2", prompt)
        # Явка и активное окно всегда в данных — шапка программы отправляет
        # модель туда за настоящей частотой.
        self.assertIn("Тренировки по календарным неделям (пн–вс", prompt)
        self.assertIn("2026-06-08…2026-06-14 (текущая, по 2026-06-12): 1", prompt)
        self.assertIn("Активное окно", prompt)
        self.assertIn("ориентиры малых групп (прямых): дельты 6–12", prompt)

    def test_user_prompt_flags_return_after_break(self) -> None:
        from datetime import date

        workouts = [
            {
                "workout_date": "2026-05-01",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 10, "weight": 100}],
                        }
                    ],
                },
            }
        ]
        prompt = prompt_builder._build_user_prompt(workouts, [], date(2026, 6, 12), 20)
        self.assertIn("ВОЗВРАТ ПОСЛЕ ПЕРЕРЫВА", prompt)
        self.assertIn("неделя блока 1", prompt)
        # Методика возврата делегирована: контекст называет единственную жёсткую
        # границу и не предписывает ни процентов, ни числа сетов.
        self.assertIn("не выше доперерывных", prompt)
        self.assertNotIn("85–90%", prompt)
        self.assertNotIn("10–14 подходов", prompt)


class RestDaysTests(unittest.TestCase):
    """``rest_days`` в схеме и ответе, дата следующей тренировки, контекст коуча."""

    def _raw(self, **extra):
        """Сырой ответ модели с переопределениями."""
        base = {
            "focus": "f",
            "load_type": "medium",
            "rationale": "r",
            "exercises": [
                {
                    "exercise_id": 8,
                    "name": "Жим ногами",
                    "note": "n",
                    "sets": [{"reps": 10, "weight": 100}],
                }
            ],
        }
        base.update(extra)
        return base

    def test_validate_defaults_rest_days_when_missing(self) -> None:
        self.assertEqual(rules.normalize_model_plan(self._raw(), CATALOG)["rest_days"], 1)

    def test_validate_clamps_and_coerces_rest_days(self) -> None:
        self.assertEqual(
            rules.normalize_model_plan(self._raw(rest_days=99), CATALOG)["rest_days"],
            limits.MAX_REST_DAYS,
        )
        self.assertEqual(
            rules.normalize_model_plan(self._raw(rest_days=-3), CATALOG)["rest_days"], 0
        )
        self.assertEqual(
            rules.normalize_model_plan(self._raw(rest_days="2"), CATALOG)["rest_days"], 2
        )

    def test_schema_requires_rest_days(self) -> None:
        schema = prompt_builder._build_schema(CATALOG)
        self.assertIn("rest_days", schema["properties"])
        self.assertIn("rest_days", schema["required"])

    def test_generate_resolves_next_workout_date(self) -> None:
        import os
        from datetime import date

        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        self.addCleanup(lambda: os.environ.pop("ANTHROPIC_API_KEY", None))
        orig = anthropic_client._call_anthropic
        self.addCleanup(lambda: setattr(recommender, "_call_anthropic", orig))
        # Почти fullbody-план на свежей истории → без нарушений жёстких границ.
        raw = self._raw(
            rest_days=2,
            exercises=[
                {
                    "exercise_id": 8,
                    "name": "Жим ногами",
                    "note": "n",
                    "sets": [{"reps": 10, "weight": 100}] * 7,
                },
                {
                    "exercise_id": 9,
                    "name": "Тяга верт.",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 60}] * 7,
                },
            ],
        )
        anthropic_client._call_anthropic = lambda *a, **k: (
            raw,
            {"input_tokens": 1, "output_tokens": 1},
        )

        # Недавняя история задевает все группы покрытия, поэтому план выше чистый.
        history = [
            {
                "workout_date": "2026-06-05",
                "data": {
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 10, "weight": 100}] * 2,
                        },
                        {
                            "exercise_id": 9,
                            "name": "Тяга верт.",
                            "sets": [{"reps": 12, "weight": 60}] * 2,
                        },
                        {
                            "exercise_id": 18,
                            "name": "Жим в тренажере",
                            "sets": [{"reps": 12, "weight": 50}] * 2,
                        },
                        {
                            "exercise_id": 15,
                            "name": "Сгибания ног",
                            "sets": [{"reps": 12, "weight": 30}] * 2,
                        },
                    ]
                },
            }
        ]
        rec, _usage, _model = recommender.generate(
            history,
            [],
            CATALOG,
            today=date(2026, 6, 12),
        )
        self.assertEqual(rec["rest_days"], 2)
        self.assertEqual(rec["next_workout_date"], "2026-06-14")
        # Контекст фазы и цикла едет в payload для iOS-клиента.
        context = rec["coach_context"]
        self.assertEqual(context["phase"], "cut_recomp")
        self.assertEqual(context["block_week"], 2)  # якорь — тренировка 06-05
        self.assertFalse(context["deload_week"])
        self.assertEqual(context["weekly_target"], [7, 10])
        self.assertEqual(context["group_targets"]["грудь"], [7, 10])
        self.assertEqual(context["group_targets"]["бицепс"], [4, 8])


class WeeklyReportTests(unittest.TestCase):
    """Промпт и текст недельного отчёта."""

    def test_report_prompt_assembles_and_returns_text(self) -> None:
        import os
        from datetime import date as _date

        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        self.addCleanup(lambda: os.environ.pop("ANTHROPIC_API_KEY", None))
        seen: dict = {}

        def fake_request(system, user, **kwargs):
            seen["system"], seen["user"] = system, user
            return "**Итоги недели** — всё по плану.", {"input_tokens": 3, "output_tokens": 4}

        orig = anthropic_client._request_model
        self.addCleanup(lambda: setattr(recommender, "_request_model", orig))
        anthropic_client._request_model = fake_request

        workouts = [
            {
                "workout_date": "2026-06-10",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 10, "weight": 100}] * 3,
                        },
                    ],
                },
            }
        ]
        report, usage, _model = recommender.generate_weekly_report(
            workouts, [], [], CATALOG, today=_date(2026, 6, 12)
        )
        self.assertIn("Итоги недели", report)
        self.assertEqual(usage["output_tokens"], 4)
        # системный промпт отчёта теперь собирается, а не берётся константой:
        # без профиля и программы он всё равно обязан быть валидным
        self.assertNotIn("{{", seen["system"])
        self.assertIn("Гейт этапа", seen["system"])
        self.assertIn("Период отчёта", seen["user"])
        self.assertIn("Тренировки за период (1)", seen["user"])
        # Легенда сырых строк — та же, что у плана: без неё «+» и «@1» модель
        # читала бы наугад, а статус заметок нигде не был бы объявлен.
        self.assertIn("Значок после подхода", seen["user"])
        self.assertIn("Заметки — факты о контексте", seen["user"])
        self.assertNotIn("cut_recomp", seen["system"])
        self.assertIn("Фаза: «лёгкий дефицит-рекомп», неделя блока 1", seen["user"])
        self.assertIn("Объём за 7 дней", seen["user"])
        self.assertIn("Новых ПР за период нет.", seen["user"])

    def test_report_requires_api_key(self) -> None:
        import os

        os.environ.pop("ANTHROPIC_API_KEY", None)
        with self.assertRaises(recommender.RecommendationError):
            recommender.generate_weekly_report([], [], [], CATALOG)


class AdviceLifecycleTests(unittest.TestCase):
    """Когда совет устарел, что его обесценивает и когда его обновлять по
    таймеру — правила в recommender, а не в хендлерах и скрипте."""

    def test_stale_only_for_a_ready_advice_built_on_an_older_workout(self) -> None:
        ready = {"status": "ready", "based_on_workout_id": 7}
        self.assertFalse(recommender.is_stale(ready, 7))
        self.assertTrue(recommender.is_stale(ready, 9))
        self.assertFalse(recommender.is_stale({"status": "pending", "based_on_workout_id": 7}, 9))
        self.assertFalse(recommender.is_stale(None, 9))

    def test_every_data_change_invalidates_except_an_idempotent_retry(self) -> None:
        for change in ("workout", "body_weight", "waist", "event"):
            self.assertTrue(recommender.advice_invalidated_by(change), change)
        self.assertFalse(recommender.advice_invalidated_by("workout", created=False))
        self.assertFalse(recommender.advice_invalidated_by("report_read"))

    def test_weekly_report_is_about_the_closed_week_and_cached_once(self) -> None:
        from datetime import date

        self.assertEqual(recommender.weekly_report_period(date(2026, 9, 2)), date(2026, 8, 30))
        self.assertEqual(recommender.weekly_report_needed(None, force=False), (True, "в кэше нет"))
        self.assertEqual(recommender.weekly_report_needed({"report": "…"}, force=False)[0], False)
        self.assertTrue(recommender.weekly_report_needed({"report": "…"}, force=True)[0])


class GenerateRepromptTests(unittest.TestCase):
    """Полный прогон ``generate_with_trace``: репромпт, обрезка до потолка, честная
    пометка, кламп возврата.
    """

    CATALOG = [
        {"id": 8, "name": "Жим ногами"},
        {"id": 9, "name": "Тяга верт."},
        {"id": 18, "name": "Жим в тренажере"},
        {"id": 15, "name": "Сгибания ног"},
    ]

    def setUp(self) -> None:
        import os

        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        self.addCleanup(lambda: os.environ.pop("ANTHROPIC_API_KEY", None))
        self._orig = anthropic_client._call_anthropic
        self.addCleanup(lambda: setattr(recommender, "_call_anthropic", self._orig))

    def _history(self, when: str = "2026-06-10"):
        """Fullbody-история, покрывающая все группы правила."""
        return [
            {
                "workout_date": when,
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 10, "weight": 100}] * 3,
                        },
                        {
                            "exercise_id": 9,
                            "name": "Тяга верт.",
                            "sets": [{"reps": 12, "weight": 60}] * 3,
                        },
                        {
                            "exercise_id": 18,
                            "name": "Жим в тренажере",
                            "sets": [{"reps": 12, "weight": 50}] * 2,
                        },
                        {
                            "exercise_id": 15,
                            "name": "Сгибания ног",
                            "sets": [{"reps": 12, "weight": 30}] * 2,
                        },
                    ],
                },
            }
        ]

    def _fullbody_raw(self, leg_press: float = 100.0, with_hamstrings: bool = True):
        """Сырой fullbody-ответ; ``with_hamstrings`` добавляет сгибания ног."""
        exercises = [
            {"exercise_id": 8, "note": "n", "sets": [{"reps": 10, "weight": leg_press}] * 4},
            {"exercise_id": 9, "note": "n", "sets": [{"reps": 12, "weight": 60}] * 4},
            {"exercise_id": 18, "note": "n", "sets": [{"reps": 12, "weight": 50}] * 3},
        ]
        if with_hamstrings:
            exercises.append(
                {"exercise_id": 15, "note": "n", "sets": [{"reps": 12, "weight": 30}] * 2}
            )
        return {
            "focus": "f",
            "load_type": "medium",
            "rest_days": 1,
            "rationale": "r",
            "exercises": exercises,
        }

    def _dry_hamstrings_history(self):
        # Бицепс бедра тренировали 12 дней назад → правило покрытия его требует.
        """История, где бицепс бедра сухой 12 дней — правило покрытия его требует."""
        workouts = self._history()
        workouts[0]["data"]["exercises"] = [
            ex for ex in workouts[0]["data"]["exercises"] if ex["exercise_id"] != 15
        ]
        workouts.append(
            {
                "workout_date": "2026-05-31",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 15,
                            "name": "Сгибания ног",
                            "sets": [{"reps": 12, "weight": 30}] * 2,
                        },
                    ],
                },
            }
        )
        return workouts

    def test_violating_answer_triggers_one_reprompt_then_succeeds(self) -> None:
        from datetime import date as _date

        answers = [
            self._fullbody_raw(with_hamstrings=False),  # сухая группа пропущена
            self._fullbody_raw(with_hamstrings=True),  # исправлено на повторе
        ]
        calls: list[list[dict]] = []

        def fake_call(system, user, schema, **kwargs):
            calls.append(user if isinstance(user, list) else [{"role": "user", "content": user}])
            return answers[len(calls) - 1], {"input_tokens": 10, "output_tokens": 5}

        anthropic_client._call_anthropic = fake_call
        rec, usage, _model, trace = recommender.generate_with_trace(
            self._dry_hamstrings_history(), [], self.CATALOG, today=_date(2026, 6, 12)
        )
        self.assertEqual(len(trace), 2)
        self.assertTrue(any("бицепс бедра" in v for v in trace[0]["violations"]))
        self.assertEqual(trace[1]["violations"], [])
        self.assertEqual(rec["exercises"][-1]["exercise_id"], 15)
        self.assertEqual(usage, {"input_tokens": 20, "output_tokens": 10})
        # Репромпт продолжает тот же разговор и перечисляет нарушения.
        self.assertEqual(len(calls[1]), 3)
        self.assertIn("жёсткие границы", calls[1][2]["content"])

    def test_clean_plan_within_the_cap_is_served_as_is(self) -> None:
        from datetime import date as _date

        calls = 0

        def fake_call(*args, **kwargs):
            nonlocal calls
            calls += 1
            return self._fullbody_raw(), {"input_tokens": 10, "output_tokens": 5}

        anthropic_client._call_anthropic = fake_call
        # 13 сетов при дефолтном потолке cut_recomp в 20: отдаётся как есть —
        # нижняя граница коридора не проверяется, и ничего не режется.
        rec, _usage, _model, trace = recommender.generate_with_trace(
            self._history(), [], self.CATALOG, today=_date(2026, 6, 12)
        )

        self.assertEqual(calls, 1)
        self.assertEqual(sum(len(exercise["sets"]) for exercise in rec["exercises"]), 13)
        self.assertEqual(trace[0]["adjustments"], [])
        self.assertEqual(trace[0]["violations"], [])
        self.assertNotIn("Проверка методики", rec["rationale"])

    def test_oversized_session_is_reprompted_then_trimmed_to_the_cap(self) -> None:
        from datetime import date as _date

        from trainer.domain import coach_state

        calls = 0

        def fake_call(*args, **kwargs):
            nonlocal calls
            calls += 1
            return self._fullbody_raw(), {"input_tokens": 10, "output_tokens": 5}

        anthropic_client._call_anthropic = fake_call
        # 13 сетов против потолка поддержания в 12, дважды: один репромпт
        # называет потолок, потом сервер убирает сет с хвоста и говорит об этом.
        state = dict(coach_state.default_state(), phase="maintenance")
        rec, _usage, _model, trace = recommender.generate_with_trace(
            self._history(), [], self.CATALOG, today=_date(2026, 6, 12), state=state
        )

        self.assertEqual(calls, 2)
        self.assertTrue(any("потолке сессии 12" in v for v in trace[0]["violations"]))
        self.assertEqual(sum(len(exercise["sets"]) for exercise in rec["exercises"]), 12)
        self.assertEqual(trace[1]["adjustments"], ["Сгибания ног −1"])
        hamstrings = next(e for e in rec["exercises"] if e["exercise_id"] == 15)
        self.assertEqual(len(hamstrings["sets"]), 1)  # урезано, не удалено
        self.assertIn("сокращена до 12 рабочих подходов", rec["rationale"])

    def test_unresolved_coverage_is_served_with_an_honest_note(self) -> None:
        from datetime import date as _date

        # Модель дважды игнорирует сухой бицепс бедра → план всё равно отдаётся,
        # а невыполненная граница названа в rationale.
        anthropic_client._call_anthropic = lambda *a, **k: (
            self._fullbody_raw(with_hamstrings=False),
            {"input_tokens": 1, "output_tokens": 1},
        )
        rec, usage, _model, trace = recommender.generate_with_trace(
            self._dry_hamstrings_history(), [], self.CATALOG, today=_date(2026, 6, 12)
        )
        self.assertEqual(len(trace), 2)
        self.assertTrue(trace[1]["violations"])
        self.assertIn("Проверка методики", rec["rationale"])
        self.assertIn("бицепс бедра", rec["rationale"])
        self.assertEqual(usage, {"input_tokens": 2, "output_tokens": 2})

    def test_comeback_overshoot_is_clamped_after_a_failed_reprompt(self) -> None:
        from datetime import date as _date

        # 21 день без зала; модель дважды настаивает на 105 против доперерывных
        # 100 → сервер зажимает провинившиеся сеты и говорит об этом в rationale.
        anthropic_client._call_anthropic = lambda *a, **k: (
            self._fullbody_raw(leg_press=105),
            {"input_tokens": 1, "output_tokens": 1},
        )
        rec, _usage, _model, trace = recommender.generate_with_trace(
            self._history("2026-05-22"), [], self.CATALOG, today=_date(2026, 6, 12)
        )
        self.assertEqual(len(trace), 2)
        self.assertTrue(trace[1]["adjustments"])
        leg_press = next(e for e in rec["exercises"] if e["exercise_id"] == 8)
        self.assertTrue(all(s["weight"] == 100 for s in leg_press["sets"]))
        self.assertIn("Проверка методики", rec["rationale"])
        self.assertIn("доперерывным", rec["rationale"])

    def test_answer_without_valid_exercises_fails_the_generation(self) -> None:
        from datetime import date as _date

        # Кривая форма ответа — провал генерации (кэш уйдёт в failed), а не 400,
        # как у кривого входа с клиента: recommender переводит ValueError из rules.
        anthropic_client._call_anthropic = lambda *a, **k: (
            {**self._fullbody_raw(), "exercises": [{"exercise_id": 999, "note": "n", "sets": []}]},
            {"input_tokens": 1, "output_tokens": 1},
        )
        with self.assertRaisesRegex(recommender.RecommendationError, "ни одного валидного"):
            recommender.generate_with_trace(
                self._history(), [], self.CATALOG, today=_date(2026, 6, 12)
            )


if __name__ == "__main__":
    unittest.main()
