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
from datetime import date, datetime, timedelta, timezone
from typing import Any

from trainer.data import coach_prompts
from trainer.domain import coach_features, coach_state, limits, plan_validator

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
    10: "рычажная горизонтальная тяга (хаммер) — толщина спины (середина трапеции, ромбовидные), "
    "вторично бицепс. Нагружается блинами: практичный шаг 10 кг, 2.5 накинуть неудобно",
    13: "махи в тренажёре с упором в локти, сидя — средняя дельта",
    19: "тренажёр обратных махов (сидя, разведение рук назад) — задняя дельта. Шаг стека 2.5 кг",
    11: "тренажёр на сгибание рук — бицепс",
    12: "трицепс на блоке вниз (ручка варьируется: прямая/канат) — трицепс",
    8: "жим ногами в платформе 45° — квадрицепс + ягодичные. Нагружается блинами: практичный шаг "
    "10 кг, часто 20 — какие блины есть на стойке",
    16: "разгибания ног сидя — квадрицепс (изоляция)",
    15: "сгибания ног лёжа — бицепс бедра",
}

# Мышцы, которые атлет НЕ МОЖЕТ тренировать текущим каталогом: постоянный
# контекст, чтобы модель знала, что их ноль структурный, а не от лени.
CATALOG_GAPS = "икры, пресс, разгибатели спины — упражнений в каталоге нет"

# Разделы рабочего документа стратегии, которые уходят в системный промпт
# ПЛАНА. Резать по ЗАГОЛОВКУ, а не по номеру: атлет перенумеровывает разделы,
# правя документ, и срез по «## 4.» начал бы молча отдавать не ту главу.
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

# Срез для НЕДЕЛЬНОГО ОТЧЁТА — свой: блок «Вес и талия» пишется по главам
# «Питание» (контур коррекции, калибровка TDEE, недели поддержки) и
# «Измерения» (протокол, норматив талии, стоп набора), которых план не читает;
# «Курс к цели» — ещё и по «Расхождениям с vision»: без честного прогноза курс
# мерился бы по недостижимому. «Пробел каталога» отчёту не нужен — он про
# состав сессии, а сессию отчёт не собирает. Оба списка сверяются с заголовками
# документа руками (см. CLAUDE.md); ненайденный заголовок попадает в промпт
# предупреждением.
REPORT_STRATEGY_SECTIONS = [
    "Скелет: семь фаз",
    "Ф0 — возврат (недели 1–4)",
    "Ф1 — рекомпозиция (недели 5–17, около 12–14)",
    "Тренировочные дни",
    "Прогрессия",
    "Питание",
    "Измерения",
    "Если выпала неделя",
    "Два расхождения с vision — честно",
]


def _render_program(strategy: str | None, sections: list[str] = STRATEGY_SECTIONS) -> str:
    """Слот {{program}}: текст под заголовком «=== ПРОГРАММА ===», который держат
    сами шаблоны, — подпись о приоритетах и срез стратегии по ``sections``.

    Без файла стратегии — строка-предупреждение, а не пустота: заголовок держит
    шаблон, и секция без содержания читалась бы моделью как пропуск данных.
    """
    if not strategy:
        return _block("program_absent")
    body, missing = coach_prompts.document_sections(strategy, sections)
    parts = [_block("program_header")]
    if missing:
        parts.append(_block("program_missing", sections=", ".join(missing)))
    if body:
        parts.append(body)
    return "\n\n".join(parts)


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


def _support_week_label(state: dict[str, Any], today: date) -> str:
    """Флаг недели поддержки для подписи недели блока (план и отчёт) либо пустая
    строка: калории на уровне TDEE, тренировки в обычном режиме, это не разгрузка.
    """
    bounds = coach_state.support_week_bounds(state, today)
    if bounds is None:
        return ""
    return _block("support_week_label", start=bounds[0].isoformat(), end=bounds[1].isoformat())


def _render_attendance(workouts: list[dict[str, Any]], today: date) -> str:
    """Блок явки: тренировочные дни по календарным неделям и серия закрытых недель
    с ≥3 (гейт программы), плюс темп — тренировок за 14 дней и интервалы между
    последними сессиями: по темпу program_header держит ротацию каркаса, а не по
    «неделям по четыре», которых у атлета через день не бывает. Общий для плана и
    недельного отчёта.
    """
    rows = coach_features.weekly_attendance(workouts, today)
    intervals = coach_features.recent_intervals(workouts, today)
    return _block(
        "attendance",
        weeks=render_weekly_attendance(rows, today),
        streak_three=str(coach_features.attendance_streak(rows, 3)),
        fortnight=str(coach_features.sessions_in_window(workouts, today, 14)),
        intervals=", ".join(str(days) for days in intervals) if intervals else "мало данных",
    )


def _render_volume(
    workouts: list[dict[str, Any]],
    today: date,
    week_target: tuple[int, int] | None,
    maintenance_sets: tuple[int, int] | None,
    params: dict[str, Any],
) -> list[str]:
    """Два блока объёма: за 7 дней — темп календарной недели, и за КРУГ из
    последних четырёх тренировок — с целями по группам: цели программы описывают
    один проход каркаса, и атлет через день по календарю вечно «недобирал» бы. В
    режиме поддержания круга нет (одна сессия в неделю), цели остаются у недельного
    блока. Общий для плана и отчёта.
    """
    weekly = coach_features.weekly_volume(workouts, today)
    if maintenance_sets:
        return [
            _block("volume_header") + "\n" + render_weekly_volume(weekly, None, maintenance_sets)
        ]
    chunks = [_block("volume_header") + "\n" + render_weekly_volume(weekly, None)]
    volume, days = coach_features.round_volume(workouts, today)
    if days:
        chunks.append(
            _block(
                "round_volume_header",
                count=str(len(days)),
                start=days[0].isoformat(),
                end=days[-1].isoformat(),
            )
            + "\n"
            + render_weekly_volume(volume, week_target, None, params.get("group_targets"))
        )
    return chunks


def previous_advice(rationale: str | None) -> str | None:
    """Пункт «Совет» из rationale прошлой карточки — то, что она обещала следующей
    сессии («следующая — ноги и спина, жим ногами первым»). Без такого пункта —
    ``None``. Зовёт ``_render_previous_card``.
    """
    for line in (rationale or "").splitlines():
        bare = line.strip().lstrip("*-• ").strip()
        if not bare.lower().startswith("совет"):
            continue
        text = bare.split(":", 1)[1] if ":" in bare else bare[len("совет") :]
        text = " ".join(text.strip(" *").split())
        return text or None
    return None


def _render_previous_card(
    previous: dict[str, Any] | None, workouts: list[dict[str, Any]]
) -> str | None:
    """Блок памяти карточки о себе: фокус, нагрузка, состав и обещание прошлого
    совета, плюс была ли по нему тренировка (по ``based_on_workout_id`` против id в
    истории). Без прошлой карточки или без payload у неё — ``None``: канал
    включает вызывающий, передав строку кэша ``previous=``.
    """
    if not isinstance(previous, dict):
        return None
    payload = previous.get("recommendation")
    if not isinstance(payload, dict) or not payload.get("exercises"):
        return None
    stamp = previous.get("updated_at") or previous.get("created_at")
    built = (
        datetime.fromtimestamp(int(stamp), tz=timezone.utc).date().isoformat()
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
        else "?"
    )
    based_on = previous.get("based_on_workout_id")
    newer = (
        sum(
            1
            for workout in workouts
            if isinstance(workout.get("id"), int) and workout["id"] > based_on
        )
        if isinstance(based_on, int)
        else 0
    )
    status = (
        f"после неё записано тренировок: {newer} — она уже отработана"
        if newer
        else "тренировок по ней ещё не было — ты пересобираешь ту же сессию"
    )
    exercises = ", ".join(
        f"{exercise.get('name') or exercise.get('exercise_id')} ×{len(exercise.get('sets') or [])}"
        for exercise in payload["exercises"]
        if isinstance(exercise, dict)
    )
    advice = previous_advice(payload.get("rationale"))
    return _block(
        "previous_card",
        built=built,
        planned=str(payload.get("next_workout_date") or "?"),
        status=status,
        focus=" ".join(str(payload.get("focus") or "").split()) or "—",
        load=str(payload.get("load_type") or "?"),
        exercises=exercises or "—",
        advice=f" Обещание на следующую сессию из её rationale: «{advice}»." if advice else "",
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
# Текст исправляющего сообщения и первый пункт списка границ — из того же файла,
# из которого plan_validator читает сами правила.
_RULES = coach_prompts.fragments("plan_rules")


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


def _render_hard_rules() -> str:
    """Блок «ЖЁСТКИЕ ГРАНИЦЫ» системного промпта: нумерованный список формулировок
    из ``plan_rules.md``. Первым — ``catalog_only`` (свойство JSON-схемы, а не
    правило валидатора), дальше формулировки правил ``plan_validator.RULES`` в
    порядке файла: модель читает те же блоки, которые сервер исполняет, и добавить
    проверку, не сказав о ней модели, нельзя.
    """
    sentences = [_RULES["catalog_only"], *(rule.sentence for rule in plan_validator.RULES)]
    last = len(sentences)
    return "\n".join(
        f"{index}) {sentence}{'.' if index == last else ';'}"
        for index, sentence in enumerate(sentences, start=1)
    )


def _build_reprompt(violations: list[str]) -> str:
    """Исправляющее сообщение после нарушений жёстких границ: строки валидатора
    списком внутри текста ``reprompt`` из ``plan_rules.md``. Зовёт
    ``recommender.generate_with_trace`` один раз, в том же разговоре.
    """
    return coach_prompts.render(_RULES["reprompt"], нарушения="- " + "\n- ".join(violations))


def _build_system_prompt(
    catalog: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    strategy: str | None = None,
) -> str:
    """Системный промпт из шаблона ``prompts/system.md``.

    Проза живёт в шаблоне; здесь только шесть слотов: профиль, каталог с семантикой
    тренажёров (без дубля id 1), пробелы каталога, политика фаз, жёсткие границы
    (из ``plan_rules.md`` в порядке ``plan_validator.RULES``), срез стратегии.
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
        hard_rules=_render_hard_rules(),
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
    previous: dict[str, Any] | None = None,
) -> str:
    """User-промпт плана: блоки в порядке, в котором их читает модель.

    КОНТЕКСТ (дата, фаза с параметрами, неделя блока, дней с последней
    тренировки, идущее событие) → объём за 7 дней (темп) и за круг из четырёх
    тренировок с целями по группам → явка с темпом за 14 дней → замеры и матрица
    питания → сводки по упражнениям → предусловия и застой → на возврате
    доперерывные веса и ступени разгона → дисциплина (факт против плана) →
    прошлая карточка тренера (``previous`` — строка кэша совета) → сырая история
    последних тренировок вперемешку с событиями → задача. Без ``events=`` хроника
    и открытое событие молча выключаются, без ``previous=`` — память карточки,
    поэтому каждый живой вызыватель проверяется отдельным тестом. Зовут
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
    week_label += _support_week_label(state, today)
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

    maintenance_sets = params.get("sets_per_group") if phase == "maintenance" else None
    chunks.extend(_render_volume(workouts, today, week_target, maintenance_sets, params))
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

    # Память карточки о себе: обещание прошлого совета следующая карточка обязана
    # прочитать, иначе «связная система» распадается на сессии в вакууме.
    card = _render_previous_card(previous, workouts)
    if card:
        chunks.append(card)

    raw_count = min(history_limit, RAW_HISTORY_COUNT)
    shown_events, dropped_events = _clip_events(events)
    header = [_block("raw_history_header", count=str(raw_count), legend=_block("history_legend"))]
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
            "load_type": {"type": "string", "enum": list(limits.PLANNED_LOAD_TYPES)},
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
_REPORT_TEMPLATE = coach_prompts.load("weekly_report")


def _build_report_system_prompt(
    profile: dict[str, Any] | None = None,
    strategy: str | None = None,
    state: dict[str, Any] | None = None,
) -> str:
    """Системный промпт недельного отчёта: профиль, политика фаз, срез программы.

    До этого он был константой: отчёт не получал ни профиля, ни программы, то
    есть писался про абстрактного атлета. Гейту этапа при этом негде было
    прозвучать — а гейт жёсткий, и должно существовать место, где его статус
    называют вслух. Политика фаз — та же, что у плана, и рендерится из
    параметров атлета: без неё блок «без ПР — и почему это ок» писался бы, не
    зная, что на срезе плато по весам не проблема, а на удержании объём не растёт.
    """
    return coach_prompts.render(
        _REPORT_TEMPLATE,
        profile=_render_profile(profile),
        phase_policy=_render_phase_policy(state),
        program=_render_program(strategy, REPORT_STRATEGY_SECTIONS),
    )


# Заголовки блоков отчёта в том виде, в каком их пишет модель: по ним из
# прошлого отчёта вырезается «Фокус следующей недели». Формат задаёт
# weekly_report.md; новый блок там — новое имя здесь, иначе фокус прошлого
# отчёта захватит его целиком.
_REPORT_BLOCKS = (
    "итоги недели",
    "прогресс",
    "вес и талия",
    "дисциплина",
    "курс к цели",
    "гейт этапа",
    "фокус следующей недели",
)
_FOCUS_BLOCK = "фокус следующей недели"
# Фокус — 2–3 пункта; длиннее — модель ушла в эссе, и в промпт едет только начало.
MAX_PREVIOUS_FOCUS_CHARS = 1200


def _report_block_name(line: str) -> str | None:
    """Имя блока отчёта, если строка — его заголовок («**Прогресс** — …», «### Прогресс»)."""
    bare = line.strip().lstrip("#*").strip().lower()
    return next((name for name in _REPORT_BLOCKS if bare.startswith(name)), None)


def previous_focus(report: str | None) -> str | None:
    """Блок «Фокус следующей недели» из текста прошлого отчёта — от его заголовка
    до следующего заголовка блока — или ``None``, если отчёта нет или блока в нём
    не нашлось. Живой тренер на созвоне начинает с «договаривались о X — как
    вышло?», и это единственная память отчёта о самом себе. Зовут
    ``_build_report_prompt`` и тесты.
    """
    if not report:
        return None
    lines = report.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if _report_block_name(line) == _FOCUS_BLOCK), None
    )
    if start is None:
        return None
    picked = [lines[start]]
    for line in lines[start + 1 :]:
        if _report_block_name(line):
            break
        picked.append(line)
    text = "\n".join(picked).strip()
    if len(text) > MAX_PREVIOUS_FOCUS_CHARS:
        text = text[:MAX_PREVIOUS_FOCUS_CHARS].rstrip() + "…"
    return text or None


def _build_report_prompt(
    workouts: list[dict[str, Any]],
    body_weights: list[dict[str, Any]],
    waists: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    state: dict[str, Any],
    today: date,
    days: int,
    events: list[dict[str, Any]] | None = None,
    measurements: list[dict[str, Any]] | None = None,
    previous_report: str | None = None,
) -> str:
    """User-промпт недельного отчёта за ``days`` дней до ``today`` включительно.

    Период и фаза → траектория фазы с её старта (вес, талия, темп, ПР, цель
    по весу) → фокус из прошлого отчёта, если он передан → тренировки периода
    хроникой (с той же легендой, что у плана) →
    события периода (пустой блок тоже нужен: «событий нет» значит, что пропуски
    ничем не объяснены) → объём за 7 дней с итогом за период и за неделю до него
    и объём за круг из четырёх тренировок с целями → явка с темпом →
    ПР периода → сводки по тренажёрам → предусловия и застой → замеры (с
    7-дневной средней), обхваты, матрица питания и оценка TDEE → дисциплина → что дальше
    (разгрузка сейчас, скоро или цель следующей недели) → задача. Зовёт
    ``recommender.generate_weekly_report``.
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
            flags=(_block("report_deload_label") if position["deload_week"] else "")
            + _support_week_label(state, today),
            calories=_format_range(params["calories"]),
            rate=str(params["rate_text"]),
            protein=_format_range(params["protein_g"]),
        ),
    ]

    # Траектория фазы: отчёт живёт в семи днях, а гейт и цели — в месяцах.
    # Без старта фазы, веса и талии на старте и темпа за фазу модели нечем
    # сказать, где атлет на пути к критерию.
    started = coach_state.phase_start(state)
    if started is not None and started <= today:
        summary = coach_features.phase_summary(
            workouts,
            body_weights,
            waists,
            catalog,
            phase=params["phase"],
            started=started,
            ended=today,
        )
        progress = [_block("report_phase_progress_header"), render_phase_summary(summary)]
        if params.get("target_weight_kg"):
            progress.append(
                _block("report_phase_target", target=_format_number(params["target_weight_kg"]))
            )
        if params.get("ceiling_weight_kg"):
            progress.append(
                _block("report_phase_ceiling", ceiling=_format_number(params["ceiling_weight_kg"]))
            )
        chunks.append("\n".join(progress))

    # Память о прошлом отчёте: его «Фокус следующей недели» — то, с чего живой
    # тренер начинает созвон. Без ``previous_report`` блока просто нет.
    focus = previous_focus(previous_report)
    if focus:
        chunks.append(_block("report_previous_focus") + "\n" + focus)

    if week_workouts:
        chunks.append(
            _block(
                "report_workouts_header",
                count=str(len(week_workouts)),
                legend=_block("history_legend"),
            )
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
    volume_chunks = _render_volume(workouts, today, week_target, maintenance_sets, params)
    volume_chunks[0] = volume_chunks[0].replace(
        _block("volume_header"),
        _block(
            "report_volume_header",
            total=str(coach_features.sets_in_window(workouts, today)),
            previous=str(coach_features.sets_in_window(workouts, today - timedelta(days=7))),
        ),
        1,
    )
    chunks.extend(volume_chunks)
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
    # Сводки по тренажёрам — те же, что читает план: без них отчёт видел одну
    # неделю сырых подходов без прошлой, и «движение весов» с силовым гейтом
    # ему было нечем подтвердить.
    if summaries:
        chunks.append(_block("summaries_header") + "\n" + render_exercise_summaries(summaries))

    matrix = coach_features.nutrition_matrix(state, params, body_weights, waists, today)
    chunks.append(_render_stall(workouts, summaries, matrix, params, state, today))

    nutrition = render_measurements(body_weights, waists, today)
    # Обхваты — метрики цели из vision (рука, плечи, грудь): без них отчёт про
    # главные цели сказать не мог ничего. Пустой блок тоже говорит вслух.
    overview = coach_features.measurement_overview(measurements or [], today)
    if overview:
        nutrition.append(
            _block("report_measurements_header") + "\n" + render_measurement_overview(overview)
        )
    else:
        nutrition.append(_block("report_measurements_none"))
    if matrix["lines"]:
        nutrition.append(_block("nutrition_matrix", lines="; ".join(matrix["lines"])))
    if matrix["goal"]:
        nutrition.append(_block("nutrition_goal", goal=matrix["goal"]))
    # Недели поддержки в калибровку не входят, как и в матрицу: их точки
    # объявили бы расход ниже на пустом месте.
    calibration_points = [
        point
        for point in coach_features.weight_points(body_weights)
        if not coach_state.is_support_week(state, point[0])
    ]
    estimate = coach_features.tdee_estimate(params, calibration_points, today, phase_start=started)
    if estimate:
        nutrition.append(
            _block(
                "report_tdee",
                intake=f"{estimate['intake']:g}",
                trend=f"{estimate['trend_per_week']:+.2f}",
                weeks=str(estimate["window_days"] // 7),
                tdee=str(estimate["tdee"]),
            )
        )
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
            "  Цели — в ПРЯМЫХ сетах на круг из четырёх тренировок (один проход каркаса; по "
            "программе это неделя четырёхдневки) и на объём ЗРЕЛОГО блока; на неделях разгона "
            "идём к ним снизу, ориентир — в разделе ПРОГРАММА. Эффективные сеты справочные: "
            "показывают, сколько косвенной работы группа уже получила, но цель не закрывают."
        )
    elif week_target:
        small = ", ".join(
            f"{group} {low}–{high}"
            for group, (low, high) in coach_features.SMALL_GROUP_TARGETS.items()
        )
        lines.append(
            f"  Цель круга (недели блока) для крупных групп: {week_target[0]}–{week_target[1]} "
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
        for group, (value, threshold) in report["volume_per_round"].items()
    )
    facts = (
        f"Активное окно {report['window_days']} дн. (с {report['window_start'].isoformat()}; "
        "перерыв ≥14 дней и прошлая фаза в него не входят): "
        f"частота {report['frequency']:.1f}/нед; прямых сетов за круг из "
        f"{coach_features.ROUND_SESSIONS} тренировок: {volume}."
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
        # Стратегия управляет 7-дневной средней, а не точками: она названа числом,
        # когда точек хватает, и рядом средняя недели раньше — это и есть «вес
        # стоит / движется» без пересчёта моделью.
        mean_now = coach_features.moving_average(weights, today)
        if count >= coach_features.WEEKLY_MEAN_MIN_POINTS and mean_now is not None:
            line += f" Средняя за 7 дней: {mean_now:.1f} кг"
            week_ago = today - timedelta(days=7)
            mean_before = coach_features.moving_average(weights, week_ago)
            enough_before = (
                coach_features.weigh_ins_in_window(weights, week_ago)
                >= coach_features.WEEKLY_MEAN_MIN_POINTS
            )
            if enough_before and mean_before is not None:
                line += f" (неделей раньше {mean_before:.1f}, {mean_now - mean_before:+.1f})"
            line += "."
        lines.append(line)
    waist = coach_features.waist_points(waists)
    if waist:
        tail = ", ".join(f"{when.isoformat()}: {value:g}см" for when, value in waist[-6:])
        age = (today - waist[-1][0]).days
        lines.append(f"Талия: {tail}. Дней с последнего замера: {age}.")
    return lines


def render_measurement_overview(rows: list[dict[str, Any]]) -> str:
    """Обхваты строками: «подпись: последний (дата, N дн. назад; раньше X от даты)»."""
    lines: list[str] = []
    for row in rows:
        line = (
            f"  {row['label']}: {row['last_value']:g} см ({row['last_date']}, "
            f"{row['days_since']} дн. назад"
        )
        if row["previous_value"] is not None:
            delta = row["last_value"] - row["previous_value"]
            line += f"; раньше {row['previous_value']:g} от {row['previous_date']}, {delta:+.1f}"
        lines.append(line + ")")
    return "\n".join(lines)


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
