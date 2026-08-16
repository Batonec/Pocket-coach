#!/usr/bin/env python3
"""Prompt text, kept out of the code that assembles it.

Everything the model reads as prose lives in ``backend/prompts/*.md`` — plain
markdown a human can open and read end to end. This module only loads a
template and fills its slots; it computes nothing and decides nothing.

The split is deliberate and is the point of the module: methodology is text and
belongs in text files, where a change reads as a prose diff. Numbers, thresholds
and anything derived from the athlete's data stay in Python and arrive here as
already-rendered strings.

Slots are written ``{{name}}`` and substituted literally — no ``str.format``,
so braces inside the prose (JSON snippets, ranges) need no escaping. A template
rendered with a missing or unknown slot raises: a prompt that silently ships
``{{phase_policy}}`` to the model is worse than a failed import at boot.

Stdlib-only, like the rest of the backend.
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_SLOT_RE = re.compile(r"\{\{([a-z_]+)\}\}")


class PromptError(RuntimeError):
    """A template is missing, unreadable, or its slots do not line up."""


def load(name: str) -> str:
    """Raw template text by file stem (``"system"`` → ``prompts/system.md``)."""
    path = PROMPTS_DIR / f"{name}.md"
    try:
        return path.read_text("utf-8")
    except OSError as exc:  # missing file on a half-deployed backend
        raise PromptError(f"промпт {name!r} не найден: {path}") from exc


def slots(template: str) -> set[str]:
    """Slot names a template expects."""
    return set(_SLOT_RE.findall(template))


def render(template: str, **values: str) -> str:
    """Fill every ``{{slot}}``; refuse to ship a half-filled prompt."""
    expected = slots(template)
    missing = expected - set(values)
    if missing:
        raise PromptError(f"не заполнены слоты промпта: {', '.join(sorted(missing))}")
    unknown = set(values) - expected
    if unknown:
        raise PromptError(f"лишние слоты промпта: {', '.join(sorted(unknown))}")
    return _SLOT_RE.sub(lambda m: values[m.group(1)], template)


def build(name: str, **values: str) -> str:
    """Load and render in one call — the usual entry point."""
    return render(load(name), **values)
