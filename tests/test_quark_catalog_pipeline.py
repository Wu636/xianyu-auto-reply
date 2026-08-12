import base64
import io
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from PIL import Image

from utils.quark_catalog_pipeline import (
    CatalogAIClient,
    QuarkDriveClient,
    QuarkEntry,
    build_folder_fingerprint,
    choose_listing_price,
    parse_chat_message_json,
    parse_json_object,
    sanitize_title,
    summarize_market_items,
)


def _tiny_png_base64() -> str:
    output = io.BytesIO()
    Image.new('RGB', (16, 16), (23, 107, 214)).save(output, 'PNG')
    return base64.b64encode(output.getvalue()).decode('ascii')


class QuarkDriveClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_lists_folders_and_creates_share(self):
        seen_parent_fids = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == '/account/info':
                return httpx.Response(200, json={'status': 200, 'data': {'nickname': '测试用户', 'uid': 'u1'}})
            if path.endswith('/file/sort'):
                seen_parent_fids.append(request.url.params.get('pdir_fid'))
                return httpx.Response(200, json={
                    'status': 200,
                    'data': {'list': [
                        {'fid': 'folder1', 'pdir_fid': '0', 'file_name': '课程资料', 'dir': True},
                        {'fid': 'file1', 'pdir_fid': '0', 'file_name': '说明.txt', 'dir': False},
                    ]},
                    'metadata': {'_total': 2, '_count': 2},
                })
            if path.endswith('/share'):
                return httpx.Response(200, json={'status': 200, 'data': {'task_id': 'task1'}})
            if path.endswith('/task'):
                return httpx.Response(200, json={'status': 200, 'data': {'share_id': 'share1'}})
            if path.endswith('/share/password'):
                return httpx.Response(200, json={
                    'status': 200,
                    'data': {'share_url': 'https://pan.quark.cn/s/abc', 'passcode': '8k2m'},
                })
            return httpx.Response(404, json={'status': 404, 'message': 'not found'})

        transport = httpx.MockTransport(handler)
        async with QuarkDriveClient('sid=test', transport=transport) as client:
            account = await client.validate_account()
            folders = await client.list_entries('chosen-folder', folders_only=True)
            share = await client.create_share(folders[0])

        self.assertEqual(account['nickname'], '测试用户')
        self.assertEqual(seen_parent_fids, ['chosen-folder'])
        self.assertEqual([item.name for item in folders], ['课程资料'])
        self.assertEqual(share['share_url'], 'https://pan.quark.cn/s/abc')
        self.assertEqual(share['passcode'], '8k2m')
        self.assertTrue(share['permanent'])

    async def test_uploads_small_artifact_before_permanent_share(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            path = request.url.path
            if path.endswith('/file/upload/pre'):
                return httpx.Response(200, json={'status': 200, 'data': {
                    'task_id': 'upload-task', 'auth_info': 'auth-info',
                    'upload_id': 'upload-id', 'obj_key': 'objects/product.pdf',
                    'bucket': 'ul-zb', 'callback': {'callbackUrl': 'https://callback.test'},
                }})
            if path.endswith('/file/update/hash'):
                return httpx.Response(200, json={'status': 200, 'data': {}})
            if path.endswith('/file/upload/auth'):
                return httpx.Response(200, json={'status': 200, 'data': {'auth_key': 'signed-key'}})
            if request.url.host == 'ul-zb.pds.quark.cn' and request.method == 'PUT':
                return httpx.Response(200, headers={'etag': 'part-etag'})
            if request.url.host == 'ul-zb.pds.quark.cn' and request.method == 'POST':
                return httpx.Response(200, content=b'<CompleteMultipartUploadResult/>')
            if path.endswith('/file/upload/finish'):
                return httpx.Response(200, json={'status': 200, 'data': {
                    'fid': 'uploaded-fid', 'pdir_fid': 'delivery-folder',
                    'file_name': 'product.pdf', 'dir': False, 'size': 12,
                }})
            return httpx.Response(404, json={'status': 404, 'message': 'not found'})

        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir) / 'product.pdf'
            artifact.write_bytes(b'%PDF-sample')
            async with QuarkDriveClient('sid=test', transport=httpx.MockTransport(handler)) as client:
                entry = await client.upload_file(artifact, parent_fid='delivery-folder')

        self.assertEqual(entry.fid, 'uploaded-fid')
        self.assertEqual(entry.parent_fid, 'delivery-folder')
        self.assertIn(('POST', '/1/clouddrive/file/upload/pre'), calls)
        self.assertIn(('PUT', '/objects/product.pdf'), calls)
        self.assertIn(('POST', '/1/clouddrive/file/upload/finish'), calls)


class CatalogAIClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_generates_listing_and_cover(self):
        image_b64 = _tiny_png_base64()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith('/chat/completions'):
                content = json.dumps({
                    'title': 'Python入门视频课程资料',
                    'description': '数字学习资料，通过网盘交付，购买前请确认需求。',
                    'cover_prompt': '蓝色简洁编程课程封面，无商标无联系方式',
                }, ensure_ascii=False)
                return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
            if request.url.path.endswith('/images/generations'):
                return httpx.Response(200, json={'data': [{'b64_json': image_b64}]})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        folder = QuarkEntry(fid='f1', parent_fid='0', name='Python课程', is_dir=True)
        child = QuarkEntry(fid='c1', parent_fid='f1', name='第一章.mp4', is_dir=False, size=1024)
        async with CatalogAIClient(
            text_base_url='https://example.test/v1', text_api_key='key', text_model='text-model',
            image_base_url='https://example.test/v1', image_api_key='key', image_model='image-model',
            transport=transport,
        ) as client:
            listing = await client.generate_listing(folder, [child])
            cover = await client.generate_cover(listing['cover_prompt'])

        self.assertEqual(listing['title'], 'Python入门视频课程资料')
        self.assertEqual((cover['width'], cover['height']), (16, 16))
        self.assertTrue(cover['content'])

    async def test_accepts_json_from_reasoning_content_when_final_content_is_empty(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={'choices': [{'message': {
                'content': '',
                'reasoning_content': '分析完成。最终结果：\n```json\n{"ok":true,"message":"兼容成功"}\n```',
            }}]})

        async with CatalogAIClient(
            text_base_url='https://example.test/v1', text_api_key='key', text_model='reasoning-model',
            image_base_url='https://example.test/v1', image_api_key='key', image_model='image-model',
            transport=httpx.MockTransport(handler),
        ) as client:
            result = await client.generate_json('只输出 JSON', '测试')

        self.assertTrue(result['ok'])

    async def test_multimodal_request_uses_dedicated_vision_model(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen['host'] = request.url.host
            seen['payload'] = json.loads(request.content.decode('utf-8'))
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}}]})

        async with CatalogAIClient(
            text_base_url='https://text.example/v1', text_api_key='text-key', text_model='text-model',
            vision_base_url='https://vision.example/v1', vision_api_key='vision-key', vision_model='vision-model',
            image_base_url='https://image.example/v1', image_api_key='image-key', image_model='image-model',
            transport=httpx.MockTransport(handler),
        ) as client:
            result = await client.generate_json_with_images(
                '只输出 JSON', '看图', ['data:image/png;base64,' + _tiny_png_base64()]
            )

        self.assertTrue(result['ok'])
        self.assertEqual(seen['host'], 'vision.example')
        self.assertEqual(seen['payload']['model'], 'vision-model')

    async def test_multimodal_timeout_is_reported_as_catalog_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout('provider timed out', request=request)

        async with CatalogAIClient(
            text_base_url='https://text.example/v1', text_api_key='text-key', text_model='text-model',
            vision_base_url='https://vision.example/v1', vision_api_key='vision-key', vision_model='vision-model',
            image_base_url='https://image.example/v1', image_api_key='image-key', image_model='image-model',
            transport=httpx.MockTransport(handler),
        ) as client:
            with self.assertRaisesRegex(RuntimeError, '识图模型请求超时'):
                await client.generate_json_with_images(
                    '只输出 JSON', '看图', ['https://example.test/test.png']
                )

    async def test_generates_with_real_reference_image(self):
        image_b64 = _tiny_png_base64()
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith('/images/edits'):
                self.assertIn('multipart/form-data', request.headers.get('content-type', ''))
                return httpx.Response(200, json={'data': [{'b64_json': image_b64}]})
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / 'primary.png'
            reference.write_bytes(base64.b64decode(image_b64))
            async with CatalogAIClient(
                text_base_url='https://example.test/v1', text_api_key='key', text_model='text-model',
                image_base_url='https://example.test/v1/images/generations',
                image_api_key='key', image_model='image-model', transport=httpx.MockTransport(handler),
            ) as client:
                result = await client.generate_with_references('保持角色外观，生成新场景', [str(reference)])

        self.assertEqual(calls, ['/v1/images/edits'])
        self.assertEqual(result['reference_mode'], 'image_edit')
        self.assertEqual(result['reference_count'], 1)

    async def test_reference_edit_falls_back_to_prompt_generation(self):
        image_b64 = _tiny_png_base64()
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith('/images/edits'):
                return httpx.Response(404, json={'error': {'message': 'edits endpoint missing'}})
            if request.url.path.endswith('/images/generations'):
                return httpx.Response(200, json={'data': [{'b64_json': image_b64}]})
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / 'primary.png'
            reference.write_bytes(base64.b64decode(image_b64))
            async with CatalogAIClient(
                text_base_url='https://example.test/v1', text_api_key='key', text_model='text-model',
                image_base_url='https://example.test/v1/images/generations',
                image_api_key='key', image_model='image-model', transport=httpx.MockTransport(handler),
            ) as client:
                result = await client.generate_with_references('保持角色外观，生成新场景', [str(reference)])

        self.assertEqual(calls.count('/v1/images/edits'), 2)
        self.assertEqual(calls[-1], '/v1/images/generations')
        self.assertEqual(result['reference_mode'], 'prompt_fallback')
        self.assertTrue(result.get('reference_warning'))


class CatalogHelperTests(unittest.TestCase):
    def test_fingerprint_changes_with_children(self):
        folder = QuarkEntry(fid='f1', parent_fid='0', name='资料', is_dir=True)
        first = QuarkEntry(fid='a', parent_fid='f1', name='a.txt', is_dir=False, size=1)
        second = QuarkEntry(fid='a', parent_fid='f1', name='a.txt', is_dir=False, size=2)
        self.assertNotEqual(build_folder_fingerprint(folder, [first]), build_folder_fingerprint(folder, [second]))

    def test_json_and_title_cleanup(self):
        self.assertEqual(parse_json_object('```json\n{"title":"测试"}\n```')['title'], '测试')
        self.assertEqual(parse_chat_message_json({'content': [{'type': 'text', 'text': '{"ok":true}'}]})['ok'], True)
        self.assertEqual(sanitize_title('  好商品\n介绍！！', max_chars=12), '好商品 介绍')

    def test_market_summary_uses_real_hot_samples_and_robust_price(self):
        items = [
            {'item_id': '1', 'title': '课程A', 'price': '¥10', 'want_count': 30},
            {'item_id': '2', 'title': '课程B', 'price': '12.5', 'want_count': 80},
            {'item_id': '3', 'title': '课程C', 'price': '¥15', 'want_count': 20},
            {'item_id': '4', 'title': '课程D', 'price': '¥9999', 'want_count': 1},
            {'item_id': 'mock_x', 'title': '模拟商品', 'price': '100', 'want_count': 999},
        ]
        summary = summarize_market_items('课程', items, sample_size=3, pricing_strategy='competitive')
        self.assertEqual(summary['samples'][0]['title'], '课程B')
        self.assertEqual(summary['sample_count'], 3)
        self.assertLess(summary['recommended_price'], 20)
        self.assertEqual(choose_listing_price(13, summary, 9.9), 13)
        self.assertEqual(choose_listing_price(5000, summary, 9.9), summary['recommended_price'])

    def test_market_price_keeps_real_cent_prices(self):
        items = [
            {'item_id': '1', 'title': '资料A', 'price': '¥0.01', 'want_count': 30},
            {'item_id': '2', 'title': '资料B', 'price': '0.10', 'want_count': 20},
            {'item_id': '3', 'title': '资料C', 'price': '¥0.10', 'want_count': 10},
        ]
        summary = summarize_market_items('资料', items, pricing_strategy='median')
        self.assertEqual(summary['price_min'], 0.01)
        self.assertEqual(summary['price_median'], 0.1)
        self.assertEqual(summary['recommended_price'], 0.1)

    def test_traffic_strategy_does_not_invent_market_recommendation(self):
        items = [{'item_id': '1', 'title': '资料A', 'price': '¥9.90', 'want_count': 1}]
        summary = summarize_market_items('资料', items, pricing_strategy='traffic')
        self.assertIsNone(summary['recommended_price'])


if __name__ == '__main__':
    unittest.main()
