"""Stage-D checks against owned fixtures inside the actual worker environment."""
import json
from pathlib import Path
from .common import TaskError, atomic_json
from .engine import CollectionEngine
from .storage import Store


def verify_stage_d(task, sources, store, directory, server):
    rows = store.records(task)
    by_url = {row["source_url"]: row for row in rows}
    checks = {"normalized_records": len(rows) == 8 and all(r["status"] in {"ok", "unchanged"} for r in rows)}
    def get(suffix):
        return next(row for url, row in by_url.items() if url.endswith(suffix))
    checks["html"] = "Alpha: 42" in get("index.html")["content"]
    extracted = get("etag.json")
    checks["json_schema_extraction"] = extracted["extracted_fields"] == {"name": "Alpha", "value": 42}
    checks["extraction_provenance"] = (extracted["metadata"]["extraction"]["model"] == "offline-json-mapping"
                                       and extracted["metadata"]["extraction"]["extraction_version"] == "v1"
                                       and extracted["metadata"]["extraction"]["review_status"] == "verified_against_source")
    checks["rss"] = get("feed.xml")["extracted_fields"]["items"][0]["title"] == "Alpha"
    checks["retry_after"] = server.request_counts.get("/rate-limited.json", 0) >= 2
    dynamic = get("browser.html")
    checks["dynamic_dom"] = ("Dynamic Alpha content" in dynamic["content"]
                             and dynamic["extracted_fields"]["value"] == "42")
    checks["browser_search"] = get("case=search")["title"] == "Fixture page two"
    checks["browser_pagination"] = get("case=pagination")["title"] == "Fixture page two"
    pngs = list((Path(directory) / "browser").glob("*/page.png"))
    checks["screenshots"] = len(pngs) == 3 and all(p.read_bytes().startswith(b"\x89PNG\r\n\x1a\n") for p in pngs)
    checks["browser_profile_cleanup"] = not list((Path(directory) / "browser").rglob("profile-*"))

    http_sources = [source for source in sources if source.get("mode", "http") == "http"]
    engine = CollectionEngine(store, directory)
    incremental = task + "-incremental"
    engine.run(incremental, http_sources)
    changed = store.records(incremental)
    checks["conditional_incremental"] = len(changed) == 5 and all(row["status"] == "unchanged" for row in changed)

    resumed = task + "-resume"
    duplicate_batch = [http_sources[0], http_sources[0], http_sources[1]]
    partial = engine.run(resumed, duplicate_batch, max_items=1)
    checkpoint = store.get_checkpoint(resumed)
    reopened = Store(store.root)
    try:
        result = CollectionEngine(reopened, directory).run(resumed, duplicate_batch)
        checks["disk_checkpoint_resume"] = (partial["state"] == "interrupted" and checkpoint["completed"] == 1
                                             and result["state"] == "completed" and result["skipped"] == 1)
        checks["url_deduplication"] = result["deduplicated"] == 1 and len(reopened.records(resumed)) == 2
    finally:
        reopened.close()

    server.fixture_revision = 2
    changed_source = next(source for source in sources if source["url"].endswith("changing.json"))
    changed_task = task + "-changed"
    engine.run(changed_task, [changed_source])
    latest = store.records(changed_task)[0]
    checks["content_change_detection"] = latest["metadata"]["changed"] and json.loads(latest["content"])["revision"] == 2
    checks["history_retained"] = store.db.execute("SELECT count(DISTINCT task) FROM records").fetchone()[0] >= 4
    report = {"task_id": task, "checks": checks, "passed": all(checks.values()),
              "model_mode": "offline_no_paid_inference", "fixture_only": True,
              "root_record_count": len(rows), "browser_screenshots": len(pngs)}
    atomic_json(Path(directory) / "acceptance-report.json", report)
    if not report["passed"]:
        raise TaskError("stage_d_acceptance_failed")
    return report
