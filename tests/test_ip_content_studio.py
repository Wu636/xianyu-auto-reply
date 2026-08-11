import base64
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from utils.ip_content_studio import (
    assess_originality,
    build_generation_prompts,
    build_source_image_review_prompts,
    build_visual_identity_prompt,
    compose_master_with_visuals,
    markdown_to_wechat_html,
    normalize_analysis,
    normalize_source_image_review,
    preclean_source_text,
    save_source_visual_assets,
    save_visual_asset,
    write_project_package,
)


def _jpeg_data() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (900, 900), (79, 70, 229)).save(buffer, "JPEG", quality=90)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class IPContentStudioTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "name": "我的科技号",
            "display_name": "小林",
            "positioning": "帮助普通人把 AI 用进日常工作",
            "audience": "职场新人和个体创业者",
            "topics": ["AI 工具", "工作方法"],
            "tone": "真诚、具体、像朋友聊天",
            "opening_template": "大家好，我是小林。",
            "closing_template": "如果有帮助，欢迎收藏。",
            "bio": "持续分享 AI 工具与个人成长。",
            "experiences": ["连续 30 天记录自己使用 AI 的工作流程"],
            "forbidden_phrases": ["月入十万"],
            "visual_style": "蓝紫色扁平插画，固定短发人物形象",
            "brand_colors": ["#4F46E5", "#F59E0B"],
        }

    def test_preclean_separates_identity_promotion_and_metrics(self):
        source = """看完这篇文章，你就知道如何建立写作系统了
62
124
208K
朋友们大家好啊，我是金尘马。
真正稳定的写作，来自持续记录、独立审稿和复盘。
每次写完都记录问题，下一次从更准确的起点开始。
金尘马｜大厂程序员｜联系方式见主页介绍：https://x.com/jinchenma_ai
想发表自己的文章吗？
升级至高级版"""
        result = preclean_source_text(source, author="金尘马")
        self.assertIn("真正稳定的写作", result["clean_text"])
        self.assertNotIn("我是金尘马", result["clean_text"])
        self.assertNotIn("联系方式", result["clean_text"])
        self.assertNotIn("208K", result["clean_text"])
        reasons = " ".join(item["reason"] for item in result["excluded"])
        self.assertIn("原作者身份", reasons)
        self.assertIn("平台界面", reasons)

    def test_generation_prompt_has_no_author_claim_or_promotion_payload(self):
        analysis = normalize_analysis({
            "clean_knowledge": "持续记录问题并进行多轮独立审稿，会形成可复用的写作系统。",
            "core_thesis": "把写作当成可迭代系统",
            "insights": ["保存复盘记录"],
            "source_author_claims": ["我在大厂工作，并靠课程赚了很多钱"],
            "excluded_sections": [{"text": "关注我的课程", "reason": "原作者宣传"}],
        }, {"clean_text": "持续记录问题并进行多轮独立审稿。", "excluded": []})
        _, user_prompt = build_generation_prompts(
            profile=self.profile,
            analysis=analysis,
            angle="recent_learning",
            custom_direction="给普通人三个可执行动作",
            platforms=["wechat", "xiaohongshu"],
            source_title="去 AI 味的方法",
        )
        self.assertIn("连续 30 天记录", user_prompt)
        self.assertIn("持续记录问题", user_prompt)
        self.assertNotIn("靠课程赚了很多钱", user_prompt)
        self.assertNotIn("关注我的课程", user_prompt)

    def test_quality_report_detects_identity_contamination(self):
        analysis = {
            "excluded_sections": [
                {"text": "大家好，我是原作者老王", "reason": "原作者身份"},
                {"text": "点击购买我的课程", "reason": "原作者宣传"},
            ]
        }
        result = assess_originality(
            "大家好，我是原作者老王。稳定写作依赖复盘。点击购买我的课程。",
            "# 新文章\n\n大家好，我是原作者老王。这里讨论如何复盘。",
            analysis,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(result["source_identity_contamination"])

    def test_visual_identity_prompt_is_stable_and_uses_library_notes(self):
        kit = {
            "character_name": "小林",
            "character_description": "短发、圆框眼镜、深蓝连帽衫",
            "signature_elements": "橙色耳机与米白纸张纹理",
            "style_keywords": "扁平杂志插画、柔和颗粒",
            "negative_prompt": "平台Logo、二维码、杂乱小字",
            "cover_rules": "人物居左，右侧保留标题区",
            "inline_rules": "4:3 横图，人物不超过画面三分之一",
        }
        references = [{
            "name": "正面定妆", "asset_type": "portrait",
            "tags": ["正面", "半身"], "notes": "发型和眼镜形状保持一致",
        }]
        first = build_visual_identity_prompt(
            profile=self.profile, visual_kit=kit, reference_assets=references,
            purpose="cover", user_prompt="整理写作流程的文章封面",
        )
        second = build_visual_identity_prompt(
            profile=self.profile, visual_kit=kit, reference_assets=references,
            purpose="cover", user_prompt="整理写作流程的文章封面",
        )
        self.assertEqual(first, second)
        self.assertIn("固定视觉IP指纹", first)
        self.assertIn("橙色耳机", first)
        self.assertIn("发型和眼镜形状保持一致", first)
        self.assertIn("人物居左", first)
        self.assertIn("平台Logo", first)

    def test_source_images_are_reviewed_and_inserted_near_matching_section(self):
        markdown = """# 模型选择指南

## 追求质量时怎么选

质量优先时关注模型能力和参数。

## 追求速度时怎么选

批量预览时优先关注响应速度。
"""
        images = [
            {"filename": "settings.jpg", "data": _jpeg_data(), "width": 1200, "height": 675, "alt": "模型设置界面"},
            {"filename": "promo.jpg", "data": _jpeg_data(), "width": 1200, "height": 675, "alt": "课程宣传"},
        ]
        system, user = build_source_image_review_prompts(
            source_title="模型选择", source_url="https://example.com/source",
            master_markdown=markdown, source_images=images, image_strategy="smart_mix",
        )
        self.assertIn("软件界面", system)
        self.assertIn("追求质量时怎么选", user)
        decisions = normalize_source_image_review({"images": [
            {
                "index": 1, "classification": "interface", "recommended_mode": "use_original",
                "reason": "界面参数有助于核对步骤", "insert_after_heading": "追求质量时怎么选",
                "caption": "模型设置界面示例", "sensitive_notes": "遮挡账号信息",
            },
            {
                "index": 2, "classification": "promotion", "recommended_mode": "use_original",
                "reason": "原作者课程宣传", "insert_after_heading": "追求速度时怎么选",
                "caption": "宣传图",
            },
        ]}, images, markdown, "smart_mix")
        self.assertEqual(decisions[0]["use_mode"], "use_original")
        self.assertEqual(decisions[1]["use_mode"], "omit")
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "9"
            visuals = save_source_visual_assets(
                project_dir, images, decisions,
                article_source_url="https://example.com/source", source_author="原作者",
            )
            composed = compose_master_with_visuals(markdown, visuals)
            heading_offset = composed.index("## 追求质量时怎么选")
            image_offset = composed.index("![模型设置界面示例]")
            paragraph_offset = composed.index("质量优先时关注")
            self.assertLess(heading_offset, image_offset)
            self.assertLess(image_offset, paragraph_offset)
            self.assertIn("查看原文", composed)
            self.assertNotIn("宣传图](", composed)
            self.assertTrue(Path(visuals[0]["path"]).is_file())
            project = {
                "title": "模型选择指南",
                "summary": "按需求选择模型。",
                "master_markdown": markdown,
                "variants": {},
                "visuals": visuals,
                "analysis": {"core_thesis": "模型选择"},
                "quality": {"originality_score": 90, "passed": True},
            }
            package_paths = write_project_package(Path(temp_dir), 9, project, self.profile)
            manifest = json.loads((project_dir / "manifest.json").read_text(encoding="utf-8"))
            review = json.loads((project_dir / "source-image-review.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["original_source_images_reused"])
            self.assertEqual(manifest["source_image_count"], 2)
            self.assertEqual(len(review), 2)
            self.assertNotIn("path", review[0])
            with zipfile.ZipFile(package_paths["zip"]) as archive:
                names = set(archive.namelist())
                self.assertIn("ip-content-9/source-image-review.json", names)
                self.assertIn("ip-content-9/assets/source-1.jpg", names)

    def test_exports_self_contained_html_docx_pdf_and_zip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            project_dir = base_dir / "7"
            visual = save_visual_asset(project_dir, {
                "id": "visual-1", "slot": "cover", "alt": "新的 IP 封面",
                "prompt": "蓝紫色个人知识管理插画",
            }, {"content": _jpeg_data(), "width": 900, "height": 900})
            project = {
                "title": "我最近重新理解了：写作不是灵感，而是一套系统",
                "summary": "一套普通人也能执行的记录、审稿和复盘方法。",
                "master_markdown": """# 我最近重新理解了：写作不是灵感，而是一套系统

大家好，我是小林。最近我重新整理了自己的写作方法。

## 先记录问题，而不是追求完美

每次写完，把反复出现的问题记下来，下一次优先检查。

## 用三轮独立审稿代替一次润色

- 第一轮只看结构
- 第二轮只看事实
- 第三轮只看表达

> 真正可复用的不是某篇成稿，而是你的判断规则。
""",
                "variants": {
                    "wechat": {"markdown": "公众号版本"},
                    "xiaohongshu": {"title": "写作系统", "body": "三个动作", "tags": ["写作"]},
                },
                "visuals": [visual],
                "analysis": {"core_thesis": "写作系统"},
                "quality": {"originality_score": 92, "passed": True},
            }
            paths = write_project_package(base_dir, 7, project, self.profile)
            html_text = Path(paths["html"]).read_text(encoding="utf-8")
            self.assertIn("data:image/jpeg;base64,", html_text)
            self.assertNotIn("src=\"assets/", html_text)
            self.assertTrue(Path(paths["docx"]).is_file())
            self.assertTrue(Path(paths["pdf"]).is_file())
            pdf_text = "".join(page.extract_text() or "" for page in PdfReader(paths["pdf"]).pages)
            self.assertIn("写作不是灵感", pdf_text)
            with zipfile.ZipFile(paths["zip"]) as archive:
                names = set(archive.namelist())
                self.assertIn("ip-content-7/article.html", names)
                self.assertIn("ip-content-7/article.docx", names)
                self.assertIn("ip-content-7/article.pdf", names)
                self.assertIn("ip-content-7/assets/visual-1.jpg", names)
            with zipfile.ZipFile(paths["docx"]) as archive:
                self.assertTrue(any(name.startswith("word/media/") for name in archive.namelist()))

    def test_wechat_html_preserves_hierarchy_and_emphasis(self):
        rendered = markdown_to_wechat_html(
            "# 标题\n\n## 重点\n\n这是一段 **加粗内容**。\n\n- 动作一\n- 动作二",
            "标题",
        )
        self.assertIn("<h1", rendered)
        self.assertIn("<h2", rendered)
        self.assertIn("<strong>加粗内容</strong>", rendered)
        self.assertIn("<li", rendered)


if __name__ == "__main__":
    unittest.main()
