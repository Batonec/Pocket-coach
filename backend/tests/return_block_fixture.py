"""Реальный блок возврата атлета (14.08–03.09.2026) как фикстура.

Это данные из разбора рекомендаций от 05.09.2026: последняя доперерывная
сессия, перерыв 25.07–13.08, девять тренировок возврата с заметками об усталости,
событие 20–23.08, замеры веса и талии и параметры фазы «Ф0 · возврат». На этих
данных сервер отдавал модели искажённые агрегаты (частота «за 6 недель» с отпуском,
лесенка к майским пикам, матрица без ветки) — тесты приёмки закрепляют, что
теперь отдаётся вместо них.

Формат тренировки — компактный: ``"id:вес×повторы[+|-],..."`` через ``;``, где
``+`` — тяжело, ``-`` — легко. Один модуль, никаких импортов backend: фикстура
не зависит от sys.path, который выставляет support.
"""

from __future__ import annotations

from datetime import date
from typing import Any

TODAY = date(2026, 9, 5)

CATALOG: list[dict[str, Any]] = [
    {"id": 8, "name": "Жим ногами"},
    {"id": 9, "name": "Тяга верт."},
    {"id": 13, "name": "Дельты"},
    {"id": 11, "name": "Бицепс"},
    {"id": 12, "name": "Трицепс"},
    {"id": 16, "name": "Разгибания ног"},
    {"id": 15, "name": "Сгибания ног"},
    {"id": 10, "name": "Тяга горизонт."},
    {"id": 17, "name": "Бабочка"},
    {"id": 18, "name": "Жим в тренажере"},
    {"id": 19, "name": "Задняя дельта"},
]
_NAMES = {item["id"]: item["name"] for item in CATALOG}

# (дата, метка нагрузки или None, упражнения, заметка к сессии)
_SESSIONS: list[tuple[str, str | None, str, str | None]] = [
    (
        "2026-07-24",
        "heavy",
        "8:80x14,80x13,80x13;9:65x13,65x12+,65x11+;18:55x13,55x13,55x12+;16:30x12;15:30x13",
        None,
    ),
    # --- разрыв 25.07–13.08 (20 дней без тренировок) ---
    (
        "2026-08-14",
        "light",
        "8:80x10,80x10,80x10;9:60x12,60x12,60x12;15:25x12,25x12;13:15x12,15x12;"
        "18:50x12,50x12,50x12;10:40x12,40x12;12:15x14",
        None,
    ),
    (
        "2026-08-16",
        "medium",
        "9:62.5x12,62.5x12,62.5x11;18:52.5x12,52.5x12,52.5x11;10:40x12,50x12,50x8+;"
        "11:10x15,10x10,10x10+;17:20x12,20x12;19:10x12,10x12+;13:17.5x15,17.5x12+;"
        "12:10x15,12.5x15+",
        None,
    ),
    (
        "2026-08-18",
        "medium",
        "15:30x13,30x12+,30x10;16:30x12,30x12,30x12;18:55x12,55x12,55x10;"
        "13:17.5x13,17.5x13+,17.5x10+;12:12.5x14,12.5x13+,12.5x6+",
        None,
    ),
    # --- событие 20–23.08 ---
    (
        "2026-08-24",
        None,
        "10:50x12,50x12,50x12,50x12;15:30x12,30x12;18:55x13,55x12,55x12;13:17.5x15,17.5x13;"
        "11:10x13,10x12,10x12+;9:62.5x13,62.5x12,62.5x12,62.5x11+;19:10x13,10x12;"
        "8:80x12,80x12,80x12",
        None,
    ),
    (
        "2026-08-26",
        "medium",
        "16:30x15,30x15;15:30x14,30x13;13:20x15,20x12,20x11+,20x10;"
        "18:57.5x12,57.5x10,57.5x10,57.5x10;17:22.5x15,22.5x13,22.5x12;"
        "12:12.5x15,12.5x14,12.5x11,12.5x11+",
        "Мне крайне не нравится порядок упражнений: сперва блок на дельты и грудь, "
        "потом пара на ноги.",
    ),
    (
        "2026-08-28",
        "medium",
        "8:80x12,100x8,100x8,100x8;9:65x12,65x10,65x10+,65x10+;15:35x12,35x12+;"
        "11:10x14,10x14,10x12+,10x12+;19:10x14,10x14,10x12;10:40x12",
        "Что то я прям помер",
    ),
    (
        "2026-08-30",
        "medium",
        "10:60x10,60x9,60x8,60x8;18:60x12,60x12,60x10,60x8+;13:20x15,20x13,20x12+;"
        "17:25x12,25x12,25x12;12:12.5x14,12.5x13,12.5x12+",
        None,
    ),
    (
        "2026-09-01",
        None,
        "10:60x10,60x10,60x9,60x9;15:35x12,35x12+,35x8;9:65x12,65x12,65x10+;19:10x14,10x13;"
        "8:100x10,100x9,100x9;11:10x14,10x14,10x8+",
        None,
    ),
    (
        "2026-09-03",
        "medium",
        "16:32.5x13,32.5x12,32.5x12;18:60x12,60x12,60x11,60x10;15:35x12,35x11;"
        "17:25x14,25x13,25x12;11:12.5x17,12.5x17;12:15x11,15x10;13:22.5x13,22.5x12,22.5x10+",
        None,
    ),
]

BODY_WEIGHTS: list[dict[str, Any]] = [
    {"entry_date": day, "weight": value}
    for day, value in (
        ("2026-08-14", 79.9),
        ("2026-08-15", 78.85),
        ("2026-08-19", 79.5),
        ("2026-08-20", 78.75),
        ("2026-08-25", 78.95),
        ("2026-08-27", 79.5),
        ("2026-08-30", 80.8),
        ("2026-09-03", 79.8),
    )
]

WAISTS: list[dict[str, Any]] = [
    {"entry_date": day, "waist": value}
    for day, value in (
        ("2026-08-15", 95.0),
        ("2026-08-19", 93.0),
        ("2026-08-27", 92.0),
        ("2026-08-30", 93.0),
        ("2026-09-01", 93.0),
        ("2026-09-03", 93.0),
    )
]

EVENTS: list[dict[str, Any]] = [
    {
        "id": 1,
        "start_date": "2026-08-20",
        "end_date": "2026-08-23",
        "text": "Напился на кварталке, потом боялся, что простудился, и не тренировался",
    }
]

# coach_state.json атлета на 05.09.2026 (без персональных данных).
STATE: dict[str, Any] = {
    "schema": 1,
    "phase": "cut_recomp",
    "phase_started": "2026-08-14",
    "phase_params": {
        "cut_recomp": {
            "title": "Ф0 · возврат",
            "calories": [2450, 2550],
            "rate_text": "±0 кг/нед — на этом этапе НЕ худеем и НЕ набираем",
            "rate_kg_per_week": [-0.1, 0.1],
            "frequency_text": "4 тренировки в неделю",
            "session_sets": [10, 20],
            "ramp_start": [6, 9],
            "ramp_cap": [9, 16],
            "protein_g": [155, 165],
            "group_targets": {
                "спина": [12, 14],
                "грудь": [12, 14],
                "бицепс": [10, 12],
                "дельты": [9, 11],
                "трицепс": [9, 11],
                "квадрицепс/ягодичные": [8, 10],
                "бицепс бедра": [5, 7],
                "задняя дельта": [3, 5],
            },
        }
    },
    "phase_history": [],
    "waist_limit_cm": 92.0,
    "waist_base_cm": None,
}


def _parse_sets(spec: str) -> list[dict[str, Any]]:
    sets: list[dict[str, Any]] = []
    for token in spec.split(","):
        effort = None
        if token.endswith("+"):
            effort, token = "hard", token[:-1]
        elif token.endswith("-"):
            effort, token = "easy", token[:-1]
        weight, reps = token.split("x")
        entry: dict[str, Any] = {"weight": float(weight), "reps": int(reps)}
        if effort:
            entry["effort"] = effort
        sets.append(entry)
    return sets


def _workout(index: int, session: tuple[str, str | None, str, str | None]) -> dict[str, Any]:
    when, load_type, spec, note = session
    exercises = []
    for part in spec.split(";"):
        exercise_id, sets = part.split(":", 1)
        exercises.append(
            {
                "exercise_id": int(exercise_id),
                "name": _NAMES[int(exercise_id)],
                "sets": _parse_sets(sets),
            }
        )
    data: dict[str, Any] = {"load_type": load_type, "exercises": exercises}
    if note:
        data["notes"] = note
    return {"id": index + 1, "workout_date": when, "data": data}


def workouts() -> list[dict[str, Any]]:
    """История как её отдаёт backend_store.list_workouts — новые сверху."""
    return [_workout(index, session) for index, session in reversed(list(enumerate(_SESSIONS)))]
