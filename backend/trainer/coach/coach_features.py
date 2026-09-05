#!/usr/bin/env python3
"""Computed history features for the coach prompt.

The model used to receive 20 raw workouts and re-derive records, stalls and
volumes on every call — anchoring on whatever the recent sessions happened to
be. This module pre-computes those facts on the server so the prompt feeds the
model *data*, not homework:

- per-exercise all-time summaries (top set, Epley e1RM, last PR, recent sessions
  with the movement's position in each session);
- a stall detector with explicit "resource exhausted" preconditions, measured
  over the ACTIVE window of the current block (never across a vacation);
- attendance by calendar week (the programme's gate and split switch);
- return-from-break ramp steps (current → PRE-BREAK working weight, not peak);
- weekly volume per muscle group in direct AND effective sets (secondary load);
- body-weight / waist trends and the nutrition decision matrix keyed on the
  phase's weight-rate corridor.

Stdlib-only, like the rest of the backend.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

from trainer.coach import coach_state
from trainer.coach.coach_state import BREAK_DAYS

# Catalog id 1 («Жим гор.») and id 18 («Жим в тренажере») are the same machine;
# old history rows still carry id 1, so every consumer maps through this alias.
EXERCISE_ALIASES: dict[int, int] = {1: 18}

# Assisted pull-ups: the weight field is the COUNTERWEIGHT (assistance), so
# progress is the weight going DOWN — every comparison below is inverted.
# The machine left the catalog in Aug 2026 (the athlete's gym has none, and the
# history never used it), but the inverted-progress support stays: it is the
# only place that knows how to read a "lower is better" weight column, and any
# future assisted machine plugs straight into it.
GRAVITRON_ID = 4

# Base movements that get an explicit comeback ramp after a break; isolation
# just follows the working-weight rule.
MAIN_MOVEMENT_IDS = (18, 9, 10, 8, GRAVITRON_ID)

# Primary muscle group per (canonical) exercise id.
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

# Effective weekly sets: direct work counts 1.0 for the primary group, and the
# compounds feed secondary muscles a fraction of a set (presses → triceps ~half;
# every pull → biceps ~half). The press credits «дельты» only 0.25: a horizontal
# press loads the FRONT delt, while the group's only direct machine measures the
# mid delt — a half-set credit would overstate coverage of the visible head.
# The leg press's extra glute share is folded into the combined quad/glute
# group, so it stays 1.0 there. The horizontal row credits «задняя дельта» 0.25:
# rowing does load the posterior head, but not enough to replace direct work.
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

# Weekly direct-set landmarks for the small groups (the coaching policy from
# the system prompt); big groups follow the block-week ramp instead.
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
    """Per-group weekly set targets for the client's volume screen.

    Without an override: big groups follow the current block-week corridor
    (ramp/deload), small groups keep their policy ranges, maintenance flattens
    everything to 2–3. A single corridor for every big group is the default
    precisely because the default methodology has no per-group priorities.

    ``group_targets`` from the athlete's phase parameters overrides that for the
    named groups: a programme where back gets 16 sets and quads 9 cannot be
    expressed by one corridor. The override states the MATURE block; the ramp of
    the current week comes from the block week and from the programme text, so
    no scaling rule is invented here.
    """
    override = {
        group: tuple(bounds)
        for group, bounds in (group_targets or {}).items()
        if group in MUSCLE_GROUPS and isinstance(bounds, (list, tuple)) and len(bounds) == 2
    }
    targets: dict[str, tuple[int, int]] = {}
    for group in MUSCLE_GROUPS:
        if group in override:
            targets[group] = override[group]
        elif maintenance_sets:
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
# Everything is measured over the ACTIVE window: at most 6 weeks back, but never
# across the start of the current block (phase start / return after a ≥14-day
# break). A window that includes a vacation reports a frequency the athlete
# never had and a volume he never trained — the model then builds fullbody
# sessions «по дефициту» that does not exist.
STALL_WINDOW_DAYS = 42  # 6 weeks
STALL_MIN_WEEKLY_FREQUENCY = 2.5  # sessions per week over the active window
STALL_MIN_WEEKLY_SETS = 10.0  # per BIG group when the phase names no group target
STALL_MIN_WINDOW_DAYS = 21  # under three weeks there is nothing to judge yet
STALL_NO_PR_DAYS = 28  # ≥4 weeks without an improvement INSIDE the window
STALL_MIN_EXERCISE_SESSIONS = 3  # can't stall a lift you barely visit
# Weight-rate tolerance around the phase corridor (kg/week) shared by the
# preconditions and the nutrition matrix.
RATE_TOLERANCE = 0.15

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


def _beats(top: dict[str, Any], best: dict[str, Any], *, inverted: bool) -> bool:
    """Is `top` a better set than `best`: higher e1RM, or for the gravitron a
    lower counterweight (more reps break the tie)? One comparison for the
    summary, the peak and the in-window stall clock — they must not drift."""
    if inverted:
        return top["weight"] < best["weight"] or (
            top["weight"] == best["weight"] and top["reps"] > best["reps"]
        )
    return epley_e1rm(top["weight"], top["reps"]) > epley_e1rm(best["weight"], best["reps"]) + 1e-9


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
        if best is None or _beats(top, best, inverted=inverted):
            best, best_when = top, when
    return best, best_when


def _exercise_positions(workouts: list[dict[str, Any]]) -> dict[tuple[date, int], tuple[int, int]]:
    """{(date, canonical exercise id): (position, exercises in that session)}.

    The working weight depends on WHERE the movement stood: a row done first
    on fresh legs and the same row sixth after a leg press are two different
    numbers, and the model must not read the second as lost strength."""
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
    mark = _EFFORT_MARK.get(workout_set.get("effort") or "", "")
    rir = workout_set.get("rir")
    rir_repr = f"@{int(rir)}" if isinstance(rir, (int, float)) and not isinstance(rir, bool) else ""
    return f"{workout_set['weight']:g}×{workout_set['reps']}{mark}{rir_repr}"


def _position_tag(position: tuple[int, int] | None) -> str:
    """«[#3/7] » — the movement was third of seven in that session."""
    if position is None:
        return ""
    index, total = position
    return f"[#{index}/{total}] "


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
    positions = _exercise_positions(workouts)

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
            if _beats(top, best, inverted=inverted):
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
    group_targets: dict[str, Any] | None = None,
) -> str:
    targets = (
        group_volume_targets(week_target, maintenance_sets, group_targets) if group_targets else {}
    )
    lines = []
    for group, counts in volume.items():
        effective = f"{counts['effective']:g}"
        goal = targets.get(group)
        if goal:
            # The goal is stated in DIRECT sets — that is how the programme's
            # table counts — so it stands next to the direct number, and the
            # effective count is labelled as the reference it is. One number
            # in the wrong column and the model picks the smaller of two goals.
            line = (
                f"  {group}: {counts['direct']} прямых (цель {goal[0]:g}–{goal[1]:g}) / "
                f"{effective} эффективных (справочно)"
            )
        else:
            line = f"  {group}: {counts['direct']} прямых / {effective} эффективных"
        lines.append(line)
    if group_targets:
        lines.append(
            "  Цели — в ПРЯМЫХ сетах и на объём ЗРЕЛОГО блока по программе; на неделях "
            "разгона идём к ним снизу, ориентир недели — в разделе ПРОГРАММА. Эффективные "
            "сеты справочные: показывают, сколько косвенной работы группа уже получила, "
            "но цель не закрывают."
        )
    elif week_target:
        small = ", ".join(
            f"{group} {low}–{high}" for group, (low, high) in SMALL_GROUP_TARGETS.items()
        )
        lines.append(
            f"  Цель этой недели блока для крупных групп: {week_target[0]}–{week_target[1]} "
            f"эффективных сетов; ориентиры малых групп (прямых): {small}."
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
def _rate_bounds(rate_range: Any, phase: str) -> tuple[float, float]:
    """The phase's weekly weight corridor as floats. A caller without an
    explicit range gets the defaults of that phase — every phase has one, so
    no branch below has to key on the phase NAME."""
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
    """(sessions inside the window, days since the last improvement there).

    The all-time PR date is the wrong stall clock after a break: the athlete
    sits legitimately below a peak set months ago while climbing back session
    by session, and «ПР 110 дн. назад» would flag every lift the day the
    preconditions turn green. Progress is therefore measured INSIDE the
    active window — the first session there is the baseline, each later
    session that beats the best-so-far is an improvement, and the clock runs
    from the last one."""
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
    """Preconditions first: a plateau only counts as «ресурс исчерпан» when the
    athlete actually trained enough, ate enough and slept the volume in. If the
    preconditions are red, the flag is withheld ON PURPOSE — the model must
    explain the plateau through attendance/food, not through a «потолок».

    `since` is the start of the current block (phase start, or the first
    session after a ≥14-day break): the window never reaches across it, so a
    vacation cannot dilute the frequency. Volume thresholds are the lower
    bounds of the phase's own per-group targets when it names them — a quad
    target of 8–10 must not be judged against a flat 10."""
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
            # Gaining: only a FALLING weight says the surplus is not there.
            if trend < low - RATE_TOLERANCE:
                reasons.append(f"вес падает ({trend:+.2f} кг/нед при коридоре {corridor})")
        elif not (low - RATE_TOLERANCE <= trend <= high + RATE_TOLERANCE):
            # Cutting or holding: the weight has to stay inside its corridor.
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


def render_stall_report(report: dict[str, Any]) -> str:
    """Two lines: the facts of the active window (always — «фактическая
    частота приходит в данных» is a promise the programme header makes), then
    the verdict on preconditions and stall."""
    volume = ", ".join(
        f"{group} {value:.1f} (порог {threshold:g})"
        for group, (value, threshold) in report["volume_per_week"].items()
    )
    facts = (
        f"Активное окно {report['window_days']} дн. (с {report['window_start'].isoformat()}; "
        "перерыв ≥14 дней и прошлая фаза в него не входят): "
        f"частота {report['frequency']:.1f}/нед; прямых сетов/нед: {volume}."
    )
    if report["too_short"]:
        verdict = (
            f"Окно короче {STALL_MIN_WINDOW_DAYS} дней — предусловия прогресса и застой "
            "пока не оцениваются."
        )
    elif not report["preconditions_ok"]:
        verdict = (
            "Предусловия прогресса НЕ выполнены ("
            + "; ".join(report["reasons"])
            + ") — плато, если оно есть, объясняй посещаемостью/питанием, а не потолком."
        )
    elif report["stalled"]:
        names = ", ".join(
            f"{s['name']} (без прироста {s['quiet_days']} дн.)" for s in report["stalled"]
        )
        verdict = (
            f"ЗАСТОЙ при выполненных предусловиях (частота/объём/питание в норме): {names}. "
            "Предложи deload −10% с разгоном или вариацию по этим движениям."
        )
    elif report["window_days"] < STALL_NO_PR_DAYS:
        verdict = (
            f"Предусловия прогресса выполнены; окну меньше {STALL_NO_PR_DAYS} дней — "
            "застой ещё не оценивается."
        )
    else:
        verdict = "Предусловия прогресса выполнены, застоя по упражнениям нет."
    return f"{facts}\n{verdict}"


# --------------------------------------------------------------------------- #
# Attendance by calendar week (the gate's «явка» and the split switch)
# --------------------------------------------------------------------------- #
def weekly_attendance(
    workouts: list[dict[str, Any]], today: date, weeks: int = 4
) -> list[dict[str, Any]]:
    """Training days per calendar week (Mon–Sun): the last `weeks` closed
    weeks plus the current one, oldest first. Pure calendar facts — events
    that explain an empty week stay in the chronicle, no number here reads
    them."""
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
    """Consecutive CLOSED weeks, counted back from the latest closed one, with
    at least `minimum` sessions each. The running week is not counted: a
    Wednesday cannot fail a week that still has four days."""
    streak = 0
    for row in reversed(rows):
        if not row["closed"]:
            continue
        if row["sessions"] < minimum:
            break
        streak += 1
    return streak


def render_weekly_attendance(rows: list[dict[str, Any]], today: date) -> str:
    parts = []
    for row in rows:
        label = f"{row['start'].isoformat()}…{row['end'].isoformat()}"
        if not row["closed"]:
            label += f" (текущая, по {today.isoformat()})"
        parts.append(f"{label}: {row['sessions']}")
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# Return-from-break ramp steps (4.3)
# --------------------------------------------------------------------------- #
def _top_weight_diffs(sessions: list[tuple[date, list[dict[str, Any]]]]) -> list[float]:
    tops = [_session_top(sets, inverted=False)["weight"] for _, sets in sessions]
    return [round(abs(b - a), 2) for a, b in pairwise(tops) if abs(b - a) > 0.01]


def _weight_granularity(sessions: list[tuple[date, list[dict[str, Any]]]]) -> float:
    """The machine's plate granularity: the smallest weight change ever made on
    it. Ramp rungs must be multiples of it — a 7.5-kg rung on a 5-kg-plate
    machine cannot be loaded."""
    diffs = _top_weight_diffs(sessions)
    if not diffs:
        return 2.5
    return max(0.5, min(diffs))


# Ramp-ladder rules. Rungs are the machine's own plate step, even where that
# step is coarser than the programme's «≤10%» (a 10-kg plate on an 80-kg leg
# press is a real rung — growing by reps between rungs is the model's call).
# The one rebuild: a LONE rung above this jump is not a ladder but a history
# artefact (the athlete once skipped plates), and equal thirds serve better.
# More rungs than the cap are compressed into equal steps: the ladder is a
# hint for the next few weeks, not a session-by-session script.
_RAMP_LONE_JUMP = 0.20
_RAMP_MAX_RUNGS = 6
# The return ladder is a fact about the current block: once the comeback is
# older than this, an unregained weight is ordinary history, not a ramp.
RETURN_LADDER_DAYS = 56


def _round_half(value: float) -> float:
    return round(value * 2) / 2


def _equal_steps(current: float, peak: float, count: int, granularity: float = 2.5) -> list[float]:
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


def _lone_jump(current: float, steps: list[float], inverted: bool) -> bool:
    """A single rung that jumps more than _RAMP_LONE_JUMP of the current weight."""
    if len(steps) != 1 or current <= 0:
        return False
    jump = (current - steps[0]) / current if inverted else (steps[0] - current) / current
    return jump > _RAMP_LONE_JUMP + 1e-9


def last_break(
    workouts: list[dict[str, Any]], min_days: int = BREAK_DAYS
) -> tuple[date, date] | None:
    """(last session before, first session after) the most recent gap of at
    least `min_days` between two LOGGED sessions — the break the athlete has
    already come back from. A break still running (no session after it yet)
    is `coach_state.is_return_from_break` territory, not reported here."""
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
    """For each main movement still below its PRE-BREAK working weight: return
    steps current → pre-break working, one machine step at a time.

    The target is what the athlete actually lifted before the pause, not the
    all-time peak: a peak set months earlier at another frequency says nothing
    about today's reserve, and the programme brings peaks back by ordinary
    progression later. The ladder exists only while there is something to
    regain — a movement back at its pre-break weight prints nothing — and only
    for a young comeback (RETURN_LADDER_DAYS). On the comeback day itself the
    block stays empty: the pre-break weights are printed as the reference and
    the model chooses the entry."""
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

        # Rung = the machine's own plate granularity (the stack step).
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
    """Each exercise's working weight as of the last session before the break.

    Pure data, no coaching opinion: HOW far below these the comeback session
    should sit is the model's call (it sees the lay-off length, the athlete's
    profile and the recovery context). The server only states what the athlete
    was actually lifting, and — via the validator — that a comeback session is
    not the place for a new PR. `until` looks at the history as it stood on
    that day (the last pre-break session) for a comeback already under way."""
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


def render_pre_break_weights(items: list[dict[str, Any]], break_days: int) -> str | None:
    if not items:
        return None
    lines = [
        f"  {item['name']}: {item['last_working']:g}" + (" противовеса" if item["inverted"] else "")
        for item in items
    ]
    return (
        f"Рабочие веса в последней сессии ПЕРЕД перерывом ({break_days} дн. "
        "назад). Это форма до паузы, а не сегодняшняя: отметки усилия и RIR в "
        "истории тоже относятся к тем сессиям. Насколько снизить вход и как "
        "быстро возвращаться к этим весам — решай сам по принципам возврата "
        "после перерыва.\n" + "\n".join(lines)
    )


def render_comeback_ramp(items: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in items:
        arrow = " → ".join(f"{step:g}" for step in item["steps"])
        unit = " противовеса" if item["inverted"] else ""
        lines.append(
            f"  {item['name']}: доперерывный рабочий {item['target']:g}{unit}, "
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
    return _measurement_points(waists, "waist", MIN_PLAUSIBLE_WAIST_CM, MAX_PLAUSIBLE_WAIST_CM)


def moving_average(
    points: list[tuple[date, float]], on_day: date, window_days: int = 7
) -> float | None:
    window = [value for when, value in points if 0 <= (on_day - when).days < window_days]
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
        if (today - p[0]).days <= TREND_WINDOW_DAYS and (since is None or p[0] >= since)
    ]
    if len(window) < 2:
        return None
    for previous, current in pairwise(window):
        if (current[0] - previous[0]).days > TREND_MAX_GAP_DAYS:
            return None
    span_days = (window[-1][0] - window[0][0]).days
    if span_days < TREND_MIN_SPAN_DAYS:
        return None
    # Least-squares slope over EVERY point in the window, not the two end
    # points: with sparse weigh-ins one heavy morning at either end would
    # otherwise define the whole week. With two points this is the same line.
    xs = [(when - window[0][0]).days for when, _ in window]
    ys = [value for _, value in window]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sxx
    return slope * 7


# The athlete's protocol decides by the 7-day mean; fewer points than this in
# a week make that mean a coin toss, and the prompt says so out loud.
WEEKLY_MEAN_MIN_POINTS = 4


def weigh_ins_in_window(points: list[tuple[date, float]], today: date, days: int = 7) -> int:
    return sum(1 for when, _ in points if 0 <= (today - when).days < days)


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
    weights = [p for p in weight_points(body_weights) if phase_start is None or p[0] >= phase_start]
    waist = [p for p in waist_points(waists) if phase_start is None or p[0] >= phase_start]

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
    waist_pair_valid = len(waist) >= 2 and (waist[-1][0] - waist[-2][0]).days <= TREND_MAX_GAP_DAYS
    waist_delta = waist[-1][1] - waist[-2][1] if waist_pair_valid else None
    waist_down = waist_delta is not None and waist_delta <= -0.3
    waist_base = state.get("waist_base_cm")
    waist_limit = state.get("waist_limit_cm")
    last_waist = waist[-1][1] if waist else None

    # The branches key on the phase's weekly weight CORRIDOR, never on its
    # name: «Ф0 · возврат» is a cut_recomp that asks to HOLD weight, and a
    # matrix reading the name would cut calories for the correct behaviour.
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
        # Phase goals: reached → the model SUGGESTS the switch, the athlete decides.
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

        # On a gain the waist speaks first: a hard limit or a creeping waist
        # decides the calories regardless of what the scale says.
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
    """The weight-trend branch against the phase corridor.

    A calorie change needs the deviation confirmed by two independent
    readings — the 3-week slope AND the difference of the 7-day means two
    weeks apart — which is what «две недели подряд» means without the server
    keeping a history of its own verdicts. Unconfirmed, the line names the
    deviation and asks for daily weigh-ins instead of a number."""
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
        count = weigh_ins_in_window(weights, today)
        line = f"Вес тела: {tail}. Дней с последнего замера: {age}. Замеров за последние 7 дней: {count}"
        line += (
            f" (для недельной средней нужно ≥{WEEKLY_MEAN_MIN_POINTS})."
            if count < WEEKLY_MEAN_MIN_POINTS
            else "."
        )
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
