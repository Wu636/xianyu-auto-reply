#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
RECOVERY_ROOT="${XIANYU_RECOVERY_DIR:-${HOME}/xianyu-auto-reply-recovery}"

echo "正在停止闲鱼自动回复系统..."

watchdog_pid="$(cat "$RECOVERY_ROOT/watchdog.pid" 2>/dev/null || true)"
if [ -n "$watchdog_pid" ] && kill -0 "$watchdog_pid" 2>/dev/null; then
    watchdog_args="$(ps -p "$watchdog_pid" -o command= 2>/dev/null || true)"
    case "$watchdog_args" in
        *integrity_watchdog.sh*"$PROJECT_DIR"*) kill -TERM "$watchdog_pid" 2>/dev/null || true ;;
    esac
fi

candidate_pids="$(
    {
        cat "$PROJECT_DIR/.server.pid" 2>/dev/null || true
        lsof -tiTCP:8090 -sTCP:LISTEN 2>/dev/null || true
    } | awk 'NF && !seen[$1]++'
)"

stopped=0
for pid in $candidate_pids; do
    [ -n "$pid" ] || continue
    process_cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
    process_args="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [ "$process_cwd" = "$PROJECT_DIR" ] && [[ "$process_args" == *"$PROJECT_DIR/Start.py"* ]]; then
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq 1 50); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.1
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill -KILL "$pid" 2>/dev/null || true
        fi
        stopped=1
    fi
done

if command -v screen >/dev/null 2>&1; then
    screen -S xianyu8090 -X quit >/dev/null 2>&1 || true
    screen -S xianyu-watchdog -X quit >/dev/null 2>&1 || true
fi

: > "$PROJECT_DIR/.server.pid"
: > "$RECOVERY_ROOT/server.pid"
if [ "$stopped" -eq 1 ]; then
    echo "已停止"
else
    echo "当前项目没有运行中的 8090 实例"
fi
