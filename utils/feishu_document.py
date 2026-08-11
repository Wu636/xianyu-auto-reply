"""Create a Feishu cloud document by importing a generated rich document."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional

import httpx


class FeishuDocumentError(RuntimeError):
    pass


class FeishuDocumentClient:
    API_BASE = "https://open.feishu.cn/open-apis"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        timeout_seconds: float = 45.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "").strip()
        if not self.app_id or not self.app_secret:
            raise FeishuDocumentError("请先填写飞书自建应用 App ID 和 App Secret")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds), follow_redirects=True, transport=transport
        )

    async def __aenter__(self) -> "FeishuDocumentClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._client.aclose()

    @staticmethod
    def _check(payload: Dict[str, Any], action: str) -> Dict[str, Any]:
        if not isinstance(payload, dict) or int(payload.get("code") or 0) != 0:
            raise FeishuDocumentError(f"{action}失败：{payload.get('msg') or payload}")
        return payload.get("data") or {}

    async def tenant_access_token(self) -> str:
        response = await self._client.post(
            f"{self.API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise FeishuDocumentError(f"飞书授权返回 HTTP {response.status_code}") from exc
        if response.is_error or int(payload.get("code") or 0) != 0:
            raise FeishuDocumentError(f"飞书授权失败：{payload.get('msg') or response.status_code}")
        token = str(payload.get("tenant_access_token") or "").strip()
        if not token:
            raise FeishuDocumentError("飞书授权响应缺少 tenant_access_token")
        return token

    async def import_document(
        self,
        document_path: Path,
        *,
        document_name: str,
        folder_token: str = "",
        poll_attempts: int = 20,
    ) -> Dict[str, Any]:
        path = Path(document_path)
        if not path.is_file():
            raise FeishuDocumentError("待导入的文档文件不存在")
        extension = path.suffix.lower().lstrip('.')
        if extension not in {'docx', 'md'}:
            raise FeishuDocumentError("飞书导入仅支持 DOCX 或 Markdown")
        media_type = (
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            if extension == 'docx' else 'text/markdown'
        )
        token = await self.tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        file_bytes = path.read_bytes()
        upload = await self._client.post(
            f"{self.API_BASE}/drive/v1/medias/upload_all",
            headers=headers,
            data={
                "file_name": path.name,
                "parent_type": "ccm_import_open",
                "size": str(len(file_bytes)),
                "extra": json.dumps({"obj_type": "docx", "file_extension": extension}),
            },
            files={"file": (path.name, file_bytes, media_type)},
        )
        try:
            upload_data = self._check(upload.json(), "上传飞书导入素材")
        except ValueError as exc:
            raise FeishuDocumentError(f"上传飞书导入素材返回 HTTP {upload.status_code}") from exc
        file_token = str(upload_data.get("file_token") or "").strip()
        if not file_token:
            raise FeishuDocumentError("飞书上传响应缺少 file_token")

        task = await self._client.post(
            f"{self.API_BASE}/drive/v1/import_tasks",
            headers={**headers, "Content-Type": "application/json; charset=utf-8"},
            json={
                "file_extension": extension,
                "file_token": file_token,
                "type": "docx",
                "file_name": str(document_name or path.stem)[:200],
                "point": {"mount_type": 1, "mount_key": str(folder_token or "").strip()},
            },
        )
        try:
            task_data = self._check(task.json(), "创建飞书文档导入任务")
        except ValueError as exc:
            raise FeishuDocumentError(f"创建飞书文档导入任务返回 HTTP {task.status_code}") from exc
        ticket = str(task_data.get("ticket") or "").strip()
        if not ticket:
            raise FeishuDocumentError("飞书导入任务响应缺少 ticket")

        for attempt in range(max(1, int(poll_attempts))):
            if attempt:
                await asyncio.sleep(min(0.5 + attempt * 0.25, 2.0))
            result_response = await self._client.get(
                f"{self.API_BASE}/drive/v1/import_tasks/{ticket}", headers=headers
            )
            try:
                result_data = self._check(result_response.json(), "查询飞书文档导入任务")
            except ValueError as exc:
                raise FeishuDocumentError(f"查询飞书文档导入任务返回 HTTP {result_response.status_code}") from exc
            result = result_data.get("result") or {}
            job_status = result.get("job_status")
            if job_status in (0, "0") and result.get("url"):
                return {
                    "ticket": ticket,
                    "token": result.get("token"),
                    "type": result.get("type") or "docx",
                    "url": result.get("url"),
                }
            if job_status not in (None, 1, "1", 2, "2"):
                raise FeishuDocumentError(
                    f"飞书文档导入失败：{result.get('job_error_msg') or job_status}"
                )
        raise FeishuDocumentError("飞书文档仍在导入，请稍后重新生成或到飞书云空间查看")

    async def import_markdown(
        self,
        markdown_path: Path,
        *,
        document_name: str,
        folder_token: str = "",
        poll_attempts: int = 20,
    ) -> Dict[str, Any]:
        """Backward-compatible wrapper for older callers and packages."""
        return await self.import_document(
            markdown_path,
            document_name=document_name,
            folder_token=folder_token,
            poll_attempts=poll_attempts,
        )
