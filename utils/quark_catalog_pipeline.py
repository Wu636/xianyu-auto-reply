"""Quark-drive helpers for the automated product catalog pipeline.

The personal Quark drive does not expose a documented public developer API for
listing a user's files or creating shares.  This client therefore talks to the
same web endpoints used by pan.quark.cn and authenticates with a user-provided
browser Cookie.  Keep calls conservative and surface authentication failures so
the user can refresh the session instead of retrying aggressively.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import mimetypes
import random
import re
import secrets
import string
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx
from PIL import Image


class QuarkDriveError(RuntimeError):
    """A readable Quark web API failure."""

    def __init__(self, message: str, *, auth_expired: bool = False):
        super().__init__(message)
        self.auth_expired = auth_expired


class CatalogAIError(RuntimeError):
    """A content or image generation failure."""


QUARK_SHARE_PERMANENT_EXPIRED_TYPE = 1


@dataclass
class QuarkEntry:
    fid: str
    parent_fid: str
    name: str
    is_dir: bool
    file_type: Any = None
    size: int = 0
    updated_at: Any = None
    created_at: Any = None
    include_items: int = 0

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "QuarkEntry":
        return cls(
            fid=str(payload.get("fid") or "").strip(),
            parent_fid=str(payload.get("pdir_fid") or "0").strip() or "0",
            name=str(payload.get("file_name") or "").strip(),
            is_dir=bool(payload.get("dir")),
            file_type=payload.get("file_type"),
            size=_safe_int(payload.get("size")),
            updated_at=payload.get("updated_at") or payload.get("l_updated_at"),
            created_at=payload.get("created_at"),
            include_items=_safe_int(payload.get("include_items")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _extract_error(payload: Any, default: str) -> str:
    if isinstance(payload, dict):
        for key in ("message", "msg", "error", "error_msg"):
            value = payload.get(key)
            if value:
                return str(value)
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("message", "msg", "error", "error_msg"):
                value = data.get(key)
                if value:
                    return str(value)
    return default


def _looks_like_auth_error(payload: Any, status_code: int = 0) -> bool:
    text = json.dumps(payload, ensure_ascii=False).lower() if isinstance(payload, (dict, list)) else str(payload).lower()
    auth_markers = (
        "未登录", "登录失效", "请登录", "not login", "unauthorized", "login required",
        "invalid cookie", "cookie expired",
    )
    return status_code in (401, 403) or any(marker in text for marker in auth_markers)


class QuarkDriveClient:
    API_BASE = "https://drive-pc.quark.cn/1/clouddrive"

    def __init__(
        self,
        cookie: str,
        *,
        timeout_seconds: float = 30.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        clean_cookie = str(cookie or "").strip()
        if not clean_cookie:
            raise ValueError("夸克 Cookie 不能为空")
        self.cookie = clean_cookie
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            transport=transport,
            headers={
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9",
                "cookie": clean_cookie,
                "origin": "https://pan.quark.cn",
                "referer": "https://pan.quark.cn/",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            },
        )

    async def __aenter__(self) -> "QuarkDriveClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.55,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Call any OpenAI-compatible chat endpoint and return one JSON object.

        This generic surface is shared by the IP content studio so it can reuse
        the same encrypted model configuration as product generation.
        """
        if not self.text_model:
            raise CatalogAIError("文案模型不能为空")
        endpoint = self._api_endpoint(self.text_base_url, "chat/completions")
        headers = self._auth_headers(self.text_api_key)
        payload: Dict[str, Any] = {
            "model": self.text_model,
            "temperature": float(temperature),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": str(system_prompt or "").strip()},
                {"role": "user", "content": str(user_prompt or "").strip()},
            ],
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        response = await self._client.post(endpoint, headers=headers, json=payload)
        if response.status_code in {400, 422} and any(
            marker in response.text.lower()
            for marker in ("response_format", "temperature", "max_tokens", "unsupported parameter")
        ):
            payload.pop("response_format", None)
            payload.pop("temperature", None)
            payload.pop("max_tokens", None)
            response = await self._client.post(endpoint, headers=headers, json=payload)
        if response.is_error:
            raise CatalogAIError(f"文案生成失败: HTTP {response.status_code} {response.text[:300]}")
        try:
            data = response.json()
            return parse_json_object(data["choices"][0]["message"]["content"])
        except Exception as exc:
            raise CatalogAIError(f"文案模型返回格式异常: {response.text[:500]}") from exc

    @staticmethod
    def _base_params() -> Dict[str, str]:
        return {
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": "",
            "__dt": str(random.randint(100, 9999)),
            "__t": str(int(time.time() * 1000)),
        }

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        query = self._base_params()
        query.update(params or {})
        try:
            response = await self._client.request(method, url, params=query, json=json_body)
        except httpx.HTTPError as exc:
            raise QuarkDriveError(f"夸克网盘连接失败: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise QuarkDriveError(f"夸克网盘返回非 JSON 响应: HTTP {response.status_code}") from exc

        status_value = payload.get("status") if isinstance(payload, dict) else None
        status_ok = status_value in (None, 0, 200, "0", "200")
        if response.is_error or not status_ok:
            auth_expired = _looks_like_auth_error(payload, response.status_code)
            message = _extract_error(payload, f"HTTP {response.status_code}")
            if auth_expired:
                message = f"夸克登录已失效，请更新 Cookie（{message}）"
            raise QuarkDriveError(message, auth_expired=auth_expired)
        return payload

    async def validate_account(self) -> Dict[str, Any]:
        payload = await self._request(
            "GET",
            "https://pan.quark.cn/account/info",
            params={"platform": "pc"},
        )
        data = payload.get("data") or {}
        if not data:
            raise QuarkDriveError("夸克登录状态无效，请更新 Cookie", auth_expired=True)
        return {
            "nickname": data.get("nickname") or data.get("user_name") or "已登录用户",
            "user_id": data.get("uid") or data.get("user_id"),
        }

    async def list_entries(
        self,
        parent_fid: str = "0",
        *,
        max_items: int = 100,
        folders_only: bool = False,
    ) -> List[QuarkEntry]:
        safe_max = max(1, min(int(max_items or 100), 500))
        result: List[QuarkEntry] = []
        page = 1
        page_size = min(100, safe_max)

        while len(result) < safe_max:
            payload = await self._request(
                "GET",
                f"{self.API_BASE}/file/sort",
                params={
                    "pdir_fid": str(parent_fid or "0"),
                    "_page": str(page),
                    "_size": str(page_size),
                    "_fetch_total": "1",
                    "_fetch_sub_dirs": "1",
                    "_sort": "file_type:asc,updated_at:desc",
                },
            )
            data = payload.get("data") or {}
            raw_list = data.get("list") or []
            if not isinstance(raw_list, list):
                raise QuarkDriveError("夸克目录响应格式异常：缺少文件列表")

            for raw in raw_list:
                if not isinstance(raw, dict):
                    continue
                entry = QuarkEntry.from_payload(raw)
                if not entry.fid or not entry.name:
                    continue
                if folders_only and not entry.is_dir:
                    continue
                result.append(entry)
                if len(result) >= safe_max:
                    break

            metadata = payload.get("metadata") or {}
            total = _safe_int(metadata.get("_total"), len(raw_list))
            count = _safe_int(metadata.get("_count"), len(raw_list))
            if not raw_list or count < page_size or page * page_size >= total:
                break
            page += 1

        return result

    async def upload_file(self, file_path: Path, *, parent_fid: str = "0") -> QuarkEntry:
        """Upload a generated small artifact through Quark's web multipart flow."""
        path = Path(file_path)
        if not path.is_file():
            raise QuarkDriveError("待上传的夸克交付文件不存在")
        size = path.stat().st_size
        if size <= 0:
            raise QuarkDriveError("待上传的夸克交付文件为空")
        if size > 5 * 1024 * 1024:
            raise QuarkDriveError("当前自动上传支持 5MB 以内的 PDF/ZIP，请缩小产物或手动上传")
        content = path.read_bytes()
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        now_ms = int(time.time() * 1000)
        pre = await self._request(
            "POST",
            f"{self.API_BASE}/file/upload/pre",
            json_body={
                "ccp_hash_update": True,
                "parallel_upload": True,
                "pdir_fid": str(parent_fid or "0"),
                "dir_name": "",
                "size": size,
                "file_name": path.name,
                "format_type": mime_type,
                "l_updated_at": now_ms,
                "l_created_at": now_ms,
            },
        )
        upload = pre.get("data") or {}
        task_id = str(upload.get("task_id") or "").strip()
        auth_info = str(upload.get("auth_info") or "")
        upload_id = str(upload.get("upload_id") or "")
        obj_key = str(upload.get("obj_key") or "")
        bucket = str(upload.get("bucket") or "ul-zb")
        callback = upload.get("callback") or {}
        if not all((task_id, upload_id, obj_key)):
            raise QuarkDriveError("夸克预上传响应缺少任务信息")
        await self._request(
            "POST",
            f"{self.API_BASE}/file/update/hash",
            json_body={
                "task_id": task_id,
                "md5": hashlib.md5(content).hexdigest(),
                "sha1": hashlib.sha1(content).hexdigest(),
            },
        )

        oss_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        agent = "aliyun-sdk-js/1.0.0 Chrome 139.0.0.0 on OS X 10.15.7 64-bit"
        put_meta = (
            f"PUT\n\n{mime_type}\n{oss_date}\n"
            f"x-oss-date:{oss_date}\nx-oss-user-agent:{agent}\n"
            f"/{bucket}/{obj_key}?partNumber=1&uploadId={upload_id}"
        )
        put_auth = await self._request(
            "POST", f"{self.API_BASE}/file/upload/auth",
            json_body={"task_id": task_id, "auth_info": auth_info, "auth_meta": put_meta},
        )
        auth_key = str((put_auth.get("data") or {}).get("auth_key") or "")
        put_url = f"https://{bucket}.pds.quark.cn/{obj_key}?partNumber=1&uploadId={upload_id}"
        put_response = await self._client.put(
            put_url,
            content=content,
            headers={
                "Content-Type": mime_type,
                "x-oss-date": oss_date,
                "x-oss-user-agent": agent,
                "authorization": auth_key,
            },
        )
        if put_response.status_code != 200:
            raise QuarkDriveError(f"夸克文件分片上传失败：HTTP {put_response.status_code}")
        etag = str(put_response.headers.get("etag") or "").strip('"')
        if not etag:
            raise QuarkDriveError("夸克文件已上传但响应缺少 ETag")

        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<CompleteMultipartUpload>\n'
            f'<Part>\n<PartNumber>1</PartNumber>\n<ETag>"{etag}"</ETag>\n</Part>\n'
            '</CompleteMultipartUpload>'
        )
        xml_md5 = base64.b64encode(hashlib.md5(xml.encode("utf-8")).digest()).decode("ascii")
        callback_b64 = base64.b64encode(
            json.dumps(callback, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        complete_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        complete_meta = (
            f"POST\n{xml_md5}\napplication/xml\n{complete_date}\n"
            f"x-oss-callback:{callback_b64}\nx-oss-date:{complete_date}\n"
            f"x-oss-user-agent:{agent}\n/{bucket}/{obj_key}?uploadId={upload_id}"
        )
        complete_auth = await self._request(
            "POST", f"{self.API_BASE}/file/upload/auth",
            json_body={"task_id": task_id, "auth_info": auth_info, "auth_meta": complete_meta},
        )
        complete_key = str((complete_auth.get("data") or {}).get("auth_key") or "")
        complete_response = await self._client.post(
            f"https://{bucket}.pds.quark.cn/{obj_key}?uploadId={upload_id}",
            content=xml,
            headers={
                "Content-Type": "application/xml",
                "Content-MD5": xml_md5,
                "x-oss-date": complete_date,
                "x-oss-user-agent": agent,
                "x-oss-callback": callback_b64,
                "authorization": complete_key,
            },
        )
        if complete_response.status_code not in {200, 203}:
            raise QuarkDriveError(f"夸克文件合并失败：HTTP {complete_response.status_code}")
        finish = await self._request(
            "POST", f"{self.API_BASE}/file/upload/finish",
            json_body={"task_id": task_id, "obj_key": obj_key},
        )
        finish_data = finish.get("data") or {}
        candidates = [finish_data, finish_data.get("file") if isinstance(finish_data, dict) else None]
        for candidate in candidates:
            if isinstance(candidate, dict) and (candidate.get("fid") or candidate.get("file_name")):
                entry = QuarkEntry.from_payload(candidate)
                if entry.fid:
                    return entry
        for entry in await self.list_entries(parent_fid, max_items=100):
            if entry.name == path.name:
                return entry
        raise QuarkDriveError("夸克上传已完成，但没有找到新文件，请刷新网盘后重试分享")

    async def create_share(
        self,
        entry: QuarkEntry,
        *,
        password: Optional[str] = None,
        expired_type: int = QUARK_SHARE_PERMANENT_EXPIRED_TYPE,
        poll_attempts: int = 8,
    ) -> Dict[str, Any]:
        if not entry.fid:
            raise ValueError("缺少夸克文件夹 ID")
        passcode = str(password or "").strip() or "".join(
            secrets.choice(string.ascii_lowercase + string.digits) for _ in range(4)
        )
        payload = await self._request(
            "POST",
            f"{self.API_BASE}/share",
            json_body={
                "fid_list": [entry.fid],
                "title": entry.name,
                "url_type": 2,
                "expired_type": int(expired_type),
                "passcode": passcode,
            },
        )
        data = payload.get("data") or {}
        task_id = str(data.get("task_id") or "").strip()
        share_id = str(data.get("share_id") or "").strip()
        if not task_id and not share_id:
            raise QuarkDriveError("创建夸克分享失败：未返回任务 ID")

        if not share_id:
            for attempt in range(max(1, int(poll_attempts))):
                if attempt:
                    await asyncio.sleep(min(0.5 + attempt * 0.25, 2.0))
                task_payload = await self._request(
                    "GET",
                    f"{self.API_BASE}/task",
                    params={"task_id": task_id, "retry_index": str(attempt)},
                )
                task_data = task_payload.get("data") or {}
                share_id = str(task_data.get("share_id") or "").strip()
                if share_id:
                    break
            if not share_id:
                raise QuarkDriveError("创建夸克分享超时，请稍后重试")

        detail_payload = await self._request(
            "POST",
            f"{self.API_BASE}/share/password",
            json_body={"share_id": share_id},
        )
        detail = detail_payload.get("data") or {}
        share_url = str(detail.get("share_url") or "").strip()
        final_passcode = str(detail.get("passcode") or passcode or "").strip()
        if not share_url:
            raise QuarkDriveError("夸克分享已创建，但未返回分享链接")
        return {
            "share_id": share_id,
            "share_url": share_url,
            "passcode": final_passcode,
            "expired_type": int(expired_type),
            "permanent": int(expired_type) == QUARK_SHARE_PERMANENT_EXPIRED_TYPE,
            "delivery_text": build_delivery_text(share_url, final_passcode),
        }


def build_delivery_text(share_url: str, passcode: str = "") -> str:
    lines = ["感谢购买，以下是夸克网盘交付信息：", str(share_url or "").strip()]
    if str(passcode or "").strip():
        lines.append(f"提取码：{str(passcode).strip()}")
    lines.append("该分享已设置为永久有效；如链接异常请联系卖家处理。")
    return "\n".join(line for line in lines if line)


def build_folder_fingerprint(entry: QuarkEntry, children: Iterable[QuarkEntry]) -> str:
    normalized_children = [
        {
            "fid": child.fid,
            "name": child.name,
            "dir": child.is_dir,
            "size": child.size,
            "updated_at": child.updated_at,
        }
        for child in children
    ]
    payload = {
        "folder": entry.to_dict(),
        "children": sorted(normalized_children, key=lambda item: (str(item["name"]), str(item["fid"]))),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_market_search_query(folder_name: str) -> str:
    """Turn a folder label into a conservative Xianyu search query."""
    query = re.sub(r"[\[\]【】()（）]+", " ", str(folder_name or ""))
    query = re.sub(r"\b(?:20\d{2}|v\d+(?:\.\d+)*)\b", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"\s+", " ", query).strip(" -_—|｜")
    return query[:40] or str(folder_name or "").strip()[:40]


def parse_market_price(value: Any) -> Optional[float]:
    text = str(value or "").replace(",", "").replace("￥", "¥").strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*(万)?", text)
    if not match:
        return None
    try:
        number = float(match.group(1))
    except (TypeError, ValueError):
        return None
    if match.group(2):
        number *= 10000
    # Digital-resource/referral listings commonly use genuine ¥0.01/¥0.10 prices.
    # Keep those values in the market sample instead of treating them as noise.
    if number < 0.01 or number > 1_000_000:
        return None
    return round(number, 2)


def _percentile(sorted_values: List[float], ratio: float) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * max(0.0, min(float(ratio), 1.0))
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return float(sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight)


def summarize_market_items(
    query: str,
    items: Iterable[Dict[str, Any]],
    *,
    sample_size: int = 12,
    pricing_strategy: str = "competitive",
) -> Dict[str, Any]:
    """Build a small, non-sensitive market brief from real Xianyu search results."""
    clean_items = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        raw_data = item.get("raw_data") or {}
        item_id = str(item.get("item_id") or "")
        if item_id.startswith("mock_") or (isinstance(raw_data, dict) and raw_data.get("mock")):
            continue
        title = str(item.get("title") or "").strip()
        if not title or title == "未知标题":
            continue
        clean_items.append({
            "item_id": item_id,
            "title": title[:100],
            "description": str(item.get("description") or "").strip()[:500],
            "price": parse_market_price(item.get("price")),
            "want_count": max(0, _safe_int(item.get("want_count"))),
            "publish_time": str(item.get("publish_time") or "")[:30],
        })

    clean_items.sort(key=lambda item: (item["want_count"], item["price"] is not None), reverse=True)
    samples = clean_items[:max(3, min(int(sample_size or 12), 30))]
    prices = sorted(float(item["price"]) for item in clean_items if item.get("price") is not None)
    filtered_prices = list(prices)
    if len(prices) >= 4:
        q1 = _percentile(prices, 0.25) or prices[0]
        q3 = _percentile(prices, 0.75) or prices[-1]
        raw_median = _percentile(prices, 0.5) or prices[len(prices) // 2]
        iqr = max(0.0, q3 - q1)
        lower = max(0.01, q1 - 1.5 * iqr, raw_median / 5)
        upper = min(q3 + 1.5 * iqr, raw_median * 5)
        filtered_prices = [price for price in prices if lower <= price <= upper] or prices

    price_min = min(filtered_prices) if filtered_prices else None
    price_max = max(filtered_prices) if filtered_prices else None
    price_median = _percentile(filtered_prices, 0.5)
    price_q1 = _percentile(filtered_prices, 0.25)
    price_q3 = _percentile(filtered_prices, 0.75)
    strategy = str(pricing_strategy or "competitive").strip().lower()
    multiplier = {"competitive": 0.95, "median": 1.0, "premium": 1.08}.get(strategy)
    recommended = (
        round(max(0.01, price_median * multiplier), 2)
        if price_median is not None and multiplier is not None
        else None
    )
    return {
        "query": str(query or "").strip(),
        "sample_count": len(samples),
        "price_count": len(filtered_prices),
        "price_min": price_min,
        "price_q1": round(price_q1, 2) if price_q1 is not None else None,
        "price_median": round(price_median, 2) if price_median is not None else None,
        "price_q3": round(price_q3, 2) if price_q3 is not None else None,
        "price_max": price_max,
        "recommended_price": recommended,
        "pricing_strategy": strategy,
        "ranking_basis": "want_count",
        "samples": samples,
    }


def choose_listing_price(
    ai_price: Any,
    market_summary: Optional[Dict[str, Any]],
    fallback_price: Any = None,
) -> Optional[float]:
    market_summary = market_summary or {}
    recommended = parse_market_price(market_summary.get("recommended_price"))
    candidate = parse_market_price(ai_price)
    q1 = parse_market_price(market_summary.get("price_q1"))
    q3 = parse_market_price(market_summary.get("price_q3"))
    if candidate is not None and q1 is not None and q3 is not None:
        lower = max(0.01, q1 * 0.75)
        upper = max(lower, q3 * 1.25)
        if lower <= candidate <= upper:
            return round(candidate, 2)
    if recommended is not None:
        return round(recommended, 2)
    return parse_market_price(fallback_price)


MOBILE_LISTING_COPY_GUIDE = (
    "文案必须适合手机端快速浏览，不能写成一整段说明书。description 使用纯文本分段，"
    "总长度建议 220-600 个中文字符，并严格按以下顺序组织：\n"
    "【先说重点】\n用 2-3 个短句制造代入感，说明它能帮买家解决什么问题；要有轻微故事感，"
    "但故事只能来自输入中的真实信息。只有输入明确给出真实购入金额时，才可以写‘当时花 XX 元购入’；"
    "划线原价、市场价或模型猜测都不能冒充真实购入价。没有金额依据时，可写‘之前花了不少时间整理，"
    "现在低价分享给真正需要的人’，但不能虚构经历。\n\n"
    "【你会得到】\n用 3-6 行‘• ’项目符号列出最有价值的内容，每行只表达一个卖点，不要把文件名全塞进一句话。\n\n"
    "【适合谁】\n用 2-4 行简短说明典型使用场景或人群，不承诺效果。\n\n"
    "【交付说明】\n说明商品形态、交付方式和购买前需要确认的事项；最后用一句自然、克制的行动提示收尾。\n\n"
    "各部分之间空一行；单段最多 3 句；不得使用虚假稀缺、虚构销量、保证效果、绝对化承诺或先低价后加价。"
)


def format_listing_description(value: Any) -> str:
    """Keep AI listing copy readable in a plain-text mobile product page."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    text = text.replace("**", "")
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "• ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    output_lines: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if output_lines and output_lines[-1] != "":
                output_lines.append("")
            continue
        if len(line) <= 96 or line.startswith(("【", "• ")):
            output_lines.append(line)
            continue

        sentences = [part.strip() for part in re.split(r"(?<=[。！？；])", line) if part.strip()]
        if len(sentences) <= 1:
            output_lines.append(line)
            continue
        paragraph = ""
        for sentence in sentences:
            if paragraph and len(paragraph) + len(sentence) > 88:
                output_lines.append(paragraph)
                output_lines.append("")
                paragraph = sentence
            else:
                paragraph += sentence
        if paragraph:
            output_lines.append(paragraph)

    return "\n".join(output_lines).strip()


class CatalogAIClient:
    def __init__(
        self,
        *,
        text_base_url: str,
        text_api_key: str,
        text_model: str,
        image_base_url: str,
        image_api_key: str,
        image_model: str,
        image_size: str = "1024x1024",
        timeout_seconds: float = 90.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.text_base_url = str(text_base_url or "").strip().rstrip("/")
        self.text_api_key = str(text_api_key or "").strip()
        self.text_model = str(text_model or "").strip()
        self.image_base_url = str(image_base_url or "").strip().rstrip("/")
        self.image_api_key = str(image_api_key or "").strip()
        self.image_model = str(image_model or "").strip()
        self.image_size = str(image_size or "1024x1024").strip()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> "CatalogAIClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _api_endpoint(base_url: str, endpoint: str) -> str:
        clean_base = str(base_url or "").strip().rstrip("/")
        clean_endpoint = endpoint.strip("/")
        if not clean_base:
            raise CatalogAIError("AI 接口地址不能为空")
        if clean_base.endswith(f"/{clean_endpoint}"):
            return clean_base
        # The settings page accepts either an API root (``.../v1``) or a full
        # image endpoint (``.../v1/images/generations``).  Reference-image
        # editing needs to switch the latter to ``images/edits`` instead of
        # accidentally appending a second endpoint path.
        if clean_endpoint.startswith("images/") and re.search(
            r"/images/(?:generations|edits)$", clean_base, flags=re.IGNORECASE
        ):
            return re.sub(
                r"/images/(?:generations|edits)$",
                f"/{clean_endpoint}",
                clean_base,
                flags=re.IGNORECASE,
            )
        return f"{clean_base}/{clean_endpoint}"

    @staticmethod
    def _auth_headers(api_key: str) -> Dict[str, str]:
        if not api_key:
            raise CatalogAIError("AI API Key 不能为空")
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.55,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Call the configured OpenAI-compatible text model for strict JSON.

        This is intentionally generic so higher-level workflows can share the
        user's existing encrypted text-model configuration without duplicating
        provider compatibility fallbacks.
        """
        if not self.text_model:
            raise CatalogAIError("文案模型不能为空")
        endpoint = self._api_endpoint(self.text_base_url, "chat/completions")
        headers = self._auth_headers(self.text_api_key)
        payload: Dict[str, Any] = {
            "model": self.text_model,
            "temperature": float(temperature),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": str(system_prompt or "")},
                {"role": "user", "content": str(user_prompt or "")},
            ],
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        response = await self._client.post(endpoint, headers=headers, json=payload)
        if response.status_code in {400, 422} and any(
            marker in response.text.lower()
            for marker in ("response_format", "temperature", "max_tokens", "unsupported parameter")
        ):
            payload.pop("response_format", None)
            payload.pop("temperature", None)
            payload.pop("max_tokens", None)
            response = await self._client.post(endpoint, headers=headers, json=payload)
        if response.is_error:
            raise CatalogAIError(f"文案生成失败: HTTP {response.status_code} {response.text[:500]}")
        try:
            api_payload = response.json()
            content = api_payload["choices"][0]["message"]["content"]
            return parse_json_object(content)
        except Exception as exc:
            raise CatalogAIError(f"文案模型返回格式异常: {response.text[:800]}") from exc

    async def generate_listing(
        self,
        folder: QuarkEntry,
        children: List[QuarkEntry],
        *,
        title_max_chars: int = 30,
        market_summary: Optional[Dict[str, Any]] = None,
        copy_background: str = "",
    ) -> Dict[str, Any]:
        if not self.text_model:
            raise CatalogAIError("文案模型不能为空")
        child_lines = []
        for child in children[:50]:
            kind = "文件夹" if child.is_dir else "文件"
            size_text = f"，{child.size} 字节" if child.size else ""
            child_lines.append(f"- {kind}：{child.name}{size_text}")
        child_summary = "\n".join(child_lines) if child_lines else "- 暂无可展示的子项信息"
        market_summary = market_summary or {}
        market_samples = market_summary.get("samples") or []
        market_lines = []
        for sample in market_samples[:20]:
            market_lines.append(
                f"- 标题：{sample.get('title') or '-'}；价格：{sample.get('price') or '-'}；"
                f"想要人数：{sample.get('want_count') or 0}；"
                f"简介：{sample.get('description') or '搜索页未提供'}"
            )
        market_text = "\n".join(market_lines) if market_lines else "- 未取得可靠的真实市场样本"
        traffic_mode = market_summary.get("pricing_strategy") == "traffic"
        traffic_price = parse_market_price(market_summary.get("fixed_price"))
        price_text = (
            f"有效价格数：{market_summary.get('price_count') or 0}；"
            f"最低/中位/最高：{market_summary.get('price_min')}/{market_summary.get('price_median')}/"
            f"{market_summary.get('price_max')}；策略参考价：{market_summary.get('recommended_price')}"
        )
        system_prompt = (
            "你是擅长闲鱼移动端阅读体验的商品文案编辑。仅依据提供的目录信息写作，不虚构内容、数量、"
            "授权、更新承诺或售后承诺。不得使用夸大、绝对化或误导性表述。只输出严格 JSON，"
            "字段为 title、description、cover_prompt、suggested_price、pricing_basis。市场样本只用于归纳"
            "常见卖点、表达方式和价格水平，禁止逐句复制或仿写某个卖家的文案，也不要声称样本销量最高。"
            "想要人数只代表公开热度信号，不等同成交量。description 要说明这是数字资料、通过网盘交付，"
            "标价必须是买家真实可成交的价格，不得用低价诱导后再临时加价。"
            "并提醒买家购买前确认需求。title 要像真实买家会搜索的标题：优先使用‘核心关键词 + 具体收益/"
            "使用场景 + 资料形式或数量’，自然顺口，避免冒号串词、同义词堆砌和空泛的‘超值必买’。"
            f"{MOBILE_LISTING_COPY_GUIDE}"
            "cover_prompt 用于生成无商标、无平台 Logo、无联系方式的方形封面。"
        )
        pricing_instruction = (
            f"当前采用拉新固定价模式，实际售价固定为 {traffic_price:.2f} 元；"
            "不要另行建议售价，suggested_price 返回该固定价格，并在 pricing_basis 中写明固定引流价。"
            if traffic_mode and traffic_price is not None
            else "建议售价必须参考有效市场区间；没有有效价格时返回 null。"
        )
        user_prompt = (
            f"目录名称：{folder.name}\n"
            f"目录包含项目数（平台字段）：{folder.include_items}\n"
            f"目录内可见项目：\n{child_summary}\n\n"
            f"卖家提供的真实文案背景：{str(copy_background or '').strip() or '未提供，不得自行编造购入经历或金额'}\n\n"
            f"闲鱼搜索词：{market_summary.get('query') or folder.name}\n"
            f"市场价格摘要：{price_text}\n"
            f"按想要人数排序的同类样本：\n{market_text}\n\n"
            f"请生成不超过 {max(12, int(title_max_chars))} 个字符的原创中文标题、适合手机阅读且带分段的中文介绍、"
            f"封面提示词和建议售价。{pricing_instruction}"
        )
        endpoint = self._api_endpoint(self.text_base_url, "chat/completions")
        headers = self._auth_headers(self.text_api_key)
        request_payload = {
            "model": self.text_model,
            "temperature": 0.45,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        response = await self._client.post(endpoint, headers=headers, json=request_payload)
        if response.status_code == 400 and any(
            marker in response.text.lower() for marker in ("response_format", "temperature", "unsupported")
        ):
            request_payload.pop("response_format", None)
            request_payload.pop("temperature", None)
            response = await self._client.post(endpoint, headers=headers, json=request_payload)
        if response.is_error:
            raise CatalogAIError(f"文案生成失败: HTTP {response.status_code} {response.text[:300]}")
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            result = parse_json_object(content)
        except Exception as exc:
            raise CatalogAIError(f"文案模型返回格式异常: {response.text[:500]}") from exc

        title = sanitize_title(result.get("title"), max_chars=title_max_chars)
        description = format_listing_description(result.get("description"))
        cover_prompt = str(result.get("cover_prompt") or "").strip()
        if not title or not description or not cover_prompt:
            raise CatalogAIError("文案模型缺少 title、description 或 cover_prompt")
        return {
            "title": title,
            "description": description,
            "cover_prompt": cover_prompt,
            "suggested_price": result.get("suggested_price"),
            "pricing_basis": str(result.get("pricing_basis") or "").strip(),
        }

    async def generate_product_draft(
        self,
        description: str,
        *,
        reference_images: Optional[List[str]] = None,
        category_hint: str = "",
        price_hint: Any = None,
        market_summary: Optional[Dict[str, Any]] = None,
        title_max_chars: int = 30,
    ) -> Dict[str, Any]:
        """Generate an editable listing from a free-form brief and optional images.

        OpenAI-compatible providers do not all accept multimodal message content.
        Reference images are therefore attempted first and the request is retried
        as text-only when the provider explicitly rejects that format.  The
        caller receives a warning instead of being told that unsupported images
        were analysed.
        """
        if not self.text_model:
            raise CatalogAIError("文案模型不能为空")
        brief = str(description or "").strip()
        if not brief:
            raise CatalogAIError("商品描述不能为空")

        safe_title_max = max(12, min(int(title_max_chars or 30), 60))
        image_urls = [str(item or "").strip() for item in (reference_images or []) if str(item or "").strip()]
        market_summary = market_summary or {}
        market_lines = []
        for sample in (market_summary.get("samples") or [])[:20]:
            market_lines.append(
                f"- 标题：{sample.get('title') or '-'}；价格：{sample.get('price') or '-'}；"
                f"想要人数：{sample.get('want_count') or 0}；"
                f"简介：{sample.get('description') or '搜索页未提供'}"
            )
        market_text = "\n".join(market_lines) if market_lines else "- 未取得可靠的真实市场样本"
        market_price_text = (
            f"有效价格数：{market_summary.get('price_count') or 0}；"
            f"最低/中位/最高：{market_summary.get('price_min')}/{market_summary.get('price_median')}/"
            f"{market_summary.get('price_max')}；策略参考价：{market_summary.get('recommended_price')}"
        )
        price_hint_text = "未指定" if price_hint in (None, "") else str(price_hint)

        system_prompt = (
            "你是二手交易平台的资深商品编辑和视觉策划。只依据用户描述、参考图和市场摘要写作，"
            "不得虚构品牌、型号、成色、数量、授权、功效、售后或发货承诺。参考同类商品只能归纳"
            "表达方式和价格水平，禁止复制具体卖家文案；想要人数只是热度信号，不代表成交量。"
            "标题要自然、准确、包含买家会搜索的核心词，采用‘核心关键词 + 具体收益/使用场景 + 形式或数量’"
            "的结构，不能堆砌或使用绝对化承诺。商品介绍应覆盖用户明确提供的卖点、规格/成色、交付方式"
            "和购买前确认事项。"
            f"{MOBILE_LISTING_COPY_GUIDE}"
            "cover_prompt 用于生成方形商品首图：主体明确、构图干净、有购买吸引力，但不得包含"
            "平台 Logo、商标仿冒、二维码、联系方式、水印或难以辨认的小字，优先使用无文字视觉。"
            "只输出严格 JSON，字段为 title、description、cover_prompt、suggested_price、"
            "pricing_basis、category。无法确认的信息不要补写。"
        )
        user_prompt = (
            f"用户的上架需求：\n{brief}\n\n"
            f"类目提示：{str(category_hint or '').strip() or '未指定'}\n"
            f"用户价格提示：{price_hint_text}\n"
            f"同款搜索词：{market_summary.get('query') or '未调研'}\n"
            f"市场价格摘要：{market_price_text}\n"
            f"按公开想要人数排序的同类样本：\n{market_text}\n\n"
            f"请生成不超过 {safe_title_max} 个字符的原创中文标题、适合手机阅读且带分段的详细介绍、"
            "方形封面生成提示词、建议售价、定价依据和简短类目词。若用户指定价格，"
            "suggested_price 应优先返回该价格。"
        )

        text_user_message: Dict[str, Any] = {"role": "user", "content": user_prompt}
        multimodal_user_message: Dict[str, Any] = text_user_message
        if image_urls:
            content_parts: List[Dict[str, Any]] = [{"type": "text", "text": user_prompt}]
            content_parts.extend({
                "type": "image_url",
                "image_url": {"url": image_url, "detail": "low"},
            } for image_url in image_urls)
            multimodal_user_message = {"role": "user", "content": content_parts}

        endpoint = self._api_endpoint(self.text_base_url, "chat/completions")
        headers = self._auth_headers(self.text_api_key)

        async def post_messages(user_message: Dict[str, Any]) -> httpx.Response:
            request_payload = {
                "model": self.text_model,
                "temperature": 0.45,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    user_message,
                ],
            }
            response = await self._client.post(endpoint, headers=headers, json=request_payload)
            if response.status_code in {400, 422} and any(
                marker in response.text.lower()
                for marker in ("response_format", "temperature", "unsupported parameter")
            ):
                request_payload.pop("response_format", None)
                request_payload.pop("temperature", None)
                response = await self._client.post(endpoint, headers=headers, json=request_payload)
            return response

        warnings: List[str] = []
        response = await post_messages(multimodal_user_message)
        reference_images_analyzed = bool(image_urls)
        if response.is_error and image_urls and response.status_code in {400, 415, 422}:
            warnings.append(
                "当前文案模型不支持参考图识别，已按文字需求生成；参考图仍会作为商品附图保留。"
            )
            reference_images_analyzed = False
            response = await post_messages(text_user_message)
        if response.is_error:
            raise CatalogAIError(f"文案生成失败: HTTP {response.status_code} {response.text[:300]}")

        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            result = parse_json_object(content)
        except Exception as exc:
            raise CatalogAIError(f"文案模型返回格式异常: {response.text[:500]}") from exc

        title = sanitize_title(result.get("title"), max_chars=safe_title_max)
        listing_description = format_listing_description(result.get("description"))
        cover_prompt = str(result.get("cover_prompt") or "").strip()
        if not title or not listing_description or not cover_prompt:
            raise CatalogAIError("文案模型缺少 title、description 或 cover_prompt")
        return {
            "title": title,
            "description": listing_description,
            "cover_prompt": cover_prompt,
            "suggested_price": result.get("suggested_price"),
            "pricing_basis": str(result.get("pricing_basis") or "").strip(),
            "category": str(result.get("category") or category_hint or "").strip(),
            "reference_images_analyzed": reference_images_analyzed,
            "warnings": warnings,
        }

    async def _parse_image_response(self, response: httpx.Response) -> Dict[str, Any]:
        try:
            payload = response.json()
            image_data = (payload.get("data") or [])[0]
        except Exception as exc:
            raise CatalogAIError(f"生图接口返回格式异常: {response.text[:500]}") from exc

        raw_bytes: Optional[bytes] = None
        b64_value = image_data.get("b64_json") if isinstance(image_data, dict) else None
        image_url = image_data.get("url") if isinstance(image_data, dict) else None
        if b64_value:
            try:
                raw_bytes = base64.b64decode(str(b64_value), validate=False)
            except Exception as exc:
                raise CatalogAIError("生图接口返回了无效的 Base64 图片") from exc
        elif image_url:
            image_response = await self._client.get(str(image_url))
            if image_response.is_error:
                raise CatalogAIError(f"下载生成封面失败: HTTP {image_response.status_code}")
            if len(image_response.content) > 12 * 1024 * 1024:
                raise CatalogAIError("生成封面超过 12MB，已拒绝保存")
            raw_bytes = image_response.content
        if not raw_bytes:
            raise CatalogAIError("生图接口未返回图片内容")

        jpeg_bytes, width, height = normalize_cover_image(raw_bytes)
        return {
            "content": base64.b64encode(jpeg_bytes).decode("ascii"),
            "filename": "quark_catalog_cover.jpg",
            "width": width,
            "height": height,
            "sha256": hashlib.sha256(jpeg_bytes).hexdigest(),
        }

    async def generate_cover(self, prompt: str) -> Dict[str, Any]:
        if not self.image_model:
            raise CatalogAIError("生图模型不能为空")
        endpoint = self._api_endpoint(self.image_base_url, "images/generations")
        headers = self._auth_headers(self.image_api_key)
        request_payload = {
            "model": self.image_model,
            "prompt": str(prompt or "").strip(),
            "size": self.image_size,
            "n": 1,
            "response_format": "b64_json",
        }
        response = await self._client.post(endpoint, headers=headers, json=request_payload)
        if response.status_code == 400 and "response_format" in response.text.lower():
            request_payload.pop("response_format", None)
            response = await self._client.post(endpoint, headers=headers, json=request_payload)
        if response.is_error:
            raise CatalogAIError(f"封面生成失败: HTTP {response.status_code} {response.text[:300]}")
        return await self._parse_image_response(response)

    async def generate_with_references(
        self,
        prompt: str,
        reference_paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Generate an image while preferring real visual references.

        OpenAI-compatible providers vary in how they expose multipart image
        edits.  Try the common multi-image and single-image forms, then keep the
        workflow productive by falling back to the visual-fingerprint prompt.
        """
        candidates: List[Path] = []
        for raw_path in reference_paths or []:
            path = Path(str(raw_path or ""))
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                candidates.append(path)
            if len(candidates) >= 4:
                break
        if not candidates:
            result = await self.generate_cover(prompt)
            result["reference_mode"] = "prompt_only"
            result["reference_count"] = 0
            return result

        if not self.image_model:
            raise CatalogAIError("生图模型不能为空")
        endpoint = self._api_endpoint(self.image_base_url, "images/edits")
        headers = self._auth_headers(self.image_api_key)
        headers.pop("Content-Type", None)
        attempts = [
            (
                [
                    (
                        "image[]",
                        (path.name, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/jpeg"),
                    )
                    for path in candidates
                ],
                {
                    "model": self.image_model, "prompt": str(prompt or "").strip(),
                    "size": self.image_size, "n": "1", "response_format": "b64_json",
                    "input_fidelity": "high",
                },
            ),
            (
                [(
                    "image",
                    (
                        candidates[0].name,
                        candidates[0].read_bytes(),
                        mimetypes.guess_type(candidates[0].name)[0] or "image/jpeg",
                    ),
                )],
                {
                    "model": self.image_model, "prompt": str(prompt or "").strip(),
                    "size": self.image_size, "n": "1",
                },
            ),
        ]
        edit_errors: List[str] = []
        for files, data in attempts:
            try:
                response = await self._client.post(endpoint, headers=headers, data=data, files=files)
                if response.is_error:
                    edit_errors.append(f"HTTP {response.status_code} {response.text[:180]}")
                    continue
                result = await self._parse_image_response(response)
                result["reference_mode"] = "image_edit"
                result["reference_count"] = len(candidates)
                return result
            except (httpx.HTTPError, CatalogAIError, OSError) as exc:
                edit_errors.append(str(exc)[:180])

        result = await self.generate_cover(prompt)
        result["reference_mode"] = "prompt_fallback"
        result["reference_count"] = len(candidates)
        result["reference_warning"] = (
            "参考图编辑接口未被当前生图服务接受，已使用同一视觉指纹提示词生成"
            + (f"（{edit_errors[-1]}）" if edit_errors else "")
        )
        return result


def parse_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("JSON 根节点必须是对象")
    return payload


def sanitize_title(value: Any, *, max_chars: int = 30) -> str:
    title = re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()
    title = re.sub(r"\s{2,}", " ", title)
    title = title.strip("-—_|｜:：,，。!！")
    safe_max = max(12, min(int(max_chars or 30), 60))
    return title[:safe_max].strip()


def normalize_cover_image(image_bytes: bytes) -> tuple[bytes, int, int]:
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except Exception as exc:
        raise CatalogAIError("生图接口返回的内容不是有效图片") from exc
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    if max(image.size) > 2048:
        image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, "JPEG", quality=90, optimize=True)
    return output.getvalue(), int(image.width), int(image.height)
