import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from .common import atomic_json


def content_hash(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "records.sqlite")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS records (
              task TEXT, url TEXT, hash TEXT, status TEXT, record TEXT,
              PRIMARY KEY(task,url));
            CREATE TABLE IF NOT EXISTS checkpoints (task TEXT PRIMARY KEY, value TEXT);
            CREATE INDEX IF NOT EXISTS url_history ON records(url);
            CREATE TABLE IF NOT EXISTS execution_log (
              id INTEGER PRIMARY KEY, task TEXT, event TEXT, details TEXT);
        ''')

    def previous(self, url):
        row = self.db.execute("SELECT record FROM records WHERE url=? AND status IN ('ok','unchanged') ORDER BY rowid DESC LIMIT 1",
                              (url,)).fetchone()
        return json.loads(row[0]) if row else None

    def done(self, task, url):
        row = self.db.execute("SELECT status FROM records WHERE task=? AND url=?", (task, url)).fetchone()
        return row and row[0] in {"ok", "unchanged"}

    def records(self, task):
        return [json.loads(row[0]) for row in self.db.execute("SELECT record FROM records WHERE task=? ORDER BY url", (task,))]

    def put(self, task, record):
        previous = self.previous(record["source_url"])
        record["metadata"]["changed"] = (previous is None or previous["content_hash"] != record["content_hash"])
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?,?)",
                            (task, record["source_url"], record["content_hash"], record["status"],
                             json.dumps(record, ensure_ascii=False)))

    def checkpoint(self, task, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?)", (task, json.dumps(value)))

    def get_checkpoint(self, task):
        row = self.db.execute("SELECT value FROM checkpoints WHERE task=?", (task,)).fetchone()
        return json.loads(row[0]) if row else None

    def log(self, task, event, details):
        with self.db:
            self.db.execute("INSERT INTO execution_log(task,event,details) VALUES(?,?,?)",
                            (task, event, json.dumps(details)))

    def export(self, task, target):
        # SQLite is authoritative; regenerate JSONL atomically to repair a crash between outputs.
        target = Path(target)
        temp = target.with_suffix(".tmp")
        rows = self.db.execute("SELECT record FROM records WHERE task=? ORDER BY url", (task,))
        with temp.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(row[0] + "\n")
        temp.replace(target)
        manifest = {"file": target.name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "count": self.db.execute("SELECT count(*) FROM records WHERE task=?", (task,)).fetchone()[0]}
        atomic_json(target.with_suffix(".manifest.json"), manifest)
        return manifest

    def backup(self, target):
        with closing(sqlite3.connect(target)) as backup:
            self.db.backup(backup)

    def close(self):
        self.db.close()


def restore_backup(backup_path, destination):
    """Restore to a new directory only; never replace an active or existing database."""
    from .common import TaskError
    destination = Path(destination)
    target = destination / "records.sqlite"
    if target.exists():
        raise TaskError("restore_target_exists")
    destination.mkdir(parents=True, exist_ok=True)
    temporary = destination / "records.sqlite.restoring"
    if temporary.exists():
        raise TaskError("restore_in_progress")
    with closing(sqlite3.connect(Path(backup_path).resolve().as_uri() + "?mode=ro", uri=True)) as source:
        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise TaskError("backup_integrity_failed")
        with closing(sqlite3.connect(temporary)) as recovered:
            source.backup(recovered)
    temporary.replace(target)
    return target
