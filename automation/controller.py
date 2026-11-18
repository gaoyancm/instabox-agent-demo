import time
import os
from dataclasses import asdict
from .common import TaskError, atomic_json, instant, utcnow
from .runtime import RuntimeMonitor
from .mail_report import collection_summary


class Controller:
    def __init__(self, api, jobs, audit, notifier, output, *, exclusive=False,
                 maximum_seconds=300, sleep=time.sleep, clock=time.monotonic, now=utcnow):
        if not 10 <= maximum_seconds <= 1800:
            raise TaskError("invalid_task_duration")
        self.api, self.jobs, self.audit, self.notifier = api, jobs, audit, notifier
        self.output, self.exclusive, self.maximum = output, exclusive, maximum_seconds
        self.sleep, self.clock, self.now = sleep, clock, now
        self.monitor = RuntimeMonitor(now=now)
        self.owned = False
        self.baseline = {}
        self.server_info = {}

    def emit(self, event, **fields):
        record = self.audit.emit(event, **fields)
        link = ""
        if os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_RUN_ID"):
            link = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        self.notifier.send(event, {"task_id": self.audit.task_id, "at": record["at"], "run_url": link,
                                  "task_profile": getattr(self.jobs, "profile", "unknown"),
                                  "server_info": self.server_info,
                                  "machine_state": fields.get("machine_state", "unknown"), **fields})
        return record

    def wait_state(self, predicate, seconds=180):
        end = self.clock() + seconds
        for _ in range(int(seconds / 3) + 2):
            machine = self.api.get()
            if predicate(machine):
                return machine
            if self.clock() >= end:
                break
            self.sleep(3)
        raise TaskError("machine_transition_timeout")

    def operation(self, action):
        op = self.api.lifecycle(action, self.audit.task_id)
        for _ in range(12):
            if op["state"] == "succeeded":
                return
            if op["state"] == "failed":
                raise TaskError(op.get("error", {}).get("code", "operation_failed"))
            op = self.api.operation(op["id"])
        raise TaskError("operation_timeout")

    def sample(self):
        machine = self.api.get()
        budget, events = self.monitor.check(machine)
        for event in events:
            self.emit(event, machine_state=machine["state"], **asdict(budget))
        return budget

    def safe_suspend(self):
        machine = self.api.get()
        if machine["state"] != "running":
            self.emit("SUSPEND_SKIPPED", machine_state=machine["state"], reason="platform_already_paused")
            return False
        if not self.owned:
            raise TaskError("machine_not_owned")
        stable = 0
        for sample in range(9):
            inspection = self.jobs.inspect()
            new_processes = {pid: tick for pid, tick in inspection["processes"].items()
                             if self.baseline.get(pid) != tick}
            self.audit.emit("SUSPEND_ACTIVITY_CHECK", sample=sample + 1, job_busy=inspection["busy"],
                            new_processes=new_processes,
                            process_details={pid: inspection.get("process_details", {}).get(pid)
                                             for pid in new_processes})
            if inspection["busy"]:
                raise TaskError("foreign_activity_or_job_active")
            stable = stable + 1 if not new_processes else 0
            if stable >= 2:
                break
            self.sleep(5)
        else:
            raise TaskError("foreign_activity_or_job_active")
        self.operation("suspend")
        self.wait_state(lambda m: m["state"] == "suspended" and m.get("storage", {}).get("synced") is True,
                        seconds=600)
        self.emit("MACHINE_SUSPENDED", machine_state="suspended", storage_synced=True)
        return True

    def run(self):
        report = {"task_id": self.audit.task_id, "status": "failed", "error_code": None,
                  "result_verified": False, "suspended_and_synced": False}
        started = self.clock()
        machine = {}
        try:
            if not self.exclusive:
                raise TaskError("exclusive_machine_authorization_required")
            machine = self.api.get()
            if machine.get("cooldownUntil") and instant(machine["cooldownUntil"]) > self.now():
                raise TaskError("machine_cooldown")
            # Conservative: never adopt a running machine owned by someone else.
            if machine["state"] != "suspended" or machine.get("operationId"):
                raise TaskError("machine_busy_or_unsupported_state")
            if machine.get("storage", {}).get("synced") is not True:
                raise TaskError("storage_not_synced")
            self.audit.emit("RESUME_REQUESTED", prior_state=machine["state"])
            self.operation("resume")
            self.owned = True
            machine = self.wait_state(lambda m: m["state"] == "running" and m.get("runtime", {}).get("ready") is True)
            self.server_info = {"name": machine.get("name", "unknown"),
                                "machine_id": machine.get("id", "unknown"),
                                "ready_at": self.now().isoformat(), "run_started_at": machine.get("runStartedAt"),
                                "max_run_minutes": machine.get("maxRunMinutes")}
            budget = self.sample()
            if budget.remaining < self.maximum:
                raise TaskError("insufficient_runtime_budget")
            release_digest = self.jobs.deploy()
            self.audit.emit("CODE_DEPLOYED", sha256=release_digest)
            inspection = self.jobs.inspect()
            if inspection["busy"]:
                self.owned = False
                raise TaskError("preserved_job_active")
            self.baseline = inspection["processes"]
            self.audit.emit("JOB_OWNERSHIP_BASELINE", processes=self.baseline)
            if hasattr(self.jobs, "prepare_runtime"):
                self.jobs.prepare_runtime()
            if hasattr(self.jobs, "network"):
                try:
                    self.server_info["network"] = self.jobs.network()
                except Exception:
                    self.server_info["network"] = {"errors": ["network_probe_unavailable"]}
            budget = self.sample()
            self.server_info["remaining_seconds"] = budget.remaining
            self.audit.emit("SERVER_READY_INFO", **self.server_info)
            self.emit("TASK_STARTED", machine_state="running", status="running", maximum_seconds=self.maximum)
            if self.sample().remaining < self.maximum:
                raise TaskError("insufficient_runtime_budget")
            self.jobs.start(self.maximum)
            next_monitor = self.clock() + min(300, budget.remaining)
            deadline = self.clock() + self.maximum
            for _ in range(240):
                if self.clock() >= next_monitor:
                    budget = self.sample()
                    next_monitor = self.clock() + min(300, max(1, budget.remaining))
                    if budget.remaining <= 0:
                        raise TaskError("runtime_finishing_window")
                job = self.jobs.status()
                self.audit.emit("JOB_PROGRESS", job_state=job["state"], completed=job.get("completed", 0))
                if job["state"] in {"completed", "failed", "interrupted", "lost"} and not job.get("busy"):
                    break
                if self.clock() >= deadline:
                    raise TaskError("task_deadline")
                self.sleep(15)
            else:
                raise TaskError("poll_limit")
            manifest = self.jobs.collect(self.output)
            report["result_verified"] = True
            self.emit("COLLECTION_COMPLETED", machine_state="running", result_summary=manifest)
            if job["state"] != "completed":
                raise TaskError(job.get("error_code") or "job_interrupted")
            report["status"] = "completed"
        except Exception as exc:
            report["error_code"] = exc.code if isinstance(exc, TaskError) else "controller_failed"
            self.emit("TASK_FAILED", error_code=report["error_code"])
        finally:
            if self.owned:
                try:
                    # Read-only get first: do not exec/file-read a platform-paused machine.
                    machine = self.api.get()
                    if machine["state"] == "running" and self.jobs.release:
                        job = self.jobs.status()
                        if job.get("busy"):
                            self.jobs.stop()
                            for _ in range(20):
                                self.sleep(5)
                                if self.api.get()["state"] != "running":
                                    break
                                if not self.jobs.status().get("busy"):
                                    break
                        if self.api.get()["state"] == "running" and not self.jobs.status().get("busy"):
                            if not report["result_verified"]:
                                try:
                                    self.jobs.collect(self.output)
                                    report["result_verified"] = True
                                except TaskError:
                                    pass
                            report["suspended_and_synced"] = self.safe_suspend()
                    elif machine["state"] != "running":
                        self.audit.emit("PLATFORM_PAUSED", machine_state=machine["state"])
                    else:
                        # No job was launched. No evidence of foreign activity => avoid assuming ownership.
                        self.audit.emit("CLEANUP_REQUIRES_REVIEW", reason="predeployment_failure")
                except Exception as exc:
                    report["cleanup_error"] = exc.code if isinstance(exc, TaskError) else "cleanup_failed"
                    if report["error_code"] is None:
                        report["error_code"] = report["cleanup_error"]
                    self.audit.emit("CLEANUP_FAILED", error_code=report["cleanup_error"])
            if not report["suspended_and_synced"]:
                report["status"] = "failed"
            try:
                machine = self.api.get()
                report["storage_synced"] = machine.get("storage", {}).get("synced")
            except Exception:
                machine = {}
                report["storage_synced"] = None
            report["elapsed_seconds"] = self.clock() - started
            report["machine_state"] = "suspended" if report["suspended_and_synced"] else machine.get("state", "unknown")
            report["server_info"] = self.server_info
            report["task_profile"] = getattr(self.jobs, "profile", "unknown")
            if report["result_verified"] and (self.output / "results.manifest.json").exists():
                try:
                    report.update(collection_summary(self.output))
                except (OSError, ValueError, KeyError, TypeError):
                    report["preview_note"] = "内容摘要不可用 / Content preview unavailable"
            self.emit("FINAL_REPORT", **{k: v for k, v in report.items() if k != "task_id"})
            atomic_json(self.output / "final-report.json", report)
        return report
