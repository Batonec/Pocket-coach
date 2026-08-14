#!/usr/bin/env python3
"""Next-workout recommendation generation via the Claude Messages API.

Zero third-party dependencies on purpose: the call to api.anthropic.com is made
with stdlib ``urllib`` (the same approach bot.py already uses for the Telegram
API), so the long-running server process gains no extra packages and no extra
RAM footprint on the small VPS.

The public entry point is :func:`generate`, which takes the user's workout
history (as returned by ``MiniAppStore.list_workouts``) plus the exercise
catalog and returns a validated, schema-shaped recommendation dict.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import coach_features
import coach_state


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")
DEFAULT_MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "3500"))
DEFAULT_HISTORY_LIMIT = int(os.getenv("RECOMMENDATION_HISTORY_LIMIT", "20"))
DEFAULT_TIMEOUT = float(os.getenv("ANTHROPIC_TIMEOUT", "90"))

# Transient failures worth retrying with backoff (rate limits, overload, gateway
# hiccups). Permanent errors (400/401/403/404, refusal) are never retried.
DEFAULT_MAX_RETRIES = int(os.getenv("ANTHROPIC_MAX_RETRIES", "2"))
DEFAULT_RETRY_BACKOFF = float(os.getenv("ANTHROPIC_RETRY_BACKOFF", "1.5"))
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

# Server-side sanity bounds (JSON Schema can't express numeric ranges, so the
# model output is clamped/filtered after parsing). These are sanitation, not
# methodology: rep ranges, session size and weight jumps are the model's
# coaching judgement and are deliberately NOT validated.
MAX_REPS = 100
MAX_WEIGHT = 1000.0
MAX_EXERCISES = 10
MAX_SETS_PER_EXERCISE = 12
MAX_REST_DAYS = 4   # rest_days is clamped to 0–4 silently, never reprompted

# Raw history shown to the model; everything older is covered by the computed
# per-exercise summaries (the prompt must not grow from the feature work).
RAW_HISTORY_COUNT = 10

ALLOWED_LOAD_TYPES = ("heavy", "medium", "light")

_EFFORT_MARK = {"easy": "-", "ok": "", "hard": "+"}

# What each catalog machine actually is (from the athlete's own descriptions) —
# the terse RU names alone don't tell the model which muscle works. Id 1 is a
# catalog duplicate of 18: history rows are re-mapped onto 18 during
# serialization, so the model never sees it.
CATALOG_SEMANTICS: dict[int, str] = {
    18: "рычажный жим сидя от груди, горизонтальный — грудь (вся), вторично трицепс и передняя дельта",
    17: "пек-дек «бабочка» — изоляция груди",
    9: "вертикальная тяга с двумя сходящимися ручками (имитация подтягиваний) — широчайшие, вторично бицепс",
    4: "подтягивания в гравитроне — широчайшие/верх спины. ВНИМАНИЕ: поле weight — вес ПРОТИВОВЕСА-помощи, прогресс = УМЕНЬШЕНИЕ веса",
    10: "рычажная горизонтальная тяга (хаммер) — толщина спины (середина трапеции, ромбовидные), вторично бицепс",
    13: "махи в тренажёре с упором в локти, сидя — средняя дельта",
    11: "тренажёр на сгибание рук — бицепс",
    12: "трицепс на блоке вниз (ручка варьируется: прямая/канат) — трицепс",
    8: "жим ногами в платформе 45° — квадрицепс + ягодичные",
    16: "разгибания ног сидя — квадрицепс (изоляция)",
    15: "сгибания ног лёжа — бицепс бедра",
}

# Muscles the athlete CANNOT train with the current catalog — standing context
# so the model knows they sit at zero structurally, not by athlete's laziness.
CATALOG_GAPS = "задняя дельта, икры, пресс, разгибатели спины — упражнений в каталоге нет"

# Re-exported from coach_features (single source of truth for the mapping).
MUSCLE_GROUPS = coach_features.MUSCLE_GROUPS
MIN_PLAUSIBLE_BODY_WEIGHT = coach_features.MIN_PLAUSIBLE_BODY_WEIGHT
MAX_PLAUSIBLE_BODY_WEIGHT = coach_features.MAX_PLAUSIBLE_BODY_WEIGHT


class RecommendationError(Exception):
    """Raised when a recommendation cannot be produced (no history, API error,
    invalid model output, ...). The message is safe to surface to the client."""


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
    (see coach_profile.example.json). Missing/broken file → None: generation
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


def update_profile_block(
    path: Path | str | None, block: str, text: str | None
) -> dict[str, Any]:
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
        raise RecommendationError(
            "Профиль без blocks{} — проверь coach_profile.json руками"
        )
    name = str(block).strip()
    if not name:
        raise RecommendationError("Не задано имя блока профиля")
    cleaned = str(text).strip() if text is not None else ""
    blocks = raw["blocks"]
    if not cleaned and name not in blocks:
        raise RecommendationError(f"Блока «{name}» нет в профиле — удалять нечего")

    backup = path.with_name(
        f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    backup.write_text(original, "utf-8")
    if cleaned:
        blocks[name] = cleaned
    else:
        del blocks[name]
    raw["updated"] = date.today().isoformat()
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return raw


def _render_profile(profile: dict[str, Any] | None) -> str:
    if not profile:
        return (
            "Профиль атлета не настроен — веди как взрослого здорового любителя, "
            "цель: качественный набор мышечной массы."
        )
    parts = []
    for title, text in profile.get("blocks", {}).items():
        body = str(text).strip()
        if body:
            parts.append(f"[{title}]\n{body}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Computed training context (plan adherence; the rest lives in coach_features)
# --------------------------------------------------------------------------- #
def _plan_adherence_report(workouts: list[dict[str, Any]]) -> str | None:
    """Compare the most recent snapshot-carrying workout against its plan."""
    for workout in workouts:  # newest-first
        snapshot = (workout.get("data", {}) or {}).get("recommendation")
        if not isinstance(snapshot, dict):
            continue
        planned = {
            ex.get("exercise_id"): ex
            for ex in snapshot.get("exercises", []) or []
            if isinstance(ex, dict)
        }
        if not planned:
            return None
        actual = {
            ex.get("exercise_id"): ex
            for ex in (workout.get("data", {}) or {}).get("exercises", []) or []
        }

        done, total = 0, 0
        deviations: list[str] = []
        for exercise_id, plan_ex in planned.items():
            plan_sets = plan_ex.get("sets", []) or []
            total += len(plan_sets)
            fact = actual.get(exercise_id)
            if fact is None:
                deviations.append(f"{plan_ex.get('name', exercise_id)}: пропущено")
                continue
            fact_sets = fact.get("sets", []) or []
            done += min(len(fact_sets), len(plan_sets))
            if len(fact_sets) != len(plan_sets):
                deviations.append(
                    f"{plan_ex.get('name', exercise_id)}: {len(fact_sets)}/{len(plan_sets)} подходов"
                )
        extras = [
            str(ex.get("name", ""))
            for exercise_id, ex in actual.items()
            if exercise_id not in planned
        ]
        if extras:
            deviations.append("сверх плана: " + ", ".join(filter(None, extras)))

        summary = f"Последняя тренировка по твоему плану ({workout.get('workout_date')}): {done}/{total} плановых подходов"
        if deviations:
            summary += "; отклонения: " + "; ".join(deviations[:4])
        return summary + "."
    return None


# --------------------------------------------------------------------------- #
# Prompt building
# --------------------------------------------------------------------------- #
def _serialize_workout(
    workout: dict[str, Any], names_by_id: dict[int, str] | None = None
) -> str:
    data = workout.get("data", {}) or {}
    load_type = data.get("load_type") or "?"
    parts: list[str] = []
    for exercise in data.get("exercises", []) or []:
        name = str(exercise.get("name", "")).strip() or "?"
        canonical = coach_features.canonical_exercise_id(exercise.get("exercise_id"))
        # Old rows may carry the duplicate id 1 — show them under the canonical
        # catalog name so the model sees one movement, not two.
        if names_by_id and canonical is not None and canonical in names_by_id:
            name = names_by_id[canonical]
        sets_repr: list[str] = []
        for workout_set in exercise.get("sets", []) or []:
            try:
                reps = int(workout_set.get("reps", 0))
                weight = float(workout_set.get("weight", 0))
            except (TypeError, ValueError):
                continue
            mark = _EFFORT_MARK.get(workout_set.get("effort") or "", "")
            rir = workout_set.get("rir")
            rir_repr = (
                f"@{int(rir)}"
                if isinstance(rir, (int, float)) and not isinstance(rir, bool)
                else ""
            )
            weight_repr = f"{weight:g}"
            sets_repr.append(f"{weight_repr}кг×{reps}{mark}{rir_repr}")
        if sets_repr:
            parts.append(f"{name} {', '.join(sets_repr)}")
    body = "; ".join(parts) if parts else "(нет подходов)"
    return f"{workout.get('workout_date', '?')} [{load_type}] {body}"


def _serialize_history(
    workouts: list[dict[str, Any]],
    limit: int,
    catalog: list[dict[str, Any]] | None = None,
) -> str:
    # list_workouts() returns newest-first; take the most recent `limit`
    # and present oldest -> newest so progression reads naturally.
    names_by_id = {item["id"]: item["name"] for item in catalog} if catalog else None
    recent = list(workouts[:limit])
    recent.reverse()
    return "\n".join(_serialize_workout(w, names_by_id) for w in recent)


def _days_since_last(workouts: list[dict[str, Any]], today: date) -> int | None:
    for workout in workouts:  # newest-first
        raw = workout.get("workout_date")
        if not raw:
            continue
        try:
            last = date.fromisoformat(str(raw))
        except ValueError:
            continue
        return (today - last).days
    return None


def _format_range(bounds: Any, unit: str = "") -> str:
    if isinstance(bounds, (tuple, list)) and len(bounds) == 2:
        return f"{bounds[0]:g}–{bounds[1]:g}{unit}"
    return f"{bounds}{unit}"


def _render_phase_policy() -> str:
    cut = coach_state.PHASE_DEFAULTS["cut_recomp"]
    bulk = coach_state.PHASE_DEFAULTS["lean_bulk"]
    maintenance = coach_state.PHASE_DEFAULTS["maintenance"]
    return (
        "Подготовка ведётся ФАЗАМИ. Текущая фаза, её неделя блока и целевые ориентиры "
        "приходят в блоке КОНТЕКСТ каждого запроса — они главнее общих цифр ниже. Все "
        "числа фаз (калории, подходы, объёмы) — ориентиры для планирования, а не "
        "жёсткие рамки. Фазу переключает ТОЛЬКО атлет (руками); если данные "
        "показывают, что цель фазы достигнута, предложи смену фазы в rationale — но "
        "план строй по текущей.\n"
        f"- cut_recomp («{cut['title']}»): калории {_format_range(cut['calories'])} ккал, "
        f"темп {cut['rate_text']}, {cut['frequency_text']}, сессия "
        f"{_format_range(cut['session_sets'])} рабочих подходов, недельный объём — ramp "
        f"{_format_range(cut['ramp_start'])} → {_format_range(cut['ramp_cap'])} сетов на "
        "крупную группу (+1–2 сета в неделю, в первую очередь на отстающие), интенсивность "
        "рабочая (1–2 RIR), прогрессия двойная. У новичка силовые растут и в дефиците — "
        "плато по весам в этой фазе НЕ проблема и не повод для паники; deload-триггеры "
        f"мягче. Белок {_format_range(cut['protein_g'])} г.\n"
        f"- lean_bulk: калории {_format_range(bulk['calories'])} ккал, темп {bulk['rate_text']}, "
        f"{bulk['frequency_text']}, сессия {_format_range(bulk['session_sets'])} подходов, "
        f"объём — ramp {_format_range(bulk['ramp_start'])} → {_format_range(bulk['ramp_cap'])} "
        "сетов на крупную группу, прогрессия двойная — ожидаются регулярные ПР. Контроль "
        f"набора по талии; потолок — талия у лимита либо ~{bulk['ceiling_weight_kg']:g} кг. "
        f"Белок {_format_range(bulk['protein_g'])} г.\n"
        f"- maintenance («{maintenance['title']}»): {maintenance['frequency_text']} — одна "
        f"fullbody heavy сессия {_format_range(maintenance['session_sets'])} подходов, "
        f"{_format_range(maintenance['sets_per_group'])} сета на группу в неделю, объём НЕ "
        "растёт и претензий к объёму в rationale быть не должно. Веса НЕ снижать — "
        "интенсивность и есть сигнал удержания; прогрессия не требуется. Калории "
        f"{_format_range(maintenance['calories'])} ккал, белок ~{maintenance['protein_g'][0]:g}+ г."
    )


def _build_system_prompt(
    catalog: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
) -> str:
    catalog_lines = "\n".join(
        f"  {item['id']} — {item['name']}: {CATALOG_SEMANTICS.get(item['id'], 'тренажёр')}"
        for item in catalog
        if item["id"] not in coach_features.EXERCISE_ALIASES
    )
    return (
        "Ты — персональный силовой тренер этого атлета и полностью заменяешь живого "
        "тренера: ведёшь его многомесячную программу, решаешь, какой будет СЛЕДУЮЩАЯ "
        "тренировка, когда её провести, когда дать отдых или разгрузку, и следишь за "
        "недельными объёмами и движением к цели. Ты видишь его историю и контекст в "
        "каждом запросе — твоя задача давать связную систему, а не тренировку в вакууме.\n\n"
        "=== АТЛЕТ ===\n"
        f"{_render_profile(profile)}\n\n"
        "=== ТРЕНАЖЁРЫ (каталог) ===\n"
        "В план включай ТОЛЬКО эти упражнения (id и названия точные):\n"
        f"{catalog_lines}\n"
        f"Структурные пробелы каталога: {CATALOG_GAPS}.\n"
        "Если для цели не хватает движения (в первую очередь задняя дельта при таком "
        "объёме жимов) — предложи его ТЕКСТОМ в rationale («советую добавить X, потому "
        "что Y»), но не в каждой карточке — примерно раз в пару недель; атлет сам решит "
        "и добавит. В exercises[] несуществующие упражнения не включай никогда.\n\n"
        "=== ФАЗЫ ПОДГОТОВКИ ===\n"
        f"{_render_phase_policy()}\n\n"
        "=== ТРЕНЕРСКАЯ ПОЛИТИКА (ориентиры по умолчанию) ===\n"
        "Всё в этом разделе — рабочие настройки по умолчанию, а не законы: ты видишь "
        "больше контекста, чем любое правило. Отступай от ориентиров, когда видишь "
        "причину, и называй её в note/rationale.\n"
        "- Ритм и недельный объём подсказывает текущая фаза (см. КОНТЕКСТ запроса). "
        "Малые группы обычно держат ниже потолков: средняя дельта 6–12, бицепс/трицепс "
        "напрямую 4–8 (жимы и тяги уже добирают им часть стимула — эффективные сеты "
        "посчитаны в данных), бицепс бедра 5–10 — ставь сгибания ног почти в каждую "
        "сессию, пока группа хронически недобирает.\n"
        "- Размер сессии выбирай сам под цель дня, время (~60 минут; при 18+ подходах "
        "предупреди, что выйдет ближе к 75–90 мин, или сократи отдых на изоляции до "
        "60–90 сек) и реальную дисциплину атлета. Разминочные подходы в план НЕ "
        "включай — атлет делает их сам.\n"
        "- Волны интенсивности для базовых движений (жимы, тяги, жим ногами) — "
        "привычная атлету схема: heavy 6–10 повторов, medium 10–14, light 12–18; "
        "изоляция (дельты, бицепс, трицепс, бабочка, разгибания/сгибания ног) 10–15+ "
        "даже в heavy-день. Это default, а не рамка — выбирай другой диапазон, когда "
        "для конкретного дня есть причина. Два по-настоящему тяжёлых дня подряд без "
        "необходимости не ставь — но суди по реальной тяжести весов и интервалу "
        "отдыха, а не по меткам.\n"
        "- Метки нагрузки в истории ([heavy]/[medium]/[light]/[?]) могут быть неточны "
        "или отсутствовать — оценивай реальную тяжесть сессии сам по весам и повторам "
        "относительно истории.\n"
        "- Усилие: рабочие подходы с запасом 1–2 повтора; на изоляции в последнем "
        "подходе допустимо 0–1; жимы (ногами, от груди) до отказа не доводить. Если у "
        "подхода в истории есть @N — это записанный RIR (повторы в запасе, 0–4) "
        "ПОСЛЕДНЕГО подхода: он ТОЧНЕЕ значков +/− — приоритет ему.\n"
        "- Прогрессия: шаг веса бери из истории КОНКРЕТНОГО тренажёра (каким шагом "
        "атлет реально менял вес). +1 повтор или +1 шаг веса, если в прошлый раз "
        "подходы шли с отметкой «-» (легко), без отметки или с RIR ≥2; при «+» "
        "(тяжело) или RIR 0–1 — закрепи вес. Одиночные аномальные подходы (резко "
        "выпадающий вес среди стабильной серии) считай шумом записи — прогрессию от "
        "них не строй. Пики и даты ПР по каждому тренажёру уже посчитаны в данных — "
        "считай их фактами, не выводи заново из сырой истории.\n"
        "- Возврат после перерыва: если с последней тренировки прошло ≥14 дней — "
        "веди возврат по принятым тренерским принципам работы после простоя "
        "(детренированность и её разная скорость по системам, готовность "
        "соединительной ткани, разгон за несколько сессий). Ты профессионал — "
        "насколько снизить вход, каким темпом возвращать прежние веса, сколько "
        "подходов и какой тяжести делать первые сессии, решаешь сам по длительности "
        "перерыва, данным о доперерывных весах и профилю атлета; обычно возврат — "
        "это medium/light сессия умеренного объёма без отказа. Жёстко фиксировано "
        "только одно: возвратная сессия не место для прибавки — вес выше "
        "доперерывного рабочего сервер не примет ни по одному движению. Ступени "
        "разгона в данных (если есть) — ориентир от сервера, не предписание. "
        "Длинный перерыв сам по себе — разгрузка: счётчик deload обнуляется. "
        "После перерыва в rationale вместо сводки нулевых объёмов опиши план "
        "разгона на 2–3 сессии.\n"
        "- Разгрузка (deload): плановая разгрузочная неделя приходит ФЛАГОМ в "
        "КОНТЕКСТЕ (каждые ~6 недель реально накопленной работы): объём −30–40%, "
        "веса рабочие, без отказа, со следующей недели ramp заново — построй сессию "
        "по этому флагу и прямо скажи в rationale, что это разгрузка. Реактивно: при "
        "падении повторов на тех же весах две сессии подряд предложи лёгкую неделю "
        "сам. Если в данных стоит флаг ЗАСТОЯ по упражнению (предусловия выполнены, "
        "ПР нет ≥4 недель) — предложи deload −10% с разгоном или вариацию именно по "
        "нему. Если предусловия НЕ выполнены — плато объясняй посещаемостью/питанием, "
        "слова «потолок» избегай.\n"
        "- Дисциплина: сводка «факт vs план» за 30 дней приходит в данных. Если "
        "какое-то упражнение стабильно пропускается — ставь его РАНЬШЕ в сессии и/или "
        "делай план реалистичного размера; адаптируйся к реальному поведению, а не "
        "читай нотации.\n"
        "- Регулярность: если перерывы >10 дней повторяются, мягко предложи в "
        "rationale привязать тренировки к конкретным дням недели — без нотаций.\n"
        "- Медицинская граница: вопросы ГЗТ, дозировок, анализов и давления — зона "
        "лечащего врача. Никаких советов и интерпретаций по ним не давай; гормональный "
        "фон атлета (из профиля) — просто часть контекста восстановления.\n\n"
        "=== ЖЁСТКИЕ ГРАНИЦЫ (проверяет сервер) ===\n"
        "Жёстких правил всего три — остальное в этом промпте лишь ориентиры, "
        "решение за тобой:\n"
        "1) в exercises[] — только упражнения каталога (id из схемы);\n"
        "2) крупная группа (грудь, спина, квадрицепс/ягодичные) или бицепс бедра, у "
        "которых 10+ дней ноль эффективных сетов, должна получить хотя бы 1–2 "
        "подхода;\n"
        "3) на возврате после перерыва (≥14 дней) вес любого движения — не выше "
        "доперерывного рабочего.\n\n"
        "=== ПИТАНИЕ ===\n"
        "Решение по калориям НЕ выводи сам из сырых замеров: сервер уже сравнил тренды "
        "веса и талии с целями фазы и положил готовую ветку в блок «Матрица питания» — "
        "переведи её в короткий совет своими словами. Правила матрицы (для понимания): "
        "cut_recomp — вес в целевом темпе и талия вниз → не трогать; вес стоит ≥2 недель "
        "→ −100–150 ккал; вес стоит, но талия идёт вниз → рекомп-бонус, калории не "
        "трогать. lean_bulk — вес растёт в темпе, талия стабильна → не трогать; талия "
        "+1 см от базовой два замера подряд → −100–150 ккал или пауза набора; талия у "
        "лимита → жёсткий сигнал, предложи мини-кат/смену фазы. maintenance — выход из "
        "коридора ±1 кг → мягкая коррекция ±100–150 ккал. Советы по калориям давай "
        "ТОЛЬКО по свежим (≤14 дней) замерам — иначе попроси взвеситься/замерить талию "
        "и калории не обсуждай. Белок — по ориентиру фазы; если по факту меньше, мягко "
        "напомни добрать.\n\n"
        "=== RATIONALE (тренерский комментарий в карточке) ===\n"
        "Пиши на «ты», как живой тренер, структурно и по делу. ФОРМАТ: rationale "
        "должен легко читаться — НЕ сплошным абзацем. Каждый пункт с новой строки "
        "(разделяй реальным переносом строки), 1–3 коротких предложения, и начинай "
        "пункт с короткого жирного заголовка в Markdown-звёздочках, например "
        "«**Когда:** завтра, дай связкам ещё день» или «**Нагрузка:** средняя — "
        "после паузы не гоним». Пункты по содержанию:\n"
        "1) **Почему так** — тяжесть и состав дня (объёмы недели, давность, отметки);\n"
        "2) **Когда** — сегодня / завтра / «дай ещё день отдыха»; обязательно тем же "
        "числом в поле rest_days (0 = сегодня, 1 = завтра, 2 = послезавтра), держи "
        "текст и rest_days согласованными;\n"
        "3) **Объём** — краткий статус недельного объёма по группам (где добор, где хватит);\n"
        "4) **Питание** — комментарий по весу тела и калориям, если есть свежие данные;\n"
        "5) при необходимости — **Совет** с новым упражнением или разгрузкой.\n"
        "В note каждого упражнения — одна фраза: почему такой вес/повторы относительно "
        "прошлого раза («+2.5 кг — прошлый раз все подходы easy», «закрепляем вес — было "
        "тяжело»); если отступаешь от ориентиров политики — причина тоже сюда. Изредка "
        "вместо этого можно дать одну важную техническую подсказку.\n\n"
        "Все веса в килограммах. Отвечай строго в заданной JSON-схеме, без текста вне JSON."
    )


def _build_user_prompt(
    workouts: list[dict[str, Any]],
    body_weights: list[dict[str, Any]],
    today: date,
    history_limit: int,
    catalog: list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
    waists: list[dict[str, Any]] | None = None,
) -> str:
    state = state if state is not None else coach_state.load_state(None)
    waists = waists or []
    params = coach_state.phase_params(state)
    phase = params["phase"]

    days = _days_since_last(workouts, today)
    returning = coach_state.is_return_from_break(workouts, today)
    position = coach_state.cycle_position(state, workouts, today)
    week = position["block_week"]
    if position["deload_week"]:
        # The planned light week caps the target back at the ramp start.
        week_target = params.get("ramp_start")
    else:
        week_target = coach_state.weekly_volume_target(state, position["cycle_week"])

    # --- explicit context block, always the first thing the model reads ------
    week_label = f"неделя блока {week}"
    if position["deload_week"]:
        week_label += (
            " — ПЛАНОВАЯ РАЗГРУЗОЧНАЯ НЕДЕЛЯ (каждые "
            f"{params.get('deload_every_weeks')} недель накопления): объём −30–40%, "
            "веса рабочие, без отказа; со следующей недели ramp начинается заново"
        )
    context_lines = [
        f"Сегодня: {today.isoformat()} ({_RU_WEEKDAYS[today.weekday()]}).",
        f"Фаза: {phase} («{params['title']}»), {week_label}. Ориентиры фазы: "
        f"{_format_range(params['calories'])} ккал, {params['rate_text']}, белок "
        f"{_format_range(params['protein_g'])} г, сессия "
        f"{_format_range(params['session_sets'])} рабочих подходов.",
    ]
    if days is None:
        context_lines.append("Дата последней тренировки неизвестна.")
    elif returning:
        context_lines.append(
            f"Дней с последней тренировки: {days} — ВОЗВРАТ ПОСЛЕ ПЕРЕРЫВА (≥14 дн). "
            "Методика возврата на тебе; жёсткая граница одна: не выше доперерывных "
            "рабочих весов (они ниже)."
        )
    else:
        context_lines.append(f"Дней с последней тренировки: {days}.")
    chunks = ["=== КОНТЕКСТ ===\n" + "\n".join(context_lines)]

    volume = coach_features.weekly_volume(workouts, today)
    maintenance_sets = params.get("sets_per_group") if phase == "maintenance" else None
    chunks.append(
        "Объём за последние 7 дней (прямые/эффективные подходы; в эффективных жимы "
        "уже добирают трицепсу и дельтам, тяги — бицепсу):\n"
        + coach_features.render_weekly_volume(volume, week_target, maintenance_sets)
    )

    measurement_lines = coach_features.render_measurements(body_weights, waists, today)
    matrix = coach_features.nutrition_matrix(state, params, body_weights, waists, today)
    nutrition_chunk = list(measurement_lines)
    if matrix["lines"]:
        nutrition_chunk.append(
            "Матрица питания (вычислено сервером): " + "; ".join(matrix["lines"]) + "."
        )
    if matrix["goal"]:
        nutrition_chunk.append("ЦЕЛЬ ФАЗЫ: " + matrix["goal"] + ".")
    if nutrition_chunk:
        chunks.append("\n".join(nutrition_chunk))

    summaries = coach_features.exercise_summaries(workouts, catalog or [], today)
    if summaries:
        chunks.append(
            "Сводка по тренажёрам за ВСЮ историю (пики, e1RM по Эпли и даты ПР "
            "посчитаны сервером — это факты, не пересчитывай):\n"
            + coach_features.render_exercise_summaries(summaries)
        )

    stall = coach_features.stall_report(
        workouts,
        summaries,
        matrix.get("trend_per_week"),
        phase,
        params.get("rate_kg_per_week"),
        today,
    )
    stall_line = coach_features.render_stall_report(stall)
    if stall_line:
        chunks.append(stall_line)

    if returning and days is not None:
        pre_break = coach_features.render_pre_break_weights(
            coach_features.pre_break_working_weights(workouts, catalog or []), days
        )
        if pre_break:
            chunks.append(pre_break)

    ramp_lines = coach_features.comeback_ramp(workouts, catalog or [], today)
    if ramp_lines:
        chunks.append(
            "Ступени разгона к пиковым весам (посчитаны сервером как ориентир — "
            "темп возврата решаешь ты):\n" + "\n".join(ramp_lines)
        )

    discipline_lines: list[str] = []
    discipline = coach_features.render_adherence_stats(
        coach_features.adherence_stats(workouts, today)
    )
    if discipline:
        discipline_lines.append(discipline)
    adherence = _plan_adherence_report(workouts)
    if adherence:
        discipline_lines.append(adherence)
    if discipline_lines:
        chunks.append("\n".join(discipline_lines))

    raw_count = min(history_limit, RAW_HISTORY_COUNT)
    chunks.append(
        f"Последние {raw_count} тренировок сырыми (от старых к новым; формат "
        "'дата [нагрузка] упражнение вес×повторы, ...'). Значок после подхода — "
        "субъективная тяжесть: '-' = легко, '+' = тяжело (это НЕ дополнительные "
        "повторы), без значка = нормально; '@N' = записанный RIR последнего "
        "подхода (точнее значков). Более старые тренировки уже учтены в сводке "
        "выше:"
    )
    chunks.append(_serialize_history(workouts, raw_count, catalog))
    chunks.append(
        "Составь план следующей тренировки и тренерский комментарий (когда идти, "
        "статус недельных объёмов, питание по матрице выше)."
    )
    return "\n\n".join(chunks)


_RU_WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
)


def _build_schema(catalog: list[dict[str, Any]]) -> dict[str, Any]:
    # The duplicate id (1 → 18) never enters the enum: old history is re-mapped
    # onto the canonical id, and the plan may only reference canonical ones.
    exercise_ids = [
        item["id"] for item in catalog if item["id"] not in coach_features.EXERCISE_ALIASES
    ]
    return {
        "type": "object",
        "properties": {
            "focus": {
                "type": "string",
                "description": "На что нацелена тренировка (кратко, по-русски)",
            },
            "load_type": {"type": "string", "enum": list(ALLOWED_LOAD_TYPES)},
            "rest_days": {
                "type": "integer",
                "description": (
                    "Через сколько дней от сегодня проводить эту тренировку: "
                    "0 = сегодня, 1 = завтра, 2 = послезавтра, максимум 4. "
                    "Учитывай давность последней тренировки, нагрузку прошлой "
                    "сессии, усталость/сон/стресс и ритм фазы"
                ),
            },
            "rationale": {
                "type": "string",
                "description": (
                    "Развёрнутое объяснение логики тренировки на русском: почему "
                    "такой состав и нагрузка, что в истории на это повлияло и "
                    "почему не выбран другой вариант"
                ),
            },
            "exercises": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "exercise_id": {"type": "integer", "enum": exercise_ids},
                        "note": {
                            "type": "string",
                            "description": (
                                "Короткое (одна фраза) обоснование выбора веса/повторов "
                                "для этого упражнения относительно прошлого раза; "
                                "изредка — одна важная техническая подсказка"
                            ),
                        },
                        "sets": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "reps": {"type": "integer"},
                                    "weight": {"type": "number"},
                                },
                                "required": ["reps", "weight"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["exercise_id", "note", "sets"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["focus", "load_type", "rest_days", "rationale", "exercises"],
        "additionalProperties": False,
    }


# --------------------------------------------------------------------------- #
# Anthropic call (stdlib urllib)
# --------------------------------------------------------------------------- #
def _fetch_anthropic(
    request: urllib.request.Request,
    *,
    timeout: float,
    max_retries: int,
    backoff: float,
    sleep: Callable[[float], None],
) -> str:
    """POST to the API, retrying transient failures with exponential backoff.

    Returns the raw response body. Raises :class:`RecommendationError` on a
    permanent failure or once retries are exhausted.
    """
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            retryable = exc.code in RETRYABLE_STATUS
            if not retryable or attempt >= max_retries:
                detail = exc.read().decode("utf-8", "replace")[:300]
                raise RecommendationError(
                    f"Claude API вернул ошибку {exc.code}: {detail}"
                ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt >= max_retries:
                if isinstance(exc, TimeoutError) or isinstance(
                    getattr(exc, "reason", None), TimeoutError
                ):
                    raise RecommendationError("Claude API не ответил вовремя") from exc
                reason = getattr(exc, "reason", exc)
                raise RecommendationError(
                    f"Не удалось связаться с Claude API: {reason}"
                ) from exc
        sleep(backoff * (2 ** attempt))
        attempt += 1


def _cacheable_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark the first user message as a prompt-cache boundary. Together with
    the cached system block this makes the validator reprompt (same system +
    same first message) and burst regenerations much cheaper."""
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
    """One model call; returns the raw text of the reply plus usage. With a
    schema the output is constrained to it; without — plain text (the weekly
    report). `user` is either the user prompt or a full message list (the
    semantic-validator reprompt continues the same conversation)."""
    messages = [{"role": "user", "content": user}] if isinstance(user, str) else user
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        # The system prompt (catalog + profile + policy) is stable between
        # calls — cache it so retries/reprompts/bursts pay ~10% for it.
        "system": [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ],
        "messages": _cacheable_messages(messages),
    }
    if schema is not None:
        payload["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
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


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _validate(raw: dict[str, Any], catalog: list[dict[str, Any]]) -> dict[str, Any]:
    valid_ids = {
        item["id"] for item in catalog if item["id"] not in coach_features.EXERCISE_ALIASES
    }
    names_by_id = {item["id"]: item["name"] for item in catalog}

    load_type = raw.get("load_type")
    if load_type not in ALLOWED_LOAD_TYPES:
        load_type = "medium"

    try:
        rest_days = int(raw.get("rest_days"))
    except (TypeError, ValueError):
        rest_days = 1
    rest_days = min(max(rest_days, 0), MAX_REST_DAYS)

    exercises_out: list[dict[str, Any]] = []
    for exercise in raw.get("exercises", []) or []:
        if not isinstance(exercise, dict):
            continue
        try:
            exercise_id = int(exercise.get("exercise_id"))
        except (TypeError, ValueError):
            continue
        # The schema enum already excludes the duplicate ids; re-map defensively
        # in case an aliased id sneaks in anyway.
        exercise_id = coach_features.EXERCISE_ALIASES.get(exercise_id, exercise_id)
        if exercise_id not in valid_ids:
            continue

        sets_out: list[dict[str, Any]] = []
        for workout_set in exercise.get("sets", []) or []:
            if not isinstance(workout_set, dict):
                continue
            try:
                reps = int(workout_set.get("reps"))
                weight = float(workout_set.get("weight"))
            except (TypeError, ValueError):
                continue
            if reps < 1:
                continue
            reps = min(reps, MAX_REPS)
            weight = min(max(weight, 0.0), MAX_WEIGHT)
            sets_out.append({"reps": reps, "weight": weight})
            if len(sets_out) >= MAX_SETS_PER_EXERCISE:
                break

        if not sets_out:
            continue

        exercises_out.append(
            {
                "exercise_id": exercise_id,
                # Trust the catalog name over whatever the model echoed back.
                "name": names_by_id.get(exercise_id, str(exercise.get("name", "")).strip()),
                "note": str(exercise.get("note", "")).strip(),
                "sets": sets_out,
            }
        )
        if len(exercises_out) >= MAX_EXERCISES:
            break

    if not exercises_out:
        raise RecommendationError("Модель не предложила ни одного валидного упражнения")

    return {
        "focus": str(raw.get("focus", "")).strip(),
        "load_type": load_type,
        "rest_days": rest_days,
        "rationale": str(raw.get("rationale", "")).strip(),
        "exercises": exercises_out,
    }


# --------------------------------------------------------------------------- #
# Semantic validation (on top of the JSON schema)
# --------------------------------------------------------------------------- #
def _comeback_ceilings(
    workouts: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    today: date,
) -> dict[int, dict[str, Any]]:
    """Per-exercise hard weight ceilings for a return-from-break session, or an
    empty dict outside one. The ceiling is the pre-break working weight: a
    comeback session is not the place for progression. Everything else about
    the return (how far below to start, ramp speed, session size) is the
    model's coaching judgement — the ladder in the prompt is data, not a bound."""
    if not coach_state.is_return_from_break(workouts, today):
        return {}
    return {
        item["exercise_id"]: item
        for item in coach_features.pre_break_working_weights(workouts, catalog)
    }


def _semantic_violations(
    recommendation: dict[str, Any],
    catalog: list[dict[str, Any]],
    workouts: list[dict[str, Any]],
    today: date,
) -> list[str]:
    """The two hard bounds the JSON schema cannot express: the comeback
    no-progression ceiling and muscle-group coverage. Rep ranges, session
    size, weight jumps and load sequencing are deliberately NOT checked —
    that is the model's coaching judgement, guided by the prompt. Each
    violation is a human-readable line — the list goes verbatim into the
    reprompt."""
    violations: list[str] = []

    # 1) return from a break: no weight above the pre-break working weight.
    ceilings = _comeback_ceilings(workouts, catalog, today)
    for exercise in recommendation.get("exercises", []) or []:
        ceiling = ceilings.get(exercise["exercise_id"])
        if not ceiling:
            continue
        allowed = ceiling["last_working"]
        for workout_set in exercise["sets"]:
            weight = workout_set["weight"]
            too_hard = (
                weight < allowed - 1e-9 if ceiling["inverted"] else weight > allowed + 1e-9
            )
            if too_hard:
                what = "противовес" if ceiling["inverted"] else "вес"
                violations.append(
                    f"{exercise['name']}: {what} {weight:g} кг тяжелее доперерывного "
                    f"рабочего ({allowed:g}) — возвратная сессия не место для "
                    "прибавки"
                )

    # 2) group coverage: a big group (or the chronically lagging hamstrings)
    # that has been dry for 10+ days must get at least a set in the plan.
    recent_volume = coach_features.weekly_volume(workouts, today, days=10)
    plan_coverage: dict[str, float] = {}
    for exercise in recommendation["exercises"]:
        for group, share in (
            coach_features.EFFECTIVE_SETS.get(exercise["exercise_id"]) or {}
        ).items():
            plan_coverage[group] = plan_coverage.get(group, 0.0) + share * len(
                exercise["sets"]
            )
    for group in (*coach_features.BIG_GROUPS, "бицепс бедра"):
        if recent_volume[group]["effective"] == 0 and not plan_coverage.get(group):
            violations.append(
                f"группа «{group}» больше 10 дней без единого эффективного подхода "
                "и отсутствует в плане — добавь хотя бы 1–2 подхода"
            )

    return violations


def _resolve_violations(
    recommendation: dict[str, Any],
    catalog: list[dict[str, Any]],
    workouts: list[dict[str, Any]],
    today: date,
) -> list[str]:
    """Deterministic last resort after the reprompt also failed: clamp comeback
    weights to their pre-break ceilings and surface anything unfixable (group
    coverage) as an honest note in the rationale. A slightly imperfect plan
    with a visible note beats an error card — generation must not fail over
    methodology."""
    adjustments: list[str] = []
    ceilings = _comeback_ceilings(workouts, catalog, today)
    for exercise in recommendation.get("exercises", []) or []:
        ceiling = ceilings.get(exercise["exercise_id"])
        if not ceiling:
            continue
        allowed = ceiling["last_working"]
        for workout_set in exercise["sets"]:
            weight = workout_set["weight"]
            too_hard = (
                weight < allowed - 1e-9 if ceiling["inverted"] else weight > allowed + 1e-9
            )
            if too_hard:
                workout_set["weight"] = allowed
                adjustments.append(
                    f"{exercise['name']}: {weight:g} → {allowed:g} кг (доперерывный "
                    "рабочий)"
                )

    notes: list[str] = []
    if adjustments:
        notes.append(
            "**Проверка методики:** возвратные веса ограничены доперерывными "
            "рабочими: " + "; ".join(adjustments) + "."
        )
    remaining = _semantic_violations(recommendation, catalog, workouts, today)
    if remaining:
        notes.append(
            "**Проверка методики:** сервер не смог согласовать с моделью: "
            + "; ".join(remaining) + " — учти при выполнении."
        )
    if notes:
        rationale = str(recommendation.get("rationale", "")).rstrip()
        appendix = "\n\n".join(notes)
        recommendation["rationale"] = (
            f"{rationale}\n\n{appendix}" if rationale else appendix
        )
    return adjustments


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
    waists: list[dict[str, Any]] | None = None,
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
    system = _build_system_prompt(catalog, profile)
    user = _build_user_prompt(
        workouts, body_weights, today, history_limit,
        catalog=catalog, state=state, waists=waists,
    )
    schema = _build_schema(catalog)

    call = lambda messages: _call_anthropic(  # noqa: E731
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
    recommendation = _validate(parsed, catalog)
    violations = _semantic_violations(recommendation, catalog, workouts, today)
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
        recommendation = _validate(parsed, catalog)
        violations = _semantic_violations(recommendation, catalog, workouts, today)
        adjustments: list[str] = []
        if violations:
            adjustments = _resolve_violations(recommendation, catalog, workouts, today)
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
    maintenance_sets = (
        params.get("sets_per_group") if params["phase"] == "maintenance" else None
    )
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
                week_target, maintenance_sets
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
    waists: list[dict[str, Any]] | None = None,
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
        waists=waists,
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
REPORT_SYSTEM_PROMPT = (
    "Ты — персональный силовой тренер этого атлета. По данным ниже напиши "
    "НЕДЕЛЬНЫЙ ОТЧЁТ на «ты»: коротко, по делу, как живой тренер на созвоне — "
    "без воды и без нотаций за пропуски. Формат: Markdown, пять блоков, каждый "
    "с новой строки с жирным заголовком:\n"
    "**Итоги недели** — сколько тренировок, что сделано по объёму против цели;\n"
    "**Прогресс** — новые ПР и движение весов (или честно «без ПР» и почему это "
    "ок/не ок по предусловиям);\n"
    "**Вес и талия** — тренды и вывод матрицы питания (если замеров нет — "
    "попроси);\n"
    "**Дисциплина** — факт против плана, что стабильно пропускается;\n"
    "**Фокус следующей недели** — 2–3 конкретных пункта (объём-цель, какие "
    "группы добирать, когда разгрузка).\n"
    "Все вычисленные числа в данных — факты, не пересчитывай. Никаких "
    "медицинских советов: ГЗТ, дозировки и анализы — зона врача."
)


def _build_report_prompt(
    workouts: list[dict[str, Any]],
    body_weights: list[dict[str, Any]],
    waists: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    state: dict[str, Any],
    today: date,
    days: int,
) -> str:
    params = coach_state.phase_params(state)
    position = coach_state.cycle_position(state, workouts, today)

    week_workouts = [
        workout
        for workout in workouts
        if (when := coach_features._workout_date(workout)) is not None
        and 0 <= (today - when).days < days
    ]

    chunks = [
        f"Период отчёта: последние {days} дней, по {today.isoformat()} "
        f"({_RU_WEEKDAYS[today.weekday()]}).",
        f"Фаза: {params['phase']} («{params['title']}»), неделя блока "
        f"{position['block_week']}"
        + (" — плановая разгрузочная неделя." if position["deload_week"] else ".")
        + f" Ориентиры фазы: {_format_range(params['calories'])} ккал, "
        f"{params['rate_text']}, белок {_format_range(params['protein_g'])} г.",
    ]

    if week_workouts:
        chunks.append(
            f"Тренировки за период ({len(week_workouts)}):\n"
            + _serialize_history(week_workouts, len(week_workouts), catalog)
        )
    else:
        chunks.append("Тренировок за период: 0.")

    week_target = (
        params.get("ramp_start")
        if position["deload_week"]
        else coach_state.weekly_volume_target(state, position["cycle_week"])
    )
    maintenance_sets = (
        params.get("sets_per_group") if params["phase"] == "maintenance" else None
    )
    chunks.append(
        "Объём за 7 дней (прямые/эффективные подходы):\n"
        + coach_features.render_weekly_volume(
            coach_features.weekly_volume(workouts, today), week_target, maintenance_sets
        )
    )

    summaries = coach_features.exercise_summaries(workouts, catalog, today)
    prs = [
        f"  {s['name']}: {s['top_weight']:g}×{s['top_reps']} ({s['top_date']})"
        for s in summaries
        if s["days_since_pr"] < days
    ]
    chunks.append(
        "Новые ПР за период:\n" + "\n".join(prs) if prs else "Новых ПР за период нет."
    )

    matrix = coach_features.nutrition_matrix(state, params, body_weights, waists, today)
    stall = coach_features.stall_report(
        workouts,
        summaries,
        matrix.get("trend_per_week"),
        params["phase"],
        params.get("rate_kg_per_week"),
        today,
    )
    stall_line = coach_features.render_stall_report(stall)
    if stall_line:
        chunks.append(stall_line)

    measurements = coach_features.render_measurements(body_weights, waists, today)
    nutrition = list(measurements)
    if matrix["lines"]:
        nutrition.append(
            "Матрица питания (вычислено сервером): " + "; ".join(matrix["lines"]) + "."
        )
    if matrix["goal"]:
        nutrition.append("ЦЕЛЬ ФАЗЫ: " + matrix["goal"] + ".")
    if nutrition:
        chunks.append("\n".join(nutrition))

    discipline = coach_features.render_adherence_stats(
        coach_features.adherence_stats(workouts, today)
    )
    if discipline:
        chunks.append(discipline)

    next_bits: list[str] = []
    every = params.get("deload_every_weeks")
    if position["deload_week"]:
        next_bits.append(
            "Эта неделя разгрузочная — со следующей ramp начинается заново с "
            f"{_format_range(params.get('ramp_start'))} сетов/группа."
        )
    elif every and position["cycle_week"] >= int(every):
        next_bits.append(
            "Следующая неделя по циклу — плановая разгрузка (−30–40% объёма), "
            "если объём блока реально набран."
        )
    else:
        next_week = coach_state.weekly_volume_target(state, position["cycle_week"] + 1)
        if next_week:
            next_bits.append(
                f"Цель следующей недели блока: {next_week[0]}–{next_week[1]} "
                "эффективных сетов на крупную группу."
            )
    if next_bits:
        chunks.append("\n".join(next_bits))

    chunks.append("Напиши недельный отчёт по формату из системного промпта.")
    return "\n\n".join(chunks)


def generate_weekly_report(
    workouts: list[dict[str, Any]],
    body_weights: list[dict[str, Any]],
    waists: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    *,
    state: dict[str, Any] | None = None,
    today: date | None = None,
    days: int = 7,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2000,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[str, dict[str, Any], str]:
    """A coach-style weekly retrospective in Markdown (plain text, no schema).

    Returns ``(report_text, usage, model)``."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RecommendationError("ANTHROPIC_API_KEY не настроен на сервере")

    today = today or date.today()
    state = state if state is not None else coach_state.load_state(None)
    user = _build_report_prompt(
        workouts, body_weights, waists, catalog, state, today, max(1, int(days))
    )
    text, usage = _request_model(
        REPORT_SYSTEM_PROMPT,
        user,
        schema=None,
        model=model,
        max_tokens=max_tokens,
        api_key=api_key,
        timeout=timeout,
    )
    return text.strip(), usage, model
