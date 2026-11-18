"""Run local checks and persist machine-readable evidence. No cloud or mail credentials used."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
OUT = ROOT / "output" / "verification"
OUT.mkdir(parents=True, exist_ok=True)
(ROOT / "output" / "tmp").mkdir(exist_ok=True)
tempfile.tempdir = str(ROOT / "output" / "tmp")
with (OUT / "unittest.txt").open("w", encoding="utf-8") as log:
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(unittest.defaultTestLoader.discover("tests"))
ruff = ROOT / "output" / "tools" / "bin" / "ruff.exe"
command = [str(ruff)] if ruff.exists() else [sys.executable, "-m", "ruff"]
lint = subprocess.run([*command, "check", "."], capture_output=True, text=True)
(OUT / "ruff.txt").write_text(lint.stdout + lint.stderr, encoding="utf-8")
syntax = subprocess.run(["node", "--check", "browser/collect.mjs"], capture_output=True, text=True)
report = {"at": datetime.now(timezone.utc).isoformat(), "tests": result.testsRun,
          "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
          "unit_success": result.wasSuccessful(), "ruff_success": lint.returncode == 0,
          "node_syntax_success": syntax.returncode == 0,
          "cloud_actions_executed": False, "real_email_sent": False}
(OUT / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
sys.exit(0 if result.wasSuccessful() and lint.returncode == syntax.returncode == 0 else 1)
