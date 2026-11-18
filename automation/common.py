import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


class TaskError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def utcnow():
    return datetime.now(timezone.utc)


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def identifier(value):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value):
        raise TaskError("invalid_task_id")
    return value


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


class Audit:
    """Only explicitly selected fields: never exception messages, headers or environment."""
    def __init__(self, path, task_id):
        self.path = Path(path)
        self.task_id = identifier(task_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event, **fields):
        record = {"at": utcnow().isoformat(), "task_id": self.task_id, "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return record
