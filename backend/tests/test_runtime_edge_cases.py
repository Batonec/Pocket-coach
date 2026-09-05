"""Граничные случаи процессов: чистые хелперы ``server.py``, фоновый воркер
совета, границы хендлеров, скрипты таймеров и Coach MCP (импортируется со
стабами пакета mcp).
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest import mock

import support

from infra.jobs import backup_db
from infra.jobs import refresh_recommendation as refresh_job
from infra.jobs import weekly_report as weekly_job
from trainer.domain import recommender

ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT_DIR / "backend"
COACH_MCP_SERVER = ROOT_DIR / "coach_mcp" / "server.py"


def bare_handler(module: types.ModuleType):
    """Хендлер без сокета и HTTPServer: голый объект с пустыми заголовками."""
    handler = object.__new__(module.MiniAppHandler)
    handler.headers = {}
    return handler


class _Record:
    """Простой объект из kwargs — стаб для типов mcp."""

    def __init__(self, **values: object) -> None:
        """Разложить значения в атрибуты."""
        vars(self).update(values)


class _FakeFastMCP:
    """Стаб FastMCP: декоратор tool ничего не оборачивает, run ничего не запускает."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        """Пустые настройки."""
        self.settings = types.SimpleNamespace()

    def tool(self):
        """Декоратор-пустышка."""
        return lambda function: function

    def run(self, *_args: object, **_kwargs: object) -> None:
        """Ничего не запускает."""
        return

    def streamable_http_app(self):
        """Любой объект вместо ASGI-приложения."""
        return object()


@contextmanager
def loaded_coach_mcp(temp_dir: Path) -> Iterator[Any]:
    """Импортировать coach_mcp/server.py с крошечными stdlib-стабами типов mcp.

    Боевая зависимость намеренно не установлена в тестовом окружении backend.
    Стабы оставляют всю граничную логику нетронутой и гарантируют, что импорт не
    запускает транспорт и не ходит в сеть.
    """
    stub_names = (
        "mcp",
        "mcp.server",
        "mcp.server.fastmcp",
        "mcp.server.transport_security",
        "mcp.types",
    )
    previous = {name: sys.modules.get(name) for name in stub_names}

    mcp_package = types.ModuleType("mcp")
    mcp_server_package = types.ModuleType("mcp.server")
    fastmcp_module: Any = types.ModuleType("mcp.server.fastmcp")
    security_module: Any = types.ModuleType("mcp.server.transport_security")
    types_module: Any = types.ModuleType("mcp.types")
    fastmcp_module.FastMCP = _FakeFastMCP
    security_module.TransportSecuritySettings = _Record
    types_module.CallToolResult = _Record
    types_module.TextContent = _Record

    sys.modules.update(
        {
            "mcp": mcp_package,
            "mcp.server": mcp_server_package,
            "mcp.server.fastmcp": fastmcp_module,
            "mcp.server.transport_security": security_module,
            "mcp.types": types_module,
        }
    )

    module_name = f"coach_mcp_server_test_{uuid.uuid4().hex}"
    env = {
        "COACH_MCP_BACKEND_DIR": str(BACKEND_DIR),
        "MINIAPP_DB_PATH": str(temp_dir / "coach-mcp.db"),
        "EXERCISE_CATALOG_PATH": str(BACKEND_DIR / "resources" / "exercises.json"),
        "COACH_MCP_PROFILE_PATH": str(temp_dir / "coach_profile.json"),
        "COACH_MCP_STRATEGY_PATH": str(temp_dir / "coach_strategy.md"),
        "COACH_STATE_PATH": str(temp_dir / "coach_state.json"),
        "COACH_MCP_USER_ID": "73",
    }
    try:
        with mock.patch.dict(os.environ, env, clear=False):
            spec = importlib.util.spec_from_file_location(module_name, COACH_MCP_SERVER)
            if spec is None or spec.loader is None:
                raise RuntimeError("coach_mcp/server.py could not be loaded")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


class ServerPureHelperTests(unittest.TestCase):
    """Чистые хелперы и генерация совета в ``server.py`` на моках стора."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.module = support.load_server_module(db_path=self.root / "trainer.db")

    def test_positive_int_accepts_integer_like_values_and_rejects_bad_ones(self) -> None:
        for value, expected in ((1, 1), (" 42 ", 42), (3.0, 3), (True, 1)):
            with self.subTest(value=value):
                self.assertEqual(self.module.positive_int(value), expected)

        for value in (None, "", "3.5", object(), 0, -7):
            with self.subTest(value=value):
                self.assertIsNone(self.module.positive_int(value))

    def test_path_id_is_strict_about_route_shape(self) -> None:
        prefix = "/api/workouts/"
        self.assertEqual(self.module._path_id("/api/workouts/17", prefix), 17)
        self.assertEqual(self.module._path_id("/api/workouts/17/", prefix), 17)
        for path in (
            "/api/events/17",
            "/api/workouts/",
            "/api/workouts/nope",
            "/api/workouts/17/more",
        ):
            with self.subTest(path=path):
                self.assertIsNone(self.module._path_id(path, prefix))

    def test_build_dev_version_filters_files_and_is_order_independent(self) -> None:
        (self.root / "nested").mkdir()
        (self.root / "__pycache__").mkdir()
        watched = self.root / "nested" / "app.py"
        watched.write_text("print('ok')", encoding="utf-8")
        (self.root / "view.css").write_text("body {}", encoding="utf-8")
        (self.root / "notes.txt").write_text("ignored", encoding="utf-8")
        (self.root / "__pycache__" / "cached.py").write_text("ignored", encoding="utf-8")

        with mock.patch.object(self.module, "BASE_DIR", self.root):
            paths = self.module.iter_watched_files()
            version = self.module.build_dev_version()

        self.assertEqual(paths, sorted([self.root / "view.css", watched]))
        self.assertEqual(version["watched_files"], 2)
        self.assertEqual(len(version["version"]), 12)
        self.assertEqual(version["latest_mtime_ns"], max(path.stat().st_mtime_ns for path in paths))

    def test_generate_marks_catalog_failure_without_calling_model(self) -> None:
        store = mock.Mock()
        with (
            mock.patch.object(self.module, "STORE", store),
            mock.patch.object(self.module, "EXERCISE_CATALOG", None),
            mock.patch.object(self.module.recommender, "generate") as generate,
        ):
            result = self.module._generate_and_store_recommendation(9)

        self.assertIsNone(result)
        store.fail_recommendation.assert_called_once_with(9, "Каталог упражнений недоступен")
        generate.assert_not_called()

    def test_generate_records_public_model_error_verbatim(self) -> None:
        store = mock.Mock()
        store.list_workouts.return_value = []
        store.list_body_weights.return_value = []
        error = self.module.recommender.RecommendationError("API temporarily unavailable")
        with (
            mock.patch.object(self.module, "STORE", store),
            mock.patch.object(self.module, "EXERCISE_CATALOG", []),
            mock.patch.object(self.module.recommender, "generate", side_effect=error),
        ):
            result = self.module._generate_and_store_recommendation(4)

        self.assertIsNone(result)
        store.fail_recommendation.assert_called_once_with(4, "API temporarily unavailable")

    def test_generate_sanitizes_unexpected_errors(self) -> None:
        store = mock.Mock()
        store.list_workouts.return_value = []
        store.list_body_weights.return_value = []
        with (
            mock.patch.object(self.module, "STORE", store),
            mock.patch.object(self.module, "EXERCISE_CATALOG", []),
            mock.patch.object(
                self.module.recommender, "generate", side_effect=RuntimeError("secret detail")
            ),
            mock.patch("builtins.print") as output,
        ):
            result = self.module._generate_and_store_recommendation(5)

        self.assertIsNone(result)
        store.fail_recommendation.assert_called_once_with(
            5, "Внутренняя ошибка генерации рекомендации"
        )
        self.assertIn("secret detail", output.call_args.args[0])

    def test_generate_passes_every_runtime_input_and_persists_usage(self) -> None:
        store = mock.Mock()
        workouts = [{"id": 8}]
        weights = [{"weight": 80}]
        waists = [{"waist": 84}]
        events = [{"text": "болел"}]
        store.list_workouts.return_value = workouts
        store.get_latest_workout_id.return_value = 8
        store.list_body_weights.return_value = weights
        store.list_waists.return_value = waists
        store.list_events.return_value = events
        store.save_recommendation.return_value = {"status": "ready"}
        generated = ({"focus": "test"}, {"input_tokens": 11, "output_tokens": 7}, "model")

        with (
            mock.patch.object(self.module, "STORE", store),
            mock.patch.object(self.module, "EXERCISE_CATALOG", [{"id": 1}]),
            mock.patch.object(self.module.files, "load_profile", return_value={"profile": 1}),
            mock.patch.object(self.module.files, "load_strategy", return_value="strategy"),
            mock.patch.object(self.module.files, "load_state", return_value={"phase": "x"}),
            mock.patch.object(self.module.recommender, "generate", return_value=generated) as call,
        ):
            result = self.module._generate_and_store_recommendation(12)

        self.assertEqual(result, {"status": "ready"})
        call.assert_called_once_with(
            workouts,
            weights,
            [{"id": 1}],
            profile={"profile": 1},
            strategy="strategy",
            state={"phase": "x"},
            waists=waists,
            events=events,
        )
        store.save_recommendation.assert_called_once_with(
            12, 8, 1, "model", {"focus": "test"}, 11, 7
        )

    def test_recommendation_response_only_marks_ready_rows_stale(self) -> None:
        handler = bare_handler(self.module)
        store = mock.Mock()
        store.get_latest_workout_id.return_value = 99
        with mock.patch.object(self.module, "STORE", store):
            for status, stale in (("ready", True), ("pending", False), ("failed", False)):
                with self.subTest(status=status):
                    store.get_recommendation.return_value = {
                        "status": status,
                        "based_on_workout_id": 1,
                    }
                    payload = handler._recommendation_response({"id": 3})
                    self.assertEqual(payload["stale"], stale)


class RecommendationWorkerTests(unittest.TestCase):
    """Фоновый воркер ``trigger_recommendation_async``: учёт, схлопывание, ошибки."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.module = support.load_server_module(db_path=Path(temporary.name) / "trainer.db")

    def test_catalog_unavailable_clears_stale_row_without_starting_thread(self) -> None:
        store = mock.Mock()
        store.get_latest_workout_id.return_value = None
        with (
            mock.patch.object(self.module, "STORE", store),
            mock.patch.object(self.module, "EXERCISE_CATALOG", None),
            mock.patch.object(self.module.threading, "Thread") as thread,
        ):
            self.module.trigger_recommendation_async(31)

        store.clear_recommendation.assert_called_once_with(31)
        thread.assert_not_called()

    def test_existing_worker_only_records_one_follow_up_request(self) -> None:
        store = mock.Mock()
        store.get_latest_workout_id.return_value = 4
        self.module._recommendation_workers.add(21)
        with (
            mock.patch.object(self.module, "STORE", store),
            mock.patch.object(self.module.threading, "Thread") as thread,
        ):
            self.module.trigger_recommendation_async(21)
            self.module.trigger_recommendation_async(21)

        self.assertEqual(self.module._recommendation_rerun_requested, {21})
        self.assertEqual(store.set_recommendation_pending.call_count, 2)
        thread.assert_not_called()

    def test_inline_worker_with_no_history_clears_and_releases_bookkeeping(self) -> None:
        store = mock.Mock()
        store.get_latest_workout_id.return_value = None

        class InlineThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self) -> None:
                self.target()

        with (
            mock.patch.object(self.module, "STORE", store),
            mock.patch.object(self.module.threading, "Thread", InlineThread),
            mock.patch.object(self.module, "_generate_and_store_recommendation") as generate,
        ):
            self.module.trigger_recommendation_async(8)

        self.assertEqual(store.clear_recommendation.call_count, 2)
        generate.assert_not_called()
        self.assertNotIn(8, self.module._recommendation_workers)
        self.assertNotIn(8, self.module._recommendation_rerun_requested)
        lock = self.module._user_recommendation_lock(8)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_worker_exception_cleans_sets_and_releases_user_lock(self) -> None:
        store = mock.Mock()
        store.get_latest_workout_id.side_effect = [1, RuntimeError("db locked")]

        class InlineThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self) -> None:
                self.target()

        with (
            mock.patch.object(self.module, "STORE", store),
            mock.patch.object(self.module.threading, "Thread", InlineThread),
            mock.patch("builtins.print") as output,
        ):
            self.module.trigger_recommendation_async(18)

        self.assertNotIn(18, self.module._recommendation_workers)
        self.assertNotIn(18, self.module._recommendation_rerun_requested)
        self.assertIn("db locked", output.call_args.args[0])
        lock = self.module._user_recommendation_lock(18)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()


class HandlerBoundaryTests(unittest.TestCase):
    """Границы хендлеров: диспетчер, OPTIONS, тело запроса, ответы, cookie, refresh, dismiss."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.module = support.load_server_module(db_path=self.root / "trainer.db")

    def test_dispatch_strips_query_routes_ids_and_returns_json_404(self) -> None:
        hits: list[tuple[str, int | None]] = []

        def exact(_handler) -> None:
            hits.append(("exact", None))

        def entity(_handler, entity_id: int) -> None:
            hits.append(("id", entity_id))

        handler = bare_handler(self.module)
        handler._send_json = mock.Mock()
        with (
            mock.patch.object(self.module, "ROUTES", {("GET", "/exact"): exact}),
            mock.patch.object(self.module, "ID_ROUTES", {("DELETE", "/items/"): entity}),
        ):
            handler.path = "/exact?cache=bust"
            handler._dispatch("GET")
            handler.path = "/items/7?ignored=yes"
            handler._dispatch("DELETE")
            handler.path = "/items/7/extra"
            handler._dispatch("DELETE")

        self.assertEqual(hits, [("exact", None), ("id", 7)])
        handler._send_json.assert_called_once_with(
            HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Not found"}
        )

    def test_options_advertises_every_supported_method(self) -> None:
        handler = bare_handler(self.module)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()

        handler.do_OPTIONS()

        handler.send_response.assert_called_once_with(HTTPStatus.NO_CONTENT)
        self.assertIn(
            mock.call("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"),
            handler.send_header.call_args_list,
        )
        handler.end_headers.assert_called_once_with()

    def test_read_json_body_handles_empty_valid_and_malformed_payloads(self) -> None:
        handler = bare_handler(self.module)
        handler._send_json = mock.Mock()

        handler.headers = {"Content-Length": "0"}
        handler.rfile = io.BytesIO()
        self.assertEqual(handler._read_json_body(), {})

        encoded = json.dumps({"тест": 3}).encode("utf-8")
        handler.headers = {"Content-Length": str(len(encoded))}
        handler.rfile = io.BytesIO(encoded)
        self.assertEqual(handler._read_json_body(), {"тест": 3})

        malformed = b"{broken"
        handler.headers = {"Content-Length": str(len(malformed))}
        handler.rfile = io.BytesIO(malformed)
        self.assertIsNone(handler._read_json_body())
        handler._send_json.assert_called_once_with(
            HTTPStatus.BAD_REQUEST, {"ok": False, "reason": "Invalid JSON body"}
        )

    def test_read_json_body_rejects_non_object_invalid_length_and_invalid_utf8(self) -> None:
        handler = bare_handler(self.module)
        handler._send_json = mock.Mock()

        for body, length, reason in (
            (b"[]", "2", "JSON body must be an object"),
            (b'"text"', "6", "JSON body must be an object"),
            (b"", "not-a-number", "Invalid JSON body"),
            (b"", "-1", "Invalid JSON body"),
            (b"\xff", "1", "Invalid JSON body"),
        ):
            with self.subTest(body=body, length=length):
                handler.headers = {"Content-Length": length}
                handler.rfile = io.BytesIO(body)
                handler._send_json.reset_mock()

                self.assertIsNone(handler._read_json_body())
                handler._send_json.assert_called_once_with(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "reason": reason},
                )

    def test_send_json_serializes_unicode_and_sets_exact_byte_length(self) -> None:
        handler = bare_handler(self.module)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = io.BytesIO()

        handler._send_json(
            HTTPStatus.ACCEPTED,
            {"message": "готово"},
            extra_headers={"X-Test": "yes"},
        )

        body = handler.wfile.getvalue()
        self.assertEqual(json.loads(body.decode("utf-8")), {"message": "готово"})
        handler.send_response.assert_called_once_with(HTTPStatus.ACCEPTED)
        self.assertIn(
            mock.call("Content-Length", str(len(body))), handler.send_header.call_args_list
        )
        self.assertIn(mock.call("X-Test", "yes"), handler.send_header.call_args_list)

    def test_send_file_covers_missing_text_and_binary_assets(self) -> None:
        missing_handler = bare_handler(self.module)
        missing_handler._send_json = mock.Mock()
        missing_handler._send_file(self.root / "missing.txt")
        missing_handler._send_json.assert_called_once_with(
            HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Missing asset"}
        )

        for name, content_type, body in (
            ("asset.txt", "text/plain; charset=utf-8", "привет".encode()),
            ("asset.unknown", "application/octet-stream", b"\x00\x01"),
        ):
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(body)
                handler = bare_handler(self.module)
                handler.send_response = mock.Mock()
                handler.send_header = mock.Mock()
                handler.end_headers = mock.Mock()
                handler.wfile = io.BytesIO()
                handler._send_file(path)
                self.assertEqual(handler.wfile.getvalue(), body)
                self.assertIn(
                    mock.call("Content-Type", content_type), handler.send_header.call_args_list
                )

    def test_session_cookies_toggle_secure_and_logout_expires_immediately(self) -> None:
        handler = bare_handler(self.module)
        with mock.patch.object(self.module, "COOKIE_SECURE", True):
            session_cookie = handler._build_session_cookie(7)
            clear_cookie = handler._clear_session_cookie()
        self.assertIn("trainer_session=7.", session_cookie)
        self.assertIn("Max-Age=2592000", session_cookie)
        self.assertIn("Secure", session_cookie)
        self.assertIn("Max-Age=0", clear_cookie)
        self.assertIn("Secure", clear_cookie)

        with mock.patch.object(self.module, "COOKIE_SECURE", False):
            self.assertNotIn("Secure", handler._build_session_cookie(7))

    def test_manual_refresh_returns_accepted_when_user_lock_is_busy(self) -> None:
        handler = bare_handler(self.module)
        handler._require_user = mock.Mock(return_value=({"id": 7}, {"X-Session": "ok"}))
        handler._recommendation_response = mock.Mock(return_value={"ok": True, "status": "ready"})
        handler._send_json = mock.Mock()
        lock = threading.Lock()
        lock.acquire()
        try:
            with mock.patch.object(self.module, "_user_recommendation_lock", return_value=lock):
                handler._post_recommendation_refresh()
        finally:
            lock.release()

        handler._send_json.assert_called_once_with(
            HTTPStatus.ACCEPTED,
            {"ok": True, "status": "pending"},
            extra_headers={"X-Session": "ok"},
        )

    def test_manual_refresh_cooldown_reuses_cache_and_releases_lock(self) -> None:
        handler = bare_handler(self.module)
        handler._require_user = mock.Mock(return_value=({"id": 9}, {}))
        handler._recommendation_response = mock.Mock(return_value={"ok": True, "status": "ready"})
        handler._send_json = mock.Mock()
        store = mock.Mock()
        lock = threading.Lock()
        with (
            mock.patch.object(self.module, "STORE", store),
            mock.patch.object(self.module, "_user_recommendation_lock", return_value=lock),
            mock.patch.object(self.module, "REFRESH_MIN_INTERVAL", 10.0),
            mock.patch.object(self.module, "_last_refresh_started", {9: 95.0}),
            mock.patch.object(self.module.time, "monotonic", return_value=100.0),
            mock.patch.object(self.module, "_generate_and_store_recommendation") as generate,
        ):
            handler._post_recommendation_refresh()

        payload = handler._send_json.call_args.args[1]
        self.assertEqual(handler._send_json.call_args.args[0], HTTPStatus.OK)
        self.assertIn("Слишком частый", payload["reason"])
        store.set_recommendation_pending.assert_not_called()
        generate.assert_not_called()
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_manual_refresh_reports_catalog_outage_before_locking(self) -> None:
        handler = bare_handler(self.module)
        handler._require_user = mock.Mock(return_value=({"id": 4}, {}))
        handler._send_json = mock.Mock()
        with (
            mock.patch.object(self.module, "EXERCISE_CATALOG", None),
            mock.patch.object(self.module, "_user_recommendation_lock") as get_lock,
        ):
            handler._post_recommendation_refresh()

        self.assertEqual(handler._send_json.call_args.args[0], HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertIn("каталог", handler._send_json.call_args.args[1]["reason"])
        get_lock.assert_not_called()

    def test_signal_dismiss_rejects_critical_and_non_integer_snooze(self) -> None:
        handler = bare_handler(self.module)
        handler._require_user = mock.Mock(return_value=({"id": 2}, {"X": "session"}))
        handler._send_json = mock.Mock()
        store = mock.Mock()
        critical = [{"instance_key": "danger", "severity": "critical"}]
        with (
            mock.patch.object(self.module, "STORE", store),
            mock.patch.object(self.module.files, "load_state", return_value={}),
            mock.patch.object(self.module.coach_signals, "compute_signals", return_value=critical),
        ):
            handler._read_json_body = mock.Mock(return_value={"instance_key": "danger"})
            handler._post_signal_dismiss()
            self.assertEqual(handler._send_json.call_args.args[0], HTTPStatus.CONFLICT)
            store.save_signal_snooze.assert_not_called()

            handler._send_json.reset_mock()
            handler._read_json_body = mock.Mock(
                return_value={"instance_key": "gone", "snooze_hours": "later"}
            )
            handler._post_signal_dismiss()
            self.assertEqual(handler._send_json.call_args.args[0], HTTPStatus.BAD_REQUEST)
            store.save_signal_snooze.assert_not_called()


class ScheduledJobEdgeCaseTests(unittest.TestCase):
    """Скрипты таймеров: пороги свежести, ошибки генерации, бэкап и ротация."""

    def test_should_refresh_exact_thresholds_missing_timestamps_and_unknown_status(self) -> None:
        now = 10 * 3600
        refresh, reason = refresh_job.recommender.should_refresh(
            {"status": "ready", "updated_at": now - 24 * 3600}, now, 24
        )
        self.assertFalse(refresh)
        self.assertIn("свежая", reason)

        refresh, reason = refresh_job.recommender.should_refresh(
            {"status": "pending", "updated_at": now - 2 * 3600}, now
        )
        self.assertFalse(refresh)
        self.assertIn("уже идёт", reason)

        for rec in (
            {"status": "mystery", "updated_at": now},
            {"status": "ready"},
        ):
            with self.subTest(rec=rec):
                self.assertTrue(refresh_job.recommender.should_refresh(rec, now, 1)[0])

    def test_refresh_run_sanitizes_unexpected_failure_after_pending(self) -> None:
        store = mock.Mock()
        store.get_recommendation.return_value = None
        store.list_workouts.return_value = [{"id": 1}]
        store.get_latest_workout_id.return_value = 1
        store.list_body_weights.return_value = []
        with (
            mock.patch.object(
                refresh_job.files, "load_catalog", side_effect=OSError("private path")
            ),
            mock.patch("builtins.print"),
        ):
            self.assertTrue(refresh_job.run(store, 6))

        store.set_recommendation_pending.assert_called_once_with(6)
        store.fail_recommendation.assert_called_once_with(
            6, "Внутренняя ошибка генерации рекомендации"
        )
        store.save_recommendation.assert_not_called()

    def test_weekly_job_model_error_does_not_write_cache(self) -> None:
        store = mock.Mock()
        store.get_coach_report.return_value = None  # ни текущего, ни прошлого отчёта в кэше
        error = recommender.RecommendationError("quota")
        with (
            mock.patch.object(weekly_job.recommender, "generate_weekly_report", side_effect=error),
            mock.patch("builtins.print"),
        ):
            generated = weekly_job.run(store, 3, force=True, today=date(2026, 8, 31))

        self.assertFalse(generated)
        store.save_coach_report.assert_not_called()

    def test_backup_main_exits_before_opening_missing_database(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        with (
            mock.patch.object(backup_db, "DB_PATH", root / "missing.db"),
            mock.patch.object(backup_db, "BACKUP_DIR", root / "backups"),
            mock.patch.object(sys, "stderr", io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            backup_db.main()

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("база не найдена", stderr.getvalue())
        self.assertFalse((root / "backups").exists())

    def test_rotate_zero_removes_all_backups_but_ignores_unrelated_files(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        root.mkdir(exist_ok=True)
        backups = [root / f"trainer-2026010{day}-000000.db.gz" for day in (1, 2)]
        for path in backups:
            path.write_bytes(b"backup")
        unrelated = root / "trainer-not-a-backup.db"
        unrelated.write_bytes(b"keep")

        removed = backup_db.rotate(root, keep=0)

        self.assertEqual({path.name for path in removed}, {path.name for path in backups})
        self.assertTrue(unrelated.exists())

    def test_backup_rotation_removes_strategy_companion_with_database(self) -> None:
        """Копия-спутник живёт столько же, сколько снимок базы."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        database = root / "trainer.db"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.commit()
        connection.close()
        backup_dir = root / "backups"
        backup_dir.mkdir()
        old_stamp = "20260101-000000"
        (backup_dir / f"trainer-{old_stamp}.db.gz").write_bytes(b"old")
        for prefix in ("coach_profile", "coach_strategy", "coach_state"):
            (backup_dir / f"{prefix}-{old_stamp}.json").write_text("{}", encoding="utf-8")
        profile = root / "coach_profile.json"
        strategy = root / "coach_strategy.md"
        state = root / "coach_state.json"
        profile.write_text("{}", encoding="utf-8")
        strategy.write_text("strategy", encoding="utf-8")
        state.write_text("{}", encoding="utf-8")

        class FixedDateTime:
            @classmethod
            def now(cls, _tz=None):
                return datetime(2026, 1, 2, tzinfo=timezone.utc)

        with (
            mock.patch.object(backup_db, "DB_PATH", database),
            mock.patch.object(backup_db, "PROFILE_PATH", profile),
            mock.patch.object(backup_db, "STRATEGY_PATH", strategy),
            mock.patch.object(backup_db, "STATE_PATH", state),
            mock.patch.object(backup_db, "BACKUP_DIR", backup_dir),
            mock.patch.object(backup_db, "KEEP", 1),
            mock.patch.object(backup_db, "datetime", FixedDateTime),
            mock.patch("builtins.print"),
        ):
            backup_db.main()

        self.assertFalse((backup_dir / f"coach_profile-{old_stamp}.json").exists())
        self.assertFalse((backup_dir / f"coach_state-{old_stamp}.json").exists())
        self.assertFalse((backup_dir / f"coach_strategy-{old_stamp}.json").exists())


class CoachMcpBoundaryTests(unittest.TestCase):
    """Coach MCP со стабами: результат, id, стоимость, кэш отчёта, генерация, bearer-middleware."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        context = loaded_coach_mcp(self.root)
        self.module = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def test_result_serializes_unicode_and_derives_error_flag(self) -> None:
        success = self.module._result({"ok": True, "value": "готово"})
        failure = self.module._result({"ok": False, "summary": "сломано"})

        self.assertFalse(success.isError)
        self.assertEqual(success.structuredContent["value"], "готово")
        self.assertIn('"value": "готово"', success.content[0].text)
        self.assertTrue(failure.isError)
        self.assertTrue(failure.content[0].text.startswith("сломано"))

    def test_uid_default_cost_table_and_event_period_edges(self) -> None:
        self.assertEqual(self.module._uid(None), 73)
        self.assertEqual(self.module._uid(0), 73)
        self.assertEqual(self.module._uid("8"), 8)
        self.assertEqual(
            self.module._estimate_cost(
                "claude-sonnet-4-6", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
            ),
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "usd": 18.0},
        )
        self.assertIsNone(self.module._estimate_cost("unknown", {}))
        self.assertEqual(
            self.module._event_period({"start_date": "2026-09-01", "end_date": None}),
            "с 2026-09-01, идёт",
        )
        self.assertEqual(
            self.module._event_period({"start_date": "2026-09-01", "end_date": "2026-09-01"}),
            "2026-09-01",
        )

    def test_stored_recommendation_marks_only_ready_mismatch_stale(self) -> None:
        store = mock.Mock()
        store.get_latest_workout_id.return_value = 9
        self.module.STORE = store
        for status, expected in (("ready", True), ("failed", False), ("pending", False)):
            with self.subTest(status=status):
                store.get_recommendation.return_value = {
                    "status": status,
                    "based_on_workout_id": 4,
                }
                result = self.module.coach_get_stored_recommendation(user_id=2)
                self.assertEqual(result.structuredContent["stale"], expected)
                self.assertFalse(result.isError)

    def test_weekly_report_cache_uses_same_closed_week_anchor_as_timer(self) -> None:
        store = mock.Mock()
        store.get_coach_report.return_value = {"report": "cached report", "model": "cached-model"}
        self.module.STORE = store

        class FixedDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 8, 31)

        with (
            mock.patch.object(self.module, "date", FixedDate),
            mock.patch.object(self.module.recommender, "generate_weekly_report") as generate,
        ):
            result = self.module.coach_weekly_report(days=7, user_id=12)

        expected_end = weekly_job.recommender.weekly_report_period(date(2026, 8, 31)).isoformat()
        self.assertEqual(expected_end, "2026-08-30")
        store.get_coach_report.assert_called_once_with(12, expected_end, 7)
        self.assertEqual(result.structuredContent["period_end"], expected_end)
        self.assertTrue(result.structuredContent["cached"])
        generate.assert_not_called()

    def test_weekly_report_generation_passes_last_weeks_focus(self) -> None:
        """Прошлый отчёт ищется тем же якорем, что и текущий, на `days` раньше."""
        store = mock.Mock()
        previous = {"report": "**Фокус следующей недели**\n- бицепс бедра", "model": "m"}
        store.get_coach_report.side_effect = lambda _uid, end, _days: (
            previous if end == "2026-08-23" else None
        )
        store.list_workouts.return_value = []
        store.list_body_weights.return_value = []
        store.list_waists.return_value = []
        store.list_events.return_value = []
        self.module.STORE = store
        seen: dict[str, object] = {}

        def capture(workouts, body_weights, waists, catalog, **kwargs):
            seen.update(kwargs)
            return "отчёт", {"input_tokens": 1, "output_tokens": 2}, "claude-test"

        class FixedDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 8, 31)

        with (
            mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}),
            mock.patch.object(self.module, "date", FixedDate),
            mock.patch.object(self.module.recommender, "generate_weekly_report", capture),
        ):
            result = self.module.coach_weekly_report(days=7, user_id=12)

        self.assertFalse(result.isError)
        self.assertEqual(seen.get("previous_report"), previous["report"])
        store.save_coach_report.assert_called_once()

    def test_weekly_report_without_cache_or_api_key_fails_before_model_call(self) -> None:
        store = mock.Mock()
        store.get_coach_report.return_value = None
        self.module.STORE = store
        with (
            mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}),
            mock.patch.object(self.module.recommender, "generate_weekly_report") as generate,
        ):
            result = self.module.coach_weekly_report(user_id=5)

        self.assertTrue(result.isError)
        self.assertIn("ANTHROPIC_API_KEY", result.structuredContent["summary"])
        generate.assert_not_called()
        store.save_coach_report.assert_not_called()

    def test_generate_recommendation_does_not_store_without_explicit_flag(self) -> None:
        store = mock.Mock()
        store.list_workouts.return_value = [{"id": 1}, {"id": 2}]
        store.list_body_weights.return_value = [{"weight": 80}]
        store.list_waists.return_value = [{"waist": 84}]
        store.list_events.return_value = [{"text": "break"}]
        self.module.STORE = store
        generated = (
            {"focus": "next"},
            {"input_tokens": 10, "output_tokens": 5},
            "claude-haiku-4-5",
        )
        with (
            mock.patch.object(self.module, "_catalog", return_value=[{"id": 1}]),
            mock.patch.object(self.module.files, "load_profile", return_value={}),
            mock.patch.object(self.module.files, "load_strategy", return_value="strategy"),
            mock.patch.object(self.module.files, "load_state", return_value={}),
            mock.patch.object(self.module.recommender, "generate", return_value=generated) as call,
        ):
            result = self.module.coach_generate_recommendation(limit=6, store=False, user_id=3)

        self.assertFalse(result.isError)
        self.assertFalse(result.structuredContent["stored"])
        store.save_recommendation.assert_not_called()
        call.assert_called_once_with(
            [{"id": 1}, {"id": 2}],
            [{"weight": 80}],
            [{"id": 1}],
            profile={},
            strategy="strategy",
            state={},
            waists=[{"waist": 84}],
            events=[{"text": "break"}],
            history_limit=6,
        )

    def test_generate_recommendation_explicit_store_uses_latest_workout(self) -> None:
        store = mock.Mock()
        store.list_workouts.return_value = [{"id": 1}, {"id": 2}]
        store.list_body_weights.return_value = []
        store.list_waists.return_value = []
        store.list_events.return_value = []
        store.get_latest_workout_id.return_value = 2
        store.save_recommendation.return_value = {"status": "ready"}
        self.module.STORE = store
        generated = ({"focus": "next"}, {}, "unknown-model")
        with (
            mock.patch.object(self.module, "_catalog", return_value=[]),
            mock.patch.object(self.module.files, "load_profile", return_value=None),
            mock.patch.object(self.module.files, "load_strategy", return_value=None),
            mock.patch.object(self.module.files, "load_state", return_value={}),
            mock.patch.object(self.module.recommender, "generate", return_value=generated),
        ):
            result = self.module.coach_generate_recommendation(store=True, user_id=3)

        store.save_recommendation.assert_called_once_with(
            3, 2, 2, "unknown-model", {"focus": "next"}, None, None
        )
        self.assertTrue(result.structuredContent["stored"])
        self.assertEqual(result.structuredContent["stored_row"], {"status": "ready"})
        self.assertIsNone(result.structuredContent["cost"])

    def test_bearer_middleware_rejects_bad_token_without_calling_app(self) -> None:
        app_calls: list[object] = []
        sent: list[dict[str, object]] = []

        async def app(scope, receive, send) -> None:
            app_calls.append(scope)

        async def receive():
            return {}

        async def send(message) -> None:
            sent.append(message)

        middleware = self.module._BearerAuthMiddleware(app, "secret")
        asyncio.run(
            middleware(
                {"type": "http", "headers": [(b"authorization", b"Bearer wrong")]},
                receive,
                send,
            )
        )

        self.assertEqual(app_calls, [])
        self.assertEqual(sent[0]["status"], 401)
        self.assertEqual(sent[1]["body"], b'{"error":"unauthorized"}')

    def test_bearer_middleware_passes_valid_http_and_non_http_scopes(self) -> None:
        app_calls: list[object] = []

        async def app(scope, receive, send) -> None:
            app_calls.append(scope)

        async def receive():
            return {}

        async def send(_message) -> None:
            self.fail("authorized requests must not be answered by middleware")

        middleware = self.module._BearerAuthMiddleware(app, "secret")
        http_scope = {"type": "http", "headers": [(b"authorization", b"Bearer secret")]}
        websocket_scope = {"type": "websocket", "headers": []}
        asyncio.run(middleware(http_scope, receive, send))
        asyncio.run(middleware(websocket_scope, receive, send))

        self.assertEqual(app_calls, [http_scope, websocket_scope])


if __name__ == "__main__":
    unittest.main()
