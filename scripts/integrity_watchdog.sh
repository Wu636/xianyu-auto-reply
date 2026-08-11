#!/bin/bash
set -u

PROJECT_DIR="${1:?project directory required}"
RECOVERY_ROOT="${2:?recovery root required}"
SENTINEL="$PROJECT_DIR/.xianyu-project-root"
DATABASE="$PROJECT_DIR/data/xianyu_data.db"
ESSENTIAL=(
  "$PROJECT_DIR/Start.py"
  "$PROJECT_DIR/reply_server.py"
  "$PROJECT_DIR/db_manager.py"
  "$PROJECT_DIR/static/index.html"
  "$PROJECT_DIR/static/js/app.js"
)

while true; do
    healthy=1
    [ -f "$SENTINEL" ] || healthy=0
    [ -s "$DATABASE" ] || healthy=0
    for path in "${ESSENTIAL[@]}"; do
        [ -f "$path" ] || healthy=0
    done
    source_count="$(find "$PROJECT_DIR" -type f -not -path '*/.git/*' 2>/dev/null | wc -l | tr -d ' ')"
    [ "${source_count:-0}" -ge 80 ] || healthy=0

    if [ "$healthy" -ne 1 ]; then
        stamp="$(date '+%Y-%m-%d %H:%M:%S')"
        printf '%s integrity alarm: root=%s files=%s\n' "$stamp" "$PROJECT_DIR" "${source_count:-0}" \
            >> "$RECOVERY_ROOT/INTEGRITY_ALERT.log"
        server_pid="$(cat "$RECOVERY_ROOT/server.pid" 2>/dev/null || true)"
        if [ -n "$server_pid" ] && kill -0 "$server_pid" 2>/dev/null; then
            kill -TERM "$server_pid" 2>/dev/null || true
        fi
        if command -v screen >/dev/null 2>&1; then
            screen -S xianyu8090 -X quit >/dev/null 2>&1 || true
        fi
        exit 80
    fi
    sleep 5
done
