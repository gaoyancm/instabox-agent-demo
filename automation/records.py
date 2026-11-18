"""Shared record contract for every transport and extraction adapter."""
import re
from .common import TaskError, instant

RECORD_SCHEMA = {
    "type": "object",
    "required": ["source_url", "source_name", "collected_at", "title", "content", "extracted_fields",
                 "content_hash", "status", "error_code", "metadata"],
    "properties": {
        "source_url": {"type": "string", "pattern": "^https?://"},
        "source_name": {"type": "string"}, "collected_at": {"type": "string"},
        "title": {"type": "string"}, "content": {"type": "string"},
        "extracted_fields": {"type": "object"}, "content_hash": {"type": "string"},
        "status": {"enum": ["ok", "unchanged", "error"]},
        "error_code": {"type": ["string", "null"]}, "metadata": {"type": "object"},
    },
}


def validate_record(record):
    from jsonschema import Draft202012Validator
    if not Draft202012Validator(RECORD_SCHEMA).is_valid(record):
        raise TaskError("record_schema_invalid")
    if instant(record["collected_at"]).tzinfo is None:
        raise TaskError("record_timestamp_without_timezone")
    if record["status"] != "error" and not re.fullmatch("[0-9a-f]{64}", record["content_hash"]):
        raise TaskError("record_hash_invalid")
    return record
