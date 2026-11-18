"""Read-only bounded observation of an already running, independently authorized cycle.

Does not resume, exec, maintain activity, change settings, or suspend another workload.
Only Free machines are eligible, so no chargeable compute is initiated by this mode.
"""
import time
from .common import TaskError, atomic_json
from .runtime import RuntimeMonitor


def observe(api, audit, output, *, authorized=False, seconds=900, cost_cap_usd=0,
            sleep=time.sleep, clock=time.monotonic, monitor=None, stop_event=None):
    if not authorized:
        raise TaskError("runtime_observation_not_authorized")
    if not 300 <= seconds <= 1800 or cost_cap_usd != 0:
        raise TaskError("observation_policy_invalid")
    monitor = monitor or RuntimeMonitor()
    started = clock()
    report = {"mode": "runtime_observation", "maximum_seconds": seconds,
              "cost_cap_usd": 0, "status": "stopped", "reason": "duration", "samples": 0,
              "lifecycle_calls": 0, "activity_leases": 0}
    try:
        for _ in range(7):
            machine = api.get()
            if stop_event is not None and stop_event.is_set():
                audit.emit("OBSERVATION_CONTROLLED_TASK_FINISHED", machine_state=machine["state"])
                report["reason"] = "controlled_task_finished"
                report["final_machine_state"] = machine["state"]
                break
            if machine.get("free") is not True:
                raise TaskError("observation_free_only")
            budget, events = monitor.check(machine)
            for event in events:
                audit.emit(event, machine_state=machine["state"], limit=budget.limit,
                           elapsed=budget.elapsed, remaining=budget.remaining)
            report["samples"] += 1
            if budget.remaining <= 300:
                report["reason"] = "finishing_window"
                break
            remaining = seconds - (clock() - started)
            if remaining <= 0:
                break
            delay = min(300, remaining, budget.remaining)
            if stop_event is not None:
                stop_event.wait(delay)
            else:
                sleep(delay)
        report["status"] = "completed"
    except TaskError as exc:
        report["reason"] = exc.code
    finally:
        audit.emit("OBSERVATION_FINISHED", **report)
        atomic_json(output / "observation-report.json", report)
    return report
