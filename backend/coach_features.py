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

import math
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

# Weekly direct-set landmarks for the small groups (the coaching policy from
# the system prompt); big groups follow the block-week ramp instead.
SMALL_GROUP_TARGETS: dict[str, tuple[int, int]] = {
    "дельты": (6, 12),
    "бицепс": (4, 8),
    "трицепс": (4, 8),
    "бицепс бедра": (5, 10),
}


def group_volume_targets(
    week_target: tuple[int, int] | None,
    maintenance_sets: tuple[int, int] | None = None,
) -> dict[str, tuple[int, int]]:
    """Per-group weekly set targets for the client's volume screen: big groups
    follow the current block-week corridor (ramp/deload), small groups keep
    their policy ranges, maintenance flattens everything to 2–3."""
    targets: dict[str, tuple[int, int]] = {}
    for group in MUSCLE_GROUPS:
        if maintenance_sets:
            targets[group] = tuple(maintenance_sets)
        elif group in BIG_GROUPS:
            targets[group] = tuple(week_target) if week_target else (10, 16)
        else:
            targets[group] = SMALL_GROUP_TARGETS[group]
    return targets

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


def _best_set(
    sessions: list[tuple[date, list[dict[str, Any]]]], *, inverted: bool
) -> tuple[dict[str, Any], date]:
    """The single source of truth for an exercise's peak: the same REAL set
    (weight+reps together) the per-exercise summary reports — best e1RM, or
    the lowest counterweight for the gravitron. Never mix the max weight of
    one set with the reps of another."""
    best: dict[str, Any] | None = None
    best_when: date | None = None
    for when, sets in sessions:
        top = _session_top(sets, inverted=inverted)
        if best is None:
            best, best_when = top, when
            continue
        if inverted:
            is_better = top["weight"] < best["weight"] or (
                top["weight"] == best["weight"] and top["reps"] > best["reps"]
            )
        else:
            is_better = epley_e1rm(top["weight"], top["reps"]) > epley_e1rm(
                best["weight"], best["reps"]
            ) + 1e-9
        if is_better:
            best, best_when = top, when
    return best, best_when


# Anomalous-set filter thresholds for the current-working-weight metric: a
# weight that appears once and sits this far from the recent median is logging
# noise; a <6-rep set only counts if the same weight shows up again nearby.
_OUTLIER_MEDIAN_RATIO = 0.25
_MIN_WORKING_REPS = 6
# «Последние 2–3 сессии» must actually be adjacent in time: a session further
# than this from the newest one belongs to a previous era (pre-break) and says
# nothing about the athlete's CURRENT weights.
_WORKING_WINDOW_DAYS = 14


def current_working_weight(
    sessions: list[tuple[date, list[dict[str, Any]]]], *, inverted: bool
) -> float | None:
    """The athlete's ACTUAL current working weight: the max (min counterweight
    for the gravitron) working-set weight over the last 2–3 sessions that sit
    within two weeks of the newest one, with the same anomaly filter the
    progression rules use — a single set that falls out of the stable series
    (a one-off «20×3» among 10×12s, a light technique day) must not define
    «сейчас» or seed the comeback ramp, while a light day next to a real
    session must not read as a regression."""
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
        return sum(1 for s in pool if abs(s["weight"] - weight) < 0.01)

    # Filter order matters: drop the low-rep garbage FIRST, so a one-off
    # «20×3» in a two-set session cannot drag the median onto itself and get
    # the real working ten thrown out as an "outlier".
    plausible = [
        s for s in all_sets
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

        current_when, _current_sets = sessions[-1]
        # «Сейчас» = the anomaly-filtered working weight of the last 2–3
        # sessions — a one-off light/garbage set must not read as a regression.
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
            label = "сейчас противовес"
        else:
            peak = (
                f"пик {summary['top_weight']:g}×{summary['top_reps']} "
                f"(e1RM {summary['e1rm']:g}, {summary['top_date']})"
            )
            label = "сейчас"
        if summary["current_weight"] is None:
            now = f"{label} — нет данных"
        else:
            now = f"{label} {summary['current_weight']:g}"
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
def _top_weight_diffs(sessions: list[tuple[date, list[dict[str, Any]]]]) -> list[float]:
    tops = [
        _session_top(sets, inverted=False)["weight"] for _, sets in sessions
    ]
    return [
        round(abs(b - a), 2)
        for a, b in zip(tops, tops[1:])
        if abs(b - a) > 0.01
    ]


def _weight_step_hint(sessions: list[tuple[date, list[dict[str, Any]]]]) -> float:
    """The step the athlete actually uses on this machine (most common diff
    between consecutive distinct session top-weights)."""
    diffs = _top_weight_diffs(sessions)
    if not diffs:
        return 5.0
    return Counter(diffs).most_common(1)[0][0]


def _weight_granularity(sessions: list[tuple[date, list[dict[str, Any]]]]) -> float:
    """The machine's plate granularity: the smallest weight change ever made on
    it. Ramp rungs must be multiples of it — a 7.5-kg rung on a 5-kg-plate
    machine cannot be loaded."""
    diffs = _top_weight_diffs(sessions)
    if not diffs:
        return 2.5
    return max(0.5, min(diffs))


# Ramp-ladder invariants: no jump above ~13% (their acceptance regression uses
# a 12.5% first step, so «10–12%» gets a hair of headroom), and after a LARGE
# gap the first rung must sit at or below ~90% of the peak. A ladder violating
# them is rebuilt as three equal steps — never served as-is.
_RAMP_MAX_JUMP = 0.13
_RAMP_BIG_GAP = 0.15
_RAMP_FIRST_STEP_CAP = 0.90


def _round_half(value: float) -> float:
    return round(value * 2) / 2


def _equal_steps(
    current: float, peak: float, count: int, granularity: float = 2.5
) -> list[float]:
    """Fallback ladder: equal fractions from current to peak (works in both
    directions), rounded to loadable plates, strictly monotonic, ending at the
    peak."""
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


def _ramp_valid(current: float, steps: list[float], peak: float, inverted: bool) -> bool:
    if not steps:
        return False
    gap = (current - peak) if inverted else (peak - current)
    big_gap = peak > 0 and gap > _RAMP_BIG_GAP * peak
    previous = current
    for index, step in enumerate(steps):
        if previous <= 0:
            return False
        jump = (previous - step) / previous if inverted else (step - previous) / previous
        if jump > _RAMP_MAX_JUMP + 1e-9:
            return False
        previous = step
    if big_gap:
        first = steps[0]
        if inverted:
            if first < peak * (2 - _RAMP_FIRST_STEP_CAP) - 1e-9:  # ≥110% of peak
                return False
        elif first > peak * _RAMP_FIRST_STEP_CAP + 1e-9:
            return False
    return True


def comeback_ramp_steps(
    workouts: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    today: date,
) -> list[dict[str, Any]]:
    """For each main movement noticeably below its peak: precomputed return
    steps current → peak over 3–4 sessions.

    The peak is THE SAME real set the per-exercise summary reports (best e1RM;
    lowest counterweight for the gravitron) — one source of truth, no phantom
    «max weight × someone else's reps» rows. The start is the anomaly-filtered
    current working weight. The rung size is the machine's own historical step
    capped at ~10% of the peak, and every ladder must pass the jump invariant
    or it is rebuilt as three equal steps."""
    names = {item["id"]: item["name"] for item in catalog}
    sessions_by_exercise = _iter_exercise_sessions(workouts)
    items: list[dict[str, Any]] = []
    for exercise_id in MAIN_MOVEMENT_IDS:
        sessions = sessions_by_exercise.get(exercise_id)
        if not sessions or len(sessions) < 2:
            continue
        inverted = exercise_id == GRAVITRON_ID
        best, _best_when = _best_set(sessions, inverted=inverted)
        peak = best["weight"]
        current = current_working_weight(sessions, inverted=inverted)
        if current is None or peak <= 0:
            continue
        gap = (current - peak) if inverted else (peak - current)
        if gap <= max(peak * 0.07, 0.01):
            continue

        # Rung size: the athlete's usual step, capped at ~10% of the peak, and
        # snapped DOWN to a multiple of the machine's plate granularity (a
        # 7.5-kg rung on a 5-kg-plate machine cannot be loaded).
        granularity = _weight_granularity(sessions)
        base = min(_weight_step_hint(sessions), max(_round_half(peak * 0.10), 0.5))
        step = max(granularity, int(base / granularity) * granularity)
        direction = -1 if inverted else 1
        steps: list[float] = []
        weight = current
        while len(steps) < 12:
            weight = _round_half(weight + direction * step)
            if (not inverted and weight >= peak - 0.01) or (
                inverted and weight <= peak + 0.01
            ):
                break
            steps.append(weight)
        steps.append(peak)
        if len(steps) > 4:
            steps = _equal_steps(current, peak, 4, granularity)
        if not _ramp_valid(current, steps, peak, inverted):
            steps = _equal_steps(current, peak, 3, granularity)

        items.append(
            {
                "exercise_id": exercise_id,
                "name": names.get(exercise_id, f"#{exercise_id}"),
                "inverted": inverted,
                "peak_weight": peak,
                "peak_reps": best["reps"],
                "current": current,
                "steps": steps,
            }
        )
    return items


# Detraining after a lay-off: neural strength survives a few weeks, but
# connective tissue, work capacity and technique lag behind — the first
# sessions back are a re-entry, not a test. Ceiling of the first session as a
# share of the athlete's own pre-break working weight, by break length.
RETURN_CEILING_BY_BREAK_DAYS: tuple[tuple[int, float], ...] = (
    (21, 0.90),   # 14–20 days off
    (35, 0.85),   # 3–5 weeks
    (56, 0.80),   # 5–8 weeks
)
RETURN_CEILING_LONG_BREAK = 0.75  # 8+ weeks


def return_ceiling_ratio(break_days: int) -> float:
    for limit, ratio in RETURN_CEILING_BY_BREAK_DAYS:
        if break_days < limit:
            return ratio
    return RETURN_CEILING_LONG_BREAK


def return_ceilings(
    workouts: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    today: date,
    break_days: int,
) -> list[dict[str, Any]]:
    """First-session weight ceilings for EVERY exercise with history.

    The comeback ladder only covers movements that fell below their peak; an
    athlete who stopped AT his peak had no ladder at all, and the generic
    ±15%-of-recent-range rule then happily allowed a PR attempt on the first
    session back. This closes that hole: after a break every exercise is capped
    at a share of its own last working weight (gravitron inverted — the
    counterweight goes UP, i.e. more assistance)."""
    ratio = return_ceiling_ratio(break_days)
    names = {item["id"]: item["name"] for item in catalog}
    ceilings: list[dict[str, Any]] = []
    for exercise_id, sessions in _iter_exercise_sessions(workouts).items():
        inverted = exercise_id == GRAVITRON_ID
        current = current_working_weight(sessions, inverted=inverted)
        if not current or current <= 0:
            continue
        if inverted:
            # More counterweight = easier: round the assistance UP so the
            # ceiling never lands harder than intended.
            ceiling = math.ceil(current / ratio * 2) / 2
        else:
            raw = current * ratio
            granularity = _weight_granularity(sessions)
            ceiling = int(raw / granularity) * granularity
            # A coarse machine (20-kg jumps) can snap the ceiling to 0 — or to
            # the pre-break weight itself, which would defeat the whole point.
            if ceiling < 0.5 or ceiling >= current:
                ceiling = math.floor(raw * 2) / 2
            ceiling = max(0.5, ceiling)
        ceilings.append(
            {
                "exercise_id": exercise_id,
                "name": names.get(exercise_id, f"#{exercise_id}"),
                "inverted": inverted,
                "last_working": current,
                "ceiling": ceiling,
            }
        )
    order = {exercise_id: index for index, exercise_id in enumerate(MAIN_MOVEMENT_IDS)}
    ceilings.sort(key=lambda item: order.get(item["exercise_id"], 99))
    return ceilings


def render_return_ceilings(items: list[dict[str, Any]], break_days: int) -> str | None:
    if not items:
        return None
    ratio = int(round(return_ceiling_ratio(break_days) * 100))
    lines = [
        f"  {item['name']}: было {item['last_working']:g} → "
        + (
            f"не легче {item['ceiling']:g} противовеса"
            if item["inverted"]
            else f"не больше {item['ceiling']:g}"
        )
        for item in items
    ]
    return (
        f"Потолки весов на первую сессию после перерыва ({break_days} дн. без "
        f"тренировок, ~{ratio}% от прежних рабочих). Почему так: сила нервной "
        "системы держится дольше всего, а связки, сухожилия и рабочая "
        "выносливость теряются быстрее — первая сессия это ВХОД обратно, а не "
        "проверка формы. Прогрессию (плюс вес или плюс повторы к прежним "
        "рабочим) на этой сессии не планируй ни по одному движению, даже если "
        "прошлые подходы были помечены как лёгкие: те отметки относятся к "
        "форме ДО перерыва. Возвращайся к прежним весам за 2–3 сессии, дальше "
        "прогрессия как обычно.\n" + "\n".join(lines)
    )


def render_comeback_ramp(items: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in items:
        arrow = " → ".join(f"{step:g}" for step in item["steps"])
        what = "противовес" if item["inverted"] else "пик"
        lines.append(
            f"  {item['name']}: {what} {item['peak_weight']:g}×{item['peak_reps']}, "
            f"сейчас {item['current']:g}. Ступени: {arrow}"
        )
    return lines


def comeback_ramp(
    workouts: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    today: date,
) -> list[str]:
    """Rendered ramp lines (kept for callers that only need the text)."""
    return render_comeback_ramp(comeback_ramp_steps(workouts, catalog, today))


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


# Trend validity: a weekly rate extrapolated across a measurement hole (a
# vacation) is garbage — the matrix would cut calories off holiday water on
# day one of a phase. A valid trend needs points inside a ~3-week window, no
# adjacent gap above two weeks, and only measurements of the CURRENT phase.
TREND_WINDOW_DAYS = 21
TREND_MAX_GAP_DAYS = 14
TREND_MIN_SPAN_DAYS = 5


def weight_trend_per_week(
    points: list[tuple[date, float]],
    today: date,
    since: date | None = None,
) -> float | None:
    """Weekly rate over the recent window, or None when the data cannot
    honestly support one (too few points, a hole between measurements, or the
    points straddle a phase boundary — pass `since` = phase start)."""
    window = [
        p
        for p in points
        if (today - p[0]).days <= TREND_WINDOW_DAYS
        and (since is None or p[0] >= since)
    ]
    if len(window) < 2:
        return None
    for previous, current in zip(window, window[1:]):
        if (current[0] - previous[0]).days > TREND_MAX_GAP_DAYS:
            return None
    span_days = (window[-1][0] - window[0][0]).days
    if span_days < TREND_MIN_SPAN_DAYS:
        return None
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
    re-derive the trends. Calorie branches fire ONLY on a valid in-phase trend
    (see weight_trend_per_week) — an invalid one yields an explicit «недостаточно
    данных» branch instead of a number extrapolated across a vacation hole."""
    phase = params.get("phase", "cut_recomp")
    phase_start: date | None = None
    started_raw = state.get("phase_started")
    if isinstance(started_raw, str):
        try:
            phase_start = date.fromisoformat(started_raw)
        except ValueError:
            phase_start = None

    # Only measurements of the CURRENT phase feed the matrix — a phase switch
    # resets the base (a measurement on the start day counts).
    weights = [
        p for p in weight_points(body_weights)
        if phase_start is None or p[0] >= phase_start
    ]
    waist = [
        p for p in waist_points(waists)
        if phase_start is None or p[0] >= phase_start
    ]

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

    trend = weight_trend_per_week(weights, today, since=phase_start)

    # Weight: current 7-day MA vs the MA two weeks ago → «стоит/движется».
    ma_now = moving_average(weights, today)
    ma_before = moving_average(weights, today - timedelta(days=14))
    stalled_2w = (
        trend is not None
        and ma_now is not None
        and ma_before is not None
        and abs(ma_now - ma_before) < 0.25
    )

    # Waist: the last two IN-PHASE measurements decide the direction (noise
    # gate 0.3 см) — and only when they sit close enough to compare.
    waist_pair_valid = (
        len(waist) >= 2 and (waist[-1][0] - waist[-2][0]).days <= TREND_MAX_GAP_DAYS
    )
    waist_delta = waist[-1][1] - waist[-2][1] if waist_pair_valid else None
    waist_down = waist_delta is not None and waist_delta <= -0.3
    waist_base = state.get("waist_base_cm")
    waist_limit = state.get("waist_limit_cm")
    last_waist = waist[-1][1] if waist else None

    if fresh_weight:
        trend_missing_line = (
            "данных для тренда веса недостаточно (замеры редкие, с разрывом или "
            "из прошлой фазы) — попроси сделать ещё 1–2 замера в ближайшие дни, "
            "калории пока не корректируй"
        )
        if phase == "cut_recomp":
            target = params.get("target_weight_kg")
            if target and ma_now is not None and ma_now <= float(target):
                goal = (
                    f"цель фазы достигнута (средний вес {ma_now:.1f} ≤ {target:g} кг) — "
                    "предложи в rationale переход в lean_bulk; фазу переключает атлет"
                )
            if trend is None:
                lines.append(trend_missing_line)
            elif stalled_2w and fresh_waist and waist_down:
                lines.append(
                    "вес стоит ≥2 недель, но талия идёт вниз — это рекомп-бонус, калории НЕ снижай"
                )
            elif stalled_2w:
                lines.append("вес стоит ≥2 недель — посоветуй −100–150 ккал")
            else:
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
                and waist_pair_valid
                and waist_base
                and waist[-1][1] >= float(waist_base) + 1
                and waist[-2][1] >= float(waist_base) + 1
            ):
                lines.append(
                    f"талия +1 см от базовой ({waist_base:g} см) два замера подряд — "
                    "посоветуй −100–150 ккал или паузу набора"
                )
            elif trend is None:
                lines.append(trend_missing_line)
            elif fresh_waist and waist_pair_valid and not waist_down and 0.05 <= trend <= 0.25:
                lines.append("вес растёт в целевом темпе, талия стабильна — чистый набор, не трогай")
        else:  # maintenance
            if trend is None:
                lines.append(trend_missing_line)
            elif abs(trend) > 0.15:
                direction = "вверх" if trend > 0 else "вниз"
                lines.append(
                    f"вес уползает {direction} ({trend:+.2f} кг/нед) — мягкая коррекция "
                    "±100–150 ккал"
                )
            else:
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
    return adherence_between(workouts, today - timedelta(days=days - 1), today)


def adherence_between(
    workouts: list[dict[str, Any]], start: date, end: date
) -> dict[str, Any] | None:
    """Fact-vs-plan aggregate over an explicit [start, end] date range (used by
    the 30-day discipline window, phase summaries and the week_done signal)."""
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
