#!/usr/bin/env python3
"""Жёсткие границы плана от модели. Сами правила — текст в
``prompts/plan_rules.md``; здесь факты истории, с которыми план сравнивают, и
словарь слов, которыми правила написаны.

Правило в markdown — формулировка (она же уходит модели в системный промпт),
область («для каждой: сухая группа»), условие («требует: подходов_в_плане > 0»),
строка нарушения для репромпта и, если чинить можно без выдумывания чисел,
«починить: вес := допустимо» или имя процедуры. Читает и исполняет их
``data/rule_engine``; смысл слов задаёт словарь ниже: область говорит, по чему
идти и что при этом известно, процедура — как чинить. Новое правило — блок в
markdown; новое слово нужно, только если правило говорит о факте, которого
сервер ещё не считает. Слово без правила и правило без слова падают на старте.

Диапазоны повторов, шаги весов, чередование нагрузок и нижняя граница сессии
сознательно не проверяются: это суждение модели, направляемое промптом.
Санитизация ответа — ``rules.normalize_model_plan``; формулировки в системный
промпт рендерит ``prompt_builder`` из ``RULES``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from trainer.data import coach_prompts, rule_engine
from trainer.data.rule_engine import Binding
from trainer.domain import coach_features, coach_state

# Сколько дней без эффективного подхода делают группу «сухой» для правила
# покрытия. Порог методики, поэтому он здесь, а не в limits; в строку нарушения
# число уходит именем «дней», в формулировке правила оно написано руками.
COVERAGE_DRY_DAYS = 10


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


def planned_sets(plan: dict[str, Any]) -> int:
    """Сколько рабочих подходов в плане всего."""
    return sum(len(exercise["sets"]) for exercise in plan["exercises"])


def plan_coverage(plan: dict[str, Any]) -> dict[str, float]:
    """Эффективные подходы плана по группам, с долями косвенной нагрузки."""
    coverage: dict[str, float] = {}
    for exercise in plan["exercises"]:
        shares = coach_features.EFFECTIVE_SETS.get(exercise["exercise_id"]) or {}
        for group, share in shares.items():
            coverage[group] = coverage.get(group, 0.0) + share * len(exercise["sets"])
    return coverage


# --------------------------------------------------------------------------- #
# Словарь правил: области — по чему идти и что при этом известно
# --------------------------------------------------------------------------- #
def _dry_groups(plan: dict[str, Any], bounds: Bounds) -> Iterator[Binding]:
    """Каждая сухая группа и сколько эффективных подходов ей даёт план."""
    covered = plan_coverage(plan)
    for group in bounds.dry_groups:
        yield Binding(
            {
                "группа": group,
                "дней": COVERAGE_DRY_DAYS,
                "подходов_в_плане": covered.get(group, 0.0),
            }
        )


def _capped_sets(plan: dict[str, Any], bounds: Bounds) -> Iterator[Binding]:
    """Каждый подход движения с возвратным потолком. «Тяжесть» и «потолок» —
    сравнимые числа: у противовеса (гравитрон) меньше значит тяжелее, поэтому там
    оба со знаком минус, и правило пишется одинаково для любого тренажёра.
    """
    for exercise in plan["exercises"]:
        ceiling = bounds.return_ceilings.get(exercise["exercise_id"])
        if not ceiling:
            continue
        sign = -1 if ceiling["inverted"] else 1
        for workout_set in exercise["sets"]:
            yield Binding(
                {
                    "упражнение": exercise["name"],
                    "что": "противовес" if ceiling["inverted"] else "вес",
                    "вес": workout_set["weight"],
                    "допустимо": ceiling["last_working"],
                    "тяжесть": sign * workout_set["weight"],
                    "потолок": sign * ceiling["last_working"],
                },
                targets={"вес": (workout_set, "weight")},
            )


def _capped_plan(plan: dict[str, Any], bounds: Bounds) -> Iterator[Binding]:
    """План целиком, один случай — и только когда у фазы есть потолок сессии."""
    if bounds.session_cap is not None:
        yield Binding({"всего_подходов": planned_sets(plan), "потолок": bounds.session_cap})


def _trim_to_cap(plan: dict[str, Any], _bounds: Bounds, binding: Binding) -> list[dict[str, Any]]:
    """Снять подходы с хвоста плана, пока он не влезет в потолок: сначала последнее
    упражнение, по одному подходу за проход, никогда не ниже одного подхода на
    упражнение — так правило покрытия (≥1 подход сухой группе) переживает срез.
    Чисел не выдумывает, поэтому у потолка сессии есть настоящая починка, а у
    покрытия только пометка. Возвращает по словарю на упражнение: сколько снято.
    """
    cap = binding.values["потолок"]
    removed: dict[str, int] = {}
    total = planned_sets(plan)
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
    return [{"упражнение": name, "снято": count} for name, count in removed.items()]


SCOPES: dict[str, rule_engine.Scope] = {
    "сухая группа": rule_engine.Scope(_dry_groups, provides=("группа", "дней", "подходов_в_плане")),
    "подход с возвратным потолком": rule_engine.Scope(
        _capped_sets,
        provides=("упражнение", "что", "вес", "допустимо", "тяжесть", "потолок"),
        writable=("вес",),
    ),
    "план с потолком сессии": rule_engine.Scope(
        _capped_plan, provides=("всего_подходов", "потолок")
    ),
}

PROCEDURES: dict[str, rule_engine.Procedure] = {
    "срезать подходы с хвоста до потолка": rule_engine.Procedure(
        _trim_to_cap, provides=("упражнение", "снято")
    ),
}

# Книга правил собирается на импорте: правило без слова в словаре, слот без
# значения или пометка без починки останавливают сервер на старте, как и
# незаполненный слот промпта.
BOOK = rule_engine.RuleBook.load(
    coach_prompts.fragments("plan_rules"),
    scopes=SCOPES,
    procedures=PROCEDURES,
    notes=coach_prompts.fragments("plan_notes", directory=coach_prompts.COPY_DIR),
)
RULES = BOOK.rules


# --------------------------------------------------------------------------- #
# Что зовёт recommender
# --------------------------------------------------------------------------- #
def violations(plan: dict[str, Any], bounds: Bounds) -> list[str]:
    """Нарушения жёстких границ в порядке правил: строки для репромпта как есть.
    Пусто — план принят."""
    return BOOK.violations(plan, bounds)


def resolve(plan: dict[str, Any], bounds: Bounds) -> list[str]:
    """Детерминированный последний рубеж после неудачного репромпта: починить,
    что правила умеют, и честно вписать остаток пометкой в rationale. Чуть
    неидеальный план с видимой пометкой лучше карточки с ошибкой — генерация не
    имеет права падать из-за методики. Возвращает строки правок для трассы.
    """
    adjustments, notes = BOOK.repair(plan, bounds)
    if notes:
        rationale = str(plan.get("rationale", "")).rstrip()
        appendix = "\n\n".join(notes)
        plan["rationale"] = f"{rationale}\n\n{appendix}" if rationale else appendix
    return adjustments
