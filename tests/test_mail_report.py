import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from automation.common import Audit, TaskError
from automation.controller import Controller
from automation.mail_report import collection_summary, notification_events, small_attachments
from automation.network import inspect_network
from automation.notify import Notifier, SMTPTransport
from automation.cli import main
from test_controller import FakeAPI, FakeJobs
from test_runtime_api import NOW


def fixture(directory, content="实际采集内容 / collected content"):
    row = {"source_url": "https://example.invalid/public", "source_name": "owned fixture", "collected_at": NOW.isoformat(),
           "title": "香品 / Incense", "content": content, "extracted_fields": {"value": 42}, "status": "ok"}
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode()
    (directory / "results.jsonl").write_bytes(data)
    (directory / "results.manifest.json").write_text(json.dumps({"count": 1, "sha256": hashlib.sha256(data).hexdigest()}))
    return row


class MailReportTests(unittest.TestCase):
    def check_preflight(self, key_failure=False):
        with tempfile.TemporaryDirectory() as folder, patch("automation.cli.signal.signal"), \
             patch.dict(os.environ, {"CLOUD_EXECUTION_APPROVED": "true", "MAIL_APPROVED": "true",
                "MAIL_MODE": "observe", "SMTP_HOST": "smtp.invalid", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "secret",
                "MAIL_FROM": "from@example.invalid", "MAIL_TO": "recipient@example.invalid"}, clear=True), \
             patch("sys.argv", ["cli", "cloud", "--task-id", "preflight", "--output", folder]), \
             patch("automation.cli.InstaBox") as api, patch("automation.cli.JobRunner") as jobs, \
             patch("automation.cli.SMTPTransport") as transport:
            if key_failure:
                api.side_effect = TaskError("api_key_required")
            else:
                jobs.side_effect = TaskError("no_collection_sources_configured")
            self.assertEqual(main(), 1)
            self.assertEqual(transport.return_value.call_count, 1)
            self.assertEqual(transport.return_value.call_args.args[0], "FINAL_REPORT")
            payload = transport.return_value.call_args.args[1]
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["server_info"], {})
            self.assertNotIn("secret", json.dumps(payload))
            api.return_value.lifecycle.assert_not_called()

    def test_preflight_failure_still_notifies_without_waking_machine(self):
        self.check_preflight()

    def test_missing_key_still_notifies_with_unavailable_machine_fields(self):
        self.check_preflight(key_failure=True)

    def test_observe_and_summary_modes_and_invalid_configuration(self):
        self.assertEqual(notification_events("observe"), {"TASK_STARTED", "FINAL_REPORT"})
        self.assertEqual(notification_events("summary", ["TASK_STARTED"]), {"FINAL_REPORT"})
        with self.assertRaisesRegex(TaskError, "invalid_mail_mode"):
            notification_events("unknown")

    def test_bilingual_body_small_attachments_and_single_recipient(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            fixture(directory)
            payload = {"task_id": "mail-test", "result_verified": True, "status": "completed",
                "machine_state": "suspended", "server_info": {"network": {"egress_ipv4": "1.1.1.1"}},
                **collection_summary(directory)}
            message = SMTPTransport("unused", 465, "user", "SECRET_NOT_IN_MAIL", "from@example.invalid",
                                    "recipient@example.invalid", directory).build_message("FINAL_REPORT", payload)
            body = message.get_body(preferencelist=("plain",)).get_content()
            self.assertIn("任务结束 / Task finished", body)
            self.assertIn("香品 / Incense", body)
            self.assertIn("1.1.1.1", body)
            self.assertIn("未取得 / Unavailable", body)
            self.assertIn("实际采集内容", body)
            self.assertEqual(message["To"], "recipient@example.invalid")
            self.assertEqual([a.get_filename() for a in message.iter_attachments()], ["results.jsonl", "results.csv"])
            self.assertNotIn("SECRET_NOT_IN_MAIL", message.as_string())

    def test_large_results_use_link_and_bounded_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            fixture(directory, "x" * 300000)
            attachments, note = small_attachments(directory)
            self.assertEqual(attachments, [])
            self.assertIn("download", note)
            self.assertEqual(len(collection_summary(directory)["collection_preview"][0]["content"]), 800)

    def test_hash_mismatch_does_not_attach_unverified_content(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            fixture(directory)
            (directory / "results.jsonl").write_text("tampered")
            message = SMTPTransport("unused", 465, "u", "p", "from@example.invalid", "recipient@example.invalid", directory).build_message(
                "FINAL_REPORT", {"task_id": "hash-test", "result_verified": True})
            self.assertEqual(list(message.iter_attachments()), [])
            self.assertIn("hash check failed", message.get_content())

    def test_csv_neutralizes_web_controlled_formulas(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            fixture(directory, "=HYPERLINK(bad)")
            attachments, _ = small_attachments(directory)
            self.assertIn("'=HYPERLINK(bad)", attachments[1][1].decode("utf-8-sig"))

    def test_summary_mode_preserves_this_cycle_startup_and_failure(self):
        for mode in ["observe", "summary"]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                audit = Audit(directory / "audit.jsonl", "controller-mail")
                messages = []
                jobs = FakeJobs()
                jobs.network = lambda: {"egress_ipv4": "1.1.1.1", "observed_at": NOW.isoformat()}
                jobs.hash_bad = True
                notifier = Notifier(audit, lambda event, data: messages.append((event, data)), notification_events(mode))
                report = Controller(FakeAPI(), jobs, audit, notifier, directory, exclusive=True,
                                    now=lambda: NOW, sleep=lambda _: None).run()
                self.assertEqual([event for event, _ in messages], ["TASK_STARTED", "FINAL_REPORT"] if mode == "observe" else ["FINAL_REPORT"])
                final = messages[-1][1]
                self.assertEqual(final["status"], "failed")
                self.assertEqual(final["server_info"]["network"]["egress_ipv4"], "1.1.1.1")
                self.assertEqual(report["server_info"], final["server_info"])

    def test_network_timeout_has_no_stale_addresses_or_region(self):
        with patch("automation.network.subprocess.run", side_effect=subprocess.TimeoutExpired("probe", 8)):
            result = inspect_network()
        self.assertIsNone(result["egress_ipv4"])
        self.assertIsNone(result["egress_ipv6"])
        self.assertIsNone(result["platform_region"])
        self.assertIn("ipv6_unavailable", result["errors"])

    def test_network_validates_addresses_and_geo_identity(self):
        def execute(argv, **kwargs):
            if argv[0] == "ip":
                data = '[{"addr_info":[{"family":"inet","local":"10.0.0.2"}]}]'
            elif "ipwho.is" in argv[-1]:
                data = '{"ip":"8.8.8.8","success":true,"city":"WRONG_CITY"}'
            elif "-4" in argv:
                data = "ip=1.1.1.1\nloc=US\n"
            else:
                data = "ip=127.0.0.1\nloc=ZZ\n"
            return subprocess.CompletedProcess(argv, 0, data.encode(), b"")
        with patch("automation.network.subprocess.run", side_effect=execute):
            result = inspect_network()
        self.assertEqual(result["egress_ipv4"], "1.1.1.1")
        self.assertIsNone(result["egress_ipv6"])
        self.assertIsNone(result["geo_region"])
        self.assertEqual(result["country_code"], "US")
        self.assertEqual(result["machine_addresses"], ["10.0.0.2"])
