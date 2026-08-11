"""Link-to-product helpers.

This module deliberately keeps the source snapshot separate from the listing
copy.  A public article URL can be fetched when the page exposes readable HTML;
dynamic pages (including many X pages) can be completed by pasting the text in
the UI.  The generated package contains attribution metadata and a review flag
so the seller can edit the result before publishing.
"""

from __future__ import annotations

import html
import base64
import hashlib
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
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
    blocks: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "excerpt": self.excerpt,
            "fetched": self.fetched,
            "blocks": self.blocks,
        }


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_SOURCE_UI_NOISE = {
    "想发表自己的文章吗？",
    "升级至高级版",
    "升级至 Premium",
    "Want to publish your own Article?",
    "Upgrade to Premium",
    "Show more",
    "显示此线程",
    "查看帖子参与量",
}


def _clean_source_lines(value: Any) -> List[str]:
    """Remove X chrome/UI residue while retaining the author's line order."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[\u200b-\u200d\ufeff]", "", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    # X long-form pages expose view/reply/repost/like counts immediately below
    # the article strapline.  They are page chrome, not article prose.
    if lines:
        index = 1
        metric_count = 0
        while index < min(len(lines), 7) and re.fullmatch(r"[\d,.]+(?:[KMW万]?)", lines[index], re.I):
            metric_count += 1
            index += 1
        if metric_count >= 2:
            del lines[1:index]
    return [line for line in lines if line and line not in _SOURCE_UI_NOISE]


def _guess_heading(line: str) -> int:
    match = re.match(r"^(#{1,6})\s+", line)
    if match:
        return min(3, len(match.group(1)))
    clean = line.strip()
    if re.match(r"^\d{1,2}[｜|]\s*\S+", clean):
        return 2
    if clean in {"写在最后", "最后的话", "结语", "总结"}:
        return 2
    if re.fullmatch(r"【[^】]{2,28}】", clean):
        return 3
    return 0


def infer_source_blocks(text: str) -> List[Dict[str, Any]]:
    """Build a readable legacy block stream when an old capture only has text."""
    blocks: List[Dict[str, Any]] = []
    in_code = False
    code_lines: List[str] = []
    for line in _clean_source_lines(text):
        if line.startswith("```"):
            if in_code:
                blocks.append({"type": "code", "text": "\n".join(code_lines).strip()})
                code_lines = []
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(line)
            continue
        level = _guess_heading(line)
        if level:
            blocks.append({"type": "heading", "level": level, "text": re.sub(r"^#{1,6}\s+", "", line)})
        elif re.match(r"^(?:[-*•]|\d+[.)、])\s*", line):
            ordered = bool(re.match(r"^\d+[.)、]", line))
            blocks.append({
                "type": "list_item",
                "ordered": ordered,
                "text": re.sub(r"^(?:[-*•]|\d+[.)、])\s*", "", line),
            })
        elif line.startswith(">"):
            blocks.append({"type": "quote", "text": line.lstrip("> ")})
        else:
            blocks.append({"type": "paragraph", "text": line})
    if code_lines:
        blocks.append({"type": "code", "text": "\n".join(code_lines).strip()})
    return blocks


def normalize_source_blocks(source: ContentSource) -> List[Dict[str, Any]]:
    """Return a complete, ordered block stream for the source snapshot.

    Browser captures can contain a mixture of rich blocks and a separate plain
    text snapshot.  Some X layouts expose only headings/images through the rich
    DOM while ``innerText`` still contains the complete article.  Previously we
    treated any non-empty rich block list as authoritative, which silently
    discarded the missing paragraphs during PDF/DOCX export.  Keep rich blocks
    when they cover the snapshot, and fall back to the complete text when they
    are only a sparse outline.  Image anchors are retained and reinserted into
    the fallback stream at their relative positions.
    """
    allowed = {"heading", "paragraph", "list_item", "quote", "code", "image", "divider"}
    normalized: List[Dict[str, Any]] = []
    for raw in source.blocks or []:
        if not isinstance(raw, dict):
            continue
        block_type = str(raw.get("type") or "paragraph").strip().lower()
        if block_type not in allowed:
            continue
        if block_type == "image":
            source_url = str(raw.get("source_url") or raw.get("url") or "").strip()
            image_index = raw.get("image_index")
            if source_url or image_index is not None:
                normalized.append({
                    "type": "image",
                    "source_url": source_url,
                    "image_index": image_index,
                    "alt": _clean_text(raw.get("alt") or "原文配图")[:240],
                })
            continue
        if block_type == "divider":
            normalized.append({"type": "divider"})
            continue
        text = str(raw.get("markdown") or raw.get("text") or "").strip()
        if not text or text in _SOURCE_UI_NOISE:
            continue
        text = re.sub(r"[\u200b-\u200d\ufeff]", "", text)
        item = {"type": block_type, "text": text[:12000]}
        if block_type == "heading":
            item["level"] = max(1, min(int(raw.get("level") or 2), 3))
        if block_type == "list_item":
            item["ordered"] = bool(raw.get("ordered"))
        normalized.append(item)
    fallback = infer_source_blocks(source.text)
    if not normalized:
        return fallback

    rich_text = "\n".join(
        str(item.get("text") or "")
        for item in normalized
        if item.get("type") not in {"image", "divider"}
    ).strip()
    source_text = "\n".join(_clean_source_lines(source.text)).strip()
    # A rich capture is considered complete when it retains most of the text
    # snapshot.  The absolute floor avoids treating a tiny source as sparse.
    # Use the fallback for old/partial captures and keep their image metadata.
    source_length = len(source_text)
    rich_length = len(rich_text)
    coverage_floor = max(80, int(source_length * 0.72))
    if source_length > 240 and rich_length < coverage_floor:
        image_blocks = [item for item in normalized if item.get("type") == "image"]
        if not image_blocks:
            return fallback

        rich_text_blocks = [
            item for item in normalized if item.get("type") not in {"image", "divider"}
        ]
        fallback_with_images = list(fallback)
        # Preserve image order and use the number of rich text blocks before an
        # image as an approximate anchor into the complete text stream.  When
        # the rich DOM has no text anchors, distribute images through the full
        # article instead of creating a detached gallery at the end.
        seen_text_before = 0
        insertion_plan: List[Tuple[int, Dict[str, Any]]] = []
        for item in normalized:
            if item.get("type") == "image":
                if rich_text_blocks:
                    ratio = seen_text_before / max(1, len(rich_text_blocks))
                    position = round(ratio * len(fallback_with_images))
                else:
                    position = round(
                        (len(insertion_plan) + 1)
                        * len(fallback_with_images)
                        / (len(image_blocks) + 1)
                    )
                insertion_plan.append((max(0, min(position, len(fallback_with_images))), item))
            elif item.get("type") != "divider":
                seen_text_before += 1
        offset = 0
        for position, item in insertion_plan:
            fallback_with_images.insert(position + offset, item)
            offset += 1
        return fallback_with_images
    return normalized


def _description_markdown(value: Any) -> str:
    lines: List[str] = []
    for raw in _clean_source_lines(value):
        if re.fullmatch(r"【[^】]{2,28}】", raw):
            lines.extend([f"### {raw.strip('【】')}", ""])
        elif re.match(r"^[•*-]\s*", raw):
            clean_item = re.sub(r"^[•*-]\s*", "", raw)
            lines.append(f"- {clean_item}")
        else:
            lines.append(raw)
    return "\n\n".join(line for line in lines if line).strip()


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
        digest = hashlib.sha1(str(title or 'content-product').encode('utf-8')).hexdigest()[:8]
        value = f"content-product-{digest}"
    if value == "ai":
        value = "ai-content-product"
    return value[:70].strip("-") or "content-product"


def build_product_document(
    source: ContentSource,
    listing: Dict[str, Any],
    *,
    rights_status: str,
    output_mode: str,
    image_assets: Optional[List[Dict[str, Any]]] = None,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rights_text = {
        "self_owned": "卖家自有内容",
        "licensed": "已取得分发许可",
        "public_domain": "公开领域/允许再分发",
        "reference_only": "仅作内部参考，发布前需要人工确认来源与授权",
    }.get(rights_status, "待确认")
    assets = list(image_assets or [])
    cover_asset = next((item for item in assets if item.get("role") == "cover"), None)
    source_assets = [item for item in assets if item.get("role") != "cover"]

    blocks = normalize_source_blocks(source)
    image_by_url = {
        str(item.get("source_url") or "").split("?", 1)[0]: item
        for item in source_assets if item.get("source_url")
    }
    consumed: set[str] = set()
    source_lines: List[str] = []
    for block in blocks:
        block_type = block.get("type")
        text = str(block.get("text") or "").strip()
        if block_type == "image":
            source_url = str(block.get("source_url") or "").split("?", 1)[0]
            asset = image_by_url.get(source_url)
            if asset is None and block.get("image_index") is not None:
                try:
                    asset = source_assets[int(block["image_index"])]
                except (IndexError, TypeError, ValueError):
                    asset = None
            if asset:
                relative = str(asset.get("relative_path") or "")
                if relative and relative not in consumed:
                    consumed.add(relative)
                    source_lines.extend([f"![{block.get('alt') or asset.get('alt') or '原文配图'}]({relative})", ""])
            continue
        if block_type == "heading":
            source_lines.extend([f"{'#' * max(2, min(int(block.get('level') or 2) + 1, 4))} {text}", ""])
        elif block_type == "list_item":
            source_lines.append(f"{'1.' if block.get('ordered') else '-'} {text}")
        elif block_type == "quote":
            source_lines.extend([f"> {text}", ""])
        elif block_type == "code":
            source_lines.extend(["```text", text, "```", ""])
        elif block_type == "divider":
            source_lines.extend(["---", ""])
        elif text:
            source_lines.extend([text, ""])

    # Legacy captures have no image anchors.  Place source images at natural
    # section boundaries rather than appending a detached gallery.
    remaining = [item for item in source_assets if item.get("relative_path") not in consumed]
    if remaining:
        paragraph_slots = [
            index for index, line in enumerate(source_lines)
            if line and not line.startswith(("#", ">", "- ", "1.", "```", "![", "---"))
        ]
        for image_index, asset in enumerate(remaining, start=1):
            relative = str(asset.get("relative_path") or "")
            if not relative:
                continue
            if paragraph_slots:
                slot_position = min(
                    len(paragraph_slots) - 1,
                    max(0, round(image_index * len(paragraph_slots) / (len(remaining) + 1)) - 1),
                )
                insert_at = paragraph_slots[slot_position] + 2 * (image_index - 1) + 1
            else:
                insert_at = len(source_lines)
            source_lines[insert_at:insert_at] = [
                "",
                f"![{asset.get('alt') or f'原文配图 {image_index}'}]({relative})",
                "",
            ]

    parts = [
        f"# {listing.get('title') or source.title}",
        "",
        f"> 生成时间：{now}  ·  内容来源：[{source.title}]({source.url or '#'})",
        f"> 来源状态：{rights_text}",
        "",
    ]
    if cover_asset and cover_asset.get("relative_path"):
        parts.extend([f"![商品封面]({cover_asset['relative_path']})", ""])
    parts.extend([
        "## 内容简介",
        "",
        _description_markdown(listing.get("description") or "请在发布前补充适用人群和交付说明。"),
        "",
        "## 阅读与使用说明",
        "",
        "1. 先阅读目录和交付说明，确认内容与自己的需求匹配。",
        "2. 正文按原文顺序整理，标题、强调、列表和配图位置尽量保持一致。",
        "3. 请保留来源信息，并按照你拥有的权利范围使用和分发。",
        "",
        "## 正文",
        "",
        f"来源链接：[{source.url or '未提供'}]({source.url or '#'})",
        "",
        *source_lines,
    ])
    return "\n".join(parts).strip() + "\n"


def _topic_blueprint(source: ContentSource, listing: Dict[str, Any]) -> Dict[str, Any]:
    """Derive a topic-specific workflow and reusable assets from the source."""
    corpus = f"{source.title}\n{listing.get('title') or ''}\n{listing.get('description') or ''}\n{source.text[:12000]}"
    headings = [
        re.sub(r"^[#\d.、（）()\s-]+", "", line).strip()[:80]
        for line in source.text.splitlines()
        if 3 <= len(line.strip()) <= 80 and (
            line.lstrip().startswith('#') or re.match(r"^\s*(?:\d+[.、]|[一二三四五六七八九十]+[、.])", line)
        )
    ]
    headings = list(dict.fromkeys(item for item in headings if item))[:10]
    topic_rules = [
        (("ai", "人工智能", "提示词", "模型", "自动化", "agent", "skill"), "AI 实战", [
            "先提取工具、输入、提示词、参数和预期输出，形成可复现清单",
            "按准备、执行、校验、迭代四阶段重组方法，不把案例结果写成保证",
            "为每个关键步骤补齐可复制的输入模板和失败排查项",
            "用一个最小示例验证工作流，并记录适用边界",
        ], "AI 实战执行记录"),
        (("运营", "小红书", "抖音", "内容", "流量", "营销", "变现"), "内容运营", [
            "提取受众、痛点、内容钩子、渠道动作和转化路径",
            "把原文案例拆成可验证的选题与发布实验，不照搬收益表述",
            "生成选题表、内容脚本和复盘指标三套可填写模板",
            "按一轮发布周期执行并沉淀下一轮优化假设",
        ], "内容运营实验单"),
        (("教程", "课程", "学习", "读书", "知识", "训练", "入门"), "学习训练", [
            "识别学习目标、前置知识、关键概念和练习任务",
            "将正文重排为循序渐进的学习路径与阶段检查点",
            "为每个阶段生成笔记问题、练习题和自测标准",
            "完成复盘并输出一页可执行学习计划",
        ], "主题学习计划"),
        (("代码", "开发", "编程", "python", "javascript", "github", "项目"), "项目实践", [
            "提取环境、依赖、目录、命令和输入输出约束",
            "按最小可运行版本、扩展功能、测试验证拆分实现步骤",
            "生成实施清单、验收清单和常见故障定位表",
            "保留来源中的版本信息并标注需要实际验证的步骤",
        ], "项目实施记录"),
    ]
    lowered = corpus.lower()
    theme = "知识整理"
    workflow = [
        "提取文章主张、证据、步骤、案例和适用边界",
        "按目标读者重新组织章节，保留来源并移除重复信息",
        "把可执行内容转成检查清单、填写模板和复盘问题",
        "使用来源核对表检查事实、链接、数字和待确认内容",
    ]
    template_name = "主题行动清单"
    for keywords, candidate_theme, candidate_workflow, candidate_template in topic_rules:
        if any(keyword in lowered for keyword in keywords):
            theme, workflow, template_name = candidate_theme, candidate_workflow, candidate_template
            break
    key_points = headings or [
        line.strip()[:100] for line in source.text.splitlines()
        if 12 <= len(line.strip()) <= 100
    ][:8]
    return {
        "theme": theme,
        "workflow": workflow,
        "template_name": template_name,
        "key_points": key_points,
        "display_name": str(listing.get('title') or source.title or theme)[:64],
    }


def build_skill_files(source: ContentSource, listing: Dict[str, Any], *, rights_status: str) -> Dict[str, str]:
    skill_name = _safe_skill_name(listing.get("title") or source.title)
    blueprint = _topic_blueprint(source, listing)
    trigger_terms = "、".join(blueprint["key_points"][:4]) or blueprint["theme"]
    description = (
        f"围绕“{blueprint['display_name']}”执行{blueprint['theme']}工作流，生成专属行动清单、"
        f"交付模板与来源核对材料。适用于用户提到 {trigger_terms}，或要求把相关长文转成可执行成果时。"
    )[:500]
    workflow_text = "\n".join(f"{index}. {step}" for index, step in enumerate(blueprint["workflow"], start=1))
    key_point_text = "\n".join(f"- {item}" for item in blueprint["key_points"]) or "- 以 references/source.md 为准"
    skill_md = f"""---
name: {skill_name}
description: {description}
---

# {listing.get('title') or source.title}

使用本 Skill 将“{blueprint['theme']}”主题材料转成可执行交付物。开始前读取 `references/topic-guide.md` 和 `references/source.md`，确认用户的目标、已有条件与期望格式。

## 专属工作流

{workflow_text}

## 输出要求

- 使用 `assets/{skill_name}-template.md` 作为最终交付结构，并根据用户场景删减空章节。
- 对数字、案例结果、工具版本和外部链接逐项使用 `references/source-checklist.md` 复核。
- 把来源未覆盖的推断明确标为“待验证”，不把个案表现改写为普遍结果。
- 用户需要商品文案时，另附标题、核心卖点、适用人群、交付方式和购买前确认项。

## 来源

- URL：{source.url or '未提供'}
- 来源状态：{rights_status}
- 本 Skill 随包附带 `references/source.md` 作为复核材料。
"""
    topic_guide = f"""# {blueprint['theme']}主题指南

## 文章中的关键节点

{key_point_text}

## 执行顺序

{workflow_text}

## 使用提示

优先把用户的真实目标代入模板，再从来源快照提取对应证据。来源中没有的数据保持为空并标记待验证。
"""
    deliverable_template = f"""# {blueprint['template_name']}

## 我的目标

- 目标结果：
- 目标读者/用户：
- 截止时间：
- 已有条件：

## 主题要点

{key_point_text}

## 分步执行

{''.join(f'### 第 {index} 步\n\n{step}\n\n- 输入：\n- 动作：\n- 输出：\n- 验收：\n\n' for index, step in enumerate(blueprint['workflow'], start=1))}
## 复盘

- 有效的做法：
- 遇到的问题：
- 下一轮调整：
- 待核实信息：
"""
    return {
        "SKILL.md": skill_md,
        "agents/openai.yaml": (
            "interface:\n"
            f"  display_name: \"{blueprint['display_name'].replace(chr(34), '')[:32]}\"\n"
            f"  short_description: \"围绕{blueprint['theme']}生成专属流程、模板和交付稿\"\n"
            f"  default_prompt: \"请使用 ${skill_name} 把我的材料整理为可执行的{blueprint['template_name']}。\"\n"
        ),
        "references/source.md": build_product_document(source, listing, rights_status=rights_status, output_mode="skill"),
        "references/topic-guide.md": topic_guide,
        "references/source-checklist.md": "# 来源核对清单\n\n- [ ] 标题与作者\n- [ ] 原始链接\n- [ ] 关键数字与日期\n- [ ] 案例和结论的适用边界\n- [ ] 工具或平台版本\n- [ ] 待验证推断\n",
        f"assets/{skill_name}-template.md": deliverable_template,
    }


def _markdown_inline_to_reportlab(value: Any, *, code_font: str) -> str:
    raw = str(value or "")
    tokens: Dict[str, str] = {}

    def stash(fragment: str) -> str:
        key = f"@@CP{len(tokens)}@@"
        tokens[key] = fragment
        return key

    raw = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        lambda match: stash(
            f'<a href="{html.escape(match.group(2), quote=True)}" color="#4F46E5">'
            f'{html.escape(match.group(1))}</a>'
        ),
        raw,
    )
    raw = re.sub(
        r"`([^`]+)`",
        lambda match: stash(
            f'<font name="{html.escape(code_font, quote=True)}" backColor="#F2F4F7">'
            f'{html.escape(match.group(1))}</font>'
        ),
        raw,
    )
    rendered = html.escape(raw)
    rendered = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", rendered)
    for key, fragment in tokens.items():
        rendered = rendered.replace(key, fragment)
    return rendered


def _write_product_pdf(path: Path, title: str, markdown_text: str, asset_root: Path) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import CondPageBreak
        from reportlab.platypus import Image as RLImage
        from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, Preformatted, SimpleDocTemplate, Spacer
    except ImportError as exc:
        raise ContentProductError("生成 PDF 需要安装 reportlab，请执行 pip install -r requirements.txt") from exc

    # Prefer an embedded Unicode font.  ReportLab's built-in CID fonts can be
    # extracted as text but render as blank pages in some PDF viewers and
    # Poppler builds when the matching CJK font pack is absent.
    font_name = "ContentProductCJK"
    font_candidates = [
        os.getenv("XIANYU_PDF_FONT", ""),
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    for font_path in font_candidates:
        if not font_path or not Path(font_path).is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont(font_name, font_path, subfontIndex=0))
            break
        except Exception:
            continue
    else:
        font_name = "STSong-Light"
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))
    try:
        pdfmetrics.registerFontFamily(
            font_name, normal=font_name, bold=font_name, italic=font_name, boldItalic=font_name
        )
    except Exception:
        pass
    styles = getSampleStyleSheet()
    ink = colors.HexColor("#172033")
    accent = colors.HexColor("#4338CA")
    base = ParagraphStyle(
        "CN", parent=styles["BodyText"], fontName=font_name, fontSize=10.8,
        leading=18, textColor=ink, alignment=TA_LEFT, spaceAfter=8,
        allowWidows=0, allowOrphans=0,
    )
    h1 = ParagraphStyle(
        "CN-H1", parent=base, fontSize=25, leading=34, textColor=colors.HexColor("#1F2A44"),
        alignment=TA_LEFT, spaceBefore=8, spaceAfter=13, keepWithNext=True,
    )
    h2 = ParagraphStyle(
        "CN-H2", parent=base, fontSize=17, leading=25, textColor=accent,
        spaceBefore=16, spaceAfter=8, keepWithNext=True,
    )
    h3 = ParagraphStyle(
        "CN-H3", parent=base, fontSize=13.5, leading=21, textColor=colors.HexColor("#312E81"),
        spaceBefore=12, spaceAfter=6, keepWithNext=True,
    )
    h4 = ParagraphStyle(
        "CN-H4", parent=base, fontSize=11.8, leading=19, textColor=colors.HexColor("#344054"),
        spaceBefore=9, spaceAfter=4, keepWithNext=True,
    )
    bullet = ParagraphStyle(
        "CN-Bullet", parent=base, leftIndent=16, firstLineIndent=-9,
        bulletIndent=4, spaceAfter=5, alignment=TA_LEFT,
    )
    quote = ParagraphStyle(
        "CN-Quote", parent=base, leftIndent=14, rightIndent=8, borderColor=colors.HexColor("#A5B4FC"),
        borderWidth=0, borderPadding=(3, 8, 3, 10), backColor=colors.HexColor("#F5F7FF"),
        textColor=colors.HexColor("#475467"), alignment=TA_LEFT, spaceBefore=4, spaceAfter=9,
    )
    caption = ParagraphStyle(
        "CN-Caption", parent=base, fontSize=8.5, leading=12, textColor=colors.HexColor("#667085"),
        alignment=TA_CENTER, spaceBefore=3, spaceAfter=8,
    )
    code_style = ParagraphStyle(
        "CN-Code", parent=base, fontName=font_name, fontSize=8.8, leading=13,
        leftIndent=8, rightIndent=8, backColor=colors.HexColor("#F2F4F7"),
        borderPadding=8, alignment=TA_LEFT, splitLongWords=True,
    )

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 8)
        canvas.setFillColor(colors.HexColor("#8791A5"))
        canvas.drawString(20 * mm, 11 * mm, str(title)[:40])
        canvas.drawRightString(190 * mm, 11 * mm, f"第 {doc.page} 页")
        canvas.restoreState()

    story: List[Any] = []
    lines = markdown_text.splitlines()
    in_code = False
    code_lines: List[str] = []
    ordered_index = 0
    for raw in lines:
        line = raw.strip()
        if line.startswith("```"):
            if in_code:
                story.append(Preformatted("\n".join(code_lines), code_style))
                code_lines = []
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(raw.rstrip())
            continue
        image_match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line)
        if image_match:
            relative = image_match.group(2).strip()
            candidate = (asset_root / relative).resolve()
            try:
                candidate.relative_to(asset_root.resolve())
            except ValueError:
                continue
            if candidate.is_file():
                try:
                    preview = RLImage(str(candidate))
                    scale = min((160 * mm) / preview.imageWidth, (92 * mm) / preview.imageHeight, 1.0)
                    preview.drawWidth = preview.imageWidth * scale
                    preview.drawHeight = preview.imageHeight * scale
                    preview.hAlign = "CENTER"
                    group: List[Any] = [Spacer(1, 3 * mm), preview]
                    if image_match.group(1) and image_match.group(1) != "商品封面":
                        group.append(Paragraph(html.escape(image_match.group(1)), caption))
                    else:
                        group.append(Spacer(1, 3 * mm))
                    story.append(KeepTogether(group))
                except Exception:
                    continue
            continue
        if not line:
            if story and not isinstance(story[-1], Spacer):
                story.append(Spacer(1, 1.8 * mm))
        elif line.startswith("# "):
            story.extend([
                Spacer(1, 7 * mm),
                Paragraph(_markdown_inline_to_reportlab(line[2:], code_font=font_name), h1),
                HRFlowable(width="100%", thickness=1.1, color=colors.HexColor("#C7D2FE"), spaceAfter=10),
            ])
        elif line.startswith("## "):
            story.extend([CondPageBreak(30 * mm), Paragraph(_markdown_inline_to_reportlab(line[3:], code_font=font_name), h2)])
        elif line.startswith("### "):
            story.append(Paragraph(_markdown_inline_to_reportlab(line[4:], code_font=font_name), h3))
        elif line.startswith("#### "):
            story.append(Paragraph(_markdown_inline_to_reportlab(line[5:], code_font=font_name), h4))
        elif re.match(r"^(?:[-*•])\s+", line):
            ordered_index = 0
            clean = re.sub(r"^(?:[-*•])\s+", "", line)
            story.append(Paragraph(_markdown_inline_to_reportlab(clean, code_font=font_name), bullet, bulletText="•"))
        elif re.match(r"^\d+[.)]\s+", line):
            ordered_index += 1
            clean = re.sub(r"^\d+[.)]\s+", "", line)
            story.append(Paragraph(_markdown_inline_to_reportlab(clean, code_font=font_name), bullet, bulletText=f"{ordered_index}."))
        elif line.startswith(">"):
            story.append(Paragraph(_markdown_inline_to_reportlab(line.lstrip("> "), code_font=font_name), quote))
        elif line == "---":
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#D0D5DD"), spaceBefore=6, spaceAfter=8))
        else:
            ordered_index = 0
            story.append(Paragraph(_markdown_inline_to_reportlab(line, code_font=font_name), base))
    if code_lines:
        story.append(Preformatted("\n".join(code_lines), code_style))
    doc = SimpleDocTemplate(
        str(path), pagesize=A4, rightMargin=20 * mm, leftMargin=20 * mm,
        topMargin=17 * mm, bottomMargin=20 * mm, title=title, author="闲鱼管理系统",
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def _write_product_docx(path: Path, title: str, markdown_text: str, asset_root: Path) -> None:
    """Create the rich intermediate document imported into Feishu."""
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.enum.style import WD_STYLE_TYPE
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Inches, Pt, RGBColor
        from PIL import Image as PILImage
    except ImportError as exc:
        raise ContentProductError("生成飞书排版文档需要安装 python-docx 和 Pillow") from exc

    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = section.bottom_margin = Inches(1.0)
    section.left_margin = section.right_margin = Inches(1.0)
    section.header_distance = section.footer_distance = Inches(0.492)

    # This single-file Unicode font renders consistently during headless DOCX
    # conversion; several macOS CJK TTC families otherwise become tofu glyphs.
    font_name = "Arial Unicode MS"
    cjk_font = "Arial Unicode MS"
    styles = doc.styles

    def style_font(style, size: float, color: str, *, bold: bool = False) -> None:
        style.font.name = font_name
        style._element.rPr.rFonts.set(qn("w:ascii"), font_name)
        style._element.rPr.rFonts.set(qn("w:hAnsi"), font_name)
        style._element.rPr.rFonts.set(qn("w:eastAsia"), cjk_font)
        language = style._element.rPr.find(qn("w:lang"))
        if language is None:
            language = OxmlElement("w:lang")
            style._element.rPr.append(language)
        language.set(qn("w:val"), "zh-CN")
        language.set(qn("w:eastAsia"), "zh-CN")
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor.from_string(color)
        style.font.bold = bold

    normal = styles["Normal"]
    style_font(normal, 11, "172033")
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(8)
    normal.paragraph_format.line_spacing = 1.333
    normal.paragraph_format.keep_together = False
    normal.paragraph_format.widow_control = True
    heading_tokens = {
        "Heading 1": (16, "4338CA", 18, 10),
        "Heading 2": (13, "4338CA", 12, 6),
        "Heading 3": (12, "312E81", 8, 4),
    }
    for style_name, (size, color, before, after) in heading_tokens.items():
        style = styles[style_name]
        style_font(style, size, color, bold=True)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.page_break_before = False
    for style_name in ("List Bullet", "List Number"):
        style = styles[style_name]
        style_font(style, 11, "172033")
        style.paragraph_format.left_indent = Inches(0.375)
        style.paragraph_format.first_line_indent = Inches(-0.188)
        style.paragraph_format.space_after = Pt(4)
        style.paragraph_format.line_spacing = 1.208
    if "Content Quote" not in styles:
        quote_style = styles.add_style("Content Quote", WD_STYLE_TYPE.PARAGRAPH)
    else:
        quote_style = styles["Content Quote"]
    style_font(quote_style, 10.5, "475467")
    quote_style.paragraph_format.left_indent = Inches(0.25)
    quote_style.paragraph_format.right_indent = Inches(0.15)
    quote_style.paragraph_format.space_before = Pt(4)
    quote_style.paragraph_format.space_after = Pt(9)
    quote_style.paragraph_format.line_spacing = 1.25
    if "Content Code" not in styles:
        code_style = styles.add_style("Content Code", WD_STYLE_TYPE.PARAGRAPH)
    else:
        code_style = styles["Content Code"]
    style_font(code_style, 9, "344054")
    code_style.paragraph_format.left_indent = Inches(0.15)
    code_style.paragraph_format.right_indent = Inches(0.15)
    code_style.paragraph_format.space_after = Pt(8)

    header = section.header.paragraphs[0]
    header.text = str(title)[:60]
    header.alignment = WD_ALIGN_PARAGRAPH.LEFT
    for run in header.runs:
        run.font.size = Pt(8.5)
        run.font.color.rgb = RGBColor.from_string("667085")
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    page_run = footer.add_run("第 ")
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    page_run._r.addnext(field)
    tail = footer.add_run(" 页")
    for run in footer.runs:
        run.font.size = Pt(8.5)
        run.font.color.rgb = RGBColor.from_string("667085")

    def set_run(run, *, bold: Optional[bool] = None, italic: Optional[bool] = None, code: bool = False) -> None:
        run.font.name = font_name
        run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), font_name)
        run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), font_name)
        run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), cjk_font)
        language = run._element.get_or_add_rPr().find(qn("w:lang"))
        if language is None:
            language = OxmlElement("w:lang")
            run._element.get_or_add_rPr().append(language)
        language.set(qn("w:val"), "zh-CN")
        language.set(qn("w:eastAsia"), "zh-CN")
        if bold is not None:
            run.bold = bold
        if italic is not None:
            run.italic = italic
        if code:
            run.font.name = "Menlo"
            run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), "Menlo")
            run.font.size = Pt(9.5)
            shade = OxmlElement("w:shd")
            shade.set(qn("w:fill"), "F2F4F7")
            run._element.get_or_add_rPr().append(shade)

    def add_hyperlink(paragraph, label: str, url: str) -> None:
        relationship = paragraph.part.relate_to(
            url,
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            is_external=True,
        )
        hyperlink = OxmlElement("w:hyperlink")
        hyperlink.set(qn("r:id"), relationship)
        run_element = OxmlElement("w:r")
        props = OxmlElement("w:rPr")
        color = OxmlElement("w:color")
        color.set(qn("w:val"), "4F46E5")
        props.append(color)
        underline = OxmlElement("w:u")
        underline.set(qn("w:val"), "single")
        props.append(underline)
        run_element.append(props)
        text_element = OxmlElement("w:t")
        text_element.text = label
        run_element.append(text_element)
        hyperlink.append(run_element)
        paragraph._p.append(hyperlink)

    token_pattern = re.compile(r"(\*\*[^*]+\*\*|(?<!\*)\*[^*]+\*(?!\*)|`[^`]+`|\[[^\]]+\]\(https?://[^)]+\))")

    def add_inline(paragraph, text: str) -> None:
        for token in filter(None, token_pattern.split(str(text or ""))):
            link = re.fullmatch(r"\[([^\]]+)\]\((https?://[^)]+)\)", token)
            if link:
                add_hyperlink(paragraph, link.group(1), link.group(2))
            elif token.startswith("**") and token.endswith("**"):
                run = paragraph.add_run(token[2:-2])
                set_run(run, bold=True)
            elif token.startswith("*") and token.endswith("*"):
                run = paragraph.add_run(token[1:-1])
                set_run(run, italic=True)
            elif token.startswith("`") and token.endswith("`"):
                run = paragraph.add_run(token[1:-1])
                set_run(run, code=True)
            else:
                run = paragraph.add_run(token)
                set_run(run)

    lines = markdown_text.splitlines()
    in_code = False
    code_lines: List[str] = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("```"):
            if in_code:
                paragraph = doc.add_paragraph(style="Content Code")
                add_inline(paragraph, "\n".join(code_lines))
                shade = OxmlElement("w:shd")
                shade.set(qn("w:fill"), "F2F4F7")
                paragraph._p.get_or_add_pPr().append(shade)
                code_lines = []
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(raw.rstrip())
            continue
        image_match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line)
        if image_match:
            candidate = (asset_root / image_match.group(2).strip()).resolve()
            try:
                candidate.relative_to(asset_root.resolve())
            except ValueError:
                continue
            if candidate.is_file():
                try:
                    with PILImage.open(candidate) as image:
                        width_px, height_px = image.size
                    width = min(6.2, width_px / max(1, height_px) * 4.2)
                    height = width * height_px / max(1, width_px)
                    if height > 4.2:
                        height = 4.2
                        width = height * width_px / max(1, height_px)
                    paragraph = doc.add_paragraph()
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    run = paragraph.add_run()
                    run.add_picture(str(candidate), width=Inches(width), height=Inches(height))
                    paragraph.paragraph_format.space_before = Pt(5)
                    paragraph.paragraph_format.space_after = Pt(3)
                    if image_match.group(1) and image_match.group(1) != "商品封面":
                        caption_paragraph = doc.add_paragraph()
                        caption_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        caption_paragraph.paragraph_format.space_after = Pt(8)
                        caption_run = caption_paragraph.add_run(image_match.group(1))
                        set_run(caption_run)
                        caption_run.font.size = Pt(8.5)
                        caption_run.font.color.rgb = RGBColor.from_string("667085")
                except Exception:
                    continue
            continue
        if not line:
            continue
        if line.startswith("# "):
            paragraph = doc.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
            paragraph.paragraph_format.space_before = Pt(8)
            paragraph.paragraph_format.space_after = Pt(12)
            run = paragraph.add_run(line[2:])
            set_run(run, bold=True)
            run.font.size = Pt(22)
            run.font.color.rgb = RGBColor.from_string("1F2A44")
        elif line.startswith("#### "):
            paragraph = doc.add_paragraph(style="Heading 3")
            add_inline(paragraph, line[5:])
        elif line.startswith("### "):
            paragraph = doc.add_paragraph(style="Heading 2")
            add_inline(paragraph, line[4:])
        elif line.startswith("## "):
            paragraph = doc.add_paragraph(style="Heading 1")
            add_inline(paragraph, line[3:])
        elif re.match(r"^[-*•]\s+", line):
            paragraph = doc.add_paragraph(style="List Bullet")
            add_inline(paragraph, re.sub(r"^[-*•]\s+", "", line))
        elif re.match(r"^\d+[.)]\s+", line):
            paragraph = doc.add_paragraph(style="List Number")
            add_inline(paragraph, re.sub(r"^\d+[.)]\s+", "", line))
        elif line.startswith(">"):
            paragraph = doc.add_paragraph(style="Content Quote")
            add_inline(paragraph, line.lstrip("> "))
            shade = OxmlElement("w:shd")
            shade.set(qn("w:fill"), "F5F7FF")
            paragraph._p.get_or_add_pPr().append(shade)
        elif line == "---":
            paragraph = doc.add_paragraph()
            border = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "4")
            bottom.set(qn("w:color"), "D0D5DD")
            border.append(bottom)
            paragraph._p.get_or_add_pPr().append(border)
        else:
            paragraph = doc.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
            add_inline(paragraph, line)
    if code_lines:
        paragraph = doc.add_paragraph(style="Content Code")
        add_inline(paragraph, "\n".join(code_lines))
    doc.core_properties.title = str(title)
    doc.core_properties.author = "闲鱼管理系统"
    doc.save(path)


def write_product_package(
    base_dir: Path,
    product_id: str,
    source: ContentSource,
    listing: Dict[str, Any],
    *,
    rights_status: str,
    output_mode: str,
    output_formats: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Write editable exports and return paths relative to the data directory."""
    product_dir = Path(base_dir) / str(product_id)
    product_dir.mkdir(parents=True, exist_ok=True)
    image_assets: List[Dict[str, Any]] = []
    image_dir = product_dir / 'images'
    for index, image in enumerate(listing.get('images') or [], start=1):
        if not isinstance(image, dict):
            continue
        encoded = str(image.get('data') or image.get('content') or image.get('base64') or '').strip()
        if not encoded:
            continue
        if encoded.startswith('data:'):
            _, _, encoded = encoded.partition(',')
        try:
            raw = base64.b64decode(re.sub(r'\s+', '', encoded), validate=True)
        except Exception:
            continue
        if not raw:
            continue
        image_dir.mkdir(parents=True, exist_ok=True)
        suffix = '.png' if raw.startswith(b'\x89PNG') else '.jpg'
        image_path = image_dir / f'image-{index}{suffix}'
        image_path.write_bytes(raw)
        source_url = str(image.get('source_url') or image.get('url') or '').strip()
        filename = str(image.get('filename') or image.get('name') or image_path.name)
        explicit_role = str(image.get('role') or '').strip().lower()
        role = explicit_role or ('cover' if index == 1 and filename == 'ai_product_cover.jpg' else 'source')
        image_assets.append({
            'path': str(image_path),
            'relative_path': f'images/{image_path.name}',
            'filename': filename,
            'source_url': source_url,
            'alt': str(image.get('alt') or '').strip(),
            'role': role,
            'width': image.get('width'),
            'height': image.get('height'),
        })
    document = build_product_document(
        source, listing, rights_status=rights_status, output_mode=output_mode,
        image_assets=image_assets,
    )
    formats = {str(item).strip().lower() for item in (output_formats or []) if str(item).strip()}
    if not formats:
        formats = {'markdown', 'pdf', 'skill'} if output_mode == 'bundle' else ({'skill'} if output_mode == 'skill' else {'markdown'})
    markdown_path = product_dir / "product.md"
    markdown_path.write_text(document, encoding="utf-8")
    (product_dir / "source.txt").write_text(source.text, encoding="utf-8")
    (product_dir / "metadata.json").write_text(
        json.dumps({
            "source": source.to_dict(),
            "listing": listing,
            "rights_status": rights_status,
            "image_assets": [
                {key: value for key, value in item.items() if key != 'path'} for item in image_assets
            ],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    skill_files = build_skill_files(source, listing, rights_status=rights_status) if 'skill' in formats else {}
    for relative, content in skill_files.items():
        path = product_dir / "skill" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    pdf_path = product_dir / "product.pdf"
    if 'pdf' in formats:
        _write_product_pdf(pdf_path, str(listing.get('title') or source.title), document, product_dir)
    elif pdf_path.exists():
        pdf_path.unlink()
    docx_path = product_dir / "product.docx"
    if 'feishu' in formats:
        _write_product_docx(docx_path, str(listing.get('title') or source.title), document, product_dir)
    elif docx_path.exists():
        docx_path.unlink()
    zip_path = product_dir.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in product_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(product_dir).as_posix())
    return {
        "directory": str(product_dir),
        "zip": str(zip_path),
        "markdown": str(markdown_path),
        "pdf": str(pdf_path) if pdf_path.is_file() else "",
        "docx": str(docx_path) if docx_path.is_file() else "",
        "skill": str(product_dir / "skill") if skill_files else "",
    }
