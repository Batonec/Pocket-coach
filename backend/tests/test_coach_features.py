"""Вычисляемые фичи истории: алиас и e1RM, сводки и ПР, объёмы, детектор застоя
по активному окну, рабочий вес, лестница возврата, тренды и матрица питания,
явка, дисциплина, итоги фазы.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

import support  # noqa: F401 — кладёт backend в sys.path

from trainer.domain import coach_features, coach_state, prompt_builder

CATALOG = [
    {"id": 18, "name": "Жим в тренажере"},
    {"id": 1, "name": "Жим гор."},
    {"id": 9, "name": "Тяга верт."},
    {"id": 4, "name": "Гравитрон"},
    {"id": 8, "name": "Жим ногами"},
]

TODAY = date(2026, 8, 14)


def _workout(when: str, exercises: list[tuple[int, list[tuple[float, int]]]]) -> dict:
    """Тренировка из списка ``(id, [(вес, повторы), ...])``."""
    return {
        "workout_date": when,
        "data": {
            "load_type": "medium",
            "exercises": [
                {
                    "exercise_id": exercise_id,
                    "name": f"#{exercise_id}",
                    "sets": [{"reps": reps, "weight": weight} for weight, reps in sets],
                }
                for exercise_id, sets in exercises
            ],
        },
    }


class AliasAndE1rmTests(unittest.TestCase):
    """Канонический id и формула Эпли."""

    def test_canonical_id_maps_the_duplicate(self) -> None:
        self.assertEqual(coach_features.canonical_exercise_id(1), 18)
        self.assertEqual(coach_features.canonical_exercise_id(18), 18)
        self.assertEqual(coach_features.canonical_exercise_id("9"), 9)
        self.assertIsNone(coach_features.canonical_exercise_id("x"))

    def test_epley(self) -> None:
        self.assertAlmostEqual(coach_features.epley_e1rm(100, 10), 133.33, places=1)


class SummaryTests(unittest.TestCase):
    """Сводка по упражнению: пик, ПР, проценты, инвертированный прогресс гравитрона."""

    def test_summary_tracks_peak_pr_and_percent(self) -> None:
        workouts = [
            _workout("2026-08-10", [(18, [(47.5, 13)])]),  # порядок: новые сверху
            _workout("2026-03-31", [(18, [(55, 12), (55, 12)])]),
            _workout("2026-03-01", [(1, [(50, 12)])]),  # дубль id сливается
        ]
        summaries = coach_features.exercise_summaries(workouts, CATALOG, TODAY)
        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary["exercise_id"], 18)
        self.assertEqual(summary["name"], "Жим в тренажере")
        self.assertEqual(summary["sessions_total"], 3)
        self.assertEqual(summary["top_weight"], 55)
        self.assertEqual(summary["last_pr_date"], "2026-03-31")
        self.assertEqual(summary["days_since_pr"], (TODAY - date(2026, 3, 31)).days)
        self.assertEqual(summary["current_weight"], 47.5)
        self.assertEqual(summary["pct_of_peak"], 86)

        text = prompt_builder.render_exercise_summaries(summaries)
        self.assertIn("пик 55×12", text)
        self.assertIn("86% пика", text)

    def test_gravitron_progress_is_inverted(self) -> None:
        workouts = [
            _workout("2026-08-01", [(4, [(28, 10)])]),
            _workout("2026-07-01", [(4, [(25, 12)])]),  # ПР: больше повторов на 25
            _workout("2026-06-15", [(4, [(25, 10)])]),  # ПР: меньше противовес
            _workout("2026-06-01", [(4, [(30, 10)])]),
        ]
        summary = coach_features.exercise_summaries(workouts, CATALOG, TODAY)[0]
        self.assertEqual(summary["top_weight"], 25)
        self.assertEqual(summary["top_reps"], 12)
        self.assertEqual(summary["last_pr_date"], "2026-07-01")
        self.assertEqual(summary["current_weight"], 28)
        self.assertEqual(summary["pct_of_peak"], 89)  # 25/28

        text = prompt_builder.render_exercise_summaries([summary])
        self.assertIn("противовес", text)
        self.assertIn("меньше = сильнее", text)


class WeeklyVolumeTests(unittest.TestCase):
    """Недельный объём в прямых и эффективных сетах."""

    def test_direct_and_effective_sets(self) -> None:
        workouts = [
            _workout("2026-08-13", [(18, [(50, 10)] * 3), (9, [(60, 10)] * 2)]),
            _workout("2026-08-01", [(18, [(50, 10)] * 5)]),  # вне 7-дневного окна
        ]
        volume = coach_features.weekly_volume(workouts, TODAY)
        self.assertEqual(volume["грудь"]["direct"], 3)
        self.assertEqual(volume["грудь"]["effective"], 3.0)
        self.assertEqual(volume["спина"]["direct"], 2)
        self.assertEqual(volume["трицепс"]["effective"], 1.5)  # 3 жима × 0.5
        # Горизонтальный жим грузит переднюю дельту, а не измеряемую среднюю →
        # только четверть сета за жим.
        self.assertEqual(volume["дельты"]["effective"], 0.75)  # 3 жима × 0.25
        self.assertEqual(volume["бицепс"]["effective"], 1.0)  # 2 тяги × 0.5

    def test_duplicate_press_id_counts_into_chest(self) -> None:
        workouts = [_workout("2026-08-13", [(1, [(50, 10)] * 4)])]
        volume = coach_features.weekly_volume(workouts, TODAY)
        self.assertEqual(volume["грудь"]["direct"], 4)


class StallTests(unittest.TestCase):
    """Детектор застоя: зелёные и красные предусловия, коридор темпа среза."""

    def _green_history(self) -> list[dict]:
        # 15 сессий внутри 6-недельного окна, ~12.5 сетов в неделю на каждую крупную
        # группу и один и тот же верхний вес всё время → застой по учебнику.
        """История с зелёными предусловиями и плоским весом — застой по учебнику."""
        workouts = []
        for index in range(15):
            when = (TODAY - timedelta(days=index * 2)).isoformat()
            workouts.append(
                _workout(
                    when,
                    [(18, [(60, 10)] * 5), (9, [(70, 10)] * 5), (8, [(100, 10)] * 5)],
                )
            )
        return workouts

    def test_green_preconditions_flag_the_stalled_lifts(self) -> None:
        workouts = self._green_history()
        summaries = coach_features.exercise_summaries(workouts, CATALOG, TODAY)
        report = coach_features.stall_report(workouts, summaries, 0.0, "lean_bulk", None, TODAY)
        self.assertTrue(report["preconditions_ok"])
        stalled_names = {s["name"] for s in report["stalled"]}
        self.assertIn("Жим в тренажере", stalled_names)

        text = prompt_builder.render_stall_report(report)
        self.assertIn("ЗАСТОЙ", text)
        self.assertIn("deload −10%", text)

    def test_red_preconditions_withhold_the_flag(self) -> None:
        workouts = [
            _workout((TODAY - timedelta(days=index * 10)).isoformat(), [(18, [(60, 10)] * 3)])
            for index in range(4)
        ]
        summaries = coach_features.exercise_summaries(workouts, CATALOG, TODAY)
        report = coach_features.stall_report(workouts, summaries, None, "lean_bulk", None, TODAY)
        self.assertFalse(report["preconditions_ok"])
        self.assertEqual(report["stalled"], [])
        text = prompt_builder.render_stall_report(report)
        self.assertIn("посещаемостью", text)

    def test_cut_requires_target_rate(self) -> None:
        workouts = self._green_history()
        summaries = coach_features.exercise_summaries(workouts, CATALOG, TODAY)
        report = coach_features.stall_report(
            workouts, summaries, +0.4, "cut_recomp", (-0.35, -0.25), TODAY
        )
        self.assertFalse(report["preconditions_ok"])
        self.assertTrue(any("темпа" in reason for reason in report["reasons"]))


class CurrentWorkingWeightTests(unittest.TestCase):
    """Текущий рабочий вес после фильтра аномалий."""

    def _sessions(self, workouts, exercise_id=8):
        """Сессии упражнения из истории."""
        return coach_features._iter_exercise_sessions(workouts)[exercise_id]

    def test_light_day_next_to_a_real_session_is_not_a_regression(self) -> None:
        workouts = [
            _workout("2026-07-22", [(8, [(40, 12)] * 3)]),  # лёгкий день
            _workout("2026-07-19", [(8, [(60, 12)] * 3)]),
        ]
        current = coach_features.current_working_weight(self._sessions(workouts), inverted=False)
        self.assertEqual(current, 60)

    def test_single_garbage_set_is_filtered(self) -> None:
        workouts = [
            _workout("2026-08-12", [(11, [(10, 12), (10, 12), (20, 3)])]),  # «20×3+»
            _workout("2026-08-09", [(11, [(10, 12)] * 3)]),
        ]
        sessions = coach_features._iter_exercise_sessions(workouts)[11]
        current = coach_features.current_working_weight(sessions, inverted=False)
        self.assertEqual(current, 10)

    def test_sessions_from_a_previous_era_are_ignored(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(80, 12)] * 3)]),  # после перерыва
            _workout("2026-04-20", [(8, [(120, 12)] * 3)]),  # прошлая эпоха
        ]
        current = coach_features.current_working_weight(self._sessions(workouts), inverted=False)
        self.assertEqual(current, 80)

    def test_summary_uses_the_filtered_metric(self) -> None:
        workouts = [
            _workout("2026-08-12", [(9, [(65, 12), (65, 12)])]),
            _workout("2026-08-09", [(9, [(75, 15), (80, 11)])]),
        ]
        summary = coach_features.exercise_summaries(workouts, CATALOG, TODAY)[0]
        # Пик — реальный ПОДХОД с лучшим e1RM (75×15), а не макс. вес × чужие повторы.
        self.assertEqual(summary["top_weight"], 75)
        self.assertEqual(summary["top_reps"], 15)
        self.assertEqual(summary["current_weight"], 80)  # макс. рабочий последних сессий


class RampInvariantTests(unittest.TestCase):
    """Лестница возврата: цель — доперерывный рабочий вес, ступень — шаг стека."""

    def test_target_is_the_pre_break_working_weight_not_the_peak(self) -> None:
        # Перерыв 40 дней между 07-01 и 08-10. Пик e1RM за всё время — 75×15, но
        # прямо перед паузой атлет РАБОТАЛ с 80 — лестница возвращает к нему, а
        # не к пику, поставленному в другое время.
        workouts = [
            _workout("2026-08-10", [(9, [(65, 12)] * 3)]),
            _workout("2026-07-01", [(9, [(75, 15)] * 2)]),  # пик e1RM 112.5
            _workout("2026-06-20", [(9, [(80, 11)] * 2)]),  # тяжелее, доперерывный рабочий
            _workout("2026-06-10", [(9, [(70, 12)] * 2)]),
        ]
        items = coach_features.comeback_ramp_steps(workouts, CATALOG, TODAY)
        item = next(i for i in items if i["exercise_id"] == 9)
        self.assertEqual(item["target"], 80)
        self.assertEqual(item["current"], 65)
        self.assertEqual(
            (item["break_start"], item["break_end"]), (date(2026, 7, 1), date(2026, 8, 10))
        )
        # Одна ступень — шаг стека (здесь 5 кг), до цели.
        self.assertEqual(item["steps"], [70, 75, 80])
        text = prompt_builder.render_comeback_ramp(items)[0]
        self.assertIn("доперерывный рабочий 80", text)
        self.assertNotIn("пик", text)

    def test_degenerate_one_jump_ladder_is_rebuilt(self) -> None:
        # Тренажёр, у которого единственный записанный шаг огромный (20 кг), дал бы
        # один прыжок 40→60 (+50%) — одиночный скачок не лестница, поэтому она
        # пересобирается равными шагами (три ступени по ~17% всё же лучше прыжка).
        workouts = [
            _workout("2026-08-10", [(10, [(40, 12)] * 3)]),
            _workout("2026-07-05", [(10, [(60, 12)] * 3)]),
            _workout("2026-06-25", [(10, [(40, 12)] * 3)]),
            _workout("2026-06-15", [(10, [(60, 12)] * 3)]),
        ]
        catalog = [*CATALOG, {"id": 10, "name": "Тяга горизонт."}]
        items = coach_features.comeback_ramp_steps(workouts, catalog, TODAY)
        item = next(i for i in items if i["exercise_id"] == 10)
        self.assertGreaterEqual(len(item["steps"]), 3)  # без одиночного прыжка 40→60
        previous = item["current"]
        for step in item["steps"]:
            self.assertGreater(step, previous)  # строго по возрастанию
            self.assertLess(step - previous, 20)  # каждая ступень меньше сырого разрыва
            previous = step
        self.assertEqual(item["steps"][-1], item["target"])

    def test_coarse_plates_keep_their_real_rungs(self) -> None:
        # Блины по 10 кг на жиме ногами в 80 кг — ступени по 12.5%: грубее
        # программных 10%, но других весов тренажёр не собирает. Выдуманные 82.5
        # были бы худшими данными, чем честные 90.
        workouts = [
            _workout("2026-08-10", [(8, [(80, 12)] * 3)]),
            _workout("2026-07-01", [(8, [(100, 10)] * 3)]),
            _workout("2026-06-24", [(8, [(90, 10)] * 3)]),
        ]
        item = coach_features.comeback_ramp_steps(workouts, CATALOG, TODAY)[0]
        self.assertEqual(item["steps"], [90, 100])

    def test_at_peak_means_no_ladder(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(120, 12)] * 3)]),
            _workout("2026-08-05", [(8, [(115, 12)] * 3)]),
        ]
        self.assertEqual(coach_features.comeback_ramp_steps(workouts, CATALOG, TODAY), [])

    def test_regained_weight_prints_nothing(self) -> None:
        # Снова на доперерывном рабочем весе (или выше) → подниматься некуда.
        workouts = [
            _workout("2026-08-12", [(8, [(100, 10)] * 3)]),
            _workout("2026-08-10", [(8, [(80, 12)] * 3)]),
            _workout("2026-07-01", [(8, [(100, 10)] * 3)]),
        ]
        self.assertEqual(coach_features.comeback_ramp_steps(workouts, CATALOG, TODAY), [])

    def test_no_ladder_while_still_on_the_break(self) -> None:
        # Сам день возврата: доперерывные веса — справочный блок, вес входа решает
        # модель — лестницы нет.
        workouts = [
            _workout("2026-07-01", [(8, [(100, 10)] * 3)]),
            _workout("2026-05-01", [(8, [(80, 12)] * 3)]),
            _workout("2026-04-01", [(8, [(120, 12)] * 3)]),
        ]
        self.assertEqual(coach_features.comeback_ramp_steps(workouts, CATALOG, TODAY), [])

    def test_old_comeback_is_history_not_a_ramp(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(80, 12)] * 3)]),
            _workout("2026-05-01", [(8, [(80, 12)] * 3)]),  # возврат 105 дней назад
            _workout("2026-03-01", [(8, [(120, 12)] * 3)]),
        ]
        self.assertEqual(coach_features.comeback_ramp_steps(workouts, CATALOG, TODAY), [])

    def test_gravitron_ladder_descends(self) -> None:
        workouts = [
            _workout("2026-08-10", [(4, [(32, 10)] * 3)]),
            _workout("2026-07-01", [(4, [(25, 12)] * 2)]),
            _workout("2026-06-20", [(4, [(27.5, 12)] * 2)]),
        ]
        items = coach_features.comeback_ramp_steps(workouts, CATALOG, TODAY)
        item = next(i for i in items if i["exercise_id"] == 4)
        self.assertTrue(item["inverted"])
        previous = item["current"]
        for step in item["steps"]:
            self.assertLess(step, previous)
            previous = step
        self.assertEqual(item["steps"][-1], 25)


class TrendValidityTests(unittest.TestCase):
    """Валидность тренда веса: дыры, граница фазы, плотные точки."""

    def test_gap_between_measurements_invalidates_the_trend(self) -> None:
        points = [(TODAY - timedelta(days=27), 77.25), (TODAY, 79.0)]
        self.assertIsNone(coach_features.weight_trend_per_week(points, TODAY))

    def test_phase_boundary_resets_the_base(self) -> None:
        phase_start = TODAY - timedelta(days=3)
        points = [
            (TODAY - timedelta(days=10), 77.0),  # прошлая фаза
            (TODAY - timedelta(days=2), 78.6),
            (TODAY, 79.0),
        ]
        self.assertIsNone(coach_features.weight_trend_per_week(points, TODAY, since=phase_start))
        # Без границы те же точки дают число.
        self.assertIsNotNone(coach_features.weight_trend_per_week(points, TODAY))

    def test_dense_in_phase_measurements_yield_a_trend(self) -> None:
        points = [
            (TODAY - timedelta(days=7), 79.4),
            (TODAY - timedelta(days=3), 79.2),
            (TODAY, 79.0),
        ]
        trend = coach_features.weight_trend_per_week(points, TODAY)
        assert trend is not None
        self.assertAlmostEqual(trend, -0.4, places=1)

    def test_matrix_asks_for_measurements_instead_of_advising(self) -> None:
        state = dict(
            coach_state.DEFAULT_STATE, phase_started=(TODAY - timedelta(days=1)).isoformat()
        )
        params = coach_state.phase_params(state)
        weights = [
            {"entry_date": (TODAY - timedelta(days=27)).isoformat(), "weight": 77.25},
            {"entry_date": TODAY.isoformat(), "weight": 79.0},
        ]
        result = coach_features.nutrition_matrix(state, params, weights, [], TODAY)
        self.assertIsNone(result["trend_per_week"])
        self.assertTrue(any("недостаточно" in line for line in result["lines"]))
        self.assertFalse(any("−150" in line for line in result["lines"]))
        self.assertFalse(any("тренд веса" in line for line in result["lines"]))


class CombackRampTests(unittest.TestCase):
    """Ступени возврата от текущего к доперерывному."""

    def test_steps_from_current_to_pre_break_use_the_machine_step(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(80, 12)])]),
            _workout("2026-04-20", [(8, [(120, 12)])]),
            _workout("2026-04-10", [(8, [(110, 12)])]),
            _workout("2026-04-01", [(8, [(100, 12)])]),
        ]
        lines = prompt_builder.comeback_ramp(workouts, CATALOG, TODAY)
        self.assertEqual(len(lines), 1)
        self.assertIn("Жим ногами: доперерывный рабочий 120, сейчас 80", lines[0])
        self.assertIn("Ступени: 90 → 100 → 110 → 120", lines[0])

    def test_no_ramp_when_current_is_at_peak(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(120, 12)])]),
            _workout("2026-08-01", [(8, [(115, 12)])]),
        ]
        self.assertEqual(prompt_builder.comeback_ramp(workouts, CATALOG, TODAY), [])


class NutritionMatrixTests(unittest.TestCase):
    """Матрица питания на срезе и наборе."""

    def _state(self, **overrides):
        """Состояние по умолчанию с переопределениями."""
        return dict(coach_state.DEFAULT_STATE, **overrides)

    def _params(self, state):
        """Параметры фазы состояния."""
        return coach_state.phase_params(state)

    def _weights(self, value: float, days: int = 21) -> list[dict]:
        """Ежедневные взвешивания с одним значением за ``days`` дней."""
        return [
            {"entry_date": (TODAY - timedelta(days=offset)).isoformat(), "weight": value}
            for offset in range(days, -1, -1)
        ]

    def test_cut_stall_with_waist_down_is_recomp_bonus(self) -> None:
        state = self._state()
        waists = [
            {"entry_date": (TODAY - timedelta(days=8)).isoformat(), "waist": 84.0},
            {"entry_date": (TODAY - timedelta(days=1)).isoformat(), "waist": 83.5},
        ]
        result = coach_features.nutrition_matrix(
            state, self._params(state), self._weights(79.0), waists, TODAY
        )
        self.assertTrue(any("рекомп-бонус" in line for line in result["lines"]))
        self.assertFalse(any("−150" in line for line in result["lines"]))

    def test_cut_stall_without_waist_drop_cuts_calories(self) -> None:
        state = self._state()
        result = coach_features.nutrition_matrix(
            state, self._params(state), self._weights(79.0), [], TODAY
        )
        self.assertTrue(any("−150 ккал" in line for line in result["lines"]))
        self.assertTrue(any("талии" in line for line in result["lines"]))  # просит замерить

    def test_cut_goal_reached_suggests_lean_bulk(self) -> None:
        state = self._state()
        result = coach_features.nutrition_matrix(
            state, self._params(state), self._weights(75.2), [], TODAY
        )
        self.assertIsNotNone(result["goal"])
        self.assertIn("lean_bulk", result["goal"])

    def test_bulk_waist_creep_pauses_the_surplus(self) -> None:
        state = self._state(phase="lean_bulk", waist_base_cm=84.0)
        weights = [
            {
                "entry_date": (TODAY - timedelta(days=offset)).isoformat(),
                "weight": 80.0 + (21 - offset) * 0.02,
            }
            for offset in range(21, -1, -1)
        ]
        waists = [
            {"entry_date": (TODAY - timedelta(days=8)).isoformat(), "waist": 85.2},
            {"entry_date": (TODAY - timedelta(days=1)).isoformat(), "waist": 85.4},
        ]
        result = coach_features.nutrition_matrix(state, self._params(state), weights, waists, TODAY)
        self.assertTrue(any("паузу набора" in line for line in result["lines"]))

    def test_bulk_waist_at_limit_is_a_hard_signal(self) -> None:
        state = self._state(phase="lean_bulk", waist_limit_cm=86.0)
        waists = [
            {"entry_date": (TODAY - timedelta(days=1)).isoformat(), "waist": 86.2},
        ]
        result = coach_features.nutrition_matrix(
            state, self._params(state), self._weights(80.0), waists, TODAY
        )
        self.assertIsNotNone(result["goal"])
        self.assertIn("мини-кат", result["goal"])

    def test_stale_weight_blocks_calorie_advice(self) -> None:
        state = self._state()
        old_weights = [
            {"entry_date": (TODAY - timedelta(days=20)).isoformat(), "weight": 79.0},
        ]
        result = coach_features.nutrition_matrix(state, self._params(state), old_weights, [], TODAY)
        self.assertTrue(any("устарел" in line for line in result["lines"]))
        self.assertFalse(any("ккал" in line for line in result["lines"] if "−100" in line))


class SupportWeekMatrixTests(unittest.TestCase):
    """Недели поддержки: матрица молчит на них и две недели после, их точки не в тренде."""

    def _state(self, *mondays: date) -> dict:
        """Срез по умолчанию с отмеченными неделями поддержки."""
        return coach_state.normalize_state(
            dict(coach_state.DEFAULT_STATE, support_weeks=[m.isoformat() for m in mondays])
        )

    def _daily(self, per_week: float, days: int = 28, bump: tuple[date, date] | None = None):
        """Ежедневные взвешивания с темпом ``per_week`` и, по желанию, +1 кг в ``bump``."""
        points = []
        for offset in range(days, -1, -1):
            when = TODAY - timedelta(days=offset)
            weight = 80.0 + per_week * (days - offset) / 7
            if bump and bump[0] <= when <= bump[1]:
                weight += 1.0
            points.append({"entry_date": when.isoformat(), "weight": round(weight, 2)})
        return points

    def _lines(self, state: dict, weights: list[dict]) -> list[str]:
        return coach_features.nutrition_matrix(
            state, coach_state.phase_params(state), weights, [], TODAY
        )["lines"]

    def test_current_support_week_silences_calorie_advice(self) -> None:
        monday = coach_state._week_start(TODAY)
        lines = self._lines(self._state(monday), self._daily(0.0))  # вес стоит
        self.assertTrue(any("НЕДЕЛЯ ПОДДЕРЖКИ" in line for line in lines))
        self.assertFalse(any("ккал" in line for line in lines))
        # Без отметки та же неделя стоячего веса на срезе — повод резать калории.
        plain = self._lines(coach_state.default_state(), self._daily(0.0))
        self.assertTrue(any("−150 ккал" in line for line in plain))

    def test_confirmation_window_restarts_after_a_support_week(self) -> None:
        last_week = coach_state._week_start(TODAY) - timedelta(days=7)
        lines = self._lines(self._state(last_week), self._daily(0.0))
        self.assertTrue(any("окно коррекции набирается заново" in line for line in lines))
        self.assertFalse(any("ккал" in line for line in lines))

    def test_support_week_points_do_not_bend_the_trend(self) -> None:
        """Три недели назад — неделя поддержки с +1 кг: без исключения её точек
        тренд выглядел бы обвалом темпа, с ним — темп в коридоре фазы."""
        monday = coach_state._week_start(TODAY) - timedelta(days=21)
        bump = (monday, monday + timedelta(days=6))
        weights = self._daily(-0.3, bump=bump)  # коридор среза −0.35…−0.25
        lines = self._lines(self._state(monday), weights)
        self.assertTrue(any("в коридоре фазы" in line for line in lines), lines)
        plain = self._lines(coach_state.default_state(), weights)
        self.assertFalse(any("в коридоре фазы" in line for line in plain), plain)


class MeasurementRenderTests(unittest.TestCase):
    """Рендер замеров: мусор отбрасывается, талия перечисляется, средняя названа."""

    def test_drops_garbage_and_lists_waist(self) -> None:
        weights = [
            {"entry_date": "2026-08-01", "weight": 22.0},  # шум логирования
            {"entry_date": "2026-08-10", "weight": 79.0},
        ]
        waists = [{"entry_date": "2026-08-10", "waist": 84.0}]
        lines = prompt_builder.render_measurements(weights, waists, TODAY)
        text = "\n".join(lines)
        self.assertIn("79кг", text)
        self.assertNotIn("22кг", text)
        self.assertIn("отброшено неправдоподобных записей: 1", text)
        self.assertIn("Талия: 2026-08-10: 84см", text)
        self.assertNotIn("Средняя за 7 дней", text)  # одной точки для средней мало

    def test_weekly_mean_is_named_when_points_suffice(self) -> None:
        """Стратегия управляет 7-дневной средней: она названа числом, рядом
        средняя неделей раньше — «стоит / движется» без пересчёта моделью."""
        weights = [
            {"entry_date": (TODAY - timedelta(days=offset)).isoformat(), "weight": weight}
            for offset, weight in ((13, 80.0), (11, 80.0), (9, 80.0), (8, 80.0), (7, 80.0))
        ] + [
            {"entry_date": (TODAY - timedelta(days=offset)).isoformat(), "weight": weight}
            for offset, weight in ((5, 79.6), (3, 79.4), (1, 79.2), (0, 79.0))
        ]
        text = "\n".join(prompt_builder.render_measurements(weights, [], TODAY))
        self.assertIn("Средняя за 7 дней: 79.3 кг (неделей раньше 80.0, -0.7).", text)


class SetsInWindowTests(unittest.TestCase):
    """Итог подходов за окно: те же подходы, что weekly_volume раскладывает по группам."""

    def test_counts_only_the_window(self) -> None:
        workouts = [
            _workout(TODAY.isoformat(), [(8, [(100, 10)] * 3), (11, [(10, 12)] * 2)]),
            _workout((TODAY - timedelta(days=6)).isoformat(), [(9, [(60, 10)] * 4)]),
            _workout((TODAY - timedelta(days=7)).isoformat(), [(9, [(60, 10)] * 4)]),
        ]
        self.assertEqual(coach_features.sets_in_window(workouts, TODAY), 9)
        self.assertEqual(coach_features.sets_in_window(workouts, TODAY - timedelta(days=7)), 4)
        self.assertEqual(coach_features.sets_in_window([], TODAY), 0)


class TdeeEstimateTests(unittest.TestCase):
    """Калибровка TDEE по темпу веса за 4 недели при ориентире фазы."""

    def _weights(self, start: date, days: int, per_week: float, first: float = 80.0):
        """Ежедневные взвешивания с линейным темпом ``per_week`` от ``start``."""
        return [
            (start + timedelta(days=offset), first + per_week * offset / 7)
            for offset in range(days + 1)
        ]

    def test_deficit_rate_raises_the_estimate_above_intake(self) -> None:
        state = dict(coach_state.DEFAULT_STATE)  # cut_recomp, 2100–2200 ккал
        params = coach_state.phase_params(state)
        started = TODAY - timedelta(days=42)
        estimate = coach_features.tdee_estimate(
            params, self._weights(started, 42, -0.5), TODAY, phase_start=started
        )
        assert estimate is not None
        self.assertEqual(estimate["intake"], 2150.0)
        self.assertAlmostEqual(estimate["trend_per_week"], -0.5, places=2)
        self.assertEqual(estimate["tdee"], 2700)  # 2150 + 0.5 × 7700 / 7

    def test_young_phase_and_holding_phase_give_nothing(self) -> None:
        state = dict(coach_state.DEFAULT_STATE)
        params = coach_state.phase_params(state)
        started = TODAY - timedelta(days=20)  # первые две недели — вода, окна ещё нет
        self.assertIsNone(
            coach_features.tdee_estimate(
                params, self._weights(started, 20, -0.5), TODAY, phase_start=started
            )
        )
        holding = coach_state.phase_params(dict(coach_state.DEFAULT_STATE, phase="maintenance"))
        started = TODAY - timedelta(days=42)
        self.assertIsNone(
            coach_features.tdee_estimate(
                holding, self._weights(started, 42, -0.5), TODAY, phase_start=started
            )
        )
        self.assertIsNone(
            coach_features.tdee_estimate(
                params, self._weights(started, 42, -0.5), TODAY, phase_start=None
            )
        )


class AdherenceStatsTests(unittest.TestCase):
    """Дисциплина «факт против плана» со сравнением через алиас."""

    def _planned_workout(self, when: str, fact_first: int = 3, include_second: bool = False):
        """Тренировка со снапшотом плана (жим 3 сета, сгибания 2); факт — ``fact_first``
        сетов жима под дублирующим id 1 и, по флагу, сгибания.
        """
        workout = _workout("PLACEHOLDER", [])
        workout["workout_date"] = when
        workout["data"]["recommendation"] = {
            "schema": 1,
            "exercises": [
                {
                    "exercise_id": 18,
                    "name": "Жим в тренажере",
                    "sets": [{"reps": 10, "weight": 50}] * 3,
                },
                {
                    "exercise_id": 15,
                    "name": "Сгибания ног",
                    "sets": [{"reps": 12, "weight": 30}] * 2,
                },
            ],
        }
        exercises = []
        if fact_first:
            # Записано под дублирующим id 1 — должно сойтись с планом id 18 через алиас.
            exercises.append(
                {
                    "exercise_id": 1,
                    "name": "Жим гор.",
                    "sets": [{"reps": 10, "weight": 50}] * fact_first,
                }
            )
        if include_second:
            exercises.append(
                {
                    "exercise_id": 15,
                    "name": "Сгибания ног",
                    "sets": [{"reps": 12, "weight": 30}] * 2,
                }
            )
        workout["data"]["exercises"] = exercises
        return workout

    def test_aggregates_pct_and_skips_with_alias_matching(self) -> None:
        workouts = [
            self._planned_workout((TODAY - timedelta(days=2)).isoformat()),
            self._planned_workout(
                (TODAY - timedelta(days=9)).isoformat(), fact_first=4, include_second=True
            ),
            self._planned_workout((TODAY - timedelta(days=60)).isoformat()),  # вне окна
        ]
        stats = coach_features.adherence_stats(workouts, TODAY)
        assert stats is not None
        self.assertEqual(stats["sessions"], 2)
        self.assertEqual(stats["planned_sets"], 10)
        # 3 + min(4, 3) + 2 = 8; лишние сеты никогда не поднимают выше плана.
        self.assertEqual(stats["done_sets"], 8)
        self.assertEqual(stats["pct"], 80)
        self.assertEqual(stats["skipped"], [("Сгибания ног", 1)])

        line = prompt_builder.render_adherence_stats(stats)
        assert line is not None
        self.assertIn("8 из 10", line)
        self.assertIn("Сгибания ног ×1", line)

    def test_none_without_snapshots(self) -> None:
        workouts = [_workout(TODAY.isoformat(), [(18, [(50, 10)] * 3)])]
        self.assertIsNone(coach_features.adherence_stats(workouts, TODAY))


class PhaseSummaryTests(unittest.TestCase):
    """Итоги фазы по границам дат."""

    def test_summary_derives_everything_from_the_date_range(self) -> None:
        started, ended = date(2026, 8, 1), date(2026, 8, 28)  # 4 недели
        workouts = [
            _workout("2026-08-05", [(8, [(100, 10)] * 3)]),
            _workout("2026-08-12", [(8, [(105, 10)] * 3)]),  # ПР внутри отрезка
            _workout("2026-08-19", [(8, [(105, 10)] * 3)]),
            _workout("2026-07-01", [(8, [(90, 10)] * 3)]),  # до фазы
        ]
        weights = [
            {"entry_date": "2026-07-30", "weight": 79.0},
            {"entry_date": "2026-08-27", "weight": 77.6},
        ]
        waists = [
            {"entry_date": "2026-08-02", "waist": 86.0},
            {"entry_date": "2026-08-26", "waist": 84.5},
        ]
        summary = coach_features.phase_summary(
            workouts,
            weights,
            waists,
            CATALOG,
            phase="cut_recomp",
            started=started,
            ended=ended,
        )
        self.assertEqual(summary["workouts"], 3)
        self.assertEqual(summary["weight_start"], 79.0)
        self.assertEqual(summary["weight_end"], 77.6)
        self.assertAlmostEqual(summary["weight_rate_per_week"], -0.35, places=2)
        self.assertEqual(summary["waist_start"], 86.0)
        self.assertEqual(summary["waist_end"], 84.5)
        self.assertEqual(summary["pr_events"], 2)  # и 100, и 105 бьют июльскую базу
        self.assertEqual(summary["prs"][0]["name"], "Жим ногами")

        text = prompt_builder.render_phase_summary(summary)
        self.assertIn("Фаза cut_recomp", text)
        self.assertIn("79.0 → 77.6", text)
        self.assertIn("ПР за фазу: 2", text)

    def test_pr_dates_exclude_the_baseline_session(self) -> None:
        workouts = [
            _workout("2026-08-10", [(8, [(105, 10)])]),  # улучшение
            _workout("2026-08-01", [(8, [(100, 10)])]),  # база
        ]
        summary = coach_features.exercise_summaries(workouts, CATALOG, TODAY)[0]
        self.assertEqual(summary["pr_dates"], ["2026-08-10"])


class GroupTargetTests(unittest.TestCase):
    """Цели по группам: коридор недели, малые группы, поддержание."""

    def test_big_groups_follow_the_week_corridor(self) -> None:
        targets = coach_features.group_volume_targets((6, 8))
        self.assertEqual(targets["грудь"], (6, 8))
        self.assertEqual(targets["спина"], (6, 8))
        self.assertEqual(targets["квадрицепс/ягодичные"], (6, 8))
        self.assertEqual(targets["дельты"], (6, 12))  # малые группы фиксированы
        self.assertEqual(targets["бицепс бедра"], (5, 10))
        self.assertEqual(set(targets), set(coach_features.MUSCLE_GROUPS))

    def test_maintenance_flattens_everything(self) -> None:
        targets = coach_features.group_volume_targets(None, maintenance_sets=(2, 3))
        self.assertTrue(all(target == (2, 3) for target in targets.values()))

    def test_missing_corridor_falls_back_to_policy_cap(self) -> None:
        self.assertEqual(coach_features.group_volume_targets(None)["грудь"], (10, 16))


class WeightRangeTests(unittest.TestCase):
    """Диапазон весов за 8 недель со слиянием дубля."""

    def test_eight_week_range_merges_the_duplicate_id(self) -> None:
        workouts = [
            _workout("2026-08-10", [(18, [(50, 10), (55, 8)])]),
            _workout("2026-07-20", [(1, [(45, 12)])]),
            _workout("2026-01-01", [(18, [(70, 10)])]),  # далеко за 8 неделями
        ]
        self.assertEqual(coach_features.recent_weight_range(workouts, 18, TODAY), (45.0, 55.0))
        self.assertIsNone(coach_features.recent_weight_range(workouts, 9, TODAY))


if __name__ == "__main__":
    unittest.main()


class HoldingPhaseMatrixTests(unittest.TestCase):
    """Этап может просить ДЕРЖАТЬ вес. Тогда «вес стоит» — выполненная задача,
    а не повод срезать калории; захардкоженный коридор советовал бы дефицит
    ровно за правильное поведение."""

    def _points(self, *pairs):
        """Взвешивания из пар (дата, кг)."""
        return [{"entry_date": d, "weight": w} for d, w in pairs]

    def _matrix(self, rate):
        """Матрица для cut_recomp с заданным коридором темпа на плоском весе."""
        state = dict(coach_state.DEFAULT_STATE, phase="cut_recomp", phase_started="2026-06-01")
        params = dict(coach_state.PHASE_DEFAULTS["cut_recomp"], rate_kg_per_week=rate)
        params["phase"] = "cut_recomp"
        weights = self._points(
            ("2026-07-25", 79.0),
            ("2026-07-28", 79.1),
            ("2026-08-01", 78.9),
            ("2026-08-05", 79.0),
            ("2026-08-09", 79.1),
            ("2026-08-13", 79.0),
            ("2026-08-15", 79.0),
        )
        return coach_features.nutrition_matrix(state, params, weights, [], date(2026, 8, 16))

    def test_holding_phase_does_not_ask_to_cut_calories(self):
        lines = " ".join(self._matrix((-0.10, 0.10))["lines"])
        self.assertIn("это и есть задача этапа", lines)
        self.assertNotIn("−100 ккал", lines)

    def test_cutting_phase_still_asks_to_cut_on_a_stall(self):
        lines = " ".join(self._matrix((-0.35, -0.25))["lines"])
        self.assertIn("−150 ккал", lines)

    def test_goal_reached_is_not_announced_while_holding(self):
        """target_weight_kg остаётся дефолтным на этапе удержания — объявлять
        «цель фазы достигнута» на нём нельзя."""
        self.assertIsNone(self._matrix((-0.10, 0.10))["goal"])


class ActiveWindowStallTests(unittest.TestCase):
    """Предусловия и застой считаются по АКТИВНОМУ окну: от старта блока, а не за
    шесть календарных недель, в которые попадает отпуск."""

    def _sessions(self, days_ago: list[int], weight: float = 60.0) -> list[dict]:
        # Все крупные группы в каждой сессии, так что вердикт решают только
        # частота и длина окна.
        """Сессии ``days_ago`` дней назад со всеми крупными группами."""
        return [
            _workout(
                (TODAY - timedelta(days=ago)).isoformat(),
                [(18, [(weight, 10)] * 5), (9, [(70, 10)] * 5), (8, [(100, 10)] * 5)],
            )
            for ago in days_ago
        ]

    def test_window_never_reaches_across_the_block_anchor(self) -> None:
        # Шесть сессий за две недели после возврата; отпуск перед ними читался бы
        # как «1.5/нед» за шесть недель.
        workouts = self._sessions([1, 3, 5, 8, 10, 12])
        report = coach_features.stall_report(
            workouts, [], 0.0, "lean_bulk", None, TODAY, since=TODAY - timedelta(days=13)
        )
        self.assertEqual(report["window_days"], 14)
        self.assertAlmostEqual(report["frequency"], 3.0, places=2)
        self.assertTrue(report["too_short"])
        self.assertFalse(report["preconditions_ok"])
        text = prompt_builder.render_stall_report(report)
        self.assertIn("Активное окно 14 дн.", text)
        self.assertIn("пока не оцениваются", text)
        self.assertNotIn("НЕ выполнены", text)

    def test_group_targets_set_the_volume_threshold(self) -> None:
        # 15 сессий × 3 сета жима через день = 12 сетов груди за КРУГ из четырёх
        # тренировок: хватает против плоских 10, но не против программы, которая
        # просит 14–16 за круг. Календарная неделя тут ни при чём: атлет через
        # день делает 10.5 сета в неделю и по ней «недобирал» бы всегда.
        workouts = [
            _workout((TODAY - timedelta(days=index * 2)).isoformat(), [(18, [(60, 10)] * 3)])
            for index in range(15)
        ]
        loose = coach_features.stall_report(workouts, [], 0.0, "lean_bulk", None, TODAY)
        self.assertFalse(any("грудь" in reason for reason in loose["reasons"]))
        strict = coach_features.stall_report(
            workouts, [], 0.0, "lean_bulk", None, TODAY, group_targets={"грудь": (14, 16)}
        )
        self.assertTrue(any("грудь 12.0 < 14" in reason for reason in strict["reasons"]))
        self.assertIn("грудь 12.0 (порог 14)", prompt_builder.render_stall_report(strict))
        self.assertIn("за круг из 4 тренировок", prompt_builder.render_stall_report(strict))

    def test_stall_clock_runs_inside_the_window_not_from_the_all_time_pr(self) -> None:
        # Пик 70 поставлен 100 дней назад, потом перерыв, потом пять недель подъёма
        # 50 → 60: ПР за всё время старый, но движение улучшилось 6 дней назад.
        climb = [
            (34, 50),
            (32, 50),
            (30, 52.5),
            (27, 52.5),
            (25, 55),
            (23, 55),
            (20, 55),
            (18, 57.5),
            (16, 57.5),
            (13, 57.5),
            (11, 60),
            (9, 60),
            (6, 62.5),
            (4, 62.5),
            (2, 62.5),
        ]
        workouts = [
            _workout(
                (TODAY - timedelta(days=ago)).isoformat(),
                [(18, [(weight, 10)] * 4), (9, [(70, 10)] * 4), (8, [(100, 10)] * 4)],
            )
            for ago, weight in climb
        ]
        workouts.append(_workout((TODAY - timedelta(days=100)).isoformat(), [(18, [(70, 10)] * 4)]))
        summaries = coach_features.exercise_summaries(workouts, CATALOG, TODAY)
        self.assertGreaterEqual(
            summaries[0]["days_since_pr"], 100
        )  # часы «за всё время» говорят «застой»
        report = coach_features.stall_report(
            workouts, summaries, 0.0, "lean_bulk", None, TODAY, since=TODAY - timedelta(days=34)
        )
        self.assertTrue(report["preconditions_ok"])
        # Часы внутри окна говорят «6 дней назад» для жима; застоялись два
        # движения, простоявшие плоско весь блок.
        self.assertEqual([s["name"] for s in report["stalled"]], ["Тяга верт.", "Жим ногами"])

        flat = [
            _workout(
                (TODAY - timedelta(days=ago)).isoformat(),
                [(18, [(60, 10)] * 4), (9, [(70, 10)] * 4), (8, [(100, 10)] * 4)],
            )
            for ago, _ in climb
        ]
        report = coach_features.stall_report(
            flat,
            coach_features.exercise_summaries(flat, CATALOG, TODAY),
            0.0,
            "lean_bulk",
            None,
            TODAY,
            since=TODAY - timedelta(days=34),
        )
        self.assertEqual(
            [s["name"] for s in report["stalled"]], ["Жим в тренажере", "Тяга верт.", "Жим ногами"]
        )
        self.assertEqual(report["stalled"][0]["quiet_days"], 34)
        self.assertIn("без прироста 34 дн.", prompt_builder.render_stall_report(report))

    def test_young_window_withholds_the_stall_verdict(self) -> None:
        workouts = self._sessions([1, 3, 5, 8, 10, 12, 15, 17, 19, 21])
        report = coach_features.stall_report(
            workouts, [], 0.0, "lean_bulk", None, TODAY, since=TODAY - timedelta(days=21)
        )
        self.assertEqual(report["window_days"], 22)
        self.assertTrue(report["preconditions_ok"])
        self.assertIn("застой ещё не оценивается", prompt_builder.render_stall_report(report))

    def test_weight_reason_names_the_corridor_of_any_phase(self) -> None:
        workouts = self._sessions([1, 3, 5, 8, 10, 12, 15, 17, 19, 21, 24, 26, 28, 31, 33])
        holding = coach_features.stall_report(workouts, [], +0.35, "cut_recomp", (-0.1, 0.1), TODAY)
        self.assertTrue(any("при коридоре -0.10…+0.10" in r for r in holding["reasons"]))
        self.assertFalse(any("среза" in r for r in holding["reasons"]))
        gaining = coach_features.stall_report(workouts, [], +0.35, "lean_bulk", (0.1, 0.2), TODAY)
        self.assertFalse(
            any("вес" in r for r in gaining["reasons"])
        )  # быстрый набор — не красный флаг
        falling = coach_features.stall_report(workouts, [], -0.3, "lean_bulk", (0.1, 0.2), TODAY)
        self.assertTrue(any("вес падает" in r for r in falling["reasons"]))


class AttendanceTests(unittest.TestCase):
    """Явка по календарным неделям."""

    def test_calendar_weeks_streaks_and_the_open_week(self) -> None:
        # TODAY — пятница 2026-08-14; текущая неделя — пн 10 → вс 16.
        days = ["2026-07-20", "2026-07-27", "2026-07-29", "2026-07-31", "2026-08-01"]
        days += ["2026-08-03", "2026-08-05", "2026-08-07", "2026-08-11"]
        workouts = [_workout(day, [(8, [(100, 10)])]) for day in days]
        rows = coach_features.weekly_attendance(workouts, TODAY)
        self.assertEqual([row["sessions"] for row in rows], [0, 1, 4, 3, 1])
        self.assertEqual([row["closed"] for row in rows], [True, True, True, True, False])
        self.assertEqual(coach_features.attendance_streak(rows, 3), 2)
        self.assertEqual(coach_features.attendance_streak(rows, 4), 0)
        text = prompt_builder.render_weekly_attendance(rows, TODAY)
        self.assertIn("2026-07-27…2026-08-02: 4", text)
        self.assertIn("2026-08-10…2026-08-16 (текущая, по 2026-08-14): 1", text)

    def test_two_sessions_on_one_day_count_as_one_training_day(self) -> None:
        workouts = [_workout("2026-08-11", [(8, [(100, 10)])])] * 2
        rows = coach_features.weekly_attendance(workouts, TODAY)
        self.assertEqual(rows[-1]["sessions"], 1)


class RoundAndTempoTests(unittest.TestCase):
    """Объём за круг из четырёх тренировок, темп за 14 дней и интервалы между
    сессиями — факты, по которым каркас идёт ротацией, а не календарной неделей."""

    def test_round_volume_takes_the_last_four_training_days(self) -> None:
        days = ["2026-08-04", "2026-08-06", "2026-08-08", "2026-08-10", "2026-08-12"]
        workouts = [_workout(day, [(18, [(60, 10)] * 3)]) for day in days]
        volume, picked = coach_features.round_volume(workouts, TODAY)
        self.assertEqual([day.isoformat() for day in picked], days[1:])
        self.assertEqual(volume["грудь"]["direct"], 12)  # 4 × 3, первая сессия за кругом
        # Две сессии в один день — один тренировочный день круга.
        doubled = [*workouts, _workout("2026-08-12", [(17, [(25, 12)] * 2)])]
        volume, picked = coach_features.round_volume(doubled, TODAY)
        self.assertEqual(len(picked), 4)
        self.assertEqual(volume["грудь"]["direct"], 14)

    def test_round_volume_ignores_workouts_after_today_and_empty_history(self) -> None:
        workouts = [
            _workout("2026-08-12", [(18, [(60, 10)])]),
            _workout("2026-08-20", [(18, [(60, 10)] * 5)]),
        ]
        volume, picked = coach_features.round_volume(workouts, TODAY)
        self.assertEqual([day.isoformat() for day in picked], ["2026-08-12"])
        self.assertEqual(volume["грудь"]["direct"], 1)
        self.assertEqual(coach_features.round_volume([], TODAY)[1], [])

    def test_tempo_and_intervals(self) -> None:
        days = ["2026-07-30", "2026-08-01", "2026-08-03", "2026-08-05"]
        days += ["2026-08-08", "2026-08-10", "2026-08-12", "2026-08-14"]
        workouts = [_workout(day, [(8, [(100, 10)])]) for day in days]
        self.assertEqual(coach_features.sessions_in_window(workouts, TODAY, 14), 7)  # с 1 августа
        self.assertEqual(coach_features.recent_intervals(workouts, TODAY), [2, 3, 2, 2, 2])
        self.assertEqual(coach_features.recent_intervals(workouts[:1], TODAY), [])


class TrendSlopeTests(unittest.TestCase):
    """МНК-наклон тренда и счётчик взвешиваний."""

    def test_slope_uses_every_point_not_the_end_points(self) -> None:
        # Плоская неделя с одним тяжёлым последним утром: крайние точки говорят
        # +0.5/нед, МНК-прямая — ~+0.4: одно взвешивание больше не правит.
        points = [
            (TODAY - timedelta(days=14), 79.0),
            (TODAY - timedelta(days=10), 79.1),
            (TODAY - timedelta(days=7), 79.0),
            (TODAY - timedelta(days=3), 79.1),
            (TODAY, 80.0),
        ]
        trend = coach_features.weight_trend_per_week(points, TODAY)
        assert trend is not None
        self.assertLess(trend, 0.45)
        self.assertGreater(trend, 0.3)

    def test_two_points_are_still_the_plain_slope(self) -> None:
        points = [(TODAY - timedelta(days=7), 80.0), (TODAY, 79.5)]
        trend = coach_features.weight_trend_per_week(points, TODAY)
        assert trend is not None
        self.assertAlmostEqual(trend, -0.5, places=6)

    def test_weigh_in_count_is_reported_with_the_protocol_minimum(self) -> None:
        weights = [
            {"entry_date": (TODAY - timedelta(days=offset)).isoformat(), "weight": 79.0}
            for offset in (9, 5, 1)
        ]
        text = "\n".join(prompt_builder.render_measurements(weights, [], TODAY))
        self.assertIn("Замеров за последние 7 дней: 2 (для недельной средней нужно ≥4).", text)
        daily = [
            {"entry_date": (TODAY - timedelta(days=offset)).isoformat(), "weight": 79.0}
            for offset in range(6)
        ]
        text = "\n".join(prompt_builder.render_measurements(daily, [], TODAY))
        self.assertIn("Замеров за последние 7 дней: 6.", text)
        self.assertNotIn("нужно ≥", text)


class RateMatrixTests(unittest.TestCase):
    """Ветки матрицы строятся от коридора темпа фазы, а не от её имени."""

    def _matrix(self, phase: str, rate, weights, waists=None, **state_overrides):
        """Матрица для фазы с заданным коридором темпа."""
        state = dict(
            coach_state.DEFAULT_STATE,
            phase=phase,
            phase_started=(TODAY - timedelta(days=30)).isoformat(),
            **state_overrides,
        )
        params = dict(coach_state.PHASE_DEFAULTS[phase], rate_kg_per_week=rate)
        params["phase"] = phase
        return coach_features.nutrition_matrix(state, params, weights, waists or [], TODAY)

    def _daily(self, start: float, per_day: float, days: int = 21) -> list[dict]:
        """Ежедневные взвешивания с линейным дрейфом ``per_day``."""
        return [
            {
                "entry_date": (TODAY - timedelta(days=offset)).isoformat(),
                "weight": round(start + (days - offset) * per_day, 3),
            }
            for offset in range(days, -1, -1)
        ]

    def test_holding_phase_rising_weight_gets_a_confirmed_cut(self) -> None:
        result = self._matrix("cut_recomp", (-0.1, 0.1), self._daily(78.0, 0.07))
        line = " ".join(result["lines"])
        self.assertIn("выше коридора фазы (-0.10…+0.10 кг/нед", line)
        self.assertIn("по средним двух недель тоже", line)
        self.assertIn("−100 ккал", line)
        self.assertNotIn("паузу набора", line)

    def test_gaining_phase_adds_the_pause_option(self) -> None:
        result = self._matrix("lean_bulk", (0.1, 0.2), self._daily(78.0, 0.1, days=35))
        line = " ".join(result["lines"])
        self.assertIn("−75 ккал или паузу набора", line)

    def test_deviation_without_a_two_week_witness_is_not_acted_on(self) -> None:
        # Только десять дней данных: наклон валиден, средние с интервалом две недели — нет.
        weights = self._daily(79.0, 0.07, days=9)
        result = self._matrix("cut_recomp", (-0.1, 0.1), weights)
        line = " ".join(result["lines"])
        self.assertIn("выше коридора фазы", line)
        self.assertIn("ещё не подтверждают", line)
        self.assertNotIn("−100 ккал", line)

    def test_falling_weight_on_a_cut_offers_a_maintenance_week(self) -> None:
        result = self._matrix("cut_recomp", (-0.35, -0.25), self._daily(80.0, -0.1))
        line = " ".join(result["lines"])
        self.assertIn("ниже коридора фазы", line)
        self.assertIn("+150 ккал или неделю поддержки", line)

    def test_stalled_weight_on_a_gain_asks_for_more_food(self) -> None:
        result = self._matrix("lean_bulk", (0.1, 0.2), self._daily(80.0, 0.0, days=35))
        line = " ".join(result["lines"])
        self.assertIn("при плане набора", line)
        self.assertIn("+75 ккал", line)

    def test_in_corridor_says_so_for_any_phase(self) -> None:
        gain = " ".join(self._matrix("lean_bulk", (0.1, 0.2), self._daily(80.0, 0.02))["lines"])
        self.assertIn("в коридоре фазы (+0.10…+0.20 кг/нед", gain)
        cut = " ".join(
            self._matrix("cut_recomp", (-0.35, -0.25), self._daily(80.0, -0.04))["lines"]
        )
        self.assertIn("в коридоре фазы (-0.35…-0.25 кг/нед", cut)
        # Плоский месяц поддержания — двухнедельный «застой» в коридоре удержания:
        # задача этапа, а не отклонение.
        hold = " ".join(self._matrix("maintenance", (0.0, 0.0), self._daily(80.0, 0.0))["lines"])
        self.assertIn("задача этапа", hold)

    def test_maintenance_uses_its_own_corridor_not_the_phase_name(self) -> None:
        result = self._matrix("maintenance", (0.0, 0.0), self._daily(80.0, 0.06))
        line = " ".join(result["lines"])
        self.assertIn("выше коридора фазы (+0.00…+0.00 кг/нед", line)
        self.assertIn("−100 ккал", line)


class PositionTagTests(unittest.TestCase):
    """Позиция упражнения в сессии в сводке."""

    def test_recent_sessions_carry_the_place_in_the_session(self) -> None:
        workouts = [
            _workout("2026-08-12", [(9, [(60, 12)]), (18, [(50, 12)]), (8, [(100, 10)])]),
            _workout("2026-08-10", [(8, [(100, 10)]), (18, [(50, 12)])]),
        ]
        summary = next(
            s
            for s in coach_features.exercise_summaries(workouts, CATALOG, TODAY)
            if s["exercise_id"] == 18
        )
        self.assertEqual(
            summary["recent_sessions"],
            [("2026-08-10", "[#2/2] 50×12"), ("2026-08-12", "[#2/3] 50×12")],
        )
        text = prompt_builder.render_exercise_summaries([summary])
        self.assertIn("2026-08-12: [#2/3] 50×12", text)
