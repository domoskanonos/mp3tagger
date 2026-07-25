#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Umgebungsvariablen aus .env laden
if [ -f .env ]; then
    set -a
    . .env
    set +a
fi

CONFIG="${CONFIG:-config.json}"
PID_FILE="./radio_ripper_tag.pid"

log()  { printf '\033[1;32m[tag]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[tag]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[tag]\033[0m %s\n' "$*" >&2; }

_CLEANUP_RAN=0
cleanup() {
  [[ "$_CLEANUP_RAN" -eq 1 ]] && return
  _CLEANUP_RAN=1
  trap - INT TERM EXIT
  local rc=$?
  if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
    log "Signal an tag-Prozess (PID $PID) — Graceful Shutdown..."
    kill -TERM "$PID" 2>/dev/null || true
    for _ in {1..30}; do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.5
    done
    kill -KILL "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
    rc=$?
  fi
  rm -f "$PID_FILE"
  log "Fertig. (Exit $rc)"
  exit "$rc"
}

if ! command -v uv >/dev/null 2>&1; then
  err "uv nicht gefunden. Installieren: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi
if [[ ! -f "$CONFIG" ]]; then
  err "Config nicht gefunden: $CONFIG"
  exit 1
fi
if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    err "Tag-Prozess laeuft bereits (PID $OLD_PID)."
    exit 1
  fi
  rm -f "$PID_FILE"
fi

log "uv sync..."
uv sync --quiet
log "Starte tag-Prozess (Config: $CONFIG)"
trap cleanup INT TERM EXIT
uv run radio-ripper --config "$CONFIG" &
PID=$!
echo "$PID" > "$PID_FILE"
wait "$PID"
RC=$?
exit "$RC"
