"""Run the complete Stage-D pipeline on owned fixtures with the actual browser adapter."""
import json
import os
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation.acceptance import verify_stage_d
from automation.common import utcnow
from automation.engine import CollectionEngine
from automation.fixtures import fixture_server
from automation.storage import Store

ROOT = Path(__file__).resolve().parents[1]

if len(sys.argv) > 1:
    os.environ["CHROMIUM_PATH"] = sys.argv[1]
out = ROOT / "output/full"
out.mkdir(parents=True, exist_ok=True)
server = fixture_server(ROOT / "fixtures")
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
store = Store(out / "store")
task = "stage-d-" + utcnow().strftime("%Y%m%d-%H%M%S")
try:
    sources = json.loads((ROOT / "config/acceptance-sources.json").read_text())
    result = CollectionEngine(store, out).run(task, sources)
    assert result["state"] == "completed"
    report = verify_stage_d(task, sources, store, out, server)
    manifest = store.export(task, out / "results.jsonl")
    store.backup(out / "backup.sqlite")
    print(json.dumps({"stage_d": report, "manifest": manifest}, indent=2))
finally:
    store.close()
    server.shutdown()
    server.server_close()
    thread.join()
