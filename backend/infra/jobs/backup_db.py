#!/usr/bin/env python3
"""Консистентный бэкап trainer.db без зависимостей, с ротацией.

Использует online-backup API SQLite (``Connection.backup``): дамп транзакционно
целостный, даже пока backend пишет, — не нужны ни sqlite3 CLI, ни остановка
сервиса. Результат сжат gzip; профиль атлета, стратегия и состояние подготовки
(лежат рядом с базой и в git не попадают) копируются рядом, чтобы один каталог
бэкапа восстанавливал всё.

Запускает infra/deploy/trainer-db-backup.timer (ежедневно). Хранит новейшие
BACKUP_KEEP бэкапов, старые удаляет.

    python3 backup_db.py                  # бэкап + ротация
    BACKUP_DIR=/mnt/x python3 backup_db.py
"""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
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


def companion_path(dest_dir: Path, prefix: str, stamp: str) -> Path:
    """Путь копии спутника базы (профиль, стратегия, состояние) под штампом её бэкапа.

    Имя копии задано только здесь: по нему main() её создаёт, а rotate() находит
    и удаляет вместе с бэкапом базы того же штампа.
    """
    return dest_dir / f"{prefix}-{stamp}.json"


def make_backup(db_path: Path, dest_dir: Path, stamp: str) -> Path:
    """Online-бэкап ``db_path`` в ``dest_dir/trainer-<stamp>.db.gz``; возвращает путь к архиву."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    snapshot = dest_dir / f"{DB_PREFIX}{stamp}.db"
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst)  # целостно даже при параллельной записи
        finally:
            dst.close()
    finally:
        src.close()

    gz_path = snapshot.with_suffix(".db.gz")
    with open(snapshot, "rb") as raw, gzip.open(gz_path, "wb") as gz:
        shutil.copyfileobj(raw, gz)
    snapshot.unlink()
    return gz_path


def rotate(dest_dir: Path, keep: int, companion_prefixes: tuple[str, ...] = ()) -> list[Path]:
    """Удалить все бэкапы базы, кроме ``keep`` новейших, и копии-спутники под их штампами.

    Спутник уходит только вслед за своим бэкапом базы: у комплекта один штамп, и
    удаляется он целиком. Возвращает удалённые пути бэкапов базы.
    """
    backups = sorted(
        dest_dir.glob(f"{DB_PREFIX}*{DB_SUFFIX}"),
        key=lambda p: p.name,
        reverse=True,
    )
    removed = backups[keep:]
    for path in removed:
        path.unlink()
        stamp = path.name[len(DB_PREFIX) : -len(DB_SUFFIX)]
        for prefix in companion_prefixes:
            companion_path(dest_dir, prefix, stamp).unlink(missing_ok=True)
    return removed


def main() -> None:
    """Бэкап базы, копии профиля, стратегии и состояния с тем же штампом, ротация;
    без базы — код выхода 1.
    """
    if not DB_PATH.exists():
        print(f"[backup] база не найдена: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    # timezone.utc, а не datetime.UTC: на VPS системный Python 3.10, где UTC ещё
    # нет, и таймер падал бы на импорте (так и было три недели в августе 2026).
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    gz_path = make_backup(DB_PATH, BACKUP_DIR, stamp)
    size_kb = gz_path.stat().st_size / 1024
    print(f"[backup] создан {gz_path.name} ({size_kb:.0f} КБ)")

    # Профиль, стратегия и состояние маленькие и не в git — снимаем их рядом с
    # бэкапом базы, чтобы один каталог восстанавливал всё.
    companions = (
        (PROFILE_PATH, "coach_profile", "профиль"),
        (STRATEGY_PATH, "coach_strategy", "стратегия"),
        (STATE_PATH, "coach_state", "состояние"),
    )
    for source, prefix, label in companions:
        if source.exists():
            copy = companion_path(BACKUP_DIR, prefix, stamp)
            shutil.copy2(source, copy)
            print(f"[backup] {label} сохранён {copy.name}")

    # Префиксы для ротации — из того же кортежа, что и снапшот: второй список,
    # переписанный руками, однажды разошёлся с первым, и копии стратегии не ротировались.
    prefixes = tuple(prefix for _source, prefix, _label in companions)
    removed = rotate(BACKUP_DIR, KEEP, prefixes)
    if removed:
        print(f"[backup] удалено старых: {len(removed)} (оставляем {KEEP})")


if __name__ == "__main__":
    main()
