#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="${PROJECT_DIR}/venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "项目虚拟环境尚未安装，请先执行：python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
    exit 2
fi

export XIANYU_ENABLE_SELF_UPDATE=0
export XIANYU_ENABLE_UPDATE_DELETIONS=0
if [ -d "$PROJECT_DIR/.playwright-browsers" ]; then
    export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$PROJECT_DIR/.playwright-browsers}"
fi

echo "========================================"
echo "  闲鱼自动回复系统（保护启动）"
echo "========================================"

"$PROJECT_DIR/scripts/install_protected_state.sh"

"$PYTHON_BIN" "$PROJECT_DIR/utils/project_guard.py" \
    --root "$PROJECT_DIR" --database "$PROJECT_DIR/data/xianyu_data.db"

echo "正在创建项目外恢复点..."
recovery_archive="$($PROJECT_DIR/scripts/create_recovery_backup.sh)"
echo "恢复点: $recovery_archive"

existing_pid="$(lsof -tiTCP:8090 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
if [ -n "$existing_pid" ]; then
    existing_cwd="$(lsof -a -p "$existing_pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
    if [ "$existing_cwd" = "$PROJECT_DIR" ]; then
        "$PROJECT_DIR/stop.sh"
    else
        echo "8090 端口已由其他目录占用: pid=$existing_pid cwd=${existing_cwd:-unknown}"
        exit 3
    fi
fi

RECOVERY_ROOT="${XIANYU_RECOVERY_DIR:-${HOME}/xianyu-auto-reply-recovery}"
mkdir -p "$RECOVERY_ROOT"
old_watchdog="$(cat "$RECOVERY_ROOT/watchdog.pid" 2>/dev/null || true)"
if [ -n "$old_watchdog" ] && kill -0 "$old_watchdog" 2>/dev/null; then
    old_args="$(ps -p "$old_watchdog" -o command= 2>/dev/null || true)"
    case "$old_args" in
        *integrity_watchdog.sh*"$PROJECT_DIR"*) kill -TERM "$old_watchdog" 2>/dev/null || true ;;
    esac
fi

echo "正在启动..."
if command -v screen >/dev/null 2>&1; then
    screen -S xianyu8090 -X quit >/dev/null 2>&1 || true
    screen -dmS xianyu8090 /bin/zsh -lc "cd '$PROJECT_DIR' && exec '$PYTHON_BIN' -u '$PROJECT_DIR/Start.py' >> '$PROJECT_DIR/realtime.log' 2>&1"
    launch_mode="screen"
    server_pid=""
else
    nohup "$PYTHON_BIN" -u "$PROJECT_DIR/Start.py" >> "$PROJECT_DIR/realtime.log" 2>&1 < /dev/null &
    server_pid=$!
    launch_mode="nohup"
    printf '%s\n' "$server_pid" > "$PROJECT_DIR/.server.pid"
fi

health_url="http://127.0.0.1:8090/"
for _ in $(seq 1 60); do
    if [ "$launch_mode" = "screen" ]; then
        if ! screen -ls 2>/dev/null | grep -q '[.]xianyu8090'; then
            echo "启动会话已退出，请检查 realtime.log"
            exit 4
        fi
    elif ! kill -0 "$server_pid" 2>/dev/null; then
        echo "启动进程已退出，请检查 realtime.log"
        exit 4
    fi
    if curl -fsS --max-time 2 "$health_url" >/dev/null 2>&1; then
        listening_pid="$(lsof -tiTCP:8090 -sTCP:LISTEN 2>/dev/null | head -n 1)"
        printf '%s\n' "$listening_pid" > "$PROJECT_DIR/.server.pid"
        printf '%s\n' "$listening_pid" > "$RECOVERY_ROOT/server.pid"
        nohup /bin/bash "$PROJECT_DIR/scripts/integrity_watchdog.sh" "$PROJECT_DIR" "$RECOVERY_ROOT" \
            >> "$RECOVERY_ROOT/watchdog.log" 2>&1 < /dev/null &
        printf '%s\n' "$!" > "$RECOVERY_ROOT/watchdog.pid"
        echo "========================================"
        echo "  启动成功"
        echo "  Web管理界面: http://localhost:8090"
        echo "  完整性看守已启用"
        echo "========================================"
        exit 0
    fi
    sleep 1
done

echo "Web 管理界面在 60 秒内尚未就绪，请检查 realtime.log"
exit 5
