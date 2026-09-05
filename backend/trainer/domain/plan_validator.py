#!/usr/bin/env python3
"""Проверка плана, который вернула модель.

Два уровня. Санитизация (``_validate``): JSON-схема не умеет числовых
диапазонов, поэтому повторы, веса, число упражнений и подходов клампятся и
фильтруются уже после разбора — это гигиена, а не методика. Семантика
(``_semantic_violations``): ровно три жёсткие границы, которые схема выразить
не может, — возвратный потолок весов после перерыва, покрытие сухих групп и
потолок размера сессии фазы. Диапазоны повторов, шаги весов, чередование
нагрузок и нижняя граница сессии сознательно не проверяются: это суждение
модели, направляемое промптом.

Нарушения уходят в модель одним репромптом; если она промахнулась снова,
``_resolve_violations`` чинит детерминированно то, что можно починить, не
выдумывая чисел, и пишет об этом в rationale. Генерация не падает из-за
методики.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from trainer.data.anthropic_client import RecommendationError
from trainer.domain import coach_features, coach_state, limits


# --------------------------------------------------------------------------- #
# Санитизация
# --------------------------------------------------------------------------- #
def _validate(raw: dict[str, Any], catalog: list[dict[str, Any]]) -> dict[str, Any]:
    """Санитизация ответа модели по границам, которых нет в JSON-схеме.

    Метка нагрузки только из допустимых (иначе medium); ``rest_days``, повторы, вес,
    число подходов и упражнений клампятся по потолкам из ``limits``; только
    канонические id каталога (дубль id 1 переводится в 18); имя берётся из
    каталога, а не то, что вернула модель. Ни одного валидного упражнения —
    ``RecommendationError``. Зовёт ``recommender.generate_with_trace`` после
    каждого вызова модели.
    """
    valid_ids = {
        item["id"] for item in catalog if item["id"] not in coach_features.EXERCISE_ALIASES
    }
    names_by_id = {item["id"]: item["name"] for item in catalog}

    load_type = raw.get("load_type")
    if load_type not in limits.PLANNED_LOAD_TYPES:
        load_type = "medium"

    raw_rest_days = raw.get("rest_days")
    try:
        rest_days = int(raw_rest_days) if raw_rest_days is not None else 1
    except (TypeError, ValueError, OverflowError):
        rest_days = 1
    rest_days = min(max(rest_days, 0), limits.MAX_REST_DAYS)

    raw_exercises = raw.get("exercises", [])
    if not isinstance(raw_exercises, list):
        raw_exercises = []
    exercises_out: list[dict[str, Any]] = []
    for exercise in raw_exercises:
        if not isinstance(exercise, dict):
            continue
        raw_exercise_id = exercise.get("exercise_id")
        if raw_exercise_id is None or isinstance(raw_exercise_id, bool):
            continue
        try:
            exercise_id = int(raw_exercise_id)
        except (TypeError, ValueError, OverflowError):
            continue
        # Enum схемы уже исключает дубли id; на всякий случай переводим
        # алиас в канонический id, если он всё же проскочил.
        exercise_id = coach_features.EXERCISE_ALIASES.get(exercise_id, exercise_id)
        if exercise_id not in valid_ids:
            continue

        raw_sets = exercise.get("sets", [])
        if not isinstance(raw_sets, list):
            continue
        sets_out: list[dict[str, Any]] = []
        for workout_set in raw_sets:
            if not isinstance(workout_set, dict):
                continue
            raw_reps = workout_set.get("reps")
            raw_weight = workout_set.get("weight")
            if raw_reps is None or raw_weight is None:
                continue
            if isinstance(raw_reps, bool) or isinstance(raw_weight, bool):
                continue
            if isinstance(raw_reps, float) and not raw_reps.is_integer():
                continue
            try:
                reps = int(raw_reps)
                weight = float(raw_weight)
            except (TypeError, ValueError, OverflowError):
                continue
            if reps < 1 or not math.isfinite(weight):
                continue
            reps = min(reps, limits.MAX_REPS)
            weight = min(max(weight, 0.0), limits.MAX_WEIGHT)
            sets_out.append({"reps": reps, "weight": weight})
            if len(sets_out) >= limits.MAX_SETS_PER_EXERCISE:
                break

        if not sets_out:
            continue

        exercises_out.append(
            {
                "exercise_id": exercise_id,
                # Имя берём из каталога, а не то, что модель повторила.
                "name": names_by_id.get(exercise_id, str(exercise.get("name", "")).strip()),
                "note": str(exercise.get("note", "")).strip(),
                "sets": sets_out,
            }
        )
        if len(exercises_out) >= limits.MAX_EXERCISES:
            break

    if not exercises_out:
        raise RecommendationError("Модель не предложила ни одного валидного упражнения")

    return {
        "focus": str(raw.get("focus", "")).strip(),
        "load_type": load_type,
        "rest_days": rest_days,
        "rationale": str(raw.get("rationale", "")).strip(),
        "exercises": exercises_out,
    }


# --------------------------------------------------------------------------- #
# Семантика: три жёсткие границы поверх JSON-схемы
# --------------------------------------------------------------------------- #
def _comeback_ceilings(
    workouts: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    today: date,
) -> dict[int, dict[str, Any]]:
    """Жёсткие потолки веса по упражнениям для возвратной сессии; вне возврата —
    пустой словарь.

    Потолок — доперерывный рабочий вес: возвратная сессия не место для прибавки.
    Всё остальное про возврат (насколько ниже стартовать, скорость разгона, размер
    сессии) — суждение модели: лестница в промпте это данные, а не граница.
    """
    if not coach_state.is_return_from_break(workouts, today):
        return {}
    return {
        item["exercise_id"]: item
        for item in coach_features.pre_break_working_weights(workouts, catalog)
    }


def _session_cap(params: dict[str, Any]) -> int | None:
    """Верхняя граница коридора сессии фазы (``session_sets``) или ``None``, если
    параметр не является пригодным диапазоном — тогда размер не проверяется.
    """
    bounds = params.get("session_sets")
    if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
        try:
            cap = int(bounds[1])
        except (TypeError, ValueError, OverflowError):
            return None
        return cap if cap >= 0 else None
    return None


def _planned_sets(recommendation: dict[str, Any]) -> int:
    """Сколько рабочих подходов в плане всего."""
    return sum(len(exercise["sets"]) for exercise in recommendation.get("exercises", []) or [])


def _semantic_violations(
    recommendation: dict[str, Any],
    catalog: list[dict[str, Any]],
    workouts: list[dict[str, Any]],
    today: date,
    session_cap: int | None = None,
) -> list[str]:
    """Три жёсткие границы, которые JSON-схема выразить не может: возвратный
    потолок весов, покрытие мышечных групп и потолок размера сессии фазы.

    Диапазоны повторов, шаги весов, чередование нагрузок и НИЖНЯЯ граница сессии
    сознательно не проверяются — это суждение модели, направляемое промптом.
    Каждое нарушение — человекочитаемая строка: список уходит в репромпт как есть.
    Зовёт ``recommender.generate_with_trace`` после каждой попытки.
    """
    violations: list[str] = []

    # 1) возврат после перерыва: ни один вес не выше доперерывного рабочего.
    ceilings = _comeback_ceilings(workouts, catalog, today)
    for exercise in recommendation.get("exercises", []) or []:
        ceiling = ceilings.get(exercise["exercise_id"])
        if not ceiling:
            continue
        allowed = ceiling["last_working"]
        for workout_set in exercise["sets"]:
            weight = workout_set["weight"]
            too_hard = weight < allowed - 1e-9 if ceiling["inverted"] else weight > allowed + 1e-9
            if too_hard:
                what = "противовес" if ceiling["inverted"] else "вес"
                violations.append(
                    f"{exercise['name']}: {what} {weight:g} кг тяжелее доперерывного "
                    f"рабочего ({allowed:g}) — возвратная сессия не место для "
                    "прибавки"
                )

    # 2) покрытие групп: крупная группа (или хронически отстающий бицепс бедра),
    # сухая 10+ дней, обязана получить в плане хотя бы подход.
    recent_volume = coach_features.weekly_volume(workouts, today, days=10)
    plan_coverage: dict[str, float] = {}
    for exercise in recommendation["exercises"]:
        for group, share in (
            coach_features.EFFECTIVE_SETS.get(exercise["exercise_id"]) or {}
        ).items():
            plan_coverage[group] = plan_coverage.get(group, 0.0) + share * len(exercise["sets"])
    for group in (*coach_features.BIG_GROUPS, "бицепс бедра"):
        if recent_volume[group]["effective"] == 0 and not plan_coverage.get(group):
            violations.append(
                f"группа «{group}» больше 10 дней без единого эффективного подхода "
                "и отсутствует в плане — добавь хотя бы 1–2 подхода"
            )

    # 3) размер сессии: верхняя граница коридора фазы — жёсткий потолок.
    # Сессии атлета на ~60 минут срывались на карточках в 19–22 подхода,
    # собранных «по дефициту объёма»; коридор — параметр самой фазы, и его
    # держит сервер. Нижняя граница не проверяется: короткая сессия может
    # быть решением.
    if session_cap is not None:
        total = _planned_sets(recommendation)
        if total > session_cap:
            violations.append(
                f"в плане {total} рабочих подходов при потолке сессии {session_cap} для этой "
                f"фазы — сократи до {session_cap}, начиная с изоляции"
            )

    return violations


def _trim_to_cap(recommendation: dict[str, Any], cap: int) -> list[str]:
    """Снять подходы с хвоста плана, пока он не влезет в потолок: сначала последнее
    упражнение, по одному подходу за проход, никогда не ниже одного подхода на
    упражнение — так правило покрытия (≥1 подход сухой группе) переживает срез.
    Удаление подходов не выдумывает чисел, поэтому у этой границы есть настоящее
    разрешение, а у покрытия только пометка. Возвращает строки «имя −N».
    """
    exercises = recommendation.get("exercises", []) or []
    removed: dict[str, int] = {}
    total = _planned_sets(recommendation)
    while total > cap:
        progressed = False
        for exercise in reversed(exercises):
            if total <= cap:
                break
            if len(exercise["sets"]) > 1:
                exercise["sets"].pop()
                removed[exercise["name"]] = removed.get(exercise["name"], 0) + 1
                total -= 1
                progressed = True
        if not progressed:
            break
    return [f"{name} −{count}" for name, count in removed.items()]


def _resolve_violations(
    recommendation: dict[str, Any],
    catalog: list[dict[str, Any]],
    workouts: list[dict[str, Any]],
    today: date,
    session_cap: int | None = None,
) -> list[str]:
    """Детерминированный последний рубеж после неудачного репромпта: ограничить
    возвратные веса доперерывными потолками, урезать раздутую сессию до потолка,
    а то, что починить нельзя (покрытие групп), честно вписать пометкой в rationale.
    Чуть неидеальный план с видимой пометкой лучше карточки с ошибкой — генерация
    не имеет права падать из-за методики. Возвращает список правок.
    """
    adjustments: list[str] = []
    ceilings = _comeback_ceilings(workouts, catalog, today)
    for exercise in recommendation.get("exercises", []) or []:
        ceiling = ceilings.get(exercise["exercise_id"])
        if not ceiling:
            continue
        allowed = ceiling["last_working"]
        for workout_set in exercise["sets"]:
            weight = workout_set["weight"]
            too_hard = weight < allowed - 1e-9 if ceiling["inverted"] else weight > allowed + 1e-9
            if too_hard:
                workout_set["weight"] = allowed
                adjustments.append(
                    f"{exercise['name']}: {weight:g} → {allowed:g} кг (доперерывный рабочий)"
                )

    notes: list[str] = []
    if adjustments:
        notes.append(
            "**Проверка методики:** возвратные веса ограничены доперерывными "
            "рабочими: " + "; ".join(adjustments) + "."
        )
    if session_cap is not None and _planned_sets(recommendation) > session_cap:
        trimmed = _trim_to_cap(recommendation, session_cap)
        if trimmed:
            adjustments.extend(trimmed)
            notes.append(
                f"**Проверка методики:** сессия сокращена до {session_cap} рабочих подходов "
                "(потолок фазы): " + ", ".join(trimmed) + "."
            )
    remaining = _semantic_violations(
        recommendation, catalog, workouts, today, session_cap=session_cap
    )
    if remaining:
        notes.append(
            "**Проверка методики:** сервер не смог согласовать с моделью: "
            + "; ".join(remaining)
            + " — учти при выполнении."
        )
    if notes:
        rationale = str(recommendation.get("rationale", "")).rstrip()
        appendix = "\n\n".join(notes)
        recommendation["rationale"] = f"{rationale}\n\n{appendix}" if rationale else appendix
    return adjustments
