"""Offline private probe contracts; no external HTTP, NATS or Gemini calls."""
import base64
import hashlib
import hmac
import unittest

from probe_worker_llm import headers, request_body, result_summary, read_bounded
from core.cluster import llm_protocol as wire


class ProbeTests(unittest.TestCase):
    def test_existing_gateway_hmac_contract(self):
        result = headers('fixture-only-key', 123)
        digest = hmac.new(b'fixture-only-key', b'xiaozhi-llm-probe|02:00:00:00:00:01|123', hashlib.sha256).digest()
        self.assertEqual(result['authorization'], 'Bearer ' + base64.urlsafe_b64encode(digest).decode().rstrip('=') + '.123')
        self.assertNotIn('fixture-only-key', str(result))

    def test_body_is_bounded_and_cannot_select_provider(self):
        value = wire.decode(request_body(8, 'Hello', 30), 8192)
        self.assertEqual(set(value), {'revision', 'dialogue', 'timeout_seconds'})
        for args in ((True, 'Hello', 30), (8, '', 30), (8, 'x' * 8192, 30), (8, 'Hello', 31)):
            with self.assertRaises(ValueError):
                request_body(*args)

    def test_safe_result_summary_validates_revision_and_defaults_to_no_text(self):
        request = {'request_id': 'a' * 32, 'revision': 8}
        data = wire.response(request, 'deskb2x', text='LLM OK')
        result = result_summary(data, 8, 1.23456, expected='LLM OK')
        self.assertTrue(result['expected_text_found'])
        self.assertNotIn('text', result)
        self.assertEqual(result_summary(data, 8, 1, show_text=True)['text'], 'LLM OK')
        with self.assertRaises(ValueError):
            result_summary(data, 9, 1)

    def test_error_output_is_fixed_and_contains_no_response_body(self):
        data = wire.response({'request_id': 'a' * 32, 'revision': 8}, 'deskb2x', error='llm_expired')
        result = result_summary(data, 8, 1)
        self.assertEqual(result['error'], 'llm_expired')
        self.assertNotIn('text', result)


class ReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_json_is_read_until_eof_with_a_hard_bound(self):
        class Stream:
            def __init__(self, parts):
                self.parts = iter(parts)
            async def read(self, limit):
                return next(self.parts, b'')
        self.assertEqual(await read_bounded(Stream([b'{', b'"ok":true', b'}']), 16), b'{"ok":true}')
        with self.assertRaises(ValueError):
            await read_bounded(Stream([b'x' * 9, b'x' * 9]), 16)


if __name__ == '__main__':
    unittest.main()
