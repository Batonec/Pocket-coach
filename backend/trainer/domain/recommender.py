#!/usr/bin/env python3
"""«Совет тренера»: точка входа и оба вызова модели.

Здесь живёт то, что связывает слой коуча воедино: :func:`generate` /
:func:`generate_with_trace` для плана следующей тренировки и
:func:`generate_weekly_report` для недельного отчёта. Каталог, профиль и
стратегию читает ``data/files`` и передаёт сюда аргументами.
По инварианту проекта модель вызывается ровно в этих двух местах.

Сам вызов разложен по трём модулям, и этот файл только дирижирует:
``prompt_builder`` собирает текст, который читает модель; ``anthropic_client``
ходит в API на stdlib ``urllib``; ``plan_validator`` держит три жёсткие
границы и детерминированное разрешение после неудачного репромпта.
Сторонних зависимостей нет намеренно — долгоживущий процесс сервера на
маленьком VPS не тянет ни пакетов, ни лишней памяти.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Any

from trainer.data import anthropic_client
from trainer.data.anthropic_client import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    RecommendationError,
)
from trainer.domain import coach_features, coach_state, plan_validator, prompt_builder

DEFAULT_HISTORY_LIMIT = int(os.getenv("RECOMMENDATION_HISTORY_LIMIT", "20"))


# Реэкспорт из coach_features: единственный источник правды о группах и границах.
MUSCLE_GROUPS = coach_features.MUSCLE_GROUPS
MIN_PLAUSIBLE_BODY_WEIGHT = coach_features.MIN_PLAUSIBLE_BODY_WEIGHT
MAX_PLAUSIBLE_BODY_WEIGHT = coach_features.MAX_PLAUSIBLE_BODY_WEIGHT


# --------------------------------------------------------------------------- #
# План следующей тренировки
# --------------------------------------------------------------------------- #
def _sum_usage(*usages: dict[str, Any]) -> dict[str, Any]:
    """Сложить токены нескольких вызовов (первый запрос и репромпт) в один usage.
    Зовёт также Coach MCP, когда показывает трассу отладки.
    """
    total: dict[str, Any] = {}
    for usage in usages:
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
    return total


def generate_with_trace(
    workouts: list[dict[str, Any]],
    body_weights: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    *,
    profile: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    strategy: str | None = None,
    waists: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    today: date | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> tuple[dict[str, Any], dict[str, Any], str, list[dict[str, Any]]]:
    """То же, что :func:`generate`, плюс трасса попыток
    ``[{raw, adjustments, violations, usage}, ...]`` для отладочных инструментов MCP.

    Порядок: системный промпт → user-промпт → JSON-схема → вызов модели →
    санитизация (``plan_validator._validate``) → три жёсткие границы
    (``_semantic_violations``) → при нарушениях один исправляющий репромпт в том же
    разговоре → если модель промахнулась снова, детерминированное разрешение
    (``_resolve_violations``) с пометкой в rationale → дата следующей тренировки и
    ``coach_context`` для клиента. Семантические нарушения никогда не роняют
    генерацию: ``RecommendationError`` только для API и структурных сбоев.
    Зовут :func:`generate` и ``coach_debug_recommendation`` в Coach MCP.
    """
    if not workouts:
        raise RecommendationError("Нет истории тренировок для рекомендации")

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RecommendationError("ANTHROPIC_API_KEY не настроен на сервере")

    today = today or date.today()
    state = state if state is not None else coach_state.default_state()
    session_cap = plan_validator._session_cap(coach_state.phase_params(state))
    system = prompt_builder._build_system_prompt(catalog, profile, state, strategy)
    user = prompt_builder._build_user_prompt(
        workouts,
        body_weights,
        today,
        history_limit,
        catalog=catalog,
        state=state,
        waists=waists,
        events=events,
    )
    schema = prompt_builder._build_schema(catalog)

    call = lambda messages: anthropic_client._call_anthropic(  # noqa: E731
        system,
        messages,
        schema,
        model=model,
        max_tokens=max_tokens,
        api_key=api_key,
        timeout=timeout,
        max_retries=max_retries,
    )

    trace: list[dict[str, Any]] = []
    parsed, usage = call(user)
    recommendation = plan_validator._validate(parsed, catalog)
    violations = plan_validator._semantic_violations(
        recommendation, catalog, workouts, today, session_cap=session_cap
    )
    trace.append(
        {
            "raw": parsed,
            "adjustments": [],
            "violations": violations,
            "usage": usage,
        }
    )

    if violations:
        # Один исправляющий круг в том же разговоре: называем нарушения
        # поимённо и просим переосмыслить план. Если модель промахивается
        # снова, сервер чинит детерминированно (ограничивает возвратные веса,
        # дописывает rationale), а не роняет генерацию.
        reprompt = (
            "Твой план нарушает жёсткие границы:\n- "
            + "\n- ".join(violations)
            + "\nИсправь ТОЛЬКО эти нарушения, сохрани остальную логику и верни "
            "полный план заново в той же JSON-схеме."
        )
        messages = [
            {"role": "user", "content": user},
            {"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)},
            {"role": "user", "content": reprompt},
        ]
        parsed, usage_retry = call(messages)
        recommendation = plan_validator._validate(parsed, catalog)
        violations = plan_validator._semantic_violations(
            recommendation, catalog, workouts, today, session_cap=session_cap
        )
        adjustments: list[str] = []
        if violations:
            adjustments = plan_validator._resolve_violations(
                recommendation, catalog, workouts, today, session_cap=session_cap
            )
        trace.append(
            {
                "raw": parsed,
                "adjustments": adjustments,
                "violations": violations,
                "usage": usage_retry,
            }
        )
        usage = _sum_usage(usage, usage_retry)

    # Относительные rest_days от модели превращаем в дату уже при генерации:
    # авто-свежесть пересобирает совет ежедневно, так что дата не протухает.
    # Карточка показывает готовую дату, а не считает её на клиенте.
    recommendation["next_workout_date"] = (
        today + timedelta(days=recommendation["rest_days"])
    ).isoformat()
    recommendation["coach_context"] = _coach_context(state, workouts, today)
    return recommendation, usage, model, trace


def _coach_context(
    state: dict[str, Any], workouts: list[dict[str, Any]], today: date
) -> dict[str, Any]:
    """Контекст фазы и цикла, который уезжает в payload совета.

    По нему iOS рисует бейдж фазы и цели ТЕКУЩЕЙ недели по группам мышц, не зашивая
    диапазоны политики у себя; плюс опорные линии для графиков «Замеров»: цель веса
    фазы (цель сушки или потолок набора) и жёсткий лимит талии, если задан.
    """
    params = coach_state.phase_params(state)
    position = coach_state.cycle_position(state, workouts, today)
    maintenance_sets = params.get("sets_per_group") if params["phase"] == "maintenance" else None
    if position["deload_week"]:
        week_target = params.get("ramp_start")
    else:
        week_target = coach_state.weekly_volume_target(state, position["cycle_week"])
    target_weight = params.get("target_weight_kg") or params.get("ceiling_weight_kg")
    return {
        "phase": params["phase"],
        "phase_title": params["title"],
        "block_week": position["block_week"],
        "deload_week": position["deload_week"],
        "return_from_break": coach_state.is_return_from_break(workouts, today),
        "weekly_target": list(week_target) if week_target else None,
        "group_targets": {
            group: list(target)
            for group, target in coach_features.group_volume_targets(
                week_target, maintenance_sets, params.get("group_targets")
            ).items()
        },
        # Опорные линии для графиков «Замеров»: цель веса фазы (цель сушки
        # или потолок набора) и жёсткий лимит талии, если задан.
        "target_weight_kg": float(target_weight) if target_weight else None,
        "waist_limit_cm": state.get("waist_limit_cm"),
    }


def generate(
    workouts: list[dict[str, Any]],
    body_weights: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    *,
    profile: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    strategy: str | None = None,
    waists: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    today: date | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Проверенный совет на следующую тренировку.

    Возвращает ``(recommendation, usage, model)``. Любая невозможность —
    ``RecommendationError``: нет истории, нет ключа, ошибка API, непригодный ответ.
    Зовут server.py (фоновая пересборка после правок и ``POST
    /api/recommendations/refresh``), скрипт таймера ``refresh_recommendation.py`` и
    Coach MCP.
    """
    recommendation, usage, model_used, _trace = generate_with_trace(
        workouts,
        body_weights,
        catalog,
        profile=profile,
        state=state,
        strategy=strategy,
        waists=waists,
        events=events,
        today=today,
        model=model,
        max_tokens=max_tokens,
        history_limit=history_limit,
        timeout=timeout,
        max_retries=max_retries,
    )
    return recommendation, usage, model_used


# --------------------------------------------------------------------------- #
# Недельный отчёт тренера
# --------------------------------------------------------------------------- #
def generate_weekly_report(
    workouts: list[dict[str, Any]],
    body_weights: list[dict[str, Any]],
    waists: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    *,
    profile: dict[str, Any] | None = None,
    strategy: str | None = None,
    state: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    today: date | None = None,
    days: int = 7,
    model: str = DEFAULT_MODEL,
    # Отчёт — проза без схемы, но мышление на high считается в тот же бюджет.
    max_tokens: int = 12000,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[str, dict[str, Any], str]:
    """Недельная ретроспектива в стиле тренера: Markdown без схемы.

    Возвращает ``(текст, usage, model)``. Период и окно данных задаёт ``today``
    (воскресенье закрытой недели, см. :func:`weekly_report_period`) и ``days``.
    Зовут скрипт таймера ``weekly_report.py`` и Coach MCP.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RecommendationError("ANTHROPIC_API_KEY не настроен на сервере")

    today = today or date.today()
    state = state if state is not None else coach_state.default_state()
    user = prompt_builder._build_report_prompt(
        workouts, body_weights, waists, catalog, state, today, max(1, int(days)), events=events
    )
    text, usage = anthropic_client._request_model(
        prompt_builder._build_report_system_prompt(profile, strategy),
        user,
        schema=None,
        model=model,
        max_tokens=max_tokens,
        api_key=api_key,
        timeout=timeout,
    )
    return text.strip(), usage, model


# --------------------------------------------------------------------------- #
# Жизненный цикл совета: когда он устарел, что его обесценивает, когда обновлять
# --------------------------------------------------------------------------- #
def is_stale(rec: dict[str, Any] | None, latest_workout_id: int | None) -> bool:
    """Готовый совет собран по тренировке, которая уже не последняя: карточка
    показывает его с пометкой, пока не досчитается новый. Зовёт server.py при выдаче
    совета и после ручного refresh.
    """
    return bool(
        rec and rec.get("status") == "ready" and rec.get("based_on_workout_id") != latest_workout_id
    )


# Что обесценивает готовый совет: любая правка данных, из которых он собран —
# тренировки, замеры веса и талии, события (они уезжают в промпт текстом).
# Ровно одно исключение: повтор POST /api/workouts с тем же client_id — это
# ретрай, а не новая тренировка, и он ничего не меняет.
ADVICE_INPUTS = frozenset({"workout", "body_weight", "waist", "event"})


def advice_invalidated_by(change: str, *, created: bool = True) -> bool:
    """Обесценивает ли правка данных готовый совет (см. ``ADVICE_INPUTS`` выше).
    Зовёт ``server._advice_changed`` после каждой мутации.
    """
    return change in ADVICE_INPUTS and created


# Строка pending старше этого — генерация, умершая на полпути (например,
# сервер перезапустили): её можно спокойно перехватить.
STUCK_PENDING_HOURS = 2.0


def should_refresh(
    rec: dict[str, Any] | None,
    now_ts: int,
    max_age_hours: float = 24.0,
) -> tuple[bool, str]:
    """Нужно ли пересобирать совет по таймеру: нет совета, прошлая генерация
    упала, зависший pending или готовый старше ``max_age_hours``.

    Возвращает ``(нужно, причина)``; причина уходит в лог скрипта. Зовёт
    ``infra/jobs/refresh_recommendation.py`` каждое утро.
    """
    if rec is None:
        return True, "рекомендации ещё нет"

    age_hours = (now_ts - int(rec.get("updated_at") or 0)) / 3600
    status = rec.get("status")

    if status == "pending":
        if age_hours > STUCK_PENDING_HOURS:
            return True, f"зависший pending ({age_hours:.1f} ч)"
        return False, f"генерация уже идёт ({age_hours:.1f} ч)"
    if status == "failed":
        return True, f"прошлая генерация упала ({age_hours:.1f} ч назад)"
    if status == "ready":
        if age_hours > max_age_hours:
            return True, f"рекомендации {age_hours:.1f} ч (> {max_age_hours:g})"
        return False, f"рекомендация свежая ({age_hours:.1f} ч)"
    return True, f"неожиданный статус: {status!r}"


def weekly_report_period(today: date) -> date:
    """Отчёт всегда про ЗАКРЫТУЮ неделю, а не про последние 7 дней: таймер
    просыпается уже в понедельник, поэтому и период, и окно данных модели
    якорятся на прошедшее воскресенье, а не на сегодня. Зовёт
    ``infra/jobs/weekly_report.py``.
    """
    return coach_state.last_closed_week_end(today)


def weekly_report_needed(cached: dict[str, Any] | None, *, force: bool) -> tuple[bool, str]:
    """Отчёт за закрытую неделю генерируется один раз и живёт в кэше; ``--force``
    перегенерирует его поверх. Возвращает ``(нужно, причина)``.
    """
    if force:
        return True, "форсировано (--force)"
    if cached:
        return False, "уже в кэше"
    return True, "в кэше нет"
