"""Personal-IP content studio: source decontamination, prompts, QA and exports."""

from __future__ import annotations

import base64
import difflib
import hashlib
import html
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from utils.content_productizer import _write_product_docx, _write_product_pdf


ANGLE_LABELS = {
    "recent_learning": "最近学到的新认知",
    "misconception": "过去的误区与认知升级",
    "practical_problem": "从一个真实问题切入",
    "ordinary_person": "普通人如何落地",
    "contrarian": "反常识观点",
    "tutorial": "步骤型教程",
    "failure_review": "失败复盘",
    "hot_event": "热点延伸解读",
}

PLATFORM_LABELS = {
    "wechat": "公众号长文",
    "xiaohongshu": "小红书图文",
    "douyin": "抖音口播",
    "x": "X 线程",
}

SOURCE_IMAGE_STRATEGIES = {
    "smart_mix": "智能混合（推荐）",
    "ip_original": "全部使用原创 IP 配图",
    "necessary_original": "仅保留必要原图",
    "keep_all": "保留全部原图（自有或已获许可）",
}

SOURCE_IMAGE_MODES = {"use_original", "redraw", "omit"}
SOURCE_IMAGE_CLASSIFICATIONS = {
    "evidence", "interface", "steps", "data", "comparison",
    "concept", "promotion", "portrait", "decorative", "sensitive", "unknown",
}

_UI_NOISE_PATTERNS = (
    r"^(回复|转发|喜欢|书签|分享|显示此线程|查看更多|阅读原文)$",
    r"^(reply|repost|like|bookmark|share|show this thread)$",
    r"^\d+(?:\.\d+)?[KkMm万亿]?$",
    r"^(想发表自己的文章吗|升级至高级版|订阅以解锁|加入会员|立即购买)$",
)

_PROMO_MARKERS = (
    "关注我", "主页介绍", "联系方式", "购买课程", "我的课程", "我的产品",
    "付费社群", "升级至高级版", "想发表自己的文章吗", "点击购买", "限时优惠",
    "私信领取", "扫码", "联系我", "加入社群", "开源我的", "skill",
)


def _clean_line(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _looks_like_ui_noise(line: str) -> bool:
    return any(re.match(pattern, line, flags=re.IGNORECASE) for pattern in _UI_NOISE_PATTERNS)


def _looks_like_author_promo(line: str, *, line_index: int, total_lines: int, author: str = "") -> bool:
    lower = line.lower()
    near_edge = line_index < 5 or line_index >= max(0, total_lines - 10)
    author_hit = bool(author and author.lower().lstrip("@").strip() in lower)
    intro_hit = bool(re.search(r"(?:(?:朋友们)?大家好\S{0,4}|朋友们好)[，, ]*我是|^我是.{1,20}[|｜]", line))
    promo_hit = any(marker.lower() in lower for marker in _PROMO_MARKERS)
    external_profile = bool(re.search(r"https?://(?:www\.)?(?:x\.com|twitter\.com)/[A-Za-z0-9_]+/?$", line))
    return (near_edge and (intro_hit or promo_hit or author_hit)) or external_profile


def preclean_source_text(source_text: str, *, author: str = "") -> Dict[str, Any]:
    """Deterministically remove obvious platform chrome and author promotion.

    The model performs a second semantic classification. Keeping this first pass
    explicit makes the audit trail visible and prevents source identity from
    leaking into the generation prompt by accident.
    """
    raw_lines = [_clean_line(line) for line in str(source_text or "").splitlines()]
    lines = [line for line in raw_lines if line]
    kept: List[str] = []
    excluded: List[Dict[str, str]] = []
    seen: set[str] = set()
    for index, line in enumerate(lines):
        fingerprint = re.sub(r"\s+", "", line).lower()
        if not fingerprint or fingerprint in seen:
            if fingerprint:
                excluded.append({"text": line, "reason": "重复文本"})
            continue
        seen.add(fingerprint)
        if _looks_like_ui_noise(line):
            excluded.append({"text": line, "reason": "平台界面或互动数据"})
            continue
        if _looks_like_author_promo(line, line_index=index, total_lines=len(lines), author=author):
            excluded.append({"text": line, "reason": "原作者身份或宣传信息"})
            continue
        kept.append(line)
    clean_text = "\n\n".join(kept).strip()
    return {
        "clean_text": clean_text,
        "excluded": excluded,
        "source_line_count": len(lines),
        "clean_line_count": len(kept),
        "excluded_line_count": len(excluded),
    }


def profile_prompt(profile: Dict[str, Any]) -> str:
    experiences = profile.get("experiences") or []
    return json.dumps({
        "IP称呼": profile.get("display_name") or profile.get("name"),
        "定位": profile.get("positioning") or "",
        "目标读者": profile.get("audience") or "",
        "长期主题": profile.get("topics") or [],
        "表达风格": profile.get("tone") or "",
        "常用开场": profile.get("opening_template") or "",
        "常用结尾": profile.get("closing_template") or "",
        "公开简介": profile.get("bio") or "",
        "可核验经历": experiences,
        "禁用表达": profile.get("forbidden_phrases") or [],
        "视觉风格": profile.get("visual_style") or "",
        "品牌色": profile.get("brand_colors") or [],
        "平台简介": profile.get("platform_bios") or {},
    }, ensure_ascii=False, indent=2)


def build_visual_identity_prompt(
    *, profile: Dict[str, Any], visual_kit: Optional[Dict[str, Any]] = None,
    reference_assets: Optional[List[Dict[str, Any]]] = None,
    purpose: str = "inline", user_prompt: str = "",
) -> str:
    """Compose a stable visual fingerprint for every image request of an IP.

    Reference files are catalogued as art-direction evidence. Their metadata is
    always converted into explicit continuity constraints, while the caller can
    additionally send the strongest local reference images to a compatible
    image-edit endpoint.
    """
    kit = visual_kit or {}
    references = reference_assets or []
    reference_lines: List[str] = []
    for asset in references[:12]:
        details = [str(asset.get("name") or "参考素材")]
        if asset.get("asset_type"):
            details.append(f"类型={asset['asset_type']}")
        if asset.get("tags"):
            details.append(f"标签={','.join(str(tag) for tag in asset['tags'][:12])}")
        if asset.get("notes"):
            details.append(f"连续性说明={str(asset['notes'])[:500]}")
        reference_lines.append("；".join(details))
    identity = {
        "ip_name": profile.get("display_name") or profile.get("name") or "个人IP",
        "character_name": kit.get("character_name") or profile.get("display_name") or "",
        "character_description": kit.get("character_description") or "",
        "signature_elements": kit.get("signature_elements") or "",
        "style_keywords": kit.get("style_keywords") or profile.get("visual_style") or "",
        "brand_colors": profile.get("brand_colors") or [],
        "negative_prompt": kit.get("negative_prompt") or "",
        "purpose_rules": kit.get("cover_rules") if purpose == "cover" else kit.get("inline_rules"),
        "prompt_template": kit.get("prompt_template") or "",
        "references": reference_lines,
    }
    fingerprint_source = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:12]
    purpose_label = "封面图" if purpose == "cover" else "正文配图"
    return f"""{str(user_prompt or '').strip()}

【固定视觉IP指纹 {fingerprint}】
用途：{purpose_label}
IP称呼：{identity['ip_name']}
固定人物：{identity['character_name'] or '不强制出现人物'}
人物外观与设定：{identity['character_description'] or '遵循主题，不虚构真人身份'}
标志性元素：{identity['signature_elements'] or '保持克制、统一'}
统一风格关键词：{identity['style_keywords'] or '清晰、自然、有辨识度'}
品牌色：{', '.join(identity['brand_colors']) or '根据主题选择协调色'}
本类图片规则：{identity['purpose_rules'] or '主体明确，构图整洁，适合移动端阅读'}
固定提示词模板：{identity['prompt_template'] or '无额外模板'}
素材库连续性线索：{' | '.join(reference_lines) if reference_lines else '暂无，严格遵循上方文字设定'}
排除项：{identity['negative_prompt'] or '平台Logo、二维码、联系方式、水印、难辨认小字、杂乱背景、错误手指和肢体'}
所有系列图片都要保持同一人物外观、配色逻辑、线条/光影语言和标志元素；本次主题可以变化，但视觉身份不要漂移。""".strip()


def build_analysis_prompts(
    *, profile: Dict[str, Any], source_title: str, source_url: str,
    source_author: str, source_text: str, deterministic_clean: Dict[str, Any],
) -> Tuple[str, str]:
    system = """你是个人IP内容研究编辑。任务是把外部文章拆成可引用的知识，而不是改写原文。
必须严格区分：1) 核心观点/方法/证据/案例；2) 原作者身份、自我介绍、个人履历、课程商品、联系方式、互动数据、平台按钮和行动号召。
第2类全部列入 excluded_sections，后续创作不得使用。原作者个人经历只可标为 source_author_claim，不得转嫁给新IP。
不要写文章，不要生成宣传文案。只输出严格JSON对象，字段：
clean_knowledge（去污染后的知识稿，保留事实逻辑但去除原作者话术）、core_thesis、insights（数组）、methods（数组）、evidence（数组）、examples（数组）、source_author_claims（数组）、excluded_sections（数组，每项含text/reason/category）、source_outline（数组）、fact_cautions（数组）、suggested_angles（数组，每项含key/title/rationale）。"""
    user = f"""来源标题：{source_title or '未命名来源'}
来源链接：{source_url or '手动粘贴'}
原作者：{source_author or '未提供'}

本机规则预清洗结果：
{json.dumps(deterministic_clean, ensure_ascii=False, indent=2)[:12000]}

原始正文（仅用于分类核对，严禁把作者身份移植给新IP）：
{str(source_text or '')[:30000]}

目标IP档案只用于判断适合的角度，不要在分析阶段编写其口吻：
{profile_prompt(profile)}"""
    return system, user


def normalize_analysis(payload: Dict[str, Any], deterministic_clean: Dict[str, Any]) -> Dict[str, Any]:
    def list_value(key: str) -> List[Any]:
        value = payload.get(key)
        return value if isinstance(value, list) else ([] if value in (None, "") else [value])

    clean_knowledge = str(payload.get("clean_knowledge") or deterministic_clean.get("clean_text") or "").strip()
    excluded = list(deterministic_clean.get("excluded") or [])
    for item in list_value("excluded_sections"):
        if isinstance(item, dict):
            excluded.append({
                "text": str(item.get("text") or "").strip(),
                "reason": str(item.get("reason") or "语义清洗").strip(),
                "category": str(item.get("category") or "semantic").strip(),
            })
        elif str(item).strip():
            excluded.append({"text": str(item).strip(), "reason": "语义清洗", "category": "semantic"})
    unique_excluded: List[Dict[str, str]] = []
    seen = set()
    for item in excluded:
        text = str(item.get("text") or "").strip()
        if text and text not in seen:
            seen.add(text)
            unique_excluded.append(item)
    return {
        "clean_knowledge": clean_knowledge,
        "core_thesis": str(payload.get("core_thesis") or "").strip(),
        "insights": list_value("insights"),
        "methods": list_value("methods"),
        "evidence": list_value("evidence"),
        "examples": list_value("examples"),
        "source_author_claims": list_value("source_author_claims"),
        "excluded_sections": unique_excluded,
        "source_outline": list_value("source_outline"),
        "fact_cautions": list_value("fact_cautions"),
        "suggested_angles": list_value("suggested_angles"),
        "deterministic_stats": {
            key: deterministic_clean.get(key)
            for key in ("source_line_count", "clean_line_count", "excluded_line_count")
        },
    }


def build_generation_prompts(
    *, profile: Dict[str, Any], analysis: Dict[str, Any], angle: str,
    custom_direction: str, platforms: Iterable[str], source_title: str,
    image_strategy: str = "smart_mix",
) -> Tuple[str, str]:
    selected = [item for item in platforms if item in PLATFORM_LABELS] or ["wechat"]
    platform_names = [PLATFORM_LABELS[item] for item in selected]
    system = """你是资深个人IP主编。依据“去污染知识分析”写一篇全新母稿，再生成平台变体。
硬规则：
1. 不是逐段改写；重新确定论点、开头、结构、例子和结尾，输出属于目标IP的原创表达。
2. 原作者姓名、账号、自我介绍、课程/商品、联系方式、收入数据、粉丝数据、升级提示、行动号召一律不进入成稿。
3. 目标IP的第一人称经历只可来自“可核验经历”。没有对应经历时使用“最近我学习/整理/观察到”，不得编造亲身实践、收入、客户案例或购买经历。
4. 事实不确定时列入 confirmation_items，不要伪装为确定事实。
5. 母稿适合公众号直接编辑发布：标题清楚，开头自然，段落短，H2/H3层级、加粗、列表、引用合理，拒绝空话和AI腔。
6. 生成的原创配图必须保持角色、品牌色和视觉风格一致。来源截图由独立审核流程决定是否引用，visual_plan 只规划原创 IP 配图，不要把来源图片伪装成新图。
7. 小红书、抖音、X不是截断母稿，而是重新适配各平台阅读方式。
只输出严格JSON，字段：title_candidates（3个）、title、summary、master_markdown、confirmation_items、fact_notes、variants、visual_plan。
variants对象可含：wechat{title,summary,markdown}；xiaohongshu{title,body,tags,card_copy}；douyin{hook,voiceover,storyboard,subtitle_lines,cover_text}；x{posts}。
visual_plan为数组，每项含slot（cover/section/quote/flow）、section_title、insert_after_heading、alt、prompt。prompt需说明方形或横版比例、统一IP形象/品牌色、无平台Logo/二维码/联系方式/密集小字。"""
    generation_knowledge = {
        key: analysis.get(key)
        for key in ("clean_knowledge", "core_thesis", "insights", "methods", "evidence", "examples", "fact_cautions")
    }
    user = f"""来源主题：{source_title}
选定创作角度：{ANGLE_LABELS.get(angle, angle or ANGLE_LABELS['recent_learning'])}
补充方向：{custom_direction or '无'}
目标平台：{', '.join(platform_names)}
配图策略：{SOURCE_IMAGE_STRATEGIES.get(image_strategy, SOURCE_IMAGE_STRATEGIES['smart_mix'])}

目标IP档案（所有第一人称信息以此为真值边界）：
{profile_prompt(profile)}

去污染知识分析（这是唯一可用于内容事实的来源）：
{json.dumps(generation_knowledge, ensure_ascii=False, indent=2)[:30000]}

生成一篇完整、有个人判断、可直接编辑发布的公众号母稿，并给出所选平台变体。母稿首段使用目标IP的自然自我介绍；不要提及原作者，也不要假装原知识是目标IP原创发现。可使用“最近学习到/看到一个观点后，我重新想了想”等诚实引入。"""
    return system, user


def markdown_headings(markdown_text: str) -> List[str]:
    headings: List[str] = []
    for raw in str(markdown_text or "").splitlines():
        match = re.match(r"^#{2,3}\s+(.+?)\s*$", raw.strip())
        if match:
            value = re.sub(r"[*_`]+", "", match.group(1)).strip()
            if value and value not in headings:
                headings.append(value)
    return headings


def build_source_image_review_prompts(
    *, source_title: str, source_url: str, master_markdown: str,
    source_images: List[Dict[str, Any]], image_strategy: str,
) -> Tuple[str, str]:
    """Build a vision-review prompt without exposing source-image bytes in text."""
    headings = markdown_headings(master_markdown)
    metadata = []
    for index, image in enumerate(source_images[:12], start=1):
        metadata.append({
            "index": index,
            "filename": str(image.get("filename") or image.get("name") or f"source-{index}"),
            "alt": str(image.get("alt") or ""),
            "width": image.get("width"),
            "height": image.get("height"),
            "source_url": str(image.get("source_url") or ""),
        })
    system = """你是内容编辑部的图片审核员。逐张检查随消息提供的来源图片，判断它在一篇二创文章里的信息价值和风险。
优先保留：软件界面、步骤、参数、数据、前后对比等去掉后会损失关键信息的图片。
优先重绘：概念示意、流程结构、只负责解释气氛的图片。
优先排除：作者自拍/头像、课程与商品宣传、二维码、联系方式、收入或粉丝炫耀、纯装饰、明显隐私与密钥。
不要根据文件名猜测未看到的内容。输出严格JSON对象，字段 images；images 每项字段：index、classification、recommended_mode、reason、insert_after_heading、caption、sensitive_notes。
classification 仅可用 evidence/interface/steps/data/comparison/concept/promotion/portrait/decorative/sensitive/unknown；recommended_mode 仅可用 use_original/redraw/omit。
insert_after_heading 必须从给定母稿小标题中原样选择，找不到合适位置则留空。caption 是客观简短的中文图片说明，不冒充原作者结论。"""
    user = f"""来源主题：{source_title or '未命名来源'}
来源链接：{source_url or '未提供'}
本次策略：{SOURCE_IMAGE_STRATEGIES.get(image_strategy, SOURCE_IMAGE_STRATEGIES['smart_mix'])}
母稿小标题：{json.dumps(headings, ensure_ascii=False)}
图片元数据（顺序与后附图片一致）：
{json.dumps(metadata, ensure_ascii=False, indent=2)}

请逐张给出审核建议。保留原图时也要指出应裁剪、遮挡或去除的敏感区域。"""
    return system, user


def _fallback_source_modes(
    source_images: List[Dict[str, Any]], image_strategy: str,
) -> Dict[int, str]:
    if image_strategy == "ip_original":
        return {index: "omit" for index in range(1, len(source_images) + 1)}
    if image_strategy == "keep_all":
        return {index: "use_original" for index in range(1, len(source_images) + 1)}

    preferred: List[int] = []
    secondary: List[int] = []
    for index, image in enumerate(source_images, start=1):
        width = float(image.get("width") or 0)
        height = float(image.get("height") or 0)
        ratio = width / height if width > 0 and height > 0 else 1.0
        # Very wide banners, tiny thumbnails and portrait video covers are less
        # likely to be indispensable explanatory screenshots.
        if width >= 700 and height >= 450 and 0.78 <= ratio <= 2.05:
            preferred.append(index)
        else:
            secondary.append(index)
    limit = 2 if image_strategy == "necessary_original" else 3
    selected = (preferred + secondary)[:limit]
    return {
        index: ("use_original" if index in selected else "redraw")
        for index in range(1, len(source_images) + 1)
    }


def normalize_source_image_review(
    payload: Optional[Dict[str, Any]], source_images: List[Dict[str, Any]],
    master_markdown: str, image_strategy: str,
) -> List[Dict[str, Any]]:
    """Normalize AI/fallback source-image decisions into auditable records."""
    strategy = image_strategy if image_strategy in SOURCE_IMAGE_STRATEGIES else "smart_mix"
    headings = markdown_headings(master_markdown)
    raw_items = (payload or {}).get("images") if isinstance(payload, dict) else []
    raw_by_index: Dict[int, Dict[str, Any]] = {}
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            if 1 <= index <= len(source_images):
                raw_by_index[index] = item
    fallback_modes = _fallback_source_modes(source_images, strategy)
    reviewed: List[Dict[str, Any]] = []
    used_count = 0
    max_originals = 3 if strategy == "smart_mix" else (2 if strategy == "necessary_original" else len(source_images))
    for index, image in enumerate(source_images[:12], start=1):
        raw = raw_by_index.get(index) or {}
        classification = str(raw.get("classification") or "unknown").strip().lower()
        if classification not in SOURCE_IMAGE_CLASSIFICATIONS:
            classification = "unknown"
        recommended = str(raw.get("recommended_mode") or fallback_modes.get(index) or "omit").strip().lower()
        if recommended not in SOURCE_IMAGE_MODES:
            recommended = fallback_modes.get(index) or "omit"
        if strategy == "ip_original":
            recommended = "omit"
        elif strategy == "keep_all":
            recommended = "use_original"
        elif classification in {"promotion", "portrait", "decorative", "sensitive"}:
            recommended = "omit" if classification != "decorative" else "redraw"
        elif recommended == "use_original" and used_count >= max_originals:
            recommended = "redraw"
        if recommended == "use_original":
            used_count += 1
        heading = str(raw.get("insert_after_heading") or "").strip()
        if heading not in headings:
            heading = headings[min(index - 1, len(headings) - 1)] if headings else ""
        alt = str(image.get("alt") or "").strip()
        if not alt or alt.lower() == "image":
            alt = f"来源说明图 {index}"
        caption = str(raw.get("caption") or "").strip() or alt
        reviewed.append({
            "index": index,
            "classification": classification,
            "recommended_mode": recommended,
            "use_mode": recommended,
            "reason": str(raw.get("reason") or (
                "识图模型未给出结论，已按尺寸与顺序作保守初选" if not raw else "待人工复核"
            )).strip(),
            "insert_after_heading": heading,
            "caption": caption[:300],
            "sensitive_notes": str(raw.get("sensitive_notes") or "").strip()[:500],
            "ai_reviewed": bool(raw),
        })
    return reviewed


def save_source_visual_assets(
    project_dir: Path, source_images: List[Dict[str, Any]], decisions: List[Dict[str, Any]],
    *, article_source_url: str = "", source_author: str = "",
) -> List[Dict[str, Any]]:
    """Persist normalized source images beside generated assets for export/review."""
    assets = Path(project_dir) / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    decision_by_index = {int(item.get("index") or 0): item for item in decisions}
    visuals: List[Dict[str, Any]] = []
    for index, image in enumerate(source_images[:12], start=1):
        decision = decision_by_index.get(index) or {"index": index, "use_mode": "omit"}
        visual: Dict[str, Any] = {
            "id": f"source-{index}",
            "source_kind": "source",
            "slot": "source",
            "source_image_index": index,
            "source_url": str(image.get("source_url") or "")[:2000],
            "article_source_url": str(article_source_url or "")[:2000],
            "source_author": str(source_author or "")[:120],
            "source_filename": str(image.get("filename") or image.get("name") or f"source-{index}.jpg")[:180],
            "alt": str(decision.get("caption") or image.get("alt") or f"来源说明图 {index}")[:300],
            "caption": str(decision.get("caption") or image.get("alt") or f"来源说明图 {index}")[:300],
            "classification": str(decision.get("classification") or "unknown"),
            "recommended_mode": str(decision.get("recommended_mode") or decision.get("use_mode") or "omit"),
            "use_mode": str(decision.get("use_mode") or "omit"),
            "reason": str(decision.get("reason") or "")[:600],
            "sensitive_notes": str(decision.get("sensitive_notes") or "")[:500],
            "insert_after_heading": str(decision.get("insert_after_heading") or "")[:300],
            "section_title": str(decision.get("insert_after_heading") or "")[:300],
            "ai_reviewed": bool(decision.get("ai_reviewed")),
            "reviewed": False,
            "width": image.get("width"),
            "height": image.get("height"),
        }
        encoded = str(image.get("data") or image.get("content") or image.get("base64") or "").strip()
        if encoded.startswith("data:"):
            encoded = encoded.partition(",")[2]
        try:
            raw = base64.b64decode(re.sub(r"\s+", "", encoded), validate=False)
            if not raw or len(raw) > 12 * 1024 * 1024:
                raise ValueError("来源图片内容为空或超过 12MB")
            path = assets / f"source-{index}.jpg"
            path.write_bytes(raw)
            visual.update({
                "status": "ready",
                "path": str(path),
                "relative_path": f"assets/{path.name}",
                "sha256": hashlib.sha256(raw).hexdigest(),
            })
        except Exception as exc:
            visual.update({"status": "failed", "error": str(exc)[:300], "use_mode": "omit"})
        visuals.append(visual)
    return visuals


def normalize_generation(payload: Dict[str, Any], platforms: Iterable[str]) -> Dict[str, Any]:
    title_candidates = payload.get("title_candidates") if isinstance(payload.get("title_candidates"), list) else []
    title = str(payload.get("title") or (title_candidates[0] if title_candidates else "未命名文章")).strip()
    markdown = str(payload.get("master_markdown") or "").strip()
    if markdown and not markdown.startswith("# "):
        markdown = f"# {title}\n\n{markdown}"
    variants = payload.get("variants") if isinstance(payload.get("variants"), dict) else {}
    if "wechat" not in variants:
        variants["wechat"] = {"title": title, "summary": payload.get("summary") or "", "markdown": markdown}
    normalized_variants = {key: value for key, value in variants.items() if key in set(platforms) | {"wechat"}}
    visuals = payload.get("visual_plan") if isinstance(payload.get("visual_plan"), list) else []
    normalized_visuals = []
    for index, item in enumerate(visuals[:8]):
        if not isinstance(item, dict):
            continue
        normalized_visuals.append({
            "id": f"visual-{index + 1}",
            "slot": str(item.get("slot") or ("cover" if index == 0 else "section")),
            "section_title": str(item.get("section_title") or ""),
            "insert_after_heading": str(item.get("insert_after_heading") or ""),
            "alt": str(item.get("alt") or item.get("section_title") or f"配图{index + 1}"),
            "prompt": str(item.get("prompt") or "").strip(),
            "status": "planned",
        })
    return {
        "title_candidates": title_candidates[:5],
        "title": title,
        "summary": str(payload.get("summary") or "").strip(),
        "master_markdown": markdown,
        "confirmation_items": payload.get("confirmation_items") if isinstance(payload.get("confirmation_items"), list) else [],
        "fact_notes": payload.get("fact_notes") if isinstance(payload.get("fact_notes"), list) else [],
        "variants": normalized_variants,
        "visuals": normalized_visuals,
    }


def _plain_text(markdown_text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", str(markdown_text or ""))
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[`*_>#~-]", "", text)
    return re.sub(r"\s+", "", text)


def assess_originality(source_text: str, generated_markdown: str, analysis: Dict[str, Any]) -> Dict[str, Any]:
    source = _plain_text(source_text)
    generated = _plain_text(generated_markdown)
    ratio = difflib.SequenceMatcher(None, source[:40000], generated[:40000], autojunk=False).ratio() if source and generated else 0.0
    generated_sentences = [item.strip() for item in re.split(r"[。！？!?\n]", generated_markdown) if len(_plain_text(item)) >= 12]
    source_sentences = [item.strip() for item in re.split(r"[。！？!?\n]", source_text) if len(_plain_text(item)) >= 12]
    overlap: List[Dict[str, Any]] = []
    for sentence in generated_sentences[:150]:
        candidate = max(
            ((difflib.SequenceMatcher(None, _plain_text(sentence), _plain_text(src), autojunk=False).ratio(), src) for src in source_sentences[:250]),
            default=(0.0, ""), key=lambda item: item[0],
        )
        if candidate[0] >= 0.86:
            overlap.append({"generated": sentence[:220], "source": candidate[1][:220], "similarity": round(candidate[0], 3)})
    contamination: List[str] = []
    generated_compact = _plain_text(generated_markdown).lower()
    for item in list(analysis.get("excluded_sections") or []):
        excluded_text = _plain_text(item.get("text") if isinstance(item, dict) else item).lower()
        if len(excluded_text) >= 8 and excluded_text[:80] in generated_compact:
            contamination.append(str(item.get("text") if isinstance(item, dict) else item)[:220])
    score = max(0, min(100, round(100 - ratio * 65 - min(35, len(overlap) * 6) - min(45, len(contamination) * 15))))
    return {
        "originality_score": score,
        "whole_text_similarity": round(ratio, 3),
        "high_similarity_passages": overlap[:12],
        "source_identity_contamination": contamination[:12],
        "passed": score >= 70 and not contamination,
    }


def save_visual_asset(project_dir: Path, visual: Dict[str, Any], image: Dict[str, Any]) -> Dict[str, Any]:
    encoded = str(image.get("content") or image.get("data") or "")
    if encoded.startswith("data:"):
        encoded = encoded.partition(",")[2]
    raw = base64.b64decode(re.sub(r"\s+", "", encoded), validate=False)
    if not raw:
        raise ValueError("生成图片为空")
    assets = Path(project_dir) / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    visual_id = re.sub(r"[^A-Za-z0-9_-]+", "-", str(visual.get("id") or "visual")).strip("-") or "visual"
    path = assets / f"{visual_id}.jpg"
    path.write_bytes(raw)
    return {
        **visual,
        "status": "ready",
        "path": str(path),
        "relative_path": f"assets/{path.name}",
        "width": image.get("width"),
        "height": image.get("height"),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def compose_master_with_visuals(master_markdown: str, visuals: List[Dict[str, Any]]) -> str:
    result = str(master_markdown or "").strip()
    ready = [
        item for item in visuals
        if item.get("status") == "ready"
        and item.get("relative_path")
        and (
            item.get("source_kind") != "source"
            or item.get("use_mode") == "use_original"
        )
        and item.get("use_mode") != "omit"
    ]
    cover = next((item for item in ready if item.get("slot") == "cover"), None)
    if cover:
        lines = result.splitlines()
        insert_at = 1 if lines and lines[0].startswith("# ") else 0
        cover_alt = _safe_markdown_label(cover.get("alt"), "文章封面")
        lines[insert_at:insert_at] = ["", f"![{cover_alt}]({cover['relative_path']})", ""]
        result = "\n".join(lines)
    for item in ready:
        if item is cover:
            continue
        image_alt = _safe_markdown_label(item.get("alt"), "文章配图")
        image_md = f"\n\n![{image_alt}]({item['relative_path']})\n"
        if item.get("source_kind") == "source":
            caption = _safe_markdown_label(item.get("caption") or item.get("alt"), "来源截图")
            source_author = _safe_markdown_label(item.get("source_author"), "")
            credit = f"来源：{source_author}" if source_author else "来源：原文"
            article_url = _safe_http_url(item.get("article_source_url"))
            source_note = f"{caption}（{credit}）"
            if article_url:
                source_note += f" · [查看原文]({article_url})"
            image_md += f"\n*{source_note}*\n"
        heading = str(item.get("insert_after_heading") or item.get("section_title") or "").strip().lstrip("# ")
        if heading:
            pattern = re.compile(rf"(^##+\s+.*{re.escape(heading)}.*$)", flags=re.MULTILINE | re.IGNORECASE)
            match = pattern.search(result)
            if match:
                result = result[:match.end()] + image_md + result[match.end():]
                continue
        result += image_md
    return re.sub(r"\n{4,}", "\n\n\n", result).strip() + "\n"


def _safe_markdown_label(value: Any, fallback: str) -> str:
    text = re.sub(r"[\r\n\[\]]+", " ", str(value or "")).strip()
    return text[:300] or fallback


def _safe_http_url(value: Any) -> str:
    url = str(value or "").strip()
    return url if re.match(r"^https?://[^\s]+$", url, flags=re.IGNORECASE) else ""


def _inline_html(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code style=\"background:#f2f4f7;padding:2px 5px;border-radius:4px\">\1</code>", escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2" target="_blank" rel="noopener noreferrer" style="color:#4f46e5">\1</a>',
        escaped,
    )
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)
    return escaped


def _self_contained_image_src(value: str, asset_root: Optional[Path]) -> str:
    """Turn a local package image into a data URI for copyable WeChat HTML."""
    source = str(value or "").strip()
    if not asset_root or source.startswith(("data:", "http://", "https://")):
        return source
    root = Path(asset_root).resolve()
    candidate = (root / source).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return source
    if not candidate.is_file() or candidate.stat().st_size > 12 * 1024 * 1024:
        return source
    suffix = candidate.suffix.lower()
    mime = {
        ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    }.get(suffix, "application/octet-stream")
    encoded = base64.b64encode(candidate.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def markdown_to_wechat_html(markdown_text: str, title: str, asset_root: Optional[Path] = None) -> str:
    parts: List[str] = []
    in_list = False
    for raw in str(markdown_text or "").splitlines():
        line = raw.strip()
        image_match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line)
        if image_match:
            if in_list:
                parts.append("</ul>")
                in_list = False
            image_src = _self_contained_image_src(image_match.group(2), asset_root)
            parts.append(f'<figure style="margin:24px 0;text-align:center"><img src="{html.escape(image_src)}" alt="{html.escape(image_match.group(1))}" style="max-width:100%;border-radius:12px"><figcaption style="font-size:13px;color:#98a2b3;margin-top:8px">{html.escape(image_match.group(1))}</figcaption></figure>')
        elif re.match(r"^[-*•]\s+", line):
            if not in_list:
                parts.append('<ul style="padding-left:1.4em;margin:10px 0 18px">')
                in_list = True
            parts.append(f'<li style="margin:8px 0">{_inline_html(re.sub(r"^[-*•]\s+", "", line))}</li>')
        else:
            if in_list:
                parts.append("</ul>")
                in_list = False
            if not line:
                continue
            if line.startswith("### "):
                parts.append(f'<h3 style="font-size:19px;color:#312e81;margin:28px 0 12px">{_inline_html(line[4:])}</h3>')
            elif line.startswith("## "):
                parts.append(f'<h2 style="font-size:23px;color:#4338ca;margin:36px 0 14px;padding-left:12px;border-left:4px solid #6366f1">{_inline_html(line[3:])}</h2>')
            elif line.startswith("# "):
                parts.append(f'<h1 style="font-size:30px;line-height:1.35;color:#172033;margin:0 0 24px">{_inline_html(line[2:])}</h1>')
            elif line.startswith(">"):
                parts.append(f'<blockquote style="margin:18px 0;padding:14px 16px;background:#f5f7ff;border-left:4px solid #a5b4fc;color:#475467">{_inline_html(line.lstrip("> "))}</blockquote>')
            else:
                parts.append(f'<p style="font-size:17px;line-height:1.9;color:#293246;margin:0 0 16px;text-align:justify">{_inline_html(line)}</p>')
    if in_list:
        parts.append("</ul>")
    body = "\n".join(parts)
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title></head><body><article style="max-width:760px;margin:36px auto;padding:0 24px;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif">{body}</article></body></html>'''


def _variant_markdown(platform: str, value: Any) -> str:
    if isinstance(value, str):
        return value.strip() + "\n"
    if not isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if platform == "wechat":
        return str(value.get("markdown") or value.get("body") or "").strip() + "\n"
    if platform == "xiaohongshu":
        tags = " ".join(f"#{str(item).lstrip('#')}" for item in value.get("tags") or [])
        cards = "\n".join(f"- {item}" for item in value.get("card_copy") or [])
        return f"# {value.get('title') or '小红书图文'}\n\n{value.get('body') or ''}\n\n## 卡片文案\n{cards}\n\n{tags}\n"
    if platform == "douyin":
        storyboard = "\n".join(f"{index}. {item}" for index, item in enumerate(value.get("storyboard") or [], start=1))
        subtitles = "\n".join(value.get("subtitle_lines") or [])
        return f"# 抖音口播与分镜\n\n## 3秒开头\n{value.get('hook') or ''}\n\n## 口播稿\n{value.get('voiceover') or ''}\n\n## 分镜\n{storyboard}\n\n## 字幕\n{subtitles}\n\n封面字：{value.get('cover_text') or ''}\n"
    posts = value.get("posts") or []
    return "# X Thread\n\n" + "\n\n---\n\n".join(f"{index}/{len(posts)} {post}" for index, post in enumerate(posts, start=1)) + "\n"


def write_project_package(base_dir: Path, project_id: int, project: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, str]:
    project_dir = Path(base_dir) / str(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    title = str(project.get("title") or "个人IP文章").strip()
    master = compose_master_with_visuals(project.get("master_markdown") or "", project.get("visuals") or [])
    (project_dir / "master.md").write_text(master, encoding="utf-8")
    (project_dir / "article.html").write_text(markdown_to_wechat_html(master, title, project_dir), encoding="utf-8")
    _write_product_docx(project_dir / "article.docx", title, master, project_dir)
    _write_product_pdf(project_dir / "article.pdf", title, master, project_dir)
    variants = project.get("variants") or {}
    for key, value in variants.items():
        (project_dir / f"{key}.md").write_text(_variant_markdown(key, value), encoding="utf-8")
    (project_dir / "source-analysis.json").write_text(json.dumps(project.get("analysis") or {}, ensure_ascii=False, indent=2), encoding="utf-8")
    (project_dir / "quality-report.json").write_text(json.dumps(project.get("quality") or {}, ensure_ascii=False, indent=2), encoding="utf-8")
    source_reviews = [
        {key: value for key, value in item.items() if key not in {"path"}}
        for item in project.get("visuals") or [] if item.get("source_kind") == "source"
    ]
    (project_dir / "source-image-review.json").write_text(
        json.dumps(source_reviews, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (project_dir / "ip-profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    manifest = {
        "project_id": project_id,
        "title": title,
        "platforms": sorted(variants.keys()),
        "original_source_images_reused": any(
            item.get("source_kind") == "source" and item.get("use_mode") == "use_original"
            for item in project.get("visuals") or []
        ),
        "source_image_count": len([
            item for item in project.get("visuals") or []
            if item.get("source_kind") == "source"
        ]),
        "source_image_used_count": len([
            item for item in project.get("visuals") or []
            if item.get("source_kind") == "source" and item.get("use_mode") == "use_original"
        ]),
        "visual_count": len([
            item for item in project.get("visuals") or []
            if item.get("status") == "ready"
            and (item.get("source_kind") != "source" or item.get("use_mode") == "use_original")
        ]),
        "files": ["master.md", "article.html", "article.docx", "article.pdf", "source-analysis.json", "source-image-review.json", "quality-report.json", "ip-profile.json"],
    }
    (project_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    zip_path = project_dir.parent / f"ip-content-{project_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(project_dir.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=f"ip-content-{project_id}/{path.relative_to(project_dir)}")
    return {
        "directory": str(project_dir),
        "zip": str(zip_path),
        "markdown": str(project_dir / "master.md"),
        "html": str(project_dir / "article.html"),
        "docx": str(project_dir / "article.docx"),
        "pdf": str(project_dir / "article.pdf"),
    }
