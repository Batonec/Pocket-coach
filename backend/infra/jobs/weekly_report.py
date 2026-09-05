#!/usr/bin/env python3
"""Generate and cache the coach weekly report.

Run by infra/deploy/trainer-weekly-report.timer in the night from Sunday to Monday,
once the week is actually over, so the athlete opens an instant, token-free
report in the Coach MCP chat. Standalone like refresh_recommendation.py: no
HTTP, reads the same env, talks to SQLite and the Claude API directly.

    python3 weekly_report.py            # generate unless that week is cached
    python3 weekly_report.py --force    # regenerate unconditionally
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

# Скрипт запускается как файл, а не как модуль: корень backend (там пакет
# trainer) в sys.path кладём сами.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trainer import backend_store
from trainer.coach import coach_state, recommender

BASE_DIR = Path(__file__).resolve().parents[2]  # backend/: там data/ и resources/
DB_PATH = Path(os.getenv("MINIAPP_DB_PATH", str(BASE_DIR / "data" / "trainer.db")))
CATALOG_PATH = Path(
    os.getenv("EXERCISE_CATALOG_PATH", str(BASE_DIR / "resources" / "exercises.json"))
)
STATE_PATH = coach_state.default_state_path(DB_PATH)
PROFILE_PATH = Path(os.getenv("COACH_PROFILE_PATH", str(DB_PATH.parent / "coach_profile.json")))
STRATEGY_PATH = Path(os.getenv("COACH_STRATEGY_PATH", str(DB_PATH.parent / "coach_strategy.md")))
USER_ID = int(os.getenv("MINIAPP_TELEGRAM_RECOVERY_USER_ID", "3") or "3")
REPORT_DAYS = int(os.getenv("WEEKLY_REPORT_DAYS", "7"))


def run(
    store: backend_store.MiniAppStore,
    user_id: int,
    force: bool = False,
    today: date | None = None,
) -> bool:
    """Returns True if a report was generated."""
    # Отчёт всегда про ЗАКРЫТУЮ неделю, а не про последние 7 дней: таймер
    # просыпается уже в понедельник, поэтому и период, и окно данных модели
    # якорятся на прошедшее воскресенье, а не на сегодня.
    period = coach_state.last_closed_week_end(today or date.today())
    period_end = period.isoformat()
    if not force and store.get_coach_report(user_id, period_end, REPORT_DAYS):
        print(f"[weekly] отчёт за {period_end} уже в кэше")
        return False

    try:
        report, usage, model = recommender.generate_weekly_report(
            store.list_workouts(user_id),
            store.list_body_weights(user_id),
            store.list_waists(user_id),
            recommender.load_catalog(CATALOG_PATH),
            profile=recommender.load_profile(PROFILE_PATH),
            strategy=recommender.load_strategy(STRATEGY_PATH),
            state=coach_state.load_state(STATE_PATH),
            events=store.list_events(user_id),
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
    parser = argparse.ArgumentParser(description="Generate and cache the weekly coach report")
    parser.add_argument("--force", action="store_true", help="regenerate unconditionally")
    args = parser.parse_args()

    store = backend_store.MiniAppStore(DB_PATH)
    run(store, USER_ID, force=args.force)


if __name__ == "__main__":
    main()
