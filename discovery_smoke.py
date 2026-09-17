"""Read-only discovery diagnostics; no DNS verification, appends, or Docs writes."""
import json
from pathlib import Path
import time

from discovery import discover


if __name__ == "__main__":
    config = json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8"))
    # Exercise pagination but keep CI within its existing five-minute job limit.
    config["discovery"] = {**config.get("discovery", {}), "max_pages_per_source": 20, "source_timeout_seconds": 75}
    start = time.monotonic()
    candidates = discover(config)
    print(f"Discovery smoke: {len(candidates)} unique candidates in {time.monotonic() - start:.2f}s; no files modified.")
