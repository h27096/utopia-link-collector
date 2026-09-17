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
from urllib.parse import urlsplit, urlunsplit
import subprocess

from discovery import SourceError, discover, hostname, settings as discovery_settings

from dns_verify import options as dns_options, resolves_to_target

USER_AGENT = "UtopiaLinkCollector/1.0"
LIMIT = 1_000_000


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
        # Rate-limit actual page requests, not rejected DNS lookups.
        time.sleep(config["request_delay_seconds"])
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
    deadline = time.monotonic() + config.get("scan_timeout_seconds", 2100)
    state_path = path.with_name("pending_candidates.json")
    if state_path.resolve() == path.resolve():
        raise ValueError("links_file must not be pending_candidates.json")
    pending = load_pending(state_path, config["target_ip"])
    try:
        discovered = discover(config)
    except SourceError:
        if not pending:
            raise
        print("Discovery unavailable; continuing previously discovered pending candidates.", flush=True)
        discovered = []
    candidates = list(dict.fromkeys(pending + discovered))
    print(f"Pending carried forward: {len(pending)} | Fresh discovered: {len(discovered)}", flush=True)
    print(f"Candidates found: {len(candidates)}", flush=True)
    already = rejected = appended = would_append = 0
    selected = candidates if max_candidates is None else candidates[:max_candidates]
    processed = 0
    if not dry_run:
        save_pending(state_path, config["target_ip"], candidates)
    for host in selected:
        remaining = deadline - time.monotonic()
        if remaining < 0.1:
            print("Scan time budget reached; remaining candidates deferred.", flush=True)
            break
        url = f"https://{host}/"
        if path.exists() and url_key(url) in saved_urls(path.read_bytes()):
            already += 1
            processed += 1
            continue
        valid, reason = bounded_verify(host, config, min(config.get("verification_timeout_seconds", 20), remaining))
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
        processed += 1
        if not dry_run and processed % 50 == 0:
            save_pending(state_path, config["target_ip"], candidates[processed:])
    if not dry_run:
        save_pending(state_path, config["target_ip"], candidates[processed:])
    print(f"Already saved: {already} | Rejected/unverified: {rejected} | Newly appended: {appended} | "
          f"Would append: {would_append} | Deferred (time/count limit): {len(candidates) - processed}", flush=True)


def load_pending(path, target):
    if not path.exists():
        return []
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("target_ip") != target:
        return []
    if not isinstance(state.get("hosts"), list):
        raise ValueError("Invalid pending candidate queue")
    return list(dict.fromkeys(host for value in state["hosts"] if (host := hostname(value))))


def save_pending(path, target, hosts):
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"target_ip": target, "hosts": hosts}, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def bounded_verify(host, config, timeout):
    # Child can resolve/fetch only. All file appends remain in this main process.
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--verify-worker", host],
            input=json.dumps(config), capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            return False, "verification worker failed"
        value = json.loads(result.stdout)
        if not isinstance(value, list) or len(value) != 2 or type(value[0]) is not bool or not isinstance(value[1], str):
            return False, "malformed verification result"
        return value[0], value[1]
    except subprocess.TimeoutExpired:
        return False, f"verification deadline ({timeout:.1f}s)"
    except (OSError, ValueError):
        return False, "verification worker unavailable"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discover-only", action="store_true", help="Log merged discovery results without verification or writes")
    parser.add_argument("--max-candidates", type=int)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        address = ipaddress.ip_address(config["target_ip"])
        dns_options(config)
        discovery_settings(config)
        for key, default, maximum in (("scan_timeout_seconds", 2100, 2400), ("verification_timeout_seconds", 20, 60)):
            value = config.get(key, default)
            if type(value) not in (int, float) or not 0.1 <= value <= maximum:
                raise ValueError(f"{key} must be between 0.1 and {maximum}")
        if address.version != 4 or not address.is_global:
            raise ValueError("target_ip must be a public IPv4 address")
        for key in ("timeout_seconds", "request_delay_seconds"):
            if not isinstance(config[key], (int, float)) or not 0 < config[key] <= 120:
                raise ValueError(f"{key} must be greater than zero and at most 120")
        if args.max_candidates is not None and args.max_candidates < 1:
            raise ValueError("--max-candidates must be positive")
        path = args.config.resolve().parent / config["links_file"]
        if args.discover_only:
            discover(config)
            return 0
        scan(config, path, args.dry_run, args.max_candidates)
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, http.client.HTTPException) as error:
        print(f"Scan failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Scan interrupted; previously appended links are retained.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--verify-worker":
        print(json.dumps(verify(sys.argv[2], json.load(sys.stdin))))
    else:
        sys.exit(main())
