from datetime import datetime, timezone, timedelta
from email.message import Message
import functools
import http.server
import json
from pathlib import Path
import smtplib
import sqlite3
from contextlib import closing
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import MagicMock, patch
from automation.ai import Extractor
from automation.collect import HTTPCollector, retry_after, validate_source
from automation.common import Audit, TaskError
from automation.notify import Notifier, SMTPTransport
from automation.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def source(url):
    return {"url": url, "name": "owned", "automation_allowed": True, "permission_basis": "owned fixture"}


class CollectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "fixtures"))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_html_json_rss_and_not_modified(self):
        collector = HTTPCollector(interval=0)
        html = collector.collect(source(self.base + "/index.html"))
        self.assertEqual(html["title"], "InstaBox Collection Fixture")
        self.assertIn("Alpha: 42", html["content"])
        second = collector.collect(source(self.base + "/index.html"), html)
        self.assertEqual(second["status"], "unchanged")
        data = collector.collect(source(self.base + "/data.json"))
        self.assertEqual(json.loads(data["content"])["value"], 42)
        rss = collector.collect(source(self.base + "/feed.xml"))
        self.assertIn("<rss", rss["content"])

    def test_retry_after_and_no_access_bypass(self):
        calls, sleeps = [], []
        def opener(req, **kwargs):
            calls.append(req)
            header = Message()
            header["Retry-After"] = "2"
            raise urllib.error.HTTPError(req.full_url, 429, "rate", header, None)
        result = HTTPCollector(interval=0, sleep=sleeps.append, opener=opener).collect(source(self.base))
        self.assertEqual(result["error_code"], "http_429")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sum(sleeps), 4)
        calls.clear()
        def denied(req, **kwargs):
            calls.append(req)
            raise urllib.error.HTTPError(req.full_url, 403, "private", Message(), None)
        self.assertEqual(HTTPCollector(opener=denied).collect(source(self.base))["error_code"], "access_denied")
        self.assertEqual(len(calls), 1)

    def test_header_retry_date(self):
        now = datetime(2026, 11, 12, tzinfo=timezone.utc)
        from email.utils import format_datetime
        self.assertEqual(retry_after(format_datetime(now + timedelta(seconds=30)), now), 30)

    def test_permission_gate(self):
        for url in ["file:///secret", "https://user:pass@example.com", "https://example.com?token=x"]:  # trufflehog:ignore (dummy credential URL that must be rejected)
            with self.assertRaises(TaskError):
                validate_source(source(url))
        with self.assertRaises(TaskError):
            validate_source({"url": self.base})

    def test_store_resume_history_change_export_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                record = HTTPCollector(interval=0).collect(source(self.base + "/data.json"))
                store.put("one", record)
                self.assertTrue(store.done("one", record["source_url"]))
                store.put("two", {**record, "metadata": {}})
                self.assertFalse(store.previous(record["source_url"])["metadata"]["changed"])
                store.put("three", {**record, "content_hash": "changed", "metadata": {}})
                self.assertTrue(store.previous(record["source_url"])["metadata"]["changed"])
                store.checkpoint("three", {"completed": 1})
                manifest = store.export("three", Path(directory) / "results.jsonl")
                self.assertEqual(manifest["count"], 1)
                store.backup(Path(directory) / "backup.sqlite")
                with closing(sqlite3.connect(Path(directory) / "backup.sqlite")) as backup:
                    self.assertEqual(backup.execute("SELECT count(*) FROM records").fetchone()[0], 3)
            finally:
                store.close()


class AINotificationTests(unittest.TestCase):
    def test_smtp_implicit_tls_single_recipient(self):
        connection = MagicMock()
        with patch('automation.notify.smtplib.SMTP_SSL', return_value=connection) as secure:
            SMTPTransport('smtp.example.invalid', 465, 'test-user', 'test-password',
                          'sender@example.invalid', 'recipient@example.invalid')('FINAL_REPORT', {'task_id': 'test'})
        secure.assert_called_once()
        smtp = connection.__enter__.return_value
        smtp.starttls.assert_not_called()
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message['To'], 'recipient@example.invalid')

    def test_ai_offline_validation_and_review(self):
        schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False}
        extractor = Extractor(schema, "v1")
        self.assertEqual(extractor.extract("Alpha 42", {"value": 42})["review_status"], "pending")
        with self.assertRaisesRegex(TaskError, "schema_invalid"):
            extractor.extract("", {"value": "42"})
        with self.assertRaisesRegex(TaskError, "not_authorized"):
            extractor.extract("Alpha 42")

    def test_mail_simulation_retry_suppression(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = Audit(Path(directory) / "audit.jsonl", "test")
            notifier = Notifier(audit, enabled=["FINAL_REPORT"])
            self.assertEqual(notifier.send("FINAL_REPORT", {"task_id": "test"}), "simulated")
            self.assertEqual(notifier.send("FINAL_REPORT", {"task_id": "test"}), "suppressed")
            calls = []
            def failed(event, payload):
                calls.append(event)
                raise smtplib.SMTPException("PASSWORD_MUST_NOT_BE_LOGGED")
            notifier = Notifier(audit, failed, enabled=["TASK_FAILED"], sleep=lambda _: None)
            self.assertEqual(notifier.send("TASK_FAILED", {"task_id": "test"}), "failed")
            self.assertEqual(len(calls), 3)
            self.assertNotIn("PASSWORD_MUST_NOT_BE_LOGGED", audit.path.read_text())

    def test_mail_success_not_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = Audit(Path(directory) / "audit.jsonl", "test")
            notifier = Notifier(audit, lambda *_: None, ["FINAL_REPORT"])
            self.assertEqual(notifier.send("FINAL_REPORT", {"task_id": "test"}), "accepted_not_confirmed")
            self.assertIn('"receipt_confirmed": false', audit.path.read_text())
