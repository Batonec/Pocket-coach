from __future__ import annotations

import pathlib
import unittest

import support  # noqa: F401 — adds backend to sys.path
from support import STATIC_DIR

import coach_prompts
import recommender


class PromptTemplateTests(unittest.TestCase):
    def test_system_template_expects_exactly_the_computed_slots(self):
        """The template's slots and what recommender computes must line up.

        A slot added to the markdown without a value in the code would ship
        «{{...}}» straight to the model; a value with no slot would silently
        vanish from the prompt. Both fail loudly instead.
        """
        self.assertEqual(
            coach_prompts.slots(coach_prompts.load("system")),
            {"profile", "catalog", "catalog_gaps", "phase_policy"},
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
        """No str.format under the hood: JSON snippets and ranges in the prose
        must survive without escaping."""
        self.assertEqual(
            coach_prompts.render('{"reps": 12} и {{slot}}', slot="X"),
            '{"reps": 12} и X',
        )


    def test_phase_policy_template_slots_match_the_renderer(self):
        """Every slot in phase_policy.md must be filled by _render_phase_policy
        for any phase — a new number in the prose must not ship as «{{...}}»."""
        import coach_state

        expected = coach_prompts.slots(coach_prompts.load("phase_policy"))
        self.assertEqual(expected, recommender._PHASE_POLICY_SLOTS)
        for phase in coach_state.PHASES:
            state = coach_state.load_state(None)
            state["phase"] = phase
            rendered = recommender._render_phase_policy(state)
            self.assertNotIn("{{", rendered, phase)

    def test_report_prompt_is_loaded_from_the_template(self):
        self.assertEqual(
            recommender.REPORT_SYSTEM_PROMPT, coach_prompts.load("report")
        )
        self.assertNotIn("{{", recommender.REPORT_SYSTEM_PROMPT)


    def test_every_declared_block_is_used_by_the_code(self):
        """Фрагмент, оставшийся в файле без единого вызова _block(), — мёртвый
        текст: его правят, а в промпт он не едет."""
        import re

        source = (support.MINIAPP_DIR / "recommender.py").read_text("utf-8")
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
                (pathlib.Path(tmp) / "dup.md").write_text(
                    "## one\nA\n\n## one\nB\n", "utf-8"
                )
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

        source = (support.MINIAPP_DIR / "coach_signals.py").read_text("utf-8")
        used = set(re.findall(r'_text\(\s*\n?\s*"([a-z_]+)"', source))
        declared = set(
            coach_prompts.fragments("signals", directory=coach_prompts.COPY_DIR)
        )
        self.assertEqual(declared - used, set(), "объявлены, но не используются")
        self.assertEqual(used - declared, set(), "используются, но не объявлены")


class BuiltPromptTests(unittest.TestCase):
    def test_built_system_prompt_has_no_unfilled_slots(self):
        catalog = recommender.load_catalog(STATIC_DIR)
        prompt = recommender._build_system_prompt(catalog)
        self.assertNotIn("{{", prompt)
        self.assertIn("=== ТРЕНАЖЁРЫ (каталог) ===", prompt)
        self.assertIn("=== ФАЗЫ ПОДГОТОВКИ ===", prompt)


if __name__ == "__main__":
    unittest.main()
