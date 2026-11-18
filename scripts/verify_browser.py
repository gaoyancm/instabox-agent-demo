"""Run only on self-owned loopback pages using a new native Chromium/CDP profile."""
import functools
import http.server
import json
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "browser"
OUT.mkdir(parents=True, exist_ok=True)
executable = sys.argv[1] if len(sys.argv) > 1 else "/usr/lib/chromium/chromium"
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "fixtures"))
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
results = []
try:
    for name, actions in [("read", []), ("search", [{"type": "fill", "selector": "#search", "value": "Alpha"},
                                                   {"type": "click", "selector": "button"}]),
                          ("pagination", [{"type": "click", "selector": "#next"}])]:
        output = OUT / name
        config = {"url": f"http://127.0.0.1:{server.server_port}/index.html", "output": str(output),
                  "executable": executable, "actions": actions, "automation_allowed": True,
                  "permission_basis": "self-owned static fixture"}
        config_path = OUT / f"{name}.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        run = subprocess.run(["node", str(ROOT / "browser/collect.mjs"), str(config_path)],
                             capture_output=True, text=True, timeout=90)
        (OUT / f"{name}.log").write_text(run.stdout + run.stderr, encoding="utf-8")
        if run.returncode:
            raise RuntimeError(f"browser_{name}_failed")
        record = json.loads((output / "record.json").read_text())
        assert record["status"] == "ok"
        assert "Alpha: 42" in record["content"] if name == "read" else "Pagination completed" in record["content"]
        assert (output / "page.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert not list(output.glob("profile-*")), "profile cleanup failed"
        results.append({"case": name, "passed": True, "title": record["title"], "profile_removed": True})
finally:
    server.shutdown()
    server.server_close()
    thread.join()
(OUT / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
print(json.dumps(results, indent=2))
