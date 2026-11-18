"""GET-only collector. Every source requires an explicit automation permission declaration."""
import json
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qsl
import copy
import xml.etree.ElementTree as ET
from .common import TaskError, utcnow
from .storage import content_hash


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.titles = [], []
        self.hidden = 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.titles.append(data)
        elif not self.hidden and data.strip():
            self.parts.append(data.strip())


def validate_source(source):
    parts = urlsplit(source["url"])
    keys = {key.lower() for key, _ in parse_qsl(parts.query)}
    allowed = {key.lower() for key in source.get("allowed_query_keys", [])}
    sensitive = {"token", "access_token", "api_key", "apikey", "password", "authorization", "secret"}
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or
            keys - allowed or keys & sensitive or parts.fragment or not source.get("automation_allowed") or
            not source.get("permission_basis")):
        raise TaskError("source_not_authorized")


def retry_after(value, now=None):
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, (parsedate_to_datetime(value) - (now or utcnow())).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 1


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect is a new source and needs its own approval.
        return None


class HTTPCollector:
    def __init__(self, interval=1, timeout=20, attempts=3, max_bytes=2_000_000,
                 sleep=time.sleep, opener=None, clock=time.monotonic, should_stop=lambda: False):
        self.interval, self.timeout, self.attempts = interval, timeout, attempts
        self.max_bytes, self.sleep, self.clock = max_bytes, sleep, clock
        self.last = {}
        self.should_stop = should_stop
        # Explicit direct transport: system proxies must not intercept loopback fixtures.
        self.open = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open

    def collect(self, source, previous=None):
        validate_source(source)
        url = source["url"]
        host = urlsplit(url).netloc
        headers = {"User-Agent": "InstaBox-Automation-Test/1.0", "Accept": "text/html,application/json,application/rss+xml"}
        if previous:
            for field, header in [("etag", "If-None-Match"), ("last_modified", "If-Modified-Since")]:
                if previous["metadata"].get(field):
                    headers[header] = previous["metadata"][field]
        record = dict(source_url=url, source_name=source.get("name", host), collected_at=utcnow().isoformat(),
                      title="", content="", extracted_fields={}, content_hash="", status="error",
                      error_code=None, metadata={"permission_basis": source["permission_basis"]})
        for attempt in range(self.attempts):
            if self.should_stop():
                record["error_code"] = "collection_interrupted"
                return record
            self.sleep(max(0, self.interval - (self.clock() - self.last.get(host, -1e12))))
            self.last[host] = self.clock()
            try:
                with self.open(urllib.request.Request(url, headers=headers), timeout=self.timeout) as response:
                    raw = response.read(self.max_bytes + 1)
                    if len(raw) > self.max_bytes:
                        raise TaskError("response_too_large")
                    content = raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
                    kind = response.headers.get_content_type()
                    if kind == "application/json":
                        content = json.dumps(json.loads(content), ensure_ascii=False, sort_keys=True)
                    elif kind in {"text/html", "application/xhtml+xml"}:
                        parser = Text()
                        parser.feed(content)
                        record["title"] = "".join(parser.titles).strip()
                        content = "\n".join(parser.parts)
                    elif kind in {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}:
                        if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
                            raise TaskError("xml_external_entities_forbidden")
                        tree = ET.fromstring(content)
                        ns = {"atom": "http://www.w3.org/2005/Atom"}
                        if tree.tag.endswith("feed"):
                            record["title"] = tree.findtext("atom:title", default="", namespaces=ns)
                            items = [{"title": node.findtext("atom:title", default="", namespaces=ns)}
                                     for node in tree.findall("atom:entry", ns)]
                        else:
                            record["title"] = tree.findtext("channel/title", default="")
                            items = [{"title": node.findtext("title", default=""),
                                      "link": node.findtext("link", default=""),
                                      "description": node.findtext("description", default="")}
                                     for node in tree.findall("channel/item")]
                        record["extracted_fields"]["items"] = items
                    record.update(content=content, content_hash=content_hash(content), status="ok")
                    record["metadata"].update(etag=response.headers.get("ETag"),
                                               last_modified=response.headers.get("Last-Modified"),
                                               content_type=kind)
                    return record
            except urllib.error.HTTPError as error:
                if error.code == 304 and previous:
                    return {**copy.deepcopy(previous), "collected_at": record["collected_at"], "status": "unchanged"}
                code = "access_denied" if error.code in {401, 403} else f"http_{error.code}"
                delay = retry_after(error.headers.get("Retry-After", 2 ** attempt))
                if error.code not in {429, 500, 502, 503, 504} or delay > 60:
                    record["error_code"] = code
                    return record
            except (urllib.error.URLError, TimeoutError, OSError):
                code, delay = "network_error", 2 ** attempt
            except (ValueError, TaskError, LookupError, ET.ParseError):
                record["error_code"] = "parse_or_size_error"
                return record
            if attempt + 1 < self.attempts:
                # Backoff is interruptible; a stop marker must not wait through 60 seconds of sleep.
                while delay > 0 and not self.should_stop():
                    step = min(1, delay)
                    self.sleep(step)
                    delay -= step
        record["error_code"] = code
        return record
