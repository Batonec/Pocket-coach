#!/usr/bin/env python3
"""Сборка текста, который читает модель.

Всё, что уезжает в промпт, собирается здесь: системный промпт (профиль атлета,
семантика каталога, политика фаз, срез стратегии), user-промпт (контекст,
вычисленные фичи, сырая история вперемешку с событиями и заметками), JSON-схема
ответа и промпт недельного отчёта. Проза живёт в prompts/*.md и подставляется
через coach_prompts; этот модуль только считает слоты из данных атлета и
складывает блоки в нужном порядке. Если сюда просится фраза, а не вычисление,
ей место в markdown.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from trainer.coach import coach_features, coach_prompts, coach_state, plan_validator

# Raw history shown to the model; everything older is covered by the computed
# per-exercise summaries (the prompt must not grow from the feature work).
RAW_HISTORY_COUNT = 10

# Потолок хроники событий. Окна по датам у неё нет намеренно: событие любой
# давности всё ещё объясняет дырку в датах, а сводки его не заменяют — из
# событий не считается ни одного числа. Единственная страховка от промпта,
# растущего без границы, — потолок по строкам; об обрезке блок говорит вслух.
MAX_EVENT_LINES = 40


_EFFORT_MARK = {"easy": "-", "ok": "", "hard": "+"}

# What each catalog machine actually is (from the athlete's own descriptions) —
# the terse RU names alone don't tell the model which muscle works. Id 1 is a
# catalog duplicate of 18: history rows are re-mapped onto 18 during
# serialization, so the model never sees it.
CATALOG_SEMANTICS: dict[int, str] = {
    18: "рычажный жим сидя от груди, горизонтальный — грудь (вся), вторично трицепс и передняя дельта",
    17: "пек-дек «бабочка» — изоляция груди",
    9: "РЫЧАЖНАЯ вертикальная тяга (хаммер) с двумя сходящимися ручками, имитация "
    "подтягиваний — широчайшие, вторично бицепс",
    10: "рычажная горизонтальная тяга (хаммер) — толщина спины (середина трапеции, ромбовидные), вторично бицепс",
    13: "махи в тренажёре с упором в локти, сидя — средняя дельта",
    19: "тренажёр обратных махов (сидя, разведение рук назад) — задняя дельта. Шаг стека 2.5 кг",
    11: "тренажёр на сгибание рук — бицепс",
    12: "трицепс на блоке вниз (ручка варьируется: прямая/канат) — трицепс",
    8: "жим ногами в платформе 45° — квадрицепс + ягодичные",
    16: "разгибания ног сидя — квадрицепс (изоляция)",
    15: "сгибания ног лёжа — бицепс бедра",
}

# Muscles the athlete CANNOT train with the current catalog — standing context
# so the model knows they sit at zero structurally, not by athlete's laziness.
CATALOG_GAPS = "икры, пресс, разгибатели спины — упражнений в каталоге нет"

# Разделы рабочего документа стратегии, которые уходят в системный промпт.
# Резать по ЗАГОЛОВКУ, а не по номеру: атлет перенумеровывает разделы, правя
# документ, и срез по «## 4.» начал бы молча отдавать не ту главу.
# Не берём: «Как это читать» и «С чего начинаем» (мета для человека), «Питание»
# и «Измерения» (числа уже приходят в КОНТЕКСТЕ, протокол — прозой в профиле),
# «Расхождения с vision» (честность для атлета, для генерации шум) и раздел
# следующего этапа — план строится по текущему.
STRATEGY_SECTIONS = [
    "Скелет: семь фаз",
    "Ф0 — возврат (недели 1–4)",
    "Ф1 — рекомпозиция (недели 5–17, около 12–14)",
    "Тренировочные дни",
    "Прогрессия",
    "Пробел каталога и зачем в плане ноги",
    "Если выпала неделя",
]


def _render_program(strategy: str | None) -> str:
    """Слот {{program}}: срез стратегии либо пустая строка.

    Пустая строка намеренно: секция появляется целиком или не появляется
    вовсе — иначе в промпте остался бы заголовок без содержания.
    """
    if not strategy:
        return ""
    body, missing = coach_prompts.document_sections(strategy, STRATEGY_SECTIONS)
    parts = [_block("program_header")]
    if missing:
        parts.append(_block("program_missing", sections=", ".join(missing)))
    if body:
        parts.append(body)
    return "\n\n".join(parts) + "\n\n"


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

        summary = (
            f"Последняя тренировка по твоему плану ({workout.get('workout_date')}): "
            f"{done}/{total} плановых подходов"
        )
        if deviations:
            summary += "; отклонения: " + "; ".join(deviations[:4])
        return summary + "."
    return None


# --------------------------------------------------------------------------- #
# Prompt building
# --------------------------------------------------------------------------- #
def _athlete_text(value: object) -> str:
    """Дословный текст атлета одной строкой.

    Переносы и лишние пробелы схлопываются: одна тренировка обязана остаться
    одной строкой хроники, иначе заметка в две строки разорвёт формат, по
    которому модель читает историю.
    """
    return " ".join(str(value or "").split())


def _quoted(value: object) -> str:
    """Слова атлета в «ёлочках» — по ним модель отличает его текст от наших
    вычисленных данных. Пустая заметка не даёт пустых кавычек."""
    text = _athlete_text(value)
    return f"«{text}»" if text else ""


def _serialize_workout(workout: dict[str, Any], names_by_id: dict[int, str] | None = None) -> str:
    data = workout.get("data", {}) or {}
    # A session logged without a coach card has no load label — say so, rather
    # than print «?», which the model reads as missing data.
    load_type = data.get("load_type") or "без плана"
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
            # Заметка стоит вплотную к своему подходу: она объясняет ИМЕННО
            # его вес (канат вместо ручки, другая скамья), и в конце строки
            # эта связь потерялась бы.
            note = _quoted(workout_set.get("notes"))
            sets_repr.append(f"{weight_repr}кг×{reps}{mark}{rir_repr}{' ' + note if note else ''}")
        if sets_repr:
            parts.append(f"{name} {', '.join(sets_repr)}")
    body = "; ".join(parts) if parts else "(нет подходов)"
    line = f"{workout.get('workout_date', '?')} [{load_type}] {body}"
    session_note = _quoted(data.get("notes"))
    # Заметка ко всей сессии — после тире в хвосте строки: разбор «дата
    # [нагрузка] упражнения» до неё не доходит, добавление ничего не ломает.
    return f"{line} — {session_note}" if session_note else line


def _serialize_event(event: dict[str, Any]) -> str:
    """Строка события в хронике — тот же скелет, что у тренировки: дата,
    маркер в квадратных скобках, дальше содержание."""
    start = str(event.get("start_date") or "?")
    end = str(event.get("end_date") or "").strip()
    if not end:
        period = f"{start} — идёт"
    elif end == start:
        period = start
    else:
        period = f"{start} — {end}"
    return f"{period} [событие] {_quoted(event.get('text'))}"


def _clip_events(events: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], int]:
    """Хроника от старых к новым и число опущенных событий.

    Режем самые старые: свежее событие объясняет разрыв, до которого модель
    ещё дойдёт, старое — давно прочитанный. Второе значение нужно, чтобы блок
    назвал обрезку вслух: урезанная хроника, прочитанная как полная, врёт про
    причины пауз.
    """
    ordered = sorted(events or [], key=lambda event: str(event.get("start_date") or ""))
    if len(ordered) <= MAX_EVENT_LINES:
        return ordered, 0
    return ordered[-MAX_EVENT_LINES:], len(ordered) - MAX_EVENT_LINES


def _open_event(events: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Идущее событие — то, у которого нет конца. Хранилище держит его
    единственным; если в данных их всё же несколько, берём самое свежее."""
    ongoing = [e for e in events or [] if not str(e.get("end_date") or "").strip()]
    if not ongoing:
        return None
    return max(ongoing, key=lambda event: str(event.get("start_date") or ""))


def _days_inclusive(value: object, today: date) -> int | None:
    """Длительность идущего события в днях, где день начала — первый."""
    try:
        start = date.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return max(1, (today - start).days + 1)


def _events_in_period(
    events: list[dict[str, Any]] | None, start: date, end: date
) -> list[dict[str, Any]]:
    """События, пересекающиеся с периодом отчёта: событие без конца считается
    идущим до конца периода, поэтому в отчёт попадает."""
    picked: list[dict[str, Any]] = []
    for event in events or []:
        try:
            begins = date.fromisoformat(str(event.get("start_date") or ""))
        except ValueError:
            continue
        raw_end = str(event.get("end_date") or "").strip()
        try:
            finishes = date.fromisoformat(raw_end) if raw_end else end
        except ValueError:
            finishes = end
        if begins <= end and finishes >= start:
            picked.append(event)
    return picked


def _serialize_history(
    workouts: list[dict[str, Any]],
    limit: int,
    catalog: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> str:
    # list_workouts() returns newest-first; take the most recent `limit`
    # and present oldest -> newest so progression reads naturally.
    names_by_id = {item["id"]: item["name"] for item in catalog} if catalog else None
    recent = list(workouts[:limit])
    recent.reverse()
    # События идут не отдельным списком, а вперемешку с тренировками: разрыв
    # в датах объясняется ровно там, где он виден. На общей дате событие стоит
    # первым — сначала обстоятельство, потом сессия.
    rows: list[tuple[str, int, str]] = [
        (str(w.get("workout_date") or ""), 1, _serialize_workout(w, names_by_id)) for w in recent
    ]
    rows += [(str(e.get("start_date") or ""), 0, _serialize_event(e)) for e in events or []]
    rows.sort(key=lambda row: row[:2])
    return "\n".join(row[2] for row in rows)


def _render_attendance(workouts: list[dict[str, Any]], today: date) -> str:
    """Training days per calendar week — the fact behind both the split switch
    («каркас включается, когда атлет держит частоту») and the attendance gate
    of the programme. Shared by the plan and the weekly report."""
    rows = coach_features.weekly_attendance(workouts, today)
    return _block(
        "attendance",
        weeks=coach_features.render_weekly_attendance(rows, today),
        streak_three=str(coach_features.attendance_streak(rows, 3)),
        streak_four=str(coach_features.attendance_streak(rows, 4)),
    )


def _render_stall(
    workouts: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    matrix: dict[str, Any],
    params: dict[str, Any],
    state: dict[str, Any],
    today: date,
) -> str:
    """Preconditions and stall over the ACTIVE window: it starts at the block
    anchor (phase start / return after a ≥14-day break), so a vacation cannot
    dilute the frequency, and volume thresholds come from the phase's own
    per-group targets."""
    report = coach_features.stall_report(
        workouts,
        summaries,
        matrix.get("trend_per_week"),
        params["phase"],
        params.get("rate_kg_per_week"),
        today,
        since=coach_state._block_anchor(state, workouts, today),
        group_targets=params.get("group_targets"),
    )
    return coach_features.render_stall_report(report)


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


# Шаблон политики фаз читается один раз: он не зависит от данных, а слоты нужны
# для сборки — рендерить только то, что шаблон реально просит.
_BLOCKS = coach_prompts.fragments("user_blocks")
_PHASE_POLICY_TEMPLATE = coach_prompts.load("phase_policy")
_PHASE_POLICY_SLOTS = coach_prompts.slots(_PHASE_POLICY_TEMPLATE)


def _block(name: str, **values: str) -> str:
    """Подпись к блоку промпта из prompts/user_blocks.md."""
    return coach_prompts.render(_BLOCKS[name], **values)


def _format_range(bounds: Any, unit: str = "") -> str:
    if isinstance(bounds, (tuple, list)) and len(bounds) == 2:
        return f"{bounds[0]:g}–{bounds[1]:g}{unit}"
    return f"{bounds}{unit}"


def _format_number(value: Any) -> str:
    """A phase parameter rendered as a single number.

    Overrides are validated on write, but only loosely: a range key may arrive
    as a scalar and a scalar key as a range. The prompt must never crash over
    it — a broken parameter has to reach the athlete as odd text, not as a
    failed generation.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}"
    return str(value)


def _format_low(bounds: Any) -> str:
    """Lower edge of a range parameter; a scalar override stands for itself."""
    if isinstance(bounds, (tuple, list)) and bounds:
        return _format_number(bounds[0])
    return _format_number(bounds)


def _render_phase_policy(state: dict[str, Any] | None = None) -> str:
    """Phase policy for the system prompt; the prose lives in
    ``prompts/phase_policy.md`` and only the numbers are computed here.

    The ACTIVE phase is rendered from the athlete's merged parameters
    (``phase_params`` overrides on top of the defaults) — otherwise the prompt
    would carry the stock numbers while the КОНТЕКСТ block carries the real
    ones, and the model gets two contradicting methodologies in one request.
    The two inactive phases keep the stock numbers: they are background, and
    they are re-set on the switch anyway.
    """
    merged = coach_state.phase_params(state) if state is not None else None
    short = {"cut_recomp": "cut", "lean_bulk": "bulk", "maintenance": "maint"}
    ranges = {"calories", "session_sets", "ramp_start", "ramp_cap", "sets_per_group"}
    values: dict[str, str] = {}
    for phase, prefix in short.items():
        params = (
            merged
            if merged is not None and merged.get("phase") == phase
            else coach_state.PHASE_DEFAULTS[phase]
        )
        for key in ("title", "rate_text", "frequency_text"):
            if f"{prefix}_{key}" in _PHASE_POLICY_SLOTS:
                values[f"{prefix}_{key}"] = str(params.get(key, ""))
        for key in ranges:
            if f"{prefix}_{key}" in _PHASE_POLICY_SLOTS:
                values[f"{prefix}_{key}"] = _format_range(params.get(key))
        if f"{prefix}_protein_g" in _PHASE_POLICY_SLOTS:
            values[f"{prefix}_protein_g"] = (
                _format_low(params.get("protein_g"))
                if phase == "maintenance"
                else _format_range(params.get("protein_g"))
            )
        if f"{prefix}_ceiling_weight_kg" in _PHASE_POLICY_SLOTS:
            values[f"{prefix}_ceiling_weight_kg"] = _format_number(params.get("ceiling_weight_kg"))
    return coach_prompts.render(_PHASE_POLICY_TEMPLATE, **values)


def _build_system_prompt(
    catalog: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    strategy: str | None = None,
) -> str:
    """Assemble the system prompt from the template in ``prompts/system.md``.

    The prose lives in the template; this function only computes the four slots
    it expects. Anything added here that is not a computed value belongs in the
    markdown instead.
    """
    catalog_lines = "\n".join(
        f"  {item['id']} — {item['name']}: {CATALOG_SEMANTICS.get(item['id'], 'тренажёр')}"
        for item in catalog
        if item["id"] not in coach_features.EXERCISE_ALIASES
    )
    return coach_prompts.build(
        "system",
        profile=_render_profile(profile),
        catalog=catalog_lines,
        catalog_gaps=CATALOG_GAPS,
        phase_policy=_render_phase_policy(state),
        program=_render_program(strategy),
    )


def _build_user_prompt(
    workouts: list[dict[str, Any]],
    body_weights: list[dict[str, Any]],
    today: date,
    history_limit: int,
    catalog: list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
    waists: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
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
        week_label += _block("deload_week_label", weeks=str(params.get("deload_every_weeks")))
    context_lines = [
        _block(
            "context_today",
            date=today.isoformat(),
            weekday=_RU_WEEKDAYS[today.weekday()],
        ),
        _block(
            "context_phase",
            phase=phase,
            title=str(params["title"]),
            week_label=week_label,
            calories=_format_range(params["calories"]),
            rate=str(params["rate_text"]),
            protein=_format_range(params["protein_g"]),
            session_sets=_format_range(params["session_sets"]),
        ),
    ]
    if days is None:
        context_lines.append(_block("context_last_unknown"))
    elif returning:
        context_lines.append(_block("context_returning", days=str(days)))
    else:
        context_lines.append(_block("context_last", days=str(days)))
    # Идущее событие стоит сразу за днями простоя: решение о возврате
    # принимается здесь, а без причины дырка в датах неотличима от лени.
    ongoing = _open_event(events)
    if ongoing is not None:
        open_days = _days_inclusive(ongoing.get("start_date"), today)
        context_lines.append(
            _block(
                "context_open_event",
                since=str(ongoing.get("start_date") or "?"),
                days=str(open_days) if open_days is not None else "?",
                text=_athlete_text(ongoing.get("text")),
            )
        )
    chunks = ["=== КОНТЕКСТ ===\n" + "\n".join(context_lines)]

    volume = coach_features.weekly_volume(workouts, today)
    maintenance_sets = params.get("sets_per_group") if phase == "maintenance" else None
    chunks.append(
        _block("volume_header")
        + "\n"
        + coach_features.render_weekly_volume(
            volume, week_target, maintenance_sets, params.get("group_targets")
        )
    )
    chunks.append(_render_attendance(workouts, today))

    measurement_lines = coach_features.render_measurements(body_weights, waists, today)
    matrix = coach_features.nutrition_matrix(state, params, body_weights, waists, today)
    nutrition_chunk = list(measurement_lines)
    if matrix["lines"]:
        nutrition_chunk.append(_block("nutrition_matrix", lines="; ".join(matrix["lines"])))
    if matrix["goal"]:
        nutrition_chunk.append(_block("nutrition_goal", goal=matrix["goal"]))
    if nutrition_chunk:
        chunks.append("\n".join(nutrition_chunk))

    summaries = coach_features.exercise_summaries(workouts, catalog or [], today)
    if summaries:
        chunks.append(
            _block("summaries_header") + "\n" + coach_features.render_exercise_summaries(summaries)
        )

    chunks.append(_render_stall(workouts, summaries, matrix, params, state, today))

    if returning and days is not None:
        pre_break = coach_features.render_pre_break_weights(
            coach_features.pre_break_working_weights(workouts, catalog or []), days
        )
        if pre_break:
            chunks.append(pre_break)

    ramp_items = coach_features.comeback_ramp_steps(workouts, catalog or [], today)
    if ramp_items:
        first = ramp_items[0]
        chunks.append(
            _block(
                "comeback_ramp_header",
                start=first["break_start"].isoformat(),
                end=first["break_end"].isoformat(),
                days=str((first["break_end"] - first["break_start"]).days - 1),
            )
            + "\n"
            + "\n".join(coach_features.render_comeback_ramp(ramp_items))
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
    shown_events, dropped_events = _clip_events(events)
    header = [_block("raw_history_header", count=str(raw_count))]
    if shown_events:
        header.append(_block("raw_history_events"))
    if dropped_events:
        header.append(
            _block(
                "events_truncated",
                count=str(len(shown_events)),
                total=str(len(shown_events) + dropped_events),
            )
        )
    chunks.append("\n".join(header))
    chunks.append(_serialize_history(workouts, raw_count, catalog, shown_events))
    chunks.append(_block("task"))
    return "\n\n".join(chunks)


_RU_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
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
            "load_type": {"type": "string", "enum": list(plan_validator.ALLOWED_LOAD_TYPES)},
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
# Weekly coach report
# --------------------------------------------------------------------------- #
_REPORT_TEMPLATE = coach_prompts.load("report")


def _build_report_system_prompt(
    profile: dict[str, Any] | None = None,
    strategy: str | None = None,
) -> str:
    """Системный промпт недельного отчёта.

    До этого он был константой: отчёт не получал ни профиля, ни программы, то
    есть писался про абстрактного атлета. Гейту этапа при этом негде было
    прозвучать — а гейт жёсткий, и должно существовать место, где его статус
    называют вслух.
    """
    return coach_prompts.render(
        _REPORT_TEMPLATE,
        profile=_render_profile(profile),
        program=_render_program(strategy),
    )


def _build_report_prompt(
    workouts: list[dict[str, Any]],
    body_weights: list[dict[str, Any]],
    waists: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    state: dict[str, Any],
    today: date,
    days: int,
    events: list[dict[str, Any]] | None = None,
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
        _block(
            "report_period",
            days=str(days),
            date=today.isoformat(),
            weekday=_RU_WEEKDAYS[today.weekday()],
        ),
        _block(
            "report_phase",
            phase=params["phase"],
            title=str(params["title"]),
            week=str(position["block_week"]),
            deload=_block("report_deload_yes" if position["deload_week"] else "report_deload_no"),
            calories=_format_range(params["calories"]),
            rate=str(params["rate_text"]),
            protein=_format_range(params["protein_g"]),
        ),
    ]

    if week_workouts:
        chunks.append(
            _block("report_workouts_header", count=str(len(week_workouts)))
            + "\n"
            + _serialize_history(week_workouts, len(week_workouts), catalog)
        )
    else:
        chunks.append(_block("report_no_workouts"))

    # События — сразу за списком тренировок: они объясняют пустые дни периода.
    # Пустой блок тоже нужен: «событий нет» значит, что пропуски ничем не
    # объяснены, и это другой разговор с атлетом.
    period_events, dropped_events = _clip_events(
        _events_in_period(events, today - timedelta(days=days - 1), today)
    )
    if period_events:
        lines = [_block("report_events_header")]
        lines += [_serialize_event(event) for event in period_events]
        if dropped_events:
            lines.append(
                _block(
                    "events_truncated",
                    count=str(len(period_events)),
                    total=str(len(period_events) + dropped_events),
                )
            )
        chunks.append("\n".join(lines))
    else:
        chunks.append(_block("report_no_events"))

    week_target = (
        params.get("ramp_start")
        if position["deload_week"]
        else coach_state.weekly_volume_target(state, position["cycle_week"])
    )
    maintenance_sets = params.get("sets_per_group") if params["phase"] == "maintenance" else None
    chunks.append(
        _block("report_volume_header")
        + "\n"
        + coach_features.render_weekly_volume(
            coach_features.weekly_volume(workouts, today),
            week_target,
            maintenance_sets,
            params.get("group_targets"),
        )
    )
    chunks.append(_render_attendance(workouts, today))

    summaries = coach_features.exercise_summaries(workouts, catalog, today)
    prs = [
        f"  {s['name']}: {s['top_weight']:g}×{s['top_reps']} ({s['top_date']})"
        for s in summaries
        if s["days_since_pr"] < days
    ]
    chunks.append(
        _block("report_prs_header") + "\n" + "\n".join(prs) if prs else _block("report_no_prs")
    )

    matrix = coach_features.nutrition_matrix(state, params, body_weights, waists, today)
    chunks.append(_render_stall(workouts, summaries, matrix, params, state, today))

    measurements = coach_features.render_measurements(body_weights, waists, today)
    nutrition = list(measurements)
    if matrix["lines"]:
        nutrition.append(_block("nutrition_matrix", lines="; ".join(matrix["lines"])))
    if matrix["goal"]:
        nutrition.append(_block("nutrition_goal", goal=matrix["goal"]))
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
            _block(
                "report_next_deload_now",
                ramp_start=_format_range(params.get("ramp_start")),
            )
        )
    elif every and position["cycle_week"] >= int(every):
        next_bits.append(_block("report_next_deload_soon"))
    else:
        next_week = coach_state.weekly_volume_target(state, position["cycle_week"] + 1)
        if next_week:
            next_bits.append(
                _block(
                    "report_next_target",
                    low=str(next_week[0]),
                    high=str(next_week[1]),
                )
            )
    if next_bits:
        chunks.append("\n".join(next_bits))

    chunks.append(_block("report_task"))
    return "\n\n".join(chunks)
