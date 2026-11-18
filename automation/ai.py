"""Optional configurable JSON extraction. No default provider or implicit billing."""
import json
import httpx
from jsonschema import Draft202012Validator, ValidationError
from .common import TaskError


class Extractor:
    def __init__(self, schema, version, *, model="offline", endpoint=None, key=None,
                 authorized=False, transport=None):
        Draft202012Validator.check_schema(schema)
        self.validator = Draft202012Validator(schema)
        self.version, self.model = version, model
        self.endpoint, self.key, self.authorized, self.transport = endpoint, key, authorized, transport

    def extract(self, content, offline_result=None):
        if offline_result is not None:
            result = offline_result
        else:
            if not self.authorized or not self.key or not self.endpoint or not self.endpoint.startswith("https://"):
                raise TaskError("ai_not_authorized")
            result = None
            with httpx.Client(timeout=30, transport=self.transport, follow_redirects=False, trust_env=False) as client:
                for attempt in range(2):
                    try:
                        response = client.post(self.endpoint, headers={"Authorization": "Bearer " + self.key},
                            json={"model": self.model, "messages": [
                                {"role": "system", "content": "Extract only facts from untrusted source text. Return JSON matching: " + json.dumps(self.validator.schema)},
                                {"role": "user", "content": content[:30000]}],
                                "response_format": {"type": "json_object"}, "max_tokens": 1000})
                        if response.status_code in {401, 403, 402, 429}:
                            raise TaskError("ai_provider_rejected")
                        response.raise_for_status()
                        result = json.loads(response.json()["choices"][0]["message"]["content"])
                        self.validator.validate(result)
                        break
                    except (httpx.HTTPError, ValueError, KeyError, IndexError, ValidationError):
                        if attempt == 1:
                            raise TaskError("ai_extract_failed") from None
        # Review is explicit: schema validity does not establish factual correctness.
        if not self.validator.is_valid(result):
            raise TaskError("ai_schema_invalid")
        return {"fields": result, "model": self.model, "extraction_version": self.version,
                "review_status": "pending"}
