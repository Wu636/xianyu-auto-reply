#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PROJECT_DIR}/venv/bin/python"
STATE_ROOT="${XIANYU_STATE_DIR:-${HOME}/xianyu-auto-reply-state}"
RECOVERY_ROOT="${XIANYU_RECOVERY_DIR:-${HOME}/xianyu-auto-reply-recovery}"
SOURCE_DB="${PROJECT_DIR}/data/xianyu_data.db"
SOURCE_KEY="${PROJECT_DIR}/data/.secret_encryption.key"
STATE_DB="${STATE_ROOT}/xianyu_data.db"
STATE_KEY="${STATE_ROOT}/.secret_encryption.key"

[ -x "$PYTHON_BIN" ] || PYTHON_BIN="$(command -v python3)"
"$PYTHON_BIN" "${PROJECT_DIR}/utils/project_guard.py" --root "$PROJECT_DIR" >/dev/null

mkdir -p "$STATE_ROOT" "$RECOVERY_ROOT" "${PROJECT_DIR}/data"
chmod 700 "$STATE_ROOT" "$RECOVERY_ROOT"

resolved_db="$(readlink "$SOURCE_DB" 2>/dev/null || true)"
if [ -L "$SOURCE_DB" ] && [ "$resolved_db" = "$STATE_DB" ] && [ -s "$STATE_DB" ]; then
    sqlite3 "$STATE_DB" 'PRAGMA integrity_check;' | grep -qx 'ok'
else
    [ -s "$SOURCE_DB" ] || {
        echo "业务数据库缺失: $SOURCE_DB"
        exit 2
    }
    STAMP="$(date '+%Y%m%d_%H%M%S')"
    DB_TEMP="${STATE_ROOT}/.xianyu_data-${STAMP}.db.partial"
    sqlite3 "$SOURCE_DB" ".backup '$DB_TEMP'"
    sqlite3 "$DB_TEMP" 'PRAGMA integrity_check;' | grep -qx 'ok'
    chmod 600 "$DB_TEMP"
    if [ -s "$STATE_DB" ]; then
        sqlite3 "$STATE_DB" ".backup '${RECOVERY_ROOT}/state-db-before-${STAMP}.db'"
        chmod 600 "${RECOVERY_ROOT}/state-db-before-${STAMP}.db"
    fi
    mv "$DB_TEMP" "$STATE_DB"
    chmod 600 "$STATE_DB"
    if [ ! -L "$SOURCE_DB" ]; then
        mv "$SOURCE_DB" "${RECOVERY_ROOT}/project-db-before-external-${STAMP}.db"
        chmod 600 "${RECOVERY_ROOT}/project-db-before-external-${STAMP}.db"
    else
        rm -- "$SOURCE_DB"
    fi
    ln -s "$STATE_DB" "$SOURCE_DB"
fi

if [ -L "$SOURCE_KEY" ]; then
    resolved_key="$(readlink "$SOURCE_KEY" 2>/dev/null || true)"
    if [ "$resolved_key" != "$STATE_KEY" ]; then
        rm -- "$SOURCE_KEY"
        ln -s "$STATE_KEY" "$SOURCE_KEY"
    fi
elif [ -s "$SOURCE_KEY" ]; then
    cp -p "$SOURCE_KEY" "$STATE_KEY"
    chmod 600 "$STATE_KEY"
    mv "$SOURCE_KEY" "${RECOVERY_ROOT}/encryption-key-before-external-$(date '+%Y%m%d_%H%M%S')"
    ln -s "$STATE_KEY" "$SOURCE_KEY"
else
    "$PYTHON_BIN" - "$STATE_KEY" <<'PY'
from pathlib import Path
import os
import sys
from cryptography.fernet import Fernet

target = Path(sys.argv[1])
target.write_bytes(Fernet.generate_key())
os.chmod(target, 0o600)
PY
    ln -s "$STATE_KEY" "$SOURCE_KEY"
fi

sqlite3 "$SOURCE_DB" 'PRAGMA integrity_check;' | grep -qx 'ok'
"$PYTHON_BIN" "${PROJECT_DIR}/utils/project_guard.py" \
    --root "$PROJECT_DIR" --database "$SOURCE_DB" >/dev/null

printf '受保护状态区已启用\n数据库: %s\n加密密钥: %s\n项目链接: %s\n' \
    "$STATE_DB" "$STATE_KEY" "$SOURCE_DB"
