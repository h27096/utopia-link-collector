# Public hostname discovery

The 500-host batch observed previously was a HackerTarget provider response, not a local `[:500]` slice. That working request is retained exactly for the first page. The collector now queries four public providers and merges their hostnames before DNS and HTTPS verification. It never submits scans, scrapes Discord, or uses private-source data.

| Source | Data and pagination | Optional GitHub Actions secret |
| --- | --- | --- |
| HackerTarget | Existing reverse-IP lookup. Membership pagination uses documented `page` values after a full 500,000-row provider page; free snapshots cannot be expanded by inventing pages. | `HACKERTARGET_API_KEY` |
| urlscan | Public historical scans matching `page.ip`. Follows the last result's `sort` value using `search_after`, even for short pages or when `has_more` is false (that flag refers to the 10,000-result total-count threshold). | `URLSCAN_API_KEY` |
| OTX | IPv4 passive DNS snapshot; accepts hostnames explicitly associated with the target address. This endpoint has no documented pagination. | `OTX_API_KEY` |
| mnemonic | Public A-record history for the IP; follows `limit`/`offset` until the reported record count is exhausted. Public pages request at most 1,000 records, fall back to 100 or 10 if the server rejects the page size, and are spaced at least 6.1 seconds apart. | None required |

References: [HackerTarget reverse IP](https://hackertarget.com/reverse-ip-lookup/), [urlscan search API](https://urlscan.io/docs/api/), [OTX official SDK](https://github.com/AlienVault-OTX/OTX-Python-SDK), [mnemonic public API](https://www.docs.mnemonic.no/api/services/pdns/01-public_api.html).

No additional credentials are necessary to attempt public access. Providers may restrict anonymous access, apply subscription result caps, return no relevant records, or exhaust shared-runner quotas. Add your own provider keys through repository **Settings > Secrets and variables > Actions**, never in code or chat, for higher authorized quotas. The workflow passes keys only to the discovery step. We do not bypass quotas, rotate identities, or buy subscriptions. Keys go in provider-specific headers, and redirects are not followed.

## Counts and partial results

Actions prints one line for every enabled source, for example:

```text
Discovery hackertarget: 500 unique candidates | pages: 1 | provider-limited snapshot...
Discovery urlscan: 1250 unique candidates | pages: 30 | complete
Discovery otx: 800 unique candidates | pages: 1 | passive DNS snapshot...
Discovery mnemonic: 3200 unique candidates | pages: 4 | complete
Discovery total unique candidates: 4100 (all sources merged before verification)
```

These numbers are illustrative, not a claim about current provider data. Each source counts its own distinct valid hostnames; the combined total removes only duplicate **candidates in memory**, not saved links. `pages` counts responses received, including a final empty or malformed page. A source outage, malformed response, quota error, repeated page/cursor, or safety bound is reported. Earlier valid pages from that source and other providers still contribute. If every source fails and no deferred work exists, the scan exits with an error rather than reporting successful empty discovery.

Only currently verified domains are appended: DNS must include `104.218.50.66`, and existing HTTPS and Utopia-branding checks remain required. More historical candidates does not guarantee more currently live Utopia links.

## Bounds and continuation

Defaults in `config.json`:

- 50,000 unique candidates **per source**, configurable up to 1,000,000. No global 500-host cap.
- 100 pages per source, configurable up to 1,000.
- Eight-second hard request deadline and 32 MB response-size ceiling.
- 120-second source budget (sources run concurrently), configurable up to 180 seconds.
- Two-second page spacing, with mnemonic's longer public rate spacing enforced.
- 35-minute scan deadline including discovery, leaving time within the existing Actions step/job for saving and Docs sync.
- 20-second full-verification deadline per candidate, in addition to the existing one-second DNS attempts and three-second DNS budget.

Request and verification workers are terminated on timeout. These bounds include network connection and response-body waits; allow small process startup/cleanup overhead. Timeouts and transient server failures get at most one retry per page after a two-second pause, within the same source budget. Rate-limit and access errors stop that source without repeated retries. No source can prevent the others from returning candidates.

`pending_candidates.json` stores merged but unprocessed hostnames for the configured IP. The main process checkpoints the queue before verification, periodically, and when the scan finishes. Later runs process it ahead of newly discovered candidates. The existing Actions save step commits the queue alongside `links.txt`; it never force-pushes. An interrupted run can safely recheck some candidates because the unchanged append function re-reads `links.txt` before every append. A user-cancelled Actions run may skip the save step, as before; the last committed queue remains available.

Discovery pagination restarts from the provider's first page each run. Queue continuation is for **already discovered candidates**, not an assertion that every historical provider page was retrieved. If a source repeatedly hits its page/time bound, the log says so; increase that bound within the allowed limits or use an authorized provider plan. Large result sets, access limits, and unavailable sources may prevent exhaustive coverage.

To diagnose without verification or writes:

```powershell
python collector.py --discover-only
```

`--dry-run` performs verification but writes neither links nor queue. `--max-candidates N` limits verification only; it does not truncate source discovery. All source settings are under `discovery` in `config.json`.

## Preservation and tests

The `append_new` implementation and Google Docs sync code are unchanged. Existing saved lines, duplicates, ordering, and bytes are never cleaned up or rewritten. The existing Actions schedule, concurrency, DNS checks, Google credentials, and Docs steps are retained. Only optional discovery secrets and queue checkpointing are added to the collection workflow.

Tests exercise discovery beyond 500 hosts, pagination, overlaps, provider failures, malformed responses, repeated cursors/pages, rate limits, process deadlines, queue resumption, and saved-byte preservation. GitHub CI runs an additional read-only discovery smoke check with 20 pages and 75 seconds per source; it does not append any links or write to Google Docs.
