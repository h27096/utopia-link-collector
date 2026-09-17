# Utopia Link Collector V1

Discovers publicly indexed hostnames associated with `104.218.50.66`, checks them, and appends new verified HTTPS URLs to `links.txt`. The core collector uses Python 3.10+ with no external dependencies, accounts, or tokens. Optional Google Docs uploads use a Google service account and the dependencies in `requirements-docs.txt`. No Discord access or scraping.

## Setup and run

Install Python 3.10 or newer, open a terminal in this folder, and run:

```powershell
python collector.py --dry-run
python collector.py
```

On Windows, `py` can be used instead of `python` if installed with the Python launcher. No `pip install` is needed. A scan runs once and exits, with a 35-minute scan budget including discovery.

```powershell
python collector.py --dry-run --max-candidates 5
python -m unittest -v
python collector.py --config config.json
```

The optional candidate limit processes N candidates, with previously deferred candidates first. Normal runs save remaining candidates to a separate queue for the next run; dry runs never change that queue. Omit it for a full scan. Dry runs perform network checks but do not create or modify the links file.

## How discovery and verification work

1. Query HackerTarget reverse IP, urlscan public scan search, OTX passive DNS, and mnemonic public passive DNS. Merge normalized hostnames from all sources before verification; the Actions log shows each source's unique count and the combined unique count. See [discovery sources and limits](DISCOVERY.md).
2. Resolve each unsaved hostname through the runner's system resolver first, then [Cloudflare DNS over HTTPS](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/) and [Google Public DNS over HTTPS](https://developers.google.com/speed/public-dns/docs/doh/json) if necessary. A successful IPv4 answer must include the configured target IP. A DNS error never authorizes a link.
3. Fetch only `/` over HTTPS directly from that IP, using the candidate hostname for TLS certificate verification, SNI, and the Host header. Require HTTP 200 and HTML, with a one-megabyte response limit. Redirects, invalid certificates, inaccessible sites, and non-HTML responses remain unverified. No insecure HTTP fallback.
4. Require the whole word `Utopia` in the HTML title, `og:site_name`, or `application-name` metadata. A body-text mention alone is insufficient.

These checks are conservative heuristics, not proof of ownership or an official Utopia fingerprint. Other sites could use the same name; real Utopia sites with disguised titles, JavaScript-rendered branding, redirects, or broken HTTPS can be missed. The provided IP comes from your request. No example domains are hard-coded as discoveries. The collector does not execute JavaScript or bypass access challenges.

## Append-only guarantee

Immediately before each append, the collector locks the output for other collector processes and re-reads the current file. If the URL is present, it does nothing. Existing duplicate lines, comments, ordering, line endings, and bytes are preserved. It never opens the file in truncate/write mode.

For a genuinely new URL it appends one line. If the old file lacks a final newline, it appends a separating newline first; all original bytes remain intact. URLs inside manually written text are recognized, too. Comparison normalizes hostname case, default ports, and an empty root path; HTTP and HTTPS remain different URLs. Prefer one full URL per line in UTF-8 text. Existing arbitrary bytes are preserved, but UTF-16 files are not supported for duplicate recognition.

The `.lock` file coordinates instances of this collector. Avoid editing the file in another application during the brief append operation because other editors do not honor this lock. If a process is forcibly terminated and leaves a stale `links.txt.lock`, confirm no collector is running before removing only that lock file. Ordinary errors release it automatically.

## Expanded public discovery

The collector retains HackerTarget and adds three public sources. urlscan uses `search_after`; mnemonic uses `offset`; HackerTarget supports documented membership pagination when applicable. OTX passive DNS is a single snapshot. There is no overall 500-candidate ceiling: the default safety bound is 50,000 unique candidates per source, up to 100 pages and 120 seconds per source. Provider subscription limits still apply and cannot be removed by this client.

`pending_candidates.json` holds deferred work separately from `links.txt`. It is committed by the existing collection job so later runs can resume. Discovery failures are isolated; already obtained candidates are retained. The scan has a 35-minute budget, each full candidate verification is capped at 20 seconds, and the existing short DNS deadlines remain unchanged. The job exits in time for the existing save and Google Docs steps. See [DISCOVERY.md](DISCOVERY.md) for credentials, logs, exact limits, and tradeoffs.

## Configuration and output

`config.json` controls the target public IPv4 address, links filename, page/discovery network timeout, separate DNS deadlines, and pause before page requests. The links path is resolved relative to the config file, so running from another folder still uses the correct list. Its parent directory must exist. TLS checks cannot be disabled through configuration. Page/discovery socket timeouts are per operation, not a total scan deadline. DNS has its own total deadline described below.

Console output includes candidates found, already saved, rejected/unverified with reasons, newly appended, dry-run matches, and deferred candidates. Saved links are skipped without rechecking availability; the collector never cleans up old links. Exit code 0 means discovery and processing completed, even if nothing could be verified. Discovery/configuration/storage failures exit 1; an interrupted scan exits 130. Previously completed appends remain saved if a later operation fails.

## DNS reliability and speed

DNS no longer depends on a single Google JSON response or the 10-second page timeout. Defaults:

| Setting | Default | Meaning |
| --- | --- | --- |
| `dns_timeout_seconds` | 1.0 | Maximum time for each resolver worker, including OS DNS and HTTPS setup |
| `dns_total_timeout_seconds` | 3.0 | Shared deadline across all resolver attempts for one hostname |
| `dns_attempts` | 2 | At most two passes; retry only transient failures if time remains |

The system resolver, Cloudflare, and Google are tried in that order. Fallbacks are tried before retries. NXDOMAIN (name does not exist) and NODATA (no IPv4 A record) are not retried against the same resolver. SERVFAIL, REFUSED, truncation, invalid responses, HTTP failures, and timeouts can fall back or retry within the same total budget. Logs identify the resolver and outcome instead of treating every negative answer as “failed or truncated.” Stale public-index hostnames can still be rejected correctly.

Worker processes are killed and reaped on timeout, so a hanging OS resolver cannot leave an unlimited background thread running or delay shutdown. The deadline excludes small process start/cleanup overhead. DNS-over-HTTPS certificate checks stay enabled, DNSSEC checking is not disabled, and A records must belong to the requested hostname or its CNAME chain.

The one-second page-request pause is applied only after DNS matches the target. DNS-only rejections no longer incur an extra one-second sleep. HTTPS and branding checks still follow DNS verification, and the append-only writer is unchanged.

For a read-only diagnostic, run `python dns_verify.py hostname.example`. The separate **Verify collector DNS** Actions workflow runs the offline suite and live positive/negative DNS controls on an Ubuntu GitHub runner when DNS code changes. It never writes to `links.txt` or Google Docs. `python dns_smoke.py` runs the same network checks locally.

## Hourly scans on GitHub

The included [GitHub Actions workflow](.github/workflows/collect.yml) schedules a scan at minute 0 of every hour (UTC). It checks the latest main branch, runs the tests, scans public sources, and commits only additions to links.txt when new links are found. Existing lines and duplicates stay unchanged. It uses GitHub's automatic token; no personal token or Discord credentials are needed.

View runs under **Actions > Collect Utopia links**. Use **Run workflow** there for a manual scan. GitHub can delay or occasionally drop scheduled runs during heavy load, especially at the start of the hour; this is a schedule, not an exact-time guarantee. In public repositories, GitHub may disable scheduled workflows after 60 days without repository activity. See [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Runs cannot overlap. Each scan has a 45-minute limit, and the job has a 55-minute limit. Completed additions are saved even if the scan later reports an error. Pushes never force-overwrite concurrent changes: if someone edits main during a scan, the push may fail and a later scan can rediscover those links. Branch rules must permit the workflow to commit to main. Provider quotas still apply; an hourly schedule makes up to 24 discovery calls per day from GitHub's shared runners, so rate limits can occur.

## Google Docs uploads

The hourly workflow appends missing saved links to your [Google Doc](https://docs.google.com/document/d/1hKE01p0Wy2jjBw5oi0f6-PgVUj4sizWw9SZI6Cu1Jgo/edit). Follow [the one-time Google Docs access setup](GOOGLE_DOCS_SETUP.md) to activate uploads. It checks the document before each batch and appends only missing URLs, preserving existing text and duplicates. Revision guards reject writes if the document changes during the check. It checks all saved links each run so missed uploads can catch up.

Until credentials are supplied, collection continues in `links.txt` and Docs uploads report pending setup. The hourly workflow no longer runs the Sheets uploader.

## Tests

`python -m unittest -v` runs offline tests covering preservation of duplicate lines and original bytes, missing final newline, URL comparison, locking, discovery error handling, conservative branding, DNS/TLS failures, repeated scans, and dry-run behavior. Test hostnames are fixtures, not claimed discoveries.

All 68 offline tests cover discovery, bounded DNS/verification, queue continuation, and the collector and Google upload logic, including Docs append-only behavior, repeated uploads, styled links, multiple tabs, revision conflicts, and recovery from an uncertain write response. These are offline API simulations; live Google access must be configured separately.
