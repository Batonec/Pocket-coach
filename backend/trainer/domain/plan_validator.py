#!/usr/bin/env python3
"""Жёсткие границы плана от модели: что сервер держит сам, а не доверяет промпту.

Правил ровно три, и все они перечислены в ``RULES`` в конце файла. Формулировка
каждого лежит в ``prompts/plan_rules.md`` под тем же именем и оттуда уходит
модели дважды: предложением в системный промпт (блок «ЖЁСТКИЕ ГРАНИЦЫ») и
строкой нарушения в репромпт, — так модель читает ровно то, что сервер
проверяет. Диапазоны повторов, шаги весов, чередование нагрузок и нижняя граница
сессии сознательно не проверяются: это суждение модели, направляемое промптом.
Новая проверка — это запись в ``RULES``, пара фрагментов в markdown и абзац в
``BUSINESS_LOGIC.md``, а не ``if`` внутри существующей.

Правило — это ``check`` (план и рамки → строки нарушений для репромпта) и, если
починка не требует выдумывать чисел, ``fix`` (правит план на месте, возвращает
заголовок пометки и строки правок). ``Bounds`` — факты истории, от плана не
зависящие: считаются один раз, до вызова модели. Движок из двух функций:
``violations`` собирает список для репромпта, ``resolve`` после второго промаха
модели чинит, что можно, и честно вписывает остаток в rationale. Генерация не
падает из-за методики.

Санитизация ответа — типы, клампы, каталожные имена — не методика; она живёт в
``rules.normalize_model_plan``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from trainer.data import coach_prompts
from trainer.domain import coach_features, coach_state

# Сколько дней без эффективного подхода делают группу «сухой» для правила
# покрытия. Порог методики, поэтому он здесь, а не в limits. В строку нарушения
# число уходит слотом; в предложении правила в plan_rules.md оно стоит руками.
COVERAGE_DRY_DAYS = 10

# Тексты двух аудиторий: формулировки и строки нарушений читает модель
# (prompts/), пометки в rationale — атлет (resources/). Грузятся один раз.
_TEXTS = coach_prompts.fragments("plan_rules")
_NOTES = coach_prompts.fragments("plan_notes", directory=coach_prompts.COPY_DIR)


def _text(name: str, **values: str) -> str:
    """Строка нарушения для репромпта из ``plan_rules.md``."""
    return coach_prompts.render(_TEXTS[name], **values)


def _note(name: str, **values: str) -> str:
    """Пометка или строка правки для rationale из ``plan_notes.md``."""
    return coach_prompts.render(_NOTES[name], **values)


# --------------------------------------------------------------------------- #
# Рамки сессии: факты истории, с которыми сравнивается план
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Bounds:
    """Что история говорит о границах ЭТОЙ сессии.

    Считается один раз до вызова модели и от плана не зависит; ``Bounds()`` без
    аргументов — «ограничений нет». Не путать с ``limits``: там статические
    потолки входа, здесь факты конкретного атлета на конкретный день.
    """

    # Доперерывный рабочий вес по id упражнения (элементы
    # ``coach_features.pre_break_working_weights``); пусто вне возврата.
    return_ceilings: dict[int, dict[str, Any]] = field(default_factory=dict)
    # Крупные группы и бицепс бедра без единого эффективного подхода за
    # COVERAGE_DRY_DAYS дней.
    dry_groups: tuple[str, ...] = ()
    # Верхняя граница коридора сессии фазы; None — размер не проверяется.
    session_cap: int | None = None


Check = Callable[[dict[str, Any], Bounds], list[str]]
Fix = Callable[[dict[str, Any], Bounds], tuple[str, list[str]]]


@dataclass(frozen=True)
class Rule:
    """Одна жёсткая граница. ``name`` — имя фрагментов в ``plan_rules.md`` и
    ``plan_notes.md``; ``check`` возвращает строки нарушений; ``fix`` правит план
    на месте и возвращает ``(заголовок пометки, строки правок)`` — или ``None``,
    если чинить без выдумывания чисел нельзя и остаток идёт пометкой.
    """

    name: str
    check: Check
    fix: Fix | None = None


def phase_session_cap(params: dict[str, Any]) -> int | None:
    """Верхняя граница коридора сессии фазы (``session_sets``) или ``None``, если
    параметр не является пригодным диапазоном — тогда размер не проверяется.
    """
    corridor = params.get("session_sets")
    if isinstance(corridor, (list, tuple)) and len(corridor) == 2:
        try:
            cap = int(corridor[1])
        except (TypeError, ValueError, OverflowError):
            return None
        return cap if cap >= 0 else None
    return None


def bounds_from_history(
    workouts: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    today: date,
    *,
    session_cap: int | None = None,
) -> Bounds:
    """Рамки сессии из истории: возвратные потолки (только когда атлет
    возвращается после перерыва), сухие группы правила покрытия и потолок сессии
    фазы, который вызывающий берёт из ``phase_session_cap``. Зовёт
    ``recommender.generate_with_trace`` один раз на генерацию.
    """
    ceilings: dict[int, dict[str, Any]] = {}
    if coach_state.is_return_from_break(workouts, today):
        ceilings = {
            item["exercise_id"]: item
            for item in coach_features.pre_break_working_weights(workouts, catalog)
        }
    recent = coach_features.weekly_volume(workouts, today, days=COVERAGE_DRY_DAYS)
    dry = tuple(
        group
        for group in (*coach_features.BIG_GROUPS, "бицепс бедра")
        if recent[group]["effective"] == 0
    )
    return Bounds(return_ceilings=ceilings, dry_groups=dry, session_cap=session_cap)


# --------------------------------------------------------------------------- #
# Движок: нарушения для репромпта и разрешение после второго промаха
# --------------------------------------------------------------------------- #
def violations(plan: dict[str, Any], bounds: Bounds) -> list[str]:
    """Нарушения жёстких границ в порядке ``RULES``: человекочитаемые строки, которые
    уходят модели в репромпт как есть. Пусто — план принят.
    """
    return [line for rule in RULES for line in rule.check(plan, bounds)]


def resolve(plan: dict[str, Any], bounds: Bounds) -> list[str]:
    """Детерминированный последний рубеж после неудачного репромпта.

    Каждое правило, у которого есть ``fix`` и которое всё ещё нарушено, чинит план
    на месте и получает пометку в rationale; то, что починить нельзя, честно
    вписывается туда же отдельной пометкой. Чуть неидеальный план с видимой
    пометкой лучше карточки с ошибкой — генерация не имеет права падать из-за
    методики. Возвращает строки правок для трассы отладки.
    """
    adjustments: list[str] = []
    notes: list[str] = []
    for rule in RULES:
        if rule.fix is None or not rule.check(plan, bounds):
            continue
        headline, changes = rule.fix(plan, bounds)
        if changes:
            adjustments.extend(changes)
            notes.append(_note("fixed", what=headline, changes="; ".join(changes)))
    remaining = violations(plan, bounds)
    if remaining:
        notes.append(_note("unresolved", violations="; ".join(remaining)))
    if notes:
        rationale = str(plan.get("rationale", "")).rstrip()
        appendix = "\n\n".join(notes)
        plan["rationale"] = f"{rationale}\n\n{appendix}" if rationale else appendix
    return adjustments


# --------------------------------------------------------------------------- #
# Покрытие групп: сухая крупная группа обязана попасть в план
# --------------------------------------------------------------------------- #
def _plan_coverage(plan: dict[str, Any]) -> dict[str, float]:
    """Эффективные подходы плана по группам, с долями косвенной нагрузки."""
    coverage: dict[str, float] = {}
    for exercise in plan["exercises"]:
        shares = coach_features.EFFECTIVE_SETS.get(exercise["exercise_id"]) or {}
        for group, share in shares.items():
            coverage[group] = coverage.get(group, 0.0) + share * len(exercise["sets"])
    return coverage


def _coverage_violations(plan: dict[str, Any], bounds: Bounds) -> list[str]:
    """Сухая группа без единого подхода в плане — нарушение. Починки нет:
    упражнение сервер не выдумывает."""
    covered = _plan_coverage(plan)
    return [
        _text("coverage_violation", group=group, days=str(COVERAGE_DRY_DAYS))
        for group in bounds.dry_groups
        if not covered.get(group)
    ]


# --------------------------------------------------------------------------- #
# Возвратный потолок: после перерыва ни один вес не выше доперерывного рабочего
# --------------------------------------------------------------------------- #
def _capped_sets(
    plan: dict[str, Any], bounds: Bounds
) -> Iterator[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """``(упражнение, подход, потолок)`` для каждого подхода движения, у которого
    есть возвратный потолок."""
    for exercise in plan["exercises"]:
        ceiling = bounds.return_ceilings.get(exercise["exercise_id"])
        if not ceiling:
            continue
        for workout_set in exercise["sets"]:
            yield exercise, workout_set, ceiling


def _too_hard(weight: float, ceiling: dict[str, Any]) -> bool:
    """Тяжелее потолка с учётом инвертированного веса (противовес: меньше — тяжелее)."""
    allowed = ceiling["last_working"]
    return weight < allowed - 1e-9 if ceiling["inverted"] else weight > allowed + 1e-9


def _return_ceiling_violations(plan: dict[str, Any], bounds: Bounds) -> list[str]:
    """Подход тяжелее доперерывного рабочего — нарушение, по одной строке на подход."""
    return [
        _text(
            "return_ceiling_violation",
            exercise=exercise["name"],
            what="противовес" if ceiling["inverted"] else "вес",
            weight=f"{workout_set['weight']:g}",
            allowed=f"{ceiling['last_working']:g}",
        )
        for exercise, workout_set, ceiling in _capped_sets(plan, bounds)
        if _too_hard(workout_set["weight"], ceiling)
    ]


def _clamp_return_weights(plan: dict[str, Any], bounds: Bounds) -> tuple[str, list[str]]:
    """Зажать провинившиеся подходы к доперерывному рабочему: число не выдумано,
    атлет его реально поднимал."""
    changes: list[str] = []
    for exercise, workout_set, ceiling in _capped_sets(plan, bounds):
        if not _too_hard(workout_set["weight"], ceiling):
            continue
        changes.append(
            _note(
                "return_ceiling_change",
                exercise=exercise["name"],
                weight=f"{workout_set['weight']:g}",
                allowed=f"{ceiling['last_working']:g}",
            )
        )
        workout_set["weight"] = ceiling["last_working"]
    return _note("return_ceiling_fixed"), changes


# --------------------------------------------------------------------------- #
# Потолок сессии: рабочих подходов не больше верхней границы коридора фазы
# --------------------------------------------------------------------------- #
def _planned_sets(plan: dict[str, Any]) -> int:
    """Сколько рабочих подходов в плане всего."""
    return sum(len(exercise["sets"]) for exercise in plan["exercises"])


def _session_cap_violations(plan: dict[str, Any], bounds: Bounds) -> list[str]:
    """План длиннее потолка фазы — нарушение. Нижняя граница не проверяется:
    короткая сессия может быть решением. Сессии атлета на ~60 минут срывались на
    карточках в 19–22 подхода, собранных «по дефициту объёма»; коридор — параметр
    самой фазы, и его держит сервер."""
    total = _planned_sets(plan)
    if bounds.session_cap is None or total <= bounds.session_cap:
        return []
    return [_text("session_cap_violation", total=str(total), cap=str(bounds.session_cap))]


def _trim_to_cap(plan: dict[str, Any], bounds: Bounds) -> tuple[str, list[str]]:
    """Снять подходы с хвоста плана, пока он не влезет в потолок: сначала последнее
    упражнение, по одному подходу за проход, никогда не ниже одного подхода на
    упражнение — так правило покрытия (≥1 подход сухой группе) переживает срез.
    Удаление подходов не выдумывает чисел, поэтому у этой границы есть настоящее
    разрешение, а у покрытия только пометка.
    """
    cap = bounds.session_cap
    if cap is None:
        return "", []
    removed: dict[str, int] = {}
    total = _planned_sets(plan)
    while total > cap:
        progressed = False
        for exercise in reversed(plan["exercises"]):
            if total <= cap:
                break
            if len(exercise["sets"]) > 1:
                exercise["sets"].pop()
                removed[exercise["name"]] = removed.get(exercise["name"], 0) + 1
                total -= 1
                progressed = True
        if not progressed:
            break
    changes = [
        _note("session_cap_change", exercise=name, count=str(count))
        for name, count in removed.items()
    ]
    return _note("session_cap_fixed", cap=str(cap)), changes


# --------------------------------------------------------------------------- #
# Три правила. Формулировки — prompts/plan_rules.md под теми же именами; порядок
# здесь — это порядок пунктов в системном промпте и строк в репромпте.
# --------------------------------------------------------------------------- #
RULES: tuple[Rule, ...] = (
    # сухая крупная группа или бицепс бедра обязаны попасть в план; починить
    # нельзя — упражнение не выдумать, остаток идёт пометкой в rationale
    Rule("coverage", check=_coverage_violations),
    # на возврате после перерыва ни один вес не выше доперерывного рабочего
    Rule("return_ceiling", check=_return_ceiling_violations, fix=_clamp_return_weights),
    # рабочих подходов не больше верхней границы коридора сессии фазы
    Rule("session_cap", check=_session_cap_violations, fix=_trim_to_cap),
)
