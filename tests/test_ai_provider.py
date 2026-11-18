import unittest
import httpx
from automation.ai import Extractor
from automation.common import TaskError


class ProviderContractTests(unittest.TestCase):
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}

    def test_invalid_provider_schema_retries_then_preserves_review_gate(self):
        calls = []
        def provider(request):
            calls.append(request)
            answer = '{"value":"42"}' if len(calls) == 1 else '{"value":42}'
            return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})
        adapter = Extractor(self.schema, 'provider-contract-v1', model='test-provider',
                            endpoint='https://provider.invalid/chat', key='offline-test-value', authorized=True,
                            transport=httpx.MockTransport(provider))
        result = adapter.extract('Source fact: value is 42')
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['fields'], {'value': 42})
        self.assertEqual(result['review_status'], 'pending')
        self.assertEqual(result['model'], 'test-provider')

    def test_provider_credential_failure_does_not_retry_or_expose_body(self):
        calls = []
        def provider(request):
            calls.append(request)
            return httpx.Response(401, text='sensitive-provider-diagnostic')
        adapter = Extractor(self.schema, 'v1', model='test-provider', endpoint='https://provider.invalid/chat',
                            key='offline-test-value', authorized=True, transport=httpx.MockTransport(provider))
        with self.assertRaisesRegex(TaskError, 'ai_provider_rejected') as error:
            adapter.extract('42')
        self.assertNotIn('sensitive-provider-diagnostic', str(error.exception))
        self.assertEqual(len(calls), 1)
