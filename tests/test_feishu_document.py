import json
import tempfile
import unittest
from pathlib import Path

import httpx

from utils.feishu_document import FeishuDocumentClient


class FeishuDocumentClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_imports_docx_and_returns_document_url(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path.endswith('/tenant_access_token/internal'):
                return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'tenant-token'})
            if request.url.path.endswith('/medias/upload_all'):
                return httpx.Response(200, json={'code': 0, 'data': {'file_token': 'box-token'}})
            if request.url.path.endswith('/import_tasks'):
                body = json.loads(request.content.decode())
                self.assertEqual(body['file_extension'], 'docx')
                self.assertEqual(body['type'], 'docx')
                return httpx.Response(200, json={'code': 0, 'data': {'ticket': 'ticket-1'}})
            if request.url.path.endswith('/import_tasks/ticket-1'):
                return httpx.Response(200, json={'code': 0, 'data': {'result': {
                    'job_status': 0, 'token': 'doc-token', 'type': 'docx',
                    'url': 'https://example.feishu.cn/docx/doc-token',
                }}})
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'product.docx'
            path.write_bytes(b'PK\x03\x04-test-docx')
            async with FeishuDocumentClient(
                'app-id', 'secret', transport=httpx.MockTransport(handler)
            ) as client:
                result = await client.import_document(path, document_name='测试文档')
        self.assertEqual(result['url'], 'https://example.feishu.cn/docx/doc-token')
        self.assertIn('/open-apis/drive/v1/medias/upload_all', seen)


if __name__ == '__main__':
    unittest.main()
