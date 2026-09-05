#!/usr/bin/env python3
"""Файлы рядом с базой и в resources/: чтение и запись, без решений.

Состояние подготовки (``coach_state.json``), профиль атлета
(``coach_profile.json``), рабочий документ стратегии (``coach_strategy.md``)
и каталог упражнений (``resources/exercises.json``). Что из прочитанного
считать валидным и как менять состояние, решает ``domain.coach_state``;
здесь только диск.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

from trainer.data.anthropic_client import RecommendationError
from trainer.domain import coach_state


def default_state_path(db_path: Path | str) -> Path:
    """Где лежит ``coach_state.json``: рядом с базой, как и ``coach_profile.json``;
    переменная ``COACH_STATE_PATH`` переопределяет. Зовут server.py, скрипты таймеров
    и Coach MCP при старте.
    """
    return Path(os.getenv("COACH_STATE_PATH") or str(Path(db_path).parent / "coach_state.json"))


def load_state(path: Path | str | None) -> dict[str, Any]:
    """Файл состояния поверх дефолтов; нет пути, файла или он битый — дефолты,
    чтобы генерация всегда работала."""
    if not path:
        return coach_state.default_state()
    try:
        raw = json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return coach_state.default_state()
    return coach_state.normalize_state(raw)


def save_state(path: Path | str, state: dict[str, Any]) -> None:
    """Записать состояние подготовки в файл как есть, JSON с отступами."""
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", "utf-8")


def set_phase(
    path: Path | str,
    phase: str,
    params: dict[str, Any] | None = None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Переключить фазу в файле: прочитать, применить ``coach_state.switch_phase``,
    записать. Зовёт инструмент Coach MCP."""
    state = coach_state.switch_phase(load_state(path), phase, params, today=today)
    save_state(path, state)
    return state


# --------------------------------------------------------------------------- #
# Каталог упражнений
# --------------------------------------------------------------------------- #
def load_catalog(path: Path | str) -> list[dict[str, Any]]:
    """Каталог упражнений из ``resources/exercises.json``: список ``{id, name}``.

    Тот же файл iOS скачивает по ``/data/exercises.json`` и держит копию в бандле.
    Строки без id или имени пропускаются; пустой каталог — ``RecommendationError``.
    Зовут server.py при старте, скрипты таймеров и Coach MCP.
    """
    raw = json.loads(Path(path).read_text("utf-8"))
    exercises = raw.get("exercises", [])
    catalog: list[dict[str, Any]] = []
    for item in exercises:
        try:
            catalog.append({"id": int(item["id"]), "name": str(item["name"]).strip()})
        except (KeyError, TypeError, ValueError):
            continue
    if not catalog:
        raise RecommendationError("Каталог упражнений пуст или недоступен")
    return catalog


# --------------------------------------------------------------------------- #
# Профиль атлета и стратегия
# --------------------------------------------------------------------------- #
def load_profile(path: Path | str | None) -> dict[str, Any] | None:
    """Профиль атлета: JSON с текстовыми блоками для системного промпта.

    Настоящий профиль живёт ТОЛЬКО на сервере рядом с базой: в нём личный и
    медицинский контекст, в публичный репозиторий он не попадает (форма описана в
    backend/README.md). Нет файла, битый или без блоков — ``None``: генерация
    работает, просто без личного контекста. Зовут server.py, скрипты таймеров и
    Coach MCP перед каждой генерацией.
    """
    if not path:
        return None
    try:
        raw = json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    blocks = raw.get("blocks")
    if not isinstance(blocks, dict) or not blocks:
        return None
    return raw


def update_profile_block(path: Path | str | None, block: str, text: str | None) -> dict[str, Any]:
    """Заменить один текстовый блок ``coach_profile.json`` или удалить его, если
    ``text`` пустой.

    Это путь записи для инструмента Coach MCP ``coach_update_profile``: профиль —
    личные данные рядом с базой, и удалённая правка идёт через него, а не через
    SSH. Предыдущая версия файла остаётся рядом как ``.bak-таймстамп``. Возвращает
    обновлённый профиль; нет пути, файла, блоков или блока для удаления —
    ``RecommendationError`` с понятным текстом.
    """
    if not path:
        raise RecommendationError("Путь к профилю атлета не настроен")
    path = Path(path)
    try:
        original = path.read_text("utf-8")
        raw = json.loads(original)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RecommendationError(
            "Профиль не найден или сломан — проверь coach_profile.json руками"
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("blocks"), dict):
        raise RecommendationError("Профиль без blocks{} — проверь coach_profile.json руками")
    name = str(block).strip()
    if not name:
        raise RecommendationError("Не задано имя блока профиля")
    cleaned = str(text).strip() if text is not None else ""
    blocks = raw["blocks"]
    if not cleaned and name not in blocks:
        raise RecommendationError(f"Блока «{name}» нет в профиле — удалять нечего")

    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    backup.write_text(original, "utf-8")
    if cleaned:
        blocks[name] = cleaned
    else:
        del blocks[name]
    raw["updated"] = date.today().isoformat()
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return raw


def load_strategy(path: Path | str | None) -> str | None:
    """Рабочий документ стратегии из data/ рядом с профилем.

    Личный текст: в репозиторий он не попадает, живёт на VPS вместе с
    coach_profile.json. Отсутствующий или битый файл — это None и промпт без
    раздела ПРОГРАММА, а не отказ генерации.
    """
    if not path:
        return None
    try:
        text = Path(path).read_text("utf-8")
    except OSError:
        return None
    return text or None
