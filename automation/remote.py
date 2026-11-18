"""Linux controlled background jobs, equivalent to CLI background-over-exec.

No model credentials needed. Lock descriptor is inherited by the worker across exec.
All state lives under /data. Never kill arbitrary PIDs or steal stale locks.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import shutil
import sys
import threading
import time
from .collect import HTTPCollector
from .browser import BrowserCollector
from .common import TaskError, atomic_json, identifier, instant, utcnow
from .storage import Store
from .fixtures import fixture_server

ROOT = Path(os.environ.get("AUTOMATION_DATA", "/data/instabox-agent-demo"))


def jobdir(task):
    return ROOT / "jobs" / identifier(task)


def process_snapshot():
    snapshot = {}
    own_group = os.getpgrp()
    ancestors = {os.getpid()}
    parent = os.getppid()
    while parent > 1:
        ancestors.add(parent)
        try:
            text = Path(f"/proc/{parent}/stat").read_text()
            parent = int(text[text.rfind(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            break
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            text = path.read_text()
            tail = text[text.rfind(")") + 2:].split()
            # PID plus kernel start ticks prevents PID-reuse confusion. No command arguments logged.
            if (path.parent.stat().st_uid == os.getuid() and int(path.parent.name) not in ancestors
                    and int(tail[2]) != own_group and tail[0] != "Z"):
                snapshot[path.parent.name] = tail[19]
        except (OSError, IndexError, ValueError):
            continue
    return snapshot


def process_details(snapshot):
    details = {}
    for pid in snapshot:
        try:
            path = Path("/proc") / pid
            text = (path / "stat").read_text()
            fields = text[text.rfind(")") + 2:].split()
            details[pid] = {"comm": (path / "comm").read_text().strip(), "state": fields[0],
                            "parent": fields[1], "process_group": fields[2], "session": fields[3]}
        except (OSError, IndexError):
            continue
    return details


def status(task):
    p = jobdir(task) / "status.json"
    return json.loads(p.read_text()) if p.exists() else {"task_id": task, "state": "absent"}


def busy():
    import fcntl
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "machine.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def start(task, seconds, release, profile="basic"):
    import fcntl
    if not 10 <= seconds <= 1800:
        raise TaskError("invalid_job_duration")
    ROOT.mkdir(parents=True, exist_ok=True)
    handle = (ROOT / "machine.lock").open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise TaskError("machine_busy") from None
    directory = jobdir(task)
    directory.mkdir(parents=True, exist_ok=True)
    prior = status(task)
    if prior["state"] != "absent":
        if not (directory / "identity.json").exists():
            handle.close()
            raise TaskError("task_identity_missing")
        identity = json.loads((directory / "identity.json").read_text())
        if identity["release"] != str(release) or identity.get("profile", "basic") != profile:
            handle.close()
            raise TaskError("task_release_conflict")
    if prior["state"] == "completed":
        handle.close()
        return prior
    (directory / "stop").unlink(missing_ok=True)
    atomic_json(directory / "status.json", {"task_id": task, "state": "starting"})
    with (directory / "job.log").open("ab") as log:
        timeout = shutil.which("timeout")
        if not timeout:
            handle.close()
            raise TaskError("controlled_job_timeout_unavailable")
        process = subprocess.Popen([timeout, "--signal=TERM", "--kill-after=30s", str(seconds),
                                    sys.executable, "-m", "automation.remote", "worker", task,
                                    "--seconds", str(seconds), "--lock-fd", str(handle.fileno())],
                                   cwd=release, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True, pass_fds=(handle.fileno(),),
                                   env={**os.environ, "AUTOMATION_PROFILE": profile})
    # Reap the child if the control process stays alive (e.g. Linux integration tests).
    # The daemon thread does not prevent the short-lived remote control process from exiting.
    threading.Thread(target=process.wait, daemon=True).start()
    atomic_json(directory / "identity.json", {"pid": process.pid, "task_id": task,
                                             "release": str(release), "profile": profile,
                                             "started_at": utcnow().isoformat()})
    handle.close()
    return {"task_id": task, "job_id": task, "pid": process.pid, "state": "starting"}


def worker(task, seconds, lock_fd):
    # Keep this descriptor alive until every resource is closed.
    lock = os.fdopen(lock_fd, "a")
    directory = jobdir(task)
    stop = directory / "stop"
    signal.signal(signal.SIGTERM, lambda *_: stop.touch())
    signal.signal(signal.SIGINT, lambda *_: stop.touch())
    server = None
    store = None
    state, error, completed = "failed", None, 0
    deadline = time.monotonic() + seconds
    try:
        atomic_json(directory / "status.json", {"task_id": task, "state": "running", "completed": 0})
        profile = os.environ.get("AUTOMATION_PROFILE", "basic")
        acceptance = profile in {"full", "full-public"}
        full = profile != "basic"
        if profile not in {"collection", "public"}:
            server = fixture_server(Path(__file__).resolve().parent.parent / "fixtures")
            threading.Thread(target=server.serve_forever, daemon=True).start()
        config_name = "acceptance-sources.json" if acceptance else "collection-sources.json" if profile == "collection" else "sources.json"
        config_path = Path("config") / config_name
        sources = json.loads(config_path.read_text())
        store = Store(ROOT / "store")
        collector = HTTPCollector(should_stop=lambda: stop.exists() or time.monotonic() >= deadline)
        if full:
            import importlib.metadata
            atomic_json(directory / "dependencies.json", {
                "python": sys.version.split()[0],
                "packages": {name: importlib.metadata.version(name) for name in ["httpx", "jsonschema"]}})
            if profile == "public":
                from .intelligence import collect_public_site
                collect_public_site(directory / "public")
                rows = (directory / "public/public-results.jsonl").read_text(encoding="utf-8").splitlines()
                for row in rows:
                    store.put(task, json.loads(row))
                completed = len(rows)
                state = "completed"
                return
            if not sources:
                raise TaskError("no_collection_sources_configured")
            from .engine import CollectionEngine
            from .acceptance import verify_stage_d
            result = CollectionEngine(store, directory, should_stop=collector.should_stop, http=collector).run(task, sources)
            completed = result["completed"]
            state = result["state"]
            if state == "completed" and acceptance:
                verify_stage_d(task, sources, store, directory, server)
                if profile == "full-public":
                    from .intelligence import collect_public_site
                    collect_public_site(directory / "public")
            return
        for index, source in enumerate(sources):
            if stop.exists() or time.monotonic() >= deadline:
                state = "interrupted"
                break
            dynamic = directory / "deadline.json"
            if dynamic.exists() and utcnow() >= instant(json.loads(dynamic.read_text())["deadline"]):
                state = "interrupted"
                break
            if not store.done(task, source["url"]):
                if source.get("mode", "http") == "browser":
                    record = BrowserCollector().collect(source, directory / "browser" / str(index))
                elif source.get("mode", "http") == "http":
                    record = collector.collect(source, store.previous(source["url"]))
                else:
                    raise TaskError("unknown_source_mode")
                store.put(task, record)
                if record["status"] == "error":
                    raise TaskError(record["error_code"])
            completed += 1
            store.checkpoint(task, {"completed": completed, "source_url": source["url"]})
            atomic_json(directory / "status.json", {"task_id": task, "state": "running", "completed": completed})
        else:
            state = "completed"
    except Exception as exc:
        state = "failed"
        error = exc.code if isinstance(exc, TaskError) else "worker_failed"
        if error == "collection_interrupted":
            state = "interrupted"
    finally:
        if server:
            server.shutdown()
            server.server_close()
        manifest = None
        if store:
            manifest = store.export(task, directory / "results.jsonl")
            store.backup(directory / "backup.sqlite")
            store.close()
        atomic_json(directory / "status.json", {"task_id": task, "state": state,
                    "completed": completed, "error_code": error, "manifest": manifest})
        lock.close()


def package_result(task):
    import zipfile
    directory = jobdir(task)
    if busy():
        raise TaskError("job_still_active")
    prior = status(task)
    if prior["state"] in {"running", "starting"}:
        # A cold boot or crash lost the worker. Recover durable rows before export.
        store = Store(ROOT / "store")
        try:
            manifest = store.export(task, directory / "results.jsonl")
            store.backup(directory / "backup.sqlite")
        finally:
            store.close()
        atomic_json(directory / "status.json", {**prior, "state": "lost", "manifest": manifest,
                                                "error_code": "worker_lost"})
    with zipfile.ZipFile(directory / "result.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in ["status.json", "identity.json", "results.jsonl", "results.manifest.json", "backup.sqlite",
                     "job.log", "acceptance-report.json", "dependencies.json"]:
            path = directory / name
            if path.exists():
                z.write(path, name)
        for browser_dir in sorted((directory / "browser").glob("[0-9]*")):
            for name in ["page.png", "record.json"]:
                path = browser_dir / name
                if path.is_file():
                    z.write(path, path.relative_to(directory).as_posix())
        for name in ["public-report.json", "public-results.jsonl", "public-results.manifest.json", "public-backup.sqlite"]:
            path = directory / "public" / name
            if path.is_file():
                z.write(path, path.relative_to(directory).as_posix())
    data = (directory / "result.zip").read_bytes()
    return {"path": str(directory / "result.zip"), "sha256": hashlib.sha256(data).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "status", "stop", "worker", "inspect", "package", "network"])
    parser.add_argument("task")
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--lock-fd", type=int, default=-1)
    parser.add_argument("--profile", choices=["basic", "full", "full-public", "collection", "public"], default="basic")
    args = parser.parse_args()
    identifier(args.task)
    if args.action == "worker":
        worker(args.task, args.seconds, args.lock_fd)
        return
    if args.action == "start":
        result = start(args.task, args.seconds, Path.cwd(), args.profile)
    elif args.action == "status":
        result = {**status(args.task), "busy": busy()}
        if not result["busy"] and result["state"] in {"running", "starting"}:
            result["state"] = "lost"
    elif args.action == "stop":
        directory = jobdir(args.task)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "stop").touch()
        result = {"stop_requested": True}
    elif args.action == "inspect":
        snapshot = process_snapshot()
        result = {"busy": busy(), "processes": snapshot, "process_details": process_details(snapshot)}
    elif args.action == "network":
        from .network import inspect_network
        result = inspect_network()
    else:
        result = package_result(args.task)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
