"""Скрипт бэкапа: gzip-дамп восстанавливается в рабочую базу, ротация оставляет N новейших."""

from __future__ import annotations

import gzip
import sqlite3
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (кладёт backend/ в sys.path)

from infra.jobs import backup_db


class BackupDbTests(unittest.TestCase):
    """Бэкап и ротация на временной базе из одной таблицы."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "trainer.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('hello')")
        conn.commit()
        conn.close()
        self.dest = self.root / "backups"

    def test_make_backup_is_a_valid_gzipped_db(self) -> None:
        gz = backup_db.make_backup(self.db, self.dest, "20260612-040000")
        self.assertTrue(gz.exists())
        self.assertTrue(gz.name.endswith(".db.gz"))

        restored = self.root / "restored.db"
        with gzip.open(gz, "rb") as src, open(restored, "wb") as out:
            out.write(src.read())
        conn = sqlite3.connect(restored)
        self.assertEqual(conn.execute("SELECT v FROM t").fetchone()[0], "hello")
        conn.close()

    def test_rotate_keeps_newest_n(self) -> None:
        for stamp in ("20260101-040000", "20260102-040000", "20260103-040000"):
            backup_db.make_backup(self.db, self.dest, stamp)
        removed = backup_db.rotate(self.dest, keep=2)
        names = sorted(p.name for p in self.dest.glob("trainer-*.db.gz"))
        self.assertEqual(len(names), 2)
        self.assertEqual(names, ["trainer-20260102-040000.db.gz", "trainer-20260103-040000.db.gz"])
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0].name, "trainer-20260101-040000.db.gz")

    def test_rotate_noop_when_under_limit(self) -> None:
        backup_db.make_backup(self.db, self.dest, "20260101-040000")
        self.assertEqual(backup_db.rotate(self.dest, keep=14), [])

    def test_rotate_removes_companions_with_their_db_backup(self) -> None:
        # Имена копий — как их пишет main(): за ними файлы, уже лежащие на VPS.
        prefixes = ("coach_profile", "coach_strategy", "coach_state")
        old, new = "20260101-040000", "20260102-040000"
        for stamp in (old, new):
            backup_db.make_backup(self.db, self.dest, stamp)
            for prefix in prefixes:
                (self.dest / f"{prefix}-{stamp}.json").write_text("{}", encoding="utf-8")

        removed = backup_db.rotate(self.dest, keep=1, companion_prefixes=prefixes)

        self.assertEqual([p.name for p in removed], [f"trainer-{old}.db.gz"])
        self.assertEqual(
            sorted(p.name for p in self.dest.iterdir()),
            sorted([f"trainer-{new}.db.gz", *(f"{prefix}-{new}.json" for prefix in prefixes)]),
        )

    def test_rotate_tolerates_a_missing_companion(self) -> None:
        # Спутник мог не существовать в день снапшота (main() копирует только то, что есть).
        old, new = "20260101-040000", "20260102-040000"
        for stamp in (old, new):
            backup_db.make_backup(self.db, self.dest, stamp)
        (self.dest / f"coach_profile-{old}.json").write_text("{}", encoding="utf-8")

        removed = backup_db.rotate(
            self.dest, keep=1, companion_prefixes=("coach_profile", "coach_strategy")
        )

        self.assertEqual([p.name for p in removed], [f"trainer-{old}.db.gz"])
        self.assertEqual([p.name for p in self.dest.iterdir()], [f"trainer-{new}.db.gz"])


if __name__ == "__main__":
    unittest.main()
