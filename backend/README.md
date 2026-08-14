# Trainer Backend

Backend для приложения `Trainer`: HTTP API на стандартной библиотеке Python + SQLite.
Обслуживает нативный iOS-клиент (см. [../ios](../ios)) и MCP-сервер
[../coach_mcp](../coach_mcp). Веб-мини-апп и Telegram-бот удалены (июнь 2026).

Продуктовая спецификация — в [BUSINESS_LOGIC.md](../BUSINESS_LOGIC.md).

## Состав

- [backend/server.py](./server.py) — HTTP API, резолв сессии (iOS fixed-user + browser debug), раздача каталога упражнений
- [backend/backend_store.py](./backend_store.py) — SQLite-хранилище и нормализация данных
- [backend/recommender.py](./recommender.py) — «Совет тренера»: сборка промпта, вызов Claude API, семантический валидатор с авто-репромптом
- [backend/coach_state.py](./coach_state.py) — машина фаз подготовки (cut_recomp / lean_bulk / maintenance), volume ramp по неделям блока, конфиг гормонального недельного цикла
- [backend/coach_features.py](./coach_features.py) — вычисляемые фичи истории: per-exercise сводки (пики, e1RM, ПР), детектор застоя, ступени разгона после перерыва, эффективные недельные объёмы, тренды веса/талии и матрица питания
- [backend/static/data/exercises.json](./static/data/exercises.json) — каталог упражнений (отдаётся клиенту по `/data/exercises.json`)
- [backend/deploy](./deploy) — деплой на VPS
- [backend/tests](./tests) — тесты backend

## HTTP endpoints

- `GET /api/health`
- `GET /api/dev/version`
- `POST /api/session/resolve` — iOS fixed-user (`shell=ios` + `native_user_id`) либо browser debug
- `POST /api/session/logout`
- `GET /api/workouts` · `POST /api/workouts` · `PUT /api/workouts/{id}` · `DELETE /api/workouts/{id}`
- `GET /api/body-weights` · `POST /api/body-weights` · `DELETE /api/body-weights/{id}`
- `GET /api/recommendations/next` · `POST /api/recommendations/refresh`

Каталог упражнений отдаётся как статика по `GET /data/exercises.json` (его читает и iOS-клиент,
и `recommender.py`).

### Сессии

- **Native iOS fixed user** — iOS присылает `shell=ios` и `native_user_id` в `POST /api/session/resolve`;
  backend находит пользователя с этим `id` и выдаёт подписанную cookie `trainer_session`. Это
  personal-build режим, не рассчитанный на публичный multi-user доступ.
- **Browser debug user** — для локальной разработки (`MINIAPP_ALLOW_DEBUG_USER=1`) браузер без
  сессии автоматически получает debug-пользователя.

## Совет тренера (LLM-рекомендация)

`recommender.py` строит план следующей тренировки по истории пользователя через Claude
Messages API (structured outputs, чистый stdlib `urllib` — без SDK/venv). Хранится одна
строка на пользователя в таблице `recommendations` (статусы `none`/`pending`/`ready`/`failed`).

- `GET /api/recommendations/next` — мгновенно отдаёт кэш (или `status: none`), не ждёт генерации;
  в ответе есть флаг `stale` (есть ли тренировка новее той, по которой считали).
- `POST /api/recommendations/refresh` — синхронная форс-генерация (10–40 с), per-user lock + анти-дребезг.
- После создания/изменения/удаления тренировки рекомендация перегенерируется в фоновом потоке.
- **Авто-свежесть:** systemd-таймер
  ([deploy/trainer-recommend-refresh.timer](./deploy/trainer-recommend-refresh.timer),
  06:30 МСК) запускает [refresh_recommendation.py](./refresh_recommendation.py) —
  перегенерирует совет, если ему больше `REFRESH_MAX_AGE_HOURS` (по умолчанию 24 ч),
  упавшие/зависшие генерации добиваются. Так «когда идти» в карточке всегда датировано
  сегодняшним днём, даже после долгого перерыва. Ручной запуск: `--force`.

## Бэкап базы

[backup_db.py](./backup_db.py) делает консистентный снапшот `trainer.db` через SQLite
online-backup API (без остановки сервиса, без зависимостей), сжимает в gzip и копирует
рядом профиль атлета; держит последние `BACKUP_KEEP` копий. Запускается ночным
systemd-таймером
([deploy/trainer-db-backup.timer](./deploy/trainer-db-backup.timer), 04:00 МСК).
Восстановление: `gunzip -c backups/trainer-<stamp>.db.gz > trainer.db`.

Формат: `focus`, `load_type` (heavy/medium/light), `rest_days` (через сколько дней от
сегодня проводить тренировку: 0 — сегодня, 1 — завтра…) и вычисленная из него абсолютная
`next_workout_date` (ISO), развёрнутый `rationale` (почему именно такой план) и
`exercises[]` с `exercise_id`/`name`/`note`/`sets[{reps,weight}]`. Требуется
`ANTHROPIC_API_KEY`; без него генерация отвечает понятной ошибкой, остальные эндпоинты работают.
Временные сбои API (429/5xx/529, таймаут, обрыв связи) повторяются с экспоненциальным
backoff (`ANTHROPIC_MAX_RETRIES`); постоянные (400/401/refusal) — нет. Каждая генерация
(успешная и упавшая) дописывается в журнал `recommendation_log` (append-only) — таблица
`recommendations` хранит только текущую строку; журнал — это история затрат токенов и
основа будущей статистики/отладки.

Промпт работает как «полный тренер»: системная часть включает **профиль атлета**
(персональный JSON, см. ниже), семантику каталога тренажёров (какая мышца у какого id),
описание **фаз подготовки** и тренерскую политику (волны интенсивности, RIR,
прогрессия, deload, планировщик по гормональному циклу, матрица питания). Первым блоком
user-промпта идёт вычисленный **контекст**: дата, день гормонального цикла, фаза + неделя
блока с целевым объёмом недели, дней с последней тренировки (и режим возврата, если
перерыв ≥14 дней).

Вместо сырых 20 тренировок модель получает **вычисляемые фичи** (`coach_features.py`):
per-exercise сводки за всю историю (топ-сет, e1RM по Эпли, дата последнего ПР, % текущего
веса от пика; гравитрон инвертирован — прогресс там это уменьшение противовеса), недельные
объёмы прямыми и **эффективными** сетами (жимы добирают трицепсу/дельтам, тяги — бицепсу),
флаг **застоя** (только при выполненных предусловиях: 6 недель частоты ≥2.5/нед, объёма
≥10 сетов/группа и веса в целевом темпе — иначе модель обязана объяснять плато
посещаемостью), готовые **ступени разгона** к пикам после перерыва и ветку **матрицы
питания** (тренд веса × тренд талии per phase). Сырыми остаются последние ~10 тренировок.

**Фазы** (`coach_state.py`, состояние в `coach_state.json` рядом с базой, шаблон —
[coach_state.example.json](./coach_state.example.json)): `cut_recomp` / `lean_bulk` /
`maintenance` + вычисляемый режим «возврат после перерыва». Переключение — только руками
через Coach MCP (`coach_set_phase`); при достижении цели фазы промпт лишь просит модель
предложить смену в rationale. Недельный объём строительных фаз идёт ramp'ом 6–8 →
потолок фазы (+1–2 сета/нед), перерыв ≥14 дней сбрасывает ramp на старт блока.
Цикл блоков включает **плановый deload**: после 6 недель реально накопленной работы
(≥2 сессии/нед в блоке) сервер помечает 7-ю неделю разгрузочной (−30–40% объёма, веса
рабочие), после неё ramp начинается заново; при редких тренировках флаг не ставится.
Гормональный недельный цикл параметризован (`injection_day`, окна пика/спада) и
используется **только** для планирования нагрузки — никакой медицины.

**Дисциплина**: агрегат «факт vs план» за 30 дней (процент плановых подходов с обрезкой
планом, тренировки по плану, стабильно пропускаемые упражнения) считается сервером и
подаётся модели — тренер адаптирует план к реальному поведению (пропускаемое — раньше в
сессии), без нотаций. **Недельный отчёт**: `generate_weekly_report()` собирает итоги
недели (объём vs цель, ПР, тренды веса/талии, дисциплина, фокус следующей недели) и
одним вызовом модели пишет Markdown-ретроспективу — MCP-инструмент `coach_weekly_report`.
Отчёт кэшируется в таблице `coach_reports`: воскресный systemd-таймер
([deploy/trainer-weekly-report.timer](./deploy/trainer-weekly-report.timer), 18:00 МСК,
скрипт [weekly_report.py](./weekly_report.py)) генерирует его заранее, и инструмент
отдаёт кэш мгновенно и бесплатно (`fresh=true` — перегенерация).

**Журнал фаз и итоги**: `coach_set_phase` закрывает уходящую фазу записью
`{phase, started, ended}` в `phase_history`; `coach_phase_summary` считает «что дала
фаза» из истории по датам (тренировки и частота, вес/талия старт→финиш с темпом, ПР за
период, дисциплина) — работает и для текущей, и для любой закрытой фазы.
`coach_costs` — помесячные токены/стоимость из `recommendation_log` + `coach_reports`.

После ответа модели работает **семантический валидатор** поверх JSON-схемы: имена
дословно из каталога, веса в пределах ±15% диапазона упражнения за 8 недель (кроме
возврата/deload), число подходов в коридоре фазы (14–20 building / 8–12 maintenance /
10–14 возврат и плановая разгрузка), покрытие групп (крупная группа или бицепс бедра
с нулём эффективных сетов за 10+ дней обязана попасть в план), диапазоны повторов
базовых движений по волне интенсивности (heavy 6–10 / medium 10–14 / light 12–18,
допуск ±1; изоляция не проверяется), `rest_days` 0–4, «после heavy не heavy».
Нарушения уходят модели одним авто-репромптом; повторный промах — ошибка генерации
с деталями. Замеры талии хранятся в таблице `waists` (пишутся через
Coach MCP), правило свежести общее с весом: данные старше 14 дней → советы по калориям
не даются, модель просит замер. Запросы к Claude используют **prompt caching**
(системный блок и первый user-месседж помечены `cache_control`) — репромпты и
всплески регенераций платят за стабильную часть промпта копейки.

### Профиль атлета

`recommender.load_profile()` читает JSON с произвольными текстовыми блоками
(`{"schema":1, "blocks": {"Атлет": "...", "Цель": "...", ...}}`), которые попадают в
системный промпт. Шаблон — [coach_profile.example.json](./coach_profile.example.json).
**Реальный профиль содержит персональные/медицинские данные и живёт только на сервере**
(`/opt/trainer-miniapp/data/coach_profile.json`, рядом с базой) — в публичный репозиторий
он не коммитится. Файл отсутствует/битый → генерация работает с нейтральным фоллбеком.
Рядом с профилем живёт `coach_state.json` — структурное состояние подготовки (фаза, дата
её старта, переопределения параметров, лимит/база талии, день инъекции).

### Связка тренировка ↔ рекомендация

Когда клиент сохраняет тренировку, выполненную по применённому совету, он кладёт в payload
`data.recommendation` — **снапшот** рекомендации (`schema`, `source`, `model`,
`generated_at`, `applied_at`, `based_on_workout_id/count`, `focus`, `load_type`,
`exercises[{exercise_id,name,sets[{reps,weight}]}]`). Именно снапшот, а не ссылка: таблица
`recommendations` хранит одну перезаписываемую строку на пользователя, поэтому только копия
стабильна для статистики «факт vs план».

Серверные правила (`backend_store.py`):
- `normalize_recommendation_snapshot` — белый список полей и лимиты (≤10 упражнений, ≤12
  подходов, ≤8 КБ); невалидный снапшот **молча отбрасывается**, тренировка сохраняется.
- `PUT /api/workouts/{id}` без снапшота в payload **сохраняет** уже записанный снапшот
  (клиент пересобирает payload из черновика при редактировании).
- Повторный `POST` с тем же `client_id` (ретрай) бэкфиллит снапшот, если в строке его не
  было, но никогда не перезаписывает существующий.

## Локальный запуск

```bash
cd /Users/batonec/AndroidStudioProjects/Trainer
MINIAPP_ALLOW_DEBUG_USER=1 python3 backend/server.py
```

API поднимается на `http://127.0.0.1:8080/`, SQLite — локально. iOS-клиент собирается
из [../ios](../ios) в Xcode.

## Переменные окружения

### `server.py`

- `MINIAPP_HOST` — по умолчанию `127.0.0.1`
- `MINIAPP_PORT` — по умолчанию `8080`
- `MINIAPP_STATIC_DIR` — каталог со статикой (`static/`), откуда отдаётся `/data/exercises.json`
- `MINIAPP_DB_PATH` — путь к SQLite
- `MINIAPP_SESSION_SECRET` — секрет для подписи cookie `trainer_session`
- `MINIAPP_SESSION_MAX_AGE` · `MINIAPP_COOKIE_SECURE`
- `MINIAPP_DEV_MODE` · `MINIAPP_ALLOW_DEBUG_USER` — включают browser debug-пользователя
- `MINIAPP_DEFAULT_DEBUG_USER_ALIAS` / `_FIRST_NAME` / `_LAST_NAME`

### Совет тренера (LLM)

- `ANTHROPIC_API_KEY` — ключ Claude API (обязателен для генерации)
- `ANTHROPIC_MODEL` — модель, по умолчанию `claude-opus-4-8`
- `ANTHROPIC_MAX_TOKENS` — лимит вывода, по умолчанию `3500`
- `ANTHROPIC_MAX_RETRIES` — повторы при временных сбоях API (429/5xx/529, таймаут, сеть), по умолчанию `2`
- `ANTHROPIC_RETRY_BACKOFF` — базовая пауза экспоненциального backoff в секундах, по умолчанию `1.5`
- `COACH_PROFILE_PATH` — путь к профилю атлета, по умолчанию `<dir(MINIAPP_DB_PATH)>/coach_profile.json`
- `COACH_STATE_PATH` — путь к состоянию подготовки (фаза/цикл/талия-лимиты), по умолчанию `<dir(MINIAPP_DB_PATH)>/coach_state.json`
- `REFRESH_MAX_AGE_HOURS` — порог авто-свежести для `refresh_recommendation.py`, по умолчанию `24`
- `BACKUP_DIR` / `BACKUP_KEEP` — каталог и глубина ротации для `backup_db.py`, по умолчанию `<dir(MINIAPP_DB_PATH)>/backups` и `14`
- `ANTHROPIC_TIMEOUT` — таймаут запроса к Claude, по умолчанию `90`
- `RECOMMENDATION_HISTORY_LIMIT` — сколько последних тренировок отдавать модели, по умолчанию `20`
- `RECOMMENDATION_REFRESH_MIN_INTERVAL` — анти-дребезг ручного refresh в секундах, по умолчанию `10`

## Тесты

```bash
cd /Users/batonec/AndroidStudioProjects/Trainer
python3 -m unittest discover -s backend/tests -p "test_*.py" -v
```

## Деплой на VPS

Backend деплоится через CI ([../.github/workflows/deploy-backend.yml](../.github/workflows/deploy-backend.yml))
после зелёных тестов на `main`, либо вручную:

```bash
cd /Users/batonec/AndroidStudioProjects/Trainer
./backend/deploy/deploy.sh backend
```

Значения по умолчанию в deploy tooling:

- `root@89.124.83.32`
- `/opt/trainer-miniapp` (исторический путь на VPS, не переименовывался)
- `trainer-miniapp-backend.service`

Backend workflow предполагает существование `/etc/trainer-miniapp/backend.env` на VPS
(там же лежит `ANTHROPIC_API_KEY`).

## GitHub Actions

- [ci.yml](../.github/workflows/ci.yml) — прогоняет тесты на каждый push; на `main` после
  зелёных тестов деплоит backend, если он затронут.
- [deploy-backend.yml](../.github/workflows/deploy-backend.yml) — reusable деплой backend.

Нужные GitHub Secrets: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_PORT` (опционально).
