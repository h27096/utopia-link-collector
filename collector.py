"""Public reverse-IP discovery with conservative checks and append-only storage."""
import argparse
from contextlib import contextmanager
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import time
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

USER_AGENT = "UtopiaLinkCollector/1.0"
LIMIT = 1_000_000


def get_text(url, timeout):
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout) as response:
        data = response.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError("response exceeds size limit")
        return data.decode("utf-8", errors="replace")


def hostname(value):
    value = value.strip().lower().rstrip(".")
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2:
        return None
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in labels):
        return None
    try:
        ipaddress.ip_address(value)
        return None
    except ValueError:
        return value


def discover(config):
    url = "https://api.hackertarget.com/reverseiplookup/?" + urlencode({"q": config["target_ip"]})
    body = get_text(url, config["timeout_seconds"])
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        raise ValueError("discovery returned an empty response")
    if any(hostname(line) is None for line in lines):
        raise ValueError("discovery provider returned an error or unexpected response: " + repr(body[:200]))
    return sorted({hostname(line) for line in lines})


def resolves_to_target(host, config):
    url = "https://dns.google/resolve?" + urlencode({"name": host, "type": "A", "edns_client_subnet": "0.0.0.0/0"})
    answer = json.loads(get_text(url, config["timeout_seconds"]))
    if answer.get("Status") != 0 or answer.get("TC"):
        raise ValueError("DNS lookup failed or was truncated")
    return any(record.get("type") == 1 and record.get("data") == config["target_ip"]
               for record in answer.get("Answer", []))


class BrandingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_title = False
        self.title = []
        self.site_names = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "meta" and (attrs.get("property", "").lower() == "og:site_name"
                              or attrs.get("name", "").lower() == "application-name"):
            self.site_names.append(attrs.get("content", ""))

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)

    def matches(self):
        return any(re.search(r"\butopia\b", value, re.I)
                   for value in ["".join(self.title)] + self.site_names)


def fetch_page(host, config):
    # Pin the connection to the configured public IP; retain hostname TLS/SNI checks.
    connection = http.client.HTTPSConnection(host, timeout=config["timeout_seconds"])
    raw = socket.create_connection((config["target_ip"], 443), config["timeout_seconds"])
    try:
        connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        connection.request("GET", "/", headers={"User-Agent": USER_AGENT, "Accept": "text/html", "Accept-Encoding": "identity"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"HTTPS status {response.status} (redirects are not followed)")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() not in ("text/html", "application/xhtml+xml"):
            raise ValueError("response is not HTML")
        body = response.read(LIMIT + 1)
        if len(body) > LIMIT:
            raise ValueError("page exceeds size limit")
        return body.decode("utf-8", errors="replace")
    finally:
        connection.close()
        raw.close()


def verify(host, config):
    try:
        if not resolves_to_target(host, config):
            return False, "current public DNS does not include target IP"
        parser = BrandingParser()
        parser.feed(fetch_page(host, config))
        if not parser.matches():
            return False, "no Utopia title/application branding"
        return True, "DNS, valid HTTPS, and Utopia branding matched"
    except (OSError, ValueError, http.client.HTTPException) as error:
        return False, str(error)


def url_key(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return url
        port = parsed.port
        host = parsed.hostname.lower().rstrip(".")
        if port and not ((parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)):
            host += f":{port}"
        return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, parsed.fragment))
    except ValueError:
        return url


def saved_urls(data):
    # Recognize URLs even in comments or other manually written text.
    text = data.decode("utf-8-sig", errors="replace")
    return {url_key(match.rstrip(".,;!)]}>")) for match in re.findall(r"https?://[^\s<>\"']+", text, re.I)}


@contextmanager
def file_lock(path):
    lock = path.with_name(path.name + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise RuntimeError(f"Another collector may be writing: {lock}. See README for stale locks.") from None
    try:
        os.close(fd)
        yield
    finally:
        lock.unlink()


def append_new(path, url):
    """Re-read immediately before each append. Never rewrite existing bytes."""
    with file_lock(path):
        with path.open("a+b") as stream:
            stream.seek(0)
            before = stream.read()
            if url_key(url) in saved_urls(before):
                return False
            separator = b"" if not before or before.endswith((b"\n", b"\r")) else b"\n"
            stream.write(separator + url.encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
            return True


def scan(config, path, dry_run=False, max_candidates=None):
    candidates = discover(config)
    print(f"Candidates found: {len(candidates)}", flush=True)
    already = rejected = appended = would_append = 0
    selected = candidates if max_candidates is None else candidates[:max_candidates]
    for host in selected:
        url = f"https://{host}/"
        if path.exists() and url_key(url) in saved_urls(path.read_bytes()):
            already += 1
            continue
        valid, reason = verify(host, config)
        if not valid:
            rejected += 1
            print(f"Unverified {host}: {reason}", flush=True)
        elif dry_run:
            would_append += 1
            print(f"Would append: {url}", flush=True)
        elif append_new(path, url):
            appended += 1
            print(f"Appended: {url}", flush=True)
        else:
            already += 1
        time.sleep(config["request_delay_seconds"])
    print(f"Already saved: {already} | Rejected/unverified: {rejected} | Newly appended: {appended} | "
          f"Would append: {would_append} | Deferred by limit: {len(candidates) - len(selected)}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-candidates", type=int)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        address = ipaddress.ip_address(config["target_ip"])
        if address.version != 4 or not address.is_global:
            raise ValueError("target_ip must be a public IPv4 address")
        for key in ("timeout_seconds", "request_delay_seconds"):
            if not isinstance(config[key], (int, float)) or not 0 < config[key] <= 120:
                raise ValueError(f"{key} must be greater than zero and at most 120")
        if args.max_candidates is not None and args.max_candidates < 1:
            raise ValueError("--max-candidates must be positive")
        path = args.config.resolve().parent / config["links_file"]
        scan(config, path, args.dry_run, args.max_candidates)
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, http.client.HTTPException) as error:
        print(f"Scan failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Scan interrupted; previously appended links are retained.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
