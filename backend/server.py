#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import threading
import time
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import coach_state
import recommender
from backend_store import MiniAppStore


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = Path(os.getenv("MINIAPP_STATIC_DIR", str(BASE_DIR / "static"))).resolve()
DATA_DIR = BASE_DIR / "data"
HOST = os.getenv("MINIAPP_HOST", "127.0.0.1")
PORT = int(os.getenv("MINIAPP_PORT", "8080"))
DEV_MODE = os.getenv("MINIAPP_DEV_MODE", "").lower() in {"1", "true", "yes", "on"}
ALLOW_DEBUG_USER = os.getenv("MINIAPP_ALLOW_DEBUG_USER", "").lower() in {"1", "true", "yes", "on"}
DEFAULT_DEBUG_USER_ALIAS = os.getenv("MINIAPP_DEFAULT_DEBUG_USER_ALIAS", "browser-default")
DEFAULT_DEBUG_USER_FIRST_NAME = os.getenv("MINIAPP_DEFAULT_DEBUG_USER_FIRST_NAME", "Browser")
DEFAULT_DEBUG_USER_LAST_NAME = os.getenv("MINIAPP_DEFAULT_DEBUG_USER_LAST_NAME", "Debug")
DB_PATH = Path(os.getenv("MINIAPP_DB_PATH", str(DATA_DIR / "trainer.db")))
# Athlete profile for the coach prompt: personal/medical context, lives next to
# the DB on the server only (never in the public repo).
COACH_PROFILE_PATH = Path(os.getenv("COACH_PROFILE_PATH", str(DB_PATH.parent / "coach_profile.json")))
# Mutable coaching state (preparation phase, waist limit, injection day) —
# same location policy as the profile; switched via the Coach MCP tools.
COACH_STATE_PATH = coach_state.default_state_path(DB_PATH)
SESSION_COOKIE_NAME = "trainer_session"
SESSION_SECRET = os.getenv("MINIAPP_SESSION_SECRET") or "trainer-dev-session-secret"
SESSION_MAX_AGE_SECONDS = int(os.getenv("MINIAPP_SESSION_MAX_AGE", "2592000"))
COOKIE_SECURE = os.getenv("MINIAPP_COOKIE_SECURE", "").lower() in {"1", "true", "yes", "on"}
WATCHED_EXTENSIONS = {".py", ".html", ".css", ".js", ".json", ".md"}
STORE = MiniAppStore(DB_PATH)

try:
    EXERCISE_CATALOG: list[dict[str, Any]] | None = recommender.load_catalog(STATIC_DIR)
except Exception as exc:  # noqa: BLE001
    EXERCISE_CATALOG = None
    print(f"[miniapp] WARNING: exercise catalog not loaded, recommendations disabled: {exc}")

# Minimum seconds between two manual /refresh starts for one user (debounce the
# open ios_fixed_user auth path so the paid endpoint can't be hammered).
REFRESH_MIN_INTERVAL = float(os.getenv("RECOMMENDATION_REFRESH_MIN_INTERVAL", "10"))

_recommendation_locks: dict[int, threading.Lock] = {}
_recommendation_locks_guard = threading.Lock()
_last_refresh_started: dict[int, float] = {}


def _user_recommendation_lock(user_id: int) -> threading.Lock:
    with _recommendation_locks_guard:
        lock = _recommendation_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _recommendation_locks[user_id] = lock
        return lock


def _generate_and_store_recommendation(user_id: int) -> dict[str, Any] | None:
    """Run one generation and persist it. Returns the stored 'ready' row, or
    None on failure (a 'failed' row carrying the error message is persisted)."""
    if EXERCISE_CATALOG is None:
        STORE.fail_recommendation(user_id, "Каталог упражнений недоступен")
        return None

    workouts = STORE.list_workouts(user_id)
    based_on_workout_id = STORE.get_latest_workout_id(user_id)
    body_weights = STORE.list_body_weights(user_id)
    try:
        recommendation, usage, model = recommender.generate(
            workouts,
            body_weights,
            EXERCISE_CATALOG,
            profile=recommender.load_profile(COACH_PROFILE_PATH),
            state=coach_state.load_state(COACH_STATE_PATH),
            waists=STORE.list_waists(user_id),
        )
    except recommender.RecommendationError as exc:
        STORE.fail_recommendation(user_id, str(exc))
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"[miniapp] recommendation error for user {user_id}: {exc}")
        STORE.fail_recommendation(user_id, "Внутренняя ошибка генерации рекомендации")
        return None

    return STORE.save_recommendation(
        user_id,
        based_on_workout_id,
        len(workouts),
        model,
        recommendation,
        usage.get("input_tokens"),
        usage.get("output_tokens"),
    )


def trigger_recommendation_async(user_id: int) -> None:
    """Regenerate the recommendation in the background (fire-and-forget).
    No-op if a generation for this user is already running."""
    if EXERCISE_CATALOG is None:
        return

    def _run() -> None:
        lock = _user_recommendation_lock(user_id)
        if not lock.acquire(blocking=False):
            return  # a generation is already in flight for this user
        try:
            STORE.set_recommendation_pending(user_id)
            _generate_and_store_recommendation(user_id)
        finally:
            lock.release()

    threading.Thread(target=_run, name=f"recommend-{user_id}", daemon=True).start()


def iter_watched_files() -> list[Path]:
    return [
        path
        for path in sorted(BASE_DIR.rglob("*"))
        if path.is_file()
        and path.suffix in WATCHED_EXTENSIONS
        and "__pycache__" not in path.parts
    ]


def build_dev_version() -> dict[str, object]:
    hasher = hashlib.sha1()
    latest_mtime_ns = 0
    watched_files = 0

    for path in iter_watched_files():
        stat = path.stat()
        relative_path = path.relative_to(BASE_DIR).as_posix()
        latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
        watched_files += 1
        hasher.update(relative_path.encode("utf-8"))
        hasher.update(b":")
        hasher.update(str(stat.st_mtime_ns).encode("utf-8"))
        hasher.update(b"\n")

    return {
        "dev_mode": DEV_MODE,
        "version": hasher.hexdigest()[:12],
        "latest_mtime_ns": latest_mtime_ns,
        "watched_files": watched_files,
    }


def positive_int(value: object) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if parsed is None or parsed <= 0:
        return None
    return parsed


def debug_user_enabled() -> bool:
    return DEV_MODE or ALLOW_DEBUG_USER


def make_session_value(user_id: int) -> str:
    payload = str(user_id)
    signature = hmac.new(
        SESSION_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def read_session_user_id(cookie_value: str) -> int | None:
    if not cookie_value or "." not in cookie_value:
        return None

    raw_user_id, received_signature = cookie_value.split(".", 1)
    try:
        user_id = int(raw_user_id)
    except ValueError:
        return None

    expected_signature = hmac.new(
        SESSION_SECRET.encode("utf-8"),
        raw_user_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, received_signature):
        return None

    return user_id


class MiniAppHandler(BaseHTTPRequestHandler):
    server_version = "TrainerMiniApp/0.1"

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "server_time": int(time.time()),
                    "debug_user_enabled": debug_user_enabled(),
                    "db_path": str(DB_PATH),
                },
            )
            return

        if path == "/api/dev/version":
            self._send_json(HTTPStatus.OK, build_dev_version())
            return

        if path == "/api/workouts":
            user, headers = self._resolve_current_user(allow_debug_fallback=True)
            if user is None:
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {
                        "ok": False,
                        "reason": "No active session. iOS client must resolve a session first.",
                    },
                )
                return

            workouts = STORE.list_workouts(int(user["id"]))
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "user": user, "workouts": workouts},
                extra_headers=headers,
            )
            return

        if path == "/api/body-weights":
            user, headers = self._resolve_current_user(allow_debug_fallback=True)
            if user is None:
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {
                        "ok": False,
                        "reason": "No active session. iOS client must resolve a session first.",
                    },
                )
                return

            entries = STORE.list_body_weights(int(user["id"]))
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "user": user, "entries": entries},
                extra_headers=headers,
            )
            return

        if path == "/api/recommendations/next":
            user, headers = self._resolve_current_user(allow_debug_fallback=True)
            if user is None:
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {
                        "ok": False,
                        "reason": "No active session. iOS client must resolve a session first.",
                    },
                )
                return

            self._send_json(
                HTTPStatus.OK,
                self._recommendation_response(user),
                extra_headers=headers,
            )
            return

        static_path = self._resolve_static_path(path)
        if static_path is not None:
            self._send_file(static_path)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/session/logout":
            self._send_json(
                HTTPStatus.OK,
                {"ok": True},
                extra_headers={"Set-Cookie": self._clear_session_cookie()},
            )
            return

        if path == "/api/session/resolve":
            payload = self._read_json_body()
            if payload is None:
                return

            request_shell = str(payload.get("shell", "") or "").strip().lower()
            prefers_debug_session = request_shell in {"", "browser"}
            current_user, current_headers = self._resolve_current_user()
            native_user_id = positive_int(
                payload.get("native_user_id")
                or payload.get("nativeUserId")
                or payload.get("nativeUserID")
            )

            # Native iOS fixed user: resolve the configured user id to a session.
            if request_shell == "ios" and native_user_id is not None:
                if current_user is not None and int(current_user["id"]) == native_user_id:
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "user": current_user},
                        extra_headers=current_headers,
                    )
                    return

                user = STORE.get_user_by_id(native_user_id)
                if user is None:
                    self._send_json(
                        HTTPStatus.UNAUTHORIZED,
                        {"ok": False, "reason": f"Configured iOS user #{native_user_id} was not found."},
                    )
                    return

                headers = {"Set-Cookie": self._build_session_cookie(int(user["id"]))}
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "user": user, "auth_mode": "ios_fixed_user"},
                    extra_headers=headers,
                )
                return

            # Browser/debug session (for local development).
            if debug_user_enabled():
                if current_user is not None and not (
                    prefers_debug_session and current_user.get("auth_source") != "debug"
                ):
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "user": current_user},
                        extra_headers=current_headers,
                    )
                    return

                user = STORE.ensure_debug_user(
                    DEFAULT_DEBUG_USER_ALIAS,
                    DEFAULT_DEBUG_USER_FIRST_NAME,
                    DEFAULT_DEBUG_USER_LAST_NAME,
                )
                headers = {"Set-Cookie": self._build_session_cookie(int(user["id"]))}
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "user": user, "auth_mode": "debug"},
                    extra_headers=headers,
                )
                return

            # Debug disabled: honour an existing signed cookie, otherwise reject.
            if current_user is not None:
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "user": current_user},
                    extra_headers=current_headers,
                )
                return

            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "reason": "No active session. Send shell=ios with native_user_id."},
            )
            return

        if path == "/api/workouts":
            payload = self._read_json_body()
            if payload is None:
                return

            user, headers = self._resolve_current_user(allow_debug_fallback=True)
            if user is None:
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {
                        "ok": False,
                        "reason": "No active session. iOS client must resolve a session first.",
                    },
                )
                return

            try:
                workout, created = STORE.save_workout(int(user["id"]), payload)
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": str(exc)})
                return

            self._send_json(
                HTTPStatus.CREATED if created else HTTPStatus.OK,
                {"ok": True, "created": created, "user": user, "workout": workout},
                extra_headers=headers,
            )
            if created:
                trigger_recommendation_async(int(user["id"]))
            return

        if path == "/api/body-weights":
            payload = self._read_json_body()
            if payload is None:
                return

            user, headers = self._resolve_current_user(allow_debug_fallback=True)
            if user is None:
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {
                        "ok": False,
                        "reason": "No active session. iOS client must resolve a session first.",
                    },
                )
                return

            try:
                entry, created = STORE.save_body_weight(int(user["id"]), payload)
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": str(exc)})
                return

            self._send_json(
                HTTPStatus.CREATED if created else HTTPStatus.OK,
                {"ok": True, "created": created, "user": user, "entry": entry},
                extra_headers=headers,
            )
            return

        if path == "/api/recommendations/refresh":
            user, headers = self._resolve_current_user(allow_debug_fallback=True)
            if user is None:
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {
                        "ok": False,
                        "reason": "No active session. iOS client must resolve a session first.",
                    },
                )
                return

            if EXERCISE_CATALOG is None:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "reason": "Рекомендации недоступны: каталог упражнений не загружен"},
                )
                return

            user_id = int(user["id"])
            lock = _user_recommendation_lock(user_id)
            if not lock.acquire(blocking=False):
                # Already generating (e.g. triggered by a recent workout save).
                payload = self._recommendation_response(user)
                payload["status"] = "pending"
                self._send_json(HTTPStatus.ACCEPTED, payload, extra_headers=headers)
                return

            try:
                now = time.monotonic()
                if now - _last_refresh_started.get(user_id, 0.0) < REFRESH_MIN_INTERVAL:
                    payload = self._recommendation_response(user)
                    payload["reason"] = "Слишком частый запрос, отдаю текущую рекомендацию"
                    self._send_json(HTTPStatus.OK, payload, extra_headers=headers)
                    return
                _last_refresh_started[user_id] = now
                STORE.set_recommendation_pending(user_id)
                result = _generate_and_store_recommendation(user_id)
            finally:
                lock.release()

            if result is None:
                rec = STORE.get_recommendation(user_id)
                reason = (rec or {}).get("error") or "Не удалось сгенерировать рекомендацию"
                payload = {"ok": False, "user": user, "reason": reason}
                if rec is not None:
                    payload.update(rec)
                self._send_json(HTTPStatus.BAD_GATEWAY, payload, extra_headers=headers)
                return

            latest = STORE.get_latest_workout_id(user_id)
            stale = bool(result.get("based_on_workout_id") != latest)
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "user": user, "stale": stale, **result},
                extra_headers=headers,
            )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Not found"})

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        workout_id = self._parse_workout_id(path)
        if workout_id is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Not found"})
            return

        payload = self._read_json_body()
        if payload is None:
            return

        user, headers = self._resolve_current_user(allow_debug_fallback=True)
        if user is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {
                    "ok": False,
                    "reason": "No active session. iOS client must resolve a session first.",
                },
            )
            return

        try:
            workout = STORE.update_workout(int(user["id"]), workout_id, payload)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": str(exc)})
            return

        if workout is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Workout not found"})
            return

        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "user": user, "workout": workout},
            extra_headers=headers,
        )
        trigger_recommendation_async(int(user["id"]))

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        body_weight_id = self._parse_body_weight_id(path)
        if body_weight_id is not None:
            user, headers = self._resolve_current_user(allow_debug_fallback=True)
            if user is None:
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {
                        "ok": False,
                        "reason": "No active session. iOS client must resolve a session first.",
                    },
                )
                return

            entry = STORE.delete_body_weight(int(user["id"]), body_weight_id)
            if entry is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Body weight entry not found"})
                return

            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "user": user, "entry": entry, "deleted": True},
                extra_headers=headers,
            )
            return

        workout_id = self._parse_workout_id(path)
        if workout_id is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Not found"})
            return

        user, headers = self._resolve_current_user(allow_debug_fallback=True)
        if user is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {
                    "ok": False,
                    "reason": "No active session. iOS client must resolve a session first.",
                },
            )
            return

        workout = STORE.delete_workout(int(user["id"]), workout_id)
        if workout is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Workout not found"})
            return

        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "user": user, "workout": workout, "deleted": True},
            extra_headers=headers,
        )
        trigger_recommendation_async(int(user["id"]))

    def log_message(self, format: str, *args: object) -> None:
        print(f"[miniapp] {self.address_string()} - {format % args}")

    def _read_json_body(self) -> dict[str, Any] | None:
        content_length = int(self.headers.get("Content-Length", "0"))
        payload_raw = self.rfile.read(content_length).decode("utf-8")
        try:
            return json.loads(payload_raw or "{}")
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": "Invalid JSON body"})
            return None

    def _recommendation_response(self, user: dict[str, Any]) -> dict[str, Any]:
        user_id = int(user["id"])
        rec = STORE.get_recommendation(user_id)
        if rec is None:
            return {
                "ok": True,
                "user": user,
                "status": "none",
                "recommendation": None,
                "stale": False,
            }
        latest = STORE.get_latest_workout_id(user_id)
        stale = bool(
            rec.get("status") == "ready" and rec.get("based_on_workout_id") != latest
        )
        return {"ok": True, "user": user, "stale": stale, **rec}

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, object],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)

    def _send_file(self, file_path: Path) -> None:
        if not file_path.exists():
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Missing asset"})
            return

        body = file_path.read_bytes()
        guessed_content_type, _ = mimetypes.guess_type(str(file_path))
        content_type = guessed_content_type or "application/octet-stream"
        if content_type.startswith("text/"):
            content_type = f"{content_type}; charset=utf-8"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _resolve_static_path(self, raw_path: str) -> Path | None:
        requested_path = "index.html" if raw_path == "/" else raw_path.lstrip("/")
        candidate = (STATIC_DIR / requested_path).resolve()

        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return None

        if candidate.is_file():
            return candidate

        return None

    def _parse_workout_id(self, path: str) -> int | None:
        prefix = "/api/workouts/"
        if not path.startswith(prefix):
            return None

        raw_id = path.removeprefix(prefix).strip("/")
        if not raw_id or "/" in raw_id:
            return None

        try:
            return int(raw_id)
        except ValueError:
            return None

    def _parse_body_weight_id(self, path: str) -> int | None:
        prefix = "/api/body-weights/"
        if not path.startswith(prefix):
            return None

        raw_id = path.removeprefix(prefix).strip("/")
        if not raw_id or "/" in raw_id:
            return None

        try:
            return int(raw_id)
        except ValueError:
            return None

    def _build_session_cookie(self, user_id: int) -> str:
        parts = [
            f"{SESSION_COOKIE_NAME}={make_session_value(user_id)}",
            "HttpOnly",
            "Path=/",
            f"Max-Age={SESSION_MAX_AGE_SECONDS}",
            "SameSite=Lax",
        ]
        if COOKIE_SECURE:
            parts.append("Secure")
        return "; ".join(parts)

    def _clear_session_cookie(self) -> str:
        parts = [
            f"{SESSION_COOKIE_NAME}=",
            "HttpOnly",
            "Path=/",
            "Max-Age=0",
            "SameSite=Lax",
        ]
        if COOKIE_SECURE:
            parts.append("Secure")
        return "; ".join(parts)

    def _resolve_current_user(
        self,
        allow_debug_fallback: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        cookie_header = self.headers.get("Cookie", "")
        if cookie_header:
            cookies = SimpleCookie()
            cookies.load(cookie_header)
            raw_cookie = cookies.get(SESSION_COOKIE_NAME)
            if raw_cookie is not None:
                user_id = read_session_user_id(raw_cookie.value)
                if user_id is not None:
                    user = STORE.get_user_by_id(user_id)
                    if user is not None:
                        return user, {}

        if allow_debug_fallback and debug_user_enabled():
            user = STORE.ensure_debug_user(
                DEFAULT_DEBUG_USER_ALIAS,
                DEFAULT_DEBUG_USER_FIRST_NAME,
                DEFAULT_DEBUG_USER_LAST_NAME,
            )
            return user, {"Set-Cookie": self._build_session_cookie(int(user["id"]))}

        return None, {}


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), MiniAppHandler)
    print(f"Trainer backend listening on http://{HOST}:{PORT}")
    print(f"SQLite database: {DB_PATH}")
    if DEV_MODE:
        print("Mini App dev mode: enabled")
    if debug_user_enabled():
        print("Browser debug user mode: enabled")
    server.serve_forever()


if __name__ == "__main__":
    main()
