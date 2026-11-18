"""InstaBox REST client for one pre-provisioned machine. No create/delete/update methods."""
import os
import time
import httpx
from .common import TaskError

# The target machine is configured per deployment; no machine ID is hard-coded.
MACHINE_ENV = "INSTABOX_MACHINE_ID"


class InstaBox:
    def __init__(self, key, transport=None, sleep=time.sleep, machine_id=None):
        if not key:
            raise TaskError("missing_instabox_key")
        machine_id = machine_id or os.environ.get(MACHINE_ENV, "m_example_machine")
        self.http = httpx.Client(base_url="https://api.instabox.sh", headers={"X-API-Key": key},
                                 timeout=80, transport=transport, follow_redirects=False, trust_env=False)
        self.sleep = sleep
        self.prefix = f"/v1/machines/{machine_id}"

    def request(self, method, path, *, safe=False, key=None, **kwargs):
        headers = kwargs.pop("headers", {})
        if key:
            headers["Idempotency-Key"] = key
        attempts = 3 if safe or key else 1
        for attempt in range(attempts):
            try:
                response = self.http.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError:
                if attempt + 1 == attempts:
                    raise TaskError("network_outcome_unknown") from None
                self.sleep(2 ** attempt)
                continue
            if response.is_success:
                return response
            try:
                err = response.json().get("error", {})
                code = err.get("code", "api_error")
            except (ValueError, AttributeError):
                raise TaskError("api_protocol_error") from None
            # Never retry a cooldown, credentials, billing, or ambiguous exec.
            if code == "machine_cooldown":
                raise TaskError(code)
            delay = err.get("retryAfterSeconds", response.headers.get("Retry-After", 2 ** attempt))
            try:
                delay = float(delay)
            except (TypeError, ValueError):
                raise TaskError("invalid_retry_after") from None
            if not err.get("retryable") or attempt + 1 == attempts or delay > 60:
                raise TaskError(code)
            self.sleep(max(0, delay))
        raise TaskError("retry_exhausted")

    def get(self):
        return self.request("GET", self.prefix, safe=True).json()

    def lifecycle(self, action, task_id):
        if action not in {"resume", "suspend"}:
            raise TaskError("forbidden_lifecycle")
        return self.request("POST", self.prefix + "/" + action, key=f"{task_id}-{action}", json={}).json()

    def operation(self, op_id):
        return self.request("GET", f"/v1/operations/{op_id}", safe=True, params={"wait": "25s"}).json()

    def exec(self, argv, stdin=None):
        if self.get().get("state") != "running":
            raise TaskError("machine_not_running")
        # No generic retry: exec has no platform idempotency support.
        body = {"argv": argv, "timeoutSeconds": 60, "noHistory": True}
        if stdin is not None:
            body["stdin"] = stdin
        result = self.request("POST", self.prefix + "/exec", json=body).json()
        if result.get("exitCode") != 0 or result.get("truncated") or result.get("timedOut"):
            raise TaskError("remote_control_failed")
        return result["stdout"]

    def upload(self, path, data):
        if self.get().get("state") != "running":
            raise TaskError("machine_not_running")
        self.request("PUT", self.prefix + "/files", safe=True,
                     params={"path": path, "parents": "true", "mode": "0600"},
                     content=data, headers={"Content-Type": "application/octet-stream"})

    def download(self, path):
        if self.get().get("state") != "running":
            raise TaskError("machine_not_running")
        return self.request("GET", self.prefix + "/files/content", safe=True, params={"path": path}).content

    def close(self):
        self.http.close()
