#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# backend/ — на два уровня выше: скрипт лежит в backend/infra/deploy/.
BACKEND_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

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
BACKEND_SERVICE="${TRAINER_BACKEND_SERVICE:-trainer-miniapp-backend.service}"
# Обратный прокси перед backend — docker-контейнер с Caddy, конфиг лежит рядом
# (Caddyfile). Имена «miniapp» исторические, как и $REMOTE_BASE.
PROXY_CONTAINER="${TRAINER_PROXY_CONTAINER:-trainer-miniapp-caddy}"


log() {
  printf '[deploy] %s\n' "$*"
}


usage() {
  cat <<EOF
Usage:
  $(basename "$0") backend
  $(basename "$0") coach-mcp
  $(basename "$0") proxy
  $(basename "$0") all

Environment variables:
  TRAINER_VPS_HOST         SSH target (required; may come from infra/deploy/target.local)
  TRAINER_REMOTE_BASE      Remote base dir, default: $REMOTE_BASE
  TRAINER_BACKEND_SERVICE  systemd service name, default: $BACKEND_SERVICE
  TRAINER_PROXY_CONTAINER  docker container running Caddy, default: $PROXY_CONTAINER
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
    rsync -az --delete --exclude __pycache__ "${src}/" "${TARGET_HOST}:${dest}/"
    return
  fi

  log "rsync unavailable, using tar+ssh fallback"

  local archive
  archive="$(mktemp "${TMPDIR:-/tmp}/trainer-backend.XXXXXX.tar")"
  tar -C "$src" --exclude __pycache__ -cf "$archive" .
  scp "$archive" "${TARGET_HOST}:/tmp/trainer-backend-sync.tar" >/dev/null
  rm -f "$archive"

  remote "mkdir -p '$dest' && find '$dest' -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar -C '$dest' -xf /tmp/trainer-backend-sync.tar && rm -f /tmp/trainer-backend-sync.tar"
}


# Coach MCP на VPS живёт ВНЕ $REMOTE_BASE (свой venv и systemd-юнит) — это
# единственный скриптовый способ его обновить.
COACH_MCP_REMOTE_DIR="${COACH_MCP_REMOTE_DIR:-/opt/coach-mcp/app}"
COACH_MCP_SERVICE="${COACH_MCP_SERVICE:-coach-mcp.service}"

deploy_coach_mcp() {
  log "Deploying coach-mcp to $TARGET_HOST:$COACH_MCP_REMOTE_DIR"
  scp "$BACKEND_DIR/../coach_mcp/server.py" "${TARGET_HOST}:${COACH_MCP_REMOTE_DIR}/server.py" >/dev/null
  scp "$BACKEND_DIR/../coach_mcp/README.md" "${TARGET_HOST}:${COACH_MCP_REMOTE_DIR}/README.md" >/dev/null
  remote "systemctl restart '$COACH_MCP_SERVICE' && systemctl is-active '$COACH_MCP_SERVICE'"
  log "coach-mcp deploy finished"
}

deploy_backend() {
  log "Deploying backend files to $TARGET_HOST"
  remote "mkdir -p '$REMOTE_BASE/app'"
  scp "$BACKEND_DIR/server.py" "${TARGET_HOST}:${REMOTE_BASE}/app/server.py" >/dev/null
  # Код едет каталогами, как и проза: пакет trainer/ и скрипты таймеров
  # infra/jobs/ целиком. Поимённого списка модулей нет специально — новому файлу негде
  # потеряться по дороге на прод.
  sync_dir "$BACKEND_DIR/trainer" "$REMOTE_BASE/app/trainer"
  sync_dir "$BACKEND_DIR/infra/jobs" "$REMOTE_BASE/app/infra/jobs"
  sync_dir "$BACKEND_DIR/prompts" "$REMOTE_BASE/app/prompts"
  sync_dir "$BACKEND_DIR/resources" "$REMOTE_BASE/app/resources"
  # Юниты и таймеры едут все: таймеры ссылаются на пути скриптов в infra/jobs/, и
  # переезд скрипта без юнита молча остановил бы таймер. Новый таймер это не
  # включает — enable делается руками один раз.
  scp "$SCRIPT_DIR"/*.service "$SCRIPT_DIR"/*.timer "${TARGET_HOST}:/etc/systemd/system/" >/dev/null
  scp "$SCRIPT_DIR/trainer-miniapp-backend.service" "${TARGET_HOST}:/etc/systemd/system/${BACKEND_SERVICE}" >/dev/null
  remote "find '$REMOTE_BASE/app' -name '*.py' -exec chmod 644 {} + && chmod 644 /etc/systemd/system/trainer-*.service /etc/systemd/system/trainer-*.timer"
  remote "test -f /etc/trainer-miniapp/backend.env"
  remote "systemctl daemon-reload && systemctl enable --now '$BACKEND_SERVICE' && systemctl restart '$BACKEND_SERVICE'"

  log "Backend deploy finished"
}

deploy_proxy() {
  log "Deploying Caddyfile to $TARGET_HOST:$REMOTE_BASE/Caddyfile"
  # Черновик кладём в caddy_config/: каталог смонтирован в контейнер как /config,
  # и caddy может провалидировать файл до того, как тот станет боевым.
  remote "cat > '$REMOTE_BASE/caddy_config/Caddyfile.new'" < "$SCRIPT_DIR/Caddyfile"
  if ! remote "docker exec '$PROXY_CONTAINER' caddy validate --config /config/Caddyfile.new --adapter caddyfile >/dev/null"; then
    remote "rm -f '$REMOTE_BASE/caddy_config/Caddyfile.new'"
    log "error: caddy validate rejected the Caddyfile, nothing changed on the VPS"
    exit 1
  fi
  # Переписываем на месте (cat >), а не scp/mv: bind-mount файла держится за
  # inode, и файл с новым inode контейнер не увидит до пересоздания.
  remote "cat '$REMOTE_BASE/caddy_config/Caddyfile.new' > '$REMOTE_BASE/Caddyfile' && rm -f '$REMOTE_BASE/caddy_config/Caddyfile.new'"
  remote "docker exec '$PROXY_CONTAINER' caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile"
  log "Proxy deploy finished"
}


main() {
  local target="${1:-}"

  require_cmd ssh
  require_cmd scp
  require_cmd tar

  case "$target" in
    backend)
      require_target_host
      deploy_backend
      ;;
    coach-mcp)
      require_target_host
      deploy_coach_mcp
      ;;
    proxy)
      require_target_host
      deploy_proxy
      ;;
    all)
      require_target_host
      deploy_backend
      deploy_coach_mcp
      deploy_proxy
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
