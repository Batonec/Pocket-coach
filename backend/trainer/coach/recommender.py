#!/usr/bin/env python3
"""«Совет тренера»: точка входа и оба вызова модели.

Здесь живёт то, что связывает слой коуча воедино: загрузка каталога, профиля
и стратегии атлета, :func:`generate` / :func:`generate_with_trace` для плана
следующей тренировки и :func:`generate_weekly_report` для недельного отчёта.
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
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from trainer.coach import (
    anthropic_client,
    coach_features,
    coach_state,
    plan_validator,
    prompt_builder,
)
from trainer.coach.anthropic_client import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    RecommendationError,
)

DEFAULT_HISTORY_LIMIT = int(os.getenv("RECOMMENDATION_HISTORY_LIMIT", "20"))


# Re-exported from coach_features (single source of truth for the mapping).
MUSCLE_GROUPS = coach_features.MUSCLE_GROUPS
MIN_PLAUSIBLE_BODY_WEIGHT = coach_features.MIN_PLAUSIBLE_BODY_WEIGHT
MAX_PLAUSIBLE_BODY_WEIGHT = coach_features.MAX_PLAUSIBLE_BODY_WEIGHT


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
def load_catalog(static_dir: Path) -> list[dict[str, Any]]:
    """Load the exercise catalog the iOS app uses (www/data/exercises.json)."""
    catalog_path = Path(static_dir) / "data" / "exercises.json"
    raw = json.loads(catalog_path.read_text("utf-8"))
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
# Athlete profile
# --------------------------------------------------------------------------- #
def load_profile(path: Path | str | None) -> dict[str, Any] | None:
    """Load the athlete profile JSON (personal context for the coach prompt).

    The real profile lives ONLY on the server next to the database — it holds
    personal/medical context and must never be committed to the public repo
    (see examples/coach_profile.example.json). Missing/broken file → None: generation
    still works, just without the personal context.
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
    """Replace one prose block of coach_profile.json (or delete it when `text`
    is empty). This is the write path for the Coach MCP tool — the profile is
    personal data living only next to the DB, so remote edits go through here
    instead of SSH. The previous file version is kept as a timestamped .bak
    next to the original. Returns the updated profile dict."""
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


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def _sum_usage(*usages: dict[str, Any]) -> dict[str, Any]:
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
    """Like :func:`generate`, but also returns the attempt trace
    ``[{raw, adjustments, violations, usage}, ...]`` for the MCP debugging
    tools. Semantic violations never fail the generation: after one corrective
    reprompt the server resolves what it can deterministically and annotates
    the rationale — errors are reserved for API/structural failures."""
    if not workouts:
        raise RecommendationError("Нет истории тренировок для рекомендации")

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RecommendationError("ANTHROPIC_API_KEY не настроен на сервере")

    today = today or date.today()
    state = state if state is not None else coach_state.load_state(None)
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
        # One corrective round-trip in the same conversation: name the exact
        # violations and ask for a rethought plan. If the model misses again,
        # the server resolves deterministically (clamp comeback weights,
        # annotate the rationale) instead of failing the generation.
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

    # Resolve the model's relative rest_days into an absolute date at generation
    # time (auto-freshness regenerates daily, so it stays current). The card
    # shows a fixed target instead of doing date math on the client.
    recommendation["next_workout_date"] = (
        today + timedelta(days=recommendation["rest_days"])
    ).isoformat()
    recommendation["coach_context"] = _coach_context(state, workouts, today)
    return recommendation, usage, model, trace


def _coach_context(
    state: dict[str, Any], workouts: list[dict[str, Any]], today: date
) -> dict[str, Any]:
    """Phase/cycle context attached to the recommendation payload so the iOS
    client can render the phase badge and the CURRENT week's volume targets
    instead of hardcoding the policy ranges."""
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
        # Reference lines for the Замеры charts: the phase's weight goal
        # (cut target / bulk ceiling) and the hard waist limit, when set.
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
    """Generate a validated next-workout recommendation.

    Returns ``(recommendation, usage, model)``. Raises :class:`RecommendationError`
    on any failure (no history, missing key, API error, unusable output)."""
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
# Weekly coach report
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
    """A coach-style weekly retrospective in Markdown (plain text, no schema).

    Returns ``(report_text, usage, model)``."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RecommendationError("ANTHROPIC_API_KEY не настроен на сервере")

    today = today or date.today()
    state = state if state is not None else coach_state.load_state(None)
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
