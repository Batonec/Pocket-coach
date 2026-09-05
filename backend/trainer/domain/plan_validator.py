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

from datetime import date
from typing import Any

from trainer.data.anthropic_client import RecommendationError
from trainer.domain import coach_features, coach_state

# Server-side sanity bounds (JSON Schema can't express numeric ranges, so the
# model output is clamped/filtered after parsing). These are sanitation, not
# methodology: rep ranges, session size and weight jumps are the model's
# coaching judgement and are deliberately NOT validated.
MAX_REPS = 100
MAX_WEIGHT = 1000.0
MAX_EXERCISES = 10
MAX_SETS_PER_EXERCISE = 12
MAX_REST_DAYS = 4  # rest_days is clamped to 0–4 silently, never reprompted

ALLOWED_LOAD_TYPES = ("heavy", "medium", "light")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _validate(raw: dict[str, Any], catalog: list[dict[str, Any]]) -> dict[str, Any]:
    valid_ids = {
        item["id"] for item in catalog if item["id"] not in coach_features.EXERCISE_ALIASES
    }
    names_by_id = {item["id"]: item["name"] for item in catalog}

    load_type = raw.get("load_type")
    if load_type not in ALLOWED_LOAD_TYPES:
        load_type = "medium"

    try:
        rest_days = int(raw.get("rest_days"))
    except (TypeError, ValueError):
        rest_days = 1
    rest_days = min(max(rest_days, 0), MAX_REST_DAYS)

    exercises_out: list[dict[str, Any]] = []
    for exercise in raw.get("exercises", []) or []:
        if not isinstance(exercise, dict):
            continue
        try:
            exercise_id = int(exercise.get("exercise_id"))
        except (TypeError, ValueError):
            continue
        # The schema enum already excludes the duplicate ids; re-map defensively
        # in case an aliased id sneaks in anyway.
        exercise_id = coach_features.EXERCISE_ALIASES.get(exercise_id, exercise_id)
        if exercise_id not in valid_ids:
            continue

        sets_out: list[dict[str, Any]] = []
        for workout_set in exercise.get("sets", []) or []:
            if not isinstance(workout_set, dict):
                continue
            try:
                reps = int(workout_set.get("reps"))
                weight = float(workout_set.get("weight"))
            except (TypeError, ValueError):
                continue
            if reps < 1:
                continue
            reps = min(reps, MAX_REPS)
            weight = min(max(weight, 0.0), MAX_WEIGHT)
            sets_out.append({"reps": reps, "weight": weight})
            if len(sets_out) >= MAX_SETS_PER_EXERCISE:
                break

        if not sets_out:
            continue

        exercises_out.append(
            {
                "exercise_id": exercise_id,
                # Trust the catalog name over whatever the model echoed back.
                "name": names_by_id.get(exercise_id, str(exercise.get("name", "")).strip()),
                "note": str(exercise.get("note", "")).strip(),
                "sets": sets_out,
            }
        )
        if len(exercises_out) >= MAX_EXERCISES:
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
# Semantic validation (on top of the JSON schema)
# --------------------------------------------------------------------------- #
def _comeback_ceilings(
    workouts: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    today: date,
) -> dict[int, dict[str, Any]]:
    """Per-exercise hard weight ceilings for a return-from-break session, or an
    empty dict outside one. The ceiling is the pre-break working weight: a
    comeback session is not the place for progression. Everything else about
    the return (how far below to start, ramp speed, session size) is the
    model's coaching judgement — the ladder in the prompt is data, not a bound."""
    if not coach_state.is_return_from_break(workouts, today):
        return {}
    return {
        item["exercise_id"]: item
        for item in coach_features.pre_break_working_weights(workouts, catalog)
    }


def _session_cap(params: dict[str, Any]) -> int | None:
    """Upper bound of the phase's session corridor (`session_sets`), or None
    when the parameter is not a usable range — then the size is not checked."""
    bounds = params.get("session_sets")
    if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
        try:
            return int(bounds[1])
        except (TypeError, ValueError):
            return None
    return None


def _planned_sets(recommendation: dict[str, Any]) -> int:
    return sum(len(exercise["sets"]) for exercise in recommendation.get("exercises", []) or [])


def _semantic_violations(
    recommendation: dict[str, Any],
    catalog: list[dict[str, Any]],
    workouts: list[dict[str, Any]],
    today: date,
    session_cap: int | None = None,
) -> list[str]:
    """The three hard bounds the JSON schema cannot express: the comeback
    no-progression ceiling, muscle-group coverage and the session-size cap of
    the phase. Rep ranges, weight jumps, load sequencing and the LOWER bound
    of the session corridor are deliberately NOT checked — that is the
    model's coaching judgement, guided by the prompt. Each violation is a
    human-readable line — the list goes verbatim into the reprompt."""
    violations: list[str] = []

    # 1) return from a break: no weight above the pre-break working weight.
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

    # 2) group coverage: a big group (or the chronically lagging hamstrings)
    # that has been dry for 10+ days must get at least a set in the plan.
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

    # 3) session size: the phase corridor's upper bound is a hard cap. The
    # athlete's ~60-minute sessions overran on 19–22-set cards built «по
    # дефициту объёма»; the corridor is the phase's own parameter, so the
    # server holds the line. The lower bound stays unchecked — a short
    # session can be a decision.
    if session_cap is not None:
        total = _planned_sets(recommendation)
        if total > session_cap:
            violations.append(
                f"в плане {total} рабочих подходов при потолке сессии {session_cap} для этой "
                f"фазы — сократи до {session_cap}, начиная с изоляции"
            )

    return violations


def _trim_to_cap(recommendation: dict[str, Any], cap: int) -> list[str]:
    """Drop sets from the tail of the plan until it fits the cap: the last
    exercise first, one set per pass, never below one set per exercise — so
    the coverage rule (≥1 set for a dry group) survives the cut. Removing sets
    invents no numbers, which is why this bound gets a real resolution and
    coverage only gets a note."""
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
    """Deterministic last resort after the reprompt also failed: clamp comeback
    weights to their pre-break ceilings, trim an oversized session to the cap,
    and surface anything unfixable (group coverage) as an honest note in the
    rationale. A slightly imperfect plan with a visible note beats an error
    card — generation must not fail over methodology."""
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
