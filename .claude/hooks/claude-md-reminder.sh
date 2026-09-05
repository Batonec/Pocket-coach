#!/usr/bin/env bash
# Stop-хук: один раз за сессию напоминает обновить CLAUDE.md, если правки
# затронули структуру проекта, а сам CLAUDE.md остался нетронутым.
#
# Триггеры выбраны по разделу «Поддержка этого файла» в CLAUDE.md — обычная
# фича внутри существующих слоёв хук не будит:
#   1) появился или исчез модуль в backend/ · coach_mcp/ · ios/TrainerIOS/ (и Views/);
#   2) тронуты места, которые надо держать в синхроне руками
#      (CI-воркфлоу, deploy.sh, project.pbxproj);
#   3) тронута граница «алгоритм / LLM» (recommender.py и его модули prompt_builder.py,
#      plan_validator.py, anthropic_client.py; coach_signals.py).
set -uo pipefail

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)" || exit 0
cd "$repo" || exit 0

payload="$(cat)"
session="$(printf '%s' "$payload" | jq -r '.session_id // "nosession"' 2>/dev/null)"
[ -n "$session" ] || session="nosession"

# Напоминаем не чаще одного раза за сессию: иначе Stop-хук зациклится, если
# Claude решит, что обновлять CLAUDE.md не нужно.
stamp="${TMPDIR:-/tmp}/pocket-coach-claudemd-${session}"
[ -e "$stamp" ] && exit 0

# -uall обязателен: без него git схлопывает целиком новый каталог в одну
# строку «?? dir/», и новые файлы внутри него хук не увидит.
changed="$(git status --porcelain -uall 2>/dev/null)" || exit 0
[ -n "$changed" ] || exit 0

strip_status() { cut -c4- | sed -e 's/^"//' -e 's/"$//' -e 's/.* -> //'; }

paths="$(printf '%s\n' "$changed" | strip_status)"
# Только появившиеся / исчезнувшие файлы: статусы ??, A_, _A, D_, _D.
newgone="$(printf '%s\n' "$changed" | grep -E '^(\?\?|[AD].|.[AD]) ' | strip_status)"

printf '%s\n' "$paths" | grep -qx 'CLAUDE\.md' && exit 0

reasons=""
add_reason() { reasons="${reasons}${reasons:+; }$1"; }

printf '%s\n' "$newgone" \
  | grep -qE '^((backend|coach_mcp)/[^/]+\.py|ios/TrainerIOS(Tests)?/([^/]+/)?[^/]+\.swift)$' \
  && add_reason "появился или исчез модуль в backend/coach_mcp/ios"

printf '%s\n' "$paths" \
  | grep -qE '^(\.github/workflows/|backend/deploy/deploy\.sh$|ios/TrainerIOS\.xcodeproj/project\.pbxproj$)' \
  && add_reason "тронуты места ручного синхрона (CI / deploy.sh / project.pbxproj)"

printf '%s\n' "$paths" \
  | grep -qE '^backend/(recommender|anthropic_client|plan_validator|prompt_builder|coach_signals)\.py$' \
  && add_reason "тронута граница «алгоритм / LLM»"

[ -n "$reasons" ] || exit 0

: > "$stamp"

jq -n --arg r "$reasons" '{
  decision: "block",
  reason: ("Правки затронули устройство проекта (" + $r + "), а CLAUDE.md не менялся. " +
           "Сверься с разделом «Поддержка этого файла» в CLAUDE.md: если изменение действительно " +
           "меняет описанную там картину — обнови CLAUDE.md сейчас, в тех же правках. " +
           "Если это обычная фича внутри существующих слоёв — скажи об этом одной строкой и заканчивай. " +
           "Напоминание срабатывает один раз за сессию."),
  systemMessage: ("CLAUDE.md: проверяю, не устарел ли (" + $r + ")")
}'
