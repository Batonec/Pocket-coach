"""Сборка промпта: отчёт «факт против плана», сериализация истории с RIR и
каноническими именами, состав user-промпта недельного отчёта."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

import support  # noqa: F401 — кладёт backend в sys.path
from support import CATALOG_PATH

from trainer.data import files
from trainer.domain import coach_state, prompt_builder, rules


class VolumeBlocksTests(unittest.TestCase):
    """Два блока объёма в промпте: 7 дней — темп без целей, КРУГ из четырёх
    тренировок — с целями по группам; явка несёт темп за 14 дней и интервалы."""

    def _workout(self, when: str, exercise_id: int = 18, sets: int = 3) -> dict:
        return {
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

    def _prompt(self, state: dict, days: list[str]) -> str:
        # Стор отдаёт от новых к старым — так же передаём и здесь.
        workouts = [self._workout(day) for day in reversed(days)]
        return prompt_builder._build_user_prompt(workouts, [], date(2026, 6, 10), 10, state=state)

    def test_targets_live_on_the_round_block_only(self) -> None:
        state = coach_state.default_state()
        state["phase_params"] = {"cut_recomp": {"group_targets": {"грудь": [12, 14]}}}
        prompt = self._prompt(
            state, ["2026-06-01", "2026-06-03", "2026-06-05", "2026-06-07", "2026-06-09"]
        )
        weekly, round_block = prompt.split("Объём за КРУГ", 1)
        self.assertIn("Объём за последние 7 дней — темп календарной недели", weekly)
        self.assertIn("грудь: 9 прямых / 9 эффективных", weekly)  # три сессии за 7 дней
        self.assertNotIn("(цель ", weekly)
        self.assertIn("последние 4 тренировки (2026-06-03 – 2026-06-09)", round_block)
        self.assertIn("грудь: 12 прямых (цель 12–14)", round_block)
        self.assertIn("Цели — в ПРЯМЫХ сетах на круг из четырёх тренировок", round_block)

    def test_maintenance_keeps_targets_on_the_week_and_has_no_round(self) -> None:
        state = coach_state.default_state()
        state["phase"] = "maintenance"
        prompt = self._prompt(state, ["2026-06-02", "2026-06-09"])
        self.assertIn("Режим поддержания", prompt)
        self.assertNotIn("Объём за КРУГ", prompt)

    def test_attendance_names_tempo_and_intervals(self) -> None:
        prompt = self._prompt(
            coach_state.default_state(),
            ["2026-05-28", "2026-05-30", "2026-06-01", "2026-06-03", "2026-06-06", "2026-06-09"],
        )
        self.assertIn("Темп: 6 тренировок за последние 14 дней", prompt)  # с 28 мая включительно
        self.assertIn("интервалы между последними сессиями, дней: 2, 2, 2, 3, 3.", prompt)
        self.assertNotIn("с ≥4", prompt)


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

    def _report(
        self,
        workouts: list[dict],
        *,
        state: dict | None = None,
        body_weights: list[dict] | None = None,
        previous_report: str | None = None,
    ) -> str:
        """Промпт отчёта на настоящем каталоге; состояние по умолчанию, если не дано."""
        return prompt_builder._build_report_prompt(
            workouts,
            body_weights or [],
            [],
            files.load_catalog(CATALOG_PATH),
            state or coach_state.default_state(),
            self.TODAY,
            7,
            previous_report=previous_report,
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

    def test_volume_header_carries_totals_for_two_weeks(self) -> None:
        """Разгон в программе задан суммами по неделям: без итога за период и за
        неделю до него «объём против цели» не сходится."""
        prompt = self._report([self._workout("2026-06-12", 105), self._workout("2026-06-03", 100)])
        self.assertIn("всего рабочих подходов за период 3, неделей раньше 3", prompt)

    def test_phase_trajectory_appears_with_a_phase_start(self) -> None:
        """Отчёт живёт в семи днях, гейт — в месяцах: с датой старта фазы модель
        получает вес, талию и темп с её начала, а также цель фазы по весу."""
        state = coach_state.default_state()
        state["phase_started"] = "2026-05-17"
        weights = [
            {"entry_date": "2026-05-16", "weight": 80.0},
            {"entry_date": "2026-06-13", "weight": 78.6},
        ]
        prompt = self._report(
            [self._workout("2026-06-12", 105), self._workout("2026-06-03", 100)],
            state=state,
            body_weights=weights,
        )
        self.assertIn("С начала фазы", prompt)
        self.assertIn("Фаза «лёгкий дефицит-рекомп»: 2026-05-17 → 2026-06-14 (4.1 нед).", prompt)
        self.assertIn("Вес: 80.0 → 78.6 кг (-1.4", prompt)
        self.assertNotIn("Оценка TDEE", prompt)  # две точки за фазу — тренда нет
        # Цель по весу — цифра стратегии атлета: дефолта у неё нет, стоковые 75.5
        # однажды доехали до отчёта на этапе, где худеть было не нужно.
        self.assertNotIn("Цель фазы по весу", prompt)
        state["phase_params"] = {"cut_recomp": {"target_weight_kg": 75.5}}
        prompt = self._report(
            [self._workout("2026-06-12", 105), self._workout("2026-06-03", 100)],
            state=state,
            body_weights=weights,
        )
        self.assertIn("Цель фазы по весу: 75.5 кг", prompt)

    def test_report_phase_line_uses_athlete_overrides(self) -> None:
        """Ориентиры фазы в отчёте — из phase_params атлета и без машинного имени:
        «без ПР — и почему это ок» судится по числам его фазы, а не дефолтов."""
        state = coach_state.default_state()
        state["phase_params"] = {"cut_recomp": {"title": "Ф0 · возврат", "calories": [1850, 1950]}}
        prompt = self._report([self._workout("2026-06-12", 105)], state=state)
        self.assertIn(
            "Фаза: «Ф0 · возврат», неделя блока 1. Ориентиры фазы: 1850–1950 ккал", prompt
        )
        self.assertNotIn("cut_recomp", prompt)

    def test_next_week_corridor_only_without_group_targets(self) -> None:
        """Коридор недели блока — эффективные сеты по дефолтной методике; при целях
        по группам он спорил бы с блоком «Объём за КРУГ» (прямые сеты, за круг)."""
        workouts = [self._workout("2026-06-12", 105)]
        self.assertIn("Цель следующей недели блока", self._report(workouts))
        state = coach_state.default_state()
        state["phase_params"] = {"cut_recomp": {"group_targets": {"спина": [12, 14]}}}
        self.assertNotIn("Цель следующей недели блока", self._report(workouts, state=state))

    def test_no_phase_start_means_no_trajectory(self) -> None:
        prompt = self._report([self._workout("2026-06-12", 105)])
        self.assertNotIn("С начала фазы", prompt)
        self.assertNotIn("Цель фазы по весу", prompt)

    def test_tdee_estimate_reaches_the_report_with_enough_history(self) -> None:
        state = coach_state.default_state()
        started = date(2026, 5, 3)  # 6 недель до воскресенья отчёта
        state["phase_started"] = started.isoformat()
        weights = [
            {
                "entry_date": (started + timedelta(days=offset)).isoformat(),
                "weight": round(80.0 - 0.5 * offset / 7, 2),
            }
            for offset in range(43)
        ]
        prompt = self._report([self._workout("2026-06-12", 105)], state=state, body_weights=weights)
        self.assertIn("Оценка TDEE (посчитано сервером): при ориентире 2150 ккал", prompt)
        self.assertIn("расход ≈ 2700 ккал/день", prompt)
        self.assertIn("Средняя за 7 дней:", prompt)

    PREVIOUS = (
        "**Итоги недели** — три тренировки.\n"
        "**Прогресс** — без ПР.\n"
        "**Курс к цели** — ПО ПЛАНУ.\n"
        "**Гейт этапа** — явка НЕ ВЫПОЛНЕН.\n"
        "**Фокус следующей недели**\n"
        "- **Объём:** 60 подходов.\n"
        "- Сгибания ног в каждую сессию.\n"
    )

    def test_previous_focus_is_cut_by_block_headings(self) -> None:
        """Жирный подпункт внутри фокуса — не заголовок блока: режем только по
        именам блоков из weekly_report.md, в любом оформлении."""
        focus = prompt_builder.previous_focus(self.PREVIOUS)
        assert focus is not None
        self.assertTrue(focus.startswith("**Фокус следующей недели**"))
        self.assertIn("**Объём:** 60 подходов.", focus)
        self.assertIn("Сгибания ног", focus)
        self.assertNotIn("Гейт этапа", focus)
        self.assertNotIn("ПО ПЛАНУ", focus)
        self.assertNotIn("три тренировки", focus)
        # Заголовок с решёткой и текстом после тире — тоже заголовок.
        loose = "### Фокус следующей недели — добрать спину\n### Гейт этапа\nнет"
        self.assertEqual(
            prompt_builder.previous_focus(loose), "### Фокус следующей недели — добрать спину"
        )
        self.assertIsNone(prompt_builder.previous_focus("**Итоги недели** — всё."))
        self.assertIsNone(prompt_builder.previous_focus(None))

    def test_previous_focus_is_capped(self) -> None:
        long = "**Фокус следующей недели**\n" + "слово " * 400
        focus = prompt_builder.previous_focus(long)
        assert focus is not None
        self.assertLessEqual(len(focus), prompt_builder.MAX_PREVIOUS_FOCUS_CHARS + 1)
        self.assertTrue(focus.endswith("…"))

    def test_previous_focus_reaches_the_prompt_only_when_given(self) -> None:
        with_memory = self._report(
            [self._workout("2026-06-12", 105)], previous_report=self.PREVIOUS
        )
        self.assertIn("Фокус, поставленный в прошлом отчёте", with_memory)
        self.assertIn("Сгибания ног в каждую сессию.", with_memory)
        self.assertNotIn("Фокус, поставленный", self._report([self._workout("2026-06-12", 105)]))

    def test_support_week_flag_reaches_plan_and_report(self) -> None:
        """Флаг недели поддержки стоит в подписи недели блока у обоих промптов;
        без отметки его нет."""
        state = coach_state.default_state()
        coach_state.mark_support_week(state, self.TODAY)  # неделя 8–14 июня
        report = self._report([self._workout("2026-06-12", 105)], state=state)
        self.assertIn(
            "неделя блока 1 — НЕДЕЛЯ ПОДДЕРЖКИ по плану (2026-06-08 – 2026-06-14)", report
        )
        plan = prompt_builder._build_user_prompt(
            [self._workout("2026-06-12", 105)],
            [],
            self.TODAY,
            10,
            catalog=files.load_catalog(CATALOG_PATH),
            state=state,
        )
        self.assertIn("НЕДЕЛЯ ПОДДЕРЖКИ по плану (2026-06-08 – 2026-06-14)", plan)
        self.assertNotIn("НЕДЕЛЯ ПОДДЕРЖКИ", self._report([self._workout("2026-06-12", 105)]))
