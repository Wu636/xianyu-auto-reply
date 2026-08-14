"""Constrained, recoverable cleanup for generated export packages.

Generated package paths are persisted in SQLite.  Treating an empty persisted
path as ``Path("")`` resolves to the process working directory, so every cleanup
must validate an exact expected target before touching the filesystem.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


class UnsafeCleanupPath(ValueError):
    """Raised when a stored path does not match its expected export target."""


@dataclass(frozen=True)
class QuarantinedPath:
    source: Path
    destination: Path


def _project_root(project_root: Optional[Path] = None) -> Path:
    return (project_root or Path(__file__).resolve().parents[1]).expanduser().resolve()


def _data_root(root: Path) -> Path:
    configured = Path(str(os.getenv("XIANYU_DATA_DIR", "data"))).expanduser()
    return (configured if configured.is_absolute() else root / configured).resolve()


def _state_root(state_root: Optional[Path] = None) -> Path:
    configured = state_root or Path(
        str(os.getenv("XIANYU_STATE_DIR", Path.home() / "xianyu-auto-reply-state"))
    )
    return configured.expanduser().resolve()


def _package_layout(
    *, kind: str, record_id: int, user_id: int, project_root: Path
) -> Tuple[Path, Path, Path]:
    if kind == "ip_content_studio":
        allowed = _data_root(project_root) / "ip_content_studio" / str(int(user_id))
        return allowed, allowed / str(int(record_id)), allowed / f"ip-content-{int(record_id)}.zip"
    if kind == "content_products":
        allowed = _data_root(project_root) / "content_products" / str(int(user_id))
        return allowed, allowed / str(int(record_id)), allowed / f"{int(record_id)}.zip"
    raise ValueError(f"未知导出包类型: {kind}")


def _resolve_stored_path(raw: Any, project_root: Path) -> Optional[Path]:
    text = str(raw or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    return (path if path.is_absolute() else project_root / path).resolve(strict=False)


def _validate_candidate(
    raw: Any, *, expected: Path, allowed_root: Path, project_root: Path, label: str
) -> Optional[Path]:
    candidate = _resolve_stored_path(raw, project_root)
    if candidate is None:
        return None
    expected = expected.resolve(strict=False)
    allowed_root = allowed_root.resolve(strict=False)
    protected = {
        Path.cwd().resolve(),
        project_root.resolve(),
        _data_root(project_root),
        Path.home().resolve(),
        Path("/").resolve(),
    }
    if candidate in protected:
        raise UnsafeCleanupPath(f"拒绝清理受保护目录（{label}）: {candidate}")
    try:
        candidate.relative_to(allowed_root)
    except ValueError as exc:
        raise UnsafeCleanupPath(f"导出路径越界（{label}）: {candidate}") from exc
    if candidate != expected:
        raise UnsafeCleanupPath(
            f"导出路径与记录不匹配（{label}）: {candidate}，预期 {expected}"
        )
    return candidate


def quarantine_export_package(
    record: Dict[str, Any],
    *,
    kind: str,
    record_id: int,
    user_id: int,
    project_root: Optional[Path] = None,
    state_root: Optional[Path] = None,
) -> List[QuarantinedPath]:
    """Move one record's exact generated package into an external recycle bin.

    Empty paths mean that no package was generated and are intentionally a
    no-op.  Any non-empty path must exactly match the deterministic package
    location for ``kind/user_id/record_id``.
    """

    root = _project_root(project_root)
    allowed, expected_dir, expected_zip = _package_layout(
        kind=kind, record_id=record_id, user_id=user_id, project_root=root
    )
    candidates = (
        _validate_candidate(
            record.get("package_dir"), expected=expected_dir, allowed_root=allowed,
            project_root=root, label="目录",
        ),
        _validate_candidate(
            record.get("package_zip"), expected=expected_zip, allowed_root=allowed,
            project_root=root, label="压缩包",
        ),
    )
    existing = [path for path in candidates if path is not None and path.exists()]
    if not existing:
        return []

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    external_state = _state_root(state_root)
    try:
        external_state.relative_to(root)
    except ValueError:
        pass
    else:
        raise UnsafeCleanupPath(f"回收站必须位于项目目录之外: {external_state}")
    trash = (
        external_state / "trash" / kind / str(int(user_id))
        / f"{int(record_id)}-{stamp}-{uuid.uuid4().hex[:8]}"
    )
    trash.mkdir(parents=True, exist_ok=False)
    moved: List[QuarantinedPath] = []
    try:
        for source in existing:
            destination = trash / source.name
            shutil.move(str(source), str(destination))
            moved.append(QuarantinedPath(source=source, destination=destination))
        (trash / "record.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        restore_quarantined_paths(moved)
        raise
    return moved


def restore_quarantined_paths(items: Iterable[QuarantinedPath]) -> None:
    """Best-effort rollback when the matching database deletion fails."""

    for item in reversed(list(items)):
        if not item.destination.exists() or item.source.exists():
            continue
        item.source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item.destination), str(item.source))
