"""Project-root and database integrity checks used by safe start/backup scripts."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable


SENTINEL_NAME = ".xianyu-project-root"
SENTINEL_VALUE = "xianyu-auto-reply:w636:protected-root:v1"
ESSENTIAL_FILES = (
    "Start.py",
    "reply_server.py",
    "db_manager.py",
    "auto_updater.py",
    "requirements.txt",
    "static/index.html",
    "static/js/app.js",
    "utils/ip_content_studio.py",
    "utils/content_productizer.py",
    "utils/safe_file_cleanup.py",
)
EXCLUDED_PARTS = {
    ".git", "venv", ".venv", "__pycache__", ".playwright-browsers",
    "node_modules", "logs", "backups", "update_backup",
}


class ProjectIntegrityError(RuntimeError):
    pass


def ensure_project_root(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    sentinel = resolved / SENTINEL_NAME
    if not sentinel.is_file():
        raise ProjectIntegrityError(f"缺少项目根标识: {sentinel}")
    value = sentinel.read_text(encoding="utf-8").strip()
    if value != SENTINEL_VALUE:
        raise ProjectIntegrityError(f"项目根标识内容异常: {sentinel}")
    return resolved


def _iter_source_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file() or any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        if path.name.endswith((".pyc", ".log")):
            continue
        yield path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_database(database: Path, minimum_bytes: int = 4096) -> Dict[str, Any]:
    if not database.is_file():
        raise ProjectIntegrityError(f"业务数据库不存在: {database}")
    size = database.stat().st_size
    if size < minimum_bytes:
        raise ProjectIntegrityError(f"业务数据库尺寸异常: {size} bytes")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
    finally:
        connection.close()
    if integrity != "ok":
        raise ProjectIntegrityError(f"SQLite 完整性检查异常: {integrity}")
    if tables < 20:
        raise ProjectIntegrityError(f"业务表数量异常: {tables}")
    return {"path": str(database), "size": size, "integrity": integrity, "tables": tables}


def verify_project(root: Path, database: Path | None = None) -> Dict[str, Any]:
    root = ensure_project_root(root)
    missing = [relative for relative in ESSENTIAL_FILES if not (root / relative).is_file()]
    if missing:
        raise ProjectIntegrityError("缺少关键源码: " + ", ".join(missing))
    source_files = list(_iter_source_files(root))
    if len(source_files) < 80:
        raise ProjectIntegrityError(f"源码文件数量异常: {len(source_files)}")
    result: Dict[str, Any] = {
        "root": str(root),
        "source_file_count": len(source_files),
        "critical_hashes": {relative: _sha256(root / relative) for relative in ESSENTIAL_FILES},
    }
    if database is not None:
        result["database"] = verify_database(database.expanduser().resolve())
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--database")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = verify_project(Path(args.root), Path(args.database) if args.database else None)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        db = result.get("database") or {}
        print(
            f"项目完整性通过: source_files={result['source_file_count']} "
            f"tables={db.get('tables', '-')} db_bytes={db.get('size', '-')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
