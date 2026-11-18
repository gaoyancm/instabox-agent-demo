from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import unittest
import httpx
from automation.api import InstaBox
from automation.common import TaskError
from automation.runtime import RuntimeMonitor

NOW = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)


def machine(limit=120, elapsed=0, **extra):
    return dict(state="running", runtime={"ready": True}, maxRunMinutes=limit,
                runStartedAt=(NOW - timedelta(seconds=elapsed)).isoformat(), cooldownUntil=None,
                operationId=None, storage={"synced": True}, **extra)


class RuntimeTests(unittest.TestCase):
    def test_increases_120_240_480(self):
        monitor = RuntimeMonitor(now=lambda: NOW)
        for limit in [120, 240, 480]:
            budget, events = monitor.check(machine(limit, 100))
            self.assertEqual(budget.remaining, limit * 60 - 700)
            if limit != 120:
                self.assertIn("RUNTIME_LIMIT_CHANGED", events)

    def test_sudden_shortening(self):
        monitor = RuntimeMonitor(now=lambda: NOW)
        monitor.check(machine(480, 7100))
        budget, events = monitor.check(machine(120, 7100))
        self.assertLess(budget.remaining, 0)
        self.assertIn("RUNTIME_LIMIT_CHANGED", events)

    def test_120_checkpoint_once(self):
        monitor = RuntimeMonitor(now=lambda: NOW)
        self.assertIn("RUNTIME_120_MINUTE_CHECK", monitor.check(machine(240, 7200))[1])
        self.assertNotIn("RUNTIME_120_MINUTE_CHECK", monitor.check(machine(240, 7200))[1])

    def test_no_fixed_budget_or_missing_fields(self):
        for field in ["maxRunMinutes", "runStartedAt"]:
            value = machine()
            value.pop(field)
            with self.assertRaises(TaskError):
                RuntimeMonitor(now=lambda: NOW).check(value)

    def test_pause_cooldown_cycle_clock(self):
        for mutation in [{"state": "suspended"}, {"cooldownUntil": (NOW + timedelta(minutes=5)).isoformat()},
                         {"runStartedAt": (NOW + timedelta(minutes=5)).isoformat()}, {"maxRunMinutes": 0}]:
            with self.subTest(mutation=mutation), self.assertRaises(TaskError):
                RuntimeMonitor(now=lambda: NOW).check({**machine(), **mutation})
        monitor = RuntimeMonitor(now=lambda: NOW)
        monitor.check(machine())
        with self.assertRaisesRegex(TaskError, "cycle_changed"):
            monitor.check(machine(elapsed=1))


class APITests(unittest.TestCase):
    def test_contract_matches_official_schema(self):
        spec = json.loads((Path(__file__).resolve().parent / "contracts/instabox-openapi.json").read_text())
        from jsonschema import validate
        requests = []
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"state": "running"})
            requests.append(request)
            if request.url.path.endswith("exec"):
                validate(json.loads(request.content), spec["components"]["schemas"]["ExecRequest"])
                return httpx.Response(200, json={"stdout": "ok", "exitCode": 0})
            return httpx.Response(202, json={"id": "op_1", "state": "succeeded"})
        api = InstaBox("test-not-a-secret", httpx.MockTransport(handler))
        self.addCleanup(api.close)
        self.assertEqual(api.exec(["printf", "%s", "$X"]), "ok")
        api.lifecycle("resume", "task1")
        self.assertNotIn("Idempotency-Key", requests[0].headers)
        self.assertEqual(requests[1].headers["Idempotency-Key"], "task1-resume")
        self.assertEqual(json.loads(requests[1].content), {})

    def test_safe_get_retry_bounded(self):
        calls, waits = [], []
        def handler(request):
            calls.append(request)
            return httpx.Response(503, json={"error": {"code": "host_unavailable", "retryable": True}})
        api = InstaBox("test", httpx.MockTransport(handler), sleep=waits.append)
        self.addCleanup(api.close)
        with self.assertRaises(TaskError):
            api.get()
        self.assertEqual(len(calls), 3)
        self.assertEqual(waits, [1, 2])

    def test_exec_never_retries_unknown_outcome(self):
        calls = []
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"state": "running"})
            calls.append(request)
            raise httpx.ReadTimeout("untrusted-secret-error")
        api = InstaBox("test", httpx.MockTransport(handler))
        self.addCleanup(api.close)
        with self.assertRaisesRegex(TaskError, "network_outcome_unknown"):
            api.exec(["true"])
        self.assertEqual(len(calls), 1)

    def test_cooldown_not_retried(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(409, json={"error": {"code": "machine_cooldown", "retryable": True}})
        api = InstaBox("test", httpx.MockTransport(handler))
        self.addCleanup(api.close)
        with self.assertRaisesRegex(TaskError, "machine_cooldown"):
            api.lifecycle("resume", "task1")
        self.assertEqual(len(calls), 1)

    def test_wrong_lifecycle_rejected(self):
        api = InstaBox("test")
        self.addCleanup(api.close)
        with self.assertRaises(TaskError):
            api.lifecycle("start", "task1")
