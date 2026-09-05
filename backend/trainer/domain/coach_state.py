#!/usr/bin/env python3
"""Машина фаз подготовки.

Изменяемое состояние коуча — текущая фаза, дата её старта, переопределения
параметров по фазам, лимиты талии — живёт в маленьком JSON рядом с базой,
``coach_state.json``, по тому же образцу, что и ``coach_profile.json``. Профиль
остаётся прозой (кто такой атлет), а этот файл — структурное состояние (что
программа делает прямо сейчас), и переключается он инструментами Coach MCP,
никогда автоматически.

Здесь только правила: дефолты, что из прочитанного файла считать валидным, как
переключается фаза, какая сейчас неделя блока, когда плановая разгрузка и
считается ли атлет вернувшимся после перерыва. Сам файл читает и пишет
``data/files``. Зовут ``prompt_builder``, ``recommender``, ``coach_signals``,
``coach_features``, ``plan_validator`` и Coach MCP.
"""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

PHASES = ("cut_recomp", "lean_bulk", "maintenance")

# Длина перерыва, которая и включает протокол возврата, и сбрасывает ramp
# объёма на старт нового блока.
BREAK_DAYS = 14

# Дефолты методики по фазам. Переопределяются пофазно через state["phase_params"]
# (например, своя цель веса); всё, что промпты говорят о фазе, генерируется из
# этой таблицы, так что правка здесь меняет поведение коуча.
PHASE_DEFAULTS: dict[str, dict[str, Any]] = {
    "cut_recomp": {
        "title": "лёгкий дефицит-рекомп",
        "calories": (2100, 2200),
        "rate_text": "−0.25…−0.35 кг/нед по недельной средней",
        "rate_kg_per_week": (-0.35, -0.25),
        "frequency_text": "3 тренировки в неделю (2–4 допустимо)",
        "sessions_per_week": 3,
        "session_sets": (14, 20),
        "ramp_start": (6, 8),
        "ramp_cap": (10, 14),
        "protein_g": (155, 165),
        # Ритм плановой разгрузки: N недель накопления, затем одна лёгкая.
        "deload_every_weeks": 6,
        # Достижение (по 7-дневной средней) — повод предложить переход в lean_bulk.
        "target_weight_kg": 75.5,
    },
    "lean_bulk": {
        "title": "lean bulk",
        "calories": (2400, 2500),
        "rate_text": "+0.5–0.8 кг/мес",
        # ≈ +0.1…+0.2 кг/нед: матрица питания и предусловия застоя ориентируются
        # на этот коридор, а не на имя фазы.
        "rate_kg_per_week": (0.1, 0.2),
        "frequency_text": "3 тренировки в неделю (2–4 допустимо)",
        "sessions_per_week": 3,
        "session_sets": (14, 20),
        "ramp_start": (6, 8),
        "ramp_cap": (10, 16),
        "protein_g": (155, 165),
        "deload_every_weeks": 6,
        # Достижение потолка — повод предложить мини-сушку или смену фазы.
        "ceiling_weight_kg": 84.0,
    },
    "maintenance": {
        "title": "поддержание",
        "calories": (2300, 2400),
        "rate_text": "±0 кг (вес держим)",
        # Коридор нулевой ширины: матрица сама добавляет допуск ±0.15.
        "rate_kg_per_week": (0.0, 0.0),
        "frequency_text": "1 тренировка в неделю, fullbody heavy",
        "sessions_per_week": 1,
        "session_sets": (8, 12),
        # Без ramp объёма: фиксированные 2–3 подхода на группу в неделю держат
        # силу; веса НЕ снижаются — интенсивность и есть сигнал удержания.
        "ramp_start": None,
        "ramp_cap": None,
        "sets_per_group": (2, 3),
        "protein_g": (150, 175),
    },
}

# МЕДИЦИНСКАЯ ГРАНИЦА (не удалять): слой коуча никогда не обрастает логикой
# дозировок, советами по схеме ГЗТ или трактовкой анализов — это территория
# лечащего врача, и промпты повторяют ту же границу. Гормональный контекст
# атлета живёт только в прозе профиля; планирование не подстраивается под
# цикл инъекций (супрафизиологический фон держится всю неделю, тайминг по дням
# умозрителен, а восстановление и история всегда важнее).

# Разумные границы лимита и базы талии, см: одни и те же при чтении файла и при
# правке через MCP.
WAIST_CM_RANGE = (40.0, 200.0)

DEFAULT_STATE: dict[str, Any] = {
    "schema": 1,
    "phase": "cut_recomp",
    "phase_started": None,  # ISO-дата; None → неделя блока считается первой
    "phase_params": {},  # переопределения по фазам: {фаза: {ключ: значение}}
    "phase_history": [],  # закрытые фазы: [{phase, started, ended}]
    "waist_limit_cm": None,  # жёсткий эстетический лимит; задаёт атлет
    "waist_base_cm": None,  # первый замер текущей фазы
}


def _valid_iso_date(value: Any) -> str | None:
    """ISO-дата как строка, если она разбирается, иначе ``None``."""
    if not isinstance(value, str):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def default_state() -> dict[str, Any]:
    """Состояние по умолчанию: с него начинается генерация, если файла нет."""
    return deepcopy(DEFAULT_STATE)


def normalize_state(raw: object) -> dict[str, Any]:
    """Прочитанный файл состояния поверх дефолтов: незнакомое и битое
    отбрасывается, чтобы генерация всегда работала."""
    state = default_state()
    if not isinstance(raw, dict):
        return state

    if raw.get("phase") in PHASES:
        state["phase"] = raw["phase"]
    if _valid_iso_date(raw.get("phase_started")):
        state["phase_started"] = raw["phase_started"]
    if isinstance(raw.get("phase_params"), dict):
        state["phase_params"] = raw["phase_params"]
    if isinstance(raw.get("phase_history"), list):
        state["phase_history"] = [
            {
                "phase": entry["phase"],
                "started": _valid_iso_date(entry.get("started")),
                "ended": _valid_iso_date(entry.get("ended")),
            }
            for entry in raw["phase_history"]
            if isinstance(entry, dict) and entry.get("phase") in PHASES
        ]
    for key in ("waist_limit_cm", "waist_base_cm"):
        value = raw.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and WAIST_CM_RANGE[0] <= value <= WAIST_CM_RANGE[1]
        ):
            state[key] = float(value)
    # Старые файлы могут нести injection_day — игнорируется: планирование больше
    # не подстраивается под цикл инъекций.
    return state


_OVERRIDABLE_PARAM_KEYS = {
    "calories",
    "rate_text",
    "rate_kg_per_week",
    "frequency_text",
    "session_sets",
    "ramp_start",
    "ramp_cap",
    "sets_per_group",
    "protein_g",
    "sessions_per_week",
    "target_weight_kg",
    "ceiling_weight_kg",
    "deload_every_weeks",
    "title",
    "group_targets",
}

# Единственный параметр не-скалярной формы: {группа: [min, max]}. Ключи должны
# быть ровно из coach_features.MUSCLE_GROUPS — опечатка в названии группы иначе
# молча не применилась бы, а экран объёма и промпт продолжили бы показывать
# дефолтный коридор.
_GROUP_TARGET_KEY = "group_targets"


def _is_finite_number(value: Any) -> bool:
    """Настоящее конечное число: ``int`` или ``float`` без ``bool``, ``nan`` и ``inf``."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _normalize_group_targets(value: Any) -> dict[str, tuple[float, float]]:
    """Цели по группам из переопределений фазы: ``{группа: (min, max)}``.

    Имя группы обязано быть из ``coach_features.MUSCLE_GROUPS``, пара — двумя
    числами; иначе ``ValueError`` с подсказкой. Импорт внутри функции: модули
    зависят друг от друга по кругу.
    """
    from trainer.domain import coach_features

    if not isinstance(value, dict):
        raise ValueError("group_targets должен быть объектом {группа: [min, max]}")
    clean: dict[str, tuple[float, float]] = {}
    for group, bounds in value.items():
        if group not in coach_features.MUSCLE_GROUPS:
            raise ValueError(
                f"Неизвестная группа {group!r}; допустимые: "
                f"{', '.join(coach_features.MUSCLE_GROUPS)}"
            )
        if not (
            isinstance(bounds, (list, tuple))
            and len(bounds) == 2
            and all(_is_finite_number(x) for x in bounds)
        ):
            raise ValueError(f"Цель группы {group!r} должна быть парой чисел [min, max]")
        clean[group] = (bounds[0], bounds[1])
    return clean


def _normalize_param_value(value: Any) -> Any:
    """Значение переопределения фазы: число, строка или пара чисел ``[min, max]``;
    всё остальное — ``ValueError``.
    """
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and all(_is_finite_number(item) for item in value):
            return (value[0], value[1])
        raise ValueError("Диапазон должен быть парой чисел [min, max]")
    if isinstance(value, bool) or value is None:
        raise ValueError("Параметр должен быть числом, строкой или парой чисел")
    if isinstance(value, (int, float)):
        if not _is_finite_number(value):
            raise ValueError("Параметр должен быть конечным числом")
        return value
    if isinstance(value, str):
        return value
    raise ValueError("Параметр должен быть числом, строкой или парой чисел")


def switch_phase(
    state: dict[str, Any],
    phase: str,
    params: dict[str, Any] | None = None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Переключить фазу руками: закрыть текущую в журнал, поставить новую и её
    переопределения. Никогда не автоматически: когда цель фазы достигнута, промпт
    просит модель лишь *предложить* смену в rationale, решает атлет. Чтение и
    запись файла — в ``data/files.set_phase``."""
    if phase not in PHASES:
        raise ValueError(f"Неизвестная фаза {phase!r}; допустимые: {', '.join(PHASES)}")
    clean: dict[str, Any] | None = None
    if params:
        if not isinstance(params, dict):
            raise ValueError("params должен быть объектом {ключ: значение}")
        clean = {}
        for key, value in params.items():
            if key not in _OVERRIDABLE_PARAM_KEYS:
                raise ValueError(
                    f"Неизвестный параметр {key!r}; допустимые: "
                    f"{', '.join(sorted(_OVERRIDABLE_PARAM_KEYS))}"
                )
            clean[key] = (
                _normalize_group_targets(value)
                if key == _GROUP_TARGET_KEY
                else _normalize_param_value(value)
            )

    today = today or date.today()
    # Уходящую фазу закрываем в журнал: инструмент итогов фазы считает все
    # числа по тренировкам и замерам за даты, так что журналу нужны только
    # границы.
    if state.get("phase_started"):
        history = list(state.get("phase_history") or [])
        history.append(
            {
                "phase": state["phase"],
                "started": state["phase_started"],
                "ended": today.isoformat(),
            }
        )
        state["phase_history"] = history
    state["phase"] = phase
    state["phase_started"] = today.isoformat()
    if clean is not None:
        phase_params = dict(state.get("phase_params") or {})
        phase_params[phase] = clean
        state["phase_params"] = phase_params
    return state


def update_waist_limits(
    state: dict[str, Any],
    *,
    waist_limit_cm: float | None = None,
    waist_base_cm: float | None = None,
) -> list[str]:
    """Жёсткий лимит талии и база фазы, в сантиметрах; непереданное не трогается.
    Возвращает, что изменилось, строками для ответа инструмента."""
    changed: list[str] = []
    low, high = WAIST_CM_RANGE
    for key, value in (("waist_limit_cm", waist_limit_cm), ("waist_base_cm", waist_base_cm)):
        if value is None:
            continue
        number = float(value)
        if not low <= number <= high:
            raise ValueError(f"{key}={number:g} вне разумного диапазона {low:g}–{high:g} см.")
        state[key] = number
        changed.append(f"{key}={number:g}")
    return changed


def phase_params(state: dict[str, Any]) -> dict[str, Any]:
    """Параметры текущей фазы: дефолты ``PHASE_DEFAULTS`` поверх переопределений
    атлета из ``state["phase_params"]``; в результат добавлен ключ ``phase``.
    Единственный способ узнать параметры фазы: зовут ``prompt_builder``,
    ``recommender``, ``coach_signals`` и Coach MCP.
    """
    phase = state.get("phase") if state.get("phase") in PHASES else "cut_recomp"
    merged = dict(PHASE_DEFAULTS[phase])
    overrides = (state.get("phase_params") or {}).get(phase) or {}
    for key, value in overrides.items():
        if key not in _OVERRIDABLE_PARAM_KEYS:
            continue
        if key == _GROUP_TARGET_KEY:
            merged[key] = {group: tuple(bounds) for group, bounds in (value or {}).items()}
        else:
            merged[key] = tuple(value) if isinstance(value, list) else value
    merged["phase"] = phase
    return merged


# --------------------------------------------------------------------------- #
# Неделя отчёта
# --------------------------------------------------------------------------- #
def last_closed_week_end(today: date) -> date:
    """Воскресенье последней ЗАКРЫТОЙ календарной недели (пн–вс).

    Единственный источник правды о том, какую неделю описывает недельный отчёт:
    его зовут и генератор (``recommender.weekly_report_period`` для скрипта
    таймера), и чтение кэша в Coach MCP. Разъедутся — таймер запишет отчёт под
    одну дату, а инструмент будет искать под другую, промахнётся мимо кэша и
    молча сожжёт токены на перегенерацию.

    Неделя считается закрытой только когда она прошла целиком: в воскресенье
    отчёт всё ещё про предыдущую неделю, а про текущую появляется в ночь на
    понедельник. Отчёт по недожитой неделе — то, ради чего это и заведено:
    вечерняя тренировка воскресенья в него не попадала.
    """
    return today - timedelta(days=today.weekday() + 1)


# --------------------------------------------------------------------------- #
# Недели блока и ramp объёма
# --------------------------------------------------------------------------- #
def _workout_dates(workouts: list[dict[str, Any]]) -> list[date]:
    """Даты тренировок без дублей, по возрастанию; битые даты пропускаются."""
    dates: set[date] = set()
    for workout in workouts:
        try:
            dates.add(date.fromisoformat(str(workout.get("workout_date", ""))))
        except ValueError:
            continue
    return sorted(dates)


def is_return_from_break(workouts: list[dict[str, Any]], today: date) -> bool:
    """Вычисляется, никогда не хранится: атлет возвращается после ≥14 дней без
    тренировок (``BREAK_DAYS``). Зовут ``prompt_builder``, ``plan_validator``,
    ``recommender``, ``coach_features`` и Coach MCP.
    """
    dates = _workout_dates(workouts)
    if not dates:
        return False
    return (today - dates[-1]).days >= BREAK_DAYS


def _block_anchor(
    state: dict[str, Any], workouts: list[dict[str, Any]], today: date
) -> date | None:
    """Якорь блока: более поздняя из даты старта фазы и первой тренировки после
    последнего перерыва ≥14 дней (долгий перерыв сбрасывает ramp, как и правило
    возврата). ``None``, если якоря нет или он в будущем.
    """
    anchor: date | None = None
    started = state.get("phase_started")
    if isinstance(started, str):
        try:
            anchor = date.fromisoformat(started)
        except ValueError:
            anchor = None

    dates = [d for d in _workout_dates(workouts) if d <= today]
    if dates:
        ramp_anchor = dates[0]
        for previous, current in pairwise(dates):
            if (current - previous).days >= BREAK_DAYS:
                ramp_anchor = current
        anchor = max(anchor, ramp_anchor) if anchor else ramp_anchor
    if anchor is None or anchor > today:
        return None
    return anchor


def block_week(state: dict[str, Any], workouts: list[dict[str, Any]], today: date) -> int:
    """Неделя тренировочного блока, с единицы. Пока атлет НА перерыве, ближайшая
    сессия открывает новый блок — неделя 1.
    """
    if is_return_from_break(workouts, today):
        return 1
    anchor = _block_anchor(state, workouts, today)
    if anchor is None:
        return 1
    return (today - anchor).days // 7 + 1


# Чтобы плановая разгрузка имела смысл, усталость должна реально накопиться:
# в среднем ≥2 сессии на неделю накопления, иначе лёгкие недели уже случились
# сами собой, и флаг не ставится.
DELOAD_MIN_SESSIONS_PER_WEEK = 2


def cycle_position(
    state: dict[str, Any], workouts: list[dict[str, Any]], today: date
) -> dict[str, Any]:
    """Где неделя блока стоит внутри цикла «накопление → разгрузка».

    Строительные фазы идут ``deload_every_weeks`` недель накопления и одну
    плановую лёгкую неделю (−30–40% объёма); после неё ramp начинается заново,
    так что длина цикла N+1, а ``cycle_week`` — неделя блока по модулю. Флаг
    разгрузки ставится, только если атлет реально натренировал блок
    (``DELOAD_MIN_SESSIONS_PER_WEEK`` в среднем). Зовут ``prompt_builder``,
    ``recommender``, ``coach_signals`` и Coach MCP.
    """
    week = block_week(state, workouts, today)
    params = phase_params(state)
    every = params.get("deload_every_weeks")
    if not every or not params.get("ramp_start"):
        return {
            "block_week": week,
            "cycle_week": week,
            "deload_week": False,
            "sessions_in_cycle": None,
        }

    cycle_length = int(every) + 1
    cycle_week = (week - 1) % cycle_length + 1
    anchor = _block_anchor(state, workouts, today)
    sessions_in_cycle = 0
    if anchor is not None:
        cycle_start = anchor + timedelta(days=(week - cycle_week) * 7)
        sessions_in_cycle = sum(
            1 for when in _workout_dates(workouts) if cycle_start <= when <= today
        )
    deload = (
        cycle_week == cycle_length
        and not is_return_from_break(workouts, today)
        and sessions_in_cycle >= DELOAD_MIN_SESSIONS_PER_WEEK * int(every)
    )
    return {
        "block_week": week,
        "cycle_week": cycle_week,
        "deload_week": deload,
        "sessions_in_cycle": sessions_in_cycle,
    }


def weekly_volume_target(state: dict[str, Any], week: int) -> tuple[int, int] | None:
    """Целевой коридор рабочих подходов на крупную группу для недели блока.

    Строительные фазы разгоняются от ``ramp_start`` на +1 (низ) / +2 (верх) подхода
    в неделю до потолка фазы. У поддержания ramp нет (фиксированные 2–3 подхода на
    группу) — ``None``.
    """
    params = phase_params(state)
    start, cap = params.get("ramp_start"), params.get("ramp_cap")
    if not start or not cap:
        return None
    week = max(1, int(week))
    low = min(start[0] + (week - 1), cap[0])
    high = min(start[1] + 2 * (week - 1), cap[1])
    return int(low), int(high)
