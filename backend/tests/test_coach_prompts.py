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
            coach_prompts.slots(coach_prompts.load("next_workout")),
            {"profile", "catalog", "catalog_gaps", "hard_rules", "program"},
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
        """Заголовок «ПРОГРАММА» держит шаблон, поэтому без файла стратегии под ним
        стоит предупреждение, а не пустота."""
        built = prompt_builder._build_report_system_prompt()
        self.assertNotIn("{{", built)
        self.assertIn("=== ПРОГРАММА ===\nПРЕДУПРЕЖДЕНИЕ: документ стратегии не загружен", built)
        self.assertEqual(built.count("=== ПРОГРАММА"), 1)

    def test_report_template_expects_exactly_the_computed_slots(self):
        self.assertEqual(
            coach_prompts.slots(coach_prompts.load("weekly_report")),
            {"profile", "program"},
        )

    def test_header_comment_is_stripped_on_load(self):
        """Шапка «<!-- … -->» в начале файла — для человека: у цельного шаблона без
        среза она уехала бы в системный промпт, а слот, упомянутый в ней, стал бы
        «незаполненным»."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            saved = coach_prompts.PROMPTS_DIR
            coach_prompts.PROMPTS_DIR = pathlib.Path(tmp)
            try:
                (pathlib.Path(tmp) / "capped.md").write_text(
                    "<!--\nШапка про {{slot}} и <!-- вложенную стрелку.\n-->\n\nТекст {{one}}.\n",
                    "utf-8",
                )
                loaded = coach_prompts.load("capped")
            finally:
                coach_prompts.PROMPTS_DIR = saved
        self.assertEqual(loaded, "Текст {{one}}.\n")
        self.assertEqual(coach_prompts.slots(loaded), {"one"})
        # Комментарий не в начале файла — обычный текст, срезать его не просили.
        self.assertIn("<!--", coach_prompts._HEADER_RE.sub("", "Текст\n<!-- x -->\n", count=1))

    def test_report_header_does_not_reach_the_model(self):
        """weekly_report.md несёт шапку для человека; собранный промпт начинается с роли."""
        self.assertTrue(
            (coach_prompts.PROMPTS_DIR / "weekly_report.md").read_text("utf-8").startswith("<!--")
        )
        built = prompt_builder._build_report_system_prompt()
        self.assertNotIn("<!--", built)
        self.assertTrue(built.startswith("Ты — персональный фитнес-тренер"))

    def test_plan_header_does_not_reach_the_model(self):
        """next_workout.md несёт такую же шапку для человека; собранный промпт плана
        начинается с роли, а не с комментария."""
        self.assertTrue(
            (coach_prompts.PROMPTS_DIR / "next_workout.md").read_text("utf-8").startswith("<!--")
        )
        built = prompt_builder._build_system_prompt([])
        self.assertNotIn("<!--", built)
        self.assertTrue(built.startswith("Ты — персональный силовой тренер"))

    def test_report_states_its_general_task_and_asks_the_four_course_questions(self):
        """Генеральная задача отчёта — курс к долгосрочной цели, а не только неделя:
        роль называет её, а блок «Курс к цели» задаёт четыре вопроса атлета."""
        built = prompt_builder._build_report_system_prompt()
        self.assertIn("где он на пути к своей долгосрочной цели", built)
        self.assertIn("**Курс к цели**", built)
        for question in (
            "где он на пути к цели",
            "в графике ли стратегии",
            "всё ли хорошо",
            "надо ли что-то корректировать",
        ):
            self.assertIn(question, built)
        self.assertIn("ПО ПЛАНУ / ОТСТАЁТ / ОПЕРЕЖАЕТ", built)
        # Все семь блоков формата известны парсеру фокуса, иначе новый блок после
        # «Фокуса» уехал бы в промпт как его часть.
        for name in prompt_builder._REPORT_BLOCKS:
            self.assertIn(f"**{name[0].upper()}{name[1:]}**", built)

    def test_every_declared_block_is_used_by_the_code(self):
        """Фрагмент, оставшийся в файле без единого вызова _block(), — мёртвый
        текст: его правят, а в промпт он не едет."""
        import re

        source = "".join(
            (support.MINIAPP_DIR / "trainer" / "domain" / name).read_text("utf-8")
            for name in ("prompt_builder.py", "recommender.py")
        )
        used = set(re.findall(r'_block\(\s*\n?\s*"([a-z_]+)"', source))
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
        self.assertTrue(blocks["report_deload_label"].startswith(" — "))
        self.assertTrue(blocks["support_week_label"].startswith(" — "))

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

    def test_program_slot_warns_without_a_strategy(self):
        """Заголовок секции — в шаблоне, значит, слот не может быть пустым."""
        for absent in (None, ""):
            self.assertIn("документ стратегии не загружен", prompt_builder._render_program(absent))
        # Со стратегией заголовка внутри слота нет: его печатает шаблон.
        self.assertNotIn("===", prompt_builder._render_program("## 1. Прогрессия\nтело\n"))

    def test_program_warns_about_renamed_sections(self):
        rendered = prompt_builder._render_program("## 1. Прогрессия\nтело\n")
        self.assertIn("не найдены разделы", rendered)
        self.assertIn("Тренировочные дни", rendered)
        self.assertIn("тело", rendered)

    def test_report_slices_nutrition_and_measurements_instead_of_catalog_gap(self):
        """Блок «Вес и талия» пишется по главам «Питание» и «Измерения», которых
        план не читает; «Пробел каталога» — про состав сессии, отчёту не нужен."""
        doc = (
            "## 6. Пробел каталога и зачем в плане ноги\nпро ноги\n\n"
            "## 7. Питание\nконтур коррекции\n\n"
            "## 8. Измерения\nпротокол ленты\n"
        )
        report = prompt_builder._build_report_system_prompt(strategy=doc)
        self.assertIn("контур коррекции", report)
        self.assertIn("протокол ленты", report)
        self.assertNotIn("про ноги", report)
        plan = prompt_builder._render_program(doc)
        self.assertIn("про ноги", plan)
        self.assertNotIn("контур коррекции", plan)

    def test_section_lists_match_the_strategy_document_when_it_is_at_hand(self):
        """Оба списка — ручная синхронизация с заголовками документа. Документ
        личный и в репозиторий не попадает (CLAUDE.md: «Персональные данные не в
        репозитории»), поэтому проверка живёт только там, где он есть, — на
        ноутбуке атлета; в CI она пропускается, а не падает."""
        path = support.ROOT_DIR / "vision" / "STRATEGY.md"
        if not path.exists():
            self.skipTest("vision/STRATEGY.md есть только на ноутбуке атлета")
        strategy = path.read_text("utf-8")
        for sections in (
            prompt_builder.STRATEGY_SECTIONS,
            prompt_builder.REPORT_STRATEGY_SECTIONS,
        ):
            _body, missing = coach_prompts.document_sections(strategy, sections)
            self.assertEqual(missing, [])


class BuiltPromptTests(unittest.TestCase):
    """Собранный системный промпт на настоящем каталоге."""

    def test_built_system_prompt_has_no_unfilled_slots(self):
        catalog = files.load_catalog(CATALOG_PATH)
        prompt = prompt_builder._build_system_prompt(catalog)
        self.assertNotIn("{{", prompt)
        self.assertIn("=== ТРЕНАЖЁРЫ (каталог) ===", prompt)
        # Машинное имя фазы модели не показывается: она читает название этапа
        # стратегии в КОНТЕКСТЕ user-промпта, а семь фаз — в срезе ПРОГРАММЫ.
        self.assertNotIn("cut_recomp", prompt)
        self.assertNotIn("ФАЗЫ ПОДГОТОВКИ", prompt)
        self.assertEqual(prompt.count("=== ПРОГРАММА"), 1)
        with_doc = prompt_builder._build_system_prompt(
            catalog, strategy="## 4. Тренировочные дни\nкаркас\n"
        )
        self.assertEqual(with_doc.count("=== ПРОГРАММА"), 1)
        self.assertIn("=== ПРОГРАММА ===\nНиже — куски рабочего документа", with_doc)


if __name__ == "__main__":
    unittest.main()
