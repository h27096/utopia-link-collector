"""Merge bounded, read-only public reverse-IP discovery sources."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SOURCES = ("hackertarget", "urlscan", "otx", "mnemonic")
HACKERTARGET_PAGE_SIZE = 500000
KEYS = {"hackertarget": ("HACKERTARGET_API_KEY", "X-API-Key"),
        "urlscan": ("URLSCAN_API_KEY", "api-key"), "otx": ("OTX_API_KEY", "X-OTX-API-KEY")}
DEFAULTS = {"sources": list(SOURCES), "request_timeout_seconds": 8,
            "source_timeout_seconds": 120, "max_pages_per_source": 100,
            "max_candidates_per_source": 50000, "page_delay_seconds": 2}


def hostname(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower().rstrip(".")
    if len(value) > 253 or len(value.split(".")) < 2:
        return None
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in value.split(".")):
        return None
    try:
        ipaddress.ip_address(value)
        return None
    except ValueError:
        return value


def url_host(value):
    try:
        parsed = urlsplit(value)
        return hostname(parsed.hostname) if parsed.scheme in ("http", "https") else None
    except (ValueError, TypeError):
        return None


def settings(config):
    opts = {**DEFAULTS, **config.get("discovery", {})}
    if not isinstance(opts["sources"], list) or not opts["sources"] or any(s not in SOURCES for s in opts["sources"]):
        raise ValueError("discovery.sources must name supported public sources")
    opts["sources"] = list(dict.fromkeys(opts["sources"]))
    for key, minimum, maximum in (("request_timeout_seconds", 0.1, 30),
                                  ("source_timeout_seconds", 1, 180), ("page_delay_seconds", 0, 30),
                                  ("max_pages_per_source", 1, 1000),
                                  ("max_candidates_per_source", 1000, 1000000)):
        value = opts[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"discovery.{key} must be between {minimum} and {maximum}")
        if key.startswith("max_") and type(value) is not int:
            raise ValueError(f"discovery.{key} must be an integer")
    return opts


class SourceError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an API key to a redirect target.
        return None


def http_worker(request):
    try:
        req = Request(request["url"], headers={"User-Agent": "UtopiaLinkCollector/2.0",
                                               "Accept": "application/json,text/plain", **request["headers"]})
        with build_opener(NoRedirect()).open(req, timeout=request["timeout"]) as response:
            data = response.read(32_000_001)
            if len(data) > 32_000_000:
                return {"error": "response exceeds 32 MB safety limit"}
            return {"text": data.decode("utf-8", errors="replace")}
    except HTTPError as error:
        return {"error": f"HTTP {error.code}; source stopped (quota/access/provider error)"}
    except (OSError, ValueError, http.client.HTTPException):
        return {"error": "request failed or timed out"}


def fetch_text(url, timeout, headers):
    # Killable process also bounds DNS bootstrap, TLS, and slow response bodies.
    # Keys travel through stdin, never command-line arguments or log messages.
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--http-worker"],
            input=json.dumps({"url": url, "timeout": timeout, "headers": headers}),
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            raise SourceError("request worker failed")
        response = json.loads(result.stdout)
        if "error" in response:
            raise SourceError(response["error"])
        return response["text"]
    except subprocess.TimeoutExpired:
        raise SourceError(f"request deadline ({timeout:.1f}s)") from None
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        raise SourceError("invalid response from request worker") from None


@dataclass
class SourceResult:
    name: str
    hosts: set = field(default_factory=set)
    pages: int = 0
    successful: bool = False
    note: str = "complete"


def collect_source(source, target, opts):
    result = SourceResult(source)
    deadline = time.monotonic() + opts["source_timeout_seconds"]
    cursor = None
    offset = 0
    fingerprints = set()
    headers = {}
    if source in KEYS:
        env_name, header = KEYS[source]
        if os.environ.get(env_name):
            headers[header] = os.environ[env_name]
    try:
        for page in range(1, opts["max_pages_per_source"] + 1):
            if page > 1:
                delay = max(opts["page_delay_seconds"], 6.1 if source == "mnemonic" else 0)
                if time.monotonic() + delay >= deadline:
                    raise SourceError("source time budget reached; partial results retained")
                time.sleep(delay)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SourceError("source time budget reached; partial results retained")
            if source == "hackertarget":
                query = {"q": target}
                if page > 1:
                    query["page"] = page
                url = "https://api.hackertarget.com/reverseiplookup/?" + urlencode(query)
            elif source == "urlscan":
                query = {"q": f"page.ip:{target} AND task.visibility:public", "size": 100}
                if cursor:
                    query["search_after"] = cursor
                url = "https://urlscan.io/api/v1/search/?" + urlencode(query)
            elif source == "otx":
                url = f"https://otx.alienvault.com/api/v1/indicators/IPv4/{target}/passive_dns"
            else:
                url = f"https://api.mnemonic.no/pdns/v3/{target}?" + urlencode({"rrType": "A", "limit": 1000, "offset": offset})
            text = fetch_text(url, min(opts["request_timeout_seconds"], remaining), headers)
            result.pages += 1
            fingerprint = hashlib.sha256(text.encode()).hexdigest()
            if fingerprint in fingerprints:
                raise SourceError("repeated page; pagination stopped")
            fingerprints.add(fingerprint)
            next_page = False
            if source == "hackertarget":
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if not lines or any(hostname(line) is None for line in lines):
                    raise SourceError("provider returned empty/error/unexpected text")
                candidates = lines
                # Documented membership pagination is for 500,000-row pages.
                next_page = bool(headers) and len(lines) >= HACKERTARGET_PAGE_SIZE and page < 20
                result.note = "provider-limited snapshot; membership required for larger results" if not headers else "complete"
            else:
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise SourceError("unexpected JSON structure")
                if source == "urlscan":
                    rows = data["results"]
                    if not isinstance(rows, list):
                        raise SourceError("unexpected results structure")
                    candidates = []
                    for row in rows:
                        page_data = row.get("page", {})
                        if page_data.get("ip") == target:
                            candidates.extend([page_data.get("domain"), url_host(page_data.get("url", "")),
                                               url_host(row.get("task", {}).get("url", ""))])
                    # has_more only signifies >10k results, not page exhaustion.
                    sort = rows[-1].get("sort") if rows else None
                    if rows and isinstance(sort, list) and sort:
                        new_cursor = ",".join(str(value) for value in sort)
                        if new_cursor == cursor:
                            raise SourceError("repeated search_after cursor")
                        cursor = new_cursor
                        next_page = True
                    elif rows:
                        result.note = "missing search_after cursor; partial results"
                elif source == "otx":
                    rows = data["passive_dns"]
                    if not isinstance(rows, list):
                        raise SourceError("unexpected passive_dns structure")
                    candidates = [row.get("hostname") for row in rows if row.get("address") == target]
                    result.note = "passive DNS snapshot; endpoint has no documented pagination"
                else:
                    if data.get("responseCode", 200) != 200:
                        raise SourceError("provider reported an API error or resource limit")
                    rows = data["data"]
                    if not isinstance(rows, list):
                        raise SourceError("unexpected passive DNS structure")
                    candidates = [row.get("query") for row in rows
                                  if str(row.get("rrtype", "")).lower() == "a" and row.get("answer") == target]
                    offset += len(rows)
                    total = data.get("count")
                    next_page = bool(rows) and (not isinstance(total, int) or offset < total)
            result.successful = True
            for candidate in candidates:
                host = hostname(candidate)
                if host:
                    result.hosts.add(host)
                if len(result.hosts) >= opts["max_candidates_per_source"]:
                    result.note = "configured candidate safety limit reached; partial results"
                    return result
            if not next_page:
                return result
        result.note = "configured page limit reached; partial results"
    except (SourceError, ValueError, KeyError, TypeError, AttributeError) as error:
        # Do not expose response bodies or API-key material in Actions logs.
        result.note = str(error) if isinstance(error, SourceError) else "malformed provider response; partial results retained"
    return result


def discover(config):
    opts = settings(config)
    results = []
    with ThreadPoolExecutor(max_workers=len(opts["sources"])) as executor:
        futures = [executor.submit(collect_source, source, config["target_ip"], opts) for source in opts["sources"]]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"Discovery {result.name}: {len(result.hosts)} unique candidates | pages: {result.pages} | {result.note}", flush=True)
    merged = set().union(*(result.hosts for result in results))
    print(f"Discovery total unique candidates: {len(merged)} (all sources merged before verification)", flush=True)
    if not any(result.successful for result in results):
        raise SourceError("All discovery sources failed; see per-source diagnostics")
    return sorted(merged)


if __name__ == "__main__":
    if sys.argv[1:] == ["--http-worker"]:
        print(json.dumps(http_worker(json.load(sys.stdin))))
