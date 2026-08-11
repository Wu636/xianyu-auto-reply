#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PROJECT_DIR}/venv/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="$(command -v python3)"
RECOVERY_ROOT="${XIANYU_RECOVERY_DIR:-${HOME}/xianyu-auto-reply-recovery}"
DATABASE="${PROJECT_DIR}/data/xianyu_data.db"
SECRET_KEY="${PROJECT_DIR}/data/.secret_encryption.key"

"$PYTHON_BIN" "${PROJECT_DIR}/utils/project_guard.py" \
  --root "$PROJECT_DIR" --database "$DATABASE" >/dev/null

mkdir -p "$RECOVERY_ROOT"
chmod 700 "$RECOVERY_ROOT"
STAMP="$(date '+%Y%m%d_%H%M%S')"
STAGE="$(mktemp -d "${RECOVERY_ROOT}/.stage-${STAMP}.XXXXXX")"
ARCHIVE_PART="${RECOVERY_ROOT}/xianyu-full-${STAMP}.tar.gz.partial"
ARCHIVE="${RECOVERY_ROOT}/xianyu-full-${STAMP}.tar.gz"

cleanup_stage() {
  case "$STAGE" in
    "${RECOVERY_ROOT}"/.stage-*) rm -rf -- "$STAGE" ;;
  esac
}
trap cleanup_stage EXIT

mkdir -p "$STAGE/project/data"
rsync -a \
  --exclude 'venv' \
  --exclude '.playwright-browsers' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.log' \
  --exclude 'logs' \
  --exclude 'backups' \
  --exclude 'update_backup' \
  --exclude 'data/xianyu_data.db' \
  --exclude 'data/.secret_encryption.key' \
  "$PROJECT_DIR/" "$STAGE/project/"

sqlite3 "$DATABASE" ".backup '$STAGE/project/data/xianyu_data.db'"
sqlite3 "$STAGE/project/data/xianyu_data.db" 'PRAGMA integrity_check;' > "$STAGE/db-integrity.txt"
grep -qx 'ok' "$STAGE/db-integrity.txt"
if [ -s "$SECRET_KEY" ]; then
  cp -L "$SECRET_KEY" "$STAGE/project/data/.secret_encryption.key"
  chmod 600 "$STAGE/project/data/.secret_encryption.key"
fi

tar -C "$STAGE" -czf "$ARCHIVE_PART" project db-integrity.txt
tar -tzf "$ARCHIVE_PART" >/dev/null
mv "$ARCHIVE_PART" "$ARCHIVE"
chmod 600 "$ARCHIVE"
shasum -a 256 "$ARCHIVE" > "${ARCHIVE}.sha256"
chmod 600 "${ARCHIVE}.sha256"
printf '%s\n' "$ARCHIVE" > "${RECOVERY_ROOT}/LATEST"
chmod 600 "${RECOVERY_ROOT}/LATEST"
printf '%s\n' "$ARCHIVE"
