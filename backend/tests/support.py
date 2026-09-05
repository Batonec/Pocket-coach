from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import Message
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
MINIAPP_DIR = ROOT_DIR / "backend"
CATALOG_PATH = MINIAPP_DIR / "resources" / "exercises.json"

if str(MINIAPP_DIR) not in sys.path:
    sys.path.insert(0, str(MINIAPP_DIR))
# Тесты импортируют server.py и пакет trainer как модули, и без этой строки после
# каждого прогона в корне backend/ и в пакете оставались бы __pycache__.
sys.dont_write_bytecode = True


def sample_workout_payload(
    *,
    client_id: str,
    workout_date: str = "2026-03-28",
    exercise_id: int = 1,
    exercise_name: str = "Bench Press",
    weight: float = 80.0,
    reps: int = 12,
    effort: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    return {
        "client_id": client_id,
        "workout_date": workout_date,
        "plan_id": None,
        "data": {
            "notes": notes,
            "load_type": None,
            "exercises": [
                {
                    "exercise_id": exercise_id,
                    "name": exercise_name,
                    "sets": [
                        {
                            "reps": reps,
                            "weight": weight,
                            "effort": effort,
                            "notes": notes,
                        }
                    ],
                }
            ],
        },
    }


def sample_body_weight_payload(
    *,
    entry_date: str = "2026-03-28",
    weight: float = 82.4,
    notes: str | None = None,
) -> dict[str, Any]:
    return {
        "entry_date": entry_date,
        "weight": weight,
        "notes": notes,
    }


@contextmanager
def temporary_env(values: dict[str, str]) -> Iterator[None]:
    previous: dict[str, str | None] = {}
    for key, value in values.items():
        previous[key] = os.environ.get(key)
        os.environ[key] = value

    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def load_server_module(
    *,
    db_path: Path,
    catalog_path: Path = CATALOG_PATH,
    allow_debug_user: bool = True,
    dev_mode: bool = False,
    session_secret: str = "trainer-test-session-secret",
) -> Any:
    env = {
        "EXERCISE_CATALOG_PATH": str(catalog_path),
        "MINIAPP_HOST": "127.0.0.1",
        "MINIAPP_PORT": "0",
        "MINIAPP_DB_PATH": str(db_path),
        "MINIAPP_DEV_MODE": "1" if dev_mode else "0",
        "MINIAPP_ALLOW_DEBUG_USER": "1" if allow_debug_user else "0",
        "MINIAPP_SESSION_SECRET": session_secret,
        "MINIAPP_COOKIE_SECURE": "0",
        "MINIAPP_DEFAULT_DEBUG_USER_ALIAS": "browser-default",
        "MINIAPP_DEFAULT_DEBUG_USER_FIRST_NAME": "Browser",
        "MINIAPP_DEFAULT_DEBUG_USER_LAST_NAME": "Debug",
    }

    with temporary_env(env):
        if "server" in sys.modules:
            return importlib.reload(sys.modules["server"])
        return importlib.import_module("server")


@dataclass
class RunningMiniApp:
    base_url: str
    module: Any
    httpd: Any
    thread: threading.Thread
    temp_dir: tempfile.TemporaryDirectory[str]

    def close(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()
        self.temp_dir.cleanup()


@contextmanager
def running_miniapp_server(
    *,
    catalog_path: Path = CATALOG_PATH,
    allow_debug_user: bool = True,
    dev_mode: bool = False,
    session_secret: str = "trainer-test-session-secret",
) -> Iterator[RunningMiniApp]:
    temp_dir = tempfile.TemporaryDirectory()
    db_path = Path(temp_dir.name) / "trainer.db"
    module = load_server_module(
        db_path=db_path,
        catalog_path=catalog_path,
        allow_debug_user=allow_debug_user,
        dev_mode=dev_mode,
        session_secret=session_secret,
    )

    httpd = module.ThreadingHTTPServer(("127.0.0.1", 0), module.MiniAppHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    running = RunningMiniApp(
        base_url=f"http://127.0.0.1:{httpd.server_port}",
        module=module,
        httpd=httpd,
        thread=thread,
        temp_dir=temp_dir,
    )

    try:
        yield running
    finally:
        running.close()


@dataclass
class JsonResponse:
    status: int
    payload: dict[str, Any]
    headers: Message


class JsonHttpClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.cookie_jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> JsonResponse:
        data = None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        # base_url собирает сам тест — это всегда http://127.0.0.1.
        request = urllib.request.Request(  # noqa: S310
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )

        try:
            with self.opener.open(request, timeout=10) as response:
                return JsonResponse(
                    status=response.status,
                    payload=json.loads(response.read().decode("utf-8")),
                    headers=response.headers,
                )
        except urllib.error.HTTPError as exc:
            return JsonResponse(
                status=exc.code,
                payload=json.loads(exc.read().decode("utf-8")),
                headers=exc.headers,
            )
