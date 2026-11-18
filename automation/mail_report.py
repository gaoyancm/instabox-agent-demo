"""Bilingual mail, verified small attachments and bounded readable collection previews."""
import csv
import hashlib
import io
import json
from pathlib import Path

MAX_ATTACHMENTS = 256 * 1024
LABELS = {"TASK_STARTED": "启动就绪 / Server ready", "FINAL_REPORT": "任务结束 / Task finished",
          "TASK_FAILED": "任务失败 / Task failed", "MACHINE_SUSPENDED": "机器已暂停 / Machine suspended",
          "COLLECTION_COMPLETED": "采集完成 / Collection completed",
          "RUNTIME_CHECKPOINT": "运行检查 / Runtime checkpoint",
          "RUNTIME_LIMIT_CHANGED": "运行上限变化 / Runtime limit changed"}
STATES = {"completed": "成功 / Success", "failed": "失败 / Failed", "interrupted": "中断 / Interrupted",
          "running": "运行中 / Running", "suspended": "已暂停 / Suspended",
          "unknown": "未取得 / Unavailable"}


def notification_events(mode, extra=()):
    if mode not in {"observe", "summary"}:
        from .common import TaskError
        raise TaskError("invalid_mail_mode")
    events = set(extra) | {"FINAL_REPORT"}
    events.discard("TASK_STARTED")
    if mode == "observe":
        events.add("TASK_STARTED")
    return events


def verified_results(directory):
    directory = Path(directory)
    data = (directory / "results.jsonl").read_bytes()
    manifest = json.loads((directory / "results.manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(data).hexdigest() != manifest["sha256"]:
        raise ValueError("result_hash_mismatch")
    records = [json.loads(row) for row in data.splitlines()]
    if len(records) != manifest["count"]:
        raise ValueError("result_count_mismatch")
    return data, records, manifest


def collection_summary(directory):
    _, records, manifest = verified_results(directory)
    previews = []
    for row in records[:5]:
        previews.append({"title": str(row.get("title", ""))[:200], "source_url": row.get("source_url", ""),
                         "collected_at": row.get("collected_at"), "status": row.get("status"),
                         "content": str(row.get("content", ""))[:800],
                         "extracted_fields": json.dumps(row.get("extracted_fields", {}), ensure_ascii=False)[:800]})
    return {"result_summary": manifest, "collection_preview": previews,
            "preview_note": "最多5条；每条内容/字段最多800字符。完整内容见附件或下载。 / Up to 5 records; content/fields capped at 800 characters each. Full data in attachments or download."}


def small_attachments(directory):
    data, records, _ = verified_results(directory)
    if len(data) > MAX_ATTACHMENTS:
        return [], "结果较大，请下载完整结果包。 / Results exceed the attachment limit; download the full artifact."
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["source_url", "source_name", "collected_at", "title", "content", "extracted_fields", "status"])
    for row in records:
        values = []
        for key in ["source_url", "source_name", "collected_at", "title", "content", "extracted_fields", "status"]:
            value = row.get(key, "")
            value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            # Web-controlled text must not turn into an Excel formula when an attachment is opened.
            values.append("'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r")) else value)
        writer.writerow(values)
    csv_data = buffer.getvalue().encode("utf-8-sig")
    attachments = [("results.jsonl", data, "application", "octet-stream")]
    if len(data) + len(csv_data) <= MAX_ATTACHMENTS:
        attachments.append(("results.csv", csv_data, "text", "csv"))
    return attachments, "完整小型结果已附上，无需登录 GitHub。 / Full small results attached; no GitHub login required."


def render_report(event, payload, attachment_note=""):
    def show(value):
        if value is None or value == "":
            return "未取得 / Unavailable"
        if isinstance(value, bool):
            return "是 / Yes" if value else "否 / No"
        return str(value)
    info = payload.get("server_info", {})
    network = info.get("network", {})
    lines = ["InstaBox 云端任务报告 / Cloud task report", "",
        f"事件 / Event: {LABELS.get(event, event)}", f"任务编号 / Task ID: {payload['task_id']}",
        f"任务模式 / Task profile: {show(payload.get('task_profile'))}",
        f"通知时间 / Notification time (UTC): {show(payload.get('at'))}",
        f"结果 / Result: {STATES.get(payload.get('status'), show(payload.get('status')))}",
        f"机器状态 / Machine state: {STATES.get(payload.get('machine_state'), show(payload.get('machine_state')))}",
        f"执行耗时 / Elapsed seconds: {show(payload.get('elapsed_seconds'))}",
        f"错误 / Error: {payload.get('error_code') or '无 / None'}", "",
        "本次启动信息 / This cycle's startup information",
        f"服务器 / Server: {show(info.get('name'))} ({show(info.get('machine_id'))})",
        f"周期开始 / Cycle started (UTC): {show(info.get('run_started_at'))}",
        f"启动就绪采样 / Ready sample (UTC): {show(info.get('ready_at'))}",
        f"平台机房区域 / Platform datacenter region: {show(network.get('platform_region'))}",
        f"出口 IP 定位 / Egress IP geolocation: {show(network.get('geo_region') or network.get('country_code'))}",
        "定位为出口IP地理定位，不是机房或固定地域保证。 / IP geolocation is not a datacenter or fixed-region guarantee.",
        f"出口 IPv4 / Egress IPv4: {show(network.get('egress_ipv4'))}",
        f"出口 IPv6 / Egress IPv6: {show(network.get('egress_ipv6'))}",
        f"机器接口地址 / Machine interface addresses: {show(network.get('machine_addresses'))}",
        f"网络采样 / Network sample (UTC): {show(network.get('observed_at'))}",
        f"采样来源 / Observation sources: {', '.join(network.get('sources', [])) or show(None)}",
        f"采样限制 / Observation limits: {', '.join(network.get('errors', [])) or '无 / None'}",
        f"启动时运行上限 / Limit at startup (minutes): {show(info.get('max_run_minutes'))}",
        f"启动时剩余预算 / Remaining budget at startup (seconds): {show(info.get('remaining_seconds'))}", "",
        f"结果已校验 / Results verified: {show(payload.get('result_verified'))}",
        f"暂停且同步 / Suspended and synced: {show(payload.get('suspended_and_synced'))}",
        f"磁盘已同步 / Storage synced: {show(payload.get('storage_synced'))}"]
    summary = payload.get("result_summary", {})
    lines += [f"记录数 / Record count: {show(summary.get('count'))}", "", "采集内容摘要 / Collected content preview"]
    for index, row in enumerate(payload.get("collection_preview", []), 1):
        lines += [f"{index}. {row['title']}", f"来源 / Source: {row['source_url']}",
                  f"采集时间 / Collected at: {row.get('collected_at')}",
                  f"内容 / Content: {row['content']}", f"提取字段 / Extracted fields: {row['extracted_fields']}", ""]
    if not payload.get("collection_preview"):
        lines.append("尚无已校验采集内容。 / No verified collection content available yet.")
    lines += [payload.get("preview_note", ""), attachment_note, "",
        f"运行及完整结果 / Run and full results: {payload.get('run_url') or show(None)}",
        "下载位置：运行页底部 Artifacts；需要有权限的 GitHub 登录，保留30天。 / Download: Artifacts at the bottom of the run page; authorized GitHub login required; retained 30 days.",
        "若邮件发送时工作流尚未结束，请稍后刷新下载页。 / If the workflow is still finishing, refresh the download page later."]
    return "\n".join(lines)
