from pathlib import Path
import tempfile
import unittest
from automation.common import Audit, TaskError
from automation.observation import observe
from automation.runtime import RuntimeMonitor
from test_runtime_api import machine, NOW


class ObservationTests(unittest.TestCase):
    def test_controlled_task_finish_stops_without_resuming(self):
        class Stop:
            stopped = False
            def is_set(self):
                return self.stopped
            def wait(self, seconds):
                self.stopped = True
        stop = Stop()
        class API:
            calls = 0
            def get(self):
                self.calls += 1
                return {**machine(), "free": True, "state": "suspended" if stop.is_set() else "running"}
        api = API()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            result = observe(api, Audit(out / 'audit.jsonl', 'obs'), out, authorized=True,
                             seconds=300, stop_event=stop, monitor=RuntimeMonitor(now=lambda: NOW))
        self.assertEqual(api.calls, 2)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['reason'], 'controlled_task_finished')
        self.assertEqual(result['final_machine_state'], 'suspended')

    def test_default_denied(self):
        with self.assertRaisesRegex(TaskError, "not_authorized"):
            observe(None, None, None)

    def test_bounded_readonly_and_free_cost_gate(self):
        clock = [0]
        class API:
            calls = 0
            def get(self):
                self.calls += 1
                return {**machine(), "free": True}
        api = API()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            result = observe(api, Audit(out / "audit.jsonl", "obs"), out, authorized=True,
                             seconds=600, clock=lambda: clock[0],
                             sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
                             monitor=RuntimeMonitor(now=lambda: NOW))
        self.assertEqual(api.calls, 3)
        self.assertEqual(result["lifecycle_calls"], 0)
        self.assertEqual(result["cost_cap_usd"], 0)
        self.assertEqual(clock[0], 600)

    def test_cost_duration_policy(self):
        for kwargs in [{"seconds": 99999}, {"cost_cap_usd": 1}]:
            with self.assertRaisesRegex(TaskError, "policy_invalid"):
                observe(None, None, None, authorized=True, **kwargs)
