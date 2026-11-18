"""Bounded acceptance with independent read-only observation; no idle keepalive."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation.api import InstaBox
from automation.common import Audit, atomic_json, identifier
from automation.observation import observe

task = identifier("gha-" + os.environ["GITHUB_RUN_ID"])
out = Path("output/cloud").resolve()
out.mkdir(parents=True, exist_ok=True)
finished = threading.Event()


def observer():
    audit = Audit(out / "observation-audit.jsonl", task + "-obs")
    api = InstaBox(os.environ.get("INSTABOX_API_KEY"))
    try:
        for _ in range(60):
            if finished.is_set():
                atomic_json(out / "observation-report.json", {"status": "not_started", "reason": "task_failed_before_ready"})
                return
            machine = api.get()
            if machine["state"] == "running" and machine.get("runtime", {}).get("ready"):
                observe(api, audit, out, authorized=True, seconds=300, stop_event=finished)
                return
            finished.wait(2)
        atomic_json(out / "observation-report.json", {"status": "not_started", "reason": "readiness_deadline"})
    except Exception:
        atomic_json(out / "observation-report.json", {"status": "failed", "reason": "observer_failed"})
    finally:
        api.close()


if os.environ.get("RUNTIME_OBSERVATION_APPROVED") != "true":
    raise SystemExit("observation_not_authorized")
thread = threading.Thread(target=observer, daemon=True)
thread.start()
try:
    run = subprocess.run([sys.executable, "-m", "automation.cli", "cloud", "--task-id", task,
                          "--seconds", "300", "--profile", os.environ.get("TASK_PROFILE", "full-public")], timeout=720)
finally:
    finished.set()
    thread.join(timeout=90)
if thread.is_alive():
    raise SystemExit("observer_join_timeout")
observation = json.loads((out / "observation-report.json").read_text())
if observation.get("status") != "completed" or observation.get("samples", 0) < 1:
    raise SystemExit("observation_acceptance_failed")
print(json.dumps({"task_id": task, "controller_exit": run.returncode}))
raise SystemExit(run.returncode)
