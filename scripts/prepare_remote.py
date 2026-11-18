"""Download pinned Linux/Python 3.13 wheels; upload them only with this project's release."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
target = root / "vendor"
target.mkdir(exist_ok=True)
command = [sys.executable, "-m", "pip", "download", "--only-binary=:all:",
           "--platform", "manylinux_2_28_x86_64", "--platform", "manylinux2014_x86_64",
           "--implementation", "cp", "--python-version", "313", "--abi", "cp313",
           "--dest", str(target), "httpx==0.28.1", "jsonschema==4.26.0"]
subprocess.run(command, check=True)
files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in target.glob("*.whl")}
(target / "manifest.json").write_text(json.dumps(files, indent=2), encoding="utf-8")
print(json.dumps({"linux_wheels": len(files), "manifest": "vendor/manifest.json"}))
