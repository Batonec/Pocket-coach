#!/usr/bin/env python3
"""HTTP-вызов Claude Messages API на чистом stdlib.

Единственное место в backend, где открывается соединение с api.anthropic.com:
urllib вместо SDK, чтобы долгоживущий процесс сервера не тянул на VPS ни
пакетов, ни лишней памяти. Модуль ничего не знает ни о тренировках, ни о
промптах: на входе системный промпт, сообщения и, при необходимости, JSON-схема
ответа; на выходе текст ответа и usage. Ретраи с backoff — только на временные
сбои (429/5xx/таймаут); отказ модели, обрезка по max_tokens и битый JSON —
ошибка сразу.

Кто и зачем зовёт модель, решает recommender.py: по инварианту проекта она
вызывается ровно в двух местах, и оба там.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")

# На Opus 5 мышление включено по умолчанию, и max_tokens ограничивает
# мышление ВМЕСТЕ с ответом. Прежние 3500 (хватало Opus 4.8 без мышления)
# обрезали бы JSON на полуслове — отсюда запас.
DEFAULT_MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "20000"))

# Глубина мышления: low | medium | high | xhigh | max. Осознанно high:
# рекомендация генерируется несколько раз в день, и качество плана здесь
# важнее задержки. Пустая строка — не слать effort вообще.
DEFAULT_EFFORT = os.getenv("ANTHROPIC_EFFORT", "high").strip()

# Таймаут ОДНОГО вызова модели. Реврайт валидатора делает второй вызов, так
# что весь HTTP-запрос в худшем случае занимает вдвое дольше — таймауты
# iOS-клиента (APIClient.longRunningSession) держатся выше этого числа.
DEFAULT_TIMEOUT = float(os.getenv("ANTHROPIC_TIMEOUT", "120"))

# Временные сбои, которые стоит повторить с backoff: лимиты, перегрузка, икота
# шлюза. Постоянные ошибки (400/401/403/404, отказ модели) не повторяются.
DEFAULT_MAX_RETRIES = int(os.getenv("ANTHROPIC_MAX_RETRIES", "2"))
DEFAULT_RETRY_BACKOFF = float(os.getenv("ANTHROPIC_RETRY_BACKOFF", "1.5"))
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


# Один тип ошибки на весь слой модели: нет истории, нет ключа, API упал,
# ответ не разобрать. Живёт здесь, потому что чаще всего рождается здесь;
# recommender реэкспортирует его, и вызывающие ловят recommender.RecommendationError.
class RecommendationError(Exception):
    """Единственная ошибка слоя модели: нет истории, нет ключа, API упал, ответ не
    разобрать. Текст безопасен для показа клиенту. Снаружи её ловят как
    ``recommender.RecommendationError``: recommender реэкспортирует класс.
    """


# --------------------------------------------------------------------------- #
# Вызов Anthropic (stdlib urllib)
# --------------------------------------------------------------------------- #
def _fetch_anthropic(
    request: urllib.request.Request,
    *,
    timeout: float,
    max_retries: int,
    backoff: float,
    sleep: Callable[[float], None],
) -> str:
    """POST в API с повторами на временные сбои и экспоненциальным backoff.

    Возвращает сырое тело ответа. Постоянная ошибка (400/401/403/404) или
    исчерпанные повторы — ``RecommendationError``. Зовёт только ``_request_model``.
    """
    attempt = 0
    while True:
        try:
            # Схема не пользовательская: request собирается тут же из константы
            # API_URL, подставить file:// или другую схему некому.
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            retryable = exc.code in RETRYABLE_STATUS
            if not retryable or attempt >= max_retries:
                detail = exc.read().decode("utf-8", "replace")[:300]
                raise RecommendationError(f"Claude API вернул ошибку {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt >= max_retries:
                if isinstance(exc, TimeoutError) or isinstance(
                    getattr(exc, "reason", None), TimeoutError
                ):
                    raise RecommendationError("Claude API не ответил вовремя") from exc
                reason = getattr(exc, "reason", exc)
                raise RecommendationError(f"Не удалось связаться с Claude API: {reason}") from exc
        sleep(backoff * (2**attempt))
        attempt += 1


def _cacheable_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Помечает первое сообщение пользователя границей кэша промпта.

    Вместе с кэшированным системным блоком это делает репромпт валидатора (тот же
    system и то же первое сообщение) и всплески регенераций заметно дешевле.
    """
    out = [dict(message) for message in messages]
    first = out[0]
    if first.get("role") == "user" and isinstance(first.get("content"), str):
        out[0] = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": first["content"],
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    return out


def _request_model(
    system: str,
    user: str | list[dict[str, Any]],
    *,
    schema: dict[str, Any] | None = None,
    model: str,
    max_tokens: int,
    api_key: str,
    timeout: float,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff: float = DEFAULT_RETRY_BACKOFF,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, dict[str, Any]]:
    """Один вызов модели: сырой текст ответа и usage.

    Со схемой ответ ограничен ею (план тренировки), без схемы — обычный текст
    (недельный отчёт). ``user`` — либо строка user-промпта, либо готовый список
    сообщений: репромпт валидатора продолжает тот же разговор. Отказ модели,
    обрезка по max_tokens, не-JSON от API и пустой ответ — ``RecommendationError``.
    Зовут: ``recommender.generate_weekly_report`` напрямую и ``_call_anthropic``.
    """
    messages = [{"role": "user", "content": user}] if isinstance(user, str) else user
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        # Системный промпт (каталог + профиль + политика) между вызовами не меняется:
        # кэшируем, чтобы повторы, репромпты и всплески платили за него ~10%.
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": _cacheable_messages(messages),
    }
    output_config: dict[str, Any] = {}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    if DEFAULT_EFFORT:
        output_config["effort"] = DEFAULT_EFFORT
    if output_config:
        payload["output_config"] = output_config
    body = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        ANTHROPIC_URL,
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )

    raw = _fetch_anthropic(
        request,
        timeout=timeout,
        max_retries=max_retries,
        backoff=backoff,
        sleep=sleep,
    )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecommendationError("Claude API вернул не-JSON ответ") from exc

    if data.get("stop_reason") == "refusal":
        raise RecommendationError("Модель отказалась генерировать ответ")

    # Отдельная ветка, потому что мышление съедает тот же max_tokens: без неё
    # обрезанный JSON доезжает до парсера и выглядит как «модель сломала схему»,
    # хотя чинить надо бюджет (ANTHROPIC_MAX_TOKENS) или effort.
    if data.get("stop_reason") == "max_tokens":
        raise RecommendationError(
            f"Ответ не поместился в max_tokens ({max_tokens}) — подними "
            "ANTHROPIC_MAX_TOKENS или снизь ANTHROPIC_EFFORT"
        )

    text = next(
        (block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"),
        "",
    )
    if not text:
        raise RecommendationError("Пустой ответ модели")

    return text, data.get("usage", {}) or {}


def _call_anthropic(
    system: str,
    user: str | list[dict[str, Any]],
    schema: dict[str, Any],
    *,
    model: str,
    max_tokens: int,
    api_key: str,
    timeout: float,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff: float = DEFAULT_RETRY_BACKOFF,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Вызов со схемой: ``_request_model`` плюс разбор JSON ответа в dict.

    Невалидный JSON от модели — ``RecommendationError``. Зовёт
    ``recommender.generate_with_trace`` для плана и его репромпта.
    """
    text, usage = _request_model(
        system,
        user,
        schema=schema,
        model=model,
        max_tokens=max_tokens,
        api_key=api_key,
        timeout=timeout,
        max_retries=max_retries,
        backoff=backoff,
        sleep=sleep,
    )

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecommendationError("Модель вернула невалидный JSON") from exc

    return parsed, usage
