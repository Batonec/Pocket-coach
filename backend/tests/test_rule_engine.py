"""Движок правил: разбор блока из markdown, выражения без eval, сверка слов при
загрузке и общий ход «нарушение → починка → пометка» на игрушечном словаре.
Про тренировки здесь ничего: как этим движком записаны реальные границы,
проверяет test_plan_validator.
"""

from __future__ import annotations

import unittest

import support  # noqa: F401 — кладёт backend в sys.path

from trainer.data import rule_engine
from trainer.data.rule_engine import Assign, Binding, Call, Procedure, RuleBook, RuleError, Scope


def _items(plan, bounds):
    """Область «предмет»: каждый элемент плана, вес с правом записи."""
    for item in plan["items"]:
        yield Binding(
            {"имя": item["name"], "вес": item["weight"], "допустимо": bounds["cap_weight"]},
            targets={"вес": (item, "weight")},
        )


def _whole(plan, bounds):
    """Область «план»: один случай."""
    yield Binding({"всего": len(plan["items"]), "потолок": bounds["cap"]})


def _drop_tail(plan, bounds, binding):
    """Процедура: снимать элементы с хвоста, пока их не станет «потолок»."""
    removed = []
    while len(plan["items"]) > binding.values["потолок"]:
        removed.append({"имя": plan["items"].pop()["name"], "снято": 1})
    return removed


SCOPES = {
    "предмет": Scope(_items, provides=("имя", "вес", "допустимо"), writable=("вес",)),
    "план": Scope(_whole, provides=("всего", "потолок")),
}
PROCEDURES = {"снять хвост": Procedure(_drop_tail, provides=("имя", "снято"))}
NOTES = {
    "fixed": "Проверка: {{что}}: {{правки}}.",
    "unresolved": "Не вышло: {{нарушения}}.",
    "weight_change": "{{имя}}: {{вес}} → {{допустимо}}",
    "weight_fixed": "веса зажаты",
    "cap_change": "{{имя}} −{{снято}}",
    "cap_fixed": "срезано до {{потолок}}",
}
WEIGHT = (
    "вес не выше допустимого\n"
    "\n"
    "для каждого: предмет\n"
    "требует: вес <= допустимо\n"
    "нарушение: {{имя}}: {{вес}} выше {{допустимо}}\n"
    "починить: вес := допустимо"
)
CAP = (
    "не больше потолка\n"
    "\n"
    "для: план\n"
    "требует: всего <= потолок\n"
    "нарушение: {{всего}} при потолке {{потолок}}\n"
    "починить: снять хвост"
)
COVER = "покрытие\n\nдля каждого: предмет\nтребует: вес > 0\nнарушение: {{имя}} пустой"


def _load(fragments, **overrides):
    return RuleBook.load(
        fragments,
        scopes=overrides.get("scopes", SCOPES),
        procedures=overrides.get("procedures", PROCEDURES),
        notes=overrides.get("notes", NOTES),
    )


def _plan(*weights):
    return {"items": [{"name": f"#{i}", "weight": w} for i, w in enumerate(weights, start=1)]}


class ParseTests(unittest.TestCase):
    """Блок с ключами — правило, без ключей — обычный текст."""

    def test_plain_fragment_is_not_a_rule(self) -> None:
        self.assertIsNone(rule_engine.parse_rule("x", "просто текст\nв две строки"))

    def test_rule_fields_and_assign_fix(self) -> None:
        rule = rule_engine.parse_rule("weight", WEIGHT)
        assert rule is not None
        self.assertEqual(rule.sentence, "вес не выше допустимого")
        self.assertEqual(rule.scope, "предмет")
        self.assertEqual(rule.check, "вес <= допустимо")
        self.assertEqual(rule.violation, "{{имя}}: {{вес}} выше {{допустимо}}")
        self.assertEqual(rule.fix, Assign("вес", "допустимо"))

    def test_procedure_fix_and_no_fix(self) -> None:
        cap = rule_engine.parse_rule("cap", CAP)
        assert cap is not None
        self.assertEqual(cap.fix, Call("снять хвост"))
        cover = rule_engine.parse_rule("cover", COVER)
        assert cover is not None
        self.assertIsNone(cover.fix)

    def test_sentence_may_span_lines_and_any_gender_of_for(self) -> None:
        for key in ("для", "для каждой", "для каждого"):
            rule = rule_engine.parse_rule(
                "x",
                f"первая строка\nвторая\n\n{key}: план\nтребует: всего <= потолок\nнарушение: н",
            )
            assert rule is not None
            self.assertEqual(rule.sentence, "первая строка вторая")
            self.assertEqual(rule.scope, "план")

    def test_broken_blocks_fail_loudly(self) -> None:
        cases = (
            ("ф\nдля: план\nтребует: всего <= потолок", "нет ключей нарушение"),
            ("ф\nдля: план\nдля: план\nтребует: 1 < 2\nнарушение: н", "повторяется"),
            ("ф\nдля: план\nтребует: 1 < 2\nнарушение: н\nхвост", "после ключей"),
            ("для: план\nтребует: 1 < 2\nнарушение: н", "нет формулировки"),
            ("ф\nдля: план\nтребует: 1 < 2\nнарушение: н\nпочинить: := x", "имя := выражение"),
        )
        for body, message in cases:
            with self.subTest(body=body), self.assertRaisesRegex(RuleError, message):
                rule_engine.parse_rule("x", body)


class ExpressionTests(unittest.TestCase):
    """Сравнения и связки — и ничего больше."""

    def test_comparisons_chains_and_logic(self) -> None:
        names = {"a": 1, "b": 2.5, "c": 2.5}
        self.assertTrue(rule_engine.evaluate("a < b <= c", names))
        self.assertFalse(rule_engine.evaluate("a < b < c", names))
        self.assertTrue(rule_engine.evaluate("a == 1 and (b != 2 or not c > 3)", names))
        self.assertTrue(rule_engine.evaluate("not a >= 2", names))
        self.assertEqual(rule_engine.names_in("a < b and not c"), {"a", "b", "c"})

    def test_unknown_name_and_bad_syntax_fail(self) -> None:
        with self.assertRaisesRegex(RuleError, "неизвестное имя «x»"):
            rule_engine.evaluate("x > 0", {})
        with self.assertRaisesRegex(RuleError, "не разобрать"):
            rule_engine.evaluate("a >", {"a": 1})

    def test_only_comparisons_and_logic_are_allowed(self) -> None:
        # Ни вызовов, ни атрибутов, ни арифметики: правило — не программа.
        for expression in ("__import__('os')", "a.b", "a + 1", "[a]", "a if a else a", "a in b"):
            with self.subTest(expression=expression), self.assertRaisesRegex(RuleError, "нельзя"):
                rule_engine.evaluate(expression, {"a": 1, "b": [1]})


class LoadTests(unittest.TestCase):
    """Каждое слово правила сверяется со словарём при загрузке, а не на генерации."""

    def test_unknown_words_fail_at_load(self) -> None:
        cases = (
            ("weight", WEIGHT.replace("предмет", "нечто"), "неизвестная область «нечто»"),
            ("weight", WEIGHT.replace("вес <= допустимо", "вес <= максимум"), "требует.*максимум"),
            ("weight", WEIGHT.replace("{{допустимо}}", "{{лимит}}"), "нарушение.*лимит"),
            ("weight", WEIGHT.replace("вес := допустимо", "допустимо := вес"), "нельзя присвоить"),
            ("weight", WEIGHT.replace("вес := допустимо", "вес := потолок"), "починить.*потолок"),
            ("cap", CAP.replace("снять хвост", "снять голову"), "неизвестная процедура"),
            # Выражение вне грамматики ловится при загрузке, а не на первом случае.
            ("weight", WEIGHT.replace("вес <= допустимо", "вес + 1 <= допустимо"), "нельзя: BinOp"),
        )
        for name, body, message in cases:
            with self.subTest(body=body), self.assertRaisesRegex(RuleError, message):
                _load({name: body})

    def test_notes_are_checked_both_ways(self) -> None:
        without_change = {k: v for k, v in NOTES.items() if k != "weight_change"}
        with self.assertRaisesRegex(RuleError, "нет пометки «weight_change»"):
            _load({"weight": WEIGHT}, notes=without_change)
        # Пометка починки у правила без «починить:» — мёртвый текст.
        with self.assertRaisesRegex(RuleError, "мёртвый текст"):
            _load({"weight": COVER})
        with self.assertRaisesRegex(RuleError, "weight_change.*чего"):
            _load({"weight": WEIGHT}, notes=dict(NOTES, weight_change="{{имя}}: {{чего}}"))
        with self.assertRaisesRegex(RuleError, "нет пометки «unresolved»"):
            _load({}, notes={"fixed": "{{что}} {{правки}}"})

    def test_dictionary_itself_is_checked(self) -> None:
        crooked = dict(SCOPES, кривая=Scope(_whole, provides=("всего",), writable=("потолок",)))
        with self.assertRaisesRegex(RuleError, "writable не из provides"):
            _load({}, scopes=crooked)
        # Область обещала имя, которого не даёт: падает на первом случае, по имени.
        boastful = dict(SCOPES, план=Scope(_whole, provides=("всего", "потолок", "лишнее")))
        book = _load({"cap": CAP}, scopes=boastful)
        with self.assertRaisesRegex(RuleError, "обещала имена.*лишнее"):
            book.violations(_plan(1, 2), {"cap": 1, "cap_weight": 5})


class BookTests(unittest.TestCase):
    """Общий ход на игрушечном словаре: нарушения по порядку, починка, пометки."""

    def setUp(self) -> None:
        self.book = _load({"weight": WEIGHT, "cap": CAP, "cover": COVER})
        self.bounds = {"cap_weight": 10, "cap": 2}

    def test_clean_plan_has_no_violations_and_nothing_to_repair(self) -> None:
        self.assertEqual(self.book.violations(_plan(5, 10), self.bounds), [])
        self.assertEqual(self.book.repair(_plan(5), self.bounds), ([], []))

    def test_violations_follow_rule_order_and_render_numbers_plainly(self) -> None:
        lines = self.book.violations(_plan(12.5, 0.0, 11), self.bounds)
        self.assertEqual(
            lines, ["#1: 12.5 выше 10", "#3: 11 выше 10", "3 при потолке 2", "#2 пустой"]
        )

    def test_repair_fixes_what_it_can_and_notes_the_rest(self) -> None:
        plan = _plan(12.5, 0.0, 11)
        changes, notes = self.book.repair(plan, self.bounds)
        # Веса зажаты до допустимого, хвост снят до потолка; пустой #2 чинить нечем.
        self.assertEqual(changes, ["#1: 12.5 → 10", "#3: 11 → 10", "#3 −1"])
        self.assertEqual([item["weight"] for item in plan["items"]], [10, 0.0])
        self.assertEqual(
            notes,
            [
                "Проверка: веса зажаты: #1: 12.5 → 10; #3: 11 → 10.",
                "Проверка: срезано до 2: #3 −1.",
                "Не вышло: #2 пустой.",
            ],
        )
