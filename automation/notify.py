import json
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from .common import TaskError
from .mail_report import LABELS, STATES, render_report, small_attachments

EVENTS = {"TASK_STARTED", "RUNTIME_CHECKPOINT", "RUNTIME_LIMIT_CHANGED", "COLLECTION_COMPLETED",
          "TASK_FAILED", "MACHINE_SUSPENDED", "FINAL_REPORT"}


class SMTPTransport:
    def __init__(self, host, port, username, password, sender, recipient, result_dir=None):
        self.host, self.port, self.username, self.password = host, port, username, password
        self.sender, self.recipient = sender, recipient
        self.result_dir = result_dir

    def build_message(self, event, payload):
        msg = EmailMessage()
        outcome = f" [{STATES.get(payload.get('status'), '未知 / Unknown')}]" if event == "FINAL_REPORT" else ""
        msg["Subject"] = f"[InstaBox] {LABELS.get(event, event)}{outcome} {payload['task_id']}"
        msg["From"], msg["To"] = self.sender, self.recipient
        msg["Date"] = formatdate(usegmt=True)
        domain = parseaddr(self.sender)[1].split("@")[-1]
        msg["Message-ID"] = make_msgid(domain=domain)
        msg["X-InstaBox-Task-ID"] = payload["task_id"]
        attachments, note = [], ""
        if event == "FINAL_REPORT" and payload.get("result_verified") and self.result_dir:
            try:
                attachments, note = small_attachments(self.result_dir)
            except (OSError, ValueError, KeyError, TypeError):
                note = "附件未通过读取或哈希检查，请查看运行记录。 / Attachment read/hash check failed; see run evidence."
        msg.set_content(render_report(event, payload, note))
        for name, data, maintype, subtype in attachments:
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
        return msg

    def __call__(self, event, payload):
        msg = self.build_message(event, payload)
        context = ssl.create_default_context()
        implicit_tls = self.port == 465
        connection = (smtplib.SMTP_SSL(self.host, self.port, timeout=20, context=context) if implicit_tls
                      else smtplib.SMTP(self.host, self.port, timeout=20))
        with connection as smtp:
            if not implicit_tls:
                smtp.starttls(context=context)
            smtp.login(self.username, self.password)
            smtp.send_message(msg)


class Notifier:
    def __init__(self, audit, transport=None, enabled=(), sleep=time.sleep, minimum_interval=1800):
        if set(enabled) - EVENTS:
            raise TaskError("unknown_notification_event")
        self.audit, self.transport, self.enabled, self.sleep = audit, transport, set(enabled), sleep
        self.sent = {}
        self.interval = minimum_interval

    def send(self, event, payload):
        if event not in self.enabled:
            return "disabled"
        # Identical state summaries are suppressed; checkpoints also have a minimum interval.
        signature = json.dumps(payload, sort_keys=True)
        last = self.sent.get(event)
        if last and (last[0] == signature or
                     (event == "RUNTIME_CHECKPOINT" and time.monotonic() - last[1] < self.interval)):
            return "suppressed"
        if self.transport is None:
            self.audit.emit("MAIL_SIMULATED", notification=event)
            self.sent[event] = (signature, time.monotonic())
            return "simulated"
        for attempt in range(3):
            try:
                self.transport(event, payload)
                self.sent[event] = (signature, time.monotonic())
                self.audit.emit("MAIL_ACCEPTED", notification=event, receipt_confirmed=False)
                return "accepted_not_confirmed"
            except (OSError, smtplib.SMTPException):
                self.audit.emit("MAIL_FAILED", notification=event, attempt=attempt + 1,
                                error_code="mail_transport_failed")
                if attempt < 2:
                    self.sleep(2 ** attempt)
        return "failed"
