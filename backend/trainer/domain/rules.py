#!/usr/bin/env python3
"""Правила формы и границ входных данных.

Всё, что сервер решает о присланном клиентом или MCP, до того как оно попадёт
в базу: какие поля и в каких пределах допустимы у тренировки, замера и
события, что считается валидной датой, чем ограничен снапшот совета.
Хранилище (``data/backend_store``) зовёт эти функции и записывает то, что
они вернули; само оно ничего не решает.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import date, timedelta
from typing import Any

ALLOWED_LOAD_TYPES = {"heavy", "medium", "light", "deload"}
ALLOWED_SET_EFFORTS = {"easy", "ok", "hard"}


def normalize_load_type(value: object) -> str | None:
    """Keep an explicitly provided load label; otherwise store None ([?] in the
    coach prompt). The old tonnage fallback (≥3000 kg → heavy) labeled nearly
    every real session heavy — a fabricated signal is worse than an honest
    unknown, and the coach model judges session heaviness from weights/reps
    anyway."""
    if isinstance(value, str) and value in ALLOWED_LOAD_TYPES:
        return value
    return None


def normalize_notes(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_set_effort(value: object) -> str | None:
    if value is None:
        return None

    text = str(value).strip().lower()
    if not text:
        return None
    if text not in ALLOWED_SET_EFFORTS:
        raise ValueError("Set effort must be one of easy, ok, hard")
    return text


def normalize_set_rir(value: object) -> int | None:
    """Optional reps-in-reserve (0–4), usually recorded on the last set of an
    exercise. More precise than the effort marks; the coach prompt prefers it."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("Set rir must be an integer between 0 and 4")
    try:
        rir = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Set rir must be an integer between 0 and 4") from exc
    if not 0 <= rir <= 4:
        raise ValueError("Set rir must be an integer between 0 and 4")
    return rir


MAX_RECOMMENDATION_SNAPSHOT_BYTES = 8192


def _snapshot_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _snapshot_str(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def normalize_recommendation_snapshot(value: object) -> dict[str, Any] | None:
    """Sanitize the optional coach-recommendation snapshot a client attaches to
    a workout (``data.recommendation``) for actual-vs-recommended stats.

    Best-effort by design: anything malformed yields ``None`` and the workout
    saves without a snapshot — linkage must never fail a save.
    """
    if not isinstance(value, dict):
        return None

    raw_exercises = value.get("exercises")
    if not isinstance(raw_exercises, list) or not raw_exercises:
        return None

    exercises: list[dict[str, Any]] = []
    for raw_exercise in raw_exercises[:10]:
        if not isinstance(raw_exercise, dict):
            continue
        exercise_id = _snapshot_int(raw_exercise.get("exercise_id"))
        raw_sets = raw_exercise.get("sets")
        if exercise_id is None or not isinstance(raw_sets, list):
            continue

        sets: list[dict[str, Any]] = []
        for raw_set in raw_sets[:12]:
            if not isinstance(raw_set, dict):
                continue
            try:
                reps = int(raw_set.get("reps"))
                weight = float(raw_set.get("weight"))
            except (TypeError, ValueError):
                continue
            if reps < 1:
                continue
            sets.append({"reps": min(reps, 1000), "weight": min(max(weight, 0.0), 10000.0)})

        if sets:
            exercises.append(
                {
                    "exercise_id": exercise_id,
                    "name": _snapshot_str(raw_exercise.get("name"), 120) or "",
                    "sets": sets,
                }
            )

    if not exercises:
        return None

    load_type = value.get("load_type")
    snapshot = {
        "schema": _snapshot_int(value.get("schema")) or 1,
        "source": _snapshot_str(value.get("source"), 32) or "coach",
        "model": _snapshot_str(value.get("model"), 64),
        "generated_at": _snapshot_int(value.get("generated_at")),
        "applied_at": _snapshot_str(value.get("applied_at"), 40),
        "based_on_workout_id": _snapshot_int(value.get("based_on_workout_id")),
        "based_on_workout_count": _snapshot_int(value.get("based_on_workout_count")),
        "focus": _snapshot_str(value.get("focus"), 200),
        "load_type": load_type if load_type in ALLOWED_LOAD_TYPES else None,
        "exercises": exercises,
    }

    if len(json.dumps(snapshot, ensure_ascii=False)) > MAX_RECOMMENDATION_SNAPSHOT_BYTES:
        return None
    return snapshot


def normalize_workout_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    raw_exercises = payload.get("data", {}).get("exercises", [])
    if not isinstance(raw_exercises, list) or not raw_exercises:
        raise ValueError("Workout must contain at least one exercise")

    normalized_exercises: list[dict[str, Any]] = []
    for raw_exercise in raw_exercises:
        if not isinstance(raw_exercise, dict):
            raise ValueError("Exercise payload must be an object")

        exercise_id = raw_exercise.get("exercise_id")
        if not isinstance(exercise_id, int):
            raise ValueError("exercise_id must be an integer")

        exercise_name = str(raw_exercise.get("name", "")).strip()
        if not exercise_name:
            raise ValueError("Exercise name is required")

        raw_sets = raw_exercise.get("sets", [])
        if not isinstance(raw_sets, list) or not raw_sets:
            raise ValueError("Each exercise must contain at least one set")

        normalized_sets: list[dict[str, Any]] = []
        for index, raw_set in enumerate(raw_sets, start=1):
            if not isinstance(raw_set, dict):
                raise ValueError("Set payload must be an object")

            try:
                reps = int(raw_set.get("reps", 0))
                weight = float(raw_set.get("weight", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("Set reps and weight must be numeric") from exc

            if reps < 1:
                raise ValueError("Set reps must be at least 1")
            if weight < 0:
                raise ValueError("Set weight must be zero or positive")

            normalized_sets.append(
                {
                    "set_index": index,
                    "reps": reps,
                    "weight": weight,
                    "effort": normalize_set_effort(raw_set.get("effort")),
                    "rir": normalize_set_rir(raw_set.get("rir")),
                    "notes": normalize_notes(raw_set.get("notes")),
                }
            )

        normalized_exercises.append(
            {
                "exercise_id": exercise_id,
                "name": exercise_name,
                "sets": normalized_sets,
            }
        )

    workout_date = _parse_input_date(payload.get("workout_date"), "workout_date").isoformat()

    client_id = str(payload.get("client_id") or payload.get("id") or "").strip()
    if not client_id:
        raise ValueError("client_id is required")

    data = payload.get("data", {})
    normalized_payload = {
        "workout_date": workout_date,
        "plan_id": None,
        "data": {
            "focus": None,
            "notes": normalize_notes(data.get("notes")),
            "load_type": normalize_load_type(data.get("load_type")),
            "exercises": normalized_exercises,
        },
    }

    recommendation = normalize_recommendation_snapshot(data.get("recommendation"))
    if recommendation is not None:
        normalized_payload["data"]["recommendation"] = recommendation

    return normalized_payload, client_id


# Plausible human body-weight bounds. Outside this range an entry is almost
# certainly a data-entry slip (e.g. an exercise weight typed into the weigh-in
# field — that is how 22kg readings appeared for an 77kg athlete). Rejected at
# write time so the garbage never reaches the DB or the coach's calorie logic.
MIN_BODY_WEIGHT_KG = 30.0
MAX_BODY_WEIGHT_KG = 400.0


def normalize_body_weight_payload(payload: dict[str, Any]) -> dict[str, Any]:
    entry_date = _parse_input_date(payload.get("entry_date"), "entry_date").isoformat()

    try:
        weight = float(payload.get("weight", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("weight must be numeric") from exc

    if weight <= 0:
        raise ValueError("weight must be greater than 0")
    if not (MIN_BODY_WEIGHT_KG <= weight <= MAX_BODY_WEIGHT_KG):
        raise ValueError(
            f"weight must be between {MIN_BODY_WEIGHT_KG:g} and {MAX_BODY_WEIGHT_KG:g} kg"
        )

    return {
        "entry_date": entry_date,
        "weight": weight,
        "notes": normalize_notes(payload.get("notes")),
    }


# The write contract must match coach_features.waist_points exactly. Accepting
# a value here and silently dropping it from calorie advice later creates an
# impossible state: the user sees a saved measurement while the coach sees
# "waist=none" and keeps the stale-measurement warning alive.
MIN_WAIST_CM = 50.0
MAX_WAIST_CM = 160.0


def normalize_waist_payload(payload: dict[str, Any]) -> dict[str, Any]:
    entry_date = _parse_input_date(payload.get("entry_date"), "entry_date").isoformat()

    try:
        waist = float(payload.get("waist", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("waist must be numeric") from exc

    if not (MIN_WAIST_CM <= waist <= MAX_WAIST_CM):
        raise ValueError(f"waist must be between {MIN_WAIST_CM:g} and {MAX_WAIST_CM:g} cm")

    return {
        "entry_date": entry_date,
        "waist": waist,
        "notes": normalize_notes(payload.get("notes")),
    }


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_input_date(value: object, field: str) -> date:
    """Дата с клиента — строго YYYY-MM-DD, одинаково на любом интерпретаторе.

    `date.fromisoformat` в 3.11+ принимает и «20260801», и недельные формы, а на
    VPS (Python 3.10) — только с дефисами. Без регулярки один и тот же ввод
    проходил бы в разработке и отвергался на проде; с ней формат один, и
    периоды сравниваются и сортируются как строки без канонизации.
    """
    text = str(value or "").strip()
    if not _ISO_DATE.fullmatch(text):
        raise ValueError(f"{field} must be in YYYY-MM-DD format")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:  # форма верная, даты такой нет: 2026-02-30
        raise ValueError(f"{field} must be in YYYY-MM-DD format") from exc


def _normalize_event_date(value: object, field: str) -> str:
    """Дата события: формат + запрет будущего (планирование отложено, событие
    описывает то, что уже случилось). «Сегодня» — локальный день сервера; VPS
    живёт в Europe/Moscow, то есть в том же дне, что и атлет, как и весь
    остальной слой коуча.
    """
    parsed = _parse_input_date(value, field)
    if parsed > date.today():
        raise ValueError(f"{field} must not be in the future")
    return parsed.isoformat()


def normalize_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    start_date = _normalize_event_date(payload.get("start_date"), "start_date")

    raw_end_date = payload.get("end_date")
    # Пустая строка от клиента — это «ещё идёт», а не кривая дата.
    if raw_end_date is None or not str(raw_end_date).strip():
        end_date = None
    else:
        end_date = _normalize_event_date(raw_end_date, "end_date")
        if end_date < start_date:
            raise ValueError("end_date must not be earlier than start_date")

    text = normalize_notes(payload.get("text"))
    if text is None:
        raise ValueError("text is required")

    return {
        "start_date": start_date,
        "end_date": end_date,
        "text": text,
    }


# --------------------------------------------------------------------------- #
# Решения при записи: что сохранить, когда запись уже есть
# --------------------------------------------------------------------------- #
def retry_backfills_snapshot(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any] | None:
    """Повтор POST с тем же client_id (офлайн-ретрай): сохранённая тренировка
    остаётся как есть, но если повтор принёс снапшот совета, а в записи его нет,
    снапшот дописывается — иначе первая, сорвавшаяся попытка молча теряет связку
    тренировка ↔ совет. Существующий снапшот не перезаписывается никогда.
    Возвращает payload для записи или None, если менять нечего."""
    incoming_snapshot = (incoming.get("data") or {}).get("recommendation")
    if incoming_snapshot is None or (existing.get("data") or {}).get("recommendation") is not None:
        return None
    patched = copy.deepcopy(existing)
    patched.setdefault("data", {})["recommendation"] = incoming_snapshot
    return patched


def edit_keeps_snapshot(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """PUT без снапшота в payload сохраняет уже записанный: клиент пересобирает
    payload из черновика при редактировании и иначе стёр бы связку с советом."""
    if "recommendation" not in (incoming.get("data") or {}):
        existing_snapshot = (existing.get("data") or {}).get("recommendation")
        if existing_snapshot is not None:
            incoming.setdefault("data", {})["recommendation"] = existing_snapshot
    return incoming


def check_single_open_event(another_is_open: bool) -> None:
    """Открытое событие — это состояние «сейчас не тренируюсь», и оно одно:
    автозакрытие тренировкой не смогло бы выбрать, какой период закрывать."""
    if another_is_open:
        raise ValueError("Another event is still open — close it before opening a new one")


def open_event_end_after_workout(workout_date: str, created: bool, today: date) -> str | None:
    """Новая сегодняшняя тренировка закрывает открытое событие вчерашним днём:
    перерыв кончился в тот день, когда атлет снова пришёл в зал. Правка
    тренировки и запись задним числом состояние не переключают — история
    меняется, «сейчас не тренируюсь» остаётся как было."""
    if created and workout_date == today.isoformat():
        return (today - timedelta(days=1)).isoformat()
    return None


def closed_event_end(start_date: str, closed_on: str) -> str:
    """Автозакрытие ставит вчерашний день, а событие могло начаться сегодня
    («заболел утром, вечером всё же потренировался»): такое закрывается
    однодневным периодом, а не концом раньше начала. Даты канонические,
    поэтому max по строкам — это max по времени."""
    return max(closed_on, start_date)
