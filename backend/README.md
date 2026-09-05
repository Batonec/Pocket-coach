# Trainer Backend

Backend для приложения `Trainer`: HTTP API на стандартной библиотеке Python + SQLite.
Обслуживает нативный iOS-клиент (см. [../ios](../ios)) и MCP-сервер
[../coach_mcp](../coach_mcp). Веб-мини-апп и Telegram-бот удалены (июнь 2026).

Продуктовая спецификация — в [BUSINESS_LOGIC.md](../BUSINESS_LOGIC.md).

## Состав

- [backend/server.py](./server.py) — HTTP API, резолв сессии (iOS fixed-user + browser debug), раздача каталога упражнений
- [backend/trainer/data/backend_store.py](./trainer/data/backend_store.py) — SQLite-хранилище: только SQL, решения берёт из `domain/rules.py`
- [backend/trainer/domain/rules.py](./trainer/domain/rules.py) — форма и границы входа: тренировка, замеры, события, снапшот совета, план от модели, даты
- [backend/trainer/data/files.py](./trainer/data/files.py) — файлы рядом с базой и каталог упражнений: чтение и запись состояния, профиля, стратегии
- [backend/trainer/domain/recommender.py](./trainer/domain/recommender.py) — точка входа «Совета тренера»: загрузка каталога, профиля и стратегии, оба вызова модели (план и недельный отчёт), один авто-репромпт по нарушениям валидатора
- [backend/trainer/domain/prompt_builder.py](./trainer/domain/prompt_builder.py) — всё, что читает модель: системный промпт, user-промпт с вычисленными фичами и историей, JSON-схема ответа, промпт недельного отчёта; проза берётся из [prompts/](./prompts), здесь только слоты
- [backend/trainer/domain/plan_validator.py](./trainer/domain/plan_validator.py) — три жёсткие границы методики таблицей `RULES` (формулировки — в [prompts/plan_rules.md](./prompts/plan_rules.md), откуда они уходят и в системный промпт, и в репромпт) и детерминированное разрешение после неудачного репромпта; санитизация ответа — в `rules.py`
- [backend/trainer/data/anthropic_client.py](./trainer/data/anthropic_client.py) — HTTP-вызов Claude Messages API на stdlib `urllib`: ретраи на временные сбои, prompt caching, structured output; единственное место, где открывается соединение с API
- [backend/trainer/domain/coach_state.py](./trainer/domain/coach_state.py) — машина фаз подготовки (cut_recomp / lean_bulk / maintenance), volume ramp по неделям блока
- [backend/trainer/domain/coach_features.py](./trainer/domain/coach_features.py) — вычисляемые фичи истории: per-exercise сводки (пики, e1RM, ПР), детектор застоя, ступени разгона после перерыва, эффективные недельные объёмы, тренды веса/талии и матрица питания
- [backend/trainer/domain/coach_signals.py](./trainer/domain/coach_signals.py) — детерминированные баннеры коуча; каноническая спецификация — [docs/COACH_SIGNALS.md](../docs/COACH_SIGNALS.md)
- [backend/resources](./resources) — три файла, которые читает клиент, а не модель: `exercises.json` (каталог упражнений, отдаётся по `/data/exercises.json`), `signals.md` (тексты баннеров) и `plan_notes.md` (пометки «Проверка методики», которые валидатор дописывает в rationale)
- [backend/infra/jobs](./infra/jobs) — скрипты systemd-таймеров: авто-свежесть совета, недельный отчёт, бэкап базы
- [backend/infra/deploy](./infra/deploy) — деплой на VPS и systemd-юниты
- [backend/tests](./tests) — тесты backend

## HTTP endpoints

- `GET /api/health`
- `GET /api/dev/version`
- `POST /api/session/resolve` — iOS fixed-user (`shell=ios` + `native_user_id`) либо browser debug
- `POST /api/session/logout`
- `GET /api/workouts` · `POST /api/workouts` · `PUT /api/workouts/{id}` · `DELETE /api/workouts/{id}`
- `GET /api/body-weights` · `POST /api/body-weights` · `DELETE /api/body-weights/{id}`
- `GET /api/waists` · `POST /api/waists` · `DELETE /api/waists/{id}`
- `GET /api/events` · `POST /api/events` · `PUT /api/events/{id}` · `DELETE /api/events/{id}` —
  события: периоды без тренировок с причиной. Открытое событие (`end_date: null`) одно; новая
  тренировка сегодняшним числом закрывает его концом «вчера» (правка истории — не закрывает).
  Ни одного числа из событий не считается: это текст в промпт тренера
- `GET /api/coach/signals` · `POST /api/coach/signals/dismiss`
- `GET /api/recommendations/next` · `POST /api/recommendations/refresh`
- `GET /api/reports/weekly` — кэшированный недельный отчёт тренера (без генерации; отчёт пишет ночной таймер понедельника или Coach MCP)

Каталог упражнений лежит в `resources/exercises.json` и отдаётся по `GET /data/exercises.json` (URL
остался от веб-версии и зашит в iOS-клиент; другой статики сервер не отдаёт). Его читают и
iOS-клиент, и `recommender.py`.

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
  Удаление последней тренировки вместо бессмысленной генерации очищает актуальную строку
  `recommendations`; история генераций в `recommendation_log` остаётся для аудита.
- **Авто-свежесть:** systemd-таймер
  ([infra/deploy/trainer-recommend-refresh.timer](./infra/deploy/trainer-recommend-refresh.timer),
  06:30 МСК) запускает [infra/jobs/refresh_recommendation.py](./infra/jobs/refresh_recommendation.py) —
  перегенерирует совет, если ему больше `REFRESH_MAX_AGE_HOURS` (по умолчанию 24 ч),
  упавшие/зависшие генерации добиваются. Так «когда идти» в карточке всегда датировано
  сегодняшним днём, даже после долгого перерыва. Ручной запуск: `--force`.

## Бэкап базы

[infra/jobs/backup_db.py](./infra/jobs/backup_db.py) делает консистентный снапшот `trainer.db` через SQLite
online-backup API (без остановки сервиса, без зависимостей), сжимает в gzip и копирует
рядом профиль атлета, состояние коуча и рабочую стратегию; держит последние
`BACKUP_KEEP` согласованных комплектов. Запускается ночным
systemd-таймером
([infra/deploy/trainer-db-backup.timer](./infra/deploy/trainer-db-backup.timer), 04:00 МСК).
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
прогрессия, deload, матрица питания). Политика подана явно как **ориентиры по
умолчанию** — модель вправе отступать с обоснованием; отдельным блоком перечислены
три жёсткие границы, которые реально проверяет сервер. Первым блоком user-промпта
идёт вычисленный **контекст**: дата, фаза + неделя блока с целевым объёмом недели,
дней с последней тренировки (и режим возврата, если перерыв ≥14 дней).

Вместо сырых 20 тренировок модель получает **вычисляемые фичи** (`coach_features.py`):
per-exercise сводки за всю историю (топ-сет, e1RM по Эпли, дата последнего ПР, % текущего
веса от пика; гравитрон инвертирован — прогресс там это уменьшение противовеса), недельные
объёмы прямыми и **эффективными** сетами (жимы добирают трицепсу/дельтам, тяги — бицепсу),
**явку по календарным неделям** (пн–вс, последние четыре закрытые плюс текущая, и серии
подряд с ≥3/≥4), флаг **застоя** по **активному окну блока** — от старта фазы или первой
тренировки после перерыва ≥14 дней, не длиннее 6 недель, так что отпуск не размывает частоту:
предусловия — частота ≥2.5/нед, объём крупных групп не ниже нижней границы `group_targets`
фазы (без них — 10 сетов), вес в коридоре темпа; сам застой — ≥4 недель без прироста ВНУТРИ
окна, а не давность всевременного ПР (после перерыва она у каждого движения), и окно короче
3 недель вердикта не даёт. Дальше **ступени возврата** к доперерывному рабочему весу (не к
пику; печатаются только пока вес не возвращён и возврат моложе 8 недель, шаг — гранулярность
стека из истории) и ветку **матрицы питания**: тренд веса — МНК-наклон по всем замерам за 21
день, ветки — от коридора `rate_kg_per_week` фазы, а не от её имени; отклонение от коридора
становится советом по калориям только при подтверждении средними двух недель. В сводке по
тренажёру у каждой сессии стоит позиция упражнения `[#k/n]` — вес на шестом месте не
сравним с весом на первом. Сырыми остаются последние ~10 тренировок.

**Фазы** (`coach_state.py`, состояние в `coach_state.json` рядом с базой): `cut_recomp` / `lean_bulk` /
`maintenance` + вычисляемый режим «возврат после перерыва». Файл состояния — JSON вида
`{"schema": 1, "phase": "cut_recomp", "phase_started": "2026-08-14", "phase_params": {},
"waist_limit_cm": null, "waist_base_cm": null}`; `phase_params` — переопределения дефолтов фазы
(`coach_state.PHASE_DEFAULTS`). В репозитории его нет: на VPS он лежит в
`/opt/trainer-miniapp/data/coach_state.json`, путь переопределяется `COACH_STATE_PATH`, меняется
только инструментами Coach MCP. Переключение — только руками
через Coach MCP (`coach_set_phase`); при достижении цели фазы промпт лишь просит модель
предложить смену в rationale. Недельный объём строительных фаз идёт ramp'ом 6–8 →
потолок фазы (+1–2 сета/нед), перерыв ≥14 дней сбрасывает ramp на старт блока.
Цикл блоков включает **плановый deload**: после 6 недель реально накопленной работы
(≥2 сессии/нед в блоке) сервер помечает 7-ю неделю разгрузочной (−30–40% объёма, веса
рабочие), после неё ramp начинается заново; при редких тренировках флаг не ставится.
Планировщика по гормональному циклу больше нет: фон супрафизиологичен всю неделю,
день-к-дню тайминг — спекуляция. Гормональный контекст атлета живёт прозой в профиле;
медицинская граница (никаких советов по ГЗТ/дозировкам/анализам) осталась в промптах.

**Возврат после перерыва** (`coach_features.pre_break_working_weights`): сервер подаёт
факты — рабочий вес каждого упражнения в последней сессии перед паузой, оговорку, что
отметки усилия/RIR относятся к той же доперерывной форме, и ступени возврата к
доперерывному рабочему (ориентир для последующих сессий, не рамка), — а системный промпт
просит вести возврат
по принятым тренерским принципам работы после простоя. Процентов снижения и
физиологических обоснований в коде нет намеренно: это суждение модели. Единственная
жёсткая граница в валидаторе — вес возвратной сессии не выше доперерывного рабочего
ни по одному движению.

**Дисциплина**: агрегат «факт vs план» за 30 дней (процент плановых подходов с обрезкой
планом, тренировки по плану, стабильно пропускаемые упражнения) считается сервером и
подаётся модели — тренер адаптирует план к реальному поведению (пропускаемое — раньше в
сессии), без нотаций. **Недельный отчёт**: `generate_weekly_report()` собирает итоги
недели (объём vs цель, ПР, тренды веса/талии, дисциплина, фокус следующей недели) и
одним вызовом модели пишет Markdown-ретроспективу — MCP-инструмент `coach_weekly_report`.
Период отчёта — последняя **закрытая** календарная неделя (пн–вс), а не «последние 7
дней от сегодня»: дату считает `coach_state.last_closed_week_end`, и её же зовёт Coach
MCP, когда ищет отчёт в кэше. Якорь общий не для красоты: разъедутся — инструмент
промахнётся мимо кэша и молча перегенерирует отчёт за токены. Отчёт кэшируется в
таблице `coach_reports`: systemd-таймер в ночь на понедельник
([infra/deploy/trainer-weekly-report.timer](./infra/deploy/trainer-weekly-report.timer), 00:00 МСК,
скрипт [infra/jobs/weekly_report.py](./infra/jobs/weekly_report.py)) генерирует его заранее, и инструмент
отдаёт кэш мгновенно и бесплатно (`fresh=true` — перегенерация). Полночь, а не
воскресный вечер, потому что вечерняя воскресная тренировка обязана попасть в отчёт
о своей неделе.

**Юниты таймеров ставятся на VPS руками**: ни CI, ни `deploy.sh` их не возят, они
кладут только `trainer-miniapp-backend.service`. Для недельного отчёта это не мелочь,
а условие работоспособности: старый воскресный юнит с новым кодом будит скрипт в день,
когда последняя закрытая неделя уже в кэше, тот честно пишет «уже в кэше» и выходит —
и отчёт перестаёт появляться совсем, молча. Код и `OnCalendar` едут вместе:
`scp backend/infra/deploy/trainer-weekly-report.timer <vps>:/etc/systemd/system/` +
`systemctl daemon-reload && systemctl restart trainer-weekly-report.timer`.

**Журнал фаз и итоги**: `coach_set_phase` закрывает уходящую фазу записью
`{phase, started, ended}` в `phase_history`; `coach_phase_summary` считает «что дала
фаза» из истории по датам (тренировки и частота, вес/талия старт→финиш с темпом, ПР за
период, дисциплина) — работает и для текущей, и для любой закрытой фазы.
`coach_costs` — помесячные токены/стоимость из `recommendation_log` + `coach_reports`.

После ответа модели работает **семантический валидатор** поверх JSON-схемы — ровно три
жёсткие границы: покрытие групп (крупная группа или бицепс бедра с нулём эффективных
сетов за 10+ дней обязана попасть в план), возвратный потолок весов и потолок размера
сессии (верхняя граница `session_sets` фазы; нижняя не проверяется — короткая сессия
может быть решением). Диапазоны повторов, шаги весов, чередование heavy/medium/light —
суждение модели (ориентиры остаются в промпте); `rest_days` клампится 0–4 молча; имя
упражнения модель не пишет — сервер подставляет каталожное по id. Нарушение границы
уходит модели одним авто-репромптом; повторный промах разрешается детерминированно
(возвратные веса клампятся к доперерывным, лишние подходы режутся с хвоста плана по
одному на упражнение, незакрытое покрытие дописывается в rationale) — из-за методики
генерация больше не падает. Правила — таблица `plan_validator.RULES`; их формулировки в
`prompts/plan_rules.md` рендерятся в блок «ЖЁСТКИЕ ГРАНИЦЫ» системного промпта, так что модель
читает ровно то, что сервер проверяет. Замеры талии хранятся в таблице `waists` (пишутся через
Coach MCP); единый допустимый диапазон записи и аналитики — 50–160 см. Значение вне
диапазона возвращает HTTP 400 и не может попасть в состояние «в UI сохранено, коучем
проигнорировано». Правило свежести общее с весом: данные старше 14 дней → советы по
калориям не даются, модель просит замер. Запросы к Claude используют **prompt caching**
(системный блок и первый user-месседж помечены `cache_control`) — репромпты и
всплески регенераций платят за стабильную часть промпта копейки.

### Профиль атлета

`recommender.load_profile()` читает JSON с произвольными текстовыми блоками
(`{"schema":1, "blocks": {"Атлет": "...", "Цель": "...", ...}}`), которые попадают в
системный промпт как есть; имена блоков произвольные.
**Реальный профиль содержит персональные/медицинские данные и живёт только на сервере**
(`/opt/trainer-miniapp/data/coach_profile.json`, рядом с базой; путь переопределяется
`COACH_PROFILE_PATH`) — в публичный репозиторий он не коммитится, шаблона в репозитории тоже нет. Файл отсутствует/битый → генерация работает с нейтральным фоллбеком.
Блоки правятся удалённо инструментом Coach MCP `coach_update_profile`
(`recommender.update_profile_block()`: замена/удаление одного блока, предыдущая версия
файла сохраняется рядом как `.bak-таймстамп`) — содержимое профиля при этом в репозиторий
не попадает.
Рядом с профилем живут `coach_state.json` — структурное состояние подготовки (фаза, дата
её старта, переопределения параметров, лимит/база талии) — и `coach_strategy.md`, рабочий
документ стратегии: обычный markdown с заголовками `## N. Название`, в системный промпт уходит
не весь документ, а срез по заголовкам из `prompt_builder.STRATEGY_SECTIONS` (по тексту заголовка,
номер не важен; ненайденный заголовок попадает в промпт предупреждением). Оба файла личные, живут
только на VPS рядом с базой; пути — `COACH_STATE_PATH` и `COACH_STRATEGY_PATH`.

### Связка тренировка ↔ рекомендация

Когда клиент сохраняет тренировку, выполненную по применённому совету, он кладёт в payload
`data.recommendation` — **снапшот** рекомендации (`schema`, `source`, `model`,
`generated_at`, `applied_at`, `based_on_workout_id/count`, `focus`, `load_type`,
`exercises[{exercise_id,name,sets[{reps,weight}]}]`). Именно снапшот, а не ссылка: таблица
`recommendations` хранит одну перезаписываемую строку на пользователя, поэтому только копия
стабильна для статистики «факт vs план».

Серверные правила (`domain/rules.py`, применяет `data/backend_store.py`):
- `normalize_recommendation_snapshot` — белый список полей и лимиты (≤10 упражнений, ≤12
  подходов, ≤8 КБ); невалидный снапшот **молча отбрасывается**, тренировка сохраняется.
- `PUT /api/workouts/{id}` без снапшота в payload **сохраняет** уже записанный снапшот
  (клиент пересобирает payload из черновика при редактировании).
- Повторный `POST` с тем же `client_id` (ретрай) бэкфиллит снапшот, если в строке его не
  было, но никогда не перезаписывает существующий.

## Локальный запуск

Команды запускаются из корня репозитория; зависимостей и venv у backend нет.

```bash
MINIAPP_ALLOW_DEBUG_USER=1 python3 backend/server.py
```

API поднимается на `http://127.0.0.1:8080/`, SQLite — локально. iOS-клиент собирается
из [../ios](../ios) в Xcode.

## Переменные окружения

### `server.py`

- `MINIAPP_HOST` — по умолчанию `127.0.0.1`
- `MINIAPP_PORT` — по умолчанию `8080`
- `EXERCISE_CATALOG_PATH` — путь к каталогу упражнений, по умолчанию `resources/exercises.json`; отдаётся по `/data/exercises.json`
- `MINIAPP_DB_PATH` — путь к SQLite
- `MINIAPP_SESSION_SECRET` — секрет для подписи cookie `trainer_session`
- `MINIAPP_SESSION_MAX_AGE` · `MINIAPP_COOKIE_SECURE`
- `MINIAPP_DEV_MODE` · `MINIAPP_ALLOW_DEBUG_USER` — включают browser debug-пользователя
- `MINIAPP_DEFAULT_DEBUG_USER_ALIAS` / `_FIRST_NAME` / `_LAST_NAME`

### Совет тренера (LLM)

- `ANTHROPIC_API_KEY` — ключ Claude API (обязателен для генерации)
- `ANTHROPIC_MODEL` — модель, по умолчанию `claude-opus-5`
- `ANTHROPIC_MAX_TOKENS` — лимит вывода, по умолчанию `20000`. На Opus 5
  мышление включено по умолчанию и считается в этот же лимит, поэтому запас
  больше прежнего; при упоре в лимит генерация падает с явной ошибкой про
  бюджет, а не с «невалидным JSON»
- `ANTHROPIC_EFFORT` — глубина мышления (`low`/`medium`/`high`/`xhigh`/`max`),
  по умолчанию `high`: качество плана здесь важнее задержки. Пустая строка —
  не слать `effort` вовсе
- `ANTHROPIC_MAX_RETRIES` — повторы при временных сбоях API (429/5xx/529, таймаут, сеть), по умолчанию `2`
- `ANTHROPIC_RETRY_BACKOFF` — базовая пауза экспоненциального backoff в секундах, по умолчанию `1.5`
- `COACH_PROFILE_PATH` — путь к профилю атлета, по умолчанию `<dir(MINIAPP_DB_PATH)>/coach_profile.json`
- `COACH_STATE_PATH` — путь к состоянию подготовки (фаза/цикл/талия-лимиты), по умолчанию `<dir(MINIAPP_DB_PATH)>/coach_state.json`
- `COACH_STRATEGY_PATH` — путь к рабочему документу стратегии, по умолчанию `<dir(MINIAPP_DB_PATH)>/coach_strategy.md`
- `REFRESH_MAX_AGE_HOURS` — порог авто-свежести для `refresh_recommendation.py`, по умолчанию `24`
- `BACKUP_DIR` / `BACKUP_KEEP` — каталог и глубина ротации для `backup_db.py`, по умолчанию `<dir(MINIAPP_DB_PATH)>/backups` и `14`
- `ANTHROPIC_TIMEOUT` — таймаут **одного** вызова Claude, по умолчанию `120`.
  Авто-репромпт валидатора делает второй вызов, поэтому весь HTTP-запрос
  `POST /api/recommendations/refresh` в худшем случае длится вдвое дольше;
  таймауты iOS (`APIClient.longRunningSession`, 150/180 с) держатся выше
- `RECOMMENDATION_HISTORY_LIMIT` — сколько последних тренировок отдавать модели, по умолчанию `20`
- `RECOMMENDATION_REFRESH_MIN_INTERVAL` — анти-дребезг ручного refresh в секундах, по умолчанию `10`

## Тесты

```bash
python3 -m unittest discover -s backend/tests -p "test_*.py" -v
```

## Деплой на VPS

Backend деплоится через CI ([../.github/workflows/deploy-backend.yml](../.github/workflows/deploy-backend.yml))
после зелёных тестов на `main`, либо вручную:

```bash
./backend/infra/deploy/deploy.sh backend
```

**Адрес VPS в репозитории не хранится** (репозиторий публичный): скрипт берёт его из
`TRAINER_VPS_HOST` либо из gitignored-файла `backend/infra/deploy/target.local` рядом с собой —
одна строка `TRAINER_VPS_HOST=root@<адрес>`. Без адреса деплой падает с подсказкой,
не дойдя до `ssh`. В CI адрес приходит из секрета `VPS_HOST`.

Остальные значения по умолчанию в deploy tooling:

- `/opt/trainer-miniapp` (исторический путь на VPS, не переименовывался)
- `trainer-miniapp-backend.service`

Backend workflow предполагает существование `/etc/trainer-miniapp/backend.env` на VPS
(там же лежит `ANTHROPIC_API_KEY`).

Код едет каталогами: `server.py` поимённо, `trainer/`, `infra/jobs/`, `prompts/`, `resources/`
целиком и вместе с ними все юниты и таймеры из `infra/deploy/`. Таймеры деплой не включает: новый таймер один
раз `systemctl enable --now` руками.

### Как запросы доходят до backend

Снаружи backend не виден: он слушает docker-bridge (`MINIAPP_HOST=172.17.0.1`,
`MINIAPP_PORT=8081` в `/etc/trainer-miniapp/backend.env`). Цепочка: Cloudflare Tunnel
(`cloudflared`, маршруты живут в дашборде Cloudflare) → docker-контейнер `trainer-miniapp-caddy`
на `127.0.0.1:80` → `host.docker.internal:8081`. Caddy — чистый обратный прокси без статики
(веб-мини-апп удалён, `/data/exercises.json` отдаёт сам backend); его конфиг —
[infra/deploy/Caddyfile](infra/deploy/Caddyfile), CI его не трогает, применяется вручную:

```bash
./backend/infra/deploy/deploy.sh proxy
```

Скрипт валидирует конфиг внутри контейнера, переписывает файл на VPS на месте (bind-mount
файла держится за inode) и делает `caddy reload`. Сам контейнер поднят один раз руками:

```bash
docker run -d --name trainer-miniapp-caddy --restart unless-stopped \
  -p 127.0.0.1:80:80 --add-host host.docker.internal:host-gateway \
  -v /opt/trainer-miniapp/Caddyfile:/etc/caddy/Caddyfile:ro \
  -v /opt/trainer-miniapp/caddy_data:/data -v /opt/trainer-miniapp/caddy_config:/config \
  caddy:2
```

## GitHub Actions

- [ci.yml](../.github/workflows/ci.yml) — прогоняет тесты на каждый push; на `main` после
  зелёных тестов деплоит backend, если он затронут.
- [deploy-backend.yml](../.github/workflows/deploy-backend.yml) — reusable деплой backend.

Нужные GitHub Secrets: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_PORT` (опционально).
