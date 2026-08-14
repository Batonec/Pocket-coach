#!/usr/bin/env python3
"""Coach signals for the История banner (see ios/COACH_SIGNALS_BANNER_BRIEF.md).

The server computes the full, already-collapsed, snooze-filtered, sorted list
on the fly from SQLite + coach_state — no cache, no LLM. The client renders
the first one or two items and never invents texts or compares dates itself.

Taxonomy (MVP): the «замеры» family (weight+waist collapsed, two stages), the
«тренировки» family (return_soon → return_mode escalation), deload_week,
weekly_report_ready and the positive week_done. Every signal dies on its own
(state change, escalation, TTL); dismissal keys are per-episode instance keys,
so «скрыть навсегда» does not exist.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

import coach_features
import coach_state

# Stage thresholds for the measurements family. Due starts on day 10, not 7:
# with a weekly cadence a day-7 start would light the banner every single week
# even for a disciplined athlete — guaranteed banner blindness.
MEASUREMENT_DUE_DAYS = 10
MEASUREMENT_OVERDUE_DAYS = coach_features.STALE_MEASUREMENT_DAYS  # 14: matrix cut-off

RETURN_SOON_FROM_DAYS = 11          # 1–3 days before the return-protocol threshold
RETURN_BREAK_DAYS = coach_state.BREAK_DAYS  # 14

REPORT_FRESH_HOURS = 48

WEEK_DONE_MIN_PCT = 90
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
        # Mockup glyph name: scale | tape | back | wave | doc | check. The
        # client maps unknown names to a neutral glyph.
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

    # Mockup titles name exactly what is stale — «Обнови талию — вес свежий».
    if set(stale) == {"вес", "талия"}:
        due_title = "Обнови замеры: вес и талия"
    elif stale == ["талия"]:
        due_title = "Обнови талию — вес свежий"
    else:
        due_title = "Обнови вес — талия свежая"

    if overdue:
        accusative = {"вес": "вес", "талия": "талию"}
        parts = " и ".join(accusative.get(part, part) for part in overdue)
        return _signal(
            "measurements_overdue", "measurements", "warn",
            "Советы по калориям на паузе",
            f"Внеси {parts} — вернутся",
            instance_fact=fact,
            action_type="open_measurements", action_label="Замеры", action_target=target,
            glyph="scale",
        )
    if due:
        worst = max(d for d in (weight_age, waist_age) if d is not None)
        days_left = max(1, MEASUREMENT_OVERDUE_DAYS + 1 - worst)
        body = f"Через {_plural_days(days_left)} советы по калориям встанут на паузу"
        # When fresh-ish data exists but the in-phase trend is still starving,
        # say why one more point matters instead of a separate nagging banner.
        note = None
        phase_start = None
        started_raw = state.get("phase_started")
        if isinstance(started_raw, str):
            try:
                phase_start = date.fromisoformat(started_raw)
            except ValueError:
                phase_start = None
        if coach_features.weight_trend_per_week(weight_points, today, since=phase_start) is None:
            note = "ещё пара точек — тренд оживёт"
        return _signal(
            "measurements_due", "measurements", "info",
            due_title, body,
            instance_fact=fact,
            action_type="open_measurements", action_label="Замеры", action_target=target,
            note=note, glyph="scale",
        )
    return None


def _trainings_signal(
    workouts: list[dict[str, Any]], today: date
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
            f"Потренируйся до {_ru_date(deadline)}",
            "Иначе следующая сессия — возвратная, ~85–90% весов",
            instance_fact=f"last_workout={last.isoformat()}",
            action_type="open_next_workout", action_label="План",
            glyph="back",
        )
    if days >= RETURN_BREAK_DAYS:
        return _signal(
            "return_mode", "trainings", "accent",
            "Возвратная сессия уже облегчена",
            "~85–90% весов. Просто приди, догонять ничего не надо",
            instance_fact=f"last_workout={last.isoformat()}",
            action_type="open_next_workout", action_label="План",
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
        "Разгрузочная неделя",
        "−30–40% объёма, веса рабочие",
        instance_fact=f"week={week_start.isoformat()}",
        action_type="open_next_workout", action_label="План",
        note="со следующей недели набор объёма заново",
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
    body = "Итоги, ПР, вес и питание"
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
        "Готов отчёт недели", body,
        instance_fact=f"period={report.get('period_end')}",
        action_type="open_weekly_report", action_label="Отчёт",
        glyph="doc",
    )


def _week_done_signal(
    workouts: list[dict[str, Any]], today: date
) -> dict[str, Any] | None:
    # The last CLOSED calendar week (Mon–Sun); shown Monday–Tuesday after it.
    week_start = today - timedelta(days=today.weekday() + 7)
    week_end = week_start + timedelta(days=6)
    if (today - week_end).days > WEEK_DONE_SHOW_DAYS:
        return None
    stats = coach_features.adherence_between(workouts, week_start, week_end)
    if (
        not stats
        or stats["sessions"] < WEEK_DONE_MIN_SESSIONS
        or stats["pct"] < WEEK_DONE_MIN_PCT
    ):
        return None
    return _signal(
        "week_done", "milestone", "positive",
        f"Неделя закрыта: {stats['pct']}% плана",
        "Так держать",
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

    workouts = store.list_workouts(user_id)
    body_weights = store.list_body_weights(user_id)
    waists = store.list_waists(user_id)

    candidates = [
        _measurements_signal(body_weights, waists, state, today),
        _trainings_signal(workouts, today),
        _deload_signal(state, workouts, today),
        _report_signal(store, user_id, workouts, now_ts),
        _week_done_signal(workouts, today),
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
    return active


def default_snooze_until(severity: str, now_ts: int) -> int | None:
    hours = SNOOZE_DEFAULT_HOURS.get(severity)
    return None if hours is None else now_ts + hours * 3600
