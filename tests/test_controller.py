from pathlib import Path
import tempfile
import unittest
from automation.common import Audit, TaskError
from automation.controller import Controller
from automation.notify import Notifier
from test_runtime_api import machine, NOW


class FakeAPI:
    def __init__(self):
        self.value = {**machine(), "state": "suspended"}
        self.calls = []
    def get(self):
        return self.value
    def lifecycle(self, action, task):
        self.calls.append(action)
        self.value = {**self.value, "state": "running" if action == "resume" else "suspended"}
        return {"state": "succeeded", "id": "op_test"}


class FakeJobs:
    release = "/data/test"
    def __init__(self):
        self.active = False
        self.foreign = False
        self.hash_bad = False
        self.started = False
        self.inspections = 0
    def deploy(self):
        return "digest"
    def inspect(self):
        self.inspections += 1
        return {"busy": self.active, "processes": {"1": "100", **({"999": "200"} if self.foreign and self.inspections > 1 else {})}}
    def start(self, seconds):
        self.started = True
    def status(self):
        return {"state": "completed", "busy": self.active}
    def stop(self):
        self.active = False
    def collect(self, destination):
        if self.hash_bad:
            raise TaskError("result_hash_mismatch")
        return {"count": 2}


class ControllerTests(unittest.TestCase):
    def run_case(self, api=None, jobs=None, exclusive=True):
        api, jobs = api or FakeAPI(), jobs or FakeJobs()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            audit = Audit(out / "audit.jsonl", "test")
            report = Controller(api, jobs, audit, Notifier(audit), out, exclusive=exclusive,
                                sleep=lambda _: None, now=lambda: NOW).run()
        return report, api, jobs

    def test_complete_short_cycle(self):
        report, api, jobs = self.run_case()
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["result_verified"])
        self.assertTrue(report["suspended_and_synced"])
        self.assertEqual(api.calls, ["resume", "suspend"])

    def test_running_machine_not_adopted(self):
        api = FakeAPI()
        api.value["state"] = "running"
        report, api, jobs = self.run_case(api)
        self.assertEqual(report["error_code"], "machine_busy_or_unsupported_state")
        self.assertEqual(api.calls, [])

    def test_unsynced_storage_does_not_resume(self):
        api = FakeAPI()
        api.value["storage"] = {"synced": False}
        report, api, jobs = self.run_case(api)
        self.assertEqual(report["error_code"], "storage_not_synced")
        self.assertEqual(api.calls, [])
        self.assertFalse(jobs.started)

    def test_sync_timeout_keeps_suspended_state_and_fails(self):
        api = FakeAPI()
        lifecycle = api.lifecycle
        def operation(action, task):
            result = lifecycle(action, task)
            if action == "suspend":
                api.value["storage"] = {"synced": False}
            return result
        api.lifecycle = operation
        report, api, _ = self.run_case(api)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error_code"], "machine_transition_timeout")
        self.assertEqual(report["machine_state"], "suspended")
        self.assertFalse(report["storage_synced"])
        self.assertEqual(api.calls, ["resume", "suspend"])

    def test_delayed_storage_sync_waits_without_another_resume(self):
        api, jobs = FakeAPI(), FakeJobs()
        clock = [0]
        suspended_at = [None]
        lifecycle, get = api.lifecycle, api.get
        def operation(action, task):
            result = lifecycle(action, task)
            if action == "suspend":
                suspended_at[0] = clock[0]
                api.value["storage"] = {"synced": False}
            return result
        def delayed_get():
            if suspended_at[0] is not None and clock[0] - suspended_at[0] >= 495:
                api.value["storage"] = {"synced": True}
            return get()
        api.lifecycle, api.get = operation, delayed_get
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            audit = Audit(out / "audit.jsonl", "delayed-sync")
            report = Controller(api, jobs, audit, Notifier(audit), out, exclusive=True,
                clock=lambda: clock[0], now=lambda: NOW,
                sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)).run()
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["suspended_and_synced"])
        self.assertTrue(report["storage_synced"])
        self.assertEqual(api.calls, ["resume", "suspend"])

    def test_no_authorization_no_resume(self):
        report, api, jobs = self.run_case(exclusive=False)
        self.assertEqual(api.calls, [])
        self.assertFalse(jobs.started)

    def test_cooldown_does_not_resume(self):
        api = FakeAPI()
        api.value["cooldownUntil"] = "2026-11-12T09:00:00Z"
        report, api, _ = self.run_case(api)
        self.assertEqual(report["error_code"], "machine_cooldown")
        self.assertEqual(api.calls, [])

    def test_foreign_activity_no_suspend(self):
        jobs = FakeJobs()
        jobs.foreign = True
        report, api, _ = self.run_case(jobs=jobs)
        self.assertEqual(report["cleanup_error"], "foreign_activity_or_job_active")
        self.assertNotIn("suspend", api.calls)

    def test_transient_process_must_disappear_before_suspend(self):
        jobs = FakeJobs()
        samples = [0]
        def inspect():
            samples[0] += 1
            processes = {"1": "100"}
            if samples[0] == 2:
                processes["999"] = "200"
            return {"busy": False, "processes": processes}
        jobs.inspect = inspect
        report, api, _ = self.run_case(jobs=jobs)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(samples[0], 4)
        self.assertEqual(api.calls, ["resume", "suspend"])

    def test_preserved_job_not_interrupted(self):
        jobs = FakeJobs()
        jobs.active = True
        report, api, _ = self.run_case(jobs=jobs)
        self.assertEqual(report["error_code"], "preserved_job_active")
        self.assertTrue(jobs.active)
        self.assertNotIn("suspend", api.calls)

    def test_bad_hash_not_success(self):
        jobs = FakeJobs()
        jobs.hash_bad = True
        report, _, _ = self.run_case(jobs=jobs)
        self.assertFalse(report["result_verified"])
        self.assertEqual(report["status"], "failed")

    def test_early_pause_no_resume_loop(self):
        api, jobs = FakeAPI(), FakeJobs()
        def start(seconds):
            api.value["state"] = "suspended"
        jobs.start = start
        def status():
            raise TaskError("machine_not_running")
        jobs.status = status
        report, api, _ = self.run_case(api, jobs)
        self.assertEqual(api.calls.count("resume"), 1)
        self.assertFalse(report["suspended_and_synced"])

    def test_network_disconnect_cannot_report_success(self):
        jobs = FakeJobs()
        jobs.status = lambda: (_ for _ in ()).throw(TaskError("network_outcome_unknown"))
        report, api, _ = self.run_case(jobs=jobs)
        self.assertEqual(report["error_code"], "network_outcome_unknown")
        self.assertEqual(report["status"], "failed")
        self.assertNotIn("suspend", api.calls)

    def test_shortened_budget_stops_owned_job(self):
        from datetime import timedelta
        clock = [0]
        api, jobs = FakeAPI(), FakeJobs()
        original_get = api.get
        def get():
            value = original_get()
            if clock[0] >= 300:
                value["maxRunMinutes"] = 5
            return value
        api.get = get
        jobs.status = lambda: {"state": "running" if jobs.active else "interrupted", "busy": jobs.active}
        jobs.start = lambda seconds: setattr(jobs, "active", True)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            audit = Audit(out / "audit.jsonl", "shorten")
            report = Controller(api, jobs, audit, Notifier(audit), out, exclusive=True,
                maximum_seconds=900, clock=lambda: clock[0], now=lambda: NOW + timedelta(seconds=clock[0]),
                sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)).run()
            self.assertIn("RUNTIME_LIMIT_CHANGED", audit.path.read_text())
        self.assertEqual(report["error_code"], "runtime_finishing_window")
        self.assertFalse(jobs.active)
        self.assertTrue(report["suspended_and_synced"])
