"""A single opt-in real mail. Must run only after credential-deployment approval."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation.common import Audit, TaskError
from automation.notify import Notifier, SMTPTransport


def main():
    if os.environ.get("MAIL_TEST_APPROVED") != "true":
        raise TaskError("mail_test_not_approved")
    names = ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_FROM", "MAIL_TO"]
    if any(not os.environ.get(name) for name in names):
        raise TaskError("mail_config_incomplete")
    transport = SMTPTransport(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")),
                              os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"],
                              os.environ["MAIL_FROM"], os.environ["MAIL_TO"])
    audit = Audit(Path("output/mail-test/audit.jsonl"), "mail-acceptance")
    status = Notifier(audit, transport, ["FINAL_REPORT"]).send("FINAL_REPORT", {
        "task_id": "mail-acceptance", "machine_state": "not_modified", "result_summary": "Single approved test",
        "run_url": os.environ.get("ACTIONS_RUN_URL", ""), "receipt_confirmed": False})
    print(status)
    return 0 if status == "accepted_not_confirmed" else 1


if __name__ == "__main__":
    sys.exit(main())
