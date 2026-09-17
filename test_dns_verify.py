import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import collector as c
import dns_verify as d

TARGET = "104.218.50.66"
CONFIG = {"target_ip": TARGET}
GOOD = {"status": "ok", "addresses": [TARGET]}
ERROR = {"status": "error", "detail": "SERVFAIL"}
NEGATIVE = {"status": "nxdomain", "detail": "NXDOMAIN"}


class DNSVerificationTests(unittest.TestCase):
    def test_system_success_needs_no_doh(self):
        with patch.object(d, "bounded_query", return_value=GOOD) as query:
            self.assertTrue(d.resolves_to_target("host.example", CONFIG))
        self.assertEqual(query.call_count, 1)
        self.assertEqual(query.call_args.args[0], "system")

    def test_system_timeout_falls_back_to_cloudflare(self):
        with patch.object(d, "bounded_query", side_effect=[ERROR, GOOD]) as query:
            self.assertTrue(d.resolves_to_target("host.example", CONFIG))
        self.assertEqual([call.args[0] for call in query.call_args_list], ["system", "cloudflare"])
        self.assertTrue(all(call.args[2] <= 1 for call in query.call_args_list))

    def test_google_is_independent_fallback(self):
        with patch.object(d, "bounded_query", side_effect=[ERROR, ERROR, GOOD]) as query:
            self.assertTrue(d.resolves_to_target("host.example", CONFIG))
        self.assertEqual(query.call_args.args[0], "google")

    def test_retries_transient_failures_only_after_fallbacks(self):
        with patch.object(d, "bounded_query", side_effect=[ERROR, ERROR, NEGATIVE, GOOD]) as query:
            self.assertTrue(d.resolves_to_target("host.example", CONFIG))
        self.assertEqual([call.args[0] for call in query.call_args_list], ["system", "cloudflare", "google", "system"])

    def test_negative_answers_are_not_retried(self):
        with patch.object(d, "bounded_query", return_value=NEGATIVE) as query:
            with self.assertRaisesRegex(d.DNSVerificationError, "NXDOMAIN"):
                d.resolves_to_target("host.example", CONFIG)
        self.assertEqual(query.call_count, 3)

    def test_other_ip_is_never_accepted(self):
        with patch.object(d, "bounded_query", return_value={"status": "ok", "addresses": ["93.184.216.34"]}):
            self.assertFalse(d.resolves_to_target("host.example", CONFIG))

    def test_total_budget_caps_attempts(self):
        now = [0.0]
        durations = []
        def query(resolver, host, timeout):
            durations.append(timeout)
            now[0] += timeout
            return ERROR
        with patch.object(d.time, "monotonic", side_effect=lambda: now[0]), patch.object(d, "bounded_query", side_effect=query):
            with self.assertRaisesRegex(d.DNSVerificationError, "budget exhausted"):
                d.resolves_to_target("host.example", {**CONFIG, "dns_total_timeout_seconds": 2.5})
        self.assertEqual(durations, [1.0, 1.0, 0.5])

    def test_hung_worker_is_killed_without_waiting_for_its_sleep(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Path(directory) / "slow_worker.py"
            worker.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            start = time.monotonic()
            with patch.object(d, "__file__", str(worker)):
                result = d.bounded_query("system", "host.example", 0.15)
            self.assertLess(time.monotonic() - start, 2.0)
            self.assertEqual(result["status"], "error")
            self.assertIn("timeout", result["detail"])

    def test_malformed_worker_output_rejected(self):
        result = subprocess.CompletedProcess([], 0, stdout="<html>oops</html>")
        with patch.object(d.subprocess, "run", return_value=result):
            self.assertEqual(d.bounded_query("google", "host.example", 1)["status"], "error")

    def test_doh_cname_and_multiple_a_records(self):
        answer = {"Status": 0, "TC": False, "Answer": [
            {"name": "host.example.", "type": 5, "data": "alias.example."},
            {"name": "alias.example.", "type": 1, "data": "93.184.216.34"},
            {"name": "alias.example.", "type": 1, "data": TARGET}]}
        self.assertIn(TARGET, d.parse_doh(answer, "host.example")["addresses"])

    def test_unrelated_a_record_not_accepted(self):
        answer = {"Status": 0, "Answer": [{"name": "other.example.", "type": 1, "data": TARGET}]}
        self.assertEqual(d.parse_doh(answer, "host.example")["status"], "nodata")

    def test_statuses_truncation_and_bad_payload_fail_closed(self):
        for answer, reason in (({"Status": 2}, "SERVFAIL"), ({"Status": 5}, "REFUSED"),
                               ({"Status": 0, "TC": True}, "truncated"),
                               ({"Status": "0"}, "malformed"),
                               ({"Status": 0, "Answer": "garbage"}, "malformed")):
            with self.subTest(answer=answer), self.assertRaisesRegex(ValueError, reason):
                d.parse_doh(answer, "host.example")
        self.assertEqual(d.parse_doh({"Status": 3}, "host.example")["status"], "nxdomain")
        self.assertEqual(d.parse_doh({"Status": 0}, "host.example")["status"], "nodata")

    def test_doh_requests_json_without_disabling_dnssec(self):
        answer = {"Status": 0, "Answer": [{"name": "host.example.", "type": 1, "data": TARGET}]}
        with patch.object(d, "urlopen") as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = json.dumps(answer).encode()
            self.assertEqual(d.query_once("cloudflare", "host.example", 0.5)["addresses"], [TARGET])
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("Accept"), "application/dns-json")
        self.assertNotIn("cd=", request.full_url)
        self.assertEqual(open_url.call_args.kwargs["timeout"], 0.5)

    def test_invalid_dns_config_rejected(self):
        for option in ({"dns_timeout_seconds": 0}, {"dns_attempts": True},
                       {"dns_total_timeout_seconds": float("nan")}, {"dns_attempts": 10}):
            with self.subTest(option=option), self.assertRaises(ValueError):
                d.options(option)

    def test_dns_failure_never_fetches_page_sleeps_or_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.txt"
            original = b"https://old.example/\r\nhttps://old.example/"
            path.write_bytes(original)
            config = {**CONFIG, "timeout_seconds": 10, "request_delay_seconds": 1}
            with patch.object(c, "discover", return_value=["host.example"]), \
                 patch.object(d, "bounded_query", return_value=NEGATIVE), \
                 patch.object(c, "fetch_page") as page, patch.object(c.time, "sleep") as sleep, \
                 patch("sys.stdout", new_callable=io.StringIO):
                c.scan(config, path)
            page.assert_not_called()
            sleep.assert_not_called()
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
