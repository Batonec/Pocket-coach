#!/usr/bin/env python3
"""Coach signals for the История banner (see docs/COACH_SIGNALS.md).

The server computes the full, already-collapsed, snooze-filtered, sorted list
on the fly from SQLite + coach_state and the cached recommendation status. It
never invokes the LLM. The client renders the first one or two items and never
invents texts or compares dates itself.

Taxonomy: the «замеры» family (weight+waist freshness collapsed, the
building-phase dead-trend nudge, plus the hard waist limit), the
«тренировки» family (return_soon → return_mode escalation),
deload_week, weekly_report_ready and the positive week_done. Every signal dies
on its own (state change, escalation, TTL); dismissal keys are per-episode
instance keys, so «скрыть навсегда» does not exist.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

import coach_features
import coach_prompts
import coach_state

# Stage thresholds for the measurements family. Due starts on day 10, not 7:
# with a weekly cadence a day-7 start would light the banner every single week
# even for a disciplined athlete — guaranteed banner blindness.
MEASUREMENT_DUE_DAYS = 10
MEASUREMENT_OVERDUE_DAYS = coach_features.STALE_MEASUREMENT_DAYS  # 14: matrix cut-off

# Building phases steer calories by the in-phase weight trend, and a dead trend
# is a cost TODAY even while the 14-day freshness gate is still green. Nudge
# from day 5: an earlier weigh-in cannot revive the trend anyway (the window
# needs a ≥5-day span between points), so day 5 is the first day a new point
# actually helps. From day 10 the due/overdue ladder takes over.
TREND_NUDGE_FROM_DAYS = 5

RETURN_SOON_FROM_DAYS = 11          # 1–3 days before the return-protocol threshold
RETURN_BREAK_DAYS = coach_state.BREAK_DAYS  # 14

REPORT_FRESH_HOURS = 48

WEEK_DONE_MIN_PCT = 90
# Нижняя граница: неделя с одной тренировкой закрытой не считается ни при какой
# фазе. Реальный порог берётся из sessions_per_week текущей фазы — поздравлять
# с закрытой неделей на половине плана значит обесценить сам баннер.
WEEK_DONE_MIN_SESSIONS = 2
WEEK_DONE_SHOW_DAYS = 2             # Monday–Tuesday after the closed week

SEVERITY_RANK = {"critical": 0, "warn": 1, "accent": 2, "info": 3, "positive": 4}
FAMILY_RANK = {"measurements": 0, "trainings": 1, "deload": 2, "report": 3, "milestone": 4}

# Default snooze per severity when the dismiss request names no hours.
# None = episodic: hidden until the instance_key changes (state moved on).
SNOOZE_DEFAULT_HOURS: dict[str, int | None] = {
    "info": 72,
    "warn": 48,
    "accent": None,
    "positive": None,
}

_RU_WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

# Тексты баннеров живут в copy/signals.md: пороги и жизненный цикл — здесь,
# копирайт — там. Ключ эпизода строится из фактов, поэтому правка текста не
# трогает ни схлопывание, ни дисмиссы.
_COPY = coach_prompts.fragments("signals", directory=coach_prompts.COPY_DIR)


def _text(name: str, **values: str) -> str:
    return coach_prompts.render(_COPY[name], **values)


def _signal(
    signal_id: str,
    family: str,
    severity: str,
    title: str,
    body: str,
    *,
    instance_fact: str,
    action_type: str | None,
    action_label: str | None = None,
    action_target: str | None = None,
    note: str | None = None,
    glyph: str | None = None,
) -> dict[str, Any]:
    action = None
    if action_type:
        action = {"type": action_type, "label": action_label or ""}
        if action_target:
            action["target"] = action_target
    return {
        "id": signal_id,
        "family": family,
        "instance_key": f"{signal_id}:{instance_fact}",
        "severity": severity,
        "title": title,
        "body": body,
        # Optional third line (italic, muted in the mockups) — e.g. the trend hint.
        "note": note,
        # Glyph name: scale | nutrition | tape | back | wave | doc | check.
        # The client maps unknown names to a neutral glyph.
        "glyph": glyph,
        "action": action,
        "snoozable": severity != "critical",
    }


_RU_MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _ru_date(day: date) -> str:
    return f"{day.day} {_RU_MONTHS_GEN[day.month - 1]}"


def _plural_days(days: int) -> str:
    return f"{days} дн."


# --------------------------------------------------------------------------- #
# Families
# --------------------------------------------------------------------------- #
def _waist_limit_signal(
    waists: list[dict[str, Any]],
    state: dict[str, Any],
    today: date,
) -> dict[str, Any] | None:
    """Critical hard-limit episode: two fresh in-phase readings at/above it.

    A single noisy tape reading must not change the athlete's phase. Restricting
    the pair to the current phase and requiring the latest point to be fresh
    also guarantees a deterministic auto-exit after a phase change, a lower
    reading, or measurement staleness.
    """
    if state.get("phase") != "lean_bulk":
        return None
    try:
        limit = float(state.get("waist_limit_cm"))
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None

    started_raw = state.get("phase_started")
    if not isinstance(started_raw, str):
        return None
    try:
        phase_start = date.fromisoformat(started_raw)
    except ValueError:
        return None

    points = [
        point for point in coach_features.waist_points(waists)
        if phase_start <= point[0] <= today
    ]
    if len(points) < 2:
        return None
    previous, latest = points[-2], points[-1]
    latest_age = (today - latest[0]).days
    if latest_age > MEASUREMENT_OVERDUE_DAYS:
        return None
    if previous[1] < limit or latest[1] < limit:
        return None

    return _signal(
        "waist_limit", "measurements", "critical",
        _text("waist_limit_title", waist=f"{latest[1]:g}", limit=f"{limit:g}"),
        _text("waist_limit_body"),
        instance_fact=(
            f"pair={previous[0].isoformat()}:{previous[1]:g},"
            f"{latest[0].isoformat()}:{latest[1]:g},limit={limit:g}"
        ),
        action_type="open_measurements", action_label=_text("action_measurements"),
        action_target="waist", glyph="tape",
    )


def _measurements_signal(
    body_weights: list[dict[str, Any]],
    waists: list[dict[str, Any]],
    state: dict[str, Any],
    today: date,
) -> dict[str, Any] | None:
    weight_points = coach_features.weight_points(body_weights)
    waist_points = coach_features.waist_points(waists)

    def age(points: list[tuple[date, float]]) -> int | None:
        return (today - points[-1][0]).days if points else None

    weight_age, waist_age = age(weight_points), age(waist_points)

    overdue: list[str] = []
    due: list[str] = []
    for name, days in (("вес", weight_age), ("талия", waist_age)):
        if days is None or days > MEASUREMENT_OVERDUE_DAYS:
            overdue.append(name)
        elif days >= MEASUREMENT_DUE_DAYS:
            due.append(name)

    last_weight = weight_points[-1][0].isoformat() if weight_points else "none"
    last_waist = waist_points[-1][0].isoformat() if waist_points else "none"
    fact = f"weight={last_weight},waist={last_waist}"
    stale = overdue + due
    target = "waist" if stale == ["талия"] else "weight"

    phase_start = None
    started_raw = state.get("phase_started")
    if isinstance(started_raw, str):
        try:
            phase_start = date.fromisoformat(started_raw)
        except ValueError:
            phase_start = None

    # Mockup titles name exactly what is stale — «Обнови талию — вес свежий».
    if set(stale) == {"вес", "талия"}:
        due_title = _text("measurements_due_title_both")
    elif stale == ["талия"]:
        due_title = _text("measurements_due_title_waist")
    else:
        due_title = _text("measurements_due_title_weight")

    if overdue:
        accusative = {
            "вес": _text("noun_accusative_weight"),
            "талия": _text("noun_accusative_waist"),
        }
        parts = _text("joiner_and").join(
            accusative.get(part, part) for part in overdue
        )
        return _signal(
            "measurements_overdue", "measurements", "warn",
            _text("measurements_overdue_title"),
            _text("measurements_overdue_body", what=parts),
            instance_fact=fact,
            action_type="open_measurements", action_label=_text("action_measurements"),
            action_target=target,
            glyph="nutrition",
        )
    if due:
        worst = max(d for d in (weight_age, waist_age) if d is not None)
        days_left = max(1, MEASUREMENT_OVERDUE_DAYS + 1 - worst)
        body = _text("measurements_due_body", days=_plural_days(days_left))
        # When fresh-ish data exists but the in-phase trend is still starving,
        # say why one more point matters instead of a separate nagging banner.
        note = None
        if coach_features.weight_trend_per_week(weight_points, today, since=phase_start) is None:
            note = _text("measurements_due_note")
        return _signal(
            "measurements_due", "measurements", "info",
            due_title, body,
            instance_fact=fact,
            action_type="open_measurements", action_label=_text("action_measurements"),
            action_target=target, note=note, glyph="scale",
        )

    # Building phases only: the freshness gate is still green, but the in-phase
    # weight trend — the signal the calorie matrix actually steers by — is not
    # computable, and today is the first day a new weigh-in can revive it.
    phase = state.get("phase")
    if (
        phase in ("cut_recomp", "lean_bulk")
        and weight_age is not None
        and TREND_NUDGE_FROM_DAYS <= weight_age < MEASUREMENT_DUE_DAYS
        and coach_features.weight_trend_per_week(weight_points, today, since=phase_start) is None
    ):
        goal = "среза" if phase == "cut_recomp" else "набора"
        return _signal(
            "weight_trend_stale", "measurements", "info",
            _text("weight_trend_stale_title"),
            _text("weight_trend_stale_body", goal=goal),
            instance_fact=f"weight={last_weight},phase={phase}",
            action_type="open_measurements", action_label=_text("action_measurements"),
            action_target="weight",
            glyph="scale",
        )
    return None


def _return_plan_state(recommendation: dict[str, Any] | None) -> str:
    """Map the cached recommendation row to the return banner's UI state."""
    if not recommendation:
        return "none"
    status = recommendation.get("status")
    if status in ("pending", "failed"):
        return str(status)
    if status != "ready":
        return "none"
    payload = recommendation.get("recommendation")
    context = payload.get("coach_context") if isinstance(payload, dict) else None
    if isinstance(context, dict) and context.get("return_from_break") is True:
        return "ready"
    return "outdated"


def _trainings_signal(
    workouts: list[dict[str, Any]],
    today: date,
    recommendation: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    dates = sorted(
        {
            when
            for when in (
                coach_features._workout_date(workout) for workout in workouts
            )
            if when is not None and when <= today
        }
    )
    if not dates:
        return None
    last = dates[-1]
    days = (today - last).days

    if RETURN_SOON_FROM_DAYS <= days < RETURN_BREAK_DAYS:
        deadline = last + timedelta(days=RETURN_BREAK_DAYS - 1)
        return _signal(
            "return_soon", "trainings", "warn",
            _text("return_soon_title", deadline=_ru_date(deadline)),
            _text("return_soon_body"),
            instance_fact=f"last_workout={last.isoformat()}",
            action_type="open_next_workout", action_label=_text("action_plan"),
            glyph="back",
        )
    if days >= RETURN_BREAK_DAYS:
        plan_state = _return_plan_state(recommendation)
        # Today already shows the generation state. A second banner on History
        # would only duplicate it and imply that an actionable plan exists.
        if plan_state == "pending":
            return None

        if plan_state == "ready":
            title = _text("return_mode_ready_title")
            body = ""
            action_type = "open_next_workout"
            action_label = _text("action_plan")
            recommendation_fact = "ready"
        else:
            title = _text("return_mode_pending_title")
            action_type = "refresh_recommendation"
            if plan_state == "failed":
                body = _text("return_mode_failed_body")
                action_label = _text("action_retry")
                recommendation_fact = (
                    f"failed:{recommendation.get('updated_at') or 'unknown'}"
                )
            elif plan_state == "outdated":
                body = _text("return_mode_outdated_body")
                action_label = _text("action_refresh")
                recommendation_fact = (
                    f"outdated:{recommendation.get('updated_at') or 'unknown'}"
                )
            else:
                body = _text("return_mode_none_body")
                action_label = _text("action_create")
                recommendation_fact = "none"

        return _signal(
            "return_mode", "trainings", "accent",
            title, body,
            instance_fact=(
                f"last_workout={last.isoformat()},recommendation={recommendation_fact}"
            ),
            action_type=action_type, action_label=action_label,
            glyph="back",
        )
    return None


def _deload_signal(
    state: dict[str, Any], workouts: list[dict[str, Any]], today: date
) -> dict[str, Any] | None:
    position = coach_state.cycle_position(state, workouts, today)
    if not position["deload_week"]:
        return None
    anchor = coach_state._block_anchor(state, workouts, today)
    if anchor is None:
        return None
    week_start = anchor + timedelta(days=(position["block_week"] - 1) * 7)
    trained_this_week = any(
        week_start <= when <= today
        for when in (
            coach_features._workout_date(workout) for workout in workouts
        )
        if when is not None
    )
    if trained_this_week:
        # The first deload session closes the banner; the plan card carries on.
        return None
    return _signal(
        "deload_week", "deload", "accent",
        _text("deload_title"),
        _text("deload_body"),
        instance_fact=f"week={week_start.isoformat()}",
        action_type="open_next_workout", action_label=_text("action_plan"),
        note=_text("deload_note"),
        glyph="wave",
    )


def _report_signal(
    store: Any,
    user_id: int,
    workouts: list[dict[str, Any]],
    now_ts: int,
) -> dict[str, Any] | None:
    report = store.get_latest_coach_report(user_id)
    if not report or report.get("read_at"):
        return None
    if now_ts - int(report.get("created_at") or 0) > REPORT_FRESH_HOURS * 3600:
        return None
    # Mockup body: «4–10 августа · 92% плана» — the covered period plus the
    # week's adherence when there was any planned work.
    try:
        period_end = date.fromisoformat(str(report.get("period_end")))
    except (TypeError, ValueError):
        period_end = None
    body = _text("report_body_default")
    if period_end is not None:
        days = int(report.get("days") or 7)
        period_start = period_end - timedelta(days=days - 1)
        if period_start.month == period_end.month:
            body = f"{period_start.day}–{_ru_date(period_end)}"
        else:
            body = f"{_ru_date(period_start)} – {_ru_date(period_end)}"
        stats = coach_features.adherence_between(workouts, period_start, period_end)
        if stats:
            body += f" · {stats['pct']}% плана"
    return _signal(
        "weekly_report_ready", "report", "info",
        _text("report_title"), body,
        instance_fact=f"period={report.get('period_end')}",
        action_type="open_weekly_report", action_label=_text("action_report"),
        glyph="doc",
    )


def _week_done_signal(
    workouts: list[dict[str, Any]], state: dict[str, Any], today: date
) -> dict[str, Any] | None:
    # The last CLOSED calendar week (Mon–Sun); shown Monday–Tuesday after it.
    week_start = today - timedelta(days=today.weekday() + 7)
    week_end = week_start + timedelta(days=6)
    if (today - week_end).days > WEEK_DONE_SHOW_DAYS:
        return None
    planned = coach_state.phase_params(state).get("sessions_per_week")
    needed = max(
        WEEK_DONE_MIN_SESSIONS,
        int(planned) if isinstance(planned, (int, float)) and not isinstance(planned, bool) else 0,
    )
    stats = coach_features.adherence_between(workouts, week_start, week_end)
    if (
        not stats
        or stats["sessions"] < needed
        or stats["pct"] < WEEK_DONE_MIN_PCT
    ):
        return None
    return _signal(
        "week_done", "milestone", "positive",
        _text("week_done_title", pct=str(stats["pct"])),
        _text("week_done_body"),
        instance_fact=f"week={week_start.isoformat()}",
        action_type=None,
        glyph="check",
    )


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
_MISSING = object()


def compute_signals(
    store: Any,
    user_id: int,
    state: dict[str, Any],
    *,
    today: date | None = None,
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    """The full sorted signal list after collapsing, snooze filtering and the
    positive-suppression rule. The client shows the first 1–2."""
    today = today or date.today()
    now_ts = now_ts or int(time.time())

    recommendation = store.get_recommendation(user_id)
    # A recommendation refresh changes the plan context that several banners
    # lead into. Do not mix the old banner snapshot with an in-flight plan:
    # History stays quiet until generation reaches a terminal state, then the
    # next request recomputes every family from fresh facts.
    if recommendation and recommendation.get("status") == "pending":
        return []

    workouts = store.list_workouts(user_id)
    body_weights = store.list_body_weights(user_id)
    waists = store.list_waists(user_id)

    # One family, one message: the critical waist-limit episode supersedes a
    # routine freshness reminder instead of stacking two measurement banners.
    measurements = (
        _waist_limit_signal(waists, state, today)
        or _measurements_signal(body_weights, waists, state, today)
    )
    candidates = [
        measurements,
        _trainings_signal(workouts, today, recommendation),
        _deload_signal(state, workouts, today),
        _report_signal(store, user_id, workouts, now_ts),
        _week_done_signal(workouts, state, today),
    ]
    signals = [signal for signal in candidates if signal is not None]

    snoozes = store.list_signal_snoozes(user_id)
    active: list[dict[str, Any]] = []
    for signal in signals:
        snooze = snoozes.get(signal["instance_key"], _MISSING)
        if snooze is not _MISSING:
            # A row with NULL until = episodic dismiss (hidden while the
            # episode lasts); a timestamp hides until it passes.
            if snooze is None or int(snooze) > now_ts:
                continue
        active.append(signal)

    # A celebration next to a warning celebrates nothing.
    if any(signal["severity"] in ("warn", "critical") for signal in active):
        active = [signal for signal in active if signal["severity"] != "positive"]

    active.sort(
        key=lambda signal: (
            SEVERITY_RANK.get(signal["severity"], 9),
            FAMILY_RANK.get(signal["family"], 9),
        )
    )

    # The info-grade trend nudge is easily masked: the client renders only the
    # first banner (the second slot opens under critical only), and e.g. during
    # a return period the accent return_mode legitimately outranks it. Instead
    # of silently losing the nudge, ride it as the muted third line of whatever
    # banner sits on top. Under a critical first the second slot is open, so
    # the nudge keeps its own card.
    trend = next(
        (signal for signal in active if signal["id"] == "weight_trend_stale"), None
    )
    if (
        trend is not None
        and active[0] is not trend
        and active[0]["severity"] != "critical"
    ):
        if not active[0].get("note"):
            active[0]["note"] = _text("weight_trend_collapsed_note")
        active.remove(trend)
    return active


def default_snooze_until(severity: str, now_ts: int) -> int | None:
    hours = SNOOZE_DEFAULT_HOURS.get(severity)
    return None if hours is None else now_ts + hours * 3600
