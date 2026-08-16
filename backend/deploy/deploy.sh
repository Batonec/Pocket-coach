#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MINIAPP_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"

# Адрес VPS в публичном репозитории не хранится: он берётся из переменной
# TRAINER_VPS_HOST либо из gitignored-файла target.local рядом с этим скриптом
# (одна строка вида: TRAINER_VPS_HOST=root@1.2.3.4).
TARGET_LOCAL="$SCRIPT_DIR/target.local"
if [[ -z "${TRAINER_VPS_HOST:-}" && -f "$TARGET_LOCAL" ]]; then
  # shellcheck source=/dev/null
  source "$TARGET_LOCAL"
fi

TARGET_HOST="${TRAINER_VPS_HOST:-}"
REMOTE_BASE="${TRAINER_REMOTE_BASE:-/opt/trainer-miniapp}"
BOT_SERVICE="${TRAINER_BOT_SERVICE:-trainer-miniapp-bot.service}"
BACKEND_SERVICE="${TRAINER_BACKEND_SERVICE:-trainer-miniapp-backend.service}"


log() {
  printf '[deploy] %s\n' "$*"
}


usage() {
  cat <<EOF
Usage:
  $(basename "$0") web
  $(basename "$0") bot
  $(basename "$0") backend
  $(basename "$0") coach-mcp
  $(basename "$0") all

Environment variables:
  TRAINER_VPS_HOST      SSH target (required; may come from deploy/target.local)
  TRAINER_REMOTE_BASE   Remote base dir, default: $REMOTE_BASE
  TRAINER_BOT_SERVICE   systemd service name, default: $BOT_SERVICE
  TRAINER_BACKEND_SERVICE systemd service name, default: $BACKEND_SERVICE
EOF
}


require_target_host() {
  if [[ -z "$TARGET_HOST" ]]; then
    cat >&2 <<EOF
[deploy] error: SSH target not set.

Set it once in $TARGET_LOCAL (gitignored):

  echo 'TRAINER_VPS_HOST=root@<адрес>' > $TARGET_LOCAL

or pass it per run: TRAINER_VPS_HOST=root@<адрес> $(basename "$0") backend
EOF
    exit 1
  fi
}


require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf '[deploy] error: command not found: %s\n' "$1" >&2
    exit 1
  fi
}


remote() {
  # shellcheck disable=SC2029  # подстановка на клиенте здесь и нужна:
  # вызывающий собирает команду с ЛОКАЛЬНЫМИ $REMOTE_BASE и именами сервисов,
  # на VPS этих переменных нет.
  ssh "$TARGET_HOST" "$@"
}


remote_has_rsync() {
  remote 'command -v rsync >/dev/null 2>&1'
}


sync_dir() {
  local src="$1"
  local dest="$2"

  if command -v rsync >/dev/null 2>&1 && remote_has_rsync; then
    log "Syncing $(basename "$src") with rsync"
    rsync -az --delete "${src}/" "${TARGET_HOST}:${dest}/"
    return
  fi

  log "rsync unavailable, using tar+ssh fallback"

  local archive
  archive="$(mktemp "${TMPDIR:-/tmp}/trainer-miniapp.XXXXXX.tar")"
  tar -C "$src" -cf "$archive" .
  scp "$archive" "${TARGET_HOST}:/tmp/trainer-miniapp-sync.tar" >/dev/null
  rm -f "$archive"

  remote "mkdir -p '$dest' && find '$dest' -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar -C '$dest' -xf /tmp/trainer-miniapp-sync.tar && rm -f /tmp/trainer-miniapp-sync.tar"
}


deploy_web() {
  log "Deploying web files to $TARGET_HOST:$REMOTE_BASE/www"
  remote "mkdir -p '$REMOTE_BASE/www'"
  sync_dir "$MINIAPP_DIR/web" "$REMOTE_BASE/www"
  log "Web deploy finished"
}


deploy_bot() {
  log "Deploying bot files to $TARGET_HOST"
  remote "mkdir -p '$REMOTE_BASE/bot'"
  scp "$MINIAPP_DIR/bot.py" "${TARGET_HOST}:${REMOTE_BASE}/bot/bot.py" >/dev/null
  scp "$SCRIPT_DIR/trainer-miniapp-bot.service" "${TARGET_HOST}:/etc/systemd/system/${BOT_SERVICE}" >/dev/null

  remote "chmod 644 '$REMOTE_BASE/bot/bot.py' '/etc/systemd/system/$BOT_SERVICE'"
  remote "test -f /etc/trainer-miniapp/bot.env"
  remote "systemctl daemon-reload && systemctl enable --now '$BOT_SERVICE' && systemctl restart '$BOT_SERVICE'"

  log "Bot deploy finished"
}


# Keep this module list in sync with .github/workflows/deploy-backend.yml.
BACKEND_MODULES=(
  server.py
  backend_store.py
  recommender.py
  coach_state.py
  coach_features.py
  coach_signals.py
  coach_prompts.py
  refresh_recommendation.py
  weekly_report.py
  backup_db.py
)

# The Coach MCP server on the VPS lives OUTSIDE $REMOTE_BASE (its own venv +
# systemd unit) — this is the only scripted way to update it.
COACH_MCP_REMOTE_DIR="${COACH_MCP_REMOTE_DIR:-/opt/coach-mcp/app}"
COACH_MCP_SERVICE="${COACH_MCP_SERVICE:-coach-mcp.service}"

deploy_coach_mcp() {
  log "Deploying coach-mcp to $TARGET_HOST:$COACH_MCP_REMOTE_DIR"
  scp "$MINIAPP_DIR/../coach_mcp/server.py" "${TARGET_HOST}:${COACH_MCP_REMOTE_DIR}/server.py" >/dev/null
  scp "$MINIAPP_DIR/../coach_mcp/README.md" "${TARGET_HOST}:${COACH_MCP_REMOTE_DIR}/README.md" >/dev/null
  remote "systemctl restart '$COACH_MCP_SERVICE' && systemctl is-active '$COACH_MCP_SERVICE'"
  log "coach-mcp deploy finished"
}

deploy_backend() {
  log "Deploying backend files to $TARGET_HOST"
  remote "mkdir -p '$REMOTE_BASE/app'"
  local module
  for module in "${BACKEND_MODULES[@]}"; do
    scp "$MINIAPP_DIR/$module" "${TARGET_HOST}:${REMOTE_BASE}/app/$module" >/dev/null
  done
  # Проза промптов едет каталогом: поимённый список .md пришлось бы держать
  # в синхроне так же, как список модулей, и он бы так же протух.
  sync_dir "$MINIAPP_DIR/prompts" "$REMOTE_BASE/app/prompts"
  sync_dir "$MINIAPP_DIR/copy" "$REMOTE_BASE/app/copy"
  scp "$SCRIPT_DIR/trainer-miniapp-backend.service" "${TARGET_HOST}:/etc/systemd/system/${BACKEND_SERVICE}" >/dev/null

  local remote_paths=""
  for module in "${BACKEND_MODULES[@]}"; do
    remote_paths+=" '$REMOTE_BASE/app/$module'"
  done
  remote "chmod 644 $remote_paths '/etc/systemd/system/$BACKEND_SERVICE'"
  remote "test -f /etc/trainer-miniapp/backend.env"
  remote "systemctl daemon-reload && systemctl enable --now '$BACKEND_SERVICE' && systemctl restart '$BACKEND_SERVICE'"

  log "Backend deploy finished"
}


main() {
  local target="${1:-web}"

  require_cmd ssh
  require_cmd scp
  require_cmd tar

  case "$target" in
    web)
      require_target_host
      deploy_web
      ;;
    bot)
      require_target_host
      deploy_bot
      ;;
    backend)
      require_target_host
      deploy_backend
      ;;
    coach-mcp)
      require_target_host
      deploy_coach_mcp
      ;;
    all)
      require_target_host
      deploy_web
      deploy_bot
      deploy_backend
      deploy_coach_mcp
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      usage >&2
      exit 1
      ;;
  esac
}


main "$@"
