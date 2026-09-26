"""Exercise the installed upstream SDK against an in-memory HTTP transport."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs

from PIL import Image
import requests

from figure_skills.tools.uniparser_client import RecognitionError, recognize_molecules


class RecordedTransport(requests.adapters.BaseAdapter):
    def __init__(self, fail=False):
        self.requests = []
        self.fail = fail

    def send(self, request, **kwargs):
        self.requests.append(request)
        if self.fail:
            raise requests.Timeout('private-service-diagnostic')
        payload = ({'status': 'success', 'token': 'snip-result'} if len(self.requests) == 1 else {
            'status': 'success', 'pages_dict': [[{
                'type': 'molecule', 'bbox': [0.1, 0.2, 0.8, 0.9],
                'page_size': [200, 100], 'esmi': 'CC<sep>', 'smi': 'CC', 'conf': 0.9,
            }]],
        })
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        return response

    def close(self):
        pass


@unittest.skipUnless(importlib.util.find_spec('uniparser_tools'), 'Install the pinned UniParser SDK to run its transport contract tests')
class SdkContractTests(unittest.TestCase):
    def invoke(self, transport):
        from uniparser_tools.api.clients import UniParserClient
        with tempfile.TemporaryDirectory() as folder, requests.Session() as session:
            session.mount('https://', transport)
            client = UniParserClient('https://recognizer.example', 'test-only-key', session=session)
            image = Path(folder) / 'figure.png'
            Image.new('RGB', (200, 100)).save(image)
            return recognize_molecules(str(image), client=client)

    def test_real_sdk_serializes_snip_and_retrieves_same_token(self):
        transport = RecordedTransport()
        result = self.invoke(transport)
        self.assertEqual(result[0]['bbox'], [20, 20, 160, 90])
        self.assertEqual(result[0]['caption'], 'CC<sep>')
        snip, retrieval = transport.requests
        self.assertTrue(snip.url.endswith('/trigger-snip-async'))
        form = parse_qs(snip.body)
        self.assertEqual(form['molecule'], ['1'])
        self.assertEqual(form['padding_snip'], ['False'])
        self.assertEqual(form['textual'], ['False'])
        self.assertEqual(snip.headers['X-API-Key'], 'test-only-key')
        self.assertIn('snip-result', retrieval.body.decode() if isinstance(retrieval.body, bytes) else retrieval.body)

    def test_real_sdk_timeout_becomes_sanitized_recognition_error(self):
        with self.assertRaises(RecognitionError) as error:
            self.invoke(RecordedTransport(fail=True))
        self.assertNotIn('private-service-diagnostic', str(error.exception))


if __name__ == '__main__':
    unittest.main()
