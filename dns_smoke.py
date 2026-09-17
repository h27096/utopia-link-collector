"""Read-only live DNS checks for GitHub Actions; never appends links."""
import json
from pathlib import Path
import time

from dns_verify import DNSVerificationError, RESOLVERS, bounded_query, resolves_to_target


def main():
    config = json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8"))
    target = config["target_ip"]
    # Public wildcard-DNS control, not a seed for discovery or an approved link.
    control = target + ".nip.io"
    start = time.monotonic()
    matched = resolves_to_target(control, config)
    print(f"Positive DNS control: matched={matched}, elapsed={time.monotonic() - start:.2f}s", flush=True)
    if not matched:
        raise RuntimeError("Public DNS control did not resolve to configured IP")
    for resolver in RESOLVERS:
        print(f"Resolver diagnostic {resolver}: {bounded_query(resolver, control, 1.0)}", flush=True)
    # Reserved invalid TLD must never authorize an append.
    start = time.monotonic()
    try:
        negative = resolves_to_target("utopia-collector-negative.invalid", config)
    except DNSVerificationError as error:
        negative = False
        print(f"Negative DNS control: {error}", flush=True)
    if negative:
        raise RuntimeError("Invalid hostname incorrectly accepted")
    print(f"Negative control rejected in {time.monotonic() - start:.2f}s", flush=True)
    # Samples from the reported failing run, for diagnosis only. They are not
    # discovery seeds and may legitimately have disappeared since that run.
    for host in ("cluckluck.mooo.com", "clever.read.sonlightsoftware.com"):
        start = time.monotonic()
        try:
            outcome = f"target matched={resolves_to_target(host, config)}"
        except DNSVerificationError as error:
            outcome = str(error)
        print(f"Candidate diagnostic {host}: {outcome} ({time.monotonic() - start:.2f}s)", flush=True)
    print("Read-only DNS smoke test passed; links.txt was not opened for writing.", flush=True)


if __name__ == "__main__":
    main()
