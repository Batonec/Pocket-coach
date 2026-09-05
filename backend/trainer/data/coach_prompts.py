#!/usr/bin/env python3
"""Текст промптов, вынесенный из кода, который их собирает.

Вся проза, которую читает модель, лежит в ``backend/prompts/*.md`` — обычный
markdown, который человек открывает и читает целиком. Этот модуль только грузит
шаблон и подставляет слоты; он ничего не считает и ничего не решает.

Разделение намеренное, и в нём смысл модуля: методика — это текст, и место ей в
текстовых файлах, где правка читается как диф прозы. Числа, пороги и всё, что
считается из данных атлета, остаются в Python и приходят сюда уже строками.

Слоты пишутся ``{{name}}`` — латиницей или кириллицей, без цифр — и подставляются
буквально, без ``str.format``, так что фигурные скобки в прозе (кусочки JSON,
диапазоны) экранировать не нужно. Шаблон с незаполненным или лишним слотом падает:
промпт, молча уехавший в модель с ``{{phase_policy}}``, хуже, чем ошибка на старте.

Шапка файла — HTML-комментарий ``<!-- … -->`` в самом начале — написана для
человека, который открыл файл: что это за промпт, кто и когда его собирает.
``load`` её срезает, так что модель шапку не видит ни у цельного шаблона
(``weekly_report.md``), ни у файла фрагментов; комментарий не в начале файла — обычный
текст, он уедет в модель как есть.

Кто зовёт: ``prompt_builder`` (шаблоны и подписи блоков), ``coach_signals``
(тексты баннеров из ``resources/signals.md``) и ``rule_engine`` (правила плана и
пометки к ним). Только stdlib, как весь backend.
"""

from __future__ import annotations

import re
from pathlib import Path

from trainer import BACKEND_DIR

# Две поверхности, один механизм: prompts/ читает модель, resources/ (signals.md) —
# клиент. Держать их в одной папке значит смешать две разные аудитории.
PROMPTS_DIR = BACKEND_DIR / "prompts"
COPY_DIR = BACKEND_DIR / "resources"

_SLOT_RE = re.compile(r"\{\{([a-zа-яё_]+)\}\}")
# Шапка для человека: один HTML-комментарий строго в начале файла и пустые
# строки вокруг него. Срезается при загрузке, иначе у цельного шаблона она
# уехала бы в системный промпт вместе с прозой.
_HEADER_RE = re.compile(r"\A\s*<!--.*?-->\s*", re.S)


class PromptError(RuntimeError):
    """Шаблон не найден, не читается или его слоты не сходятся с переданными."""


def load(name: str, *, directory: Path | None = None) -> str:
    """Текст шаблона по имени файла (``"next_workout"`` → ``prompts/next_workout.md``) без
    шапки-комментария в начале.

    ``directory`` переопределяет папку: так ``coach_signals`` читает баннеры из
    ``resources/``. Нет файла — ``PromptError``.
    """
    path = (directory or PROMPTS_DIR) / f"{name}.md"
    try:
        text = path.read_text("utf-8")
    except OSError as exc:  # файла нет: backend задеплоен наполовину
        raise PromptError(f"промпт {name!r} не найден: {path}") from exc
    return _HEADER_RE.sub("", text, count=1)


def slots(template: str) -> set[str]:
    """Имена слотов ``{{name}}``, которые ожидает шаблон."""
    return set(_SLOT_RE.findall(template))


_FRAGMENT_RE = re.compile(r"^## ([a-z_]+)[ \t]*$", re.M)


def fragments(name: str, *, directory: Path | None = None) -> dict[str, str]:
    """Именованные фрагменты файла блоков: заголовок ``## имя`` начинает фрагмент.

    Проза, которая идёт вперемешку с вычисленными данными — подписи к блокам,
    отдельные строки по условию, — не может жить в одном шаблоне с необязательными
    слотами, не ослабив правило «незаполненный слот — это падение». Она живёт здесь:
    текст в файле, условие в Python.

    У фрагмента срезаются только переводы строк, пробелы остаются: подпись может
    законно начинаться с пробела (« — плановая разгрузочная неделя.»). Всё до первого
    заголовка — комментарий. Зовут ``prompt_builder`` (``user_blocks.md``) и
    ``coach_signals`` (``signals.md``).
    """
    text = load(name, directory=directory)
    marks = list(_FRAGMENT_RE.finditer(text))
    if not marks:
        raise PromptError(f"в {name!r} нет ни одного фрагмента «## имя»")
    out: dict[str, str] = {}
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[mark.end() : end].strip("\n")
        if mark.group(1) in out:
            raise PromptError(f"фрагмент {mark.group(1)!r} в {name!r} объявлен дважды")
        out[mark.group(1)] = body
    return out


_HEADING_RE = re.compile(r"^## +(?:\d+\.\s*)?(.+?)\s*$", re.M)


def document_sections(text: str, wanted: list[str]) -> tuple[str, list[str]]:
    """Вырезает разделы ``## N. Заголовок`` из длинного человеческого документа.

    Сопоставление по ТЕКСТУ заголовка без номера: атлет перенумеровывает разделы,
    правя документ, и срез по «## 4.» молча начал бы отдавать не ту главу.
    Ненайденный заголовок возвращается списком, а не исчезает: промпт без раздела
    про сплит обязан быть заметен, а не просто короче.

    Возвращает ``(склеенные_разделы, ненайденные_заголовки)``; порядок как в
    ``wanted``. Зовёт ``prompt_builder._render_program`` для среза стратегии.
    """
    marks = list(_HEADING_RE.finditer(text))
    found: dict[str, str] = {}
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        found[mark.group(1).lower()] = text[mark.start() : end].strip("\n")
    picked, missing = [], []
    for heading in wanted:
        body = found.get(heading.lower())
        if body is None:
            missing.append(heading)
        else:
            picked.append(body)
    return "\n\n".join(picked), missing


def render(template: str, **values: str) -> str:
    """Заполняет каждый ``{{slot}}``; наполовину заполненный промпт не отдаёт.

    Незаполненный или лишний слот — ``PromptError`` с именами слотов.
    """
    expected = slots(template)
    missing = expected - set(values)
    if missing:
        raise PromptError(f"не заполнены слоты промпта: {', '.join(sorted(missing))}")
    unknown = set(values) - expected
    if unknown:
        raise PromptError(f"лишние слоты промпта: {', '.join(sorted(unknown))}")
    return _SLOT_RE.sub(lambda m: values[m.group(1)], template)


def build(name: str, **values: str) -> str:
    """Загрузить и отрендерить одним вызовом — обычная точка входа для шаблона целиком."""
    return render(load(name), **values)
