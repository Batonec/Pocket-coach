#!/usr/bin/env python3
"""Сигналы коуча для баннера «История» (см. docs/COACH_SIGNALS.md).

Сервер считает полный список — уже схлопнутый, отфильтрованный по отсрочкам и
отсортированный — на лету из SQLite, coach_state и статуса кэшированного совета.
Модель здесь не вызывается никогда. Клиент рисует первые один-два пункта и сам
не выдумывает текстов и не сравнивает дат.

Семейства: «замеры» (свежесть веса и талии схлопнута в один баннер, подсказка
про мёртвый тренд в строительной фазе, жёсткий лимит талии), «тренировки»
(эскалация return_soon → return_mode), плановая разгрузка, готовый недельный
отчёт и позитивный week_done. Каждый сигнал гаснет сам (смена состояния,
эскалация, TTL); ключи дисмисса — ключи эпизода, поэтому «скрыть навсегда» не
существует. Зовёт server.py: ``GET /api/coach/signals`` и дисмисс.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

from trainer.data import coach_prompts
from trainer.domain import coach_features, coach_state

# Пороги семейства «замеры». «Пора» начинается с 10-го дня, а не с 7-го: при
# недельном ритме старт на 7-м дне зажигал бы баннер каждую неделю даже
# дисциплинированному атлету — гарантированная слепота к баннерам.
MEASUREMENT_DUE_DAYS = 10
MEASUREMENT_OVERDUE_DAYS = coach_features.STALE_MEASUREMENT_DAYS  # 14: порог матрицы питания

# Строительные фазы ведут калории по тренду веса внутри фазы, и мёртвый тренд
# стоит денег УЖЕ СЕГОДНЯ, даже пока 14-дневная свежесть ещё зелёная. Подсказка
# с 5-го дня: более ранний замер тренд всё равно не оживит (окну нужен разброс
# точек ≥5 дней), так что 5-й день — первый, когда новая точка реально помогает.
# С 10-го дня перехватывает лестница «пора / просрочено».
TREND_NUDGE_FROM_DAYS = 5

RETURN_SOON_FROM_DAYS = 11  # за 1–3 дня до порога протокола возврата
RETURN_BREAK_DAYS = coach_state.BREAK_DAYS  # 14

REPORT_FRESH_HOURS = 48

WEEK_DONE_MIN_PCT = 90
# Нижняя граница: неделя с одной тренировкой закрытой не считается ни при какой
# фазе. Реальный порог берётся из sessions_per_week текущей фазы — поздравлять
# с закрытой неделей на половине плана значит обесценить сам баннер.
WEEK_DONE_MIN_SESSIONS = 2
WEEK_DONE_SHOW_DAYS = 2  # понедельник и вторник после закрытой недели

SEVERITY_RANK = {"critical": 0, "warn": 1, "accent": 2, "info": 3, "positive": 4}
FAMILY_RANK = {"measurements": 0, "trainings": 1, "deload": 2, "report": 3, "milestone": 4}

# Отсрочка по умолчанию для severity, когда дисмисс не назвал часов.
# None = эпизодическая: скрыт, пока не сменится instance_key (состояние ушло).
SNOOZE_DEFAULT_HOURS: dict[str, int | None] = {
    "info": 72,
    "warn": 48,
    "accent": None,
    "positive": None,
}

_RU_WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

# Тексты баннеров живут в resources/signals.md: пороги и жизненный цикл — здесь,
# копирайт — там. Ключ эпизода строится из фактов, поэтому правка текста не
# трогает ни схлопывание, ни дисмиссы.
_COPY = coach_prompts.fragments("signals", directory=coach_prompts.COPY_DIR)


def _text(name: str, **values: str) -> str:
    """Текст баннера из ``resources/signals.md`` с подставленными слотами."""
    return coach_prompts.render(_COPY[name], **values)


def _signal(
    signal_id: str,
    family: str,
    severity: str,
    title: str,
    body: str,
    *,
    instance_fact: str,
    action_type: str | None,
    action_label: str | None = None,
    action_target: str | None = None,
    note: str | None = None,
    glyph: str | None = None,
) -> dict[str, Any]:
    """Собрать сигнал в форме, которую отдаёт API: id, семейство, ключ эпизода
    (id плюс факт, из которого он состоит), severity, заголовок и тело, действие,
    глиф и можно ли отложить.
    """
    action = None
    if action_type:
        action = {"type": action_type, "label": action_label or ""}
        if action_target:
            action["target"] = action_target
    return {
        "id": signal_id,
        "family": family,
        "instance_key": f"{signal_id}:{instance_fact}",
        "severity": severity,
        "title": title,
        "body": body,
        # Необязательная третья строка (в макетах курсив, приглушённая): подсказка о тренде.
        "note": note,
        # Имя глифа: scale | nutrition | tape | back | wave | doc | check.
        # Незнакомое имя клиент рисует нейтральным глифом.
        "glyph": glyph,
        "action": action,
        "snoozable": severity != "critical",
    }


_RU_MONTHS_GEN = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _ru_date(day: date) -> str:
    """«5 сентября»: число и месяц в родительном падеже для текстов баннеров."""
    return f"{day.day} {_RU_MONTHS_GEN[day.month - 1]}"


def _plural_days(days: int) -> str:
    """«N дн.» для тела баннера."""
    return f"{days} дн."


# --------------------------------------------------------------------------- #
# Семейства сигналов
# --------------------------------------------------------------------------- #
def _waist_limit_signal(
    waists: list[dict[str, Any]],
    state: dict[str, Any],
    today: date,
) -> dict[str, Any] | None:
    """Критический эпизод жёсткого лимита талии: два свежих замера текущей фазы
    на лимите или выше.

    Один шумный замер лентой не должен менять атлету фазу. Пара только из текущей
    фазы и свежесть последней точки заодно гарантируют детерминированный выход:
    после смены фазы, замера ниже лимита или устаревания замеров сигнал гаснет сам.
    """
    if state.get("phase") != "lean_bulk":
        return None
    try:
        limit = float(state.get("waist_limit_cm"))
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None

    started_raw = state.get("phase_started")
    if not isinstance(started_raw, str):
        return None
    try:
        phase_start = date.fromisoformat(started_raw)
    except ValueError:
        return None

    points = [
        point for point in coach_features.waist_points(waists) if phase_start <= point[0] <= today
    ]
    if len(points) < 2:
        return None
    previous, latest = points[-2], points[-1]
    latest_age = (today - latest[0]).days
    if latest_age > MEASUREMENT_OVERDUE_DAYS:
        return None
    if previous[1] < limit or latest[1] < limit:
        return None

    return _signal(
        "waist_limit",
        "measurements",
        "critical",
        _text("waist_limit_title", waist=f"{latest[1]:g}", limit=f"{limit:g}"),
        _text("waist_limit_body"),
        instance_fact=(
            f"pair={previous[0].isoformat()}:{previous[1]:g},"
            f"{latest[0].isoformat()}:{latest[1]:g},limit={limit:g}"
        ),
        action_type="open_measurements",
        action_label=_text("action_measurements"),
        action_target="waist",
        glyph="tape",
    )


def _measurements_signal(
    body_weights: list[dict[str, Any]],
    waists: list[dict[str, Any]],
    state: dict[str, Any],
    today: date,
) -> dict[str, Any] | None:
    """Семейство «замеры»: один баннер о свежести веса и талии.

    Лестница: просрочено (warn) → пора (info, с числом дней до просрочки) →
    в строительных фазах подсказка про мёртвый тренд веса (info), когда свежесть
    ещё в норме, но тренд, по которому матрица питания ведёт калории, посчитать
    нельзя, и сегодня первый день, когда новый замер его оживит.
    """
    weight_points = coach_features.weight_points(body_weights)
    waist_points = coach_features.waist_points(waists)

    def age(points: list[tuple[date, float]]) -> int | None:
        """Дней от последнего замера до ``today``; без замеров — ``None``."""
        return (today - points[-1][0]).days if points else None

    weight_age, waist_age = age(weight_points), age(waist_points)

    overdue: list[str] = []
    due: list[str] = []
    for name, days in (("вес", weight_age), ("талия", waist_age)):
        if days is None or days > MEASUREMENT_OVERDUE_DAYS:
            overdue.append(name)
        elif days >= MEASUREMENT_DUE_DAYS:
            due.append(name)

    last_weight = weight_points[-1][0].isoformat() if weight_points else "none"
    last_waist = waist_points[-1][0].isoformat() if waist_points else "none"
    fact = f"weight={last_weight},waist={last_waist}"
    stale = overdue + due
    target = "waist" if stale == ["талия"] else "weight"

    phase_start = None
    started_raw = state.get("phase_started")
    if isinstance(started_raw, str):
        try:
            phase_start = date.fromisoformat(started_raw)
        except ValueError:
            phase_start = None

    # Заголовки из макетов называют ровно то, что устарело: «Обнови талию — вес свежий».
    if set(stale) == {"вес", "талия"}:
        due_title = _text("measurements_due_title_both")
    elif stale == ["талия"]:
        due_title = _text("measurements_due_title_waist")
    else:
        due_title = _text("measurements_due_title_weight")

    if overdue:
        accusative = {
            "вес": _text("noun_accusative_weight"),
            "талия": _text("noun_accusative_waist"),
        }
        parts = _text("joiner_and").join(accusative.get(part, part) for part in overdue)
        return _signal(
            "measurements_overdue",
            "measurements",
            "warn",
            _text("measurements_overdue_title"),
            _text("measurements_overdue_body", what=parts),
            instance_fact=fact,
            action_type="open_measurements",
            action_label=_text("action_measurements"),
            action_target=target,
            glyph="nutrition",
        )
    if due:
        worst = max(d for d in (weight_age, waist_age) if d is not None)
        days_left = max(1, MEASUREMENT_OVERDUE_DAYS + 1 - worst)
        body = _text("measurements_due_body", days=_plural_days(days_left))
        # Данные почти свежие, а тренд внутри фазы всё ещё голодает: объясняем,
        # зачем ещё одна точка, вместо отдельного зудящего баннера.
        note = None
        if coach_features.weight_trend_per_week(weight_points, today, since=phase_start) is None:
            note = _text("measurements_due_note")
        return _signal(
            "measurements_due",
            "measurements",
            "info",
            due_title,
            body,
            instance_fact=fact,
            action_type="open_measurements",
            action_label=_text("action_measurements"),
            action_target=target,
            note=note,
            glyph="scale",
        )

    # Только строительные фазы: свежесть ещё зелёная, но тренд веса внутри фазы —
    # то, по чему матрица калорий реально рулит, — посчитать нельзя, и сегодня
    # первый день, когда новое взвешивание его оживит.
    phase = state.get("phase")
    if (
        phase in ("cut_recomp", "lean_bulk")
        and weight_age is not None
        and TREND_NUDGE_FROM_DAYS <= weight_age < MEASUREMENT_DUE_DAYS
        and coach_features.weight_trend_per_week(weight_points, today, since=phase_start) is None
    ):
        goal = "среза" if phase == "cut_recomp" else "набора"
        return _signal(
            "weight_trend_stale",
            "measurements",
            "info",
            _text("weight_trend_stale_title"),
            _text("weight_trend_stale_body", goal=goal),
            instance_fact=f"weight={last_weight},phase={phase}",
            action_type="open_measurements",
            action_label=_text("action_measurements"),
            action_target="weight",
            glyph="scale",
        )
    return None


def _return_plan_state(recommendation: dict[str, Any] | None) -> str:
    """Состояние кэшированного совета глазами баннера возврата: нет, генерируется,
    упал, готов и построен для возврата, или готов, но устарел.
    """
    if not recommendation:
        return "none"
    status = recommendation.get("status")
    if status in ("pending", "failed"):
        return str(status)
    if status != "ready":
        return "none"
    payload = recommendation.get("recommendation")
    context = payload.get("coach_context") if isinstance(payload, dict) else None
    if isinstance(context, dict) and context.get("return_from_break") is True:
        return "ready"
    return "outdated"


def _trainings_signal(
    workouts: list[dict[str, Any]],
    today: date,
    recommendation: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Семейство «тренировки»: за 1–3 дня до порога возврата — предупреждение с
    дедлайном (return_soon), с 14-го дня — режим возврата (return_mode) с действием
    по состоянию плана: открыть, обновить, создать или повторить. Пока план
    генерируется, баннер молчит: «Сегодня» уже показывает это.
    """
    dates = sorted(
        {
            when
            for when in (coach_features._workout_date(workout) for workout in workouts)
            if when is not None and when <= today
        }
    )
    if not dates:
        return None
    last = dates[-1]
    days = (today - last).days

    if RETURN_SOON_FROM_DAYS <= days < RETURN_BREAK_DAYS:
        deadline = last + timedelta(days=RETURN_BREAK_DAYS - 1)
        return _signal(
            "return_soon",
            "trainings",
            "warn",
            _text("return_soon_title", deadline=_ru_date(deadline)),
            _text("return_soon_body"),
            instance_fact=f"last_workout={last.isoformat()}",
            action_type="open_next_workout",
            action_label=_text("action_plan"),
            glyph="back",
        )
    if days >= RETURN_BREAK_DAYS:
        plan_state = _return_plan_state(recommendation)
        # «Сегодня» уже показывает состояние генерации. Второй баннер в «Истории»
        # только дублировал бы его и намекал, что план уже есть.
        if plan_state == "pending":
            return None

        if plan_state == "ready":
            title = _text("return_mode_ready_title")
            body = ""
            action_type = "open_next_workout"
            action_label = _text("action_plan")
            recommendation_fact = "ready"
        else:
            title = _text("return_mode_pending_title")
            action_type = "refresh_recommendation"
            if plan_state == "failed":
                body = _text("return_mode_failed_body")
                action_label = _text("action_retry")
                recommendation_fact = f"failed:{recommendation.get('updated_at') or 'unknown'}"
            elif plan_state == "outdated":
                body = _text("return_mode_outdated_body")
                action_label = _text("action_refresh")
                recommendation_fact = f"outdated:{recommendation.get('updated_at') or 'unknown'}"
            else:
                body = _text("return_mode_none_body")
                action_label = _text("action_create")
                recommendation_fact = "none"

        return _signal(
            "return_mode",
            "trainings",
            "accent",
            title,
            body,
            instance_fact=(f"last_workout={last.isoformat()},recommendation={recommendation_fact}"),
            action_type=action_type,
            action_label=action_label,
            glyph="back",
        )
    return None


def _deload_signal(
    state: dict[str, Any], workouts: list[dict[str, Any]], today: date
) -> dict[str, Any] | None:
    """Плановая разгрузка: неделя блока разгрузочная (см. ``coach_state.cycle_position``)
    и на этой неделе ещё не тренировались. Первая сессия разгрузки закрывает баннер.
    """
    position = coach_state.cycle_position(state, workouts, today)
    if not position["deload_week"]:
        return None
    anchor = coach_state._block_anchor(state, workouts, today)
    if anchor is None:
        return None
    week_start = anchor + timedelta(days=(position["block_week"] - 1) * 7)
    trained_this_week = any(
        week_start <= when <= today
        for when in (coach_features._workout_date(workout) for workout in workouts)
        if when is not None
    )
    if trained_this_week:
        # Первая сессия разгрузки закрывает баннер; карточка плана продолжает.
        return None
    return _signal(
        "deload_week",
        "deload",
        "accent",
        _text("deload_title"),
        _text("deload_body"),
        instance_fact=f"week={week_start.isoformat()}",
        action_type="open_next_workout",
        action_label=_text("action_plan"),
        note=_text("deload_note"),
        glyph="wave",
    )


def _report_signal(
    store: Any,
    user_id: int,
    workouts: list[dict[str, Any]],
    now_ts: int,
) -> dict[str, Any] | None:
    """Готовый недельный отчёт: свежий (моложе ``REPORT_FRESH_HOURS``) и ещё не
    прочитанный. В теле период отчёта и, если была плановая работа, процент плана.
    """
    report = store.get_latest_coach_report(user_id)
    if not report or report.get("read_at"):
        return None
    if now_ts - int(report.get("created_at") or 0) > REPORT_FRESH_HOURS * 3600:
        return None
    # Тело из макета: «4–10 августа · 92% плана» — период отчёта плюс
    # выполнение плана за неделю, если плановая работа вообще была.
    try:
        period_end = date.fromisoformat(str(report.get("period_end")))
    except (TypeError, ValueError):
        period_end = None
    body = _text("report_body_default")
    if period_end is not None:
        days = int(report.get("days") or 7)
        period_start = period_end - timedelta(days=days - 1)
        if period_start.month == period_end.month:
            body = f"{period_start.day}–{_ru_date(period_end)}"
        else:
            body = f"{_ru_date(period_start)} – {_ru_date(period_end)}"
        stats = coach_features.adherence_between(workouts, period_start, period_end)
        if stats:
            body += f" · {stats['pct']}% плана"
    return _signal(
        "weekly_report_ready",
        "report",
        "info",
        _text("report_title"),
        body,
        instance_fact=f"period={report.get('period_end')}",
        action_type="open_weekly_report",
        action_label=_text("action_report"),
        glyph="doc",
    )


def _week_done_signal(
    workouts: list[dict[str, Any]], state: dict[str, Any], today: date
) -> dict[str, Any] | None:
    # Последняя ЗАКРЫТАЯ календарная неделя (пн–вс); показывается в пн и вт после неё.
    """Позитивный итог закрытой недели: показывается в понедельник и вторник, если
    сессий не меньше плана фазы (и не меньше двух) и выполнено ≥90% плана.
    """
    week_start = today - timedelta(days=today.weekday() + 7)
    week_end = week_start + timedelta(days=6)
    if (today - week_end).days > WEEK_DONE_SHOW_DAYS:
        return None
    planned = coach_state.phase_params(state).get("sessions_per_week")
    needed = max(
        WEEK_DONE_MIN_SESSIONS,
        int(planned) if isinstance(planned, (int, float)) and not isinstance(planned, bool) else 0,
    )
    stats = coach_features.adherence_between(workouts, week_start, week_end)
    if not stats or stats["sessions"] < needed or stats["pct"] < WEEK_DONE_MIN_PCT:
        return None
    return _signal(
        "week_done",
        "milestone",
        "positive",
        _text("week_done_title", pct=str(stats["pct"])),
        _text("week_done_body"),
        instance_fact=f"week={week_start.isoformat()}",
        action_type=None,
        glyph="check",
    )


# --------------------------------------------------------------------------- #
# Сборка списка
# --------------------------------------------------------------------------- #
_MISSING = object()


def compute_signals(
    store: Any,
    user_id: int,
    state: dict[str, Any],
    *,
    today: date | None = None,
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    """Полный отсортированный список сигналов после схлопывания, фильтра отсрочек и
    правила «без похвалы рядом с предупреждением». Клиент показывает первые 1–2.

    Пока совет генерируется, список пуст: контекст плана меняется, и старые баннеры
    не должны смешиваться с ним. Зовёт server.py на ``GET /api/coach/signals`` и
    при дисмиссе, чтобы найти сигнал по ключу.
    """
    today = today or date.today()
    now_ts = now_ts or int(time.time())

    recommendation = store.get_recommendation(user_id)
    # Пересборка совета меняет контекст плана, к которому ведут несколько
    # баннеров. Старый снимок баннеров с планом в полёте не смешиваем: «История»
    # молчит, пока генерация не дойдёт до конца, а следующий запрос пересчитает
    # все семейства по свежим фактам.
    if recommendation and recommendation.get("status") == "pending":
        return []

    workouts = store.list_workouts(user_id)
    body_weights = store.list_body_weights(user_id)
    waists = store.list_waists(user_id)

    # Одно семейство — одно сообщение: критический эпизод лимита талии
    # вытесняет рутинное напоминание о свежести, а не стоит рядом с ним.
    measurements = _waist_limit_signal(waists, state, today) or _measurements_signal(
        body_weights, waists, state, today
    )
    candidates = [
        measurements,
        _trainings_signal(workouts, today, recommendation),
        _deload_signal(state, workouts, today),
        _report_signal(store, user_id, workouts, now_ts),
        _week_done_signal(workouts, state, today),
    ]
    signals = [signal for signal in candidates if signal is not None]

    snoozes = store.list_signal_snoozes(user_id)
    active: list[dict[str, Any]] = []
    for signal in signals:
        snooze = snoozes.get(signal["instance_key"], _MISSING)
        # Строка с NULL в until — эпизодический дисмисс (скрыт, пока длится
        # эпизод); метка времени прячет, пока не пройдёт.
        if snooze is not _MISSING and (snooze is None or int(snooze) > now_ts):
            continue
        active.append(signal)

    # Похвала рядом с предупреждением ничего не празднует.
    if any(signal["severity"] in ("warn", "critical") for signal in active):
        active = [signal for signal in active if signal["severity"] != "positive"]

    active.sort(
        key=lambda signal: (
            SEVERITY_RANK.get(signal["severity"], 9),
            FAMILY_RANK.get(signal["family"], 9),
        )
    )

    # Подсказку о тренде уровня info легко заслонить: клиент рисует только
    # первый баннер (второй слот открывается лишь под критическим), и, например,
    # в период возврата accent return_mode законно стоит выше. Чтобы не терять
    # подсказку молча, сажаем её приглушённой третьей строкой на тот баннер,
    # что оказался сверху. Под критическим второй слот открыт, и подсказка
    # остаётся своей карточкой.
    trend = next((signal for signal in active if signal["id"] == "weight_trend_stale"), None)
    if trend is not None and active[0] is not trend and active[0]["severity"] != "critical":
        if not active[0].get("note"):
            active[0]["note"] = _text("weight_trend_collapsed_note")
        active.remove(trend)
    return active


def default_snooze_until(severity: str, now_ts: int) -> int | None:
    """Отсрочка по умолчанию для severity: ``None`` — эпизодическая (скрыт, пока не
    сменится ключ эпизода), иначе метка времени, до которой сигнал спрятан.
    """
    hours = SNOOZE_DEFAULT_HOURS.get(severity)
    return None if hours is None else now_ts + hours * 3600


class CriticalSignalDismissed(ValueError):
    """Критический сигнал не откладывается — он гаснет только действием."""


def snooze_until_for(
    matched: dict[str, Any] | None, snooze_hours: object, now_ts: int
) -> int | None:
    """Решение по дисмиссу баннера. Критический не откладывается; явный срок в
    часах — как попросили; иначе дефолт по severity (``SNOOZE_DEFAULT_HOURS``), а
    сигнал, которого уже нет в списке, откладывается как info. Зовёт
    ``server._post_signal_dismiss``, который переводит исключения в 409 и 400.
    """
    if matched is not None and matched["severity"] == "critical":
        raise CriticalSignalDismissed(
            "Критический сигнал не откладывается — он гаснет только действием"
        )
    if snooze_hours is not None:
        try:
            return now_ts + int(snooze_hours) * 3600
        except (TypeError, ValueError) as exc:
            raise ValueError("snooze_hours must be an integer") from exc
    return default_snooze_until(matched["severity"] if matched else "info", now_ts)
