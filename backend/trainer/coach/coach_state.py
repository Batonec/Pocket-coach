#!/usr/bin/env python3
"""Coach preparation-phase state machine.

The mutable coaching state (current phase, its start date, per-phase parameter
overrides, waist limits) lives in a small JSON file next to the database —
``coach_state.json`` — the same pattern as ``coach_profile.json``. The profile
stays prose (who the athlete is); this file is structured state (what the
program is doing right now) and is switched via the Coach MCP tools, never
automatically.

Stdlib-only on purpose, like the rest of the backend.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from itertools import pairwise
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
        "sessions_per_week": 3,
        "session_sets": (14, 20),
        "ramp_start": (6, 8),
        "ramp_cap": (10, 14),
        "protein_g": (155, 165),
        # Planned deload cadence: N accumulation weeks, then one light week.
        "deload_every_weeks": 6,
        # Reaching this (by the 7-day moving average) suggests moving to lean_bulk.
        "target_weight_kg": 75.5,
    },
    "lean_bulk": {
        "title": "lean bulk",
        "calories": (2400, 2500),
        "rate_text": "+0.5–0.8 кг/мес",
        # ≈ +0.1…+0.2 кг/нед: the nutrition matrix and the stall preconditions
        # steer by this corridor, not by the phase name.
        "rate_kg_per_week": (0.1, 0.2),
        "frequency_text": "3 тренировки в неделю (2–4 допустимо)",
        "sessions_per_week": 3,
        "session_sets": (14, 20),
        "ramp_start": (6, 8),
        "ramp_cap": (10, 16),
        "protein_g": (155, 165),
        "deload_every_weeks": 6,
        # Reaching either ceiling suggests a mini-cut / phase change.
        "ceiling_weight_kg": 84.0,
    },
    "maintenance": {
        "title": "поддержание",
        "calories": (2300, 2400),
        "rate_text": "±0 кг (вес держим)",
        # Zero-width corridor: the matrix adds its own ±0.15 tolerance.
        "rate_kg_per_week": (0.0, 0.0),
        "frequency_text": "1 тренировка в неделю, fullbody heavy",
        "sessions_per_week": 1,
        "session_sets": (8, 12),
        # No volume ramp: a fixed 2–3 sets per group per week keeps strength;
        # weights are NOT reduced — intensity is the retention signal.
        "ramp_start": None,
        "ramp_cap": None,
        "sets_per_group": (2, 3),
        "protein_g": (150, 175),
    },
}

# MEDICAL BOUNDARY (do not remove): the coach layer never grows dosage logic,
# HRT-scheme advice or lab interpretation — that is the treating physician's
# territory, and the prompts repeat the same boundary. The athlete's hormonal
# context lives in the prose profile only; planning does not schedule around
# the injection cycle (supraphysiological background all week — day-to-day
# timing is speculative and recovery/history always dominate anyway).
DEFAULT_STATE: dict[str, Any] = {
    "schema": 1,
    "phase": "cut_recomp",
    "phase_started": None,  # ISO date; None → block week counts as 1
    "phase_params": {},  # per-phase overrides: {phase: {key: value}}
    "phase_history": [],  # closed phases: [{phase, started, ended}]
    "waist_limit_cm": None,  # hard aesthetic limit; set by the athlete
    "waist_base_cm": None,  # first measurement of the current phase
}


def _valid_iso_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def default_state_path(db_path: Path | str) -> Path:
    """coach_state.json lives next to the DB, like coach_profile.json."""
    return Path(os.getenv("COACH_STATE_PATH") or str(Path(db_path).parent / "coach_state.json"))


def load_state(path: Path | str | None) -> dict[str, Any]:
    """Read the state file; a missing/broken file falls back to defaults so
    generation always works."""
    state = {
        key: (dict(value) if isinstance(value, dict) else value)
        for key, value in DEFAULT_STATE.items()
    }
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
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 40 <= value <= 200:
            state[key] = float(value)
    # Legacy files may still carry injection_day — ignored: planning no longer
    # schedules around the injection cycle.
    return state


def save_state(path: Path | str, state: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", "utf-8")


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


def _normalize_group_targets(value: Any) -> dict[str, tuple[float, float]]:
    from trainer.coach import coach_features

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
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in bounds)
        ):
            raise ValueError(f"Цель группы {group!r} должна быть парой чисел [min, max]")
        clean[group] = (bounds[0], bounds[1])
    return clean


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
    today = today or date.today()
    # Close the outgoing phase into the history journal — the phase-summary
    # tool derives all its numbers from workouts/measurements by date range,
    # so the journal only needs the boundaries.
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
            clean[key] = (
                _normalize_group_targets(value)
                if key == _GROUP_TARGET_KEY
                else _normalize_param_value(value)
            )
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
        if key not in _OVERRIDABLE_PARAM_KEYS:
            continue
        if key == _GROUP_TARGET_KEY:
            merged[key] = {group: tuple(bounds) for group, bounds in (value or {}).items()}
        else:
            merged[key] = tuple(value) if isinstance(value, list) else value
    merged["phase"] = phase
    return merged


# --------------------------------------------------------------------------- #
# The reporting week
# --------------------------------------------------------------------------- #
def last_closed_week_end(today: date) -> date:
    """Воскресенье последней ЗАКРЫТОЙ календарной недели (пн–вс).

    Единственный источник правды о том, какую неделю описывает недельный отчёт:
    его зовут и генератор (`weekly_report.py`), и чтение кэша в Coach MCP.
    Разъедутся — таймер запишет отчёт под одну дату, а инструмент будет искать
    под другую, промахнётся мимо кэша и молча сожжёт токены на перегенерацию.

    Неделя считается закрытой только когда она прошла целиком: в воскресенье
    отчёт всё ещё про предыдущую неделю, а про текущую появляется в ночь на
    понедельник. Отчёт по недожитой неделе — то, ради чего это и заведено:
    вечерняя тренировка воскресенья в него не попадала."""
    return today - timedelta(days=today.weekday() + 1)


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


def _block_anchor(
    state: dict[str, Any], workouts: list[dict[str, Any]], today: date
) -> date | None:
    """The block anchor is the later of the phase start and the first workout
    after the most recent >=14-day gap (a long break resets the ramp,
    consistent with the return-from-break rule)."""
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
    """1-based training-block week. While currently ON a break, the coming
    session opens a new block → week 1."""
    if is_return_from_break(workouts, today):
        return 1
    anchor = _block_anchor(state, workouts, today)
    if anchor is None:
        return 1
    return (today - anchor).days // 7 + 1


# Fatigue actually has to be accumulated for a planned deload to make sense:
# on average >=2 sessions per accumulation week, else the light weeks already
# happened by themselves and the flag is withheld.
DELOAD_MIN_SESSIONS_PER_WEEK = 2


def cycle_position(
    state: dict[str, Any], workouts: list[dict[str, Any]], today: date
) -> dict[str, Any]:
    """Where the current block week sits inside the accumulate→deload cycle.

    Building phases run `deload_every_weeks` accumulation weeks and then one
    planned light week (−30–40% volume); after it the ramp restarts, so the
    cycle length is N+1 and `cycle_week` is the block week modulo that. The
    deload flag fires only when the athlete actually trained the block in."""
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
