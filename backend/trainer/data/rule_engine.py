#!/usr/bin/env python3
"""Правила как текст: разбор блоков из markdown, вычисление выражений без
``eval`` и общий ход «проверить → назвать нарушение → починить».

Блок правила в файле фрагментов (``## имя``) — это формулировка (абзац до
первой строки с ключом) и строки ``ключ: значение``::

    для каждой: сухая группа
    требует: подходов_в_плане > 0
    нарушение: группа «{{группа}}» больше {{дней}} дней без подходов
    починить: вес := допустимо        # либо имя процедуры из словаря

Про тренировки, потолки и группы мышц модуль не знает ничего: смысл слов
приносит вызывающий (``plan_validator``) словарём. Область (``Scope``) говорит,
по чему идти и какие имена при этом известны; процедура (``Procedure``) — как
чинить то, что одним присваиванием не починить. Выражения в ``требует`` и
справа от ``:=`` — сравнения (и цепочкой) и связки ``and``/``or``/``not`` над
именами области и числами. Ни вызовов, ни атрибутов, ни арифметики: выражение
читается через ``ast`` и вычисляется по короткому списку узлов, поэтому это
правило, а не программа, и ``eval`` не нужен.

Всё, что может разойтись между текстом и словарём — неизвестная область, имя не
из области, слот без значения, процедура без словаря, пометка без починки, —
падает при загрузке книги (``RuleBook.load``), то есть на старте сервера, а не
на генерации. Как и незаполненный слот промпта в ``coach_prompts``.
"""

from __future__ import annotations

import ast
import operator
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from trainer.data import coach_prompts


class RuleError(RuntimeError):
    """Правило не разобрать, или его слова не сходятся со словарём."""


# --------------------------------------------------------------------------- #
# Словарь: что приносит вызывающий
# --------------------------------------------------------------------------- #
@dataclass
class Binding:
    """Один случай области: значения имён и, для имён с правом записи, куда писать."""

    values: dict[str, Any]
    targets: dict[str, tuple[dict[str, Any], str]] = field(default_factory=dict)

    def assign(self, name: str, value: Any) -> None:
        """Записать значение в предмет (``починить: имя := ...``) и в сам случай."""
        container, key = self.targets[name]
        container[key] = value
        self.values[name] = value


@dataclass(frozen=True)
class Scope:
    """Область правила. ``iterate(*subject)`` перечисляет случаи; ``provides`` —
    имена, известные в каждом (для выражений и слотов текстов); ``writable`` — те
    из них, которым ``починить: имя := ...`` имеет право присвоить.
    """

    iterate: Callable[..., Iterator[Binding]]
    provides: tuple[str, ...]
    writable: tuple[str, ...] = ()


@dataclass(frozen=True)
class Procedure:
    """Починка, которая не сводится к присваиванию. ``run(*subject, binding)``
    правит предмет и возвращает по словарю на каждую правку; из них рендерятся
    строки ``имя_change`` с именами ``provides``.
    """

    run: Callable[..., list[dict[str, Any]]]
    provides: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Выражения
# --------------------------------------------------------------------------- #
_COMPARISONS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}


# Узлы, из которых может состоять выражение правила. Всё остальное — вызовы,
# атрибуты, арифметика, индексы, in — отвергается уже при разборе, то есть при
# загрузке книги, а не на первом случае области.
_ALLOWED_NODES = (
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Compare,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    *_COMPARISONS,
)


def _parse(expression: str) -> ast.expr:
    try:
        body = ast.parse(expression.strip(), mode="eval").body
    except SyntaxError as exc:
        raise RuleError(f"не разобрать выражение «{expression}»: {exc.msg}") from exc
    for node in ast.walk(body):
        if not isinstance(node, _ALLOWED_NODES):
            raise RuleError(f"в выражении «{expression}» нельзя: {type(node).__name__}")
    return body


def names_in(expression: str) -> set[str]:
    """Имена, которые выражение читает."""
    return {node.id for node in ast.walk(_parse(expression)) if isinstance(node, ast.Name)}


def evaluate(expression: str, names: Mapping[str, Any]) -> Any:
    """Значение выражения при данных именах: константы, имена, сравнения (в том
    числе цепочкой ``a < b <= c``), ``and`` / ``or`` / ``not``. Всё остальное —
    ``RuleError``.
    """
    return _evaluate(_parse(expression), names)


def _evaluate(node: ast.expr, names: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in names:
            raise RuleError(f"в выражении неизвестное имя «{node.id}»")
        return names[node.id]
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, names)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _evaluate(comparator, names)
            if not _COMPARISONS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        values = (_evaluate(value, names) for value in node.values)
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp):
        return not _evaluate(node.operand, names)
    raise RuleError(f"в выражении нельзя: {type(node).__name__}")  # _parse такого не пропустит


# --------------------------------------------------------------------------- #
# Разбор блока правила
# --------------------------------------------------------------------------- #
_KEY_LINE = re.compile(r"^(для(?: каждой| каждого)?|требует|нарушение|починить):\s*(.+?)\s*$")
_REQUIRED_KEYS = ("для", "требует", "нарушение")


@dataclass(frozen=True)
class Assign:
    """``починить: имя := выражение`` — присвоить имени области значение выражения."""

    target: str
    expression: str


@dataclass(frozen=True)
class Call:
    """``починить: фраза`` — позвать процедуру из словаря."""

    procedure: str


Fix = Assign | Call


@dataclass(frozen=True)
class Rule:
    """Одно правило из markdown."""

    name: str
    sentence: str  # формулировка: уходит модели как есть
    scope: str  # фраза области из словаря
    check: str  # выражение, которое обязано быть истинным в каждом случае
    violation: str  # шаблон строки нарушения со слотами из области
    fix: Fix | None = None


def parse_rule(name: str, body: str) -> Rule | None:
    """Правило из тела фрагмента или ``None``, если это обычный текст без ключей."""
    sentence: list[str] = []
    fields: dict[str, str] = {}
    for line in body.splitlines():
        match = _KEY_LINE.match(line)
        if match:
            key = "для" if match.group(1).startswith("для") else match.group(1)
            if key in fields:
                raise RuleError(f"правило {name!r}: ключ «{key}» повторяется")
            fields[key] = match.group(2)
        elif fields and line.strip():
            raise RuleError(f"правило {name!r}: после ключей текст не ожидается: «{line.strip()}»")
        elif not fields:
            sentence.append(line.strip())
    if not fields:
        return None
    missing = [key for key in _REQUIRED_KEYS if key not in fields]
    if missing:
        raise RuleError(f"правило {name!r}: нет ключей {', '.join(missing)}")
    formulation = " ".join(part for part in sentence if part)
    if not formulation:
        raise RuleError(f"правило {name!r}: нет формулировки перед ключами")
    return Rule(
        name,
        formulation,
        fields["для"],
        fields["требует"],
        fields["нарушение"],
        _parse_fix(name, fields.get("починить")),
    )


def _parse_fix(name: str, value: str | None) -> Fix | None:
    if value is None:
        return None
    if ":=" not in value:
        return Call(value)
    target, expression = (part.strip() for part in value.split(":=", 1))
    if not target or not expression:
        raise RuleError(
            f"правило {name!r}: починить — ожидается «имя := выражение», а не «{value}»"
        )
    return Assign(target, expression)


# --------------------------------------------------------------------------- #
# Книга правил: загрузка с проверкой словаря и общий ход
# --------------------------------------------------------------------------- #
# Две пометки, которые книга рендерит сама: про починку одного правила и про
# то, что починить не вышло. Их слоты фиксированы здесь.
FIXED_NOTE = "fixed"  # слоты: что, правки
UNRESOLVED_NOTE = "unresolved"  # слот: нарушения
_NOTE_SLOTS = {FIXED_NOTE: {"что", "правки"}, UNRESOLVED_NOTE: {"нарушения"}}


def _show(value: Any) -> str:
    """Значение в текст: число без хвоста ``.0`` (``:g``), остальное как есть."""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _render(template: str, values: Mapping[str, Any]) -> str:
    return coach_prompts.render(
        template, **{slot: _show(values[slot]) for slot in coach_prompts.slots(template)}
    )


@dataclass(frozen=True)
class RuleBook:
    """Правила из одного файла вместе со словарём и пометками, по которым они
    исполняются. Строится через :meth:`load`, которая сверяет каждое слово.
    """

    rules: tuple[Rule, ...]
    scopes: Mapping[str, Scope]
    procedures: Mapping[str, Procedure]
    notes: Mapping[str, str]

    @classmethod
    def load(
        cls,
        fragments: Mapping[str, str],
        *,
        scopes: Mapping[str, Scope],
        procedures: Mapping[str, Procedure],
        notes: Mapping[str, str],
    ) -> RuleBook:
        """Разобрать блоки с ключами (остальные фрагменты — обычный текст, он не
        трогается) и сверить каждое слово со словарём и пометками.
        """
        parsed = (parse_rule(name, body) for name, body in fragments.items())
        book = cls(tuple(rule for rule in parsed if rule is not None), scopes, procedures, notes)
        for name, scope in scopes.items():
            stray = set(scope.writable) - set(scope.provides)
            if stray:
                raise RuleError(
                    f"область «{name}»: writable не из provides: {', '.join(sorted(stray))}"
                )
        for note, expected in _NOTE_SLOTS.items():
            if note not in notes:
                raise RuleError(f"нет пометки «{note}»")
            _expect(f"пометка «{note}»", coach_prompts.slots(notes[note]), expected)
        for rule in book.rules:
            book._check_words(rule)
        return book

    def _check_words(self, rule: Rule) -> None:
        where = f"правило {rule.name!r}"
        scope = self.scopes.get(rule.scope)
        if scope is None:
            raise RuleError(
                f"{where}: неизвестная область «{rule.scope}»; есть: {', '.join(self.scopes)}"
            )
        known = set(scope.provides)
        _expect(f"{where}, требует", names_in(rule.check), known)
        _expect(f"{where}, нарушение", coach_prompts.slots(rule.violation), known)
        fixed_note, change_note = f"{rule.name}_fixed", f"{rule.name}_change"
        if rule.fix is None:
            for note in (fixed_note, change_note):
                if note in self.notes:
                    raise RuleError(f"{where}: пометка «{note}» без починки — мёртвый текст")
            return
        if isinstance(rule.fix, Call):
            procedure = self.procedures.get(rule.fix.procedure)
            if procedure is None:
                raise RuleError(
                    f"{where}: неизвестная процедура «{rule.fix.procedure}»; есть: {', '.join(self.procedures)}"
                )
            change_names = set(procedure.provides)
        else:
            if rule.fix.target not in scope.writable:
                allowed = ", ".join(scope.writable) or "ничему"
                raise RuleError(
                    f"{where}: имени «{rule.fix.target}» нельзя присвоить; можно: {allowed}"
                )
            _expect(f"{where}, починить", names_in(rule.fix.expression), known)
            change_names = known
        for note, allowed in ((fixed_note, known), (change_note, change_names)):
            if note not in self.notes:
                raise RuleError(f"{where}: нет пометки «{note}»")
            _expect(f"{where}, пометка «{note}»", coach_prompts.slots(self.notes[note]), allowed)

    def violations(self, *subject: Any) -> list[str]:
        """Строки нарушений по всем правилам в порядке книги; пусто — предмет принят."""
        return [
            _render(rule.violation, binding.values)
            for rule in self.rules
            for binding in self._failing(rule, *subject)
        ]

    def repair(self, *subject: Any) -> tuple[list[str], list[str]]:
        """Починить, что правила умеют, и назвать остальное: ``(правки, пометки)``.

        Пометка на правило — ``fixed`` с заголовком ``имя_fixed`` и правками через
        «;»; то, что после починок всё ещё нарушено, — одной пометкой ``unresolved``.
        """
        all_changes: list[str] = []
        notes: list[str] = []
        for rule in self.rules:
            if rule.fix is None:
                continue
            changes: list[str] = []
            first: Binding | None = None
            for binding in self._failing(rule, *subject):
                first = first or binding
                changes.extend(self._apply(rule, rule.fix, binding, *subject))
            if changes and first is not None:
                what = _render(self.notes[f"{rule.name}_fixed"], first.values)
                notes.append(
                    _render(self.notes[FIXED_NOTE], {"что": what, "правки": "; ".join(changes)})
                )
                all_changes.extend(changes)
        remaining = self.violations(*subject)
        if remaining:
            notes.append(_render(self.notes[UNRESOLVED_NOTE], {"нарушения": "; ".join(remaining)}))
        return all_changes, notes

    def _failing(self, rule: Rule, *subject: Any) -> Iterator[Binding]:
        scope = self.scopes[rule.scope]
        for binding in scope.iterate(*subject):
            missing = set(scope.provides) - set(binding.values)
            if missing:
                raise RuleError(
                    f"область «{rule.scope}» обещала имена, которых не дала: {', '.join(sorted(missing))}"
                )
            if not evaluate(rule.check, binding.values):
                yield binding

    def _apply(self, rule: Rule, fix: Fix, binding: Binding, *subject: Any) -> list[str]:
        change = self.notes[f"{rule.name}_change"]
        if isinstance(fix, Call):
            return [
                _render(change, values)
                for values in self.procedures[fix.procedure].run(*subject, binding)
            ]
        # Строка правки — до присваивания: в ней и старое значение, и новое.
        rendered = _render(change, binding.values)
        binding.assign(fix.target, evaluate(fix.expression, binding.values))
        return [rendered]


def _expect(where: str, used: set[str], known: set[str]) -> None:
    unknown = used - known
    if unknown:
        raise RuleError(
            f"{where}: имена не из области: {', '.join(sorted(unknown))}; известны: {', '.join(sorted(known))}"
        )
