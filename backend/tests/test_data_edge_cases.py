from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import support  # noqa: F401 - adds backend/ to sys.path
from support import sample_body_weight_payload, sample_workout_payload

from trainer.data import anthropic_client, backend_store, coach_prompts, files
from trainer.domain import coach_state


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self.body


def _http_error(code: int, detail: bytes = b"detail") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.anthropic.test/messages",
        code,
        "failure",
        None,
        io.BytesIO(detail),
    )


class AnthropicClientEdgeCaseTests(unittest.TestCase):
    def _request_with_response(
        self,
        response: object,
    ) -> tuple[str, dict[str, object]]:
        body = response if isinstance(response, bytes) else json.dumps(response).encode("utf-8")
        with mock.patch.object(
            anthropic_client.urllib.request,
            "urlopen",
            return_value=_Response(body),
        ):
            return anthropic_client._request_model(
                "system",
                "user",
                schema=None,
                model="test-model",
                max_tokens=100,
                api_key="secret",
                timeout=2.5,
                max_retries=0,
            )

    def test_cacheable_messages_does_not_mutate_callers_conversation(self) -> None:
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": [{"type": "text", "text": "already structured"}]},
        ]
        original = json.loads(json.dumps(messages))

        cached = anthropic_client._cacheable_messages(messages)

        self.assertEqual(messages, original)
        self.assertIsNot(cached, messages)
        self.assertEqual(cached[0]["content"][0]["text"], "first")
        self.assertEqual(cached[0]["content"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(cached[1:], messages[1:])

    def test_cacheable_messages_leaves_non_user_first_message_unchanged(self) -> None:
        messages = [
            {"role": "assistant", "content": "prefill"},
            {"role": "user", "content": "question"},
        ]

        self.assertEqual(anthropic_client._cacheable_messages(messages), messages)

    def test_request_sets_transport_metadata_and_defaults_null_usage(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request: object, timeout: float) -> _Response:
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response(
                json.dumps(
                    {
                        "content": [
                            {"type": "thinking", "thinking": "hidden"},
                            {"type": "text", "text": "visible"},
                        ],
                        "usage": None,
                    }
                ).encode("utf-8")
            )

        with mock.patch.object(anthropic_client.urllib.request, "urlopen", fake_urlopen):
            text, usage = anthropic_client._request_model(
                "system",
                "user",
                schema=None,
                model="test-model",
                max_tokens=123,
                api_key="secret",
                timeout=2.5,
                max_retries=0,
            )

        request = captured["request"]
        self.assertEqual(text, "visible")
        self.assertEqual(usage, {})
        self.assertEqual(captured["timeout"], 2.5)
        self.assertEqual(request.full_url, anthropic_client.ANTHROPIC_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-api-key"), "secret")
        self.assertEqual(
            request.get_header("Anthropic-version"),
            anthropic_client.ANTHROPIC_VERSION,
        )

    def test_request_wraps_non_json_api_response(self) -> None:
        with self.assertRaisesRegex(
            anthropic_client.RecommendationError,
            "не-JSON",
        ):
            self._request_with_response(b"not json")

    def test_request_rejects_refusal_before_returning_text(self) -> None:
        with self.assertRaisesRegex(
            anthropic_client.RecommendationError,
            "отказалась",
        ):
            self._request_with_response(
                {
                    "stop_reason": "refusal",
                    "content": [{"type": "text", "text": "should not escape"}],
                }
            )

    def test_request_rejects_missing_or_empty_text_blocks(self) -> None:
        payloads = (
            {},
            {"content": []},
            {"content": [{"type": "thinking", "thinking": "only thought"}]},
            {"content": [{"type": "text", "text": ""}]},
        )
        for payload in payloads:
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(
                    anthropic_client.RecommendationError,
                    "Пустой ответ",
                ),
            ):
                self._request_with_response(payload)

    def test_call_anthropic_parses_json_and_preserves_usage(self) -> None:
        usage = {"input_tokens": 7, "output_tokens": 3}
        with mock.patch.object(
            anthropic_client,
            "_request_model",
            return_value=(' {"focus": "legs"} ', usage),
        ):
            parsed, returned_usage = anthropic_client._call_anthropic(
                "system",
                "user",
                {"type": "object"},
                model="test-model",
                max_tokens=100,
                api_key="secret",
                timeout=1,
            )

        self.assertEqual(parsed, {"focus": "legs"})
        self.assertIs(returned_usage, usage)

    def test_call_anthropic_wraps_invalid_model_json(self) -> None:
        with (
            mock.patch.object(
                anthropic_client,
                "_request_model",
                return_value=("{broken", {}),
            ),
            self.assertRaisesRegex(
                anthropic_client.RecommendationError,
                "невалидный JSON",
            ),
        ):
            anthropic_client._call_anthropic(
                "system",
                "user",
                {"type": "object"},
                model="test-model",
                max_tokens=100,
                api_key="secret",
                timeout=1,
            )

    def test_fetch_distinguishes_direct_and_wrapped_timeouts(self) -> None:
        failures = (
            TimeoutError("direct timeout"),
            urllib.error.URLError(TimeoutError("wrapped timeout")),
        )
        for failure in failures:
            sleeps: list[float] = []
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(
                    anthropic_client.urllib.request,
                    "urlopen",
                    side_effect=failure,
                ) as urlopen,
                self.assertRaisesRegex(
                    anthropic_client.RecommendationError,
                    "не ответил вовремя",
                ),
            ):
                anthropic_client._fetch_anthropic(
                    object(),
                    timeout=1,
                    max_retries=1,
                    backoff=0.25,
                    sleep=sleeps.append,
                )
            self.assertEqual(urlopen.call_count, 2)
            self.assertEqual(sleeps, [0.25])

    def test_fetch_surfaces_non_timeout_connection_reason(self) -> None:
        with (
            mock.patch.object(
                anthropic_client.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("connection reset"),
            ),
            self.assertRaisesRegex(
                anthropic_client.RecommendationError,
                "connection reset",
            ),
        ):
            anthropic_client._fetch_anthropic(
                object(),
                timeout=1,
                max_retries=0,
                backoff=1,
                sleep=lambda _: None,
            )

    def test_http_error_detail_is_replaced_and_capped(self) -> None:
        detail = b"prefix-" + b"x" * 400 + b"\xff"
        with (
            mock.patch.object(
                anthropic_client.urllib.request,
                "urlopen",
                side_effect=_http_error(401, detail),
            ),
            self.assertRaises(anthropic_client.RecommendationError) as context,
        ):
            anthropic_client._fetch_anthropic(
                object(),
                timeout=1,
                max_retries=3,
                backoff=1,
                sleep=lambda _: self.fail("permanent errors must not sleep"),
            )

        message = str(context.exception)
        self.assertIn("401", message)
        self.assertIn("prefix-", message)
        surfaced_detail = message.split(": ", 1)[1]
        self.assertEqual(len(surfaced_detail), 300)


class FileHelperEdgeCaseTests(unittest.TestCase):
    def test_default_state_path_prefers_env_and_otherwise_neighbors_database(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COACH_STATE_PATH", None)
            self.assertEqual(
                files.default_state_path("/srv/trainer/data/trainer.db"),
                Path("/srv/trainer/data/coach_state.json"),
            )
            os.environ["COACH_STATE_PATH"] = "/private/state.json"
            self.assertEqual(
                files.default_state_path("/ignored/db.sqlite"), Path("/private/state.json")
            )

    def test_load_state_without_a_configured_path_returns_fresh_defaults(self) -> None:
        first = files.load_state(None)
        second = files.load_state(None)

        self.assertEqual(first, coach_state.default_state())
        self.assertEqual(second, coach_state.default_state())
        self.assertIsNot(first, second)

    def test_save_state_writes_readable_unicode_json_with_a_final_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = {"phase": "maintenance", "note": "лёгкая неделя"}

            files.save_state(path, state)

            raw = path.read_text("utf-8")
            self.assertTrue(raw.endswith("\n"))
            self.assertIn("лёгкая неделя", raw)
            self.assertEqual(json.loads(raw), state)

    def test_load_catalog_coerces_valid_fields_and_skips_bad_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(
                json.dumps(
                    {
                        "exercises": [
                            {"id": "7", "name": "  Front squat  "},
                            {},
                            {"id": "not-an-int", "name": "Bad"},
                            None,
                            {"id": 8},
                        ]
                    }
                ),
                "utf-8",
            )

            self.assertEqual(files.load_catalog(path), [{"id": 7, "name": "Front squat"}])

    def test_load_catalog_rejects_a_catalog_without_any_valid_items(self) -> None:
        payloads = ({}, {"exercises": []}, {"exercises": [{}, None]})
        for index, payload in enumerate(payloads):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"catalog-{index}.json"
                path.write_text(json.dumps(payload), "utf-8")
                with (
                    self.subTest(payload=payload),
                    self.assertRaisesRegex(
                        anthropic_client.RecommendationError,
                        "Каталог упражнений пуст",
                    ),
                ):
                    files.load_catalog(path)

    def test_load_profile_rejects_non_object_roots_and_non_object_blocks(self) -> None:
        payloads = ([], "text", {"blocks": []}, {"blocks": "profile"}, {"blocks": {}})
        for index, payload in enumerate(payloads):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"profile-{index}.json"
                path.write_text(json.dumps(payload), "utf-8")
                with self.subTest(payload=payload):
                    self.assertIsNone(files.load_profile(path))

    def test_update_profile_rejects_invalid_structure_and_blank_block_name(self) -> None:
        payloads = ([], {"schema": 1}, {"blocks": []})
        for index, payload in enumerate(payloads):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"profile-{index}.json"
                path.write_text(json.dumps(payload), "utf-8")
                with (
                    self.subTest(payload=payload),
                    self.assertRaisesRegex(
                        anthropic_client.RecommendationError,
                        "без blocks",
                    ),
                ):
                    files.update_profile_block(path, "Цель", "текст")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text('{"blocks":{"Цель":"текст"}}', "utf-8")
            with self.assertRaisesRegex(
                anthropic_client.RecommendationError,
                "имя блока",
            ):
                files.update_profile_block(path, "   ", "новый текст")

    def test_update_profile_trims_names_and_text_and_none_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text(
                '{"schema":1,"blocks":{"Цель":"старая","Убрать":"лишнее"}}',
                "utf-8",
            )

            updated = files.update_profile_block(path, "  Новый блок  ", "  значение  ")
            self.assertEqual(updated["blocks"]["Новый блок"], "значение")
            self.assertNotIn("  Новый блок  ", updated["blocks"])

            deleted = files.update_profile_block(path, "Убрать", None)
            self.assertNotIn("Убрать", deleted["blocks"])

    def test_load_strategy_handles_unconfigured_missing_empty_and_nonempty_files(self) -> None:
        self.assertIsNone(files.load_strategy(None))
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.md"
            empty = Path(tmp) / "empty.md"
            strategy = Path(tmp) / "strategy.md"
            empty.write_text("", "utf-8")
            strategy.write_text("## План\nРабочий текст\n", "utf-8")

            self.assertIsNone(files.load_strategy(missing))
            self.assertIsNone(files.load_strategy(empty))
            self.assertEqual(files.load_strategy(strategy), "## План\nРабочий текст\n")


class PromptHelperEdgeCaseTests(unittest.TestCase):
    def test_fragments_rejects_a_file_without_fragment_headings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "plain.md").write_text("just prose\n", "utf-8")

            with self.assertRaisesRegex(coach_prompts.PromptError, "ни одного фрагмента"):
                coach_prompts.fragments("plain", directory=directory)

    def test_fragments_ignores_preamble_and_preserves_declared_order_and_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "parts.md").write_text(
                "comment before headings\n\n## first\n leading space\n\n## second\nB\n",
                "utf-8",
            )

            parsed = coach_prompts.fragments("parts", directory=directory)

            self.assertEqual(list(parsed), ["first", "second"])
            self.assertEqual(parsed["first"], " leading space")
            self.assertEqual(parsed["second"], "B")
            self.assertNotIn("comment", "".join(parsed.values()))

    def test_document_sections_reports_every_wanted_heading_when_document_has_none(self) -> None:
        body, missing = coach_prompts.document_sections(
            "# Intro\nNo second-level headings here.",
            ["Training", "Progression"],
        )

        self.assertEqual(body, "")
        self.assertEqual(missing, ["Training", "Progression"])

    def test_document_sections_matches_case_insensitively_and_keeps_wanted_order(self) -> None:
        text = "## 9. SECOND\nB\n\n## first\nA\n"

        body, missing = coach_prompts.document_sections(text, ["First", "Second"])

        self.assertEqual(missing, [])
        self.assertLess(body.index("A"), body.index("B"))

    def test_render_replaces_every_occurrence_of_the_same_slot(self) -> None:
        self.assertEqual(
            coach_prompts.render("{{value}} / {{value}}", value="x"),
            "x / x",
        )


def _recommendation(focus: str = "Test") -> dict[str, object]:
    return {
        "focus": focus,
        "load_type": "medium",
        "rationale": "reason",
        "exercises": [
            {
                "exercise_id": 1,
                "name": "Bench Press",
                "note": "",
                "sets": [{"reps": 10, "weight": 50}],
            }
        ],
    }


class StoreEdgeCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "nested" / "trainer.db"
        self.store = backend_store.MiniAppStore(self.db_path)
        self.user_id = int(self.store.ensure_debug_user("edge-user")["id"])

    def test_constructor_creates_parent_and_connection_enables_safety_pragmas(self) -> None:
        self.assertTrue(self.db_path.parent.is_dir())
        with self.store._connection() as connection:
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

        self.assertEqual(foreign_keys, 1)
        self.assertEqual(busy_timeout, 5000)

    def test_connection_rolls_back_the_whole_transaction_on_exception(self) -> None:
        class AbortTransaction(Exception):
            pass

        with self.assertRaises(AbortTransaction), self.store._connection() as connection:
            connection.execute(
                """
                    INSERT INTO signal_snoozes
                        (user_id, instance_key, snooze_until, created_at)
                    VALUES (?, ?, ?, ?)
                """,
                (self.user_id, "temporary", None, 1),
            )
            raise AbortTransaction

        self.assertEqual(self.store.list_signal_snoozes(self.user_id), {})

    def test_foreign_keys_reject_orphans_and_leave_connection_usable(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_body_weight(
                999_999,
                sample_body_weight_payload(entry_date="2026-01-01", weight=80),
            )

        saved, created = self.store.save_body_weight(
            self.user_id,
            sample_body_weight_payload(entry_date="2026-01-01", weight=80),
        )
        self.assertTrue(created)
        self.assertEqual(saved["weight"], 80.0)

    def test_deleting_user_cascades_through_all_data_tables(self) -> None:
        self.store.save_workout(
            self.user_id,
            sample_workout_payload(client_id="cascade-workout", workout_date="2026-01-01"),
        )
        self.store.save_body_weight(
            self.user_id,
            {"entry_date": "2026-01-01", "weight": 80},
        )
        self.store.save_waist(
            self.user_id,
            {"entry_date": "2026-01-01", "waist": 85},
        )
        self.store.save_event(
            self.user_id,
            {"start_date": "2026-01-01", "end_date": "2026-01-02", "text": "rest"},
        )
        self.store.save_recommendation(
            self.user_id,
            None,
            1,
            "model",
            _recommendation(),
            2,
            1,
        )
        self.store.save_coach_report(
            self.user_id,
            "2026-01-04",
            7,
            "report",
            "model",
            3,
            2,
        )
        self.store.save_signal_snooze(self.user_id, "signal:one", None)

        with self.store._connection() as connection:
            connection.execute("DELETE FROM users WHERE id = ?", (self.user_id,))

        with self.store._connection() as connection:
            table_queries = (
                ("workouts", "SELECT COUNT(*) FROM workouts"),
                ("body_weights", "SELECT COUNT(*) FROM body_weights"),
                ("waists", "SELECT COUNT(*) FROM waists"),
                ("events", "SELECT COUNT(*) FROM events"),
                ("recommendations", "SELECT COUNT(*) FROM recommendations"),
                ("recommendation_log", "SELECT COUNT(*) FROM recommendation_log"),
                ("coach_reports", "SELECT COUNT(*) FROM coach_reports"),
                ("signal_snoozes", "SELECT COUNT(*) FROM signal_snoozes"),
            )
            for table, query in table_queries:
                with self.subTest(table=table):
                    count = connection.execute(query).fetchone()[0]
                    self.assertEqual(count, 0)

    def test_upsert_telegram_user_rejects_missing_and_non_numeric_ids(self) -> None:
        invalid_ids = (None, "", "not-a-number", " 123 ", 12.5, object())
        for invalid_id in invalid_ids:
            with (
                self.subTest(invalid_id=invalid_id),
                self.assertRaisesRegex(
                    ValueError,
                    "Telegram user id",
                ),
            ):
                self.store.upsert_telegram_user({"id": invalid_id, "first_name": "Bad"})

    def test_existing_debug_alias_keeps_original_identity_fields(self) -> None:
        original = self.store.ensure_debug_user("stable", "First", "Identity")
        repeated = self.store.ensure_debug_user("stable", "Changed", "Name")

        self.assertEqual(repeated["id"], original["id"])
        self.assertEqual(repeated["display_name"], "First Identity")

    def test_get_workout_is_user_scoped_and_latest_uses_date_then_id(self) -> None:
        other_id = int(self.store.ensure_debug_user("other-user")["id"])
        newest_date, _ = self.store.save_workout(
            self.user_id,
            sample_workout_payload(client_id="new-date", workout_date="2026-01-03"),
        )
        self.store.save_workout(
            self.user_id,
            sample_workout_payload(client_id="old-date", workout_date="2026-01-01"),
        )
        same_date_later, _ = self.store.save_workout(
            self.user_id,
            sample_workout_payload(client_id="new-date-2", workout_date="2026-01-03"),
        )

        self.assertIsNone(self.store.get_workout_by_id(other_id, newest_date["id"]))
        self.assertEqual(
            self.store.get_workout_by_id(self.user_id, newest_date["id"])["client_id"],
            "new-date",
        )
        self.assertEqual(self.store.get_latest_workout_id(self.user_id), same_date_later["id"])

    def test_failed_recommendation_error_is_capped_in_cache_and_log(self) -> None:
        long_error = "ошибка" * 100

        self.store.fail_recommendation(self.user_id, long_error)

        cached = self.store.get_recommendation(self.user_id)
        logged = self.store.list_recommendation_log(self.user_id)
        self.assertEqual(cached["error"], long_error[:500])
        self.assertEqual(logged[0]["error"], long_error[:500])
        self.assertEqual(len(cached["error"]), 500)

    def test_ready_recommendation_clears_a_previous_failure(self) -> None:
        self.store.fail_recommendation(self.user_id, "temporary failure")

        ready = self.store.save_recommendation(
            self.user_id,
            None,
            0,
            "model",
            _recommendation("Recovered"),
            None,
            None,
        )

        self.assertEqual(ready["status"], "ready")
        self.assertIsNone(ready["error"])
        self.assertEqual(ready["recommendation"]["focus"], "Recovered")

    def test_clearing_recommendation_cache_preserves_bounded_audit_log(self) -> None:
        for focus in ("First", "Second"):
            self.store.save_recommendation(
                self.user_id,
                None,
                0,
                "model",
                _recommendation(focus),
                1,
                1,
            )

        self.store.clear_recommendation(self.user_id)

        self.assertIsNone(self.store.get_recommendation(self.user_id))
        latest = self.store.list_recommendation_log(self.user_id, limit=1)
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["recommendation"]["focus"], "Second")
        self.assertEqual(latest[0]["updated_at"], latest[0]["created_at"])
        self.assertEqual(self.store.list_recommendation_log(self.user_id, limit=0), [])

    def test_latest_report_filters_days_and_read_receipt_is_idempotent(self) -> None:
        self.assertFalse(self.store.mark_coach_report_read(self.user_id))
        self.store.save_coach_report(
            self.user_id,
            "2026-01-04",
            7,
            "old weekly",
            "model",
            1,
            1,
        )
        self.store.save_coach_report(
            self.user_id,
            "2026-01-11",
            7,
            "new weekly",
            "model",
            1,
            1,
        )
        self.store.save_coach_report(
            self.user_id,
            "2026-01-20",
            30,
            "monthly",
            "model",
            1,
            1,
        )

        latest = self.store.get_latest_coach_report(self.user_id, days=7)
        self.assertEqual(latest["report"], "new weekly")
        self.assertIsNone(latest["read_at"])
        self.assertTrue(self.store.mark_coach_report_read(self.user_id, days=7))
        self.assertFalse(self.store.mark_coach_report_read(self.user_id, days=7))
        self.assertIsNotNone(self.store.get_coach_report(self.user_id, "2026-01-11", 7)["read_at"])
        self.assertIsNone(self.store.get_coach_report(self.user_id, "2026-01-11", 30))

    def test_signal_snoozes_upsert_and_remain_isolated_per_user(self) -> None:
        other_id = int(self.store.ensure_debug_user("snooze-other")["id"])
        self.store.save_signal_snooze(self.user_id, "same-key", 123)
        self.store.save_signal_snooze(other_id, "same-key", 456)

        self.store.save_signal_snooze(self.user_id, "same-key", None)

        self.assertEqual(self.store.list_signal_snoozes(self.user_id), {"same-key": None})
        self.assertEqual(self.store.list_signal_snoozes(other_id), {"same-key": 456})

    def test_token_spend_groups_recommendations_and_reports_and_isolates_users(self) -> None:
        other_id = int(self.store.ensure_debug_user("spend-other")["id"])
        january = 1_767_225_600  # 2026-01-01T00:00:00Z
        with mock.patch.object(backend_store, "utc_now", return_value=january):
            self.store.save_recommendation(
                self.user_id,
                None,
                1,
                "model-a",
                _recommendation(),
                None,
                None,
            )
            self.store.save_coach_report(
                self.user_id,
                "2026-01-04",
                7,
                "report",
                "model-a",
                20,
                10,
            )

        february = 1_769_904_000  # 2026-02-01T00:00:00Z
        with mock.patch.object(backend_store, "utc_now", return_value=february):
            self.store.save_coach_report(
                self.user_id,
                "2026-02-01",
                7,
                "second report",
                "model-b",
                30,
                15,
            )

        rows = self.store.token_spend(self.user_id)
        keyed = {(row["month"], row["source"], row["model"]): row for row in rows}
        self.assertEqual(
            set(keyed),
            {
                ("2026-01", "recommendation", "model-a"),
                ("2026-01", "weekly_report", "model-a"),
                ("2026-02", "weekly_report", "model-b"),
            },
        )
        recommendation = keyed[("2026-01", "recommendation", "model-a")]
        self.assertEqual(recommendation["calls"], 1)
        self.assertEqual(recommendation["input_tokens"], 0)
        self.assertEqual(recommendation["output_tokens"], 0)
        self.assertEqual(keyed[("2026-01", "weekly_report", "model-a")]["input_tokens"], 20)
        self.assertEqual(keyed[("2026-02", "weekly_report", "model-b")]["input_tokens"], 30)
        self.assertEqual(self.store.token_spend(other_id), [])


class StoreMigrationEdgeCaseTests(unittest.TestCase):
    def test_initialization_adds_read_at_to_legacy_coach_reports_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE coach_reports (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        period_end TEXT NOT NULL,
                        days INTEGER NOT NULL,
                        report TEXT NOT NULL,
                        model TEXT,
                        input_tokens INTEGER,
                        output_tokens INTEGER,
                        created_at INTEGER NOT NULL,
                        UNIQUE(user_id, period_end, days)
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()

            store = backend_store.MiniAppStore(db_path)
            user_id = int(store.ensure_debug_user("legacy-user")["id"])
            stored = store.save_coach_report(
                user_id,
                "2026-01-04",
                7,
                "migrated",
                "model",
                1,
                1,
            )

            self.assertIn("read_at", stored)
            self.assertIsNone(stored["read_at"])
            self.assertTrue(store.mark_coach_report_read(user_id))


if __name__ == "__main__":
    unittest.main()
