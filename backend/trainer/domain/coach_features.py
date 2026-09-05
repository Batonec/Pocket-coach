#!/usr/bin/env python3
"""Вычисляемые фичи истории для промпта тренера.

Раньше модель получала 20 сырых тренировок и на каждом вызове заново выводила
рекорды, застой и объёмы, цепляясь за то, какими случайно оказались последние
сессии. Этот модуль считает те же факты на сервере, чтобы промпт кормил модель
*данными*, а не домашним заданием:

- сводки по упражнениям за всё время (лучший подход, e1RM по Эпли, последний
  ПР, последние сессии с позицией движения в каждой);
- детектор застоя с явными предусловиями «ресурс исчерпан», измеренными по
  АКТИВНОМУ окну текущего блока (никогда через отпуск);
- явка по календарным неделям (гейт программы и переключатель сплита);
- ступени возврата после перерыва (от текущего к ДОПЕРЕРЫВНОМУ рабочему, не к пику);
- недельный объём по группам мышц в прямых И эффективных сетах (косвенная нагрузка);
- тренды веса и талии и матрица решений по питанию, привязанная к коридору
  темпа веса фазы.

Здесь только числа и структуры; строки для модели из них делает
``prompt_builder`` (``render_*``). Зовут ``prompt_builder``, ``plan_validator``,
``coach_signals``, ``rules`` и Coach MCP. Только stdlib, как весь backend.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

from trainer.domain import coach_state, limits
from trainer.domain.coach_state import BREAK_DAYS

# Id 1 («Жим гор.») и id 18 («Жим в тренажере») в каталоге — один тренажёр; старые
# строки истории всё ещё несут id 1, поэтому все потребители идут через алиас.
EXERCISE_ALIASES: dict[int, int] = {1: 18}

# Подтягивания с помощью: поле веса — это ПРОТИВОВЕС (помощь), так что прогресс
# это вес ВНИЗ, и каждое сравнение ниже инвертировано. Тренажёр вышел из каталога
# в августе 2026 (в зале атлета его нет, история им не пользовалась), но
# поддержка инвертированного прогресса остаётся: это единственное место, умеющее
# читать колонку веса «меньше значит лучше», и любая будущая машина с противовесом
# включается в него без переписывания.
GRAVITRON_ID = 4

# Базовые движения, которым после перерыва строится явная лестница возврата;
# изоляция просто следует правилу рабочего веса.
MAIN_MOVEMENT_IDS = (18, 9, 10, 8, GRAVITRON_ID)

# Основная группа мышц по (каноническому) id упражнения.
MUSCLE_GROUPS: dict[str, tuple[int, ...]] = {
    "грудь": (18, 17),
    "спина": (9, GRAVITRON_ID, 10),
    "дельты": (13,),
    "задняя дельта": (19,),
    "бицепс": (11,),
    "трицепс": (12,),
    "квадрицепс/ягодичные": (8, 16),
    "бицепс бедра": (15,),
}

# Эффективные сеты в неделю: прямая работа считается за 1.0 для основной группы,
# а базовые движения дают вторичным мышцам долю сета (жимы → трицепс ~половина;
# любая тяга → бицепс ~половина). Жим засчитывает «дельтам» только 0.25:
# горизонтальный жим грузит ПЕРЕДНЮЮ дельту, а единственный прямой тренажёр
# группы меряет среднюю — полсета завысили бы покрытие видимой головки. Доля
# ягодичных у жима ногами сложена в общую группу квадрицепс/ягодичные, там он
# остаётся 1.0. Горизонтальная тяга даёт «задней дельте» 0.25: тяга грузит заднюю
# головку, но не настолько, чтобы заменить прямую работу.
EFFECTIVE_SETS: dict[int, dict[str, float]] = {
    18: {"грудь": 1.0, "трицепс": 0.5, "дельты": 0.25},
    17: {"грудь": 1.0},
    9: {"спина": 1.0, "бицепс": 0.5},
    GRAVITRON_ID: {"спина": 1.0, "бицепс": 0.5},
    10: {"спина": 1.0, "бицепс": 0.5, "задняя дельта": 0.25},
    13: {"дельты": 1.0},
    19: {"задняя дельта": 1.0},
    11: {"бицепс": 1.0},
    12: {"трицепс": 1.0},
    8: {"квадрицепс/ягодичные": 1.0},
    16: {"квадрицепс/ягодичные": 1.0},
    15: {"бицепс бедра": 1.0},
}

BIG_GROUPS = ("грудь", "спина", "квадрицепс/ягодичные")

# Недельные ориентиры прямых сетов для малых групп (политика из системного
# промпта); крупные группы вместо этого идут по ramp недели блока.
SMALL_GROUP_TARGETS: dict[str, tuple[int, int]] = {
    "дельты": (6, 12),
    "задняя дельта": (4, 8),
    "бицепс": (4, 8),
    "трицепс": (4, 8),
    "бицепс бедра": (5, 10),
}


def group_volume_targets(
    week_target: tuple[int, int] | None,
    maintenance_sets: tuple[int, int] | None = None,
    group_targets: dict[str, Any] | None = None,
) -> dict[str, tuple[int, int]]:
    """Целевые сеты в неделю по группам для экрана объёма в клиенте.

    Без переопределений: крупные группы идут по коридору текущей недели блока
    (ramp или разгрузка), малые держат свои ориентиры из политики, поддержание
    сплющивает всё в 2–3. Один коридор на все крупные группы по умолчанию именно
    потому, что у дефолтной методики нет приоритетов между ними.

    ``group_targets`` из параметров фазы атлета переопределяет это для названных
    групп: программу, где спине 16 сетов, а квадрицепсу 9, одним коридором не
    выразить. Переопределение описывает ЗРЕЛЫЙ блок; разгон текущей недели идёт из
    недели блока и текста программы, правило масштабирования здесь не выдумывается.
    Зовут ``prompt_builder`` и ``recommender._coach_context``.
    """
    override = {
        group: (bounds[0], bounds[1])
        for group, bounds in (group_targets or {}).items()
        if group in MUSCLE_GROUPS and isinstance(bounds, (list, tuple)) and len(bounds) == 2
    }
    targets: dict[str, tuple[int, int]] = {}
    for group in MUSCLE_GROUPS:
        if group in override:
            targets[group] = override[group]
        elif maintenance_sets:
            targets[group] = (maintenance_sets[0], maintenance_sets[1])
        elif group in BIG_GROUPS:
            targets[group] = (week_target[0], week_target[1]) if week_target else (10, 16)
        else:
            targets[group] = SMALL_GROUP_TARGETS[group]
    return targets


# Общее правило свежести для веса и талии: устаревшие данные → без советов по калориям.
STALE_MEASUREMENT_DAYS = 14

# Калибровка детектора застоя (предусловия «ресурс исчерпан», раздел 4.2).
# Всё меряется по АКТИВНОМУ окну: не больше 6 недель назад, но никогда через
# старт текущего блока (начало фазы или возврат после перерыва ≥14 дней). Окно
# с отпуском внутри показывает частоту, которой у атлета не было, и объём,
# который он не тренировал, — и модель строит fullbody-сессии «по дефициту»,
# которого нет.
STALL_WINDOW_DAYS = 42  # 6 недель
STALL_MIN_WEEKLY_FREQUENCY = 2.5  # сессий в неделю по активному окну
STALL_MIN_WEEKLY_SETS = 10.0  # на КРУПНУЮ группу, если фаза не задала цель по группам
STALL_MIN_WINDOW_DAYS = 21  # меньше трёх недель — судить ещё не о чем
STALL_NO_PR_DAYS = 28  # ≥4 недели без улучшения ВНУТРИ окна
STALL_MIN_EXERCISE_SESSIONS = 3  # не застоится движение, которое почти не делают
# Допуск темпа веса вокруг коридора фазы (кг/нед), общий для предусловий и
# матрицы питания.
RATE_TOLERANCE = 0.15
# Сколько дней после недели поддержки матрица ещё молчит: ровно окно её
# подтверждения — разница 7-дневных средних с интервалом две недели — иначе
# «вторым свидетелем» отклонения оказалась бы сама неделя поддержки.
SUPPORT_WEEK_SETTLE_DAYS = 14

_EFFORT_MARK = {"easy": "-", "ok": "", "hard": "+"}


def canonical_exercise_id(exercise_id: Any) -> int | None:
    """Id упражнения как ``int`` с переводом дубля (1 → 18); мусор — ``None``."""
    if isinstance(exercise_id, bool):
        return None
    if isinstance(exercise_id, float) and (
        not math.isfinite(exercise_id) or not exercise_id.is_integer()
    ):
        return None
    try:
        parsed = int(exercise_id)
    except (TypeError, ValueError, OverflowError):
        return None
    return EXERCISE_ALIASES.get(parsed, parsed)


def epley_e1rm(weight: float, reps: int) -> float:
    """Оценка одноповторного максимума по Эпли: вес × (1 + повторы / 30)."""
    return weight * (1 + reps / 30)


def _workout_date(workout: dict[str, Any]) -> date | None:
    """Дата тренировки из строки ISO или ``None``, если её не разобрать."""
    try:
        return date.fromisoformat(str(workout.get("workout_date", "")))
    except ValueError:
        return None


def _iter_exercise_sessions(
    workouts: list[dict[str, Any]],
) -> dict[int, list[tuple[date, list[dict[str, Any]]]]]:
    """``{канонический id: [(дата, подходы), ...]}`` от старых к новым; подходы с
    повторами < 1 или битыми числами пропущены.
    """
    sessions: dict[int, list[tuple[date, list[dict[str, Any]]]]] = {}
    for workout in workouts:
        when = _workout_date(workout)
        if when is None:
            continue
        for exercise in (workout.get("data", {}) or {}).get("exercises", []) or []:
            exercise_id = canonical_exercise_id(exercise.get("exercise_id"))
            if exercise_id is None:
                continue
            sets: list[dict[str, Any]] = []
            for workout_set in exercise.get("sets", []) or []:
                if not isinstance(workout_set, dict):
                    continue
                raw_reps = workout_set.get("reps", 0)
                if isinstance(raw_reps, bool) or isinstance(workout_set.get("weight"), bool):
                    continue
                if isinstance(raw_reps, float) and not raw_reps.is_integer():
                    continue
                try:
                    reps = int(raw_reps)
                    weight = float(workout_set.get("weight", 0))
                except (TypeError, ValueError, OverflowError):
                    continue
                if reps < 1 or not math.isfinite(weight):
                    continue
                sets.append(
                    {
                        "reps": reps,
                        "weight": weight,
                        "effort": workout_set.get("effort"),
                        "rir": workout_set.get("rir"),
                    }
                )
            if sets:
                sessions.setdefault(exercise_id, []).append((when, sets))
    for exercise_id in sessions:
        sessions[exercise_id].sort(key=lambda item: item[0])
    return sessions


def _session_top(sets: list[dict[str, Any]], *, inverted: bool) -> dict[str, Any]:
    """Подход, определяющий сессию: лучший e1RM, а для гравитрона — наименьший
    противовес (при равенстве больше повторов).
    """
    if inverted:
        return min(sets, key=lambda s: (s["weight"], -s["reps"]))
    return max(sets, key=lambda s: epley_e1rm(s["weight"], s["reps"]))


def _beats(top: dict[str, Any], best: dict[str, Any], *, inverted: bool) -> bool:
    """Лучше ли ``top``, чем ``best``: выше e1RM, а для гравитрона ниже противовес
    (при равенстве больше повторов)? Одно сравнение для сводки, пика и часов застоя
    внутри окна — они не должны разъезжаться.
    """
    if inverted:
        return top["weight"] < best["weight"] or (
            top["weight"] == best["weight"] and top["reps"] > best["reps"]
        )
    return epley_e1rm(top["weight"], top["reps"]) > epley_e1rm(best["weight"], best["reps"]) + 1e-9


def _exercise_positions(workouts: list[dict[str, Any]]) -> dict[tuple[date, int], tuple[int, int]]:
    """``{(дата, канонический id): (позиция, упражнений в сессии)}``.

    Рабочий вес зависит от того, ГДЕ стояло движение: тяга первой на свежих ногах
    и та же тяга шестой после жима ногами — два разных числа, и модель не должна
    читать второе как потерю силы.
    """
    positions: dict[tuple[date, int], tuple[int, int]] = {}
    for workout in workouts:
        when = _workout_date(workout)
        if when is None:
            continue
        exercises = (workout.get("data", {}) or {}).get("exercises", []) or []
        total = len(exercises)
        for index, exercise in enumerate(exercises, start=1):
            exercise_id = canonical_exercise_id(exercise.get("exercise_id"))
            if exercise_id is not None:
                positions[(when, exercise_id)] = (index, total)
    return positions


# Пороги фильтра аномальных подходов для текущего рабочего веса: вес, который
# встретился один раз и стоит так далеко от недавней медианы, — шум логирования;
# подход на <6 повторов считается, только если тот же вес повторился рядом.
_OUTLIER_MEDIAN_RATIO = 0.25
_MIN_WORKING_REPS = 6
# «Последние 2–3 сессии» обязаны быть соседями по времени: сессия дальше этого
# от самой свежей принадлежит прошлой эпохе (до перерыва) и ничего не говорит
# о ТЕКУЩИХ весах атлета.
_WORKING_WINDOW_DAYS = 14


def current_working_weight(
    sessions: list[tuple[date, list[dict[str, Any]]]], *, inverted: bool
) -> float | None:
    """НАСТОЯЩИЙ текущий рабочий вес атлета: максимум (минимум противовеса для
    гравитрона) рабочих подходов за последние 2–3 сессии, лежащие в двух неделях от
    самой свежей, с тем же фильтром аномалий, что у правил прогрессии.

    Одиночный подход, выпадающий из стабильной серии (случайный «20×3» среди
    10×12, лёгкий технический день), не должен ни определять «сейчас», ни задавать
    старт ступеней возврата, а лёгкий день рядом с нормальной сессией не должен
    читаться как откат. Зовут сводки, ступени возврата и доперерывные веса.
    """
    if not sessions:
        return None
    last_when = sessions[-1][0]
    recent = [
        (when, sets)
        for when, sets in sessions[-3:]
        if (last_when - when).days <= _WORKING_WINDOW_DAYS
    ]
    if not recent:
        return None
    all_sets = [workout_set for _, sets in recent for workout_set in sets]
    if not all_sets:
        return None

    def occurrences(pool: list[dict[str, Any]], weight: float) -> int:
        """Сколько подходов в ``pool`` сделано с этим весом (допуск 0.01 кг)."""
        return sum(1 for s in pool if abs(s["weight"] - weight) < 0.01)

    # Порядок фильтров важен: СНАЧАЛА выбрасываем малоповторный мусор, чтобы
    # случайный «20×3» в сессии из двух подходов не перетянул медиану на себя
    # и не выкинул настоящую рабочую десятку как «выброс».
    plausible = [
        s
        for s in all_sets
        if s["reps"] >= _MIN_WORKING_REPS or occurrences(all_sets, s["weight"]) >= 2
    ]
    if not plausible:
        plausible = all_sets
    weights = sorted(s["weight"] for s in plausible)
    median = weights[len(weights) // 2]

    working = []
    for workout_set in plausible:
        weight = workout_set["weight"]
        if (
            occurrences(plausible, weight) == 1
            and median > 0
            and abs(weight - median) > _OUTLIER_MEDIAN_RATIO * median
        ):
            continue
        working.append(weight)
    if not working:
        working = [s["weight"] for s in plausible]
    return min(working) if inverted else max(working)


def _format_set(workout_set: dict[str, Any]) -> str:
    """«80×10+@2»: вес×повторы, метка тяжести (-/+) и RIR, если есть."""
    mark = _EFFORT_MARK.get(workout_set.get("effort") or "", "")
    rir = workout_set.get("rir")
    rir_repr = f"@{int(rir)}" if isinstance(rir, (int, float)) and not isinstance(rir, bool) else ""
    return f"{workout_set['weight']:g}×{workout_set['reps']}{mark}{rir_repr}"


def _position_tag(position: tuple[int, int] | None) -> str:
    """«[#3/7] »: движение было третьим из семи в той сессии."""
    if position is None:
        return ""
    index, total = position
    return f"[#{index}/{total}] "


# --------------------------------------------------------------------------- #
# Сводки по упражнениям (4.1)
# --------------------------------------------------------------------------- #
def exercise_summaries(
    workouts: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    today: date,
) -> list[dict[str, Any]]:
    """Сводка по каждому упражнению, у которого есть хотя бы две сессии.

    В словаре: лучший подход с датой и e1RM, дата последнего ПР и дней с него,
    даты всех улучшений, текущий рабочий вес с датой и процентом от пика, три
    последние сессии с позицией в тренировке. Порядок: базовые движения первыми,
    дальше по числу сессий. Зовут ``prompt_builder`` (план, отчёт) и
    ``phase_summary``.
    """
    names = {item["id"]: item["name"] for item in catalog}
    sessions_by_exercise = _iter_exercise_sessions(workouts)
    positions = _exercise_positions(workouts)

    summaries: list[dict[str, Any]] = []
    for exercise_id, sessions in sessions_by_exercise.items():
        if len(sessions) < 2:
            continue
        inverted = exercise_id == GRAVITRON_ID

        best: dict[str, Any] | None = None
        best_when: date | None = None
        last_pr: date | None = None
        pr_dates: list[str] = []  # только улучшения, без базовой сессии
        for when, sets in sessions:
            top = _session_top(sets, inverted=inverted)
            if best is None:
                best, best_when, last_pr = top, when, when
                continue
            if _beats(top, best, inverted=inverted):
                best, best_when, last_pr = top, when, when
                pr_dates.append(when.isoformat())
        if best is None or best_when is None or last_pr is None:
            continue

        current_when, _current_sets = sessions[-1]
        # «Сейчас» — рабочий вес последних 2–3 сессий после фильтра аномалий:
        # случайный лёгкий или мусорный подход не должен читаться как откат.
        current_weight = current_working_weight(sessions, inverted=inverted)
        peak_weight = best["weight"]
        if current_weight is None:
            pct = None
        elif inverted:
            pct = round(peak_weight / current_weight * 100) if current_weight > 0 else None
        else:
            pct = round(current_weight / peak_weight * 100) if peak_weight > 0 else None

        summaries.append(
            {
                "exercise_id": exercise_id,
                "name": names.get(exercise_id, f"#{exercise_id}"),
                "inverted": inverted,
                "sessions_total": len(sessions),
                "top_weight": best["weight"],
                "top_reps": best["reps"],
                "top_date": best_when.isoformat(),
                "e1rm": None if inverted else round(epley_e1rm(best["weight"], best["reps"]), 1),
                "last_pr_date": last_pr.isoformat(),
                "days_since_pr": (today - last_pr).days,
                "pr_dates": pr_dates,
                "current_weight": current_weight,
                "current_date": current_when.isoformat(),
                "pct_of_peak": pct,
                "recent_sessions": [
                    (
                        when.isoformat(),
                        _position_tag(positions.get((when, exercise_id)))
                        + ", ".join(_format_set(s) for s in sets),
                    )
                    for when, sets in sessions[-3:]
                ],
            }
        )

    order = {exercise_id: index for index, exercise_id in enumerate(MAIN_MOVEMENT_IDS)}
    summaries.sort(key=lambda s: (order.get(s["exercise_id"], 99), -s["sessions_total"]))
    return summaries


# --------------------------------------------------------------------------- #
# Недельный объём в прямых и эффективных сетах (4.4)
# --------------------------------------------------------------------------- #
def weekly_volume(
    workouts: list[dict[str, Any]], today: date, days: int = 7
) -> dict[str, dict[str, float]]:
    """Объём за последние ``days`` дней по группам: ``{группа: {"direct": прямые
    сеты, "effective": эффективные с долями косвенной нагрузки}}``. Зовут
    ``prompt_builder``, ``stall_report`` и ``plan_validator`` (покрытие групп).
    """
    volume = {group: {"direct": 0, "effective": 0.0} for group in MUSCLE_GROUPS}
    group_of: dict[int, str] = {}
    for group, ids in MUSCLE_GROUPS.items():
        for exercise_id in ids:
            group_of[exercise_id] = group

    for workout in workouts:
        when = _workout_date(workout)
        if when is None or when > today or (today - when).days > days - 1:
            continue
        for exercise in (workout.get("data", {}) or {}).get("exercises", []) or []:
            exercise_id = canonical_exercise_id(exercise.get("exercise_id"))
            if exercise_id is None:
                continue
            set_count = len(exercise.get("sets", []) or [])
            if not set_count:
                continue
            primary = group_of.get(exercise_id)
            if primary is not None:
                volume[primary]["direct"] += set_count
            for group, weight in (EFFECTIVE_SETS.get(exercise_id) or {}).items():
                volume[group]["effective"] += set_count * weight
    return volume


def sets_in_window(workouts: list[dict[str, Any]], today: date, days: int = 7) -> int:
    """Рабочих подходов за последние ``days`` дней до ``today`` включительно —
    итог недели одним числом: разгон в программе задан суммами (44 → 55 → 64 →
    79), а не группами, и «объём против цели» без суммы не сходится. Считает те
    же подходы, что ``weekly_volume`` раскладывает по группам. Зовёт
    ``prompt_builder`` (отчёт: период и неделя перед ним).
    """
    total = 0
    for workout in workouts:
        when = _workout_date(workout)
        if when is None or when > today or (today - when).days > days - 1:
            continue
        for exercise in (workout.get("data", {}) or {}).get("exercises", []) or []:
            if canonical_exercise_id(exercise.get("exercise_id")) is None:
                continue
            total += len(exercise.get("sets", []) or [])
    return total


# --------------------------------------------------------------------------- #
# Детектор застоя (4.2)
# --------------------------------------------------------------------------- #
def _rate_bounds(rate_range: Any, phase: str) -> tuple[float, float]:
    """Недельный коридор веса фазы парой чисел. Вызывающий без явного диапазона
    получает дефолты этой фазы — коридор есть у каждой, поэтому ни одна ветка ниже
    не привязана к ИМЕНИ фазы.
    """
    if isinstance(rate_range, (list, tuple)) and len(rate_range) == 2:
        return float(rate_range[0]), float(rate_range[1])
    fallback = coach_state.PHASE_DEFAULTS.get(phase, {}).get("rate_kg_per_week") or (0.0, 0.0)
    return float(fallback[0]), float(fallback[1])


def _in_window_progress(
    sessions: list[tuple[date, list[dict[str, Any]]]],
    window_start: date,
    today: date,
    *,
    inverted: bool,
) -> tuple[int, int] | None:
    """``(сессий внутри окна, дней с последнего улучшения в нём)``.

    Дата ПР за всё время — неверные часы застоя после перерыва: атлет законно
    сидит ниже пика, поставленного месяцы назад, и возвращается сессия за сессией,
    а «ПР 110 дн. назад» помечало бы каждое движение в день, когда предусловия
    позеленели. Поэтому прогресс измеряется ВНУТРИ активного окна: первая сессия
    там — база, каждая следующая, побившая лучшее на тот момент, — улучшение, и
    часы идут от последнего.
    """
    inside = [(when, sets) for when, sets in sessions if window_start <= when <= today]
    if not inside:
        return None
    best: dict[str, Any] | None = None
    last_improvement = inside[0][0]
    for when, sets in inside:
        top = _session_top(sets, inverted=inverted)
        if best is not None and _beats(top, best, inverted=inverted):
            last_improvement = when
        if best is None or _beats(top, best, inverted=inverted):
            best = top
    return len(inside), (today - last_improvement).days


def stall_report(
    workouts: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    trend: float | None,
    phase: str,
    rate_range: tuple[float, float] | None,
    today: date,
    *,
    since: date | None = None,
    group_targets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Сначала предусловия: плато считается «ресурс исчерпан», только если атлет
    реально достаточно тренировался, ел и спал объём. При красных предусловиях
    флаг НАМЕРЕННО не ставится: модель обязана объяснять плато явкой и питанием, а
    не «потолком».

    ``since`` — старт текущего блока (начало фазы или первая сессия после перерыва
    ≥14 дней): окно через него не переходит, и отпуск не разбавляет частоту. Пороги
    объёма — нижние границы целей по группам самой фазы, если она их называет: цель
    квадрицепса 8–10 нельзя судить по плоским 10. Возвращает факты окна, причины и
    список застоявшихся упражнений. Зовёт ``prompt_builder._render_stall``.
    """
    window_start = today - timedelta(days=STALL_WINDOW_DAYS - 1)
    if since is not None and window_start < since <= today:
        window_start = since
    window_days = (today - window_start).days + 1
    weeks = window_days / 7

    window_dates = {
        when
        for when in (_workout_date(w) for w in workouts)
        if when is not None and window_start <= when <= today
    }
    frequency = len(window_dates) / weeks

    volume = weekly_volume(workouts, today, days=window_days)
    volume_per_week: dict[str, tuple[float, float]] = {}
    for group in BIG_GROUPS:
        threshold = STALL_MIN_WEEKLY_SETS
        target = (group_targets or {}).get(group)
        if isinstance(target, (list, tuple)) and len(target) == 2:
            threshold = float(target[0])
        volume_per_week[group] = (volume[group]["direct"] / weeks, threshold)

    reasons: list[str] = []
    if frequency < STALL_MIN_WEEKLY_FREQUENCY:
        reasons.append(f"частота {frequency:.1f}/нед (нужно ≥{STALL_MIN_WEEKLY_FREQUENCY:g})")
    low_groups = [
        f"{group} {value:.1f} < {threshold:g}"
        for group, (value, threshold) in volume_per_week.items()
        if value < threshold
    ]
    if low_groups:
        reasons.append("объём/нед ниже порога: " + ", ".join(low_groups))

    if trend is None:
        reasons.append("нет свежего тренда веса")
    else:
        low, high = _rate_bounds(rate_range, phase)
        corridor = f"{low:+.2f}…{high:+.2f}"
        if low > 0.0:
            # Набор: только ПАДАЮЩИЙ вес говорит, что профицита нет.
            if trend < low - RATE_TOLERANCE:
                reasons.append(f"вес падает ({trend:+.2f} кг/нед при коридоре {corridor})")
        elif not (low - RATE_TOLERANCE <= trend <= high + RATE_TOLERANCE):
            # Срез или удержание: вес обязан оставаться в своём коридоре.
            reasons.append(
                f"вес вне целевого темпа фазы ({trend:+.2f} кг/нед при коридоре {corridor})"
            )

    too_short = window_days < STALL_MIN_WINDOW_DAYS
    ok = not reasons and not too_short
    stalled: list[dict[str, Any]] = []
    if ok and window_days >= STALL_NO_PR_DAYS:
        sessions_by_exercise = _iter_exercise_sessions(workouts)
        for summary in summaries:
            progress = _in_window_progress(
                sessions_by_exercise.get(summary["exercise_id"]) or [],
                window_start,
                today,
                inverted=summary["inverted"],
            )
            if progress is None:
                continue
            count, quiet_days = progress
            if count >= STALL_MIN_EXERCISE_SESSIONS and quiet_days >= STALL_NO_PR_DAYS:
                stalled.append(
                    {
                        "exercise_id": summary["exercise_id"],
                        "name": summary["name"],
                        "quiet_days": quiet_days,
                    }
                )
    return {
        "window_start": window_start,
        "window_days": window_days,
        "frequency": frequency,
        "volume_per_week": volume_per_week,
        "too_short": too_short,
        "preconditions_ok": ok,
        "reasons": reasons,
        "stalled": stalled,
    }


# --------------------------------------------------------------------------- #
# Явка по календарным неделям (гейт программы и переключатель сплита)
# --------------------------------------------------------------------------- #
def weekly_attendance(
    workouts: list[dict[str, Any]], today: date, weeks: int = 4
) -> list[dict[str, Any]]:
    """Тренировочные дни по календарным неделям (пн–вс): последние ``weeks``
    закрытых недель плюс текущая, от старых к новым. Чистые календарные факты:
    события, объясняющие пустую неделю, остаются в хронике, ни одно число здесь их
    не читает. Зовёт ``prompt_builder._render_attendance``.
    """
    training_days = {when for when in (_workout_date(w) for w in workouts) if when is not None}
    monday = today - timedelta(days=today.weekday())
    rows: list[dict[str, Any]] = []
    for offset in range(weeks, -1, -1):
        start = monday - timedelta(days=7 * offset)
        end = start + timedelta(days=6)
        rows.append(
            {
                "start": start,
                "end": end,
                "sessions": sum(1 for when in training_days if start <= when <= end),
                "closed": offset > 0,
            }
        )
    return rows


def attendance_streak(rows: list[dict[str, Any]], minimum: int) -> int:
    """Подряд идущие ЗАКРЫТЫЕ недели, считая назад от последней закрытой, с не менее
    чем ``minimum`` сессиями в каждой. Текущая неделя не считается: среда не может
    провалить неделю, у которой ещё четыре дня впереди.
    """
    streak = 0
    for row in reversed(rows):
        if not row["closed"]:
            continue
        if row["sessions"] < minimum:
            break
        streak += 1
    return streak


# --------------------------------------------------------------------------- #
# Ступени возврата после перерыва (4.3)
# --------------------------------------------------------------------------- #
def _top_weight_diffs(sessions: list[tuple[date, list[dict[str, Any]]]]) -> list[float]:
    """Ненулевые изменения веса лучшего подхода между соседними сессиями."""
    tops = [_session_top(sets, inverted=False)["weight"] for _, sets in sessions]
    return [round(abs(b - a), 2) for a, b in pairwise(tops) if abs(b - a) > 0.01]


def _weight_granularity(sessions: list[tuple[date, list[dict[str, Any]]]]) -> float:
    """Шаг стека тренажёра: наименьшее изменение веса, которое на нём когда-либо
    делали. Ступени обязаны быть ему кратны: ступень 7.5 кг на тренажёре с блинами
    по 5 кг не собрать.
    """
    diffs = _top_weight_diffs(sessions)
    if not diffs:
        return 2.5
    return max(0.5, min(diffs))


# Правила лестницы. Ступень — шаг стека самого тренажёра, даже там, где он
# грубее программных «≤10%» (блин 10 кг на жиме ногами в 80 кг — настоящая
# ступень; расти повторами между ступенями — решение модели). Единственная
# пересборка: ОДИНОЧНАЯ ступень выше этого прыжка — не лестница, а артефакт
# истории (атлет однажды перескочил блины), и равные трети служат лучше. Ступеней
# больше потолка сжимаются в равные шаги: лестница — подсказка на ближайшие
# недели, а не сценарий по сессиям.
_RAMP_LONE_JUMP = 0.20
_RAMP_MAX_RUNGS = 6
# Лестница возврата — факт текущего блока: когда возврат старше этого, не
# возвращённый вес — обычная история, а не ступени.
RETURN_LADDER_DAYS = 56


def _round_half(value: float) -> float:
    """Округление до 0.5."""
    return round(value * 2) / 2


def _equal_steps(current: float, peak: float, count: int, granularity: float = 2.5) -> list[float]:
    """Запасная лестница: равные доли от текущего к пику (в обе стороны), округлённые
    до собираемых блинов, строго монотонные, с пиком в конце.
    """
    quantum = min(max(granularity, 0.5), 2.5)
    steps: list[float] = []
    for index in range(1, count + 1):
        raw = current + (peak - current) * index / count
        value = round(raw / quantum) * quantum
        if steps and abs(value - steps[-1]) < 0.01:
            continue
        steps.append(value)
    if not steps or abs(steps[-1] - peak) > 0.01:
        if steps and (steps[-1] > peak) != (current > peak):
            steps.pop()
        steps.append(peak)
    return steps


def _lone_jump(current: float, steps: list[float], inverted: bool) -> bool:
    """Единственная ступень, прыгающая больше ``_RAMP_LONE_JUMP`` от текущего веса."""
    if len(steps) != 1 or current <= 0:
        return False
    jump = (current - steps[0]) / current if inverted else (steps[0] - current) / current
    return jump > _RAMP_LONE_JUMP + 1e-9


def last_break(
    workouts: list[dict[str, Any]], min_days: int = BREAK_DAYS
) -> tuple[date, date] | None:
    """``(последняя сессия до, первая после)`` самого свежего разрыва не меньше
    ``min_days`` между двумя ЗАПИСАННЫМИ сессиями — перерыв, из которого атлет уже
    вернулся. Перерыв, который ещё идёт (сессии после нет), — территория
    ``coach_state.is_return_from_break``, здесь не сообщается.
    """
    dates = sorted({when for when in (_workout_date(w) for w in workouts) if when is not None})
    found: tuple[date, date] | None = None
    for previous, current in pairwise(dates):
        if (current - previous).days >= min_days:
            found = (previous, current)
    return found


def comeback_ramp_steps(
    workouts: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    today: date,
) -> list[dict[str, Any]]:
    """Для каждого базового движения, которое всё ещё ниже ДОПЕРЕРЫВНОГО рабочего:
    ступени от текущего к доперерывному, по одному шагу стека.

    Цель — то, что атлет реально поднимал перед паузой, а не пик за всё время: пик
    месяцы назад при другой частоте ничего не говорит о сегодняшнем запасе, а к
    пикам программа возвращает обычной прогрессией позже. Лестница существует,
    только пока есть что возвращать (движение на доперерывном весе ничего не
    печатает) и только для молодого возврата (``RETURN_LADDER_DAYS``). В сам день
    возврата блок пуст: доперерывные веса печатаются как ориентир, вход выбирает
    модель. Зовёт ``prompt_builder._build_user_prompt``.
    """
    names = {item["id"]: item["name"] for item in catalog}
    gap = last_break(workouts)
    if gap is None:
        return []
    before, after = gap
    newest = max(when for when in (_workout_date(w) for w in workouts) if when is not None)
    if (today - newest).days >= BREAK_DAYS or (today - after).days > RETURN_LADDER_DAYS:
        return []

    sessions_by_exercise = _iter_exercise_sessions(workouts)
    items: list[dict[str, Any]] = []
    for exercise_id in MAIN_MOVEMENT_IDS:
        sessions = sessions_by_exercise.get(exercise_id) or []
        pre = [session for session in sessions if session[0] <= before]
        post = [session for session in sessions if session[0] >= after]
        if not pre or not post:
            continue
        inverted = exercise_id == GRAVITRON_ID
        target = current_working_weight(pre, inverted=inverted)
        current = current_working_weight(post, inverted=inverted)
        if not target or not current or target <= 0 or current <= 0:
            continue
        remaining = (current - target) if inverted else (target - current)
        if remaining <= 0.01:
            continue

        # Ступень — шаг стека самого тренажёра.
        granularity = _weight_granularity(sessions)
        direction = -1 if inverted else 1
        steps: list[float] = []
        weight = current
        while len(steps) < 12:
            weight = _round_half(weight + direction * granularity)
            if (not inverted and weight >= target - 0.01) or (inverted and weight <= target + 0.01):
                break
            steps.append(weight)
        steps.append(target)
        if len(steps) > _RAMP_MAX_RUNGS:
            steps = _equal_steps(current, target, _RAMP_MAX_RUNGS, granularity)
        if _lone_jump(current, steps, inverted):
            steps = _equal_steps(current, target, 3, granularity)

        items.append(
            {
                "exercise_id": exercise_id,
                "name": names.get(exercise_id, f"#{exercise_id}"),
                "inverted": inverted,
                "target": target,
                "current": current,
                "steps": steps,
                "break_start": before,
                "break_end": after,
            }
        )
    return items


def pre_break_working_weights(
    workouts: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    *,
    until: date | None = None,
) -> list[dict[str, Any]]:
    """Рабочий вес каждого упражнения на последней сессии перед перерывом.

    Чистые данные без тренерского мнения: НАСКОЛЬКО ниже них стартовать возвратную
    сессию, решает модель (она видит длину паузы, профиль и контекст
    восстановления). Сервер лишь называет, что атлет реально поднимал, и через
    валидатор держит, что возвратная сессия не место для нового ПР. ``until``
    смотрит на историю по состоянию на тот день (последняя доперерывная сессия)
    для уже идущего возврата. Зовут ``prompt_builder`` и
    ``plan_validator.bounds_from_history``.
    """
    names = {item["id"]: item["name"] for item in catalog}
    items: list[dict[str, Any]] = []
    for exercise_id, all_sessions in _iter_exercise_sessions(workouts).items():
        sessions = (
            [session for session in all_sessions if session[0] <= until]
            if until is not None
            else all_sessions
        )
        if not sessions:
            continue
        inverted = exercise_id == GRAVITRON_ID
        current = current_working_weight(sessions, inverted=inverted)
        if not current or current <= 0:
            continue
        items.append(
            {
                "exercise_id": exercise_id,
                "name": names.get(exercise_id, f"#{exercise_id}"),
                "inverted": inverted,
                "last_working": current,
            }
        )
    order = {exercise_id: index for index, exercise_id in enumerate(MAIN_MOVEMENT_IDS)}
    items.sort(key=lambda item: order.get(item["exercise_id"], 99))
    return items


# --------------------------------------------------------------------------- #
# Тренды веса и талии и матрица решений по питанию (P3)
# --------------------------------------------------------------------------- #
def _measurement_points(
    entries: list[dict[str, Any]], value_key: str, low: float, high: float
) -> list[tuple[date, float]]:
    """``[(дата, значение)]`` из записей замеров от старых к новым; битые даты и
    значения вне правдоподобных границ пропущены.
    """
    points: list[tuple[date, float]] = []
    for entry in entries:  # от старых к новым
        try:
            when = date.fromisoformat(str(entry.get("entry_date", "")))
            value = float(entry.get(value_key, 0))
        except (TypeError, ValueError):
            continue
        if low <= value <= high:
            points.append((when, value))
    return points


def weight_points(body_weights: list[dict[str, Any]]) -> list[tuple[date, float]]:
    """Правдоподобные взвешивания как точки ``(дата, кг)``. Зовут промпт, сигналы и rules."""
    return _measurement_points(
        body_weights, "weight", limits.MIN_PLAUSIBLE_BODY_WEIGHT, limits.MAX_PLAUSIBLE_BODY_WEIGHT
    )


def waist_points(waists: list[dict[str, Any]]) -> list[tuple[date, float]]:
    """Правдоподобные замеры талии как точки ``(дата, см)``. Зовут промпт, сигналы и rules."""
    return _measurement_points(waists, "waist", limits.MIN_WAIST_CM, limits.MAX_WAIST_CM)


def measurement_overview(measurements: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Обхваты кроме талии по видам: последний замер, дней с него и предыдущий —
    факты для блока «Вес и талия» отчёта; периодичность протокола модель берёт
    из главы «Измерения» стратегии, сервер её не судит. Порядок — как в
    ``limits.MEASUREMENT_KINDS``; виды без замеров пропущены. Зовёт
    ``prompt_builder`` (отчёт).
    """
    by_kind: dict[str, list[tuple[date, float]]] = {}
    for entry in measurements:  # от старых к новым
        kind = str(entry.get("kind") or "")
        if kind not in limits.MEASUREMENT_KINDS:
            continue
        try:
            when = date.fromisoformat(str(entry.get("entry_date", "")))
            value = float(entry.get("value_cm", 0))
        except (TypeError, ValueError):
            continue
        if limits.MIN_CIRCUMFERENCE_CM <= value <= limits.MAX_CIRCUMFERENCE_CM:
            by_kind.setdefault(kind, []).append((when, value))

    rows: list[dict[str, Any]] = []
    for kind, label in limits.MEASUREMENT_KINDS.items():
        points = sorted(by_kind.get(kind, []))
        if not points:
            continue
        last_when, last_value = points[-1]
        previous = points[-2] if len(points) > 1 else None
        rows.append(
            {
                "kind": kind,
                "label": label,
                "last_date": last_when.isoformat(),
                "last_value": last_value,
                "days_since": (today - last_when).days,
                "previous_date": previous[0].isoformat() if previous else None,
                "previous_value": previous[1] if previous else None,
                "count": len(points),
            }
        )
    return rows


def moving_average(
    points: list[tuple[date, float]], on_day: date, window_days: int = 7
) -> float | None:
    """Среднее значений за ``window_days`` дней до ``on_day`` включительно или ``None``."""
    window = [value for when, value in points if 0 <= (on_day - when).days < window_days]
    if not window:
        return None
    return sum(window) / len(window)


# Валидность тренда: недельный темп, экстраполированный через дыру в замерах
# (отпуск), — мусор: матрица срезала бы калории по отпускной воде в первый же
# день фазы. Валидному тренду нужны точки внутри окна ~3 недели, без соседнего
# разрыва больше двух недель, и только замеры ТЕКУЩЕЙ фазы.
TREND_WINDOW_DAYS = 21
TREND_MAX_GAP_DAYS = 14
TREND_MIN_SPAN_DAYS = 5


def weight_trend_per_week(
    points: list[tuple[date, float]],
    today: date,
    since: date | None = None,
    *,
    window_days: int = TREND_WINDOW_DAYS,
    min_span_days: int = TREND_MIN_SPAN_DAYS,
) -> float | None:
    """Недельный темп по недавнему окну или ``None``, когда данные честно его не
    поддерживают: мало точек, дыра между замерами или точки по разные стороны
    границы фазы (передай ``since`` = старт фазы). Окно и минимальный разброс по
    умолчанию — матрицы питания; калибровка TDEE передаёт свои. Зовут
    ``nutrition_matrix``, ``tdee_estimate`` и ``coach_signals``.
    """
    window = [
        p for p in points if (today - p[0]).days <= window_days and (since is None or p[0] >= since)
    ]
    if len(window) < 2:
        return None
    for previous, current in pairwise(window):
        if (current[0] - previous[0]).days > TREND_MAX_GAP_DAYS:
            return None
    span_days = (window[-1][0] - window[0][0]).days
    if span_days < min_span_days:
        return None
    # МНК-наклон по ВСЕМ точкам окна, а не по двум крайним: при редких
    # взвешиваниях одно тяжёлое утро с любого края иначе задавало бы всю
    # неделю. С двумя точками это та же прямая.
    xs = [(when - window[0][0]).days for when, _ in window]
    ys = [value for _, value in window]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sxx
    return slope * 7


# Протокол атлета решает по 7-дневной средней; точек в неделю меньше этого
# делают среднюю подбрасыванием монеты, и промпт говорит об этом вслух.
WEEKLY_MEAN_MIN_POINTS = 4


def weigh_ins_in_window(points: list[tuple[date, float]], today: date, days: int = 7) -> int:
    """Сколько взвешиваний за последние ``days`` дней до ``today`` включительно."""
    return sum(1 for when, _ in points if 0 <= (today - when).days < days)


def _is_fresh(points: list[tuple[date, float]], today: date) -> bool:
    """Есть ли замер не старше ``STALE_MEASUREMENT_DAYS``."""
    return bool(points) and (today - points[-1][0]).days <= STALE_MEASUREMENT_DAYS


def nutrition_matrix(
    state: dict[str, Any],
    params: dict[str, Any],
    body_weights: list[dict[str, Any]],
    waists: list[dict[str, Any]],
    today: date,
) -> dict[str, Any]:
    """Серверная ветка матрицы «тренд веса × тренд талии». Модель получает
    выбранную ветку как данные и формулирует совет; тренды ей заново выводить не
    надо. Калорийные ветки срабатывают ТОЛЬКО по валидному тренду внутри фазы (см.
    ``weight_trend_per_week``): невалидный даёт явную ветку «недостаточно данных»,
    а не число, экстраполированное через дыру отпуска.

    Недели поддержки из состояния (``coach_state.support_week_bounds``) в тренд и
    средние не входят; пока идёт сама неделя и ``SUPPORT_WEEK_SETTLE_DAYS`` после
    неё, ветка одна — «калории не трогай».

    Возвращает ``{"lines": строки для промпта, "goal": цель фазы, если достигнута,
    "trend_per_week": тренд}``. Зовёт ``prompt_builder`` (план и отчёт).
    """
    phase = params.get("phase", "cut_recomp")
    phase_start: date | None = None
    started_raw = state.get("phase_started")
    if isinstance(started_raw, str):
        try:
            phase_start = date.fromisoformat(started_raw)
        except ValueError:
            phase_start = None

    # В матрицу идут только замеры ТЕКУЩЕЙ фазы: смена фазы сбрасывает базу
    # (замер в день старта считается).
    in_phase = [
        p for p in weight_points(body_weights) if phase_start is None or p[0] >= phase_start
    ]
    waist = [p for p in waist_points(waists) if phase_start is None or p[0] >= phase_start]

    lines: list[str] = []
    goal: str | None = None

    # Свежесть — по всем замерам фазы; тренд и средние — без недель поддержки:
    # вес на них стоит или растёт по плану, и точка оттуда в окне читалась бы
    # как плато или обвал темпа.
    fresh_weight = _is_fresh(in_phase, today)
    fresh_waist = _is_fresh(waist, today)
    weights = [p for p in in_phase if not coach_state.is_support_week(state, p[0])]
    support = coach_state.support_week_bounds(state, today)
    settling = coach_state.days_since_support_week(state, today)
    if not fresh_weight:
        lines.append(
            "замер веса устарел (>14 дней) или отсутствует — советы по калориям не давай, "
            "попроси взвеситься"
        )
    if not fresh_waist:
        lines.append(
            "замера талии за последние 14 дней нет — попроси замерить (утром натощак, по пупку)"
        )

    trend = weight_trend_per_week(weights, today, since=phase_start)

    # Вес: текущая 7-дневная средняя против средней две недели назад → «стоит / движется».
    ma_now = moving_average(weights, today)
    ma_before = moving_average(weights, today - timedelta(days=14))
    stalled_2w = (
        trend is not None
        and ma_now is not None
        and ma_before is not None
        and abs(ma_now - ma_before) < 0.25
    )

    # Талия: направление решают два последних замера ВНУТРИ ФАЗЫ (порог шума
    # 0.3 см) — и только если они достаточно близко, чтобы их сравнивать.
    waist_pair_valid = len(waist) >= 2 and (waist[-1][0] - waist[-2][0]).days <= TREND_MAX_GAP_DAYS
    waist_delta = waist[-1][1] - waist[-2][1] if waist_pair_valid else None
    waist_down = waist_delta is not None and waist_delta <= -0.3
    waist_base = state.get("waist_base_cm")
    waist_limit = state.get("waist_limit_cm")
    last_waist = waist[-1][1] if waist else None

    # Ветки привязаны к недельному КОРИДОРУ веса фазы, никогда к её имени:
    # «Ф0 · возврат» — это cut_recomp, который просит ДЕРЖАТЬ вес, и матрица,
    # читающая имя, срезала бы калории за правильное поведение.
    rate_low, rate_high = _rate_bounds(params.get("rate_kg_per_week"), phase)
    holding = rate_low <= 0.0 <= rate_high
    cutting = rate_high < 0.0
    gaining = rate_low > 0.0
    corridor = f"{rate_low:+.2f}…{rate_high:+.2f} кг/нед, допуск ±{RATE_TOLERANCE:.2f}"

    if fresh_weight:
        trend_missing_line = (
            "данных для тренда веса недостаточно (замеры редкие, с разрывом или "
            "из прошлой фазы) — попроси сделать ещё 1–2 замера в ближайшие дни, "
            "калории пока не корректируй"
        )
        # Цели фазы: достигнута → модель ПРЕДЛАГАЕТ смену, решает атлет.
        target = params.get("target_weight_kg")
        if cutting and target and ma_now is not None and ma_now <= float(target):
            goal = (
                f"цель фазы достигнута (средний вес {ma_now:.1f} ≤ {target:g} кг) — "
                "предложи в rationale переход в lean_bulk; фазу переключает атлет"
            )
        ceiling = params.get("ceiling_weight_kg")
        if gaining and ceiling and ma_now is not None and ma_now >= float(ceiling):
            goal = (
                f"потолок набора достигнут (средний вес {ma_now:.1f} ≥ {ceiling:g} кг) — "
                "предложи мини-кат/смену фазы; фазу переключает атлет"
            )

        # На наборе первой говорит талия: жёсткий лимит или ползущая талия
        # решают калории независимо от того, что показывают весы.
        weight_line_due = True
        if gaining and fresh_waist:
            if waist_limit and last_waist is not None and last_waist >= float(waist_limit):
                goal = (
                    f"талия {last_waist:g} см у жёсткого лимита {waist_limit:g} см — "
                    "жёсткий сигнал: предложи мини-кат или смену фазы; решает атлет"
                )
                weight_line_due = False
            elif (
                waist_pair_valid
                and waist_base
                and waist[-1][1] >= float(waist_base) + 1
                and waist[-2][1] >= float(waist_base) + 1
            ):
                lines.append(
                    f"талия +1 см от базовой ({waist_base:g} см) два замера подряд — "
                    "посоветуй −100–150 ккал или паузу набора"
                )
                weight_line_due = False

        # Неделя поддержки (стратегия §3/§7): пока она идёт и пока окно
        # подтверждения матрицы (две недели) захватывает её, советов по
        # калориям нет — остановка или рост веса тут запланированы.
        if weight_line_due and support is not None:
            lines.append(
                f"НЕДЕЛЯ ПОДДЕРЖКИ по плану ({support[0].isoformat()} – "
                f"{support[1].isoformat()}): калории на уровне TDEE, вес стоит или растёт "
                "запланированно — это не плато, калории не трогай; тренд и окно коррекции "
                "считаются после неё"
            )
            weight_line_due = False
        elif weight_line_due and settling is not None and settling <= SUPPORT_WEEK_SETTLE_DAYS:
            lines.append(
                f"неделя поддержки закончилась {settling} дн. назад — окно коррекции "
                f"набирается заново (нужно {SUPPORT_WEEK_SETTLE_DAYS} дн. без неё), калории "
                "не трогай"
            )
            weight_line_due = False

        if weight_line_due:
            if trend is None:
                lines.append(trend_missing_line)
            elif stalled_2w and holding:
                lines.append("вес стоит ≥2 недель — это и есть задача этапа, калории НЕ трогай")
            elif stalled_2w and cutting and fresh_waist and waist_down:
                lines.append(
                    "вес стоит ≥2 недель, но талия идёт вниз — это рекомп-бонус, калории НЕ снижай"
                )
            elif stalled_2w and cutting:
                lines.append("вес стоит ≥2 недель — посоветуй −100–150 ккал")
            elif stalled_2w:
                lines.append("вес стоит ≥2 недель при плане набора — посоветуй +100–150 ккал")
            else:
                lines.append(
                    _rate_line(
                        trend,
                        rate_low,
                        rate_high,
                        corridor,
                        ma_now,
                        ma_before,
                        cutting=cutting,
                        gaining=gaining,
                    )
                )

    return {"lines": lines, "goal": goal, "trend_per_week": trend}


def _rate_line(
    trend: float,
    rate_low: float,
    rate_high: float,
    corridor: str,
    ma_now: float | None,
    ma_before: float | None,
    *,
    cutting: bool,
    gaining: bool,
) -> str:
    """Ветка тренда веса относительно коридора фазы.

    Смена калорий требует отклонения, подтверждённого двумя независимыми
    показаниями — наклоном за 3 недели И разницей 7-дневных средних с интервалом в
    две недели, — это и значит «две недели подряд» без хранения сервером истории
    своих вердиктов. Без подтверждения строка называет отклонение и просит
    взвешиваться ежедневно вместо числа.
    """
    if rate_low - RATE_TOLERANCE <= trend <= rate_high + RATE_TOLERANCE:
        return f"тренд веса {trend:+.2f} кг/нед — в коридоре фазы ({corridor}), калории не трогай"
    above = trend > rate_high + RATE_TOLERANCE
    two_week = (ma_now - ma_before) / 2 if ma_now is not None and ma_before is not None else None
    confirmed = two_week is not None and (
        two_week > rate_high + RATE_TOLERANCE if above else two_week < rate_low - RATE_TOLERANCE
    )
    side = "выше" if above else "ниже"
    if not confirmed:
        return (
            f"тренд веса {trend:+.2f} кг/нед {side} коридора фазы ({corridor}), но средние "
            "двух недель этого ещё не подтверждают — калории не трогай, взвешиваться ежедневно"
        )
    if above:
        advice = "−100–150 ккал" + (" или паузу набора" if gaining else "")
    else:
        advice = "+100–150 ккал" + (" или неделю поддержки" if cutting else "")
    return (
        f"тренд веса {trend:+.2f} кг/нед {side} коридора фазы ({corridor}) и по средним "
        f"двух недель тоже — посоветуй {advice}"
    )


# --------------------------------------------------------------------------- #
# Калибровка TDEE (стратегия §7: темп веса за 4 недели при известной еде)
# --------------------------------------------------------------------------- #
# Первые две недели фазы — вода и гликоген, в окно не входят; оценке нужен
# разброс точек не меньше трёх недель, иначе одно тяжёлое утро задаёт
# результат. Только фазы с ненулевым коридором темпа: на удержании и на
# возврате (Ф0) вес растёт на гликогене, и формула объявила бы расход выше на
# пустом месте — стратегия прямо говорит «не в Ф0».
TDEE_WINDOW_DAYS = 28
TDEE_SKIP_DAYS = 14
TDEE_MIN_SPAN_DAYS = 21
KCAL_PER_KG = 7700.0


def _calorie_anchor(value: Any) -> float | None:
    """Ориентир калорий фазы одним числом: середина коридора или само число."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (float(value[0]) + float(value[1])) / 2
        except (TypeError, ValueError):
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def tdee_estimate(
    params: dict[str, Any],
    points: list[tuple[date, float]],
    today: date,
    *,
    phase_start: date | None,
) -> dict[str, Any] | None:
    """Оценка расхода: ориентир калорий фазы минус дневной баланс по темпу веса
    за ``TDEE_WINDOW_DAYS``. ``None``, когда оценке нельзя верить: фаза держит
    вес, старта фазы нет, окно моложе трёх недель или тренд невалиден. Верна,
    только если атлет ел по ориентиру — факт калорий приложение не хранит, и
    промпт говорит это вслух. Зовёт ``prompt_builder`` (отчёт).
    """
    rate_low, rate_high = _rate_bounds(
        params.get("rate_kg_per_week"), params.get("phase", "cut_recomp")
    )
    if rate_low <= 0.0 <= rate_high or phase_start is None:
        return None
    intake = _calorie_anchor(params.get("calories"))
    if intake is None:
        return None
    trend = weight_trend_per_week(
        points,
        today,
        since=phase_start + timedelta(days=TDEE_SKIP_DAYS),
        window_days=TDEE_WINDOW_DAYS,
        min_span_days=TDEE_MIN_SPAN_DAYS,
    )
    if trend is None:
        return None
    tdee = intake - trend * KCAL_PER_KG / 7
    return {
        "intake": intake,
        "trend_per_week": round(trend, 2),
        "tdee": int(round(tdee / 10) * 10),
        "window_days": TDEE_WINDOW_DAYS,
    }


# --------------------------------------------------------------------------- #
# Итоги фазы (что фаза подготовки реально дала)
# --------------------------------------------------------------------------- #
def phase_summary(
    workouts: list[dict[str, Any]],
    body_weights: list[dict[str, Any]],
    waists: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    *,
    phase: str,
    started: date,
    ended: date,
) -> dict[str, Any]:
    """Итоги фазы за ``[started, ended]``: тренировки и частота, вес и талия от
    начала к концу, ПР за фазу, дисциплина. Всё выводится из истории по датам,
    поэтому прошлые фазы можно подвести в любой момент — журнал фаз хранит только
    границы. Зовёт Coach MCP (``coach_phase_summary``).
    """
    days = max(1, (ended - started).days + 1)
    weeks = days / 7

    session_dates = sorted(
        {
            when
            for when in (_workout_date(w) for w in workouts)
            if when is not None and started <= when <= ended
        }
    )

    weights = weight_points(body_weights)
    weight_start = moving_average(weights, started) or next(
        (value for when, value in weights if when >= started), None
    )
    weight_end = moving_average(weights, ended) or next(
        (value for when, value in reversed(weights) if when <= ended), None
    )
    weight_rate = (
        (weight_end - weight_start) / weeks
        if weight_start is not None and weight_end is not None and weeks >= 1
        else None
    )

    waist = [(when, value) for when, value in waist_points(waists) if started <= when <= ended]
    waist_start = waist[0][1] if waist else None
    waist_end = waist[-1][1] if waist else None

    summaries = exercise_summaries(workouts, catalog, ended)
    prs: list[dict[str, Any]] = []
    for summary in summaries:
        in_phase = [pr for pr in summary["pr_dates"] if started <= date.fromisoformat(pr) <= ended]
        if in_phase:
            prs.append(
                {
                    "name": summary["name"],
                    "count": len(in_phase),
                    "top_weight": summary["top_weight"],
                    "top_reps": summary["top_reps"],
                    "inverted": summary["inverted"],
                }
            )
    prs.sort(key=lambda item: -item["count"])

    return {
        "phase": phase,
        "started": started.isoformat(),
        "ended": ended.isoformat(),
        "days": days,
        "weeks": round(weeks, 1),
        "workouts": len(session_dates),
        "per_week": round(len(session_dates) / weeks, 1) if weeks else None,
        "weight_start": weight_start,
        "weight_end": weight_end,
        "weight_rate_per_week": round(weight_rate, 2) if weight_rate is not None else None,
        "waist_start": waist_start,
        "waist_end": waist_end,
        "pr_events": sum(item["count"] for item in prs),
        "prs": prs,
        "adherence": adherence_stats(workouts, ended, days=days),
    }


# --------------------------------------------------------------------------- #
# Дисциплина «факт против плана» (сводка за 30 дней)
# --------------------------------------------------------------------------- #
def adherence_stats(
    workouts: list[dict[str, Any]], today: date, days: int = 30
) -> dict[str, Any] | None:
    """Сводка «факт против плана» за скользящее окно: процент выполненных плановых
    подходов (выполненные ограничены планом, лишние не поднимают выше 100%), сколько
    сессий шли по плану и какие упражнения пропускаются целиком. Коуч использует
    это, чтобы делать планы реалистичными, а не читать нотации. Зовут
    ``prompt_builder`` и ``phase_summary``.
    """
    return adherence_between(workouts, today - timedelta(days=days - 1), today)


def adherence_between(
    workouts: list[dict[str, Any]], start: date, end: date
) -> dict[str, Any] | None:
    """Сводка «факт против плана» за явный отрезок ``[start, end]``: её зовут
    30-дневное окно дисциплины, итоги фазы и сигнал week_done. ``None``, если ни
    одна тренировка отрезка не шла по плану.
    """
    days = max(1, (end - start).days + 1)
    planned_total = 0
    done_total = 0
    sessions = 0
    skipped: Counter[str] = Counter()

    for workout in workouts:
        when = _workout_date(workout)
        if when is None or when < start or when > end:
            continue
        data = workout.get("data", {}) or {}
        snapshot = data.get("recommendation")
        if not isinstance(snapshot, dict):
            continue
        planned = {}
        for exercise in snapshot.get("exercises", []) or []:
            if not isinstance(exercise, dict):
                continue
            canonical = canonical_exercise_id(exercise.get("exercise_id"))
            if canonical is not None:
                planned[canonical] = exercise
        if not planned:
            continue
        sessions += 1
        actual: dict[int, int] = {}
        for exercise in data.get("exercises", []) or []:
            canonical = canonical_exercise_id(exercise.get("exercise_id"))
            if canonical is not None:
                actual[canonical] = actual.get(canonical, 0) + len(exercise.get("sets", []) or [])
        for canonical, plan_exercise in planned.items():
            plan_sets = len(plan_exercise.get("sets", []) or [])
            planned_total += plan_sets
            fact_sets = actual.get(canonical)
            if not fact_sets:
                skipped[str(plan_exercise.get("name") or canonical)] += 1
                continue
            done_total += min(fact_sets, plan_sets)

    if not sessions or not planned_total:
        return None
    return {
        "days": days,
        "sessions": sessions,
        "planned_sets": planned_total,
        "done_sets": done_total,
        "pct": round(done_total / planned_total * 100),
        "skipped": skipped.most_common(4),
    }


# --------------------------------------------------------------------------- #
# Для семантического валидатора (P5)
# --------------------------------------------------------------------------- #
def recent_weight_range(
    workouts: list[dict[str, Any]],
    exercise_id: int,
    today: date,
    days: int = 56,
) -> tuple[float, float] | None:
    """Диапазон рабочих весов упражнения за последние 8 недель или ``None``."""
    canonical = canonical_exercise_id(exercise_id)
    weights: list[float] = []
    for workout in workouts:
        when = _workout_date(workout)
        if when is None or when > today or (today - when).days >= days:
            continue
        for exercise in (workout.get("data", {}) or {}).get("exercises", []) or []:
            if canonical_exercise_id(exercise.get("exercise_id")) != canonical:
                continue
            for workout_set in exercise.get("sets", []) or []:
                try:
                    weight = float(workout_set.get("weight", 0))
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(weight) and weight > 0:
                    weights.append(weight)
    if not weights:
        return None
    return min(weights), max(weights)
