"""Link-to-product helpers.

This module deliberately keeps the source snapshot separate from the listing
copy.  A public article URL can be fetched when the page exposes readable HTML;
dynamic pages (including many X pages) can be completed by pasting the text in
the UI.  The generated package contains attribution metadata and a review flag
so the seller can edit the result before publishing.
"""

from __future__ import annotations

import html
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import httpx


class ContentProductError(RuntimeError):
    """Readable errors for source extraction and package generation."""


@dataclass
class ContentSource:
    url: str
    title: str
    text: str
    excerpt: str
    fetched: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "excerpt": self.excerpt,
            "fetched": self.fetched,
        }


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_html(value: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", value)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|article|section|h[1-6]|li|blockquote|tr)>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    return _clean_text(text)


def _meta(html_text: str, key: str) -> str:
    pattern = rf"<meta[^>]+(?:property|name)=[\"']{re.escape(key)}[\"'][^>]+content=[\"']([^\"']+)[\"']"
    match = re.search(pattern, html_text, flags=re.IGNORECASE)
    if match:
        return _clean_text(match.group(1))
    pattern = rf"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']{re.escape(key)}[\"']"
    match = re.search(pattern, html_text, flags=re.IGNORECASE)
    return _clean_text(match.group(1)) if match else ""


def _title(html_text: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html_text)
    return _clean_text(match.group(1)) if match else ""


def validate_source_url(url: str) -> str:
    clean = str(url or "").strip()
    parsed = urlparse(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ContentProductError("来源链接必须是 http:// 或 https:// 开头的完整网址")
    return clean[:2000]


async def fetch_content_source(url: str, *, timeout_seconds: float = 25.0) -> ContentSource:
    """Fetch a readable public page and extract a conservative text snapshot."""
    clean_url = validate_source_url(url)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds), follow_redirects=True, headers=headers) as client:
            response = await client.get(clean_url)
    except httpx.HTTPError as exc:
        raise ContentProductError(f"来源页面抓取失败：{exc}") from exc
    if response.status_code >= 400:
        raise ContentProductError(f"来源页面返回 HTTP {response.status_code}")
    raw = response.text or ""
    title = _meta(raw, "og:title") or _title(raw) or clean_url
    description = _meta(raw, "og:description") or _meta(raw, "description")
    candidates: List[str] = []
    for tag in ("article", "main"):
        matches = re.findall(rf"(?is)<{tag}[^>]*>(.*?)</{tag}>", raw)
        candidates.extend(_strip_html(item) for item in matches)
    candidates.append(_strip_html(raw))
    candidates = [item for item in candidates if len(item) >= 80]
    text = max(candidates, key=len) if candidates else description
    text = _clean_text(text)
    if len(text) < 80:
        raise ContentProductError(
            "页面是动态渲染或正文未公开，抓到的内容太少；请把文章正文粘贴到‘正文补充’后重试"
        )
    return ContentSource(
        url=clean_url,
        title=title[:300],
        text=text[:50000],
        excerpt=text[:500],
        fetched=True,
    )


def merge_source(url: str, pasted_text: str, fetched: Optional[ContentSource]) -> ContentSource:
    """Merge a fetched snapshot and user-pasted text, preferring the latter."""
    clean_url = str(url or (fetched.url if fetched else "")).strip()
    text = _clean_text(pasted_text)
    if text:
        title = (fetched.title if fetched else "") or text.splitlines()[0][:120]
        return ContentSource(clean_url, title, text[:50000], text[:500], bool(fetched))
    if fetched:
        return fetched
    raise ContentProductError("请提供来源链接，或粘贴至少 80 个字的正文")


def _safe_skill_name(title: str) -> str:
    # Skill metadata requires lowercase ASCII hyphen-case.  Keep any ASCII
    # keywords from the title and use a stable generic suffix for Chinese-only
    # titles instead of emitting an invalid directory name.
    parts = re.findall(r"[a-zA-Z0-9]+", str(title or ""))
    value = "-".join(parts).lower()
    if not value:
        value = "content-product"
    if value == "ai":
        value = "ai-content-product"
    return value[:70].strip("-") or "content-product"


def build_product_document(
    source: ContentSource,
    listing: Dict[str, Any],
    *,
    rights_status: str,
    output_mode: str,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rights_text = {
        "self_owned": "卖家自有内容",
        "licensed": "已取得分发许可",
        "public_domain": "公开领域/允许再分发",
        "reference_only": "仅作内部参考，发布前需要人工确认来源与授权",
    }.get(rights_status, "待确认")
    return (
        f"# {listing.get('title') or source.title}\n\n"
        f"> 生成时间：{now}  ·  内容来源：[{source.title}]({source.url or '#'})\n"
        f"> 来源状态：{rights_text}\n\n"
        f"## 这份内容适合谁\n\n{listing.get('description') or '请在发布前补充适用人群和交付说明。'}\n\n"
        "## 使用说明\n\n"
        "1. 先阅读目录和交付说明，确认内容与自己的需求匹配。\n"
        "2. 内容来自来源页面的整理与结构化，不替代专业意见。\n"
        "3. 请保留来源信息，并按照你拥有的权利范围使用和分发。\n\n"
        "## 原文快照（用于检索与复核）\n\n"
        f"来源链接：{source.url or '未提供'}\n\n"
        f"{source.text}\n"
    )


def build_skill_files(source: ContentSource, listing: Dict[str, Any], *, rights_status: str) -> Dict[str, str]:
    skill_name = _safe_skill_name(listing.get("title") or source.title)
    description = str(listing.get("description") or "").replace("\n", " ")[:180]
    skill_md = f"""---
name: {skill_name}
description: {description or '将来源内容整理为结构化交付稿，并根据用户要求生成可读的商品化内容。'}
---

# {listing.get('title') or source.title}

将用户提供的长文或资料整理为清晰、可复用的交付稿。先确认用户的目标读者、输出格式和事实边界，再按以下顺序处理：

1. 提取主题、关键事实、章节结构和可执行步骤。
2. 用简洁标题、分段和项目符号重排内容，保留原意，不补造来源没有的事实。
3. 输出一份适合阅读的 Markdown 文档，并给出来源链接和归属说明。
4. 如果用户要求商品文案，分别给出标题、卖点、适用人群、交付方式和购买前确认事项。
5. 对无法核实的授权、收益、销量或效果使用“待确认”标记。

## 来源

- URL：{source.url or '未提供'}
- 来源状态：{rights_status}
- 本 Skill 随包附带 `references/source.md` 作为复核材料。
"""
    return {
        "SKILL.md": skill_md,
        "agents/openai.yaml": """interface:
  display_name: "内容整理商品化"
  short_description: "把长文整理成可阅读、可交付的内容包"
  default_prompt: "请根据来源资料整理出结构化交付稿，并保留来源与事实边界。"
""",
        "references/source.md": build_product_document(source, listing, rights_status=rights_status, output_mode="skill"),
    }


def write_product_package(
    base_dir: Path,
    product_id: str,
    source: ContentSource,
    listing: Dict[str, Any],
    *,
    rights_status: str,
    output_mode: str,
) -> Dict[str, str]:
    """Write editable exports and return paths relative to the data directory."""
    product_dir = Path(base_dir) / str(product_id)
    product_dir.mkdir(parents=True, exist_ok=True)
    document = build_product_document(source, listing, rights_status=rights_status, output_mode=output_mode)
    (product_dir / "product.md").write_text(document, encoding="utf-8")
    (product_dir / "source.txt").write_text(source.text, encoding="utf-8")
    (product_dir / "metadata.json").write_text(
        json.dumps({"source": source.to_dict(), "listing": listing, "rights_status": rights_status}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    skill_files = build_skill_files(source, listing, rights_status=rights_status) if output_mode in {"skill", "bundle"} else {}
    for relative, content in skill_files.items():
        path = product_dir / "skill" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    zip_path = product_dir.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in product_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(product_dir).as_posix())
    return {
        "directory": str(product_dir),
        "zip": str(zip_path),
        "markdown": str(product_dir / "product.md"),
        "skill": str(product_dir / "skill") if skill_files else "",
    }
