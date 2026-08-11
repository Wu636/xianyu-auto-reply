import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from utils.content_productizer import (
    ContentSource,
    build_skill_files,
    normalize_source_blocks,
    write_product_package,
)


def _image_data() -> str:
    buffer = io.BytesIO()
    Image.new('RGB', (120, 80), (76, 87, 230)).save(buffer, 'JPEG')
    return base64.b64encode(buffer.getvalue()).decode('ascii')


class ContentProductizerTests(unittest.TestCase):
    def test_builds_markdown_pdf_and_topic_specific_skill(self):
        source = ContentSource(
            url='https://x.com/example/status/1',
            title='用 AI Agent 自动整理客户访谈',
            text='# 准备输入\n整理访谈录音和目标。\n# 执行自动化\n使用模型提取痛点并人工核验。',
            excerpt='整理客户访谈',
            fetched=True,
            blocks=[
                {'type': 'heading', 'level': 1, 'markdown': '准备输入'},
                {'type': 'paragraph', 'markdown': '整理**访谈录音**和目标。'},
                {'type': 'image', 'source_url': 'https://pbs.twimg.com/media/source-one', 'alt': '访谈整理流程'},
                {'type': 'heading', 'level': 2, 'markdown': '执行自动化'},
                {'type': 'paragraph', 'markdown': '使用模型提取痛点并人工核验。'},
            ],
        )
        listing = {
            'title': 'AI Agent客户访谈整理实战',
            'description': '一套从访谈输入到结构化结论的实战流程。',
            'images': [
                {'data': _image_data(), 'filename': 'ai_product_cover.jpg', 'role': 'cover'},
                {
                    'data': _image_data(), 'filename': 'source.jpg', 'role': 'source',
                    'source_url': 'https://pbs.twimg.com/media/source-one', 'alt': '访谈整理流程',
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = write_product_package(
                Path(temp_dir), '42', source, listing,
                rights_status='self_owned', output_mode='markdown+pdf+skill',
                output_formats=['markdown', 'pdf', 'feishu', 'skill'],
            )
            self.assertTrue(Path(paths['markdown']).is_file())
            self.assertTrue(Path(paths['pdf']).is_file())
            self.assertTrue(Path(paths['docx']).is_file())
            self.assertTrue(Path(paths['zip']).is_file())
            markdown = Path(paths['markdown']).read_text(encoding='utf-8')
            self.assertNotIn('## 随包图片', markdown)
            self.assertLess(markdown.index('整理**访谈录音**'), markdown.index('![访谈整理流程]'))
            self.assertLess(markdown.index('![访谈整理流程]'), markdown.index('执行自动化'))
            pdf_text = ''.join(page.extract_text() or '' for page in PdfReader(paths['pdf']).pages)
            self.assertIn('AI Agent', pdf_text)
            self.assertNotIn('![访谈整理流程]', pdf_text)
            with zipfile.ZipFile(paths['zip']) as archive:
                names = set(archive.namelist())
                self.assertIn('product.pdf', names)
                self.assertIn('product.docx', names)
                self.assertIn('images/image-1.jpg', names)
                self.assertIn('images/image-2.jpg', names)
                self.assertIn('skill/references/topic-guide.md', names)
                self.assertTrue(any(name.startswith('skill/assets/') for name in names))
                skill_md = archive.read('skill/SKILL.md').decode('utf-8')
                self.assertIn('AI 实战', skill_md)
                agent_yaml = archive.read('skill/agents/openai.yaml').decode('utf-8')
                self.assertIn('$ai-agent', agent_yaml)
            with zipfile.ZipFile(paths['docx']) as docx_archive:
                self.assertTrue(any(name.startswith('word/media/') for name in docx_archive.namelist()))

    def test_content_theme_changes_workflow(self):
        source = ContentSource('', '小红书运营复盘', '选题\n内容钩子\n发布测试\n转化复盘', '', True)
        files = build_skill_files(source, {'title': '小红书内容运营方法'}, rights_status='self_owned')
        self.assertIn('内容运营', files['SKILL.md'])
        self.assertIn('选题表', files['references/topic-guide.md'])

    def test_sparse_rich_capture_keeps_complete_text_and_image_anchor(self):
        source = ContentSource(
            'https://x.com/example/status/2',
            '完整长文',
            ('第一段正文内容，补充背景和实际使用场景。' * 4) + '\n'
            + ('第二段正文内容，补充操作步骤和交付说明。' * 4) + '\n'
            + ('第三段正文内容，补充注意事项和适用边界。' * 4) + '\n'
            + ('第四段正文内容，补充复盘方法和后续计划。' * 4),
            '第一段正文内容。',
            True,
            blocks=[
                {'type': 'heading', 'level': 1, 'markdown': '完整长文'},
                {'type': 'image', 'source_url': 'https://pbs.twimg.com/media/one', 'alt': '配图'},
            ],
        )
        blocks = normalize_source_blocks(source)
        block_text = '\n'.join(str(item.get('text') or '') for item in blocks)
        self.assertIn('第四段正文内容', block_text)
        self.assertIn('https://pbs.twimg.com/media/one', '\n'.join(str(item) for item in blocks))


if __name__ == '__main__':
    unittest.main()
