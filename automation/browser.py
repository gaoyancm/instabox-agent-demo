import json
import os
from pathlib import Path
import shutil
import subprocess
from .collect import validate_source
from .common import TaskError, atomic_json


class BrowserCollector:
    def collect(self, source, output):
        validate_source(source)
        native = "/usr/lib/chromium/chromium"
        executable = (os.environ.get("CHROMIUM_PATH") or shutil.which("google-chrome")
                      or (native if Path(native).exists() else None) or shutil.which("chromium"))
        if not executable:
            raise TaskError("chromium_unavailable")
        output = Path(output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        config = {"url": source["url"], "executable": executable, "output": str(output),
                  "automation_allowed": True, "permission_basis": source["permission_basis"],
                  "actions": source.get("actions", []), "wait_selector": source.get("wait_selector"),
                  "dom_fields": source.get("dom_fields", {}), "expected_title": source.get("expected_title")}
        config_path = output / "config.json"
        atomic_json(config_path, config)
        try:
            result = subprocess.run(["node", str(Path(__file__).resolve().parent.parent / "browser/collect.mjs"),
                                     str(config_path)], capture_output=True, timeout=90)
        except (OSError, subprocess.TimeoutExpired):
            raise TaskError("browser_execution_failed") from None
        finally:
            config_path.unlink(missing_ok=True)
        if result.returncode:
            diagnostic = output / "browser-error.json"
            if diagnostic.exists():
                data = json.loads(diagnostic.read_text(encoding="utf-8"))
                code = data.get("error_code", "browser_collection_failed")
                if isinstance(code, str) and code.replace("_", "").isalnum():
                    raise TaskError("browser_" + code)
            raise TaskError("browser_collection_failed")
        record = json.loads((output / "record.json").read_text(encoding="utf-8"))
        record["source_name"] = source.get("name", record["source_name"])
        record["metadata"]["permission_basis"] = source["permission_basis"]
        return record
