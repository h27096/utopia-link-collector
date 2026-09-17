# Validation performed

- All 10 offline tests passed on September 16, 2026 using the bundled Python runtime.
- A live `--dry-run --max-candidates 3` completed successfully after network access was approved.
- The public reverse-IP provider returned 500 distinct candidate hostnames (provider result limits may apply).
- Two candidates passed the configured DNS, HTTPS, and Utopia branding heuristic; one was left unverified because its DNS lookup failed or was truncated. The remaining 497 were deferred by the explicit test limit.
- No URLs were appended during validation. The delivered `links.txt` is empty, ready for the first normal run.

The live smoke test validates the discovery/checking path for a small sample. It does not certify all 500 candidates or establish official ownership of sites with matching branding.
