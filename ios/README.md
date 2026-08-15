# Trainer iOS

Нативный SwiftUI-клиент Pocket Coach: история тренировок, конструктор сессии, прогресс,
вес тела и «Совет тренера». Единственный клиент продукта — веб-мини-апп и Telegram-бот
удалены в июне 2026.

Продуктовое поведение экранов описано в [BUSINESS_LOGIC.md](../BUSINESS_LOGIC.md);
здесь — устройство клиента и его контракт с backend.

## Слои

Зависимость строго в одну сторону: чистая логика → состояние → фреймворк.

| Файл | Ответственность |
| --- | --- |
| [`TrainerLogic.swift`](./TrainerIOS/TrainerLogic.swift) | чистые статические функции: сортировки, planned sets, summary, прогресс, объёмы. Ничего не знает про сеть и состояние — на нём висит основная масса тестов |
| [`TrainerStore.swift`](./TrainerIOS/TrainerStore.swift) | `@MainActor ObservableObject`, единственный источник состояния; persistence черновика и настроек в `UserDefaults`, все мутации через API |
| [`APIClient.swift`](./TrainerIOS/APIClient.swift) | HTTP. Две `URLSession` намеренно: дефолтная с коротким таймаутом и `longRunningSession` под генерацию совета |
| [`Views.swift`](./TrainerIOS/Views.swift) | экраны и дизайн-система |
| [`Models.swift`](./TrainerIOS/Models.swift) | модели API, черновика и UI-состояний |

Голосовой слой повторяет ту же лестницу:

| Файл | Ответственность |
| --- | --- |
| [`VoiceSetParser.swift`](./TrainerIOS/VoiceSetParser.swift) | язык и грамматика фразы: упражнение, вес, повторы, тяжесть. Чистые функции |
| [`TrainerStoreVoice.swift`](./TrainerIOS/TrainerStoreVoice.swift) | команды поверх стора и тексты ответов Siri (ru/en) |
| [`VoiceIntents.swift`](./TrainerIOS/VoiceIntents.swift) | App Intents и фразы Siri — см. [VOICE_LOGGING_BRIEF.md](./VOICE_LOGGING_BRIEF.md) |

Интенты объявлены **в таргете приложения, а не в расширении**: они выполняются в его
процессе и работают с тем же `TrainerStore.shared`, что и UI. Отсюда два следствия — стор
стал синглтоном, а голосовой путь обязан работать без `boot()`: каталог упражнений он
берёт из бандла ([`Resources/exercises.json`](./TrainerIOS/Resources/exercises.json)),
если сети не было.

Фразы Siri продублированы в [`ru.lproj`](./TrainerIOS/ru.lproj/AppShortcuts.strings) и
[`en.lproj`](./TrainerIOS/en.lproj/AppShortcuts.strings) и требуют
`LM_NO_APP_SHORTCUT_LOCALIZATION = NO` в настройках таргета — иначе NLU-модель соберётся
только для языка разработки, без ошибки сборки.

## Сборка и тесты

```bash
xcodebuild -project TrainerIOS.xcodeproj -scheme TrainerIOS -destination 'generic/platform=iOS Simulator' build
```

```bash
xcodebuild -project TrainerIOS.xcodeproj -scheme TrainerIOS -destination 'platform=iOS Simulator,name=iPhone 17' test
```

Таргет `TrainerIOSTests` (129 тестов) проверяет бизнес-логику, модели API, разбор голосовых
фраз, fallback-каталог упражнений и persistence в `UserDefaults`.

> Xcode-проект держит файлы явными ссылками: новый `.swift` не попадёт в сборку, пока не
> добавлен в `project.pbxproj` — молча, без ошибки компиляции.

## Backend и сессия

По умолчанию клиент ходит в `https://trainer.superbatonec.org`; адрес меняется через
шестерёнку на экране `Trainings` — например на `http://127.0.0.1:8080` для локального
backend (`MINIAPP_ALLOW_DEBUG_USER=1 python3 backend/server.py` из корня репозитория).

Авторизации нет: это personal-build. Клиент отправляет `shell=ios` и `native_user_id=3`
в `POST /api/session/resolve`, backend проверяет наличие такого пользователя в SQLite и
выдаёт cookie `trainer_session`. Если пользователя нет — экран повтора подключения с
настройкой URL.

`reload()` ограничен дедлайном 3 с: не успели — рвём задачу и уходим в error screen с
ретраем. Рекомендация читается **вне** этого дедлайна, отдельным запросом после старта.

## API-контракт

```
POST   /api/session/resolve          POST   /api/session/logout
GET    /data/exercises.json
GET    /api/workouts                 POST   /api/workouts
PUT    /api/workouts/{id}            DELETE /api/workouts/{id}
GET    /api/body-weights             POST   /api/body-weights
DELETE /api/body-weights/{id}
GET    /api/waists                   POST   /api/waists
DELETE /api/waists/{id}
GET    /api/coach/signals            POST   /api/coach/signals/dismiss
GET    /api/recommendations/next     POST   /api/recommendations/refresh
GET    /api/reports/weekly           POST   /api/reports/weekly/read
```

`GET /api/recommendations/next` отдаёт кэш мгновенно и не ждёт генерации;
`POST /api/recommendations/refresh` — синхронная форс-генерация (10–40 с), под неё и
существует `longRunningSession`.

## «Совет тренера» в интерфейсе

Карточка `CoachCard` вверху `Trainings` показывает рекомендацию следующей тренировки:
`focus`, нагрузку, упражнения с подходами и пояснениями, сворачиваемое «Почему так».
Состояния: `none` / `pending` / `ready` (+ `stale`) / `failed`.

**Применённый план ≠ выполненные подходы.** «Применить в план» кладёт совет в
`TrainerStore.appliedPlan` (живёт в `UserDefaults`); тренировка не считается начатой, пока
не записан первый реальный сет. Пока план применён:

- карточки секции идут в порядке рекомендации, цель показывается с весом («90кг ×12»);
- заголовок секции — «План от тренера» с кнопкой «Сбросить», превью-карточку можно убрать
  из плана long press-ом;
- кнопка карточки совета — «В плане ✓» (для нового совета — «Применить новый»).

Приоритет следующего подхода по «+» един для всего конструктора: последний кастомный
подход повторяется → цель применённого плана по индексу → история +1 повтор → 12×0.

При сохранении тренировки с применённым планом в payload уходит `data.recommendation` —
**снапшот** совета для статистики «факт vs план» (см. [backend/README.md](../backend/README.md)),
после чего план очищается, а рекомендация перегенерируется.

## Голос

Полная спецификация — [VOICE_LOGGING_BRIEF.md](./VOICE_LOGGING_BRIEF.md). Коротко: команды
работают на заблокированном экране (`authenticationPolicy = .alwaysAllowed`), не выводят
приложение на передний план (`openAppWhenRun = false`) и отвечают вслух в наушники.

| Коротко | Что делает |
| --- | --- |
| «Зал жим ногами» | подход весом и повторами **из плана тренера**, без переспросов |
| «Зал подход» → *«жим ногами 80 на 10, тяжело»* | разбирает свободную фразу |
| «Зал отмена» · «Зал дальше» · «Зал финиш» | отменить последний сет · проговорить следующий · сохранить тренировку |

По-английски то же самое через `Gym`.

## Прочие брифы

- [RECOMMENDATION_CARD_BRIEF.md](./RECOMMENDATION_CARD_BRIEF.md) — карточка «Совет тренера»
- [COACH_SIGNALS_BANNER_BRIEF.md](./COACH_SIGNALS_BANNER_BRIEF.md) — баннеры коуча
- [PROGRESS_VOLUME_DISCIPLINE_BRIEF.md](./PROGRESS_VOLUME_DISCIPLINE_BRIEF.md) — объём и дисциплина на `Progress`
