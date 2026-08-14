#!/usr/bin/env python3
"""Coach preparation-phase state machine + weekly hormone-cycle configuration.

The mutable coaching state (current phase, its start date, per-phase parameter
overrides, waist limit, injection day) lives in a small JSON file next to the
database — ``coach_state.json`` — the same pattern as ``coach_profile.json``.
The profile stays prose (who the athlete is); this file is structured state
(what the program is doing right now) and is switched via the Coach MCP tools,
never automatically.

Stdlib-only on purpose, like the rest of the backend.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

PHASES = ("cut_recomp", "lean_bulk", "maintenance")

# Break length that both triggers the return-from-break protocol and resets
# the volume ramp to the start of a new block.
BREAK_DAYS = 14

# Per-phase methodology defaults. Overridable per phase via state["phase_params"]
# (e.g. a custom target weight); everything the prompts say about the phase is
# generated from this table, so a change here changes the coach's behaviour.
PHASE_DEFAULTS: dict[str, dict[str, Any]] = {
    "cut_recomp": {
        "title": "лёгкий дефицит-рекомп",
        "calories": (2100, 2200),
        "rate_text": "−0.25…−0.35 кг/нед по недельной средней",
        "rate_kg_per_week": (-0.35, -0.25),
        "frequency_text": "3 тренировки в неделю (2–4 допустимо)",
        "session_sets": (14, 20),
        "ramp_start": (6, 8),
        "ramp_cap": (10, 14),
        "protein_g": (155, 165),
        # Reaching this (by the 7-day moving average) suggests moving to lean_bulk.
        "target_weight_kg": 75.5,
    },
    "lean_bulk": {
        "title": "lean bulk",
        "calories": (2400, 2500),
        "rate_text": "+0.5–0.8 кг/мес",
        "frequency_text": "3 тренировки в неделю (2–4 допустимо)",
        "session_sets": (14, 20),
        "ramp_start": (6, 8),
        "ramp_cap": (10, 16),
        "protein_g": (155, 165),
        # Reaching either ceiling suggests a mini-cut / phase change.
        "ceiling_weight_kg": 84.0,
    },
    "maintenance": {
        "title": "поддержание",
        "calories": (2300, 2400),
        "rate_text": "±0 кг (вес держим)",
        "frequency_text": "1 тренировка в неделю, fullbody heavy",
        "session_sets": (8, 12),
        # No volume ramp: a fixed 2–3 sets per group per week keeps strength;
        # weights are NOT reduced — intensity is the retention signal.
        "ramp_start": None,
        "ramp_cap": None,
        "sets_per_group": (2, 3),
        "protein_g": (150, 175),
    },
}

# --- weekly hormone cycle (HRT) ----------------------------------------------
# MEDICAL BOUNDARY (do not remove): this config exists ONLY to time training
# load around the weekly recovery background (peak vs trough). It must never
# grow dosage logic, HRT-scheme advice or lab interpretation — that is the
# treating physician's territory, and the prompts repeat the same boundary.
DEFAULT_INJECTION_DAY = 5  # Saturday (Monday=0)

_WEEKDAY_ALIASES = {
    "mon": 0, "пн": 0, "понедельник": 0,
    "tue": 1, "вт": 1, "вторник": 1,
    "wed": 2, "ср": 2, "среда": 2,
    "thu": 3, "чт": 3, "четверг": 3,
    "fri": 4, "пт": 4, "пятница": 4,
    "sat": 5, "сб": 5, "суббота": 5,
    "sun": 6, "вс": 6, "воскресенье": 6,
}

_RU_WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

DEFAULT_STATE: dict[str, Any] = {
    "schema": 1,
    "phase": "cut_recomp",
    "phase_started": None,          # ISO date; None → block week counts as 1
    "phase_params": {},             # per-phase overrides: {phase: {key: value}}
    "waist_limit_cm": None,         # hard aesthetic limit; set by the athlete
    "waist_base_cm": None,          # first measurement of the current phase
    "injection_day": DEFAULT_INJECTION_DAY,
}


def default_state_path(db_path: Path | str) -> Path:
    """coach_state.json lives next to the DB, like coach_profile.json."""
    return Path(
        os.getenv("COACH_STATE_PATH") or str(Path(db_path).parent / "coach_state.json")
    )


def parse_weekday(value: Any) -> int:
    """Accept 0–6 (Mon=0), or en/ru weekday names/abbreviations."""
    if isinstance(value, bool):
        raise ValueError("injection_day must be a weekday, not a bool")
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ValueError("injection_day as a number must be 0–6 (Monday=0)")
    text = str(value).strip().lower()
    if text[:3] in _WEEKDAY_ALIASES:
        return _WEEKDAY_ALIASES[text[:3]]
    if text in _WEEKDAY_ALIASES:
        return _WEEKDAY_ALIASES[text]
    raise ValueError(f"Не понял день недели: {value!r}")


def load_state(path: Path | str | None) -> dict[str, Any]:
    """Read the state file; a missing/broken file falls back to defaults so
    generation always works."""
    state = {key: (dict(value) if isinstance(value, dict) else value)
             for key, value in DEFAULT_STATE.items()}
    if not path:
        return state
    try:
        raw = json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return state
    if not isinstance(raw, dict):
        return state

    if raw.get("phase") in PHASES:
        state["phase"] = raw["phase"]
    started = raw.get("phase_started")
    if isinstance(started, str):
        try:
            date.fromisoformat(started)
            state["phase_started"] = started
        except ValueError:
            pass
    if isinstance(raw.get("phase_params"), dict):
        state["phase_params"] = raw["phase_params"]
    for key in ("waist_limit_cm", "waist_base_cm"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 40 <= value <= 200:
            state[key] = float(value)
    try:
        state["injection_day"] = parse_weekday(raw.get("injection_day", state["injection_day"]))
    except ValueError:
        pass
    return state


def save_state(path: Path | str, state: dict[str, Any]) -> None:
    Path(path).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )


_OVERRIDABLE_PARAM_KEYS = {
    "calories", "rate_text", "rate_kg_per_week", "frequency_text",
    "session_sets", "ramp_start", "ramp_cap", "sets_per_group", "protein_g",
    "target_weight_kg", "ceiling_weight_kg", "title",
}


def _normalize_param_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and all(
            isinstance(item, (int, float)) and not isinstance(item, bool) for item in value
        ):
            return (value[0], value[1])
        raise ValueError("Диапазон должен быть парой чисел [min, max]")
    if isinstance(value, bool) or value is None:
        raise ValueError("Параметр должен быть числом, строкой или парой чисел")
    if isinstance(value, (int, float, str)):
        return value
    raise ValueError("Параметр должен быть числом, строкой или парой чисел")


def set_phase(
    path: Path | str,
    phase: str,
    params: dict[str, Any] | None = None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Switch the phase by hand (called from the MCP tool). Never automatic:
    when a phase goal is reached the prompt asks the model to *suggest* the
    switch in the rationale — the athlete decides and calls this."""
    if phase not in PHASES:
        raise ValueError(f"Неизвестная фаза {phase!r}; допустимые: {', '.join(PHASES)}")
    state = load_state(path)
    state["phase"] = phase
    state["phase_started"] = (today or date.today()).isoformat()
    if params:
        if not isinstance(params, dict):
            raise ValueError("params должен быть объектом {ключ: значение}")
        clean: dict[str, Any] = {}
        for key, value in params.items():
            if key not in _OVERRIDABLE_PARAM_KEYS:
                raise ValueError(
                    f"Неизвестный параметр {key!r}; допустимые: "
                    f"{', '.join(sorted(_OVERRIDABLE_PARAM_KEYS))}"
                )
            clean[key] = _normalize_param_value(value)
        phase_params = dict(state.get("phase_params") or {})
        phase_params[phase] = clean
        state["phase_params"] = phase_params
    save_state(path, state)
    return state


def phase_params(state: dict[str, Any]) -> dict[str, Any]:
    """Defaults for the current phase merged with the athlete's overrides."""
    phase = state.get("phase") if state.get("phase") in PHASES else "cut_recomp"
    merged = dict(PHASE_DEFAULTS[phase])
    overrides = (state.get("phase_params") or {}).get(phase) or {}
    for key, value in overrides.items():
        if key in _OVERRIDABLE_PARAM_KEYS:
            merged[key] = tuple(value) if isinstance(value, list) else value
    merged["phase"] = phase
    return merged


# --------------------------------------------------------------------------- #
# Block weeks and the volume ramp
# --------------------------------------------------------------------------- #
def _workout_dates(workouts: list[dict[str, Any]]) -> list[date]:
    dates: set[date] = set()
    for workout in workouts:
        try:
            dates.add(date.fromisoformat(str(workout.get("workout_date", ""))))
        except ValueError:
            continue
    return sorted(dates)


def is_return_from_break(workouts: list[dict[str, Any]], today: date) -> bool:
    """Computed, never stored: the athlete is coming back after >=14 days off."""
    dates = _workout_dates(workouts)
    if not dates:
        return False
    return (today - dates[-1]).days >= BREAK_DAYS


def block_week(state: dict[str, Any], workouts: list[dict[str, Any]], today: date) -> int:
    """1-based training-block week. The block anchor is the later of the phase
    start and the first workout after the most recent >=14-day gap (a long
    break resets the ramp, consistent with the return-from-break rule). While
    currently ON a break, the coming session opens a new block → week 1."""
    if is_return_from_break(workouts, today):
        return 1

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
        for previous, current in zip(dates, dates[1:]):
            if (current - previous).days >= BREAK_DAYS:
                ramp_anchor = current
        anchor = max(anchor, ramp_anchor) if anchor else ramp_anchor
    if anchor is None or anchor > today:
        return 1
    return (today - anchor).days // 7 + 1


def weekly_volume_target(state: dict[str, Any], week: int) -> tuple[int, int] | None:
    """Target work-sets-per-big-group corridor for the given block week.

    Building phases ramp from ``ramp_start`` by +1 (lower bound) / +2 (upper
    bound) sets per week up to the phase cap. Maintenance has no ramp (fixed
    2–3 sets/group) → None."""
    params = phase_params(state)
    start, cap = params.get("ramp_start"), params.get("ramp_cap")
    if not start or not cap:
        return None
    week = max(1, int(week))
    low = min(start[0] + (week - 1), cap[0])
    high = min(start[1] + 2 * (week - 1), cap[1])
    return int(low), int(high)


# --------------------------------------------------------------------------- #
# Weekly hormone cycle (timing only — see the medical boundary note above)
# --------------------------------------------------------------------------- #
def cycle_info(state: dict[str, Any], today: date) -> dict[str, Any]:
    """Where `today` sits in the weekly injection cycle.

    Day 1 = injection day. Peak background: days +1..+3 after the injection;
    trough: the day before and the day of the next injection. Changing the
    injection day is a one-parameter edit in coach_state.json."""
    injection_day = state.get("injection_day", DEFAULT_INJECTION_DAY)
    if not isinstance(injection_day, int) or not 0 <= injection_day <= 6:
        injection_day = DEFAULT_INJECTION_DAY
    day = (today.weekday() - injection_day) % 7 + 1  # 1..7, 1 = injection day
    if day in (2, 3, 4):
        level = "высокий (пик)"
    elif day in (5, 6):
        level = "средний"
    else:  # day 7 (eve of injection) and day 1 (injection day)
        level = "минимальный"
    peak = f"{_RU_WEEKDAYS_SHORT[(injection_day + 1) % 7]}–{_RU_WEEKDAYS_SHORT[(injection_day + 3) % 7]}"
    trough = f"{_RU_WEEKDAYS_SHORT[(injection_day - 1) % 7]}–{_RU_WEEKDAYS_SHORT[injection_day]}"
    return {
        "day": day,
        "level": level,
        "injection_weekday": injection_day,
        "peak_days": peak,
        "trough_days": trough,
    }
