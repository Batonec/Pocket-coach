#!/usr/bin/env python3
"""HTTP API тренера: процесс, который крутит systemd.

``BaseHTTPRequestHandler`` без фреймворка: ``do_*`` отдают запрос в ``_dispatch``,
тот ищет обработчик в таблицах ``ROUTES`` (метод + точный путь) и ``ID_ROUTES``
(``/api/<коллекция>/<id>``). Один эндпоинт — один метод ``_get_*`` / ``_post_*`` /
``_put_*`` / ``_delete_*``; новый эндпоинт — это метод плюс строка в таблице.
Нормализация входа живёт в ``trainer.domain.rules``, SQL — в ``backend_store``:
здесь только коды ответов, cookie сессии и фоновая генерация совета.

Клиент один — iOS (``shell=ios`` плюс фиксированный id пользователя); debug-сессия
по cookie осталась для локальной разработки (``MINIAPP_ALLOW_DEBUG_USER``).
Настройки — переменные окружения ``MINIAPP_*`` и ``COACH_*``, см. backend/README.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import threading
import time
from collections.abc import Callable
from datetime import date
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from trainer import RESOURCES_DIR
from trainer.data import files
from trainer.data.backend_store import MiniAppStore
from trainer.domain import coach_signals, limits, recommender, rules

BASE_DIR = Path(__file__).resolve().parent
# Каталог упражнений едет с кодом; путь переопределяется только ради тестов и
# нестандартных стендов.
CATALOG_PATH = Path(os.getenv("EXERCISE_CATALOG_PATH", str(RESOURCES_DIR / "exercises.json")))
DATA_DIR = BASE_DIR / "data"
HOST = os.getenv("MINIAPP_HOST", "127.0.0.1")
PORT = int(os.getenv("MINIAPP_PORT", "8080"))
DEV_MODE = os.getenv("MINIAPP_DEV_MODE", "").lower() in {"1", "true", "yes", "on"}
ALLOW_DEBUG_USER = os.getenv("MINIAPP_ALLOW_DEBUG_USER", "").lower() in {"1", "true", "yes", "on"}
DEFAULT_DEBUG_USER_ALIAS = os.getenv("MINIAPP_DEFAULT_DEBUG_USER_ALIAS", "browser-default")
DEFAULT_DEBUG_USER_FIRST_NAME = os.getenv("MINIAPP_DEFAULT_DEBUG_USER_FIRST_NAME", "Browser")
DEFAULT_DEBUG_USER_LAST_NAME = os.getenv("MINIAPP_DEFAULT_DEBUG_USER_LAST_NAME", "Debug")
DB_PATH = Path(os.getenv("MINIAPP_DB_PATH", str(DATA_DIR / "trainer.db")))
# Профиль атлета для промпта тренера: личный и медицинский контекст, живёт только
# на сервере рядом с базой (в публичном репозитории его нет).
COACH_PROFILE_PATH = Path(
    os.getenv("COACH_PROFILE_PATH", str(DB_PATH.parent / "coach_profile.json"))
)
COACH_STRATEGY_PATH = Path(
    os.getenv("COACH_STRATEGY_PATH", str(DB_PATH.parent / "coach_strategy.md"))
)
# Изменяемое состояние подготовки (фаза, лимиты талии) — та же политика
# расположения, что у профиля; переключается инструментами Coach MCP.
COACH_STATE_PATH = files.default_state_path(DB_PATH)
SESSION_COOKIE_NAME = "trainer_session"
SESSION_SECRET = os.getenv("MINIAPP_SESSION_SECRET") or "trainer-dev-session-secret"
SESSION_MAX_AGE_SECONDS = int(os.getenv("MINIAPP_SESSION_MAX_AGE", "2592000"))
COOKIE_SECURE = os.getenv("MINIAPP_COOKIE_SECURE", "").lower() in {"1", "true", "yes", "on"}
WATCHED_EXTENSIONS = {".py", ".html", ".css", ".js", ".json", ".md"}
STORE = MiniAppStore(DB_PATH)

try:
    EXERCISE_CATALOG: list[dict[str, Any]] | None = files.load_catalog(CATALOG_PATH)
except Exception as exc:  # noqa: BLE001
    EXERCISE_CATALOG = None
    print(f"[miniapp] WARNING: exercise catalog not loaded, recommendations disabled: {exc}")

# Минимум секунд между двумя ручными стартами /refresh для одного пользователя:
# путь авторизации ios_fixed_user открытый, и платный эндпоинт нельзя долбить.
REFRESH_MIN_INTERVAL = float(os.getenv("RECOMMENDATION_REFRESH_MIN_INTERVAL", "10"))

_recommendation_locks: dict[int, threading.Lock] = {}
_recommendation_locks_guard = threading.Lock()
_recommendation_workers: set[int] = set()
_recommendation_rerun_requested: set[int] = set()
_last_refresh_started: dict[int, float] = {}


def _user_recommendation_lock(user_id: int) -> threading.Lock:
    """Замок генерации на пользователя: один объект и для фонового воркера, и для
    ручного ``/refresh``, чтобы модель никогда не звалась дважды параллельно для
    одного атлета.
    """
    with _recommendation_locks_guard:
        lock = _recommendation_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _recommendation_locks[user_id] = lock
        return lock


def _generate_and_store_recommendation(user_id: int) -> dict[str, Any] | None:
    """Одна генерация совета и её запись: история и замеры из базы, профиль,
    стратегия и состояние с диска → ``recommender.generate`` → строка кэша.
    Возвращает записанную строку ``ready`` или ``None`` при ошибке (строка
    ``failed`` с текстом ошибки записана). Зовут фоновый воркер и
    ``_post_recommendation_refresh``.
    """
    if EXERCISE_CATALOG is None:
        STORE.fail_recommendation(user_id, "Каталог упражнений недоступен")
        return None

    workouts = STORE.list_workouts(user_id)
    based_on_workout_id = STORE.get_latest_workout_id(user_id)
    body_weights = STORE.list_body_weights(user_id)
    # Строка кэша к этому моменту уже pending, но прошлый payload в ней цел:
    # это память карточки о себе для нового промпта.
    previous = STORE.get_recommendation(user_id)
    try:
        recommendation, usage, model = recommender.generate(
            workouts,
            body_weights,
            EXERCISE_CATALOG,
            profile=files.load_profile(COACH_PROFILE_PATH),
            strategy=files.load_strategy(COACH_STRATEGY_PATH),
            state=files.load_state(COACH_STATE_PATH),
            waists=STORE.list_waists(user_id),
            events=STORE.list_events(user_id),
            previous=previous,
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
    """Пересобрать совет в фоне (fire-and-forget) после правки данных; зовёт
    ``_advice_changed``.

    Одновременные триггеры схлопываются в один добавочный прогон по самым свежим
    данным: правки тренировок и замеров часто приходят, пока прошлая генерация ещё
    идёт. Модель для одного пользователя никогда не зовётся параллельно, но и
    последняя правка никогда не теряется, пока работает старая генерация.
    """
    # Без истории валидного совета на следующую тренировку нет. Воркер ниже всё
    # равно запускаем: старая генерация может прямо сейчас сохранять payload по
    # только что удалённой тренировке, и добавочный прогон обязан снести эту
    # протухшую строку ещё раз.
    has_workouts = STORE.get_latest_workout_id(user_id) is not None
    if not has_workouts:
        STORE.clear_recommendation(user_id)
    if EXERCISE_CATALOG is None:
        return
    if has_workouts:
        # Pending виден синхронно клиенту, который опрашивает сразу после ответа
        # на правку. Прошлый payload остаётся как фолбэк для показа.
        STORE.set_recommendation_pending(user_id)

    with _recommendation_locks_guard:
        _recommendation_rerun_requested.add(user_id)
        if user_id in _recommendation_workers:
            return
        _recommendation_workers.add(user_id)

    def _run() -> None:
        """Воркер: держит замок пользователя и гоняет генерацию, пока приходят
        новые запросы прогона; выходя, снимает с себя флаг воркера."""
        lock = _user_recommendation_lock(user_id)
        lock.acquire()  # тот же замок прямо сейчас может держать ручной /refresh
        try:
            while True:
                with _recommendation_locks_guard:
                    _recommendation_rerun_requested.discard(user_id)
                if STORE.get_latest_workout_id(user_id) is None:
                    STORE.clear_recommendation(user_id)
                else:
                    STORE.set_recommendation_pending(user_id)
                    _generate_and_store_recommendation(user_id)

                # Правка, пришедшая во время генерации, просит ровно один
                # добавочный прогон. Несколько быстрых правок схлопываются в него.
                with _recommendation_locks_guard:
                    if user_id in _recommendation_rerun_requested:
                        continue
                    _recommendation_workers.discard(user_id)
                    lock.release()
                    return
        except Exception as exc:  # noqa: BLE001
            print(f"[miniapp] recommendation worker error for user {user_id}: {exc}")
            with _recommendation_locks_guard:
                _recommendation_workers.discard(user_id)
                _recommendation_rerun_requested.discard(user_id)
                lock.release()

    threading.Thread(target=_run, name=f"recommend-{user_id}", daemon=True).start()


def iter_watched_files() -> list[Path]:
    """Файлы, по которым считается dev-версия: код, проза и JSON под backend/, без ``__pycache__``."""
    return [
        path
        for path in sorted(BASE_DIR.rglob("*"))
        if path.is_file() and path.suffix in WATCHED_EXTENSIONS and "__pycache__" not in path.parts
    ]


def build_dev_version() -> dict[str, object]:
    # sha1 здесь — не подпись, а дешёвый отпечаток «путь + mtime» для
    # cache-busting в деве. usedforsecurity=False говорит это и линтеру,
    # и FIPS-сборкам OpenSSL, где иначе sha1 недоступен вовсе.
    """Отпечаток исходников для ``/api/dev/version``: хеш «путь + mtime» всех
    наблюдаемых файлов, самый свежий mtime и их число. Клиент в дев-режиме
    сравнивает версию и перезагружается.
    """
    hasher = hashlib.sha1(usedforsecurity=False)
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


def positive_int(value: Any) -> int | None:
    """Значение как положительный ``int`` или ``None`` (мусор, ноль, отрицательное)."""
    try:
        parsed = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if parsed is None or parsed <= 0:
        return None
    return parsed


def debug_user_enabled() -> bool:
    """Разрешён ли debug-пользователь: дев-режим или явный ``MINIAPP_ALLOW_DEBUG_USER``."""
    return DEV_MODE or ALLOW_DEBUG_USER


def make_session_value(user_id: int) -> str:
    """Значение cookie сессии: ``<user_id>.<HMAC-SHA256 от id на секрете сессии>``."""
    payload = str(user_id)
    signature = hmac.new(
        SESSION_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def read_session_user_id(cookie_value: str) -> int | None:
    """Id пользователя из значения cookie, если подпись сходится; иначе ``None``.
    Сравнение подписи постоянное по времени.
    """
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


def _path_id(path: str, prefix: str) -> int | None:
    """Целый id из пути вида «/api/<коллекция>/<id>»; None — это не тот маршрут."""
    if not path.startswith(prefix):
        return None
    raw_id = path.removeprefix(prefix).strip("/")
    if not raw_id or "/" in raw_id:
        return None
    try:
        return int(raw_id)
    except ValueError:
        return None


class MiniAppHandler(BaseHTTPRequestHandler):
    """Обработчик одного HTTP-запроса: экземпляр на запрос, поток на соединение
    (``ThreadingHTTPServer``). Состояние процесса — модульные ``STORE`` и замки
    генерации.
    """

    server_version = "TrainerMiniApp/0.1"

    def do_OPTIONS(self) -> None:
        """CORS preflight: разрешить всё. Наследие браузерной версии, iOS его не шлёт."""
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        """Все методы идут в ``_dispatch``; обработчики перечислены в ``ROUTES`` и ``ID_ROUTES``."""
        self._dispatch("GET")

    def do_POST(self) -> None:
        """См. ``do_GET``."""
        self._dispatch("POST")

    def do_PUT(self) -> None:
        """См. ``do_GET``."""
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        """См. ``do_GET``."""
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        """Один эндпоинт — один метод класса; какой именно, решает ROUTES (точный
        путь) или ID_ROUTES (/api/<коллекция>/<id>); всё остальное — 404."""
        path = urlparse(self.path).path
        handler = ROUTES.get((method, path))
        if handler is not None:
            handler(self)
            return
        for (route_method, prefix), id_handler in ID_ROUTES.items():
            if route_method != method:
                continue
            entity_id = _path_id(path, prefix)
            if entity_id is not None:
                id_handler(self, entity_id)
                return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Not found"})

    # --- GET -----------------------------------------------------------------------
    def _get_health(self) -> None:
        """``GET /api/health``: время сервера, включён ли debug-пользователь, путь к базе. Без сессии."""
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "server_time": int(time.time()),
                "debug_user_enabled": debug_user_enabled(),
                "db_path": str(DB_PATH),
            },
        )

    def _get_dev_version(self) -> None:
        """``GET /api/dev/version``: отпечаток исходников (``build_dev_version``). Без сессии."""
        self._send_json(HTTPStatus.OK, build_dev_version())

    def _get_exercise_catalog(self) -> None:
        # URL остался от веб-версии и зашит в iOS-клиент: файл переехал, адрес нет.
        """``GET /data/exercises.json``: каталог упражнений файлом из resources/. Без сессии."""
        self._send_file(CATALOG_PATH)

    def _get_workouts(self) -> None:
        """``GET /api/workouts``: все тренировки пользователя от новых к старым, с payload целиком."""
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        workouts = STORE.list_workouts(int(user["id"]))
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "user": user, "workouts": workouts},
            extra_headers=headers,
        )

    def _get_body_weights(self) -> None:
        """``GET /api/body-weights``: все взвешивания от старых к новым."""
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        entries = STORE.list_body_weights(int(user["id"]))
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "user": user, "entries": entries},
            extra_headers=headers,
        )

    def _get_recommendation_next(self) -> None:
        """``GET /api/recommendations/next``: текущий совет со статусом и флагом
        ``stale`` (собран не по последней тренировке). Никогда не генерирует.
        """
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        self._send_json(
            HTTPStatus.OK,
            self._recommendation_response(user),
            extra_headers=headers,
        )

    def _get_weekly_report(self) -> None:
        """``GET /api/reports/weekly``: последний кэшированный недельный отчёт или ``None``."""
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        # Только кэш, намеренно: отчёты генерируют таймер в ночь на понедельник
        # (weekly_report.py) или инструмент Coach MCP; этот эндпоинт токенов не
        # тратит.
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "report": STORE.get_latest_coach_report(int(user["id"]))},
            extra_headers=headers,
        )

    def _get_weekly_report_history(self) -> None:
        """``GET /api/reports/weekly/history``: весь кэш недельных отчётов, новые
        сверху, с телами — экран «Все отчёты» открывает любую неделю без второго
        запроса. Токенов не тратит, как и ``/api/reports/weekly``.
        """
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "reports": STORE.list_coach_reports(int(user["id"]))},
            extra_headers=headers,
        )

    def _get_measurements(self) -> None:
        """``GET /api/measurements``: обхваты кроме талии от старых к новым, все виды."""
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "user": user,
                "kinds": limits.MEASUREMENT_KINDS,
                "entries": STORE.list_measurements(int(user["id"])),
            },
            extra_headers=headers,
        )

    def _get_waists(self) -> None:
        """``GET /api/waists``: все замеры талии от старых к новым."""
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "user": user, "entries": STORE.list_waists(int(user["id"]))},
            extra_headers=headers,
        )

    def _get_events(self) -> None:
        """``GET /api/events``: все события (перерывы с причиной) пользователя."""
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "user": user, "events": STORE.list_events(int(user["id"]))},
            extra_headers=headers,
        )

    def _get_coach_signals(self) -> None:
        """``GET /api/coach/signals``: баннеры без LLM, посчитанные
        ``coach_signals.compute_signals`` по базе и состоянию подготовки прямо сейчас.
        """
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        signals = coach_signals.compute_signals(
            STORE,
            int(user["id"]),
            files.load_state(COACH_STATE_PATH),
        )
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "generated_at": int(time.time()), "signals": signals},
            extra_headers=headers,
        )

    # --- POST ----------------------------------------------------------------------
    def _post_session_logout(self) -> None:
        """``POST /api/session/logout``: погасить cookie сессии."""
        self._send_json(
            HTTPStatus.OK,
            {"ok": True},
            extra_headers={"Set-Cookie": self._clear_session_cookie()},
        )

    def _post_session_resolve(self) -> None:
        """``POST /api/session/resolve``: превратить клиента в сессию.

        Три ветки: iOS (``shell=ios`` плюс ``native_user_id`` настроенного пользователя →
        подписанная cookie; 401, если такого пользователя нет); debug-сессия, когда она
        разрешена (браузер локальной разработки получает debug-пользователя); иначе
        только уже существующая валидная cookie, без неё 401.
        """
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

        # Нативный iOS с фиксированным пользователем: настроенный id → сессия.
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
                    {
                        "ok": False,
                        "reason": f"Configured iOS user #{native_user_id} was not found.",
                    },
                )
                return

            headers = {"Set-Cookie": self._build_session_cookie(int(user["id"]))}
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "user": user, "auth_mode": "ios_fixed_user"},
                extra_headers=headers,
            )
            return

        # Браузерная debug-сессия (локальная разработка).
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

        # Debug выключен: принимаем существующую подписанную cookie, иначе отказ.
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

    def _post_workout(self) -> None:
        """``POST /api/workouts``: записать тренировку. Повтор с тем же ``client_id``
        не создаёт дубль (200 вместо 201). Новая сегодняшняя тренировка закрывает
        открытое событие (``rules.open_event_end_after_workout``); совет
        пересобирается в фоне.
        """
        payload = self._read_json_body()
        if payload is None:
            return

        session = self._require_user()
        if session is None:
            return
        user, headers = session

        try:
            workout, created = STORE.save_workout(int(user["id"]), payload)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": str(exc)})
            return

        closes_event_on = rules.open_event_end_after_workout(
            workout["workout_date"], created, date.today()
        )
        if closes_event_on:
            STORE.close_open_event(int(user["id"]), closes_event_on)

        self._send_json(
            HTTPStatus.CREATED if created else HTTPStatus.OK,
            {"ok": True, "created": created, "user": user, "workout": workout},
            extra_headers=headers,
        )
        self._advice_changed(int(user["id"]), "workout", created=created)

    def _post_body_weight(self) -> None:
        """``POST /api/body-weights``: взвешивание за дату (повтор за тот же день
        обновляет запись); совет пересобирается в фоне.
        """
        payload = self._read_json_body()
        if payload is None:
            return

        session = self._require_user()
        if session is None:
            return
        user, headers = session

        try:
            entry, created = STORE.save_body_weight(int(user["id"]), payload)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": str(exc)})
            return

        self._advice_changed(int(user["id"]), "body_weight")
        self._send_json(
            HTTPStatus.CREATED if created else HTTPStatus.OK,
            {"ok": True, "created": created, "user": user, "entry": entry},
            extra_headers=headers,
        )

    def _post_measurement(self) -> None:
        """``POST /api/measurements``: обхват вида за дату, один на день. Совет не
        пересобирается: план обхваты не читает, это вход недельного отчёта."""
        payload = self._read_json_body()
        if payload is None:
            return

        session = self._require_user()
        if session is None:
            return
        user, headers = session

        try:
            entry, created = STORE.save_measurement(int(user["id"]), payload)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": str(exc)})
            return

        self._advice_changed(int(user["id"]), "measurement")
        self._send_json(
            HTTPStatus.CREATED if created else HTTPStatus.OK,
            {"ok": True, "created": created, "user": user, "entry": entry},
            extra_headers=headers,
        )

    def _post_waist(self) -> None:
        """``POST /api/waists``: замер талии за дату, один на день; совет пересобирается в фоне."""
        payload = self._read_json_body()
        if payload is None:
            return

        session = self._require_user()
        if session is None:
            return
        user, headers = session

        try:
            entry, created = STORE.save_waist(int(user["id"]), payload)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": str(exc)})
            return

        self._advice_changed(int(user["id"]), "waist")
        self._send_json(
            HTTPStatus.CREATED if created else HTTPStatus.OK,
            {"ok": True, "created": created, "user": user, "entry": entry},
            extra_headers=headers,
        )

    def _post_event(self) -> None:
        """``POST /api/events``: новое событие (перерыв с причиной); валидация в
        ``rules``, второе открытое событие — 400.
        """
        payload = self._read_json_body()
        if payload is None:
            return

        session = self._require_user()
        if session is None:
            return
        user, headers = session

        try:
            event = STORE.save_event(int(user["id"]), payload)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": str(exc)})
            return

        # Событие уезжает в промпт тренера текстом, поэтому любая его правка
        # обесценивает готовый совет ровно так же, как новый замер.
        self._advice_changed(int(user["id"]), "event")
        # Всегда 201: ключа дедупа у события нет, save_event только вставляет.
        self._send_json(
            HTTPStatus.CREATED,
            {"ok": True, "user": user, "event": event},
            extra_headers=headers,
        )

    def _post_signal_dismiss(self) -> None:
        """``POST /api/coach/signals/dismiss``: отложить баннер по ``instance_key``.

        Срок отсрочки считает ``coach_signals.snooze_until_for`` по живому сигналу:
        критичный баннер отложить нельзя (409), кривые часы — 400. Отсрочка пишется в
        базу и действует на всех клиентах.
        """
        payload = self._read_json_body()
        if payload is None:
            return

        session = self._require_user()
        if session is None:
            return
        user, headers = session

        instance_key = str(payload.get("instance_key") or "").strip()
        if not instance_key:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"ok": False, "reason": "instance_key is required"}
            )
            return

        user_id = int(user["id"])
        now_ts = int(time.time())
        active = coach_signals.compute_signals(
            STORE, user_id, files.load_state(COACH_STATE_PATH), now_ts=now_ts
        )
        matched = next(
            (signal for signal in active if signal["instance_key"] == instance_key),
            None,
        )
        try:
            snooze_until = coach_signals.snooze_until_for(
                matched, payload.get("snooze_hours"), now_ts
            )
        except coach_signals.CriticalSignalDismissed as exc:
            self._send_json(
                HTTPStatus.CONFLICT, {"ok": False, "reason": str(exc)}, extra_headers=headers
            )
            return
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": str(exc)})
            return

        STORE.save_signal_snooze(user_id, instance_key, snooze_until)
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "instance_key": instance_key, "snooze_until": snooze_until},
            extra_headers=headers,
        )

    def _post_weekly_report_read(self) -> None:
        """``POST /api/reports/weekly/read``: отметить последний отчёт прочитанным;
        гасит баннер weekly_report_ready у всех клиентов.
        """
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        marked = STORE.mark_coach_report_read(int(user["id"]))
        self._send_json(HTTPStatus.OK, {"ok": True, "read": marked}, extra_headers=headers)

    def _post_recommendation_refresh(self) -> None:
        """``POST /api/recommendations/refresh``: ручная генерация совета, синхронно
        (клиент ждёт до 90 с).

        Уже идёт генерация — 202 с текущим payload и статусом pending; повтор раньше
        ``REFRESH_MIN_INTERVAL`` — 200 с текущим советом и причиной; ошибка модели —
        502 с текстом из строки ``failed``, и кулдаун сбрасывается, чтобы карточка
        ошибки могла повторить сразу. 503, если каталог упражнений не загрузился.
        """
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        if EXERCISE_CATALOG is None:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "ok": False,
                    "reason": "Рекомендации недоступны: каталог упражнений не загружен",
                },
            )
            return

        user_id = int(user["id"])
        lock = _user_recommendation_lock(user_id)
        if not lock.acquire(blocking=False):
            # Генерация уже идёт (например, её запустило сохранение тренировки).
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
            # Упавшую генерацию карточка ошибки должна уметь повторить сразу;
            # кулдаун от долбёжки защищает только успешные или ещё актуальные
            # обновления.
            _last_refresh_started.pop(user_id, None)
            rec = STORE.get_recommendation(user_id)
            reason = (rec or {}).get("error") or "Не удалось сгенерировать рекомендацию"
            payload = {"ok": False, "user": user, "reason": reason}
            if rec is not None:
                payload.update(rec)
            self._send_json(HTTPStatus.BAD_GATEWAY, payload, extra_headers=headers)
            return

        stale = recommender.is_stale(result, STORE.get_latest_workout_id(user_id))
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "user": user, "stale": stale, **result},
            extra_headers=headers,
        )

    # --- PUT -----------------------------------------------------------------------
    def _put_event(self, event_id: int) -> None:
        """``PUT /api/events/<id>``: перезаписать событие; 404, если его нет, 400 на
        второе открытое; совет пересобирается.
        """
        payload = self._read_json_body()
        if payload is None:
            return

        session = self._require_user()
        if session is None:
            return
        user, headers = session

        try:
            event = STORE.update_event(int(user["id"]), event_id, payload)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": str(exc)})
            return

        if event is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Event not found"})
            return

        self._advice_changed(int(user["id"]), "event")
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "user": user, "event": event},
            extra_headers=headers,
        )

    def _put_workout(self, workout_id: int) -> None:
        """``PUT /api/workouts/<id>``: перезаписать тренировку (правка сетов и заметок в
        iOS); 404, если её нет; совет пересобирается.
        """
        payload = self._read_json_body()
        if payload is None:
            return

        session = self._require_user()
        if session is None:
            return
        user, headers = session

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
        self._advice_changed(int(user["id"]), "workout")

    # --- DELETE --------------------------------------------------------------------
    def _delete_measurement(self, entry_id: int) -> None:
        """``DELETE /api/measurements/<id>``: удалить обхват; 404, если его нет."""
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        entry = STORE.delete_measurement(int(user["id"]), entry_id)
        if entry is None:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Measurement entry not found"}
            )
            return

        self._advice_changed(int(user["id"]), "measurement", created=False)
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "user": user, "entry": entry, "deleted": True},
            extra_headers=headers,
        )

    def _delete_waist(self, waist_id: int) -> None:
        """``DELETE /api/waists/<id>``: удалить замер талии; 404, если его нет; совет пересобирается."""
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        entry = STORE.delete_waist(int(user["id"]), waist_id)
        if entry is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Waist entry not found"})
            return

        self._advice_changed(int(user["id"]), "waist")
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "user": user, "entry": entry, "deleted": True},
            extra_headers=headers,
        )

    def _delete_body_weight(self, body_weight_id: int) -> None:
        """``DELETE /api/body-weights/<id>``: удалить взвешивание; 404, если его нет; совет пересобирается."""
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        entry = STORE.delete_body_weight(int(user["id"]), body_weight_id)
        if entry is None:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Body weight entry not found"}
            )
            return

        self._advice_changed(int(user["id"]), "body_weight")
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "user": user, "entry": entry, "deleted": True},
            extra_headers=headers,
        )

    def _delete_event(self, event_id: int) -> None:
        """``DELETE /api/events/<id>``: удалить событие; 404, если его нет; совет пересобирается."""
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        event = STORE.delete_event(int(user["id"]), event_id)
        if event is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Event not found"})
            return

        self._advice_changed(int(user["id"]), "event")
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "user": user, "event": event, "deleted": True},
            extra_headers=headers,
        )

    def _delete_workout(self, workout_id: int) -> None:
        """``DELETE /api/workouts/<id>``: удалить тренировку; 404, если её нет.
        Удаление последней тренировки сносит и кэш совета (``trigger_recommendation_async``).
        """
        session = self._require_user()
        if session is None:
            return
        user, headers = session

        workout = STORE.delete_workout(int(user["id"]), workout_id)
        if workout is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "reason": "Workout not found"})
            return

        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "user": user, "workout": workout, "deleted": True},
            extra_headers=headers,
        )
        self._advice_changed(int(user["id"]), "workout")

    def log_message(self, format: str, *args: object) -> None:
        """Лог запроса в stdout с префиксом ``[miniapp]`` (systemd собирает его в journal)."""
        print(f"[miniapp] {self.address_string()} - {format % args}")

    def _read_json_body(self) -> dict[str, Any] | None:
        """Тело запроса как JSON-словарь; пустое тело — ``{}``. Битый JSON, кривой
        Content-Length или не-объект — сам отвечает 400 и возвращает ``None``,
        обработчику остаётся выйти."""
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length < 0:
                raise ValueError
            payload_raw = self.rfile.read(content_length).decode("utf-8")
            payload = json.loads(payload_raw or "{}")
        except (TypeError, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "reason": "Invalid JSON body"})
            return None
        if not isinstance(payload, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "reason": "JSON body must be an object"},
            )
            return None
        return payload

    def _recommendation_response(self, user: dict[str, Any]) -> dict[str, Any]:
        """Payload текущего совета для клиента: статус ``none`` без записи, иначе
        строка кэша с флагом ``stale`` (``recommender.is_stale``). Зовут GET next и
        обе ветки refresh.
        """
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
        stale = recommender.is_stale(rec, STORE.get_latest_workout_id(user_id))
        return {"ok": True, "user": user, "stale": stale, **rec}

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, object],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """Ответ JSON с кодом, ``Cache-Control: no-store`` и открытым CORS;
        ``extra_headers`` — обычно ``Set-Cookie`` из ``_require_user``.
        """
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
        """Отдать файл с диска с MIME по расширению (и charset для текста); нет файла —
        404. Единственный потребитель — каталог упражнений.
        """
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

    def _build_session_cookie(self, user_id: int) -> str:
        """``Set-Cookie`` сессии: подписанное значение, HttpOnly, месяц жизни
        (``MINIAPP_SESSION_MAX_AGE``), ``Secure`` по ``MINIAPP_COOKIE_SECURE``.
        """
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
        """``Set-Cookie``, гасящий сессию (пустое значение, ``Max-Age=0``)."""
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

    def _advice_changed(self, user_id: int, change: str, *, created: bool = True) -> None:
        """Совет пересобирается после правки данных, из которых он собран; что
        именно его обесценивает, решает recommender.advice_invalidated_by."""
        if recommender.advice_invalidated_by(change, created=created):
            trigger_recommendation_async(user_id)

    def _require_user(self) -> tuple[dict[str, Any], dict[str, str]] | None:
        """Сессия эндпоинта: пользователь по cookie либо debug-пользователь, если
        он разрешён. Без сессии метод сам отвечает 401 и возвращает None —
        обработчику остаётся просто выйти."""
        user, headers = self._resolve_current_user(allow_debug_fallback=True)
        if user is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {
                    "ok": False,
                    "reason": "No active session. iOS client must resolve a session first.",
                },
            )
            return None
        return user, headers

    def _resolve_current_user(
        self,
        allow_debug_fallback: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """Пользователь по подписанной cookie, если она есть и валидна; с
        ``allow_debug_fallback`` и разрешённым debug-пользователем — он, вместе с
        ``Set-Cookie`` для него. Иначе ``(None, {})``. Зовут ``_require_user`` и
        ``_post_session_resolve``.
        """
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


# Маршруты с точным путём: (метод, путь) → обработчик. Порядок не важен, пути
# не пересекаются; новый эндпоинт — это метод класса плюс строка здесь.
ROUTES: dict[tuple[str, str], Callable[[MiniAppHandler], None]] = {
    ("GET", "/api/health"): MiniAppHandler._get_health,
    ("GET", "/api/dev/version"): MiniAppHandler._get_dev_version,
    ("GET", "/data/exercises.json"): MiniAppHandler._get_exercise_catalog,
    ("GET", "/api/workouts"): MiniAppHandler._get_workouts,
    ("GET", "/api/body-weights"): MiniAppHandler._get_body_weights,
    ("GET", "/api/recommendations/next"): MiniAppHandler._get_recommendation_next,
    ("GET", "/api/reports/weekly"): MiniAppHandler._get_weekly_report,
    ("GET", "/api/reports/weekly/history"): MiniAppHandler._get_weekly_report_history,
    ("GET", "/api/waists"): MiniAppHandler._get_waists,
    ("GET", "/api/measurements"): MiniAppHandler._get_measurements,
    ("GET", "/api/events"): MiniAppHandler._get_events,
    ("GET", "/api/coach/signals"): MiniAppHandler._get_coach_signals,
    ("POST", "/api/session/logout"): MiniAppHandler._post_session_logout,
    ("POST", "/api/session/resolve"): MiniAppHandler._post_session_resolve,
    ("POST", "/api/workouts"): MiniAppHandler._post_workout,
    ("POST", "/api/body-weights"): MiniAppHandler._post_body_weight,
    ("POST", "/api/waists"): MiniAppHandler._post_waist,
    ("POST", "/api/measurements"): MiniAppHandler._post_measurement,
    ("POST", "/api/events"): MiniAppHandler._post_event,
    ("POST", "/api/coach/signals/dismiss"): MiniAppHandler._post_signal_dismiss,
    ("POST", "/api/reports/weekly/read"): MiniAppHandler._post_weekly_report_read,
    ("POST", "/api/recommendations/refresh"): MiniAppHandler._post_recommendation_refresh,
}

# Маршруты вида /api/<коллекция>/<id>: обработчик получает разобранный id.
ID_ROUTES: dict[tuple[str, str], Callable[[MiniAppHandler, int], None]] = {
    ("PUT", "/api/events/"): MiniAppHandler._put_event,
    ("PUT", "/api/workouts/"): MiniAppHandler._put_workout,
    ("DELETE", "/api/waists/"): MiniAppHandler._delete_waist,
    ("DELETE", "/api/measurements/"): MiniAppHandler._delete_measurement,
    ("DELETE", "/api/body-weights/"): MiniAppHandler._delete_body_weight,
    ("DELETE", "/api/events/"): MiniAppHandler._delete_event,
    ("DELETE", "/api/workouts/"): MiniAppHandler._delete_workout,
}


def main() -> None:
    """Точка входа процесса: ``ThreadingHTTPServer`` на ``MINIAPP_HOST:MINIAPP_PORT``, поток на соединение."""
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
