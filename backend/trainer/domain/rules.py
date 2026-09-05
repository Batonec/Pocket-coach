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
import math
import re
from datetime import date, timedelta
from typing import Any

ALLOWED_LOAD_TYPES = {"heavy", "medium", "light", "deload"}
ALLOWED_SET_EFFORTS = {"easy", "ok", "hard"}


def normalize_load_type(value: object) -> str | None:
    """Метка нагрузки сессии: оставить явно присланную, иначе ``None`` («без плана»
    в промпте тренера).

    Старый фолбэк по тоннажу (≥3000 кг → heavy) помечал тяжёлой почти каждую
    реальную сессию. Выдуманный сигнал хуже честного «неизвестно», а тяжесть
    сессии модель и так видит по весам и повторам.
    """
    if isinstance(value, str) and value in ALLOWED_LOAD_TYPES:
        return value
    return None


def normalize_notes(value: object) -> str | None:
    """Свободный текст: обрезать пробелы, пустую строку превратить в ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_set_effort(value: object) -> str | None:
    """Тяжесть подхода: ``easy`` / ``ok`` / ``hard`` без учёта регистра; пусто —
    ``None``, что-то другое — ``ValueError``.
    """
    if value is None:
        return None

    text = str(value).strip().lower()
    if not text:
        return None
    if text not in ALLOWED_SET_EFFORTS:
        raise ValueError("Set effort must be one of easy, ok, hard")
    return text


def normalize_set_rir(value: object) -> int | None:
    """Повторы в запасе (RIR, 0–4), обычно на последнем подходе упражнения.

    Точнее меток тяжести; промпт тренера предпочитает его. Пусто — ``None``,
    не целое или вне 0–4 — ``ValueError``.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("Set rir must be an integer between 0 and 4")
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise ValueError("Set rir must be an integer between 0 and 4")
    try:
        rir = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Set rir must be an integer between 0 and 4") from exc
    if not 0 <= rir <= 4:
        raise ValueError("Set rir must be an integer between 0 and 4")
    return rir


MAX_RECOMMENDATION_SNAPSHOT_BYTES = 8192


def _snapshot_int(value: object) -> int | None:
    """Целое из снапшота или ``None``; ``bool`` не считается целым."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _snapshot_str(value: object, limit: int) -> str | None:
    """Строка из снапшота, обрезанная до ``limit`` символов, или ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def normalize_recommendation_snapshot(value: object) -> dict[str, Any] | None:
    """Санитизация снапшота совета, который клиент прикладывает к тренировке
    (``data.recommendation``) ради статистики «факт против плана».

    Намеренно best-effort: любая кривизна даёт ``None``, и тренировка сохраняется
    без снапшота — связка не имеет права уронить запись. Белый список полей,
    ≤10 упражнений, ≤12 подходов, ≤8 КБ.
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
            if isinstance(raw_set.get("reps"), bool) or isinstance(raw_set.get("weight"), bool):
                continue
            raw_reps = raw_set.get("reps")
            if isinstance(raw_reps, float) and not raw_reps.is_integer():
                continue
            try:
                reps = int(raw_reps)
                weight = float(raw_set.get("weight"))
            except (TypeError, ValueError, OverflowError):
                continue
            if reps < 1 or not math.isfinite(weight):
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

    encoded = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_RECOMMENDATION_SNAPSHOT_BYTES:
        return None
    return snapshot


def normalize_workout_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Тренировка с клиента в форму для базы: ``(payload, client_id)``.

    Проверяет каждое упражнение и подход (id, имя, повторы ≥1, вес ≥0, тяжесть,
    RIR, заметки), дату по ``YYYY-MM-DD`` и обязательный ``client_id`` — по нему
    стор дедуплицирует офлайн-ретраи. Снапшот совета прикладывается, если прошёл
    санитизацию. Любая ошибка — ``ValueError`` с текстом для клиента. Зовёт
    ``backend_store.save_workout`` и ``update_workout``.
    """
    data = payload.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("Workout data must be an object")
    raw_exercises = data.get("exercises", [])
    if not isinstance(raw_exercises, list) or not raw_exercises:
        raise ValueError("Workout must contain at least one exercise")

    normalized_exercises: list[dict[str, Any]] = []
    for raw_exercise in raw_exercises:
        if not isinstance(raw_exercise, dict):
            raise ValueError("Exercise payload must be an object")

        exercise_id = raw_exercise.get("exercise_id")
        if not isinstance(exercise_id, int) or isinstance(exercise_id, bool):
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

            raw_reps = raw_set.get("reps", 0)
            if isinstance(raw_reps, bool) or isinstance(raw_set.get("weight"), bool):
                raise ValueError("Set reps and weight must be numeric")
            if isinstance(raw_reps, float) and not raw_reps.is_integer():
                raise ValueError("Set reps must be an integer")
            try:
                reps = int(raw_reps)
                weight = float(raw_set.get("weight", 0))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Set reps and weight must be numeric") from exc

            if reps < 1:
                raise ValueError("Set reps must be at least 1")
            if not math.isfinite(weight):
                raise ValueError("Set weight must be finite")
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


# Правдоподобные границы веса тела. За ними запись почти наверняка описка:
# вес с тренажёра, вбитый в поле взвешивания, — так у атлета в 77 кг появлялись
# замеры по 22 кг. Отсекается на записи, чтобы мусор не доехал ни до базы,
# ни до расчёта калорий у коуча.
MIN_BODY_WEIGHT_KG = 30.0
MAX_BODY_WEIGHT_KG = 400.0


def normalize_body_weight_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Замер веса тела в форму для базы: дата, вес в границах 30–400 кг, заметка.

    Вне границ — ``ValueError``: такое значение почти наверняка описка. Зовёт
    ``backend_store.save_body_weight``.
    """
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


# Границы записи обязаны совпадать с coach_features.waist_points. Принять
# значение здесь и молча выбросить его из совета по калориям потом — значит
# создать невозможное состояние: пользователь видит сохранённый замер, а коуч
# видит «талии нет» и держит предупреждение о старом замере.
MIN_WAIST_CM = 50.0
MAX_WAIST_CM = 160.0


def normalize_waist_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Замер талии в форму для базы: дата, обхват в границах 50–160 см, заметка.

    Границы те же, что у аналитики (``coach_features``): значение не может быть
    «сохранено в UI, но проигнорировано коучем». Зовёт ``backend_store.save_waist``.
    """
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
    """Событие (период без тренировок с причиной) в форму для базы.

    Даты в прошлом или сегодня, конец не раньше начала, пустой конец — событие
    ещё идёт, текст обязателен. Зовут ``backend_store.save_event`` и
    ``update_event``.
    """
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
