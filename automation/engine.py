"""Source-configured collection -> optional extraction -> normalization -> durable checkpoint."""
import json
import os
from pathlib import Path
from .collect import HTTPCollector, validate_source
from .common import TaskError, identifier, utcnow
from .records import validate_record
from .storage import content_hash


def extract_record(record, config):
    if not config or record["status"] == "error":
        return record
    from .ai import Extractor
    schema = config["schema"]
    mode = config.get("mode", "offline_json")
    if mode == "offline_json":
        original = json.loads(record["content"])
        # Resolve actual source values; no canned model answer is substituted for extraction.
        fields = {destination: original[source] for destination, source in config["field_map"].items()}
        result = Extractor(schema, config["version"], model="offline-json-mapping").extract(record["content"], fields)
        result["review_status"] = "verified_against_source"
        result["evidence"] = config["field_map"]
    elif mode == "provider":
        result = Extractor(schema, config["version"], model=os.environ.get("MODEL_NAME", ""),
                           endpoint=os.environ.get("MODEL_ENDPOINT"), key=os.environ.get("MODEL_API_KEY"),
                           authorized=os.environ.get("AI_NETWORK_APPROVED") == "true").extract(record["content"])
    else:
        raise TaskError("unknown_extraction_mode")
    record["extracted_fields"].update(result["fields"])
    record["metadata"]["extraction"] = {key: value for key, value in result.items() if key != "fields"}
    return record


class CollectionEngine:
    def __init__(self, store, output, *, should_stop=lambda: False, http=None):
        self.store, self.output, self.should_stop = store, Path(output), should_stop
        self.http = http or HTTPCollector(should_stop=should_stop)

    def run(self, task, sources, *, max_items=None):
        identifier(task)
        if max_items is not None and max_items < 1:
            raise TaskError("invalid_batch_size")
        unique = {}
        for source in sources:
            validate_source(source)
            if source["url"] in unique and unique[source["url"]] != source:
                raise TaskError("conflicting_duplicate_source")
            unique[source["url"]] = source
        completed = skipped = new_items = 0
        self.store.log(task, "BATCH_STARTED", {"unique_sources": len(unique)})
        for index, source in enumerate(unique.values()):
            if self.should_stop() or (max_items is not None and new_items >= max_items):
                return {"state": "interrupted", "completed": completed, "skipped": skipped}
            if self.store.done(task, source["url"]):
                skipped += 1
            else:
                previous = self.store.previous(source["url"])
                try:
                    mode = source.get("mode", "http")
                    if mode == "http":
                        record = self.http.collect(source, previous)
                    elif mode == "browser":
                        from .browser import BrowserCollector
                        record = BrowserCollector().collect(source, self.output / "browser" / str(index))
                    else:
                        raise TaskError("unknown_source_mode")
                    if record["status"] != "error" and content_hash(record["content"]) != record["content_hash"]:
                        raise TaskError("collector_content_hash_mismatch")
                    record = extract_record(record, source.get("extraction"))
                    validate_record(record)
                except Exception as exc:
                    record = {"source_url": source["url"], "source_name": source.get("name", ""),
                              "collected_at": utcnow().isoformat(), "title": "", "content": "",
                              "extracted_fields": {}, "content_hash": "", "status": "error",
                              "error_code": exc.code if isinstance(exc, TaskError) else "extraction_or_record_failed",
                              "metadata": {"permission_basis": source["permission_basis"]}}
                self.store.put(task, record)
                self.store.log(task, "SOURCE_RESULT", {"url": source["url"], "status": record["status"],
                                                       "error_code": record["error_code"]})
                if record["status"] == "error":
                    raise TaskError(record["error_code"])
                new_items += 1
            completed += 1
            self.store.checkpoint(task, {"completed": completed, "source_url": source["url"],
                                         "source_count": len(unique), "skipped": skipped})
        return {"state": "completed", "completed": completed, "skipped": skipped,
                "deduplicated": len(sources) - len(unique)}
