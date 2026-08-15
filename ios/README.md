# Trainer iOS

Нативный SwiftUI-клиент для текущего `Trainer` backend.

Этот проект повторяет актуальную логику Telegram Mini App из соседнего репозитория:

- три вкладки: `Trainings`, `Progress`, `Weight`;
- история тренировок с compact-summary сетов;
- редактирование и удаление сохранённой тренировки через свайп карточки влево;
- создание и редактирование тренировки через отдельный `New Workout` flow;
- preview-карточки основной шестёрки упражнений;
- planned add по синей кнопке `+`;
- ручной ввод сета через long press по `+`;
- per-set effort: `🙂 / 😐 / 😣`;
- draft тренировки в `UserDefaults`;
- кольцевой прогресс по основной шестёрке;
- экран прогресса по упражнению;
- экран веса тела с inline-вводом, графиком и удалением записи по тапу на точку.
- нативная сессия без внешней авторизации: клиент запрашивает backend-user `id=3`.

## Backend

По умолчанию приложение ходит в:

```text
https://trainer.superbatonec.org
```

Для локальной разработки можно поменять URL через шестерёнку на `http://127.0.0.1:8080` и запустить backend из соседнего проекта:

```bash
cd /Users/batonec/AndroidStudioProjects/Trainer
MINIAPP_ALLOW_DEBUG_USER=1 python3 backend/server.py
```

В iOS-приложении адрес backend можно поменять через шестерёнку на экране `Trainings`.

Нативный iOS-клиент не получает Telegram `initData`. Сейчас в клиенте намеренно захардкожен backend-user:

- iOS отправляет `shell=ios` и `native_user_id=3` в `POST /api/session/resolve`;
- backend проверяет, что такой пользователь есть в SQLite;
- после этого backend выдаёт обычную cookie `trainer_session`;
- все остальные endpoints остаются теми же, что у Mini App.

Если пользователя `id=3` нет на backend, приложение покажет экран повтора подключения и настройки URL.

## Сборка

Открой:

```text
TrainerIOS.xcodeproj
```

Или проверь из терминала:

```bash
xcodebuild \
  -project TrainerIOS.xcodeproj \
  -scheme TrainerIOS \
  -destination 'generic/platform=iOS Simulator' \
  build
```

## Тесты

В проекте есть XCTest target `TrainerIOSTests`. Он проверяет Swift-перенос бизнес-логики Mini App, модели API, backend endpoints из README, fallback-каталог `Resources/exercises.json` и persistence в `UserDefaults`.

```bash
xcodebuild \
  -project TrainerIOS.xcodeproj \
  -scheme TrainerIOS \
  -destination 'platform=iOS Simulator,name=iPhone 17' \
  test
```

## Что внутри

- `TrainerIOSApp.swift` — entrypoint приложения.
- `Models.swift` — модели API, draft и UI-состояний.
- `APIClient.swift` — HTTP-клиент для текущих backend endpoints.
- `TrainerLogic.swift` — перенос бизнес-логики Mini App: сортировки, planned sets, summary, progress, weight stats.
- `TrainerStore.swift` — observable app state, persistence draft/settings, API mutations.
- `Views.swift` — SwiftUI-экраны и компоненты.
- `Resources/exercises.json` — fallback-каталог упражнений из Mini App.

## API-контракт

Клиент использует существующие endpoints:

- `POST /api/session/resolve`
- `POST /api/session/logout`
- `GET /data/exercises.json`
- `GET /api/workouts`
- `POST /api/workouts`
- `PUT /api/workouts/{id}`
- `DELETE /api/workouts/{id}`
- `GET /api/body-weights`
- `POST /api/body-weights`
- `DELETE /api/body-weights/{id}`
- `GET /api/recommendations/next` — кэш «Совета тренера» (мгновенно, не ждёт генерации)
- `POST /api/recommendations/refresh` — форс-генерация рекомендации (синхронно, на отдельной 90-сек сессии)

### «Совет тренера»

Карточка `CoachCard` вверху TodayScreen показывает рекомендацию следующей тренировки
(`focus`, нагрузка, упражнения с подходами и пояснениями, сворачиваемое «Почему так»).
`GET /api/recommendations/next` читается после старта (вне 3-сек дедлайна `reload`);
«Обновить» дёргает `POST /api/recommendations/refresh` (10–40 с, показывается оверлей).
Состояния: `none` / `pending` / `ready` (+ `stale`) / `failed`.

Если клиент не дождался ответа на `refresh`, карточка ошибки НЕ показывается: сервер
продолжает генерацию и сохраняет результат, даже когда запрос уже оборван. По таймауту
(`TrainerAPIError.timeout` / `URLError.timedOut`) iOS остаётся в `pending` и переходит на
опрос дешёвого `GET /api/recommendations/next` — окно 180 с, чтобы покрыть худший случай
сервера (авто-репромпт семантического валидатора удваивает вызов модели). Готовый план
подхватывается сам. Любая другая ошибка (401, 5xx, нет сети, битый payload) по-прежнему
даёт `failed` с кнопкой «Повторить».

**«Применить в план»** делает совет **планом** (`TrainerStore.appliedPlan`, живёт в
`UserDefaults`), а НЕ выполненными подходами — тренировка не стартует, пока не записан
первый сет. Пока план применён:

- карточки секции идут в порядке рекомендации, цель показывается с весом («90кг ×12»);
- заголовок секции — «План от тренера» с кнопкой «Сбросить»; превью-карточку можно убрать
  из плана long press-ом («Убрать из плана»);
- кнопка карточки совета — «В плане ✓» (для нового совета — «Применить новый»).

Быстрый «+» предлагает следующий подход по приоритету: последний **кастомный** подход
повторяется (а не сбрасывается к плану) → цель применённого плана по индексу → история
+1 повтор → 12×0. Long press по «+» открывает конструктор подхода (`QuickAddSheet`).

При сохранении новой тренировки с применённым планом в payload уходит
`data.recommendation` — снапшот совета для будущей статистики «факт vs план»
(см. backend/README), после чего план очищается, а рекомендация перегенерируется.
