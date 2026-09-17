"""Bounded DNS verification using the runner resolver and independent DoH fallbacks."""
import argparse
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

RESOLVERS = ("system", "cloudflare", "google")
ENDPOINTS = {
    "cloudflare": "https://cloudflare-dns.com/dns-query",
    "google": "https://dns.google/resolve",
}
RCODES = {1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}


class DNSVerificationError(ValueError):
    pass


def options(config):
    timeout = config.get("dns_timeout_seconds", 1.0)
    budget = config.get("dns_total_timeout_seconds", 3.0)
    attempts = config.get("dns_attempts", 2)
    for name, value, maximum in (("dns_timeout_seconds", timeout, 3),
                                 ("dns_total_timeout_seconds", budget, 10)):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0.1 <= value <= maximum:
            raise ValueError(f"{name} must be between 0.1 and {maximum}")
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise ValueError("dns_attempts must be an integer between 1 and 3")
    return timeout, budget, attempts


def parse_doh(answer, host):
    if not isinstance(answer, dict) or type(answer.get("Status")) is not int:
        raise ValueError("malformed DNS status")
    if type(answer.get("TC", False)) is not bool:
        raise ValueError("malformed truncation flag")
    if answer.get("TC"):
        raise ValueError("truncated response")
    status = answer["Status"]
    if status == 3:
        return {"status": "nxdomain", "detail": "NXDOMAIN (hostname does not exist)"}
    if status != 0:
        raise ValueError(RCODES.get(status, f"RCODE {status}"))
    records = answer.get("Answer", [])
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise ValueError("malformed DNS answer")
    # Only accept A records belonging to the queried name or its CNAME chain.
    reachable = {host.lower().rstrip(".")}
    for _ in range(16):
        aliases = {str(item.get("data", "")).lower().rstrip(".") for item in records
                   if item.get("type") == 5 and str(item.get("name", "")).lower().rstrip(".") in reachable}
        if aliases <= reachable:
            break
        reachable.update(aliases)
    addresses = set()
    for item in records:
        if item.get("type") == 1 and str(item.get("name", "")).lower().rstrip(".") in reachable:
            address = ipaddress.ip_address(item.get("data", ""))
            if address.version != 4:
                raise ValueError("invalid IPv4 answer")
            addresses.add(str(address))
    if addresses:
        return {"status": "ok", "addresses": sorted(addresses)}
    if len(reachable) > 1:
        raise ValueError("incomplete CNAME answer (no terminal A record)")
    return {"status": "nodata", "detail": "NODATA (no IPv4 A record)"}


def query_once(resolver, host, timeout):
    """Runs only in a killable worker: OS DNS has no portable Python timeout."""
    try:
        if resolver == "system":
            records = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
            addresses = sorted({str(ipaddress.IPv4Address(record[4][0])) for record in records})
            return {"status": "ok", "addresses": addresses} if addresses else {
                "status": "nodata", "detail": "NODATA (no IPv4 A record)"}
        url = ENDPOINTS[resolver] + "?" + urlencode({"name": host, "type": "A"})
        request = Request(url, headers={"User-Agent": "UtopiaLinkCollector/1.1",
                                       "Accept": "application/dns-json"})
        with urlopen(request, timeout=timeout) as response:
            body = response.read(65_537)
            if len(body) > 65_536:
                raise ValueError("DNS response exceeds size limit")
            return parse_doh(json.loads(body), host)
    except socket.gaierror as error:
        if error.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)):
            return {"status": "nxdomain", "detail": "NXDOMAIN/no address from system resolver"}
        return {"status": "error", "detail": f"system resolver error {error.errno}"}
    except HTTPError as error:
        return {"status": "error", "detail": f"HTTP {error.code}"}
    except (TimeoutError, socket.timeout):
        return {"status": "error", "detail": "timeout"}
    except (OSError, ValueError, http.client.HTTPException) as error:
        return {"status": "error", "detail": str(error)[:160]}


def bounded_query(resolver, host, timeout):
    # subprocess.run kills and reaps a timed-out worker. A timed-out thread or
    # socket.setdefaulttimeout cannot bound a hanging OS getaddrinfo call.
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                 "--worker", resolver, host, str(timeout)],
                                capture_output=True, text=True, timeout=timeout,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            return {"status": "error", "detail": "resolver worker failed"}
        answer = json.loads(result.stdout)
        if not isinstance(answer, dict) or answer.get("status") not in ("ok", "nxdomain", "nodata", "error"):
            raise ValueError("malformed worker result")
        return answer
    except subprocess.TimeoutExpired:
        return {"status": "error", "detail": f"timeout ({timeout:.2f}s)"}
    except (OSError, ValueError):
        return {"status": "error", "detail": "resolver worker unavailable or malformed"}


def resolves_to_target(host, config):
    timeout, budget, attempts = options(config)
    target = str(ipaddress.IPv4Address(config["target_ip"]))
    deadline = time.monotonic() + budget
    retry = list(RESOLVERS)
    details = []
    for _ in range(attempts):
        next_retry = []
        for resolver in retry:
            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                raise DNSVerificationError(f"DNS budget exhausted ({budget:g}s): " + "; ".join(details))
            answer = bounded_query(resolver, host, min(timeout, remaining))
            if answer["status"] == "ok":
                addresses = {str(ipaddress.IPv4Address(item)) for item in answer.get("addresses", [])}
                if addresses:
                    # A successful answer for a different IP is a rejection,
                    # never permission to bypass the target-IP requirement.
                    return target in addresses
                answer = {"status": "nodata", "detail": "NODATA (no IPv4 A record)"}
            details.append(f"{resolver}: {answer.get('detail', answer['status'])}")
            # Try independent resolvers before retrying any transient failure.
            # Never repeatedly retry definitive negative answers.
            if answer["status"] == "error":
                next_retry.append(resolver)
        retry = next_retry
        if not retry:
            break
    raise DNSVerificationError("DNS unverified: " + "; ".join(details))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("--target", default="104.218.50.66")
    args = parser.parse_args()
    start = time.monotonic()
    try:
        matched = resolves_to_target(args.host, {"target_ip": args.target})
        print(f"DNS target matched: {matched} ({time.monotonic() - start:.2f}s)")
        return 0 if matched else 1
    except DNSVerificationError as error:
        print(f"{error} ({time.monotonic() - start:.2f}s)")
        return 1


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--worker":
        print(json.dumps(query_once(sys.argv[2], sys.argv[3], float(sys.argv[4]))))
    else:
        sys.exit(main())
