#!/usr/bin/env python3
"""Разбор входных значений: тип, границы и текст ошибки.

Служебный словарь для ``domain/rules``: превратить присланное клиентом или MCP
в число, дату, текст или значение из списка — либо бросить ``ValueError``, чей
текст сервер отдаст клиенту как ``reason`` ответа 400. Про тренировки, замеры
и события модуль не знает ничего: методика остаётся в ``rules``, здесь только
механика, которую незачем повторять у каждого поля.

Соглашение об ошибке одно: ``<поле> must ...``, и ``<поле>`` называет ровно то,
что не прошло, — этот текст читает атлет в приложении.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import date
from typing import Any


def as_object(value: Any, field: str) -> dict[str, Any]:
    """Вложенный объект payload'а."""
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def as_list(value: Any, field: str, *, item: str) -> list[Any]:
    """Непустой список; ``item`` — как назвать элемент в тексте ошибки."""
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must contain at least one {item}")
    return value


def as_int(
    value: Any, field: str, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    """Целое в границах. ``bool`` целым не считается: ``true`` в поле повторов —
    ошибка клиента, а не единица; дробное и нечисло тоже не проходят.

    Когда заданы обе границы, все промахи говорят одним текстом
    («must be an integer between 0 and 4»): у шкалы вроде RIR «не число» и «вне
    шкалы» для атлета — одна и та же ошибка ввода.
    """
    bounded = minimum is not None and maximum is not None
    wrong = f"{field} must be an integer" + (
        f" between {minimum:g} and {maximum:g}" if bounded else ""
    )

    if isinstance(value, bool):
        raise ValueError(wrong)
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise ValueError(wrong)
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(wrong) from exc

    if minimum is not None and number < minimum:
        raise ValueError(wrong if bounded else f"{field} must be at least {minimum:g}")
    if maximum is not None and number > maximum:
        raise ValueError(wrong if bounded else f"{field} must be at most {maximum:g}")
    return number


def maybe_int(value: Any) -> int | None:
    """Целое или ``None``: строгая проверка без исключения, для best-effort
    разбора, где кривое поле опускают, а не отвергают запись целиком."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def as_id(value: Any, field: str) -> int:
    """Ссылка на строку каталога: только настоящее целое.

    Здесь, в отличие от введённых человеком чисел, терпимость вредна: ``"1"``
    в поле идентификатора — это баг клиента, а не ввод атлета, и молча принять
    его значит записать тренировку с упражнением, которого он не выбирал.
    """
    number = maybe_int(value)
    if number is None:
        raise ValueError(f"{field} must be an integer")
    return number


def as_float(
    value: Any,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    unit: str = "",
) -> float:
    """Число в границах. ``bool`` числом не считается. Нечисло и ``nan``/``inf``
    разведены по тексту («numeric» против «finite»): это разные ошибки клиента,
    и по сообщению видно, какая именно.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")

    suffix = f" {unit}" if unit else ""
    if minimum is not None and maximum is not None and not minimum <= number <= maximum:
        raise ValueError(f"{field} must be between {minimum:g} and {maximum:g}{suffix}")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} must be at least {minimum:g}{suffix}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field} must be at most {maximum:g}{suffix}")
    return number


def as_text(value: Any, *, limit: int | None = None) -> str | None:
    """Свободный текст: обрезать пробелы, пустое — ``None``, длинное — до ``limit``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit] if limit is not None else text


def required_text(value: Any, field: str) -> str:
    """Обязательный текст: пустой — ошибка."""
    text = as_text(value)
    if text is None:
        raise ValueError(f"{field} is required")
    return text


def as_choice(value: Any, field: str, allowed: Sequence[str]) -> str | None:
    """Значение из списка без учёта регистра; пусто — ``None``, чужое — ошибка."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)}")
    return text


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def as_date(value: Any, field: str) -> date:
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


def as_past_date(value: Any, field: str) -> date:
    """Дата не позже сегодняшней. «Сегодня» — локальный день сервера; VPS живёт
    в Europe/Moscow, то есть в том же дне, что и атлет, как и весь слой коуча.
    """
    parsed = as_date(value, field)
    if parsed > date.today():
        raise ValueError(f"{field} must not be in the future")
    return parsed
