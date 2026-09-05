"""Валидатор плана: ровно три жёсткие границы (покрытие групп, возвратный потолок,
размер сессии); всё остальное — суждение модели, и тесты держат эту свободу.
"""

from __future__ import annotations

import unittest

import support  # noqa: F401 — кладёт backend в sys.path

from trainer.domain import plan_validator, prompt_builder

CATALOG = [
    {"id": 8, "name": "Жим ногами"},
    {"id": 9, "name": "Тяга верт."},
    {"id": 1, "name": "Жим гор."},
]


class SemanticValidatorTests(unittest.TestCase):
    """Валидатор держит ровно три жёсткие границы (возвратный потолок, покрытие
    групп, потолок сессии). Всё, что проверял старый валидатор — полосы весов,
    коридоры сессии, волны повторов, rest_days, порядок нагрузок, — теперь
    тренерское суждение модели, и эти тесты закрепляют свободу, чтобы она молча
    не отросла обратно.
    """

    CATALOG = [
        {"id": 8, "name": "Жим ногами"},
        {"id": 9, "name": "Тяга верт."},
        {"id": 4, "name": "Гравитрон"},
    ]

    def _history(self, when: str = "2026-06-10", load_type: str = "medium"):
        # Покрывает все группы правила покрытия (грудь/спина/квадрицепс/бицепс
        # бедра), чтобы тесты ниже проверяли ровно то правило, о котором они.
        """История, покрывающая все группы правила покрытия."""
        return [
            {
                "workout_date": when,
                "data": {
                    "load_type": load_type,
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

    def _rec(
        self,
        weight: float = 100.0,
        sets: int = 14,
        load_type: str = "medium",
        rest_days: int = 1,
        reps: int = 10,
        focus: str = "f",
        rationale: str = "r",
    ):
        """Сырой ответ модели: жим ногами и тяга на ``sets`` подходов."""
        first = sets - sets // 2
        return {
            "focus": focus,
            "load_type": load_type,
            "rest_days": rest_days,
            "rationale": rationale,
            "exercises": [
                {
                    "exercise_id": 8,
                    "name": "Жим ногами",
                    "note": "n",
                    "sets": [{"reps": reps, "weight": weight}] * first,
                },
                {
                    "exercise_id": 9,
                    "name": "Тяга верт.",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 60}] * (sets // 2),
                },
            ],
        }

    def _violations(self, rec, workouts=None, today=None):
        """Нарушения жёстких границ для ответа на данной истории."""
        from datetime import date as _date

        return plan_validator._semantic_violations(
            rec,
            self.CATALOG,
            workouts if workouts is not None else self._history(),
            today or _date(2026, 6, 12),
        )

    def test_clean_plan_has_no_violations(self) -> None:
        rec = plan_validator._validate(self._rec(weight=105, sets=14), self.CATALOG)
        self.assertEqual(self._violations(rec), [])

    def test_weight_jumps_are_the_models_call(self) -> None:
        # +40% к недавнему диапазону раньше отвергалось; теперь это суждение
        # модели (дефолты прогрессии несёт промпт).
        rec = plan_validator._validate(self._rec(weight=140, sets=14), self.CATALOG)
        self.assertEqual(self._violations(rec), [])
        low = plan_validator._validate(self._rec(weight=60, sets=14), self.CATALOG)
        self.assertEqual(self._violations(low), [])

    def test_session_size_is_capped_by_the_phase_but_never_padded(self) -> None:
        from datetime import date as _date

        tiny = plan_validator._validate(self._rec(sets=5), self.CATALOG)
        big = plan_validator._validate(self._rec(sets=24), self.CATALOG)
        # Без потолка (нет параметров фазы) размер — решение модели.
        self.assertEqual(self._violations(tiny), [])
        self.assertEqual(self._violations(big), [])
        # С потолком фазы держится только ВЕРХНЯЯ граница: короткая сессия может
        # быть решением, раздутая — нет.
        capped = lambda rec: plan_validator._semantic_violations(  # noqa: E731
            rec, self.CATALOG, self._history(), _date(2026, 6, 12), session_cap=20
        )
        self.assertEqual(capped(tiny), [])
        violations = capped(big)
        self.assertEqual(len(violations), 1)
        self.assertIn("24 рабочих подходов при потолке сессии 20", violations[0])
        self.assertIn("начиная с изоляции", violations[0])

    def test_rep_ranges_and_load_sequencing_are_the_models_call(self) -> None:
        pump_heavy = plan_validator._validate(
            self._rec(sets=14, load_type="heavy", reps=14), self.CATALOG
        )
        violations = self._violations(pump_heavy, workouts=self._history(load_type="heavy"))
        self.assertEqual(violations, [])

    def test_rest_days_are_clamped_not_flagged(self) -> None:
        rec = plan_validator._validate(self._rec(sets=14, rest_days=6), self.CATALOG)
        self.assertEqual(rec["rest_days"], plan_validator.MAX_REST_DAYS)
        self.assertEqual(self._violations(rec), [])


class CoverageAndDeloadValidatorTests(unittest.TestCase):
    """Покрытие сухих групп и то, что разгрузка больше не ограничивает план."""

    CATALOG = [
        {"id": 8, "name": "Жим ногами"},
        {"id": 9, "name": "Тяга верт."},
        {"id": 18, "name": "Жим в тренажере"},
        {"id": 15, "name": "Сгибания ног"},
    ]

    def _fullbody(self, when: str, sets_each: int = 2):
        """Fullbody-сессия на дату по ``sets_each`` подходов."""
        return {
            "workout_date": when,
            "data": {
                "load_type": "medium",
                "exercises": [
                    {
                        "exercise_id": eid,
                        "name": f"#{eid}",
                        "sets": [{"reps": 10, "weight": 60}] * sets_each,
                    }
                    for eid in (8, 9, 18, 15)
                ],
            },
        }

    def _plan(self, ids_sets: list[tuple[int, int]], rationale: str = "r"):
        """План из пар (id, сетов)."""
        names = {item["id"]: item["name"] for item in self.CATALOG}
        return {
            "focus": "f",
            "load_type": "medium",
            "rest_days": 1,
            "rationale": rationale,
            "exercises": [
                {
                    "exercise_id": eid,
                    "name": names[eid],
                    "note": "n",
                    "sets": [{"reps": 10, "weight": 60}] * count,
                }
                for eid, count in ids_sets
            ],
        }

    def test_dry_group_missing_from_plan_is_flagged(self) -> None:
        from datetime import date as _date

        # Бицепс бедра (id 15) тренировали 12 дней назад → сухой; план его пропускает.
        workouts = [
            self._fullbody("2026-06-10", sets_each=2),
            {
                "workout_date": "2026-05-31",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 15,
                            "name": "Сгибания ног",
                            "sets": [{"reps": 10, "weight": 60}] * 2,
                        },
                    ],
                },
            },
        ]
        workouts[0]["data"]["exercises"] = [
            ex for ex in workouts[0]["data"]["exercises"] if ex["exercise_id"] != 15
        ]
        raw = self._plan([(8, 5), (9, 5), (18, 4)])
        rec = plan_validator._validate(raw, self.CATALOG)
        violations = plan_validator._semantic_violations(
            rec, self.CATALOG, workouts, _date(2026, 6, 12)
        )
        self.assertTrue(any("бицепс бедра" in v for v in violations))

        covered = self._plan([(8, 4), (9, 4), (18, 4), (15, 2)])
        rec = plan_validator._validate(covered, self.CATALOG)
        violations = plan_validator._semantic_violations(
            rec, self.CATALOG, workouts, _date(2026, 6, 12)
        )
        self.assertEqual(violations, [])

    def test_planned_deload_week_no_longer_constrains_the_plan(self) -> None:
        from datetime import date as _date
        from datetime import timedelta as _timedelta

        from trainer.domain import coach_state

        start = _date(2026, 5, 1)
        state = dict(coach_state.DEFAULT_STATE, phase_started=start.isoformat())
        workouts = [
            self._fullbody((start + _timedelta(days=index * 3)).isoformat()) for index in range(15)
        ]
        today = start + _timedelta(days=42)  # неделя блока 7 → плановая разгрузка
        # Флаг по-прежнему доходит до промпта через блок контекста…
        self.assertTrue(coach_state.cycle_position(state, workouts, today)["deload_week"])

        # …но валидатор больше не следит за объёмом и весами разгрузки: как
        # строить лёгкую неделю — решение модели.
        heavy_volume = self._plan([(8, 5), (9, 5), (18, 4), (15, 2)])  # 16 сетов
        rec = plan_validator._validate(heavy_volume, self.CATALOG)
        violations = plan_validator._semantic_violations(rec, self.CATALOG, workouts, today)
        self.assertEqual(violations, [])


class ReturnLadderIsDataOnlyTests(unittest.TestCase):
    """Лестница возврата раньше была границей валидации (первая ступень плюс шаг).
    Теперь это только данные: на возврате единственный жёсткий потолок для
    КАЖДОГО движения — доперерывный рабочий вес; лестница в промпте ведёт
    следующие сессии.
    """

    CATALOG = [
        {"id": 8, "name": "Жим ногами"},
        {"id": 9, "name": "Тяга верт."},
        {"id": 18, "name": "Жим в тренажере"},
        {"id": 15, "name": "Сгибания ног"},
    ]

    def _history(self):
        # Перерыв 40 дней; пик жима ногами 120, последний рабочий 80 → лестница
        # (90 → 100 → 110 → 120) существует как данные промпта, но сама
        # возвратная сессия ограничена доперерывными 80.
        """Перерыв 40 дней; пик жима ногами 120, последний рабочий 80."""
        return [
            {
                "workout_date": "2026-05-03",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 12, "weight": 80}] * 3,
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
            },
            {
                "workout_date": "2026-04-15",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 12, "weight": 120}] * 3,
                        },
                    ],
                },
            },
            {
                "workout_date": "2026-04-08",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 12, "weight": 110}] * 3,
                        },
                    ],
                },
            },
            {
                "workout_date": "2026-04-01",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 12, "weight": 100}] * 3,
                        },
                    ],
                },
            },
        ]

    def _plan(self, leg_press_weight: float):
        """Возвратный план с заданным весом жима ногами."""
        return {
            "focus": "возврат",
            "load_type": "medium",
            "rest_days": 0,
            "rationale": "r",
            "exercises": [
                {
                    "exercise_id": 8,
                    "name": "Жим ногами",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": leg_press_weight}] * 4,
                },
                {
                    "exercise_id": 9,
                    "name": "Тяга верт.",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 47.5}] * 4,
                },
                {
                    "exercise_id": 18,
                    "name": "Жим в тренажере",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 40}] * 2,
                },
                {
                    "exercise_id": 15,
                    "name": "Сгибания ног",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 22.5}] * 2,
                },
            ],
        }

    def _violations(self, plan):
        """Нарушения после санитизации плана."""
        from datetime import date as _date

        rec = plan_validator._validate(plan, self.CATALOG)
        return plan_validator._semantic_violations(
            rec, self.CATALOG, self._history(), _date(2026, 6, 12)
        )

    def test_ladder_rungs_above_the_pre_break_weight_are_rejected(self) -> None:
        violations = self._violations(self._plan(leg_press_weight=90))
        self.assertTrue(any("не место для прибавки" in v for v in violations))
        violations = self._violations(self._plan(leg_press_weight=120))
        self.assertTrue(any("не место для прибавки" in v for v in violations))

    def test_pre_break_weight_and_below_pass(self) -> None:
        self.assertEqual(self._violations(self._plan(leg_press_weight=80)), [])
        self.assertEqual(self._violations(self._plan(leg_press_weight=60)), [])


class ReturnCeilingTests(unittest.TestCase):
    """После перерыва КАЖДОЕ упражнение ограничено доперерывным рабочим весом — и те,
    что атлет оставил на пике.
    """

    CATALOG = [
        {"id": 8, "name": "Жим ногами"},
        {"id": 9, "name": "Тяга верт."},
        {"id": 18, "name": "Жим в тренажере"},
        {"id": 15, "name": "Сгибания ног"},
    ]

    def _history(self, last: str = "2026-05-22"):
        # Две одинаковые сессии на тех же весах: ничего ниже пика, поэтому
        # comeback_ramp_steps не даёт лестницы ни одному движению.
        """Две одинаковые сессии на одних весах: ничего ниже пика, лестницы нет."""
        return [
            {
                "workout_date": last,
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 12, "weight": 100}] * 3,
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
            },
            {
                "workout_date": "2026-05-19",
                "data": {
                    "load_type": "medium",
                    "exercises": [
                        {
                            "exercise_id": 8,
                            "name": "Жим ногами",
                            "sets": [{"reps": 12, "weight": 100}] * 3,
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
            },
        ]

    def _plan(self, leg_press: float):
        """Возвратный план с заданным жимом ногами."""
        return {
            "focus": "возврат",
            "load_type": "medium",
            "rest_days": 0,
            "rationale": "r",
            "exercises": [
                {
                    "exercise_id": 8,
                    "name": "Жим ногами",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": leg_press}] * 4,
                },
                {
                    "exercise_id": 9,
                    "name": "Тяга верт.",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 50}] * 4,
                },
                {
                    "exercise_id": 18,
                    "name": "Жим в тренажере",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 42.5}] * 2,
                },
                {
                    "exercise_id": 15,
                    "name": "Сгибания ног",
                    "note": "n",
                    "sets": [{"reps": 12, "weight": 25}] * 2,
                },
            ],
        }

    def _violations(self, plan, today):
        """Нарушения плана на дату ``today``."""
        rec = plan_validator._validate(plan, self.CATALOG)
        return plan_validator._semantic_violations(rec, self.CATALOG, self._history(), today)

    def test_progression_after_a_break_is_rejected(self) -> None:
        from datetime import date as _date

        # 21 день без зала, а модель всё равно прибавляет к доперерывным 100.
        violations = self._violations(self._plan(leg_press=105), _date(2026, 6, 12))
        self.assertTrue(any("не место для прибавки" in v for v in violations))

    def test_below_pre_break_weight_passes(self) -> None:
        from datetime import date as _date

        # Насколько ниже — решение тренера; сервер блокирует только плюс.
        self.assertEqual(self._violations(self._plan(leg_press=85), _date(2026, 6, 12)), [])
        self.assertEqual(self._violations(self._plan(leg_press=100), _date(2026, 6, 12)), [])

    def test_no_ceilings_outside_a_break(self) -> None:
        from datetime import date as _date

        # Тренировался 3 дня назад: обычные правила прогрессии, без возвратного ограничителя.
        rec = plan_validator._validate(self._plan(leg_press=105), self.CATALOG)
        violations = plan_validator._semantic_violations(
            rec, self.CATALOG, self._history(last="2026-06-09"), _date(2026, 6, 12)
        )
        self.assertFalse(any("не место для прибавки" in v for v in violations))

    def test_prompt_states_facts_without_prescribing_numbers(self) -> None:
        from trainer.domain import coach_features

        text = prompt_builder.render_pre_break_weights(
            coach_features.pre_break_working_weights(self._history(), self.CATALOG), 21
        )
        assert text is not None
        self.assertIn("21 дн.", text)
        self.assertIn("Жим ногами: 100", text)
        # Сервер называет данные и оставляет суждение модели: ни процентов, ни
        # лекции по физиологии, зашитой в алгоритмический слой.
        self.assertIn("решай сам", text)
        self.assertNotIn("%", text)
        self.assertNotIn("связк", text)
