#!/usr/bin/env python3
"""Сгенерировать и закэшировать недельный отчёт тренера.

Запускает infra/deploy/trainer-weekly-report.timer в ночь с воскресенья на
понедельник, когда неделя реально закончилась, чтобы атлет открывал в чате
Coach MCP мгновенный отчёт без трат токенов. Автономный, как
refresh_recommendation.py: без HTTP, то же окружение, SQLite и Claude API напрямую.

    python3 weekly_report.py            # сгенерировать, если та неделя не в кэше
    python3 weekly_report.py --force    # пересобрать безусловно
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Скрипт запускается как файл, а не как модуль: корень backend (там пакет
# trainer) в sys.path кладём сами.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trainer.data import backend_store, files
from trainer.domain import recommender

BASE_DIR = Path(__file__).resolve().parents[2]  # backend/: там data/ и resources/
DB_PATH = Path(os.getenv("MINIAPP_DB_PATH", str(BASE_DIR / "data" / "trainer.db")))
CATALOG_PATH = Path(
    os.getenv("EXERCISE_CATALOG_PATH", str(BASE_DIR / "resources" / "exercises.json"))
)
STATE_PATH = files.default_state_path(DB_PATH)
PROFILE_PATH = Path(os.getenv("COACH_PROFILE_PATH", str(DB_PATH.parent / "coach_profile.json")))
STRATEGY_PATH = Path(os.getenv("COACH_STRATEGY_PATH", str(DB_PATH.parent / "coach_strategy.md")))
USER_ID = 3  # personal-build: единственный атлет, см. CLAUDE.md
REPORT_DAYS = int(os.getenv("WEEKLY_REPORT_DAYS", "7"))


def run(
    store: backend_store.MiniAppStore,
    user_id: int,
    force: bool = False,
    today: date | None = None,
) -> bool:
    """Отчёт за закрытую неделю до ``today`` (``recommender.weekly_report_period``):
    пропустить, если он уже в кэше и не ``force``, иначе ``generate_weekly_report``
    и запись в кэш. Возвращает ``True``, если отчёт сгенерирован; ошибка модели —
    в stdout, без записи. Тесты зовут с фейковым стором и явным ``today``.
    """
    period = recommender.weekly_report_period(today or date.today())
    period_end = period.isoformat()
    needed, reason = recommender.weekly_report_needed(
        store.get_coach_report(user_id, period_end, REPORT_DAYS), force=force
    )
    if not needed:
        print(f"[weekly] отчёт за {period_end}: {reason}")
        return False

    # Прошлый отчёт — за период, закрывшийся ровно на REPORT_DAYS раньше: из
    # него в промпт уходит «Фокус следующей недели», чтобы новый начинался с
    # «договаривались о X — как вышло».
    previous = store.get_coach_report(
        user_id, (period - timedelta(days=REPORT_DAYS)).isoformat(), REPORT_DAYS
    )
    try:
        report, usage, model = recommender.generate_weekly_report(
            store.list_workouts(user_id),
            store.list_body_weights(user_id),
            store.list_waists(user_id),
            files.load_catalog(CATALOG_PATH),
            profile=files.load_profile(PROFILE_PATH),
            strategy=files.load_strategy(STRATEGY_PATH),
            state=files.load_state(STATE_PATH),
            events=store.list_events(user_id),
            measurements=store.list_measurements(user_id),
            previous_report=previous["report"] if previous else None,
            today=period,
            days=REPORT_DAYS,
        )
    except recommender.RecommendationError as exc:
        print(f"[weekly] ошибка генерации: {exc}")
        return False

    store.save_coach_report(
        user_id,
        period_end,
        REPORT_DAYS,
        report,
        model,
        usage.get("input_tokens"),
        usage.get("output_tokens"),
    )
    print(
        f"[weekly] отчёт за {period_end} сохранён "
        f"({usage.get('input_tokens')} in / {usage.get('output_tokens')} out, {model})"
    )
    return True


def main() -> None:
    """CLI: ``--force`` пересобирает безусловно; пользователь — единственный атлет ``USER_ID``."""
    parser = argparse.ArgumentParser(description="Generate and cache the weekly coach report")
    parser.add_argument("--force", action="store_true", help="regenerate unconditionally")
    args = parser.parse_args()

    store = backend_store.MiniAppStore(DB_PATH)
    run(store, USER_ID, force=args.force)


if __name__ == "__main__":
    main()
