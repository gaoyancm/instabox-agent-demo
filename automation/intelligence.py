"""Public-only website intelligence. No private endpoint discovery or order scraping."""
import hashlib
import json
import os
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import httpx
from .common import TaskError, atomic_json, utcnow
from .storage import Store, content_hash
from .robots import RobotsPolicy

AGENT = "InstaBoxPublicResearch/1.0"
PRIVATE = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:admin|account|orders|checkout|checkouts|cart|services|api)(?:/|$)", re.I)


class PublicPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = []
        self.in_title = False
        self.title_complete = False
        self.links = set()
        self.description = ""
        self.product_data = []
        self.jsonld = False
        self.buffer = []
        self.anchor = None
        self.anchor_parts = []
        self.product_links = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title" and not self.title_complete:
            self.in_title = True
        if tag == "a" and attrs.get("href"):
            self.links.add(attrs["href"])
            if urlsplit(attrs["href"]).path.startswith("/products/"):
                self.anchor, self.anchor_parts = attrs["href"], []
        if tag == "img" and self.anchor and attrs.get("alt"):
            self.anchor_parts.append(attrs["alt"])
        if tag == "meta" and attrs.get("name") == "description":
            self.description = attrs.get("content", "")[:400]
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.jsonld, self.buffer = True, []

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor:
            text = " ".join(self.anchor_parts).strip()[:400]
            if text:
                self.product_links[self.anchor] = text
            self.anchor = None
        if tag == "title":
            if self.in_title:
                self.title_complete = True
            self.in_title = False
        if tag == "script" and self.jsonld:
            try:
                self.product_data.append(json.loads("".join(self.buffer)))
            except ValueError:
                pass
            self.jsonld = False

    def handle_data(self, data):
        if self.anchor and data.strip():
            self.anchor_parts.append(data.strip())
        if self.in_title:
            self.title.append(data)
        if self.jsonld:
            self.buffer.append(data)


def authorized_domains():
    """Domains the operator explicitly authorized, e.g. PUBLIC_RESEARCH_DOMAINS=example.com."""
    return {d.strip().lower() for d in os.environ.get("PUBLIC_RESEARCH_DOMAINS", "example.com").split(",") if d.strip()}


def collect_public_site(output, *, domain=None, estimate_providers=(), transport=None, sleep=time.sleep):
    # Only an explicitly authorized domain is crawled, never arbitrary hosts or backend paths.
    domain = (domain or os.environ.get("PUBLIC_RESEARCH_DOMAIN", "example.com")).lower()
    if domain not in authorized_domains():
        raise TaskError("public_target_not_authorized")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    base = "https://" + domain
    evidence = []
    observations = []
    with httpx.Client(timeout=25, follow_redirects=False, trust_env=False, transport=transport,
                      headers={"User-Agent": AGENT}) as client:
        robots_response = client.get(base + "/robots.txt")
        evidence.append({"url": base + "/robots.txt", "http_status": robots_response.status_code})
        if robots_response.status_code != 200:
            raise TaskError("robots_policy_unavailable")
        rules = RobotsPolicy(robots_response.text, AGENT)
        # External instructions in comments are data only; no skills, apps or shopping tools are installed.
        policy = {"source": base + "/robots.txt", "sha256": hashlib.sha256(robots_response.content).hexdigest(),
                  "home_allowed": rules.can_fetch(base + "/"),
                  "admin_disallowed": not rules.can_fetch(base + "/admin"),
                  "orders_disallowed": not rules.can_fetch(base + "/orders")}
        if not policy["home_allowed"]:
            raise TaskError("public_crawl_disallowed")
        queue = [base + "/"]
        visited = set()
        platform = "unconfirmed"
        throttled = None
        for _ in range(4):
            if not queue:
                break
            url = queue.pop(0)
            parts = urlsplit(url)
            if parts.netloc != domain or parts.query or PRIVATE.match(parts.path) or not rules.can_fetch(url):
                raise TaskError("private_or_disallowed_path")
            sleep(1)
            response = client.get(url)
            evidence.append({"url": url, "http_status": response.status_code})
            if response.status_code == 429:
                throttled = {"error_code": "http_429", "retry_after": response.headers.get("Retry-After"),
                             "action": "stopped_requests_to_target_host"}
                break
            if response.status_code != 200:
                continue
            if "text/html" not in response.headers.get("Content-Type", ""):
                continue
            if len(response.content) > 4_000_000:
                raise TaskError("public_page_too_large")
            parser = PublicPage()
            parser.feed(response.text)
            title = "".join(parser.title).strip()
            if re.search(r"captcha|access denied|verify you are human", title, re.I):
                raise TaskError("public_access_control_encountered")
            if "cdn.shopify.com" in response.text or "Shopify.shop" in response.text:
                platform = "Shopify_public_html_indicators"
            facts = []
            def walk(value):
                if isinstance(value, list):
                    for item in value:
                        walk(item)
                elif isinstance(value, dict):
                    if value.get("@type") == "Product":
                        offers = value.get("offers", [])
                        offers = offers if isinstance(offers, list) else [offers]
                        facts.append({"name": value.get("name"), "offers": [
                            {key: offer.get(key) for key in ["price", "lowPrice", "highPrice", "priceCurrency", "availability"]
                             if key in offer} for offer in offers if isinstance(offer, dict)]})
                    for key in ["@graph", "mainEntity"]:
                        if key in value:
                            walk(value[key])
            for value in parser.product_data:
                walk(value)
            unique_links = {}
            for href, text in sorted(parser.product_links.items()):
                linked = urljoin(base, href).split("#", 1)[0]
                if urlsplit(linked).netloc == domain and not urlsplit(linked).query:
                    previous = unique_links.get(linked, {}).get("public_label", "")
                    if len(text) >= len(previous):
                        unique_links[linked] = {"url": linked, "public_label": text,
                                              "display_prices": re.findall(r"[£€$]\s*\d+(?:[.,]\d{2})?", text)}
            product_links = list(unique_links.values())
            content = json.dumps({"title": title, "products": facts, "product_links": product_links},
                                 ensure_ascii=False, sort_keys=True)
            observations.append({"source_url": url, "source_name": f"{domain} public storefront",
                "collected_at": utcnow().isoformat(), "title": title, "content": content,
                "extracted_fields": {"public_products": facts, "public_product_links": product_links},
                "content_hash": content_hash(content),
                "status": "ok", "error_code": None,
                "metadata": {"data_class": "public_observation", "platform": platform,
                             "permission_basis": "User-authorized public research and robots-allowed HTML",
                             "private_data_accessed": False}})
            visited.add(url)
            if len(visited) == 1:
                candidates = sorted({urljoin(base, link).split("#", 1)[0] for link in parser.links
                    if urlsplit(urljoin(base, link)).netloc == domain
                    and urlsplit(urljoin(base, link)).path.startswith("/products/")
                    and not urlsplit(urljoin(base, link)).query})
                queue.extend([candidate for candidate in candidates if candidate not in visited][:3])

        providers = []
        # Optional public third-party estimate pages: (name, url template with {domain}).
        for name, template in estimate_providers:
            url = template.format(domain=domain)
            try:
                response = client.get(url)
                providers.append({"provider": name, "source_url": url, "http_status": response.status_code,
                                  "metrics_verified": False, "data_class": "third_party_estimate",
                                  "reason": "No independently verifiable domain metrics extracted; no login/paywall bypass"})
            except httpx.HTTPError:
                providers.append({"provider": name, "source_url": url, "metrics_verified": False,
                                  "data_class": "third_party_estimate", "reason": "network_error"})
    metrics = {}
    for name in ["visits", "unique_visitors", "traffic_sources", "referring_domains", "visit_hours",
                 "daily_trend", "weekly_pattern", "monthly_trend", "order_count", "sales_revenue", "conversion_rate"]:
        metrics[name] = {"value": None, "period_start": None, "period_end": None, "time_zone": None,
                         "availability": "not_publicly_verified",
                         "required_access": "owner_authorized_analytics_or_order_API"}
    estimates = {name: {"value": None, "period_start": None, "period_end": None,
                        "time_zone": None, "data_class": "third_party_estimate",
                        "availability": "no_verifiable_public_provider_report",
                        "possible_source": "public_provider_report_or_separately_authorized_licensed_API"}
                 for name in ["visits", "traffic_sources", "referring_domains", "daily_trend",
                              "monthly_trend", "geographic_distribution"]}
    report = {"domain": domain, "collected_at": utcnow().isoformat(), "scope": "third_party_public_only",
              "platform_indication": platform, "robots_policy": policy, "request_evidence": evidence,
              "public_pages_collected": len(observations), "third_party_providers": providers,
              "target_rate_limit": throttled,
              "business_metrics": metrics, "private_endpoints_requested": [],
              "estimated_metrics": estimates,
              "interpretation": "Unknown values are not zero visits or zero orders; reviews and product stock are not order counts"}
    store = Store(output / "store")
    try:
        task = "public-" + utcnow().strftime("%Y%m%d-%H%M%S")
        for record in observations:
            store.put(task, record)
        store.export(task, output / "public-results.jsonl")
        store.backup(output / "public-backup.sqlite")
    finally:
        store.close()
    atomic_json(output / "public-report.json", report)
    return report
