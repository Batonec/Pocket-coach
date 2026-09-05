#!/usr/bin/env python3
"""Consistent, dependency-free backup of trainer.db with rotation.

Uses the SQLite online-backup API (``Connection.backup``) so the dump is
transactionally consistent even while the backend is writing — no need for the
sqlite3 CLI or a service stop. The result is gzip-compressed, and the athlete
profile (which lives next to the DB and is NOT in git) is copied alongside so a
single backup directory restores everything.

Run by infra/deploy/trainer-db-backup.timer (daily). Keeps the newest
BACKUP_KEEP backups and deletes older ones.

    python3 backup_db.py                  # backup + rotate
    BACKUP_DIR=/mnt/x python3 backup_db.py
"""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]  # backend/: там data/ и resources/
DB_PATH = Path(os.getenv("MINIAPP_DB_PATH", str(BASE_DIR / "data" / "trainer.db")))
PROFILE_PATH = Path(os.getenv("COACH_PROFILE_PATH", str(DB_PATH.parent / "coach_profile.json")))
STRATEGY_PATH = Path(os.getenv("COACH_STRATEGY_PATH", str(DB_PATH.parent / "coach_strategy.md")))
STATE_PATH = Path(os.getenv("COACH_STATE_PATH", str(DB_PATH.parent / "coach_state.json")))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(DB_PATH.parent / "backups")))
KEEP = int(os.getenv("BACKUP_KEEP", "14"))

DB_PREFIX = "trainer-"
DB_SUFFIX = ".db.gz"


def make_backup(db_path: Path, dest_dir: Path, stamp: str) -> Path:
    """Online-backup db_path into dest_dir/trainer-<stamp>.db.gz. Returns the path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    snapshot = dest_dir / f"{DB_PREFIX}{stamp}.db"
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst)  # consistent even under concurrent writes
        finally:
            dst.close()
    finally:
        src.close()

    gz_path = snapshot.with_suffix(".db.gz")
    with open(snapshot, "rb") as raw, gzip.open(gz_path, "wb") as gz:
        shutil.copyfileobj(raw, gz)
    snapshot.unlink()
    return gz_path


def rotate(dest_dir: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` db backups. Returns the removed paths."""
    backups = sorted(
        dest_dir.glob(f"{DB_PREFIX}*{DB_SUFFIX}"),
        key=lambda p: p.name,
        reverse=True,
    )
    removed = backups[keep:]
    for path in removed:
        path.unlink()
    return removed


def main() -> None:
    if not DB_PATH.exists():
        print(f"[backup] база не найдена: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    gz_path = make_backup(DB_PATH, BACKUP_DIR, stamp)
    size_kb = gz_path.stat().st_size / 1024
    print(f"[backup] создан {gz_path.name} ({size_kb:.0f} КБ)")

    # The profile and coaching state are small and not in git — snapshot them
    # next to the DB backup so one directory restores everything.
    for source, prefix, label in (
        (PROFILE_PATH, "coach_profile", "профиль"),
        (STRATEGY_PATH, "coach_strategy", "стратегия"),
        (STATE_PATH, "coach_state", "состояние"),
    ):
        if source.exists():
            copy = BACKUP_DIR / f"{prefix}-{stamp}.json"
            shutil.copy2(source, copy)
            print(f"[backup] {label} сохранён {copy.name}")

    removed = rotate(BACKUP_DIR, KEEP)
    # Keep companion copies in lockstep with the db backups we removed.
    for path in removed:
        stamp_part = path.name[len(DB_PREFIX) : -len(DB_SUFFIX)]
        for prefix in ("coach_profile", "coach_state"):
            companion = BACKUP_DIR / f"{prefix}-{stamp_part}.json"
            if companion.exists():
                companion.unlink()
    if removed:
        print(f"[backup] удалено старых: {len(removed)} (оставляем {KEEP})")


if __name__ == "__main__":
    main()
