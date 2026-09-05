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
from typing import Any

# Скрипт запускается как файл, а не как модуль: корень backend (там пакет
# trainer) в sys.path кладём сами.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trainer import backend_store, coach_state, recommender

BASE_DIR = Path(__file__).resolve().parents[2]  # backend/: там data/ и resources/
DB_PATH = Path(os.getenv("MINIAPP_DB_PATH", str(BASE_DIR / "data" / "trainer.db")))
CATALOG_PATH = Path(
    os.getenv("EXERCISE_CATALOG_PATH", str(BASE_DIR / "resources" / "exercises.json"))
)
PROFILE_PATH = Path(os.getenv("COACH_PROFILE_PATH", str(DB_PATH.parent / "coach_profile.json")))
STRATEGY_PATH = Path(os.getenv("COACH_STRATEGY_PATH", str(DB_PATH.parent / "coach_strategy.md")))
STATE_PATH = coach_state.default_state_path(DB_PATH)
USER_ID = int(os.getenv("MINIAPP_TELEGRAM_RECOVERY_USER_ID", "3") or "3")
MAX_AGE_HOURS = float(os.getenv("REFRESH_MAX_AGE_HOURS", "24"))
# A 'pending' row older than this is a generation that died mid-flight
# (e.g. the server was restarted) — safe to take over.
STUCK_PENDING_HOURS = 2.0


def should_refresh(
    rec: dict[str, Any] | None,
    now_ts: int,
    max_age_hours: float = MAX_AGE_HOURS,
) -> tuple[bool, str]:
    """Decide whether the stored recommendation needs regeneration."""
    if rec is None:
        return True, "рекомендации ещё нет"

    age_hours = (now_ts - int(rec.get("updated_at") or 0)) / 3600
    status = rec.get("status")

    if status == "pending":
        if age_hours > STUCK_PENDING_HOURS:
            return True, f"зависший pending ({age_hours:.1f} ч)"
        return False, f"генерация уже идёт ({age_hours:.1f} ч)"
    if status == "failed":
        return True, f"прошлая генерация упала ({age_hours:.1f} ч назад)"
    if status == "ready":
        if age_hours > max_age_hours:
            return True, f"рекомендации {age_hours:.1f} ч (> {max_age_hours:g})"
        return False, f"рекомендация свежая ({age_hours:.1f} ч)"
    return True, f"неожиданный статус: {status!r}"


def run(store: backend_store.MiniAppStore, user_id: int, force: bool = False) -> bool:
    """Returns True if a regeneration was performed (successfully or not)."""
    rec = store.get_recommendation(user_id)
    refresh, reason = (
        (True, "форсировано (--force)") if force else should_refresh(rec, int(time.time()))
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
        catalog = recommender.load_catalog(CATALOG_PATH)
        recommendation, usage, model = recommender.generate(
            workouts,
            body_weights,
            catalog,
            profile=recommender.load_profile(PROFILE_PATH),
            strategy=recommender.load_strategy(STRATEGY_PATH),
            state=coach_state.load_state(STATE_PATH),
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
