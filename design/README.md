# Макеты приложения (Claude Design)

Снимок дизайн-проекта **Trainer iOS** из [Claude Design](https://claude.ai/design) —
HTML/JSX-прототипы всех экранов приложения. Это исходник визуального языка
(liquid glass, JetBrains Mono, ink-палитра, accent `#FF4D1F`), по которому
написан SwiftUI-клиент в [`../ios`](../ios), и источник скриншотов для
[README](../README.md).

- `Trainer iOS.html` — точка входа (открыть в браузере)
- `screens/*.jsx` — экраны: today, history, progress, weight, picker, quickadd,
  exercise, **coach** (карточка «Совет тренера»), **signals** (баннеры),
  **events** (события и заметки)
- `styles.css`, `ios-frame.jsx`, `shell.jsx`, `icons.jsx` — дизайн-токены и рамка телефона
- `shots/` — рендер скриншотов для README
- `_*.png` — снапшоты канваса

## Как обновить макеты

Handoff-ссылки Claude Design одноразовые, поэтому синк полуавтоматический:

1. В Claude Design открой проект → **Share / Handoff** → скопируй ссылку.
2. Скажи Claude Code: «синкни макеты <ссылка>».
3. Он скачает бандл, положит изменившиеся файлы сюда и закоммитит.

Чаты с дизайн-ассистентом и загруженные фото из бандла сюда сознательно
не копируются (репозиторий публичный).

## Как пересобрать скриншоты для README

[`shots/index.html`](./shots/index.html) рендерит те же компоненты, что и канвас, но по
одному экрану на элемент; [`shots/capture.py`](./shots/capture.py) снимает их безголовым
Chromium в 2x и кладёт уменьшенные WebP с прозрачным фоном в `docs/assets/`.

```bash
pip install playwright pillow && playwright install chromium
python3 design/shots/capture.py
```

Это единственный скрипт в репозитории с pip-зависимостями вне `coach_mcp/`; в сборке,
CI и деплое он не участвует. Новый экран в README = новый элемент `#shot-<id>` в
`index.html` и его id в списке `IDS` внутри `capture.py`.

_Последний синк макетов: 2026-06-12; частичный досинк 2026-08-24._

**Снапшот неполный, и это надо знать.** 24 августа сюда приехали только файлы, нужные
экрану событий: `screens/events.jsx`, `screens/signals.jsx`, `screens/coach.jsx`,
`screens/history.jsx` и `data.jsx`. В самом Claude Design живут ещё четыре экрана, которых
здесь нет — `week`, `measurements`, `progress-blocks` и обновлённые `today`/`progress`, —
а `app.jsx` (раскладка канваса) остался июньским и про новые секции не знает. Из бандла
сознательно не копируются `uploads/` и `screenshots/`: репозиторий публичный.
