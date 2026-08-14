#!/usr/bin/env python3
"""Computed history features for the coach prompt.

The model used to receive 20 raw workouts and re-derive records, stalls and
volumes on every call — anchoring on whatever the recent sessions happened to
be. This module pre-computes those facts on the server so the prompt feeds the
model *data*, not homework:

- per-exercise all-time summaries (top set, Epley e1RM, last PR, recent sessions);
- a stall detector with explicit "resource exhausted" preconditions;
- return-from-break ramp steps (current → peak over 3–4 sessions);
- weekly volume per muscle group in direct AND effective sets (secondary load);
- body-weight / waist trends and the phase nutrition decision matrix.

Stdlib-only, like the rest of the backend.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Any

# Catalog id 1 («Жим гор.») and id 18 («Жим в тренажере») are the same machine;
# old history rows still carry id 1, so every consumer maps through this alias.
EXERCISE_ALIASES: dict[int, int] = {1: 18}

# Assisted pull-ups: the weight field is the COUNTERWEIGHT (assistance), so
# progress is the weight going DOWN — every comparison below is inverted.
GRAVITRON_ID = 4

# Base movements that get an explicit comeback ramp after a break; isolation
# just follows the working-weight rule.
MAIN_MOVEMENT_IDS = (18, 9, 10, 8, GRAVITRON_ID)

# Primary muscle group per (canonical) exercise id.
MUSCLE_GROUPS: dict[str, tuple[int, ...]] = {
    "грудь": (18, 17),
    "спина": (9, GRAVITRON_ID, 10),
    "дельты": (13,),
    "бицепс": (11,),
    "трицепс": (12,),
    "квадрицепс/ягодичные": (8, 16),
    "бицепс бедра": (15,),
}

# Effective weekly sets: direct work counts 1.0 for the primary group, and the
# compounds feed secondary muscles ~half a set each (presses → triceps + front
# delts; every pull → biceps). The leg press's extra glute share is folded into
# the combined quad/glute group, so it stays 1.0 there.
EFFECTIVE_SETS: dict[int, dict[str, float]] = {
    18: {"грудь": 1.0, "трицепс": 0.5, "дельты": 0.5},
    17: {"грудь": 1.0},
    9: {"спина": 1.0, "бицепс": 0.5},
    GRAVITRON_ID: {"спина": 1.0, "бицепс": 0.5},
    10: {"спина": 1.0, "бицепс": 0.5},
    13: {"дельты": 1.0},
    11: {"бицепс": 1.0},
    12: {"трицепс": 1.0},
    8: {"квадрицепс/ягодичные": 1.0},
    16: {"квадрицепс/ягодичные": 1.0},
    15: {"бицепс бедра": 1.0},
}

BIG_GROUPS = ("грудь", "спина", "квадрицепс/ягодичные")

# Plausible adult body-weight bounds: entries outside are logging noise (e.g.
# an exercise weight saved into the body-weight table) and must never reach
# the calorie-advice logic.
MIN_PLAUSIBLE_BODY_WEIGHT = 40.0
MAX_PLAUSIBLE_BODY_WEIGHT = 150.0
MIN_PLAUSIBLE_WAIST_CM = 50.0
MAX_PLAUSIBLE_WAIST_CM = 160.0

# Freshness rule shared by weight and waist: stale data → no calorie advice.
STALE_MEASUREMENT_DAYS = 14

# Stall-detector calibration ("resource exhausted" preconditions, section 4.2).
STALL_WINDOW_DAYS = 42            # 6 weeks
STALL_MIN_WORKOUTS = 15           # ≥2.5 sessions/week over the window
STALL_MIN_WEEKLY_SETS = 10.0      # per BIG group, averaged over the window
STALL_NO_PR_DAYS = 28             # ≥4 weeks without a PR
STALL_MIN_EXERCISE_SESSIONS = 3   # can't stall a lift you barely visit

_EFFORT_MARK = {"easy": "-", "ok": "", "hard": "+"}


def canonical_exercise_id(exercise_id: Any) -> int | None:
    try:
        parsed = int(exercise_id)
    except (TypeError, ValueError):
        return None
    return EXERCISE_ALIASES.get(parsed, parsed)


def epley_e1rm(weight: float, reps: int) -> float:
    return weight * (1 + reps / 30)


def _workout_date(workout: dict[str, Any]) -> date | None:
    try:
        return date.fromisoformat(str(workout.get("workout_date", "")))
    except ValueError:
        return None


def _iter_exercise_sessions(
    workouts: list[dict[str, Any]],
) -> dict[int, list[tuple[date, list[dict[str, Any]]]]]:
    """{canonical exercise id: [(date, sets), ...] oldest-first}."""
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
                try:
                    reps = int(workout_set.get("reps", 0))
                    weight = float(workout_set.get("weight", 0))
                except (TypeError, ValueError):
                    continue
                if reps < 1:
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
    """The set that defines the session: best e1RM, or for the gravitron the
    lowest counterweight (more reps breaks the tie)."""
    if inverted:
        return min(sets, key=lambda s: (s["weight"], -s["reps"]))
    return max(sets, key=lambda s: epley_e1rm(s["weight"], s["reps"]))


def _format_set(workout_set: dict[str, Any]) -> str:
    mark = _EFFORT_MARK.get(workout_set.get("effort") or "", "")
    rir = workout_set.get("rir")
    rir_repr = f"@{int(rir)}" if isinstance(rir, (int, float)) and not isinstance(rir, bool) else ""
    return f"{workout_set['weight']:g}×{workout_set['reps']}{mark}{rir_repr}"


# --------------------------------------------------------------------------- #
# Per-exercise summaries (4.1)
# --------------------------------------------------------------------------- #
def exercise_summaries(
    workouts: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    today: date,
) -> list[dict[str, Any]]:
    names = {item["id"]: item["name"] for item in catalog}
    sessions_by_exercise = _iter_exercise_sessions(workouts)

    summaries: list[dict[str, Any]] = []
    for exercise_id, sessions in sessions_by_exercise.items():
        if len(sessions) < 2:
            continue
        inverted = exercise_id == GRAVITRON_ID

        best: dict[str, Any] | None = None
        best_when: date | None = None
        last_pr: date | None = None
        pr_dates: list[str] = []  # improvement events only (baseline excluded)
        for when, sets in sessions:
            top = _session_top(sets, inverted=inverted)
            if best is None:
                best, best_when, last_pr = top, when, when
                continue
            if inverted:
                is_pr = top["weight"] < best["weight"] or (
                    top["weight"] == best["weight"] and top["reps"] > best["reps"]
                )
            else:
                is_pr = epley_e1rm(top["weight"], top["reps"]) > epley_e1rm(
                    best["weight"], best["reps"]
                ) + 1e-9
            if is_pr:
                best, best_when, last_pr = top, when, when
                pr_dates.append(when.isoformat())

        current_when, current_sets = sessions[-1]
        current_top = _session_top(current_sets, inverted=inverted)
        if inverted:
            peak_weight, current_weight = best["weight"], current_top["weight"]
            pct = round(peak_weight / current_weight * 100) if current_weight > 0 else None
        else:
            peak_weight, current_weight = best["weight"], current_top["weight"]
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
                    (when.isoformat(), ", ".join(_format_set(s) for s in sets))
                    for when, sets in sessions[-3:]
                ],
            }
        )

    order = {exercise_id: index for index, exercise_id in enumerate(MAIN_MOVEMENT_IDS)}
    summaries.sort(key=lambda s: (order.get(s["exercise_id"], 99), -s["sessions_total"]))
    return summaries


def render_exercise_summaries(summaries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for summary in summaries:
        if summary["inverted"]:
            peak = (
                f"лучший противовес {summary['top_weight']:g}×{summary['top_reps']} "
                f"({summary['top_date']}, меньше = сильнее)"
            )
            now = f"сейчас противовес {summary['current_weight']:g}"
        else:
            peak = (
                f"пик {summary['top_weight']:g}×{summary['top_reps']} "
                f"(e1RM {summary['e1rm']:g}, {summary['top_date']})"
            )
            now = f"сейчас {summary['current_weight']:g}"
        if summary["pct_of_peak"] is not None:
            now += f" ({summary['pct_of_peak']}% пика)"
        recent = "; ".join(f"{when}: {sets}" for when, sets in summary["recent_sessions"])
        lines.append(
            f"  {summary['name']}: {peak}; ПР {summary['days_since_pr']} дн. назад; "
            f"{now}. Последние: {recent}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Weekly volume in direct and effective sets (4.4)
# --------------------------------------------------------------------------- #
def weekly_volume(
    workouts: list[dict[str, Any]], today: date, days: int = 7
) -> dict[str, dict[str, float]]:
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


def render_weekly_volume(
    volume: dict[str, dict[str, float]],
    week_target: tuple[int, int] | None,
    maintenance_sets: tuple[int, int] | None = None,
) -> str:
    lines = []
    for group, counts in volume.items():
        effective = f"{counts['effective']:g}"
        lines.append(f"  {group}: {counts['direct']} прямых / {effective} эффективных")
    if week_target:
        lines.append(
            f"  Цель этой недели блока для крупных групп: {week_target[0]}–{week_target[1]} "
            "эффективных сетов; малые группы — пропорционально ниже своих потолков."
        )
    elif maintenance_sets:
        lines.append(
            f"  Режим поддержания: {maintenance_sets[0]}–{maintenance_sets[1]} сета на группу "
            "в неделю, объём НЕ растёт."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Stall detector (4.2)
# --------------------------------------------------------------------------- #
def stall_report(
    workouts: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    weight_trend_per_week: float | None,
    phase: str,
    rate_range: tuple[float, float] | None,
    today: date,
) -> dict[str, Any]:
    """Preconditions first: a plateau only counts as «ресурс исчерпан» when the
    athlete actually trained enough, ate enough and slept the volume in. If the
    preconditions are red, the flag is withheld ON PURPOSE — the model must
    explain the plateau through attendance/food, not through a «potолок»."""
    window_dates = [
        when
        for when in (_workout_date(w) for w in workouts)
        if when is not None and 0 <= (today - when).days < STALL_WINDOW_DAYS
    ]
    reasons: list[str] = []
    frequency = len(window_dates) / (STALL_WINDOW_DAYS / 7)
    if len(window_dates) < STALL_MIN_WORKOUTS:
        reasons.append(
            f"частота {frequency:.1f}/нед за 6 недель (нужно ≥2.5)"
        )

    volume = weekly_volume(workouts, today, days=STALL_WINDOW_DAYS)
    weeks = STALL_WINDOW_DAYS / 7
    low_groups = [
        f"{group} {volume[group]['direct'] / weeks:.1f}"
        for group in BIG_GROUPS
        if volume[group]["direct"] / weeks < STALL_MIN_WEEKLY_SETS
    ]
    if low_groups:
        reasons.append(
            f"объём ниже {STALL_MIN_WEEKLY_SETS:g} сетов/нед: {', '.join(low_groups)}"
        )

    if weight_trend_per_week is None:
        reasons.append("нет свежего тренда веса")
    elif phase == "cut_recomp":
        if rate_range and not (
            rate_range[0] - 0.15 <= weight_trend_per_week <= rate_range[1] + 0.15
        ):
            reasons.append(
                f"вес вне целевого темпа среза ({weight_trend_per_week:+.2f} кг/нед)"
            )
    else:  # lean_bulk / maintenance: the weight must not be falling
        if weight_trend_per_week < -0.1:
            reasons.append(f"вес падает ({weight_trend_per_week:+.2f} кг/нед)")

    ok = not reasons
    stalled: list[dict[str, Any]] = []
    if ok:
        for summary in summaries:
            recent_sessions = sum(
                1
                for when, _ in (
                    (date.fromisoformat(w), s) for w, s in summary["recent_sessions"]
                )
                if (today - when).days < STALL_WINDOW_DAYS
            )
            if (
                summary["days_since_pr"] >= STALL_NO_PR_DAYS
                and summary["sessions_total"] >= STALL_MIN_EXERCISE_SESSIONS
                and recent_sessions >= 2
            ):
                stalled.append(summary)
    return {"preconditions_ok": ok, "reasons": reasons, "stalled": stalled}


def render_stall_report(report: dict[str, Any]) -> str | None:
    if report["preconditions_ok"] and report["stalled"]:
        names = ", ".join(
            f"{s['name']} (ПР {s['days_since_pr']} дн. назад)" for s in report["stalled"]
        )
        return (
            f"ЗАСТОЙ при выполненных предусловиях (частота/объём/питание в норме): {names}. "
            "Предложи deload −10% с разгоном или вариацию по этим движениям."
        )
    if not report["preconditions_ok"] and report["reasons"]:
        return (
            "Предусловия прогресса НЕ выполнены ("
            + "; ".join(report["reasons"])
            + ") — плато, если оно есть, объясняй посещаемостью/питанием, а не потолком."
        )
    return None


# --------------------------------------------------------------------------- #
# Return-from-break ramp steps (4.3)
# --------------------------------------------------------------------------- #
def _weight_step_hint(sessions: list[tuple[date, list[dict[str, Any]]]]) -> float:
    """The step the athlete actually uses on this machine (most common diff
    between consecutive distinct session top-weights)."""
    tops = [
        _session_top(sets, inverted=False)["weight"] for _, sets in sessions
    ]
    diffs = [
        round(abs(b - a), 2)
        for a, b in zip(tops, tops[1:])
        if abs(b - a) > 0.01
    ]
    if not diffs:
        return 5.0
    return Counter(diffs).most_common(1)[0][0]


def comeback_ramp(
    workouts: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    today: date,
) -> list[str]:
    """For each main movement noticeably below its peak, precomputed return
    steps: current → peak over 3–4 sessions, using the machine's own step."""
    names = {item["id"]: item["name"] for item in catalog}
    sessions_by_exercise = _iter_exercise_sessions(workouts)
    lines: list[str] = []
    for exercise_id in MAIN_MOVEMENT_IDS:
        sessions = sessions_by_exercise.get(exercise_id)
        if not sessions or len(sessions) < 2:
            continue
        inverted = exercise_id == GRAVITRON_ID
        tops = [_session_top(sets, inverted=inverted) for _, sets in sessions]
        if inverted:
            peak = min(top["weight"] for top in tops)
            current = tops[-1]["weight"]
            gap = current - peak
            threshold = peak * 0.07
        else:
            peak = max(top["weight"] for top in tops)
            current = tops[-1]["weight"]
            gap = peak - current
            threshold = peak * 0.07
        if gap <= max(threshold, 0.01):
            continue

        step_hint = _weight_step_hint(sessions) if not inverted else max(
            _weight_step_hint(sessions), 2.5
        )
        session_count = 4 if gap / peak > 0.2 else 3
        increment = max(step_hint, round(gap / session_count / step_hint) * step_hint)
        steps: list[float] = []
        weight = current
        direction = -1 if inverted else 1
        while len(steps) < session_count - 1:
            weight = weight + direction * increment
            if (not inverted and weight >= peak) or (inverted and weight <= peak):
                break
            steps.append(weight)
        steps.append(peak)
        peak_top = max(tops, key=lambda t: t["reps"]) if inverted else max(
            tops, key=lambda t: epley_e1rm(t["weight"], t["reps"])
        )
        arrow = " → ".join(f"{step:g}" for step in steps)
        what = "противовес" if inverted else "пик"
        lines.append(
            f"  {names.get(exercise_id, f'#{exercise_id}')}: {what} "
            f"{peak:g}×{peak_top['reps']}, сейчас {current:g}. Ступени: {arrow}"
        )
    return lines


# --------------------------------------------------------------------------- #
# Body-weight / waist trends + nutrition decision matrix (P3)
# --------------------------------------------------------------------------- #
def _measurement_points(
    entries: list[dict[str, Any]], value_key: str, low: float, high: float
) -> list[tuple[date, float]]:
    points: list[tuple[date, float]] = []
    for entry in entries:  # oldest-first
        try:
            when = date.fromisoformat(str(entry.get("entry_date", "")))
            value = float(entry.get(value_key, 0))
        except (TypeError, ValueError):
            continue
        if low <= value <= high:
            points.append((when, value))
    return points


def weight_points(body_weights: list[dict[str, Any]]) -> list[tuple[date, float]]:
    return _measurement_points(
        body_weights, "weight", MIN_PLAUSIBLE_BODY_WEIGHT, MAX_PLAUSIBLE_BODY_WEIGHT
    )


def waist_points(waists: list[dict[str, Any]]) -> list[tuple[date, float]]:
    return _measurement_points(
        waists, "waist", MIN_PLAUSIBLE_WAIST_CM, MAX_PLAUSIBLE_WAIST_CM
    )


def moving_average(
    points: list[tuple[date, float]], on_day: date, window_days: int = 7
) -> float | None:
    window = [
        value
        for when, value in points
        if 0 <= (on_day - when).days < window_days
    ]
    if not window:
        return None
    return sum(window) / len(window)


def weight_trend_per_week(points: list[tuple[date, float]], today: date) -> float | None:
    """Slope over the recent window, moving-average based (single outliers
    don't decide anything)."""
    window = [p for p in points if (today - p[0]).days <= 42]
    if len(window) < 2 or (window[-1][0] - window[0][0]).days < 7:
        return None
    span_days = (window[-1][0] - window[0][0]).days
    return (window[-1][1] - window[0][1]) / span_days * 7


def _is_fresh(points: list[tuple[date, float]], today: date) -> bool:
    return bool(points) and (today - points[-1][0]).days <= STALE_MEASUREMENT_DAYS


def nutrition_matrix(
    state: dict[str, Any],
    params: dict[str, Any],
    body_weights: list[dict[str, Any]],
    waists: list[dict[str, Any]],
    today: date,
) -> dict[str, Any]:
    """Server-side branch of the (weight trend × waist trend) matrix. The model
    receives the chosen branch as data and words the advice; it never has to
    re-derive the trends."""
    phase = params.get("phase", "cut_recomp")
    weights = weight_points(body_weights)
    waist = waist_points(waists)

    lines: list[str] = []
    goal: str | None = None

    fresh_weight = _is_fresh(weights, today)
    fresh_waist = _is_fresh(waist, today)
    if not fresh_weight:
        lines.append(
            "замер веса устарел (>14 дней) или отсутствует — советы по калориям не давай, "
            "попроси взвеситься"
        )
    if not fresh_waist:
        lines.append(
            "замера талии за последние 14 дней нет — попроси замерить (утром натощак, по пупку)"
        )

    # Weight: current 7-day MA vs the MA two weeks ago → «стоит/движется».
    ma_now = moving_average(weights, today)
    ma_before = moving_average(weights, today - timedelta(days=14))
    trend = weight_trend_per_week(weights, today)
    stalled_2w = (
        ma_now is not None
        and ma_before is not None
        and abs(ma_now - ma_before) < 0.25
    )

    # Waist: the last two measurements decide the direction (noise gate 0.3 см).
    waist_delta = None
    if len(waist) >= 2:
        waist_delta = waist[-1][1] - waist[-2][1]
    waist_down = waist_delta is not None and waist_delta <= -0.3
    waist_base = state.get("waist_base_cm")
    waist_limit = state.get("waist_limit_cm")
    last_waist = waist[-1][1] if waist else None

    if fresh_weight:
        if phase == "cut_recomp":
            target = params.get("target_weight_kg")
            if target and ma_now is not None and ma_now <= float(target):
                goal = (
                    f"цель фазы достигнута (средний вес {ma_now:.1f} ≤ {target:g} кг) — "
                    "предложи в rationale переход в lean_bulk; фазу переключает атлет"
                )
            if stalled_2w and fresh_waist and waist_down:
                lines.append(
                    "вес стоит ≥2 недель, но талия идёт вниз — это рекомп-бонус, калории НЕ снижай"
                )
            elif stalled_2w:
                lines.append("вес стоит ≥2 недель — посоветуй −100–150 ккал")
            elif trend is not None:
                lines.append(f"тренд веса {trend:+.2f} кг/нед — в рамках плана, калории не трогай"
                             if -0.5 <= trend <= -0.1
                             else f"тренд веса {trend:+.2f} кг/нед — сверь с целевым темпом")
        elif phase == "lean_bulk":
            ceiling = params.get("ceiling_weight_kg")
            if ceiling and ma_now is not None and ma_now >= float(ceiling):
                goal = (
                    f"потолок набора достигнут (средний вес {ma_now:.1f} ≥ {ceiling:g} кг) — "
                    "предложи мини-кат/смену фазы; фазу переключает атлет"
                )
            if fresh_waist and waist_limit and last_waist is not None and last_waist >= float(waist_limit):
                goal = (
                    f"талия {last_waist:g} см у жёсткого лимита {waist_limit:g} см — "
                    "жёсткий сигнал: предложи мини-кат или смену фазы; решает атлет"
                )
            elif (
                fresh_waist
                and waist_base
                and len(waist) >= 2
                and waist[-1][1] >= float(waist_base) + 1
                and waist[-2][1] >= float(waist_base) + 1
            ):
                lines.append(
                    f"талия +1 см от базовой ({waist_base:g} см) два замера подряд — "
                    "посоветуй −100–150 ккал или паузу набора"
                )
            elif trend is not None and fresh_waist and not waist_down and 0.05 <= trend <= 0.25:
                lines.append("вес растёт в целевом темпе, талия стабильна — чистый набор, не трогай")
        else:  # maintenance
            if trend is not None and abs(trend) > 0.15:
                direction = "вверх" if trend > 0 else "вниз"
                lines.append(
                    f"вес уползает {direction} ({trend:+.2f} кг/нед) — мягкая коррекция "
                    "±100–150 ккал"
                )
            elif trend is not None:
                lines.append("вес и талия стабильны — калории не трогай")

    return {"lines": lines, "goal": goal, "trend_per_week": trend}


def render_measurements(
    body_weights: list[dict[str, Any]],
    waists: list[dict[str, Any]],
    today: date,
) -> list[str]:
    """Compact recent weigh-ins and waist measurements for the prompt."""
    lines: list[str] = []
    weights = weight_points(body_weights)
    if weights:
        tail = ", ".join(f"{when.isoformat()}: {value:g}кг" for when, value in weights[-6:])
        age = (today - weights[-1][0]).days
        dropped = len(body_weights) - len(weights)
        line = f"Вес тела: {tail}. Дней с последнего замера: {age}."
        if dropped:
            line += f" (отброшено неправдоподобных записей: {dropped})"
        lines.append(line)
    waist = waist_points(waists)
    if waist:
        tail = ", ".join(f"{when.isoformat()}: {value:g}см" for when, value in waist[-6:])
        age = (today - waist[-1][0]).days
        lines.append(f"Талия: {tail}. Дней с последнего замера: {age}.")
    return lines


# --------------------------------------------------------------------------- #
# Phase summary (what a preparation phase actually delivered)
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
    """Everything is derived from history by date range, so past phases can be
    summarized at any time — the phase journal only stores the boundaries."""
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
        in_phase = [
            pr for pr in summary["pr_dates"]
            if started <= date.fromisoformat(pr) <= ended
        ]
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


def render_phase_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"Фаза {summary['phase']}: {summary['started']} → {summary['ended']} "
        f"({summary['weeks']} нед)."
    ]
    per_week = f" ({summary['per_week']}/нед)" if summary["per_week"] is not None else ""
    lines.append(f"Тренировок: {summary['workouts']}{per_week}.")
    if summary["weight_start"] is not None and summary["weight_end"] is not None:
        rate = (
            f", темп {summary['weight_rate_per_week']:+.2f} кг/нед"
            if summary["weight_rate_per_week"] is not None
            else ""
        )
        lines.append(
            f"Вес: {summary['weight_start']:.1f} → {summary['weight_end']:.1f} кг "
            f"({summary['weight_end'] - summary['weight_start']:+.1f}{rate})."
        )
    else:
        lines.append("Вес: замеров в периоде фазы нет.")
    if summary["waist_start"] is not None and summary["waist_end"] is not None:
        lines.append(
            f"Талия: {summary['waist_start']:g} → {summary['waist_end']:g} см "
            f"({summary['waist_end'] - summary['waist_start']:+.1f})."
        )
    if summary["prs"]:
        top = "; ".join(
            f"{item['name']} ×{item['count']} (лучшее "
            + (
                f"противовес {item['top_weight']:g}×{item['top_reps']}"
                if item["inverted"]
                else f"{item['top_weight']:g}×{item['top_reps']}"
            )
            + ")"
            for item in summary["prs"][:5]
        )
        lines.append(f"ПР за фазу: {summary['pr_events']} — {top}.")
    else:
        lines.append("ПР за фазу: нет.")
    adherence_line = render_adherence_stats(summary["adherence"])
    if adherence_line:
        lines.append(adherence_line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Plan-adherence history (30-day discipline aggregate)
# --------------------------------------------------------------------------- #
def adherence_stats(
    workouts: list[dict[str, Any]], today: date, days: int = 30
) -> dict[str, Any] | None:
    """Aggregate fact-vs-plan over the trailing window: % of planned sets done
    (done sets are capped by the plan so extras never inflate past 100%), how
    many sessions followed a plan, and which exercises get skipped outright.
    The coach uses it to make plans realistic, not to lecture."""
    planned_total = 0
    done_total = 0
    sessions = 0
    skipped: Counter[str] = Counter()

    for workout in workouts:
        when = _workout_date(workout)
        if when is None or when > today or (today - when).days >= days:
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
                actual[canonical] = actual.get(canonical, 0) + len(
                    exercise.get("sets", []) or []
                )
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


def render_adherence_stats(stats: dict[str, Any] | None) -> str | None:
    if not stats:
        return None
    line = (
        f"Дисциплина за {stats['days']} дней: {stats['sessions']} трен. по плану, "
        f"{stats['done_sets']} из {stats['planned_sets']} плановых подходов "
        f"({stats['pct']}%)."
    )
    if stats["skipped"]:
        skipped = ", ".join(f"{name} ×{count}" for name, count in stats["skipped"])
        line += f" Полностью пропускались: {skipped}."
    return line


# --------------------------------------------------------------------------- #
# Semantic-validator support (P5)
# --------------------------------------------------------------------------- #
def recent_weight_range(
    workouts: list[dict[str, Any]],
    exercise_id: int,
    today: date,
    days: int = 56,
) -> tuple[float, float] | None:
    """Working-weight range of the exercise over the trailing 8 weeks."""
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
                except (TypeError, ValueError):
                    continue
                if weight > 0:
                    weights.append(weight)
    if not weights:
        return None
    return min(weights), max(weights)
