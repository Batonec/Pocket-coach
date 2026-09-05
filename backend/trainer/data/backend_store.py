from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from typing import Any

from trainer.domain import rules


def utc_now() -> int:
    return int(time.time())


class MiniAppStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # Recommendations are written from a background thread; wait instead of
        # immediately failing with 'database is locked' on concurrent writes.
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> sqlite3.Connection:
        with closing(self._connect()) as connection:
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _initialize_schema(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER UNIQUE,
                    auth_source TEXT NOT NULL,
                    debug_alias TEXT UNIQUE,
                    username TEXT,
                    first_name TEXT NOT NULL,
                    last_name TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workouts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    client_id TEXT,
                    workout_date TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(user_id, client_id)
                );

                CREATE INDEX IF NOT EXISTS idx_workouts_user_date
                ON workouts(user_id, workout_date DESC, id DESC);

                CREATE TABLE IF NOT EXISTS body_weights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    entry_date TEXT NOT NULL,
                    weight REAL NOT NULL,
                    notes TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(user_id, entry_date)
                );

                CREATE INDEX IF NOT EXISTS idx_body_weights_user_date
                ON body_weights(user_id, entry_date ASC, id ASC);

                -- Weekly waist measurements (cm): the second body-composition
                -- metric next to weight; feeds the coach nutrition matrix.
                CREATE TABLE IF NOT EXISTS waists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    entry_date TEXT NOT NULL,
                    waist REAL NOT NULL,
                    notes TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(user_id, entry_date)
                );

                CREATE INDEX IF NOT EXISTS idx_waists_user_date
                ON waists(user_id, entry_date ASC, id ASC);

                -- Периоды без тренировок с причиной («болел», «командировка»):
                -- текст для промпта тренера, из событий сознательно не строится
                -- ни одного числа. end_date NULL = событие идёт прямо сейчас.
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    start_date TEXT NOT NULL,
                    end_date TEXT,
                    text TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_events_user_date
                ON events(user_id, start_date DESC, id DESC);

                -- Cached coach weekly reports: generated once per closed week
                -- (by the Monday-midnight timer or on demand), then served
                -- instantly and token-free.
                CREATE TABLE IF NOT EXISTS coach_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    period_end TEXT NOT NULL,
                    days INTEGER NOT NULL,
                    report TEXT NOT NULL,
                    model TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    created_at INTEGER NOT NULL,
                    read_at INTEGER,
                    UNIQUE(user_id, period_end, days)
                );

                -- Snoozed coach signals (the История banner): one row per
                -- dismissed episode. snooze_until NULL = hidden while that
                -- exact instance_key (state episode) lasts.
                CREATE TABLE IF NOT EXISTS signal_snoozes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    instance_key TEXT NOT NULL,
                    snooze_until INTEGER,
                    created_at INTEGER NOT NULL,
                    UNIQUE(user_id, instance_key)
                );

                CREATE TABLE IF NOT EXISTS recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    based_on_workout_id INTEGER,
                    based_on_workout_count INTEGER,
                    model TEXT,
                    payload_json TEXT,
                    error TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(user_id)
                );

                -- Append-only journal of every generation (the table above keeps
                -- only the current cached row). Feeds future stats — token spend
                -- over time, плановая дисциплина — and gives a debugging trail.
                CREATE TABLE IF NOT EXISTS recommendation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    based_on_workout_id INTEGER,
                    based_on_workout_count INTEGER,
                    model TEXT,
                    payload_json TEXT,
                    error TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_recommendation_log_user
                ON recommendation_log(user_id, id DESC);
                """
            )
            # Additive migration for DBs created before read_at existed
            # (CREATE IF NOT EXISTS cannot add a column to an existing table).
            with suppress(sqlite3.OperationalError):
                connection.execute("ALTER TABLE coach_reports ADD COLUMN read_at INTEGER")

    def ensure_debug_user(
        self, alias: str, first_name: str = "Browser", last_name: str = "Debug"
    ) -> dict[str, Any]:
        timestamp = utc_now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE debug_alias = ?",
                (alias,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO users (
                        telegram_user_id,
                        auth_source,
                        debug_alias,
                        username,
                        first_name,
                        last_name,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (None, "debug", alias, None, first_name, last_name, timestamp, timestamp),
                )
                row = connection.execute(
                    "SELECT * FROM users WHERE debug_alias = ?",
                    (alias,),
                ).fetchone()

        if row is None:
            raise RuntimeError("Failed to create debug user")
        return self._serialize_user(row)

    def upsert_telegram_user(
        self,
        telegram_user: dict[str, Any],
        auth_source: str = "telegram",
    ) -> dict[str, Any]:
        telegram_user_id = telegram_user.get("id")
        if isinstance(telegram_user_id, str) and telegram_user_id.isdigit():
            telegram_user_id = int(telegram_user_id)
        if not isinstance(telegram_user_id, int):
            raise ValueError("Telegram user id is missing in initData")

        timestamp = utc_now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()

            username = str(telegram_user.get("username") or "").strip() or None
            first_name = str(telegram_user.get("first_name") or "").strip() or "Telegram"
            last_name = str(telegram_user.get("last_name") or "").strip() or None
            values = (
                username,
                first_name,
                last_name,
                timestamp,
            )

            if row is None:
                connection.execute(
                    """
                    INSERT INTO users (
                        telegram_user_id,
                        auth_source,
                        debug_alias,
                        username,
                        first_name,
                        last_name,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        telegram_user_id,
                        auth_source,
                        None,
                        values[0],
                        values[1],
                        values[2],
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE users
                    SET auth_source = ?, username = ?, first_name = ?, last_name = ?, updated_at = ?
                    WHERE telegram_user_id = ?
                    """,
                    (auth_source, *values, telegram_user_id),
                )

            row = connection.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()

        if row is None:
            raise RuntimeError("Failed to upsert Telegram user")
        return self._serialize_user(row)

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return self._serialize_user(row) if row is not None else None

    def list_workouts(self, user_id: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, client_id, workout_date, payload_json, created_at, updated_at
                FROM workouts
                WHERE user_id = ?
                ORDER BY workout_date DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._deserialize_workout(row) for row in rows]

    def get_workout_by_id(self, user_id: int, workout_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = self._get_workout_row(connection, user_id, workout_id)
        return self._deserialize_workout(row) if row is not None else None

    def save_workout(self, user_id: int, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        normalized_payload, client_id = rules.normalize_workout_payload(payload)
        timestamp = utc_now()

        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT id, user_id, client_id, workout_date, payload_json, created_at, updated_at
                FROM workouts
                WHERE user_id = ? AND client_id = ?
                """,
                (user_id, client_id),
            ).fetchone()

            if existing is not None:
                # Ретрай с тем же client_id: что дописать в сохранённую запись,
                # решает rules.retry_backfills_snapshot.
                patched = rules.retry_backfills_snapshot(
                    json.loads(existing["payload_json"]), normalized_payload
                )
                if patched is not None:
                    connection.execute(
                        "UPDATE workouts SET payload_json = ?, updated_at = ? WHERE id = ?",
                        (
                            json.dumps(patched, ensure_ascii=False),
                            timestamp,
                            existing["id"],
                        ),
                    )
                    existing = connection.execute(
                        """
                        SELECT id, user_id, client_id, workout_date, payload_json, created_at, updated_at
                        FROM workouts
                        WHERE id = ?
                        """,
                        (existing["id"],),
                    ).fetchone()
                return self._deserialize_workout(existing), False

            cursor = connection.execute(
                """
                INSERT INTO workouts (
                    user_id,
                    client_id,
                    workout_date,
                    payload_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    client_id,
                    normalized_payload["workout_date"],
                    json.dumps(normalized_payload, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )

            row = connection.execute(
                """
                SELECT id, user_id, client_id, workout_date, payload_json, created_at, updated_at
                FROM workouts
                WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()

        if row is None:
            raise RuntimeError("Failed to persist workout")
        return self._deserialize_workout(row), True

    def update_workout(
        self,
        user_id: int,
        workout_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        normalized_payload, normalized_client_id = rules.normalize_workout_payload(payload)
        timestamp = utc_now()

        with self._connection() as connection:
            existing = self._get_workout_row(connection, user_id, workout_id)
            if existing is None:
                return None

            normalized_payload = rules.edit_keeps_snapshot(
                json.loads(existing["payload_json"]), normalized_payload
            )

            resolved_client_id = existing["client_id"] or normalized_client_id
            connection.execute(
                """
                UPDATE workouts
                SET client_id = ?, workout_date = ?, payload_json = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    resolved_client_id,
                    normalized_payload["workout_date"],
                    json.dumps(normalized_payload, ensure_ascii=False),
                    timestamp,
                    workout_id,
                    user_id,
                ),
            )

            row = self._get_workout_row(connection, user_id, workout_id)

        if row is None:
            raise RuntimeError("Failed to update workout")
        return self._deserialize_workout(row)

    def delete_workout(self, user_id: int, workout_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            existing = self._get_workout_row(connection, user_id, workout_id)
            if existing is None:
                return None

            connection.execute(
                "DELETE FROM workouts WHERE id = ? AND user_id = ?",
                (workout_id, user_id),
            )

        return self._deserialize_workout(existing)

    def get_latest_workout_id(self, user_id: int) -> int | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM workouts
                WHERE user_id = ?
                ORDER BY workout_date DESC, id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return int(row["id"]) if row is not None else None

    def get_recommendation(self, user_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM recommendations WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return self._deserialize_recommendation(row) if row is not None else None

    def clear_recommendation(self, user_id: int) -> None:
        """Drop the mutable next-workout cache while preserving its audit log."""
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM recommendations WHERE user_id = ?",
                (user_id,),
            )

    def set_recommendation_pending(self, user_id: int) -> None:
        timestamp = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO recommendations (user_id, status, created_at, updated_at)
                VALUES (?, 'pending', ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    status = 'pending',
                    updated_at = excluded.updated_at
                """,
                (user_id, timestamp, timestamp),
            )

    def save_recommendation(
        self,
        user_id: int,
        based_on_workout_id: int | None,
        based_on_workout_count: int,
        model: str,
        recommendation: dict[str, Any],
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> dict[str, Any]:
        timestamp = utc_now()
        payload_json = json.dumps(recommendation, ensure_ascii=False)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO recommendations (
                    user_id, status, based_on_workout_id, based_on_workout_count,
                    model, payload_json, error, input_tokens, output_tokens,
                    created_at, updated_at
                )
                VALUES (?, 'ready', ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    status = 'ready',
                    based_on_workout_id = excluded.based_on_workout_id,
                    based_on_workout_count = excluded.based_on_workout_count,
                    model = excluded.model,
                    payload_json = excluded.payload_json,
                    error = NULL,
                    input_tokens = excluded.input_tokens,
                    output_tokens = excluded.output_tokens,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    based_on_workout_id,
                    based_on_workout_count,
                    model,
                    payload_json,
                    input_tokens,
                    output_tokens,
                    timestamp,
                    timestamp,
                ),
            )
            self._append_recommendation_log(
                connection,
                user_id=user_id,
                status="ready",
                based_on_workout_id=based_on_workout_id,
                based_on_workout_count=based_on_workout_count,
                model=model,
                payload_json=payload_json,
                error=None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                timestamp=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM recommendations WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to persist recommendation")
        return self._deserialize_recommendation(row)

    def fail_recommendation(self, user_id: int, error: str) -> None:
        timestamp = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO recommendations (user_id, status, error, created_at, updated_at)
                VALUES (?, 'failed', ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    status = 'failed',
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (user_id, error[:500], timestamp, timestamp),
            )
            self._append_recommendation_log(
                connection,
                user_id=user_id,
                status="failed",
                based_on_workout_id=None,
                based_on_workout_count=None,
                model=None,
                payload_json=None,
                error=error[:500],
                input_tokens=None,
                output_tokens=None,
                timestamp=timestamp,
            )

    def _append_recommendation_log(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: int,
        status: str,
        based_on_workout_id: int | None,
        based_on_workout_count: int | None,
        model: str | None,
        payload_json: str | None,
        error: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        timestamp: int,
    ) -> None:
        """Append one immutable row to the generation journal (same transaction)."""
        connection.execute(
            """
            INSERT INTO recommendation_log (
                user_id, status, based_on_workout_id, based_on_workout_count,
                model, payload_json, error, input_tokens, output_tokens, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                status,
                based_on_workout_id,
                based_on_workout_count,
                model,
                payload_json,
                error,
                input_tokens,
                output_tokens,
                timestamp,
            ),
        )

    def list_recommendation_log(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """Past generations for a user, newest first (append-only journal)."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM recommendation_log
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [self._deserialize_recommendation(row) for row in rows]

    def _deserialize_recommendation(self, row: sqlite3.Row) -> dict[str, Any]:
        payload_raw = row["payload_json"]
        recommendation = json.loads(payload_raw) if payload_raw else None
        keys = row.keys()
        return {
            "id": row["id"],
            "status": row["status"],
            "based_on_workout_id": row["based_on_workout_id"],
            "based_on_workout_count": row["based_on_workout_count"],
            "model": row["model"],
            "recommendation": recommendation,
            "error": row["error"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "created_at": row["created_at"],
            # The append-only log has no updated_at; fall back to created_at.
            "updated_at": row["updated_at"] if "updated_at" in keys else row["created_at"],
        }

    def list_body_weights(self, user_id: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, entry_date, weight, notes, created_at, updated_at
                FROM body_weights
                WHERE user_id = ?
                ORDER BY entry_date ASC, id ASC
                """,
                (user_id,),
            ).fetchall()
        return [self._deserialize_body_weight(row) for row in rows]

    def save_body_weight(
        self, user_id: int, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        normalized_payload = rules.normalize_body_weight_payload(payload)
        timestamp = utc_now()

        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT id, user_id, entry_date, weight, notes, created_at, updated_at
                FROM body_weights
                WHERE user_id = ? AND entry_date = ?
                """,
                (user_id, normalized_payload["entry_date"]),
            ).fetchone()

            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO body_weights (
                        user_id,
                        entry_date,
                        weight,
                        notes,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        normalized_payload["entry_date"],
                        normalized_payload["weight"],
                        normalized_payload["notes"],
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT id, user_id, entry_date, weight, notes, created_at, updated_at
                    FROM body_weights
                    WHERE id = ?
                    """,
                    (cursor.lastrowid,),
                ).fetchone()
                created = True
            else:
                connection.execute(
                    """
                    UPDATE body_weights
                    SET weight = ?, notes = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        normalized_payload["weight"],
                        normalized_payload["notes"],
                        timestamp,
                        existing["id"],
                        user_id,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT id, user_id, entry_date, weight, notes, created_at, updated_at
                    FROM body_weights
                    WHERE id = ?
                    """,
                    (existing["id"],),
                ).fetchone()
                created = False

        if row is None:
            raise RuntimeError("Failed to persist body weight entry")
        return self._deserialize_body_weight(row), created

    def delete_body_weight(self, user_id: int, entry_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, entry_date, weight, notes, created_at, updated_at
                FROM body_weights
                WHERE id = ? AND user_id = ?
                """,
                (entry_id, user_id),
            ).fetchone()
            if row is None:
                return None

            connection.execute(
                """
                DELETE FROM body_weights
                WHERE id = ? AND user_id = ?
                """,
                (entry_id, user_id),
            )

        return self._deserialize_body_weight(row)

    # --- waist measurements (weekly, cm) ---------------------------------- #
    def list_waists(self, user_id: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, entry_date, waist, notes, created_at, updated_at
                FROM waists
                WHERE user_id = ?
                ORDER BY entry_date ASC, id ASC
                """,
                (user_id,),
            ).fetchall()
        return [self._deserialize_waist(row) for row in rows]

    def save_waist(self, user_id: int, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Upsert by (user, entry_date) — one measurement per day, like weight."""
        normalized_payload = rules.normalize_waist_payload(payload)
        timestamp = utc_now()

        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT id FROM waists
                WHERE user_id = ? AND entry_date = ?
                """,
                (user_id, normalized_payload["entry_date"]),
            ).fetchone()

            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO waists (
                        user_id, entry_date, waist, notes, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        normalized_payload["entry_date"],
                        normalized_payload["waist"],
                        normalized_payload["notes"],
                        timestamp,
                        timestamp,
                    ),
                )
                row_id, created = cursor.lastrowid, True
            else:
                connection.execute(
                    """
                    UPDATE waists
                    SET waist = ?, notes = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        normalized_payload["waist"],
                        normalized_payload["notes"],
                        timestamp,
                        existing["id"],
                        user_id,
                    ),
                )
                row_id, created = existing["id"], False

            row = connection.execute(
                """
                SELECT id, user_id, entry_date, waist, notes, created_at, updated_at
                FROM waists
                WHERE id = ?
                """,
                (row_id,),
            ).fetchone()

        if row is None:
            raise RuntimeError("Failed to persist waist entry")
        return self._deserialize_waist(row), created

    def delete_waist(self, user_id: int, entry_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, entry_date, waist, notes, created_at, updated_at
                FROM waists
                WHERE id = ? AND user_id = ?
                """,
                (entry_id, user_id),
            ).fetchone()
            if row is None:
                return None

            connection.execute(
                "DELETE FROM waists WHERE id = ? AND user_id = ?",
                (entry_id, user_id),
            )

        return self._deserialize_waist(row)

    # --- events: gaps in training with a reason ---------------------------- #
    def list_events(self, user_id: int) -> list[dict[str, Any]]:
        """Новые сверху — событие читают рядом с дыркой, которую оно объясняет."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, start_date, end_date, text, created_at, updated_at
                FROM events
                WHERE user_id = ?
                ORDER BY start_date DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._deserialize_event(row) for row in rows]

    def save_event(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Всегда создаёт запись: ключа апсерта у события нет — событий подряд
        может быть несколько (болезнь → сразу командировка), в том числе с одной
        датой начала."""
        normalized_payload = rules.normalize_event_payload(payload)
        timestamp = utc_now()

        with self._connection() as connection:
            if normalized_payload["end_date"] is None:
                self._reject_second_open_event(connection, user_id)

            cursor = connection.execute(
                """
                INSERT INTO events (
                    user_id, start_date, end_date, text, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    normalized_payload["start_date"],
                    normalized_payload["end_date"],
                    normalized_payload["text"],
                    timestamp,
                    timestamp,
                ),
            )
            row = self._get_event_row(connection, user_id, int(cursor.lastrowid))

        if row is None:
            raise RuntimeError("Failed to persist event")
        return self._deserialize_event(row)

    def update_event(
        self,
        user_id: int,
        event_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        normalized_payload = rules.normalize_event_payload(payload)
        timestamp = utc_now()

        with self._connection() as connection:
            if self._get_event_row(connection, user_id, event_id) is None:
                return None

            if normalized_payload["end_date"] is None:
                self._reject_second_open_event(connection, user_id, exclude_id=event_id)

            connection.execute(
                """
                UPDATE events
                SET start_date = ?, end_date = ?, text = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    normalized_payload["start_date"],
                    normalized_payload["end_date"],
                    normalized_payload["text"],
                    timestamp,
                    event_id,
                    user_id,
                ),
            )
            row = self._get_event_row(connection, user_id, event_id)

        if row is None:
            raise RuntimeError("Failed to update event")
        return self._deserialize_event(row)

    def delete_event(self, user_id: int, event_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = self._get_event_row(connection, user_id, event_id)
            if row is None:
                return None

            connection.execute(
                "DELETE FROM events WHERE id = ? AND user_id = ?",
                (event_id, user_id),
            )

        return self._deserialize_event(row)

    def close_open_event(self, user_id: int, end_date: str) -> dict[str, Any] | None:
        """Закрыть текущее открытое событие; вернуть его или None, если открытых
        нет. Идемпотентен — вызывается на каждой созданной тренировке."""
        closed_on = rules._normalize_event_date(end_date, "end_date")
        timestamp = utc_now()

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, start_date, end_date, text, created_at, updated_at
                FROM events
                WHERE user_id = ? AND end_date IS NULL
                ORDER BY start_date DESC, id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return None

            resolved_end = rules.closed_event_end(row["start_date"], closed_on)
            connection.execute(
                "UPDATE events SET end_date = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (resolved_end, timestamp, row["id"], user_id),
            )
            closed = self._get_event_row(connection, user_id, int(row["id"]))

        if closed is None:
            raise RuntimeError("Failed to close open event")
        return self._deserialize_event(closed)

    def _reject_second_open_event(
        self,
        connection: sqlite3.Connection,
        user_id: int,
        exclude_id: int | None = None,
    ) -> None:
        """Есть ли у пользователя другое открытое событие; само правило «оно
        одно» — в rules.check_single_open_event."""
        # `id IS NOT ?` вместо `id != ?`: SQLite сравнивает с NULL без сюрпризов,
        # поэтому exclude_id=None не требует второй ветки запроса.
        row = connection.execute(
            """
            SELECT id FROM events
            WHERE user_id = ? AND end_date IS NULL AND id IS NOT ?
            LIMIT 1
            """,
            (user_id, exclude_id),
        ).fetchone()
        rules.check_single_open_event(row is not None)

    def _get_event_row(
        self,
        connection: sqlite3.Connection,
        user_id: int,
        event_id: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT id, user_id, start_date, end_date, text, created_at, updated_at
            FROM events
            WHERE id = ? AND user_id = ?
            """,
            (event_id, user_id),
        ).fetchone()

    # --- cached coach reports + token spend -------------------------------- #
    def save_coach_report(
        self,
        user_id: int,
        period_end: str,
        days: int,
        report: str,
        model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> dict[str, Any]:
        timestamp = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO coach_reports (
                    user_id, period_end, days, report, model,
                    input_tokens, output_tokens, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, period_end, days) DO UPDATE SET
                    report = excluded.report,
                    model = excluded.model,
                    input_tokens = excluded.input_tokens,
                    output_tokens = excluded.output_tokens,
                    created_at = excluded.created_at
                """,
                (
                    user_id,
                    period_end,
                    int(days),
                    report,
                    model,
                    input_tokens,
                    output_tokens,
                    timestamp,
                ),
            )
        stored = self.get_coach_report(user_id, period_end, days)
        if stored is None:
            raise RuntimeError("Failed to persist coach report")
        return stored

    def get_latest_coach_report(self, user_id: int, days: int = 7) -> dict[str, Any] | None:
        """The most recent cached weekly report (served by /api/reports/weekly)."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT period_end, days, report, model, input_tokens, output_tokens,
                       created_at, read_at
                FROM coach_reports
                WHERE user_id = ? AND days = ?
                ORDER BY period_end DESC
                LIMIT 1
                """,
                (user_id, int(days)),
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_coach_report_read(self, user_id: int, days: int = 7) -> bool:
        """Server-side read receipt for the latest weekly report — it kills the
        weekly_report_ready signal for every client (iOS, MCP chat) at once."""
        timestamp = utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE coach_reports
                SET read_at = ?
                WHERE id = (
                    SELECT id FROM coach_reports
                    WHERE user_id = ? AND days = ?
                    ORDER BY period_end DESC
                    LIMIT 1
                ) AND read_at IS NULL
                """,
                (timestamp, user_id, int(days)),
            )
        return cursor.rowcount > 0

    # --- coach-signal snoozes (the История banner) ------------------------- #
    def list_signal_snoozes(self, user_id: int) -> dict[str, int | None]:
        """{instance_key: snooze_until | None} — None means an episodic dismiss
        (hidden while that exact state episode lasts)."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT instance_key, snooze_until FROM signal_snoozes WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return {row["instance_key"]: row["snooze_until"] for row in rows}

    def save_signal_snooze(self, user_id: int, instance_key: str, snooze_until: int | None) -> None:
        timestamp = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO signal_snoozes (user_id, instance_key, snooze_until, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, instance_key) DO UPDATE SET
                    snooze_until = excluded.snooze_until,
                    created_at = excluded.created_at
                """,
                (user_id, str(instance_key), snooze_until, timestamp),
            )

    def get_coach_report(self, user_id: int, period_end: str, days: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT period_end, days, report, model, input_tokens, output_tokens,
                       created_at, read_at
                FROM coach_reports
                WHERE user_id = ? AND period_end = ? AND days = ?
                """,
                (user_id, period_end, int(days)),
            ).fetchone()
        return dict(row) if row is not None else None

    def token_spend(self, user_id: int) -> list[dict[str, Any]]:
        """Monthly token totals per source/model — recommendation generations
        (the append-only log) plus cached weekly reports."""
        query = """
            SELECT strftime('%Y-%m', created_at, 'unixepoch') AS month,
                   'recommendation' AS source,
                   model,
                   COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM recommendation_log
            WHERE user_id = ?
            GROUP BY month, model
            UNION ALL
            SELECT strftime('%Y-%m', created_at, 'unixepoch') AS month,
                   'weekly_report' AS source,
                   model,
                   COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM coach_reports
            WHERE user_id = ?
            ORDER BY month DESC, source
        """
        with self._connection() as connection:
            rows = connection.execute(query, (user_id, user_id)).fetchall()
        return [dict(row) for row in rows]

    def _get_workout_row(
        self,
        connection: sqlite3.Connection,
        user_id: int,
        workout_id: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT id, user_id, client_id, workout_date, payload_json, created_at, updated_at
            FROM workouts
            WHERE id = ? AND user_id = ?
            """,
            (workout_id, user_id),
        ).fetchone()

    def _deserialize_workout(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        return {
            "id": row["id"],
            "client_id": row["client_id"],
            "workout_date": payload["workout_date"],
            "plan_id": payload.get("plan_id"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "data": payload["data"],
        }

    def _serialize_user(self, row: sqlite3.Row) -> dict[str, Any]:
        first_name = row["first_name"] or ""
        last_name = row["last_name"] or ""
        display_name = f"{first_name} {last_name}".strip() or "Trainer user"
        return {
            "id": row["id"],
            "auth_source": row["auth_source"],
            "telegram_user_id": row["telegram_user_id"],
            "username": row["username"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "debug_alias": row["debug_alias"],
            "is_default_debug_user": row["auth_source"] == "debug",
            "display_name": display_name,
        }

    def _deserialize_body_weight(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "entry_date": row["entry_date"],
            "weight": float(row["weight"]),
            "notes": row["notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _deserialize_waist(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "entry_date": row["entry_date"],
            "waist": float(row["waist"]),
            "notes": row["notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _deserialize_event(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "text": row["text"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
