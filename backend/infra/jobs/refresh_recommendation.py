#!/usr/bin/env python3
"""Keep the coach recommendation fresh: regenerate it when it's older than
REFRESH_MAX_AGE_HOURS (default 24).

Run by a systemd timer every morning (infra/deploy/trainer-recommend-refresh.timer),
so the "когда идти" advice in the card is dated today, even if the athlete
hasn't trained for a while. Standalone on purpose — no HTTP, no server import:
reads the same env (EnvironmentFile=/etc/trainer-miniapp/backend.env), talks to
SQLite and the Claude API directly via backend_store + recommender.

Usage:
    python3 refresh_recommendation.py            # regenerate only if stale
    python3 refresh_recommendation.py --force    # regenerate unconditionally
"""

from __future__ import annotations

import argparse
import os
import sys
import time
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
PROFILE_PATH = Path(os.getenv("COACH_PROFILE_PATH", str(DB_PATH.parent / "coach_profile.json")))
STRATEGY_PATH = Path(os.getenv("COACH_STRATEGY_PATH", str(DB_PATH.parent / "coach_strategy.md")))
STATE_PATH = files.default_state_path(DB_PATH)
USER_ID = 3  # personal-build: единственный атлет, см. CLAUDE.md
MAX_AGE_HOURS = float(os.getenv("REFRESH_MAX_AGE_HOURS", "24"))


def run(store: backend_store.MiniAppStore, user_id: int, force: bool = False) -> bool:
    """Returns True if a regeneration was performed (successfully or not)."""
    rec = store.get_recommendation(user_id)
    refresh, reason = (
        (True, "форсировано (--force)")
        if force
        else recommender.should_refresh(rec, int(time.time()), MAX_AGE_HOURS)
    )
    print(f"[refresh] user {user_id}: {reason}")
    if not refresh:
        return False

    workouts = store.list_workouts(user_id)
    if not workouts:
        print("[refresh] нет тренировок — нечего рекомендовать")
        return False

    based_on = store.get_latest_workout_id(user_id)
    body_weights = store.list_body_weights(user_id)
    store.set_recommendation_pending(user_id)
    try:
        catalog = files.load_catalog(CATALOG_PATH)
        recommendation, usage, model = recommender.generate(
            workouts,
            body_weights,
            catalog,
            profile=files.load_profile(PROFILE_PATH),
            strategy=files.load_strategy(STRATEGY_PATH),
            state=files.load_state(STATE_PATH),
            waists=store.list_waists(user_id),
            events=store.list_events(user_id),
        )
    except recommender.RecommendationError as exc:
        store.fail_recommendation(user_id, str(exc))
        print(f"[refresh] ошибка генерации: {exc}")
        return True
    except Exception as exc:  # noqa: BLE001
        store.fail_recommendation(user_id, "Внутренняя ошибка генерации рекомендации")
        print(f"[refresh] внутренняя ошибка: {exc}")
        return True

    store.save_recommendation(
        user_id,
        based_on,
        len(workouts),
        model,
        recommendation,
        usage.get("input_tokens"),
        usage.get("output_tokens"),
    )
    print(
        f"[refresh] обновлено: {recommendation.get('focus', '')!r} "
        f"({usage.get('input_tokens')} in / {usage.get('output_tokens')} out, {model})"
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the coach recommendation if stale")
    parser.add_argument("--force", action="store_true", help="regenerate unconditionally")
    args = parser.parse_args()

    store = backend_store.MiniAppStore(DB_PATH)
    run(store, USER_ID, force=args.force)


if __name__ == "__main__":
    main()
