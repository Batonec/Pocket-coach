"""Шаблоны промптов: слоты совпадают с тем, что считает код, фрагменты и тексты
баннеров не мёртвые, срез стратегии идёт по заголовкам, собранный промпт без
пустых слотов.
"""

from __future__ import annotations

import pathlib
import unittest

import support
from support import CATALOG_PATH

from trainer.data import coach_prompts, files
from trainer.domain import prompt_builder


class PromptTemplateTests(unittest.TestCase):
    """Загрузчик ``coach_prompts`` и рендеры ``prompt_builder`` против файлов в prompts/."""

    def test_system_template_expects_exactly_the_computed_slots(self):
        """Слоты шаблона и то, что считает код, обязаны совпадать.

        Слот, добавленный в markdown без значения в коде, отправил бы «{{...}}» прямо
        в модель; значение без слота молча пропало бы из промпта. И то и другое
        падает громко.
        """
        self.assertEqual(
            coach_prompts.slots(coach_prompts.load("system")),
            {"profile", "catalog", "catalog_gaps", "phase_policy", "program"},
        )

    def test_missing_slot_raises(self):
        with self.assertRaises(coach_prompts.PromptError):
            coach_prompts.render("а {{one}} и {{two}}", one="1")

    def test_unknown_slot_raises(self):
        with self.assertRaises(coach_prompts.PromptError):
            coach_prompts.render("а {{one}}", one="1", two="2")

    def test_missing_template_raises(self):
        with self.assertRaises(coach_prompts.PromptError):
            coach_prompts.load("no_such_prompt")

    def test_braces_in_prose_are_not_touched(self):
        """Под капотом нет str.format: JSON-фрагменты и диапазоны в прозе должны
        выживать без экранирования.
        """
        self.assertEqual(
            coach_prompts.render('{"reps": 12} и {{slot}}', slot="X"),
            '{"reps": 12} и X',
        )

    def test_phase_policy_template_slots_match_the_renderer(self):
        """Каждый слот phase_policy.md обязан заполняться _render_phase_policy для любой
        фазы — новое число в прозе не должно уехать как «{{...}}».
        """
        from trainer.domain import coach_state

        expected = coach_prompts.slots(coach_prompts.load("phase_policy"))
        self.assertEqual(expected, prompt_builder._PHASE_POLICY_SLOTS)
        for phase in coach_state.PHASES:
            state = coach_state.default_state()
            state["phase"] = phase
            rendered = prompt_builder._render_phase_policy(state)
            self.assertNotIn("{{", rendered, phase)

    def test_report_prompt_carries_profile_program_and_gate(self):
        """Отчёт — единственное место, где жёсткий гейт этапа может прозвучать
        вслух, поэтому он получает и профиль, и программу."""
        built = prompt_builder._build_report_system_prompt(
            {"blocks": {"Цель": "тело цели"}},
            "## 4. Тренировочные дни\nкаркас\n",
        )
        self.assertNotIn("{{", built)
        self.assertIn("тело цели", built)
        self.assertIn("каркас", built)
        self.assertIn("Гейт этапа", built)

    def test_report_prompt_without_profile_still_builds(self):
        built = prompt_builder._build_report_system_prompt()
        self.assertNotIn("{{", built)
        self.assertNotIn("=== ПРОГРАММА", built)

    def test_every_declared_block_is_used_by_the_code(self):
        """Фрагмент, оставшийся в файле без единого вызова _block(), — мёртвый
        текст: его правят, а в промпт он не едет."""
        import re

        source = "".join(
            (support.MINIAPP_DIR / "trainer" / "domain" / name).read_text("utf-8")
            for name in ("prompt_builder.py", "recommender.py")
        )
        used = set(re.findall(r'_block\(\s*\n?\s*"([a-z_]+)"', source))
        used |= set(re.findall(r'"(report_deload_(?:yes|no))"', source))
        declared = set(coach_prompts.fragments("user_blocks"))
        self.assertEqual(declared - used, set(), "объявлены, но не используются")
        self.assertEqual(used - declared, set(), "используются, но не объявлены")

    def test_duplicate_fragment_name_raises(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            saved = coach_prompts.PROMPTS_DIR
            coach_prompts.PROMPTS_DIR = pathlib.Path(tmp)
            try:
                (pathlib.Path(tmp) / "dup.md").write_text("## one\nA\n\n## one\nB\n", "utf-8")
                with self.assertRaises(coach_prompts.PromptError):
                    coach_prompts.fragments("dup")
            finally:
                coach_prompts.PROMPTS_DIR = saved

    def test_fragment_keeps_leading_space(self):
        blocks = coach_prompts.fragments("user_blocks")
        self.assertTrue(blocks["deload_week_label"].startswith(" — "))
        self.assertEqual(blocks["report_deload_no"], ".")

    def test_every_signal_text_is_used(self):
        """Текст баннера, объявленный и не вызванный, — мёртвый копирайт."""
        import re

        source = (support.MINIAPP_DIR / "trainer" / "domain" / "coach_signals.py").read_text(
            "utf-8"
        )
        used = set(re.findall(r'_text\(\s*\n?\s*"([a-z_]+)"', source))
        declared = set(coach_prompts.fragments("signals", directory=coach_prompts.COPY_DIR))
        self.assertEqual(declared - used, set(), "объявлены, но не используются")
        self.assertEqual(used - declared, set(), "используются, но не объявлены")

    def test_sections_are_sliced_by_heading_not_by_number(self):
        """Атлет перенумеровывает разделы, правя документ: срез по «## 4.»
        начал бы молча отдавать не ту главу."""
        doc = "## 7. Прогрессия\nтело П\n\n## 8. Тренировочные дни\nтело Т\n"
        body, missing = coach_prompts.document_sections(doc, ["Тренировочные дни", "Прогрессия"])
        self.assertEqual(missing, [])
        self.assertLess(body.index("тело Т"), body.index("тело П"))  # порядок запроса

    def test_missing_section_is_reported_not_swallowed(self):
        body, missing = coach_prompts.document_sections(
            "## 1. Прогрессия\nтело\n", ["Прогрессия", "Тренировочные дни"]
        )
        self.assertEqual(missing, ["Тренировочные дни"])
        self.assertIn("тело", body)

    def test_program_slot_is_empty_without_a_strategy(self):
        """Секция появляется целиком или не появляется вовсе."""
        self.assertEqual(prompt_builder._render_program(None), "")
        self.assertEqual(prompt_builder._render_program(""), "")

    def test_program_warns_about_renamed_sections(self):
        rendered = prompt_builder._render_program("## 1. Прогрессия\nтело\n")
        self.assertIn("не найдены разделы", rendered)
        self.assertIn("Тренировочные дни", rendered)
        self.assertIn("тело", rendered)


class BuiltPromptTests(unittest.TestCase):
    """Собранный системный промпт на настоящем каталоге."""

    def test_built_system_prompt_has_no_unfilled_slots(self):
        catalog = files.load_catalog(CATALOG_PATH)
        prompt = prompt_builder._build_system_prompt(catalog)
        self.assertNotIn("{{", prompt)
        self.assertIn("=== ТРЕНАЖЁРЫ (каталог) ===", prompt)
        self.assertIn("=== ФАЗЫ ПОДГОТОВКИ ===", prompt)


if __name__ == "__main__":
    unittest.main()
