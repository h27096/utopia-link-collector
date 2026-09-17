import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import collector as c
import discovery as d

IP = "104.218.50.66"
CONFIG = {"target_ip": IP, "timeout_seconds": 10, "request_delay_seconds": 0.01}


def urlscan_row(host, cursor):
    return {"page": {"ip": IP, "domain": host}, "sort": cursor}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        output = patch("sys.stdout", new_callable=io.StringIO)
        self.output = output.start()
        self.addCleanup(output.stop)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def source(self, name, responses, **overrides):
        opts = {**d.DEFAULTS, "page_delay_seconds": 0, **overrides}
        with patch.object(d, "fetch_text", side_effect=responses) as fetch, patch.object(d.time, "sleep"):
            result = d.collect_source(name, IP, opts)
        return result, fetch

    def test_hackertarget_not_locally_capped_at_500(self):
        body = "\n".join(f"host{i}.example" for i in range(2000))
        result, fetch = self.source("hackertarget", [body])
        self.assertEqual(len(result.hosts), 2000)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("membership", result.note)

    def test_urlscan_paginates_even_when_has_more_false_and_short_page(self):
        first = {"results": [urlscan_row("one.example", [123, "a"])], "has_more": False, "total": 2}
        second = {"results": [urlscan_row("two.example", [122, "b"])], "has_more": False, "total": 2}
        result, fetch = self.source("urlscan", [json.dumps(first), json.dumps(second), '{"results": []}'])
        self.assertEqual(result.hosts, {"one.example", "two.example"})
        self.assertEqual(fetch.call_count, 3)
        self.assertIn("search_after=123%2Ca", fetch.call_args_list[1].args[0])

    def test_hackertarget_membership_pagination_preserves_free_first_request(self):
        with patch.dict(os.environ, {"HACKERTARGET_API_KEY": "test-key"}), patch.object(d, "HACKERTARGET_PAGE_SIZE", 2):
            result, fetch = self.source("hackertarget", ["one.example\ntwo.example", "three.example"])
        self.assertEqual(len(result.hosts), 3)
        self.assertNotIn("page=", fetch.call_args_list[0].args[0])
        self.assertIn("page=2", fetch.call_args_list[1].args[0])

    def test_urlscan_public_results_only_and_matching_ip(self):
        row = urlscan_row("wrong.example", [1, "x"])
        row["page"]["ip"] = "1.2.3.4"
        result, fetch = self.source("urlscan", [json.dumps({"results": [row]}), '{"results": []}'])
        self.assertFalse(result.hosts)
        self.assertIn("task.visibility%3Apublic", fetch.call_args_list[0].args[0])

    def test_mnemonic_offset_pagination_filters_exact_ip(self):
        first = {"count": 2, "data": [{"rrtype": "a", "query": "One.Example.", "answer": IP}]}
        second = {"count": 2, "data": [{"rrtype": "a", "query": "two.example", "answer": IP}]}
        result, fetch = self.source("mnemonic", [json.dumps(first), json.dumps(second)])
        self.assertEqual(result.hosts, {"one.example", "two.example"})
        self.assertIn("offset=1", fetch.call_args_list[1].args[0])

    def test_otx_snapshot_normalizes_and_ignores_other_addresses(self):
        payload = {"passive_dns": [{"hostname": "One.Example.", "address": IP},
            {"hostname": "one.example", "address": IP}, {"hostname": "wrong.example", "address": "1.2.3.4"}]}
        result, fetch = self.source("otx", [json.dumps(payload)])
        self.assertEqual(result.hosts, {"one.example"})
        self.assertEqual(fetch.call_count, 1)

    def test_mnemonic_reduces_page_size_when_server_rejects_limit(self):
        result, fetch = self.source("mnemonic", [d.SourceError("HTTP 412", status=412),
            json.dumps({"count": 1, "data": [{"rrtype": "a", "query": "one.example", "answer": IP}]})])
        self.assertEqual(result.hosts, {"one.example"})
        self.assertIn("limit=100&", fetch.call_args_list[1].args[0])
        self.assertIn("offset=0", fetch.call_args_list[1].args[0])

    def test_transient_timeout_gets_one_retry(self):
        result, fetch = self.source("otx", [d.SourceError("timeout", retryable=True), '{"passive_dns": []}'])
        self.assertTrue(result.successful)
        self.assertEqual(fetch.call_count, 2)

    def test_repeated_page_stops_and_retains_candidates(self):
        payload = json.dumps({"results": [urlscan_row("one.example", [123, "x"])]})
        result, fetch = self.source("urlscan", [payload, payload])
        self.assertEqual(result.hosts, {"one.example"})
        self.assertIn("repeated page", result.note)
        self.assertEqual(fetch.call_count, 2)

    def test_page_limit_is_reported(self):
        result, fetch = self.source("urlscan", [json.dumps({"results": [urlscan_row("one.example", [1, "x"])]})], max_pages_per_source=1)
        self.assertEqual(result.hosts, {"one.example"})
        self.assertIn("page limit", result.note)

    def test_candidate_safety_limit_is_explicit(self):
        result, fetch = self.source("hackertarget", ["\n".join(f"h{i}.example" for i in range(1200))], max_candidates_per_source=1000)
        self.assertEqual(len(result.hosts), 1000)
        self.assertIn("candidate safety limit", result.note)

    def test_rate_limit_preserves_previous_page_without_retry(self):
        result, fetch = self.source("urlscan", [json.dumps({"results": [urlscan_row("one.example", [1, "x"])]}), d.SourceError("HTTP 429")])
        self.assertTrue(result.successful)
        self.assertEqual(result.hosts, {"one.example"})
        self.assertIn("429", result.note)
        self.assertEqual(fetch.call_count, 2)

    def test_merges_all_sources_and_reports_per_source_counts(self):
        results = {"hackertarget": d.SourceResult("hackertarget", {"one.example", "same.example"}, 1, True),
                   "urlscan": d.SourceResult("urlscan", {"two.example", "same.example"}, 2, True),
                   "otx": d.SourceResult("otx", {"three.example"}, 1, True),
                   "mnemonic": d.SourceResult("mnemonic", set(), 0, False, "HTTP 402")}
        with patch.object(d, "collect_source", side_effect=lambda name, ip, opts: results[name]):
            self.assertEqual(d.discover(CONFIG), ["one.example", "same.example", "three.example", "two.example"])
        log = self.output.getvalue()
        for source in d.SOURCES:
            self.assertIn(f"Discovery {source}:", log)
        self.assertIn("total unique candidates: 4", log)

    def test_all_failed_is_not_silent_empty_success(self):
        with patch.object(d, "collect_source", side_effect=lambda name, ip, opts: d.SourceResult(name, note="failed")):
            with self.assertRaises(d.SourceError):
                d.discover(CONFIG)

    def test_credentials_are_headers_not_query_parameters(self):
        with patch.dict(os.environ, {"URLSCAN_API_KEY": "private-test-key"}):
            result, fetch = self.source("urlscan", ['{"results": []}'])
        self.assertNotIn("private-test-key", fetch.call_args.args[0])
        self.assertEqual(fetch.call_args.args[2]["api-key"], "private-test-key")

    def test_hung_http_worker_is_terminated(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "slow.py"
            script.write_text("import time\ntime.sleep(30)\n")
            start = time.monotonic()
            with patch.object(d, "__file__", str(script)), self.assertRaises(d.SourceError):
                d.fetch_text("https://example.com", 0.15, {})
            self.assertLess(time.monotonic() - start, 2)

    def test_malformed_json_or_empty_fields_fail_source(self):
        for text in ("<html>blocked</html>", "{}", '{"results": null}'):
            result, fetch = self.source("urlscan", [text])
            self.assertFalse(result.successful)
            self.assertFalse(result.hosts)

    def test_scan_queue_resumes_and_leaves_existing_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.txt"
            original = b"https://old.example/\r\nhttps://old.example/"
            path.write_bytes(original)
            with patch.object(c, "discover", return_value=["one.example", "two.example"]), \
                 patch.object(c, "bounded_verify", return_value=(True, "verified")) as verify:
                c.scan(CONFIG, path, max_candidates=1)
                c.scan(CONFIG, path, max_candidates=1)
            self.assertEqual([call.args[0] for call in verify.call_args_list], ["one.example", "two.example"])
            self.assertEqual(path.read_bytes(), original + b"\nhttps://one.example/\nhttps://two.example/\n")

    def test_dry_run_never_writes_queue_or_links(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.txt"
            with patch.object(c, "discover", return_value=["one.example"]), patch.object(c, "bounded_verify", return_value=(True, "ok")):
                c.scan(CONFIG, path, dry_run=True)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_scan_budget_retains_unprocessed_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.txt"
            with patch.object(c, "discover", return_value=["one.example"]), patch.object(c.time, "monotonic", side_effect=[0, 2]), \
                 patch.object(c, "bounded_verify") as verify:
                c.scan({**CONFIG, "scan_timeout_seconds": 1}, path)
            verify.assert_not_called()
            self.assertEqual(c.load_pending(path.with_name("pending_candidates.json"), IP), ["one.example"])

    def test_pending_work_survives_discovery_outage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.txt"
            c.save_pending(path.with_name("pending_candidates.json"), IP, ["one.example"])
            with patch.object(c, "discover", side_effect=d.SourceError("outage")), patch.object(c, "bounded_verify", return_value=(False, "unverified")) as verify:
                c.scan(CONFIG, path)
            self.assertEqual(verify.call_count, 1)
            self.assertFalse(path.exists())

    def test_verification_deadline_never_appends(self):
        with patch.object(c.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 0.1)):
            self.assertEqual(c.bounded_verify("one.example", CONFIG, 0.1), (False, "verification deadline (0.1s)"))


if __name__ == "__main__":
    unittest.main()
