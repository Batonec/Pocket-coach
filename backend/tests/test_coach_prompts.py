from __future__ import annotations

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


class BuiltPromptTests(unittest.TestCase):
    def test_built_system_prompt_has_no_unfilled_slots(self):
        catalog = recommender.load_catalog(STATIC_DIR)
        prompt = recommender._build_system_prompt(catalog)
        self.assertNotIn("{{", prompt)
        self.assertIn("=== ТРЕНАЖЁРЫ (каталог) ===", prompt)
        self.assertIn("=== ФАЗЫ ПОДГОТОВКИ ===", prompt)


if __name__ == "__main__":
    unittest.main()
