# Coach MCP

MCP-сервер над данными Trainer: инструменты для разговора с Claude как с
«тренером» о своих тренировках, для **отладки рекомендаций «следующая
тренировка»** и для управления **состоянием подготовки** (фаза, замеры талии,
события). Читает ту же базу SQLite, что и backend, и импортирует
`backend_store` / `recommender` / `prompt_builder` / `coach_state` /
`coach_features` напрямую, поэтому здесь видно ровно то, что генерирует backend
приложения.

Второй сценарий — разбор пайплайна рекомендаций: точный промпт, попытки модели с
нарушениями семантического валидатора и авто-репромптом, токены и стоимость.

## Инструменты

### Данные (только чтение)

| Инструмент | Что делает |
|------|--------------|
| `coach_list_workouts(limit=20)` | История тренировок (новые сверху) плюс компактная сериализация, которую видит модель |
| `coach_get_workout(workout_id)` | Одна тренировка целиком |
| `coach_list_body_weights()` | История взвешиваний |
| `coach_list_waists()` | История замеров талии (см) |
| `coach_get_catalog()` | Каталог упражнений (других не существует) |
| `coach_get_state()` | Состояние подготовки: фаза и её параметры, неделя блока, целевой объём недели, плановая разгрузка, флаг возврата после перерыва |
| `coach_list_events()` | События — перерывы в тренировках с причиной (новые сверху); `end_date: null` — то, которое идёт сейчас |

### Состояние подготовки (запись)

| Инструмент | Что делает |
|------|--------------|
| `coach_set_phase(phase, params?)` | Переключить фазу руками (`cut_recomp` / `lean_bulk` / `maintenance`); стартом фазы становится сегодня. Автопереключений нет: при достигнутой цели промпт лишь просит модель *предложить* смену |
| `coach_update_state(waist_limit_cm?, waist_base_cm?)` | Глобальные ручки: жёсткий лимит талии, базовая талия фазы |
| `coach_update_profile(block, text?)` | Заменить один блок профиля (пустой текст удаляет блок); прошлая версия файла остаётся рядом как `.bak` с таймстампом |
| `coach_add_waist(waist_cm, entry_date?)` | Записать замер талии (один на дату, повтор перезаписывает) |
| `coach_delete_waist(entry_id)` | Удалить ошибочный замер |
| `coach_add_event(text, start_date?, end_date?)` | Записать событие — перерыв с причиной («болел», «командировка»). Без даты конца событие идёт сейчас, и открытым может быть только одно; будущие даты отвергаются |
| `coach_update_event(event_id, text?, start_date?, end_date?)` | Поправить событие; не переданные поля не меняются, так что «закончилось вчера» — один вызов с `end_date`. `end_date=""` открывает событие снова |
| `coach_delete_event(event_id)` | Удалить событие, записанное по ошибке |

### Движок рекомендаций

| Инструмент | Что делает |
|------|--------------|
| `coach_get_stored_recommendation()` | Совет, который сейчас лежит в кэше приложения (status / based_on / payload / токены / stale) |
| `coach_preview_prompt(limit=20)` | Точный system+user промпт и JSON-схема — **без вызова API** (бесплатно). Включает фазу, неделю блока и положение в цикле |
| `coach_debug_recommendation(limit=20)` | Полный прогон генерации с семантическим валидатором: каждая попытка (сырой ответ + нарушения + репромпт), итог, токены и стоимость. В базу не пишет |
| `coach_generate_recommendation(limit=20, store=false)` | Сгенерировать валидированный совет; `store=true` перезаписывает кэш приложения |
| `coach_weekly_report(days=7, fresh=false)` | Недельный отчёт тренера (Markdown): итоги недели против целей, ПР, тренды веса и талии, дисциплина, фокус следующей недели. Всегда про последнюю **закрытую** календарную неделю (пн–вс), отдаётся из кэша мгновенно (таймер в ночь на понедельник генерирует его заранее); `fresh=true` пересобирает за токены |
| `coach_phase_summary(history_index?)` | Что дала фаза подготовки: длительность, сессии и частота, вес и талия старт→финиш с темпом, ПР, дисциплина. Без аргументов — текущая фаза; индекс — закрытая фаза из журнала |
| `coach_costs()` | Расход на Claude API по месяцам: генерации совета и недельные отчёты (вызовы, токены, оценка в USD) |

Все инструменты принимают необязательный `user_id` (по умолчанию — настроенный
пользователь).

## Файлы состояния (рядом с базой)

- `coach_profile.json` — текстовый профиль атлета (личный и медицинский
  контекст; в репозитории его нет, форма описана в `backend/README.md`; путь
  переопределяется `COACH_PROFILE_PATH`);
- `coach_strategy.md` — рабочий документ стратегии, срез которого уезжает в
  промпт (`COACH_STRATEGY_PATH`);
- `coach_state.json` — структурированное состояние подготовки: фаза, её старт,
  переопределения по фазам, лимит и база талии, журнал фаз (`COACH_STATE_PATH`).

## Окружение

| Переменная | По умолчанию | Примечание |
|-----|---------|-------|
| `ANTHROPIC_API_KEY` | — | Нужен для `coach_debug_recommendation` / `coach_generate_recommendation` / `coach_weekly_report` |
| `COACH_MCP_BACKEND_DIR` | `../backend` | Корень backend с пакетом `trainer/`. На VPS: `/opt/trainer-miniapp/app` |
| `MINIAPP_DB_PATH` | `<backend_dir>/data/trainer.db` | Путь к SQLite. На VPS: `/opt/trainer-miniapp/data/trainer.db` |
| `EXERCISE_CATALOG_PATH` | `<backend_dir>/resources/exercises.json` | Каталог упражнений; едет с кодом backend. На VPS: `/opt/trainer-miniapp/app/resources/exercises.json` |
| `COACH_MCP_PROFILE_PATH` / `COACH_PROFILE_PATH` | `<каталог базы>/coach_profile.json` | Профиль атлета; первая переменная имеет приоритет |
| `COACH_MCP_STRATEGY_PATH` / `COACH_STRATEGY_PATH` | `<каталог базы>/coach_strategy.md` | Документ стратегии |
| `COACH_STATE_PATH` | `<каталог базы>/coach_state.json` | Состояние подготовки |
| `COACH_MCP_USER_ID` | `3` | Для какого пользователя работать |
| `ANTHROPIC_MODEL` | из `anthropic_client` (`claude-opus-5`) | Модель генерации |
| `COACH_MCP_HOST` / `COACH_MCP_PORT` | `127.0.0.1` / `8001` | Адрес streamable-http (8001, чтобы не пересечься с investor-mcp на 8000) |
| `COACH_MCP_PATH` | `/mcp` | HTTP-путь; в проде — секретный |
| `COACH_MCP_AUTH_TOKEN` | — | Если задан, требуется `Authorization: Bearer <token>` |
| `COACH_MCP_ALLOWED_HOSTS` | — | Список через запятую → строгая защита от DNS-rebinding |

## Локальный запуск (stdio, например Claude Desktop)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r coach_mcp/requirements.txt
ANTHROPIC_API_KEY=sk-ant-... python coach_mcp/server.py
```

## Деплой на VPS (streamable-http за Cloudflare Tunnel)

Та же схема, что у `investor-mcp`. Backend уже лежит в `/opt/trainer-miniapp/app`,
поэтому импортёр указывает туда и переиспользует секреты из `backend.env`.
Обновление кода — `./backend/infra/deploy/deploy.sh coach-mcp`: копирует
`server.py` и этот README и перезапускает юнит.

```bash
# один раз
python3 -m venv /opt/coach-mcp/venv
/opt/coach-mcp/venv/bin/pip install -r requirements.txt
# окружение (свой EnvironmentFile или тот же, что у backend):
#   COACH_MCP_BACKEND_DIR=/opt/trainer-miniapp/app
#   MINIAPP_DB_PATH=/opt/trainer-miniapp/data/trainer.db
#   ANTHROPIC_API_KEY=...           (уже есть в /etc/trainer-miniapp/backend.env)
#   COACH_MCP_PATH=/<случайный-секретный-путь>/mcp
/opt/coach-mcp/venv/bin/python server.py --transport streamable-http --host 127.0.0.1 --port 8001
```

Дальше — публичный hostname в Cloudflare Tunnel → `http://localhost:8001`, а URL
коннектора в Claude — `https://<host>/<секретный-путь>/mcp`.

> **Про память:** на VPS ~1 ГБ, и там уже крутятся backend, два контейнера Caddy,
> cloudflared и туннель investor-mcp. Второй процесс `mcp`+uvicorn добавляет
> ~50–80 МБ — проверь запас по `free -m` (или запускай по требованию), прежде чем
> оставлять его постоянно.

> **Безопасность:** инструменты открывают всю историю тренировок и умеют тратить
> токены Anthropic (`coach_debug_recommendation` / `coach_generate_recommendation`).
> За публичным туннелем — секретный `COACH_MCP_PATH` и/или `COACH_MCP_AUTH_TOKEN`.
