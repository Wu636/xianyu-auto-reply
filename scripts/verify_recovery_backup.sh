#!/bin/bash
set -euo pipefail

ARCHIVE="${1:-}"
if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
  echo "用法: $0 /absolute/path/xianyu-full-TIMESTAMP.tar.gz"
  exit 2
fi

tar -tzf "$ARCHIVE" >/dev/null
TEMP_DIR="$(mktemp -d /private/tmp/xianyu-backup-check.XXXXXX)"
cleanup_temp() {
  case "$TEMP_DIR" in
    /private/tmp/xianyu-backup-check.*) rm -rf -- "$TEMP_DIR" ;;
  esac
}
trap cleanup_temp EXIT
tar -C "$TEMP_DIR" -xzf "$ARCHIVE" project/data/xianyu_data.db db-integrity.txt
grep -qx 'ok' "$TEMP_DIR/db-integrity.txt"
sqlite3 "$TEMP_DIR/project/data/xianyu_data.db" 'PRAGMA integrity_check;' | grep -qx 'ok'
printf '恢复包校验通过: %s\n' "$ARCHIVE"
