import argparse
import os
import signal
from pathlib import Path
import sys
from .api import InstaBox
from .common import Audit, TaskError, identifier, atomic_json, utcnow
from .controller import Controller
from .jobs import JobRunner
from .notify import Notifier, SMTPTransport
from .mail_report import notification_events
from .observation import observe


def main():
    def interrupted(*_):
        raise TaskError("controller_interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["cloud", "runtime_observation"])
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--output", default="output/cloud")
    parser.add_argument("--profile", choices=["basic", "full", "full-public", "collection", "public"], default="full")
    args = parser.parse_args()
    task = identifier(args.task_id)
    out = Path(args.output).resolve()
    audit = Audit(out / "audit.jsonl", task)
    if args.mode == "runtime_observation":
        if os.environ.get("RUNTIME_OBSERVATION_APPROVED") != "true":
            audit.emit("OBSERVATION_GATE_BLOCKED", reason="separate_acceptance_required")
            raise TaskError("runtime_observation_not_authorized")
        api = InstaBox(os.environ.get("INSTABOX_API_KEY"))
        try:
            result = observe(api, audit, out, authorized=True, seconds=args.seconds)
            return 0 if result["status"] == "completed" else 1
        finally:
            api.close()
    if os.environ.get("CLOUD_EXECUTION_APPROVED") != "true":
        raise TaskError("cloud_execution_not_approved")
    transport = None
    events = []
    if os.environ.get("MAIL_APPROVED") == "true":
        names = ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_FROM", "MAIL_TO"]
        if any(not os.environ.get(name) for name in names):
            raise TaskError("mail_config_incomplete")
        transport = SMTPTransport(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")),
                                  os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"],
                                  os.environ["MAIL_FROM"], os.environ["MAIL_TO"], result_dir=out)
        events = notification_events(os.environ.get("MAIL_MODE", "observe"),
                                    filter(None, os.environ.get("MAIL_EVENTS", "").split(",")))
    notifier = Notifier(audit, transport, events)
    api = None
    try:
        api = InstaBox(os.environ.get("INSTABOX_API_KEY"))
        jobs = JobRunner(api, task, Path(__file__).resolve().parent.parent, profile=args.profile)
        report = Controller(api, jobs, audit, notifier, out,
                            exclusive=os.environ.get("EXCLUSIVE_MACHINE_APPROVED") == "true",
                            maximum_seconds=args.seconds).run()
        return 0 if report["status"] == "completed" else 1
    except TaskError as exc:
        # Preflight can fail before a Controller exists (e.g. no sources/dependencies/key).
        # Still send the single final notification; do not wake/query a machine to fill fields.
        report = {"task_id": task, "task_profile": args.profile, "status": "failed",
                  "error_code": exc.code, "machine_state": "unknown", "server_info": {},
                  "result_verified": False, "suspended_and_synced": False, "storage_synced": None}
        audit.emit("PREFLIGHT_FAILED", error_code=exc.code)
        link = ""
        if os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_RUN_ID"):
            link = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        notifier.send("FINAL_REPORT", {**report, "at": utcnow().isoformat(), "run_url": link})
        atomic_json(out / "final-report.json", report)
        return 1
    finally:
        if api is not None:
            api.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TaskError as exc:
        print(exc.code, file=sys.stderr)
        sys.exit(1)
