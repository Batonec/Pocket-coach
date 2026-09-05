#!/usr/bin/env python3
"""Coach MCP — разговор с данными тренировок в Claude и отладка рекомендаций.

Тонкий MCP-сервер над той же базой SQLite, что и backend приложения: импортирует
``backend_store``, ``files``, ``coach_state``, ``coach_features``,
``prompt_builder`` и ``recommender`` напрямую, поэтому инструменты отладки
генерируют ровно то, что сгенерировал бы backend. Читает историю и замеры; пишет
талию, события, состояние подготовки и профиль; по просьбе генерирует совет и
недельный отчёт. Docstring каждого инструмента — его описание для модели,
поэтому они на русском и короткие.

Запуск (stdio, локальный Claude Desktop):
    python coach_mcp/server.py

Запуск (streamable-http, за Cloudflare Tunnel, как investor-mcp):
    python coach_mcp/server.py --transport streamable-http --host 127.0.0.1 --port 8001

Окружение:
    ANTHROPIC_API_KEY        нужен инструментам генерации и отладки
    COACH_MCP_BACKEND_DIR    корень backend с пакетом trainer/ (по умолчанию
                             ../backend; на VPS — /opt/trainer-miniapp/app)
    MINIAPP_DB_PATH          путь к SQLite (по умолчанию <backend_dir>/data/trainer.db)
    EXERCISE_CATALOG_PATH    JSON каталога упражнений (по умолчанию
                             <backend_dir>/resources/exercises.json)
    COACH_MCP_PROFILE_PATH   профиль атлета (иначе COACH_PROFILE_PATH, иначе рядом с базой)
    COACH_MCP_STRATEGY_PATH  документ стратегии (иначе COACH_STRATEGY_PATH, иначе рядом с базой)
    COACH_STATE_PATH         состояние подготовки (по умолчанию рядом с базой)
    COACH_MCP_USER_ID        id пользователя (по умолчанию 3)
    ANTHROPIC_MODEL          модель вместо дефолтной из anthropic_client
    COACH_MCP_PATH           HTTP-путь streamable-транспорта (по умолчанию /mcp)
    COACH_MCP_AUTH_TOKEN     если задан, требуется Authorization: Bearer <token>
    COACH_MCP_ALLOWED_HOSTS  список через запятую → строгая защита от DNS-rebinding
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001, S110 — dotenv опционален, .env может не быть
    pass

# --- найти и импортировать пакет backend (trainer/) ---------------------------
_BACKEND_DIR = os.getenv("COACH_MCP_BACKEND_DIR") or str(
    Path(__file__).resolve().parent.parent / "backend"
)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp.types import CallToolResult, TextContent  # noqa: E402

from trainer.data import (  # noqa: E402
    backend_store,
    files,
)
from trainer.domain import (  # noqa: E402
    coach_features,
    coach_state,
    limits,
    prompt_builder,
    recommender,
)

# --- настройки ----------------------------------------------------------------
_DB_PATH = Path(os.getenv("MINIAPP_DB_PATH") or str(Path(_BACKEND_DIR) / "data" / "trainer.db"))
_CATALOG_PATH = Path(
    os.getenv("EXERCISE_CATALOG_PATH") or str(Path(_BACKEND_DIR) / "resources" / "exercises.json")
)
_PROFILE_PATH = Path(
    os.getenv("COACH_MCP_PROFILE_PATH")
    or os.getenv("COACH_PROFILE_PATH")
    or str(_DB_PATH.parent / "coach_profile.json")
)
# Рабочий документ стратегии — там же, рядом с базой. Личный текст, в
# репозиторий не попадает.
_STRATEGY_PATH = Path(
    os.getenv("COACH_MCP_STRATEGY_PATH")
    or os.getenv("COACH_STRATEGY_PATH")
    or str(_DB_PATH.parent / "coach_strategy.md")
)
# Изменяемое состояние подготовки (фаза, лимиты талии) — рядом с базой, как и
# профиль; COACH_STATE_PATH переопределяет.
_STATE_PATH = files.default_state_path(_DB_PATH)
_DEFAULT_USER_ID = int(os.getenv("COACH_MCP_USER_ID") or "3")

STORE = backend_store.MiniAppStore(_DB_PATH)

_INSTRUCTIONS = """\
Тренер-ассистент по силовым тренировкам пользователя.

У тебя есть инструменты к истории тренировок, замерам веса тела и талии,
обхватам (рука, плечи, грудь, шея, бедро — метрики цели), каталогу упражнений,
состоянию подготовки (фаза/цикл) и к движку рекомендаций «следующая тренировка».

Когда пользователь спрашивает «что мне потренировать дальше / разбери мой
прогресс / почему такая рекомендация»:
1) посмотри состояние (coach_get_state), историю (coach_list_workouts) и при
   необходимости каталог (coach_get_catalog), вес (coach_list_body_weights) и
   талию (coach_list_waists);
2) для отладки рекомендаций используй coach_preview_prompt (увидеть точный
   промпт без траты токенов), coach_debug_recommendation (попытки модели с
   нарушениями валидатора и репромптом + токены) и
   coach_get_stored_recommendation (что сейчас лежит в кэше приложения);
3) coach_generate_recommendation генерирует новую рекомендацию; по умолчанию
   НЕ записывает её в базу приложения (store=false) — поставь store=true, только
   если пользователь хочет обновить рекомендацию в самом приложении;
4) coach_weekly_report — недельный отчёт тренера (итоги, ПР, вес/талия,
   дисциплина, фокус следующей недели); зови по просьбе «как прошла неделя /
   недельный отчёт» — сегодняшний отчёт отдаётся из кэша мгновенно;
5) coach_phase_summary — итоги текущей или завершённой фазы подготовки
   («что дала фаза»); coach_costs — расходы на API по месяцам.

Записывающие инструменты: coach_set_phase (смена фазы подготовки — только по
явной просьбе пользователя), coach_update_state (лимит/база талии),
coach_mark_support_week (неделя поддержки внутри дефицита — только по явной
просьбе: матрица питания на ней и две недели после молчит),
coach_update_profile (правка блока профиля атлета — только по явной просьбе),
coach_add_waist / coach_delete_waist (замеры талии, см), coach_add_measurement /
coach_list_measurements / coach_delete_measurement (обхваты кроме талии, см;
план их не читает, недельный отчёт — читает), coach_add_event /
coach_update_event / coach_delete_event (события, см. ниже).

Событие — период без тренировок с причиной («болел», «командировка»):
coach_list_events показывает их (новые сверху), coach_add_event записывает.
Событие с end_date = null идёт прямо сейчас, и такое оно одно; первая
тренировка сегодняшним числом закрывает его сама, вручную — coach_update_event
с датой конца. Из события ничего не считается: это текст для тренера, а не
число.

Это аналитика и сценарии, не медицинский совет: никаких рекомендаций по
дозировкам/схеме ГЗТ/анализам. Веса — в килограммах, талия — в сантиметрах,
отвечай по-русски."""

mcp = FastMCP("Coach MCP", instructions=_INSTRUCTIONS)


# --- хелперы ------------------------------------------------------------------
def _json(data: dict[str, Any]) -> str:
    """JSON с юникодом и отступами для текстовой части ответа."""
    return json.dumps(data, ensure_ascii=False, indent=2)


def _result(payload: dict[str, Any]) -> CallToolResult:
    """Обернуть словарь в ``CallToolResult`` (текст + structuredContent + isError).

    Та же конвенция, что в investor-mcp: весь payload сериализуется в текстовый
    блок (так данные видит модель в любом клиенте) и дублируется в
    structuredContent; isError выводится из ``ok``.
    """
    summary = payload.get("summary") or ("Ошибка." if not payload.get("ok", True) else "Готово.")
    return CallToolResult(
        content=[TextContent(type="text", text=f"{summary}\n\n{_json(payload)}")],
        structuredContent=payload,
        isError=not payload.get("ok", True),
    )


def _err(summary: str) -> dict[str, Any]:
    """Payload ошибки: ``ok=False`` и текст для модели."""
    return {"ok": False, "summary": summary}


def _uid(user_id: int | None) -> int:
    """Id пользователя из аргумента инструмента или настроенный по умолчанию."""
    return int(user_id) if user_id else _DEFAULT_USER_ID


def _catalog() -> list[dict[str, Any]]:
    """Каталог упражнений из ``EXERCISE_CATALOG_PATH`` — тот же файл, что у backend."""
    return files.load_catalog(_CATALOG_PATH)


def _estimate_cost(model: str, usage: dict[str, Any]) -> dict[str, Any] | None:
    # Цены за миллион токенов для моделей, которыми пользуется проект.
    """Оценка стоимости вызова в USD по таблице цен; ``None`` для незнакомой модели."""
    prices = {
        "claude-opus-5": (5.0, 25.0),
        "claude-opus-4-8": (5.0, 25.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5": (1.0, 5.0),
    }
    if model not in prices:
        return None
    in_p, out_p = prices[model]
    it = usage.get("input_tokens") or 0
    ot = usage.get("output_tokens") or 0
    usd = it * in_p / 1_000_000 + ot * out_p / 1_000_000
    return {"input_tokens": it, "output_tokens": ot, "usd": round(usd, 4)}


# --- инструменты: данные ------------------------------------------------------
@mcp.tool()
def coach_list_workouts(limit: int = 20, user_id: int | None = None) -> CallToolResult:
    """История тренировок (новые сверху) + компактная сериализация, как её видит модель."""
    try:
        uid = _uid(user_id)
        workouts = STORE.list_workouts(uid)
        compact = prompt_builder._serialize_history(workouts, limit, _catalog())
        return _result(
            {
                "ok": True,
                "summary": f"Тренировок: {len(workouts)} (показаны последние {min(limit, len(workouts))}).",
                "user_id": uid,
                "total": len(workouts),
                "workouts": workouts[:limit],
                "compact_history": compact,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Не удалось прочитать тренировки: {exc}"))


@mcp.tool()
def coach_get_workout(workout_id: int, user_id: int | None = None) -> CallToolResult:
    """Одна тренировка по id (полные данные)."""
    try:
        uid = _uid(user_id)
        workout = STORE.get_workout_by_id(uid, int(workout_id))
        if workout is None:
            return _result(_err(f"Тренировка #{workout_id} не найдена."))
        return _result({"ok": True, "summary": "Готово.", "user_id": uid, "workout": workout})
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_list_body_weights(user_id: int | None = None) -> CallToolResult:
    """История замеров веса тела (старые сверху)."""
    try:
        uid = _uid(user_id)
        entries = STORE.list_body_weights(uid)
        return _result(
            {
                "ok": True,
                "summary": f"Замеров веса: {len(entries)}.",
                "user_id": uid,
                "entries": entries,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_get_catalog() -> CallToolResult:
    """Каталог доступных упражнений (id + название) — других упражнений не существует."""
    try:
        catalog = _catalog()
        return _result({"ok": True, "summary": f"Упражнений: {len(catalog)}.", "catalog": catalog})
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Каталог недоступен: {exc}"))


# --- инструменты: состояние подготовки (фазы, лимиты талии) -------------------
@mcp.tool()
def coach_get_state(user_id: int | None = None) -> CallToolResult:
    """Текущее состояние подготовки: фаза + её параметры, неделя блока, целевой объём недели, лимит/база талии."""
    try:
        uid = _uid(user_id)
        state = files.load_state(_STATE_PATH)
        workouts = STORE.list_workouts(uid)
        today = date.today()
        params = coach_state.phase_params(state)
        position = coach_state.cycle_position(state, workouts, today)
        week_target = (
            params.get("ramp_start")
            if position["deload_week"]
            else coach_state.weekly_volume_target(state, position["cycle_week"])
        )
        return _result(
            {
                "ok": True,
                "summary": (
                    f"Фаза: {params['phase']} («{params['title']}»), неделя блока "
                    f"{position['block_week']}"
                    + (" — плановая разгрузка." if position["deload_week"] else ".")
                ),
                "user_id": uid,
                "state": state,
                "phase_params": {k: v for k, v in params.items() if k != "phase"},
                "block_week": position["block_week"],
                "cycle_week": position["cycle_week"],
                "deload_week": position["deload_week"],
                "sessions_in_cycle": position["sessions_in_cycle"],
                "weekly_volume_target": week_target,
                "return_from_break": coach_state.is_return_from_break(workouts, today),
                "state_path": str(_STATE_PATH),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_set_phase(phase: str, params: dict[str, Any] | None = None) -> CallToolResult:
    """Переключить фазу подготовки РУКАМИ (cut_recomp / lean_bulk / maintenance). Дата старта фазы =
    сегодня; params — переопределения дефолтов фазы (например {"target_weight_kg": 75}).
    Автопереключений нет: вызывай только по явной просьбе пользователя."""
    try:
        state = files.set_phase(_STATE_PATH, str(phase).strip(), params)
        merged = coach_state.phase_params(state)
        return _result(
            {
                "ok": True,
                "summary": (
                    f"Фаза переключена: {merged['phase']} («{merged['title']}») "
                    f"с {state['phase_started']}."
                ),
                "state": state,
                "phase_params": {k: v for k, v in merged.items() if k != "phase"},
            }
        )
    except ValueError as exc:
        return _result(_err(str(exc)))
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_update_state(
    waist_limit_cm: float | None = None,
    waist_base_cm: float | None = None,
) -> CallToolResult:
    """Обновить глобальные параметры состояния: жёсткий лимит талии (см), базовую талию фазы (см). Не
    переданные поля не трогаются."""
    try:
        state = files.load_state(_STATE_PATH)
        changed = coach_state.update_waist_limits(
            state, waist_limit_cm=waist_limit_cm, waist_base_cm=waist_base_cm
        )
        if not changed:
            return _result(_err("Не передано ни одного параметра для обновления."))
        files.save_state(_STATE_PATH, state)
        return _result(
            {
                "ok": True,
                "summary": "Обновлено: " + ", ".join(changed) + ".",
                "state": state,
            }
        )
    except ValueError as exc:
        return _result(_err(str(exc)))
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_mark_support_week(day: str | None = None, active: bool = True) -> CallToolResult:
    """Отметить (active=true) или снять (active=false) НЕДЕЛЮ ПОДДЕРЖКИ — неделю на уровне TDEE
    внутри дефицита (стратегия: 4 недели дефицита → 1 неделя поддержки, либо по стоп-сигналу «два
    утра подряд разбитость без причины»). day — любая дата этой недели, YYYY-MM-DD, по умолчанию
    сегодня; неделя считается пн–вс. На ней и две недели после матрица питания калории не трогает,
    её замеры не входят в тренд; план и отчёт видят флаг в контексте. Только по явной просьбе
    атлета."""
    try:
        when = date.fromisoformat(day) if day else date.today()
        state = files.load_state(_STATE_PATH)
        monday, sunday, changed = coach_state.mark_support_week(state, when, active=active)
        files.save_state(_STATE_PATH, state)
        week = f"{monday.isoformat()} – {sunday.isoformat()}"
        if not changed:
            summary = f"Неделя {week} " + (
                "уже отмечена неделей поддержки." if active else "и так не была неделей поддержки."
            )
        else:
            summary = f"Неделя {week} " + (
                "отмечена неделей поддержки." if active else "больше не неделя поддержки."
            )
        return _result(
            {
                "ok": True,
                "summary": summary,
                "changed": changed,
                "week_start": monday.isoformat(),
                "week_end": sunday.isoformat(),
                "support_weeks": state["support_weeks"],
            }
        )
    except ValueError as exc:
        return _result(_err(f"Неверная дата: {exc}"))
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_update_profile(block: str, text: str | None = None) -> CallToolResult:
    """Заменить текст ОДНОГО блока профиля атлета (coach_profile.json); пустой text удаляет блок.
    Только по явной просьбе пользователя — профиль содержит персональный/медицинский контекст.
    Предыдущая версия файла сохраняется рядом (.bak-таймстамп)."""
    try:
        profile = files.update_profile_block(_PROFILE_PATH, block, text)
        replaced = text is not None and str(text).strip()
        return _result(
            {
                "ok": True,
                "summary": (
                    f"Профиль обновлён: блок «{str(block).strip()}» заменён."
                    if replaced
                    else f"Блок «{str(block).strip()}» удалён из профиля."
                ),
                "blocks": list(profile.get("blocks", {}).keys()),
                "updated": profile.get("updated"),
                "profile_path": str(_PROFILE_PATH),
            }
        )
    except recommender.RecommendationError as exc:
        return _result(_err(str(exc)))
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


# --- инструменты: замеры талии ------------------------------------------------
@mcp.tool()
def coach_add_waist(
    waist_cm: float,
    entry_date: str | None = None,
    notes: str | None = None,
    user_id: int | None = None,
) -> CallToolResult:
    """Записать замер талии в см (утром натощак, по пупку). entry_date по умолчанию — сегодня;
    повторный замер за ту же дату перезаписывается."""
    try:
        uid = _uid(user_id)
        payload = {
            "entry_date": entry_date or date.today().isoformat(),
            "waist": waist_cm,
            "notes": notes,
        }
        entry, created = STORE.save_waist(uid, payload)
        return _result(
            {
                "ok": True,
                "summary": (
                    f"Талия {entry['waist']:g} см за {entry['entry_date']} "
                    + ("записана." if created else "обновлена.")
                ),
                "user_id": uid,
                "entry": entry,
                "created": created,
            }
        )
    except ValueError as exc:
        return _result(_err(f"Неверные данные: {exc}"))
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_add_measurement(
    kind: str,
    value_cm: float,
    entry_date: str | None = None,
    notes: str | None = None,
    user_id: int | None = None,
) -> CallToolResult:
    """Записать обхват тела (см, лентой) кроме талии — метрики цели. kind: arm_relaxed (рука
    расслабленно), arm_flexed (рука в напряжении), forearm, shoulders (плечи через дельты), chest
    (грудь по соскам на выдохе), neck (шея под кадыком), thigh (бедро), hips (ягодицы). entry_date
    по умолчанию — сегодня; повторный замер того же вида за ту же дату перезаписывается. Читает
    недельный отчёт; план обхваты не читает."""
    try:
        uid = _uid(user_id)
        payload = {
            "entry_date": entry_date or date.today().isoformat(),
            "kind": kind,
            "value_cm": value_cm,
            "notes": notes,
        }
        entry, created = STORE.save_measurement(uid, payload)
        label = limits.MEASUREMENT_KINDS.get(str(entry["kind"]), str(entry["kind"]))
        return _result(
            {
                "ok": True,
                "summary": (
                    f"{label.capitalize()}: {entry['value_cm']:g} см за {entry['entry_date']} "
                    + ("записано." if created else "обновлено.")
                ),
                "user_id": uid,
                "entry": entry,
                "created": created,
            }
        )
    except ValueError as exc:
        return _result(_err(f"Неверные данные: {exc}"))
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_list_measurements(kind: str | None = None, user_id: int | None = None) -> CallToolResult:
    """История обхватов кроме талии (старые сверху): все виды или один kind (см. coach_add_measurement)."""
    try:
        uid = _uid(user_id)
        entries = STORE.list_measurements(uid, kind=kind)
        return _result(
            {
                "ok": True,
                "summary": f"Обхватов: {len(entries)}.",
                "user_id": uid,
                "kinds": limits.MEASUREMENT_KINDS,
                "entries": entries,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_delete_measurement(entry_id: int, user_id: int | None = None) -> CallToolResult:
    """Удалить обхват по id (например, опечатку)."""
    try:
        uid = _uid(user_id)
        deleted = STORE.delete_measurement(uid, int(entry_id))
        if deleted is None:
            return _result(_err(f"Обхват #{entry_id} не найден."))
        label = limits.MEASUREMENT_KINDS.get(str(deleted["kind"]), str(deleted["kind"]))
        return _result(
            {
                "ok": True,
                "summary": f"Удалён обхват «{label}» {deleted['value_cm']:g} см за {deleted['entry_date']}.",
                "user_id": uid,
                "deleted": deleted,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_list_waists(user_id: int | None = None) -> CallToolResult:
    """История замеров талии (старые сверху)."""
    try:
        uid = _uid(user_id)
        entries = STORE.list_waists(uid)
        return _result(
            {
                "ok": True,
                "summary": f"Замеров талии: {len(entries)}.",
                "user_id": uid,
                "entries": entries,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_delete_waist(entry_id: int, user_id: int | None = None) -> CallToolResult:
    """Удалить замер талии по id (например, опечатку)."""
    try:
        uid = _uid(user_id)
        deleted = STORE.delete_waist(uid, int(entry_id))
        if deleted is None:
            return _result(_err(f"Замер #{entry_id} не найден."))
        return _result(
            {
                "ok": True,
                "summary": f"Удалён замер {deleted['waist']:g} см за {deleted['entry_date']}.",
                "user_id": uid,
                "deleted": deleted,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


# --- инструменты: события (перерывы в тренировках с причиной) -----------------
def _event_period(entry: dict[str, Any]) -> str:
    """Период события строкой: открытое, однодневное или отрезок."""
    if entry["end_date"] is None:
        return f"с {entry['start_date']}, идёт"
    if entry["end_date"] == entry["start_date"]:
        return entry["start_date"]
    return f"{entry['start_date']} — {entry['end_date']}"


@mcp.tool()
def coach_list_events(user_id: int | None = None) -> CallToolResult:
    """События — периоды без тренировок с причиной («болел», «командировка»), новые сверху.
    end_date = null означает, что событие идёт прямо сейчас; такое событие одно."""
    try:
        uid = _uid(user_id)
        entries = STORE.list_events(uid)
        # Открытое достаётся фильтром по тому же списку — отдельного запроса не нужно,
        # а модели важно именно оно: им объясняется сегодняшний день.
        open_event = next((entry for entry in entries if entry["end_date"] is None), None)
        summary = f"Событий: {len(entries)}."
        if open_event is not None:
            summary += f" Открытое ({_event_period(open_event)}): {open_event['text']}"
        return _result(
            {
                "ok": True,
                "summary": summary,
                "user_id": uid,
                "entries": entries,
                "open_event": open_event,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_add_event(
    text: str,
    start_date: str | None = None,
    end_date: str | None = None,
    user_id: int | None = None,
) -> CallToolResult:
    """Записать событие — период без тренировок с причиной. start_date по умолчанию сегодня,
    пустой end_date означает «ещё идёт»; будущие даты запрещены — событие описывает то, что уже
    случилось. Открытое событие одно: пока оно не закрыто, второе открытое записать нельзя.
    Ключа дедупа у события нет, повторный вызов создаст вторую запись."""
    try:
        uid = _uid(user_id)
        payload = {
            "start_date": start_date or date.today().isoformat(),
            "end_date": end_date,
            "text": text,
        }
        entry = STORE.save_event(uid, payload)
        return _result(
            {
                "ok": True,
                "summary": f"Событие записано ({_event_period(entry)}): {entry['text']}",
                "user_id": uid,
                "entry": entry,
            }
        )
    except ValueError as exc:
        return _result(_err(f"Событие не записано: {exc}"))
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_update_event(
    event_id: int,
    text: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    user_id: int | None = None,
) -> CallToolResult:
    """Поправить событие: текст и даты. Не переданные поля не трогаются, поэтому «событие
    закончилось вчера» — это один вызов с end_date. Чтобы снова открыть событие, передай
    end_date="" — пустая строка означает «ещё идёт»."""
    try:
        uid = _uid(user_id)
        eid = int(event_id)
        current = next((entry for entry in STORE.list_events(uid) if entry["id"] == eid), None)
        if current is None:
            return _result(_err(f"Событие #{eid} не найдено."))
        # Хранилище перезаписывает запись целиком, а в разговоре меняют одно поле
        # («всё, вышел вчера»): недостающее берём из текущей записи. В хранилище
        # None и "" у end_date значат одно, поэтому здесь None — это «не передавали»,
        # а пустая строка — явная просьба открыть событие снова.
        payload = {
            "start_date": current["start_date"] if start_date is None else start_date,
            "end_date": current["end_date"] if end_date is None else end_date,
            "text": current["text"] if text is None else text,
        }
        updated = STORE.update_event(uid, eid, payload)
        if updated is None:
            return _result(_err(f"Событие #{eid} не найдено."))
        return _result(
            {
                "ok": True,
                "summary": f"Событие обновлено ({_event_period(updated)}): {updated['text']}",
                "user_id": uid,
                "entry": updated,
            }
        )
    except ValueError as exc:
        return _result(_err(f"Событие не обновлено: {exc}"))
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_delete_event(event_id: int, user_id: int | None = None) -> CallToolResult:
    """Удалить событие по id (например, записанное по ошибке)."""
    try:
        uid = _uid(user_id)
        deleted = STORE.delete_event(uid, int(event_id))
        if deleted is None:
            return _result(_err(f"Событие #{event_id} не найдено."))
        return _result(
            {
                "ok": True,
                "summary": f"Удалено событие ({_event_period(deleted)}): {deleted['text']}",
                "user_id": uid,
                "deleted": deleted,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


# --- инструменты: отладка и генерация советов ---------------------------------
@mcp.tool()
def coach_get_stored_recommendation(user_id: int | None = None) -> CallToolResult:
    """Текущая рекомендация из кэша приложения (status, based_on, payload, токены, ошибка)."""
    try:
        uid = _uid(user_id)
        rec = STORE.get_recommendation(uid)
        latest = STORE.get_latest_workout_id(uid)
        if rec is None:
            return _result(
                {
                    "ok": True,
                    "summary": "Рекомендации в кэше ещё нет.",
                    "user_id": uid,
                    "status": "none",
                }
            )
        rec["stale"] = bool(
            rec.get("status") == "ready" and rec.get("based_on_workout_id") != latest
        )
        rec.update(
            {
                "ok": True,
                "user_id": uid,
                "latest_workout_id": latest,
                "summary": f"Статус: {rec.get('status')}.",
            }
        )
        return _result(rec)
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_preview_prompt(limit: int = 20, user_id: int | None = None) -> CallToolResult:
    """Показать ТОЧНЫЙ промпт (system + user) и JSON-схему, которые уйдут в Claude. Без вызова API (бесплатно)."""
    try:
        uid = _uid(user_id)
        catalog = _catalog()
        workouts = STORE.list_workouts(uid)
        body_weights = STORE.list_body_weights(uid)
        waists = STORE.list_waists(uid)
        events = STORE.list_events(uid)
        profile = files.load_profile(_PROFILE_PATH)
        state = files.load_state(_STATE_PATH)
        today = date.today()
        # state обязателен: без него политика фаз рендерится из дефолтов, и
        # preview показывает не тот промпт, который уйдёт в модель.
        system = prompt_builder._build_system_prompt(
            catalog, profile, state, files.load_strategy(_STRATEGY_PATH)
        )
        user = prompt_builder._build_user_prompt(
            workouts,
            body_weights,
            today,
            limit,
            catalog=catalog,
            state=state,
            waists=waists,
            events=events,
            previous=STORE.get_recommendation(uid),
        )
        schema = prompt_builder._build_schema(catalog)
        return _result(
            {
                "ok": True,
                "summary": "Промпт собран (без обращения к модели).",
                "profile_loaded": profile is not None,
                "user_id": uid,
                "model": recommender.DEFAULT_MODEL,
                "phase": coach_state.phase_params(state)["phase"],
                "cycle_position": coach_state.cycle_position(state, workouts, today),
                "history_raw": min(limit, prompt_builder.RAW_HISTORY_COUNT, len(workouts)),
                "system_prompt": system,
                "user_prompt": user,
                "json_schema": schema,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_debug_recommendation(limit: int = 20, user_id: int | None = None) -> CallToolResult:
    """Глубокая отладка: полный прогон генерации с семантическим валидатором — попытки модели (сырой
    ответ + нарушения + репромпт), итог и токены/стоимость.

    Ничего не записывает в базу приложения."""
    try:
        uid = _uid(user_id)
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return _result(_err("ANTHROPIC_API_KEY не задан в окружении."))
        catalog = _catalog()
        workouts = STORE.list_workouts(uid)
        if not workouts:
            return _result(_err("Нет истории тренировок для генерации."))
        body_weights = STORE.list_body_weights(uid)
        waists = STORE.list_waists(uid)
        events = STORE.list_events(uid)
        profile = files.load_profile(_PROFILE_PATH)
        state = files.load_state(_STATE_PATH)
        today = date.today()
        model = recommender.DEFAULT_MODEL
        user = prompt_builder._build_user_prompt(
            workouts,
            body_weights,
            today,
            limit,
            catalog=catalog,
            state=state,
            waists=waists,
            events=events,
            previous=STORE.get_recommendation(uid),
        )

        validated = None
        error = None
        trace: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        try:
            validated, usage, model, trace = recommender.generate_with_trace(
                workouts,
                body_weights,
                catalog,
                profile=profile,
                strategy=files.load_strategy(_STRATEGY_PATH),
                state=state,
                waists=waists,
                events=events,
                previous=STORE.get_recommendation(uid),
                today=today,
                history_limit=limit,
            )
        except recommender.RecommendationError as exc:
            error = str(exc)
            trace = getattr(exc, "trace", trace)
            usage = recommender._sum_usage(*(a.get("usage", {}) for a in trace))

        return _result(
            {
                "ok": error is None,
                "summary": (
                    f"Готово за {len(trace)} попытк{'у' if len(trace) == 1 else 'и'} "
                    "(сырые ответы и нарушения — в attempts)."
                    if error is None
                    else f"Генерация не прошла валидатор: {error}"
                ),
                "user_id": uid,
                "model": model,
                "attempts": trace,
                "validated": validated,
                "error": error,
                "usage": usage,
                "cost": _estimate_cost(model, usage),
                "user_prompt": user,
            }
        )
    except recommender.RecommendationError as exc:
        return _result(_err(str(exc)))
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка отладки: {exc}"))


@mcp.tool()
def coach_weekly_report(
    days: int = 7, fresh: bool = False, user_id: int | None = None
) -> CallToolResult:
    """Недельный отчёт тренера (Markdown): итоги недели, прогресс/ПР, вес и талия, дисциплина, фокус
    следующей недели. Отчёт всегда про последнюю ЗАКРЫТУЮ неделю (пн–вс) и отдаётся из кэша
    мгновенно и бесплатно (таймер в ночь на понедельник генерирует его сам); fresh=true —
    перегенерировать за токены."""
    try:
        uid = _uid(user_id)
        # Тот же якорь, что и у таймера (weekly_report.py), иначе промах мимо кэша.
        period = coach_state.last_closed_week_end(date.today())
        period_end = period.isoformat()
        if not fresh:
            cached = STORE.get_coach_report(uid, period_end, days)
            if cached:
                return _result(
                    {
                        "ok": True,
                        "summary": cached["report"],
                        "user_id": uid,
                        "cached": True,
                        "model": cached.get("model"),
                        "report": cached["report"],
                        "period_end": period_end,
                    }
                )
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return _result(_err("ANTHROPIC_API_KEY не задан в окружении."))
        # Прошлый отчёт (период на `days` раньше) — память о «Фокусе следующей
        # недели», тот же поиск, что у таймера weekly_report.py.
        previous = STORE.get_coach_report(uid, (period - timedelta(days=days)).isoformat(), days)
        report, usage, model = recommender.generate_weekly_report(
            STORE.list_workouts(uid),
            STORE.list_body_weights(uid),
            STORE.list_waists(uid),
            _catalog(),
            profile=files.load_profile(_PROFILE_PATH),
            strategy=files.load_strategy(_STRATEGY_PATH),
            state=files.load_state(_STATE_PATH),
            events=STORE.list_events(uid),
            measurements=STORE.list_measurements(uid),
            previous_report=previous["report"] if previous else None,
            today=period,
            days=days,
        )
        STORE.save_coach_report(
            uid,
            period_end,
            days,
            report,
            model,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
        )
        return _result(
            {
                "ok": True,
                "summary": report,
                "user_id": uid,
                "cached": False,
                "model": model,
                "report": report,
                "period_end": period_end,
                "usage": usage,
                "cost": _estimate_cost(model, usage),
            }
        )
    except recommender.RecommendationError as exc:
        return _result(_err(str(exc)))
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка отчёта: {exc}"))


@mcp.tool()
def coach_phase_summary(
    history_index: int | None = None, user_id: int | None = None
) -> CallToolResult:
    """Итоги фазы подготовки: длительность, тренировки и частота, вес/талия старт→финиш с темпом, ПР за
    фазу, дисциплина. Без аргументов — текущая фаза; history_index (0 = самая старая) — завершённая
    фаза из журнала переходов."""
    try:
        uid = _uid(user_id)
        state = files.load_state(_STATE_PATH)
        today = date.today()
        history = state.get("phase_history") or []

        if history_index is None:
            phase = str(state.get("phase") or "cut_recomp")
            started_raw = state.get("phase_started")
            if not started_raw:
                return _result(_err("У текущей фазы нет даты старта (phase_started)."))
            started, ended = date.fromisoformat(started_raw), today
        else:
            index = int(history_index)
            if not 0 <= index < len(history):
                return _result(
                    _err(f"history_index={index} вне журнала (закрытых фаз: {len(history)}).")
                )
            entry = history[index]
            if not entry.get("started") or not entry.get("ended"):
                return _result(_err("У этой записи журнала нет полных дат."))
            phase = entry["phase"]
            started = date.fromisoformat(entry["started"])
            ended = date.fromisoformat(entry["ended"])

        summary = coach_features.phase_summary(
            STORE.list_workouts(uid),
            STORE.list_body_weights(uid),
            STORE.list_waists(uid),
            _catalog(),
            phase=phase,
            started=started,
            ended=ended,
        )
        text = prompt_builder.render_phase_summary(summary)
        return _result(
            {
                "ok": True,
                "summary": text,
                "user_id": uid,
                "current": history_index is None,
                "phase_summary": summary,
                "phase_history": history,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_costs(user_id: int | None = None) -> CallToolResult:
    """Расходы на Claude API по месяцам: генерации рекомендаций и недельные отчёты — вызовы, токены, оценка в USD."""
    try:
        uid = _uid(user_id)
        rows = STORE.token_spend(uid)
        total_usd = 0.0
        enriched = []
        for row in rows:
            cost = _estimate_cost(
                row.get("model") or "",
                {"input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"]},
            )
            usd = cost["usd"] if cost else None
            if usd:
                total_usd += usd
            enriched.append({**row, "usd": usd})
        lines = [
            f"  {row['month']} {row['source']} [{row.get('model') or '?'}]: "
            f"{row['calls']} выз., {row['input_tokens']} in / {row['output_tokens']} out"
            + (f", ~${row['usd']:.2f}" if row.get("usd") else "")
            for row in enriched
        ]
        return _result(
            {
                "ok": True,
                "summary": (
                    "Расходы по месяцам:\n" + "\n".join(lines) + f"\nИтого: ~${total_usd:.2f}"
                    if enriched
                    else "Журнал вызовов пуст."
                ),
                "user_id": uid,
                "months": enriched,
                "total_usd": round(total_usd, 2),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка: {exc}"))


@mcp.tool()
def coach_generate_recommendation(
    limit: int = 20, store: bool = False, user_id: int | None = None
) -> CallToolResult:
    """Сгенерировать новую рекомендацию (валидированную). store=false (по умолчанию) — НЕ писать в базу
    приложения; store=true — обновить кэш, который видит приложение."""
    try:
        uid = _uid(user_id)
        workouts = STORE.list_workouts(uid)
        body_weights = STORE.list_body_weights(uid)
        catalog = _catalog()
        recommendation, usage, model = recommender.generate(
            workouts,
            body_weights,
            catalog,
            profile=files.load_profile(_PROFILE_PATH),
            strategy=files.load_strategy(_STRATEGY_PATH),
            state=files.load_state(_STATE_PATH),
            waists=STORE.list_waists(uid),
            events=STORE.list_events(uid),
            previous=STORE.get_recommendation(uid),
            history_limit=limit,
        )
        stored = None
        if store:
            based_on = STORE.get_latest_workout_id(uid)
            stored = STORE.save_recommendation(
                uid,
                based_on,
                len(workouts),
                model,
                recommendation,
                usage.get("input_tokens"),
                usage.get("output_tokens"),
            )
        return _result(
            {
                "ok": True,
                "summary": (
                    "Сгенерировано и сохранено в кэш приложения."
                    if store
                    else "Сгенерировано (в базу НЕ записано)."
                ),
                "user_id": uid,
                "model": model,
                "stored": bool(store),
                "recommendation": recommendation,
                "usage": usage,
                "cost": _estimate_cost(model, usage),
                "stored_row": stored,
            }
        )
    except recommender.RecommendationError as exc:
        return _result(_err(str(exc)))
    except Exception as exc:  # noqa: BLE001
        return _result(_err(f"Ошибка генерации: {exc}"))


# --- ASGI-middleware с bearer-токеном (та же форма, что в investor-mcp) --------
class _BearerAuthMiddleware:
    """Требовать ``Authorization: Bearer <token>`` на HTTP; включается, только когда токен задан."""

    def __init__(self, app: Any, token: str) -> None:
        """Обернуть ASGI-приложение и запомнить ожидаемый токен."""
        self.app = app
        self.token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Не-HTTP scope пропустить как есть; HTTP без верного токена — 401 JSON."""
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if auth != f"Bearer {self.token}":
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)


def main() -> None:
    """CLI: stdio по умолчанию; streamable-http с хостом, портом, путём и защитой
    от DNS-rebinding, а при заданном токене — через uvicorn с bearer-middleware.
    """
    parser = argparse.ArgumentParser(description="Coach MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default=os.getenv("COACH_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("COACH_MCP_PORT", "8001")))
    parser.add_argument("--mcp-path", default=os.getenv("COACH_MCP_PATH", "/mcp"))
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run()
        return

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.settings.streamable_http_path = args.mcp_path
    allowed = os.getenv("COACH_MCP_ALLOWED_HOSTS")
    if allowed:
        hosts = [h.strip() for h in allowed.split(",") if h.strip()]
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[f"https://{h}" for h in hosts],
        )
    else:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )

    token = os.getenv("COACH_MCP_AUTH_TOKEN")
    if token:
        import uvicorn

        uvicorn.run(
            _BearerAuthMiddleware(mcp.streamable_http_app(), token),
            host=args.host,
            port=args.port,
            log_level="info",
        )
    else:
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
