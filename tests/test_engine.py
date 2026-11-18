import json
from pathlib import Path
import tempfile
import threading
import unittest
from automation.collect import HTTPCollector, validate_source
from automation.common import TaskError
from automation.engine import CollectionEngine
from automation.fixtures import fixture_server
from automation.records import validate_record
from automation.storage import Store, restore_backup


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = fixture_server(Path(__file__).resolve().parents[1] / "fixtures", port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def source(self, path):
        return {"url": self.base + path, "automation_allowed": True, "permission_basis": "owned fixture"}

    def test_etag_offline_extraction_and_history(self):
        source = self.source("/etag.json")
        source["extraction"] = {"mode": "offline_json", "version": "v1", "field_map": {"value": "value"},
                                "schema": {"type": "object", "properties": {"value": {"type": "integer"}},
                                           "required": ["value"], "additionalProperties": False}}
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            try:
                engine = CollectionEngine(store, tmp, http=HTTPCollector(interval=0))
                engine.run("initial", [source])
                first = store.records("initial")[0]
                self.assertEqual(first["extracted_fields"], {"value": 42})
                self.assertEqual(first["metadata"]["extraction"]["review_status"], "verified_against_source")
                engine.run("incremental", [source])
                second = store.records("incremental")[0]
                self.assertEqual(second["status"], "unchanged")
                self.assertFalse(second["metadata"]["changed"])
                self.assertEqual(first["content_hash"], second["content_hash"])
            finally:
                store.close()

    def test_disk_resume_and_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = [self.source("/index.html"), self.source("/index.html"), self.source("/data.json")]
            first = Store(tmp)
            try:
                result = CollectionEngine(first, tmp).run("resume", sources, max_items=1)
                self.assertEqual(result["state"], "interrupted")
                self.assertEqual(first.get_checkpoint("resume")["completed"], 1)
            finally:
                first.close()
            second = Store(tmp)
            try:
                result = CollectionEngine(second, tmp).run("resume", sources)
                self.assertEqual(result["skipped"], 1)
                self.assertEqual(result["deduplicated"], 1)
                self.assertEqual(len(second.records("resume")), 2)
                self.assertGreater(second.db.execute("SELECT count(*) FROM execution_log").fetchone()[0], 2)
            finally:
                second.close()

    def test_changed_content_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            try:
                engine = CollectionEngine(store, tmp)
                self.server.fixture_revision = 1
                engine.run("one", [self.source("/changing.json")])
                self.server.fixture_revision = 2
                engine.run("two", [self.source("/changing.json")])
                latest = store.records("two")[0]
                self.assertTrue(latest["metadata"]["changed"])
                self.assertNotEqual(latest["content_hash"], store.records("one")[0]["content_hash"])
                self.assertEqual(json.loads(latest["content"])["revision"], 2)
            finally:
                store.close()

    def test_rss_and_record_contract(self):
        record = HTTPCollector().collect(self.source("/feed.xml"))
        validate_record(record)
        self.assertEqual(record["title"], "Test feed")
        self.assertEqual(record["extracted_fields"]["items"][0]["title"], "Alpha")
        record["content_hash"] = "bad"
        with self.assertRaisesRegex(TaskError, "hash_invalid"):
            validate_record(record)

    def test_backup_restore_roundtrip_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = Store(root / "original")
            try:
                CollectionEngine(original, tmp).run('backup-task', [self.source('/data.json')])
                original.backup(root / 'backup.sqlite')
                expected = original.records('backup-task')
            finally:
                original.close()
            restore_backup(root / 'backup.sqlite', root / 'restored')
            recovered = Store(root / 'restored')
            try:
                self.assertEqual(recovered.records('backup-task'), expected)
            finally:
                recovered.close()
            with self.assertRaisesRegex(TaskError, 'target_exists'):
                restore_backup(root / 'backup.sqlite', root / 'restored')

    def test_explicit_query_permission_does_not_allow_credentials(self):
        validate_source({**self.source("/data.json?page=2"), "allowed_query_keys": ["page"]})
        with self.assertRaises(TaskError):
            validate_source({**self.source("/data.json?api_key=private"), "allowed_query_keys": ["api_key"]})

    def test_stop_and_extraction_failure_keep_recoverable_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            try:
                result = CollectionEngine(store, tmp, should_stop=lambda: True).run("stopped", [self.source("/data.json")])
                self.assertEqual(result["state"], "interrupted")
                bad = self.source("/data.json")
                bad["extraction"] = {"mode": "provider", "version": "v1", "schema": {"type": "object"}}
                with self.assertRaisesRegex(TaskError, "ai_not_authorized"):
                    CollectionEngine(store, tmp).run("failed", [bad])
                self.assertFalse(store.done("failed", bad["url"]))
                self.assertEqual(store.records("failed")[0]["error_code"], "ai_not_authorized")
                CollectionEngine(store, tmp).run("failed", [self.source("/data.json")])
                self.assertTrue(store.done("failed", bad["url"]))
            finally:
                store.close()
