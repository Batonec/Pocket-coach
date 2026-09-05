#!/usr/bin/env python3
"""Сборка текста, который читает модель.

Всё, что уезжает в промпт, собирается здесь: системный промпт (профиль атлета,
семантика каталога, политика фаз, срез стратегии), user-промпт (контекст,
вычисленные фичи, сырая история вперемешку с событиями и заметками), JSON-схема
ответа и промпт недельного отчёта. Проза живёт в prompts/*.md и подставляется
через coach_prompts; этот модуль только считает слоты из данных атлета и
складывает блоки в нужном порядке. Если сюда просится фраза, а не вычисление,
ей место в markdown.

В конце файла — рендеры вычисленных фич из ``coach_features`` в строки для
модели: фичи считают, здесь их показывают. Зовут ``recommender`` (обе генерации)
и Coach MCP (``coach_preview_prompt``, ``coach_phase_summary``).
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from trainer.data import coach_prompts
from trainer.domain import coach_features, coach_state, plan_validator

# Сколько сырых тренировок видит модель; всё старше покрывают вычисленные
# сводки по упражнениям (промпт не должен расти от работы фич).
RAW_HISTORY_COUNT = 10

# Потолок хроники событий. Окна по датам у неё нет намеренно: событие любой
# давности всё ещё объясняет дырку в датах, а сводки его не заменяют — из
# событий не считается ни одного числа. Единственная страховка от промпта,
# растущего без границы, — потолок по строкам; об обрезке блок говорит вслух.
MAX_EVENT_LINES = 40


_EFFORT_MARK = {"easy": "-", "ok": "", "hard": "+"}

# Что за тренажёр стоит за каждым id каталога (по описаниям самого атлета):
# по коротким именам модель не поймёт, какая мышца работает. Id 1 — дубль 18
# в каталоге: строки истории переводятся на 18 при сериализации, и модель его
# не видит.
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

# Мышцы, которые атлет НЕ МОЖЕТ тренировать текущим каталогом: постоянный
# контекст, чтобы модель знала, что их ноль структурный, а не от лени.
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
    """Слот ``{{profile}}``: блоки профиля как «[Заголовок]» и текст, через пустую
    строку; без профиля — нейтральный фолбэк про взрослого здорового любителя.
    """
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
# Дисциплина «факт против плана» (остальные фичи считает coach_features)
# --------------------------------------------------------------------------- #
def _plan_adherence_report(workouts: list[dict[str, Any]]) -> str | None:
    """Сравнить последнюю тренировку со снапшотом совета, по которому её делали:
    сколько плановых подходов выполнено и какие отклонения (пропущено, меньше
    подходов, сверх плана). Одна строка для блока дисциплины или ``None``, если
    снапшота нет ни у одной недавней тренировки.
    """
    for workout in workouts:  # от новых к старым
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
# Хроника: тренировки и события строками
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
    """Одна тренировка одной строкой хроники: дата, метка нагрузки в скобках, потом
    упражнения через «;», у каждого подходы «вес×повторы» с меткой тяжести (-/+),
    ``@RIR`` и заметкой в «ёлочках» вплотную к своему подходу. Заметка ко всей
    сессии — после тире в хвосте. Старый дубль id 1 показывается под каноническим
    именем из каталога.
    """
    data = workout.get("data", {}) or {}
    # У сессии, записанной без карточки совета, нет метки нагрузки: так и
    # говорим, а не печатаем «?», который модель читает как пропуск данных.
    load_type = data.get("load_type") or "без плана"
    parts: list[str] = []
    for exercise in data.get("exercises", []) or []:
        name = str(exercise.get("name", "")).strip() or "?"
        canonical = coach_features.canonical_exercise_id(exercise.get("exercise_id"))
        # Старые строки могут нести дубль id 1: показываем их под каноническим
        # именем из каталога, чтобы модель видела одно движение, а не два.
        if names_by_id and canonical is not None and canonical in names_by_id:
            name = names_by_id[canonical]
        sets_repr: list[str] = []
        for workout_set in exercise.get("sets", []) or []:
            if not isinstance(workout_set, dict):
                continue
            raw_reps = workout_set.get("reps", 0)
            if isinstance(raw_reps, bool) or isinstance(workout_set.get("weight"), bool):
                continue
            if isinstance(raw_reps, float) and not raw_reps.is_integer():
                continue
            try:
                reps = int(raw_reps)
                weight = float(workout_set.get("weight", 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if reps < 1 or not math.isfinite(weight):
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
    # list_workouts() отдаёт от новых к старым: берём последние `limit` и
    # показываем от старых к новым, чтобы прогрессия читалась естественно.
    """Последние ``limit`` тренировок от старых к новым (стор отдаёт от новых),
    вперемешку с событиями по датам: разрыв в датах объясняется ровно там, где он
    виден, на общей дате событие стоит первым. Зовут ``_build_user_prompt``,
    ``_build_report_prompt`` и Coach MCP.
    """
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
    """Блок явки: тренировочные дни по календарным неделям и серии из ≥3 и ≥4 —
    факт, который стоит и за переключением сплита («каркас включается, когда атлет
    держит частоту»), и за гейтом программы. Общий для плана и недельного отчёта.
    """
    rows = coach_features.weekly_attendance(workouts, today)
    return _block(
        "attendance",
        weeks=render_weekly_attendance(rows, today),
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
    """Предусловия прогресса и застой по АКТИВНОМУ окну: оно начинается с якоря
    блока (старт фазы или возврат после перерыва ≥14 дней), так что отпуск не
    разбавляет частоту, а пороги объёма берутся из целей по группам самой фазы.
    """
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
    return render_stall_report(report)


def _days_since_last(workouts: list[dict[str, Any]], today: date) -> int | None:
    """Дней с последней тренировки или ``None``, если истории нет."""
    for workout in workouts:  # от новых к старым
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
    """«6–8» (с единицей, если дана) из пары чисел; скаляр — как есть."""
    if isinstance(bounds, (tuple, list)) and len(bounds) == 2:
        return f"{bounds[0]:g}–{bounds[1]:g}{unit}"
    return f"{bounds}{unit}"


def _format_number(value: Any) -> str:
    """Параметр фазы одним числом.

    Переопределения проверяются на записи, но нестрого: диапазонный ключ может
    прийти скаляром, а скалярный — диапазоном. Промпт из-за этого падать не
    должен: кривой параметр обязан доехать до атлета странным текстом, а не
    упавшей генерацией.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}"
    return str(value)


def _format_low(bounds: Any) -> str:
    """Нижняя граница диапазонного параметра; скалярное переопределение — как есть."""
    if isinstance(bounds, (tuple, list)) and bounds:
        return _format_number(bounds[0])
    return _format_number(bounds)


def _render_phase_policy(state: dict[str, Any] | None = None) -> str:
    """Политика фаз для системного промпта: проза в ``prompts/phase_policy.md``,
    здесь считаются только числа в её слотах.

    АКТИВНАЯ фаза рендерится из слитых параметров атлета (переопределения
    ``phase_params`` поверх дефолтов), иначе промпт нёс бы стоковые числа, а блок
    КОНТЕКСТ — настоящие, и модель получала бы две противоречащие методики в одном
    запросе. Две неактивные фазы остаются со стоковыми числами: они фон, и при
    переключении их всё равно задают заново.
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
    """Системный промпт из шаблона ``prompts/system.md``.

    Проза живёт в шаблоне; здесь только пять слотов: профиль, каталог с семантикой
    тренажёров (без дубля id 1), пробелы каталога, политика фаз, срез стратегии.
    Всё, что не вычисленное значение, должно уйти в markdown. Зовут
    ``recommender.generate_with_trace`` и Coach MCP (``coach_preview_prompt``).
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
    """User-промпт плана: блоки в порядке, в котором их читает модель.

    КОНТЕКСТ (дата, фаза с параметрами, неделя блока, дней с последней
    тренировки, идущее событие) → недельный объём по группам с целями → явка →
    замеры и матрица питания → сводки по упражнениям → предусловия и застой →
    на возврате доперерывные веса и ступени разгона → дисциплина (факт против
    плана) → сырая история последних тренировок вперемешку с событиями → задача.
    Без ``events=`` хроника и открытое событие молча выключаются, поэтому каждый
    живой вызыватель проверяется отдельным тестом. Зовут
    ``recommender.generate_with_trace`` и Coach MCP.
    """
    state = state if state is not None else coach_state.default_state()
    waists = waists or []
    params = coach_state.phase_params(state)
    phase = params["phase"]

    days = _days_since_last(workouts, today)
    returning = coach_state.is_return_from_break(workouts, today)
    position = coach_state.cycle_position(state, workouts, today)
    week = position["block_week"]
    if position["deload_week"]:
        # Плановая лёгкая неделя возвращает цель к старту ramp.
        week_target = params.get("ramp_start")
    else:
        week_target = coach_state.weekly_volume_target(state, position["cycle_week"])

    # --- явный блок контекста: первое, что читает модель ----------------------
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
        + render_weekly_volume(volume, week_target, maintenance_sets, params.get("group_targets"))
    )
    chunks.append(_render_attendance(workouts, today))

    measurement_lines = render_measurements(body_weights, waists, today)
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
        chunks.append(_block("summaries_header") + "\n" + render_exercise_summaries(summaries))

    chunks.append(_render_stall(workouts, summaries, matrix, params, state, today))

    if returning and days is not None:
        pre_break = render_pre_break_weights(
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
            + "\n".join(render_comeback_ramp(ramp_items))
        )

    discipline_lines: list[str] = []
    discipline = render_adherence_stats(coach_features.adherence_stats(workouts, today))
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
    # Дубль id (1 → 18) в enum не попадает: старая история переведена на
    # канонический id, и план может ссылаться только на канонические.
    """JSON-схема ответа модели (structured output): фокус, метка нагрузки, через
    сколько дней тренироваться, обоснование и упражнения с подходами. ``exercise_id``
    — enum из каталога без дубля id 1: план ссылается только на канонические id.
    Имён упражнений в схеме нет, их подставляет сервер. Зовут
    ``recommender.generate_with_trace`` и Coach MCP.
    """
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
# Недельный отчёт тренера
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
    """User-промпт недельного отчёта за ``days`` дней до ``today`` включительно.

    Период и фаза → тренировки периода хроникой → события периода (пустой блок
    тоже нужен: «событий нет» значит, что пропуски ничем не объяснены) → объём →
    явка → ПР периода → предусловия и застой → замеры и матрица питания →
    дисциплина → что дальше (разгрузка сейчас, скоро или цель следующей недели)
    → задача. Зовёт ``recommender.generate_weekly_report``.
    """
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
        + render_weekly_volume(
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

    measurements = render_measurements(body_weights, waists, today)
    nutrition = list(measurements)
    if matrix["lines"]:
        nutrition.append(_block("nutrition_matrix", lines="; ".join(matrix["lines"])))
    if matrix["goal"]:
        nutrition.append(_block("nutrition_goal", goal=matrix["goal"]))
    if nutrition:
        chunks.append("\n".join(nutrition))

    discipline = render_adherence_stats(coach_features.adherence_stats(workouts, today))
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


# --------------------------------------------------------------------------- #
# Рендер вычисленных фич в текст для модели
# --------------------------------------------------------------------------- #
def render_exercise_summaries(summaries: list[dict[str, Any]]) -> str:
    """Сводки по упражнениям строками: пик с e1RM и датой (для противовеса — лучший
    противовес), дней с ПР, «сейчас» с процентом от пика и последние сессии с
    позицией в тренировке.
    """
    lines: list[str] = []
    for summary in summaries:
        if summary["inverted"]:
            peak = (
                f"лучший противовес {summary['top_weight']:g}×{summary['top_reps']} "
                f"({summary['top_date']}, меньше = сильнее)"
            )
            label = "сейчас противовес"
        else:
            peak = (
                f"пик {summary['top_weight']:g}×{summary['top_reps']} "
                f"(e1RM {summary['e1rm']:g}, {summary['top_date']})"
            )
            label = "сейчас"
        if summary["current_weight"] is None:
            now = f"{label} — нет данных"
        else:
            now = f"{label} {summary['current_weight']:g}"
            if summary["pct_of_peak"] is not None:
                now += f" ({summary['pct_of_peak']}% пика)"
        recent = "; ".join(f"{when}: {sets}" for when, sets in summary["recent_sessions"])
        lines.append(
            f"  {summary['name']}: {peak}; ПР {summary['days_since_pr']} дн. назад; "
            f"{now}. Последние: {recent}"
        )
    return "\n".join(lines)


def render_weekly_volume(
    volume: dict[str, dict[str, float]],
    week_target: tuple[int, int] | None,
    maintenance_sets: tuple[int, int] | None = None,
    group_targets: dict[str, Any] | None = None,
) -> str:
    """Недельный объём по группам: прямые сеты и эффективные, рядом цель.

    Цель дана в ПРЯМЫХ сетах, как считает таблица программы, поэтому стоит рядом
    с прямым числом, а эффективные подписаны как справочные. Без целей по группам
    печатается коридор недели блока для крупных групп и ориентиры малых, в режиме
    поддержания — фиксированные сеты на группу.
    """
    targets = (
        coach_features.group_volume_targets(week_target, maintenance_sets, group_targets)
        if group_targets
        else {}
    )
    lines = []
    for group, counts in volume.items():
        effective = f"{counts['effective']:g}"
        goal = targets.get(group)
        if goal:
            # Цель задана в ПРЯМЫХ сетах — так считает таблица программы, —
            # поэтому стоит рядом с прямым числом, а эффективные подписаны как
            # справочные. Одно число не в той колонке, и модель выберет меньшую
            # из двух целей.
            line = (
                f"  {group}: {counts['direct']} прямых (цель {goal[0]:g}–{goal[1]:g}) / "
                f"{effective} эффективных (справочно)"
            )
        else:
            line = f"  {group}: {counts['direct']} прямых / {effective} эффективных"
        lines.append(line)
    if group_targets:
        lines.append(
            "  Цели — в ПРЯМЫХ сетах и на объём ЗРЕЛОГО блока по программе; на неделях "
            "разгона идём к ним снизу, ориентир недели — в разделе ПРОГРАММА. Эффективные "
            "сеты справочные: показывают, сколько косвенной работы группа уже получила, "
            "но цель не закрывают."
        )
    elif week_target:
        small = ", ".join(
            f"{group} {low}–{high}"
            for group, (low, high) in coach_features.SMALL_GROUP_TARGETS.items()
        )
        lines.append(
            f"  Цель этой недели блока для крупных групп: {week_target[0]}–{week_target[1]} "
            f"эффективных сетов; ориентиры малых групп (прямых): {small}."
        )
    elif maintenance_sets:
        lines.append(
            f"  Режим поддержания: {maintenance_sets[0]}–{maintenance_sets[1]} сета на группу "
            "в неделю, объём НЕ растёт."
        )
    return "\n".join(lines)


def render_stall_report(report: dict[str, Any]) -> str:
    """Две строки: факты активного окна (всегда — «фактическая частота приходит в
    данных» это обещание из шапки программы), затем вердикт по предусловиям и
    застою.
    """
    volume = ", ".join(
        f"{group} {value:.1f} (порог {threshold:g})"
        for group, (value, threshold) in report["volume_per_week"].items()
    )
    facts = (
        f"Активное окно {report['window_days']} дн. (с {report['window_start'].isoformat()}; "
        "перерыв ≥14 дней и прошлая фаза в него не входят): "
        f"частота {report['frequency']:.1f}/нед; прямых сетов/нед: {volume}."
    )
    if report["too_short"]:
        verdict = (
            f"Окно короче {coach_features.STALL_MIN_WINDOW_DAYS} дней — предусловия прогресса и застой "
            "пока не оцениваются."
        )
    elif not report["preconditions_ok"]:
        verdict = (
            "Предусловия прогресса НЕ выполнены ("
            + "; ".join(report["reasons"])
            + ") — плато, если оно есть, объясняй посещаемостью/питанием, а не потолком."
        )
    elif report["stalled"]:
        names = ", ".join(
            f"{s['name']} (без прироста {s['quiet_days']} дн.)" for s in report["stalled"]
        )
        verdict = (
            f"ЗАСТОЙ при выполненных предусловиях (частота/объём/питание в норме): {names}. "
            "Предложи deload −10% с разгоном или вариацию по этим движениям."
        )
    elif report["window_days"] < coach_features.STALL_NO_PR_DAYS:
        verdict = (
            f"Предусловия прогресса выполнены; окну меньше {coach_features.STALL_NO_PR_DAYS} дней — "
            "застой ещё не оценивается."
        )
    else:
        verdict = "Предусловия прогресса выполнены, застоя по упражнениям нет."
    return f"{facts}\n{verdict}"


def render_weekly_attendance(rows: list[dict[str, Any]], today: date) -> str:
    """Явка по неделям одной строкой: «начало…конец: сессий», текущая помечена."""
    parts = []
    for row in rows:
        label = f"{row['start'].isoformat()}…{row['end'].isoformat()}"
        if not row["closed"]:
            label += f" (текущая, по {today.isoformat()})"
        parts.append(f"{label}: {row['sessions']}")
    return ", ".join(parts)


def render_pre_break_weights(items: list[dict[str, Any]], break_days: int) -> str | None:
    """Рабочие веса последней сессии ПЕРЕД перерывом с пояснением, что это форма до
    паузы, а насколько снизить вход, модель решает сама. ``None``, если нечего
    показывать.
    """
    if not items:
        return None
    lines = [
        f"  {item['name']}: {item['last_working']:g}" + (" противовеса" if item["inverted"] else "")
        for item in items
    ]
    return (
        f"Рабочие веса в последней сессии ПЕРЕД перерывом ({break_days} дн. "
        "назад). Это форма до паузы, а не сегодняшняя: отметки усилия и RIR в "
        "истории тоже относятся к тем сессиям. Насколько снизить вход и как "
        "быстро возвращаться к этим весам — решай сам по принципам возврата "
        "после перерыва.\n" + "\n".join(lines)
    )


def render_comeback_ramp(items: list[dict[str, Any]]) -> list[str]:
    """Ступени разгона к доперерывному рабочему весу, по строке на упражнение."""
    lines: list[str] = []
    for item in items:
        arrow = " → ".join(f"{step:g}" for step in item["steps"])
        unit = " противовеса" if item["inverted"] else ""
        lines.append(
            f"  {item['name']}: доперерывный рабочий {item['target']:g}{unit}, "
            f"сейчас {item['current']:g}. Ступени: {arrow}"
        )
    return lines


def render_measurements(
    body_weights: list[dict[str, Any]],
    waists: list[dict[str, Any]],
    today: date,
) -> list[str]:
    """Последние взвешивания и замеры талии для промпта: хвост точек, дней с
    последнего замера, число замеров за 7 дней и хватает ли их для недельной
    средней; отброшенные неправдоподобные записи названы числом.
    """
    lines: list[str] = []
    weights = coach_features.weight_points(body_weights)
    if weights:
        tail = ", ".join(f"{when.isoformat()}: {value:g}кг" for when, value in weights[-6:])
        age = (today - weights[-1][0]).days
        dropped = len(body_weights) - len(weights)
        count = coach_features.weigh_ins_in_window(weights, today)
        line = f"Вес тела: {tail}. Дней с последнего замера: {age}. Замеров за последние 7 дней: {count}"
        line += (
            f" (для недельной средней нужно ≥{coach_features.WEEKLY_MEAN_MIN_POINTS})."
            if count < coach_features.WEEKLY_MEAN_MIN_POINTS
            else "."
        )
        if dropped:
            line += f" (отброшено неправдоподобных записей: {dropped})"
        lines.append(line)
    waist = coach_features.waist_points(waists)
    if waist:
        tail = ", ".join(f"{when.isoformat()}: {value:g}см" for when, value in waist[-6:])
        age = (today - waist[-1][0]).days
        lines.append(f"Талия: {tail}. Дней с последнего замера: {age}.")
    return lines


def render_phase_summary(summary: dict[str, Any]) -> str:
    """Итоги фазы текстом: длительность, тренировки, вес и талия от начала к концу,
    ПР за фазу, дисциплина. Зовёт Coach MCP (``coach_phase_summary``).
    """
    lines = [
        f"Фаза {summary['phase']}: {summary['started']} → {summary['ended']} "
        f"({summary['weeks']} нед)."
    ]
    per_week = f" ({summary['per_week']}/нед)" if summary["per_week"] is not None else ""
    lines.append(f"Тренировок: {summary['workouts']}{per_week}.")
    if summary["weight_start"] is not None and summary["weight_end"] is not None:
        rate = (
            f", темп {summary['weight_rate_per_week']:+.2f} кг/нед"
            if summary["weight_rate_per_week"] is not None
            else ""
        )
        lines.append(
            f"Вес: {summary['weight_start']:.1f} → {summary['weight_end']:.1f} кг "
            f"({summary['weight_end'] - summary['weight_start']:+.1f}{rate})."
        )
    else:
        lines.append("Вес: замеров в периоде фазы нет.")
    if summary["waist_start"] is not None and summary["waist_end"] is not None:
        lines.append(
            f"Талия: {summary['waist_start']:g} → {summary['waist_end']:g} см "
            f"({summary['waist_end'] - summary['waist_start']:+.1f})."
        )
    if summary["prs"]:
        top = "; ".join(
            f"{item['name']} ×{item['count']} (лучшее "
            + (
                f"противовес {item['top_weight']:g}×{item['top_reps']}"
                if item["inverted"]
                else f"{item['top_weight']:g}×{item['top_reps']}"
            )
            + ")"
            for item in summary["prs"][:5]
        )
        lines.append(f"ПР за фазу: {summary['pr_events']} — {top}.")
    else:
        lines.append("ПР за фазу: нет.")
    adherence_line = render_adherence_stats(summary["adherence"])
    if adherence_line:
        lines.append(adherence_line)
    return "\n".join(lines)


def render_adherence_stats(stats: dict[str, Any] | None) -> str | None:
    """Строка дисциплины: сессий по плану, выполненных плановых подходов и процент;
    полностью пропущенные упражнения перечислены. ``None``, если плановой работы
    не было.
    """
    if not stats:
        return None
    line = (
        f"Дисциплина за {stats['days']} дней: {stats['sessions']} трен. по плану, "
        f"{stats['done_sets']} из {stats['planned_sets']} плановых подходов "
        f"({stats['pct']}%)."
    )
    if stats["skipped"]:
        skipped = ", ".join(f"{name} ×{count}" for name, count in stats["skipped"])
        line += f" Полностью пропускались: {skipped}."
    return line


def comeback_ramp(
    workouts: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    today: date,
) -> list[str]:
    """Ступени разгона сразу строками — для вызывающих, которым нужен только текст."""
    return render_comeback_ramp(coach_features.comeback_ramp_steps(workouts, catalog, today))
