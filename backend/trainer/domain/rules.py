#!/usr/bin/env python3
"""Правила формы и границ входных данных.

Всё, что сервер решает о присланном клиентом, MCP или моделью, до того как
оно попадёт в базу или к атлету: какие поля допустимы у тренировки, замера и
события, что считается валидной датой, как санитизируются снапшот совета и
план, который вернула модель. Сами пределы — словари меток, диапазоны и
потолки — лежат в ``limits`` с объяснением каждого числа. Хранилище
(``data/backend_store``) и ``recommender`` зовут эти функции и берут то, что
они вернули; сами они ничего не решают.

Механика разбора — типы, границы, тексты ошибок — вынесена в ``parsing``,
чтобы здесь остались только правила и решения, то есть сама методика.
"""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from typing import Any

from trainer.data.parsing import (
    as_choice,
    as_date,
    as_float,
    as_id,
    as_int,
    as_list,
    as_object,
    as_past_date,
    as_text,
    maybe_int,
    required_text,
)
from trainer.domain import coach_features, limits


def normalize_load_type(value: object) -> str | None:
    """Метка нагрузки сессии: оставить явно присланную, иначе ``None`` («без плана»
    в промпте тренера).

    Старый фолбэк по тоннажу (≥3000 кг → heavy) помечал тяжёлой почти каждую
    реальную сессию. Выдуманный сигнал хуже честного «неизвестно», а тяжесть
    сессии модель и так видит по весам и повторам.
    """
    return value if isinstance(value, str) and value in limits.LOGGED_LOAD_TYPES else None


def normalize_set_effort(value: object) -> str | None:
    """Тяжесть подхода: ``easy`` / ``ok`` / ``hard`` без учёта регистра."""
    return as_choice(value, "Set effort", limits.ALLOWED_SET_EFFORTS)


def normalize_set_rir(value: Any) -> int | None:
    """Повторы в запасе (RIR, 0–4), обычно на последнем подходе упражнения.

    Точнее меток тяжести; промпт тренера предпочитает его. Пусто — ``None``.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return as_int(value, "Set rir", minimum=limits.MIN_SET_RIR, maximum=limits.MAX_SET_RIR)


# --------------------------------------------------------------------------- #
# Снапшот совета: копия плана, приложенная к тренировке
# --------------------------------------------------------------------------- #
def _snapshot_text(value: object, limit: int) -> str | None:
    """Строка из снапшота: не строка — мусор, поле опускается.

    Здесь, в отличие от заметки атлета, приводить типы незачем: снапшот — эхо
    нашего же ответа, а не то, что печатал человек.
    """
    return as_text(value, limit=limit) if isinstance(value, str) else None


def _clamped_set(raw: object, *, max_reps: int, max_weight: float) -> dict[str, Any] | None:
    """Подход из плана-эха — снапшота или ответа модели — либо ``None``, если он
    кривой: не объект, повторы не целое от единицы, вес не конечное число.
    Величины не отвергаются, а зажимаются: в копии совета важна форма, а не
    точность; потолки у двух источников свои, из ``limits``.
    """
    if not isinstance(raw, dict):
        return None
    try:
        reps = as_int(raw.get("reps"), "reps", minimum=1)
        weight = as_float(raw.get("weight"), "weight")
    except ValueError:
        return None
    return {"reps": min(reps, max_reps), "weight": min(max(weight, 0.0), max_weight)}


def _snapshot_exercise(raw: object) -> dict[str, Any] | None:
    """Упражнение из снапшота или ``None``, если в нём не осталось подходов."""
    if not isinstance(raw, dict) or not isinstance(raw.get("sets"), list):
        return None
    exercise_id = maybe_int(raw.get("exercise_id"))
    sets = [
        normalized
        for normalized in (
            _clamped_set(
                item, max_reps=limits.SNAPSHOT_MAX_REPS, max_weight=limits.SNAPSHOT_MAX_WEIGHT
            )
            for item in raw["sets"][: limits.MAX_SNAPSHOT_SETS]
        )
        if normalized is not None
    ]
    if exercise_id is None or not sets:
        return None
    return {
        "exercise_id": exercise_id,
        "name": _snapshot_text(raw.get("name"), limits.SNAPSHOT_TEXT_LIMITS["name"]) or "",
        "sets": sets,
    }


def normalize_recommendation_snapshot(value: object) -> dict[str, Any] | None:
    """Санитизация снапшота совета, который клиент прикладывает к тренировке
    (``data.recommendation``) ради статистики «факт против плана».

    Намеренно best-effort: любая кривизна даёт ``None``, и тренировка сохраняется
    без снапшота — связка не имеет права уронить запись. Белый список полей и
    потолки из ``limits``: упражнения, подходы, длины строк, байты.
    """
    if not isinstance(value, dict) or not isinstance(value.get("exercises"), list):
        return None

    exercises = [
        normalized
        for normalized in (
            _snapshot_exercise(item) for item in value["exercises"][: limits.MAX_SNAPSHOT_EXERCISES]
        )
        if normalized is not None
    ]
    if not exercises:
        return None

    caps = limits.SNAPSHOT_TEXT_LIMITS
    snapshot = {
        "schema": maybe_int(value.get("schema")) or 1,
        "source": _snapshot_text(value.get("source"), caps["source"]) or "coach",
        "model": _snapshot_text(value.get("model"), caps["model"]),
        "generated_at": maybe_int(value.get("generated_at")),
        "applied_at": _snapshot_text(value.get("applied_at"), caps["applied_at"]),
        "based_on_workout_id": maybe_int(value.get("based_on_workout_id")),
        "based_on_workout_count": maybe_int(value.get("based_on_workout_count")),
        "focus": _snapshot_text(value.get("focus"), caps["focus"]),
        "load_type": normalize_load_type(value.get("load_type")),
        "exercises": exercises,
    }

    encoded = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    return snapshot if len(encoded) <= limits.MAX_RECOMMENDATION_SNAPSHOT_BYTES else None


# --------------------------------------------------------------------------- #
# План от модели: форма ответа поверх JSON-схемы
# --------------------------------------------------------------------------- #
def _plan_exercise(raw: object, names: dict[int, str]) -> dict[str, Any] | None:
    """Упражнение из ответа модели или ``None``: не объект, id не каноническая
    строка каталога, ни одного годного подхода. Имя — из каталога, а не то, что
    модель повторила; алиас id (1 → 18) переводится в канонический, хотя enum
    схемы его и так не предлагает. Годных подходов не больше
    ``MAX_SETS_PER_EXERCISE``: кривые пропускаются, а не занимают место.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("sets"), list):
        return None
    exercise_id = coach_features.canonical_exercise_id(raw.get("exercise_id"))
    if exercise_id is None or exercise_id not in names:
        return None
    sets = [
        normalized
        for normalized in (
            _clamped_set(item, max_reps=limits.MAX_REPS, max_weight=limits.MAX_WEIGHT)
            for item in raw["sets"]
        )
        if normalized is not None
    ][: limits.MAX_SETS_PER_EXERCISE]
    if not sets:
        return None
    return {
        "exercise_id": exercise_id,
        "name": names[exercise_id],
        "note": as_text(raw.get("note")) or "",
        "sets": sets,
    }


def normalize_model_plan(raw: dict[str, Any], catalog: list[dict[str, Any]]) -> dict[str, Any]:
    """Форма плана, который вернула модель, поверх JSON-схемы.

    Схема не умеет числовых диапазонов, поэтому повторы, веса, число подходов и
    упражнений клампятся и фильтруются после разбора. Это гигиена, а не методика:
    жёсткие границы держит ``plan_validator``. Метка нагрузки только из
    ``PLANNED_LOAD_TYPES`` (иначе ``medium``); ``rest_days`` — целое в
    0..``MAX_REST_DAYS``, кривое или пустое — 1, молча и без репромпта;
    упражнения — только с каноническими id каталога и хотя бы одним годным
    подходом, не больше ``MAX_EXERCISES``, имя каталожное. Ни одного упражнения —
    ``ValueError``: для генерации это провал, а не пустой совет (``recommender``
    переводит его в ``RecommendationError``).
    """
    names = {
        item["id"]: item["name"]
        for item in catalog
        if item["id"] not in coach_features.EXERCISE_ALIASES
    }
    load_type = raw.get("load_type")
    if load_type not in limits.PLANNED_LOAD_TYPES:
        load_type = "medium"
    try:
        rest_days = min(max(as_int(raw.get("rest_days"), "rest_days"), 0), limits.MAX_REST_DAYS)
    except ValueError:
        rest_days = 1
    raw_exercises = raw.get("exercises")
    exercises = [
        normalized
        for normalized in (
            _plan_exercise(item, names)
            for item in (raw_exercises if isinstance(raw_exercises, list) else [])
        )
        if normalized is not None
    ][: limits.MAX_EXERCISES]
    if not exercises:
        raise ValueError("Модель не предложила ни одного валидного упражнения")
    return {
        "focus": as_text(raw.get("focus")) or "",
        "load_type": load_type,
        "rest_days": rest_days,
        "rationale": as_text(raw.get("rationale")) or "",
        "exercises": exercises,
    }


# --------------------------------------------------------------------------- #
# Тренировка
# --------------------------------------------------------------------------- #
def _normalize_set(raw: object, index: int) -> dict[str, Any]:
    """Подход: повторы от одного, вес от нуля, тяжесть, RIR и заметка."""
    raw_set = as_object(raw, "Set payload")
    return {
        "set_index": index,
        "reps": as_int(raw_set.get("reps", 0), "Set reps", minimum=1),
        "weight": as_float(raw_set.get("weight", 0), "Set weight", minimum=0),
        "effort": normalize_set_effort(raw_set.get("effort")),
        "rir": normalize_set_rir(raw_set.get("rir")),
        "notes": as_text(raw_set.get("notes")),
    }


def _normalize_exercise(raw: object) -> dict[str, Any]:
    """Упражнение: id из каталога, имя и хотя бы один подход."""
    exercise = as_object(raw, "Exercise payload")
    exercise_id = as_id(exercise.get("exercise_id"), "exercise_id")
    name = required_text(exercise.get("name"), "Exercise name")
    sets = as_list(exercise.get("sets", []), "Each exercise", item="set")
    return {
        "exercise_id": exercise_id,
        "name": name,
        "sets": [_normalize_set(item, index) for index, item in enumerate(sets, start=1)],
    }


def normalize_workout_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Тренировка с клиента в форму для базы: ``(payload, client_id)``.

    Проверяет каждое упражнение и подход, дату по ``YYYY-MM-DD`` и обязательный
    ``client_id`` — по нему стор дедуплицирует офлайн-ретраи. Снапшот совета
    прикладывается, если прошёл санитизацию. Любая ошибка — ``ValueError`` с
    текстом для клиента. Зовёт ``backend_store.save_workout`` и ``update_workout``.
    """
    data = as_object(payload.get("data", {}), "Workout data")
    exercises = [
        _normalize_exercise(item)
        for item in as_list(data.get("exercises", []), "Workout", item="exercise")
    ]
    workout_date = as_date(payload.get("workout_date"), "workout_date").isoformat()
    client_id = required_text(payload.get("client_id") or payload.get("id"), "client_id")

    normalized: dict[str, Any] = {
        "workout_date": workout_date,
        "plan_id": None,
        "data": {
            "focus": None,
            "notes": as_text(data.get("notes")),
            "load_type": normalize_load_type(data.get("load_type")),
            "exercises": exercises,
        },
    }

    recommendation = normalize_recommendation_snapshot(data.get("recommendation"))
    if recommendation is not None:
        normalized["data"]["recommendation"] = recommendation

    return normalized, client_id


# --------------------------------------------------------------------------- #
# Замеры
# --------------------------------------------------------------------------- #
def normalize_body_weight_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Замер веса тела в форму для базы: дата, вес в границах записи из ``limits``,
    заметка.

    Вне границ — ``ValueError``: такое значение почти наверняка описка. Зовёт
    ``backend_store.save_body_weight``.
    """
    return {
        "entry_date": as_date(payload.get("entry_date"), "entry_date").isoformat(),
        "weight": as_float(
            payload.get("weight"),
            "weight",
            minimum=limits.MIN_BODY_WEIGHT_KG,
            maximum=limits.MAX_BODY_WEIGHT_KG,
            unit="kg",
        ),
        "notes": as_text(payload.get("notes")),
    }


def normalize_waist_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Замер талии в форму для базы: дата, обхват в границах из ``limits``, заметка.

    Константа одна и для записи, и для аналитики (``coach_features.waist_points``):
    значение не может быть «сохранено в UI, но проигнорировано коучем». Зовёт
    ``backend_store.save_waist``.
    """
    return {
        "entry_date": as_date(payload.get("entry_date"), "entry_date").isoformat(),
        "waist": as_float(
            payload.get("waist"),
            "waist",
            minimum=limits.MIN_WAIST_CM,
            maximum=limits.MAX_WAIST_CM,
            unit="cm",
        ),
        "notes": as_text(payload.get("notes")),
    }


# --------------------------------------------------------------------------- #
# События
# --------------------------------------------------------------------------- #
def normalize_event_date(value: object, field: str) -> str:
    """Дата события: формат плюс запрет будущего — планирование отложено,
    событие описывает то, что уже случилось."""
    return as_past_date(value, field).isoformat()


def normalize_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Событие (период без тренировок с причиной) в форму для базы.

    Даты в прошлом или сегодня, конец не раньше начала, пустой конец — событие
    ещё идёт, текст обязателен. Зовут ``backend_store.save_event`` и
    ``update_event``.
    """
    start_date = normalize_event_date(payload.get("start_date"), "start_date")

    raw_end_date = payload.get("end_date")
    # Пустая строка от клиента — это «ещё идёт», а не кривая дата.
    if raw_end_date is None or not str(raw_end_date).strip():
        end_date = None
    else:
        end_date = normalize_event_date(raw_end_date, "end_date")
        if end_date < start_date:
            raise ValueError("end_date must not be earlier than start_date")

    return {
        "start_date": start_date,
        "end_date": end_date,
        "text": required_text(payload.get("text"), "text"),
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
