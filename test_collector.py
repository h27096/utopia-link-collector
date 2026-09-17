import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import collector as c

CONFIG = {"target_ip": "104.218.50.66", "timeout_seconds": 1, "request_delay_seconds": 0.001}


class CollectorTests(unittest.TestCase):
    def test_existing_duplicate_lines_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.txt"
            original = b"# My list\r\nhttps://old.example/\r\nhttps://old.example/\r\n"
            path.write_bytes(original)
            self.assertFalse(c.append_new(path, "https://old.example/"))
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(c.append_new(path, "https://new.example/"))
            self.assertEqual(path.read_bytes(), original + b"https://new.example/\n")
            self.assertFalse(c.append_new(path, "https://new.example/"))

    def test_no_final_newline_and_non_utf8_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.txt"
            original = b"notes: \xff\r\nhttps://old.example/"
            path.write_bytes(original)
            c.append_new(path, "https://new.example/")
            self.assertEqual(path.read_bytes(), original + b"\nhttps://new.example/\n")

    def test_manual_entry_reread_and_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.txt"
            path.write_bytes(b"\xef\xbb\xbfMy link: https://NEW.example:443\n")
            self.assertFalse(c.append_new(path, "https://new.example/"))
            self.assertNotEqual(c.url_key("http://new.example/"), c.url_key("https://new.example/"))

    def test_lock_refuses_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.txt"
            with c.file_lock(path):
                with self.assertRaises(RuntimeError):
                    c.append_new(path, "https://new.example/")
            self.assertFalse(path.exists())

    def test_discovery_validation_and_duplicate_candidates(self):
        with patch.object(c, "get_text", return_value="one.example\nONE.example\ntwo.example\n"):
            self.assertEqual(c.discover(CONFIG), ["one.example", "two.example"])
        for body in ("API count exceeded", "error check your search parameter", "", "127.0.0.1", "a.example\nerror"):
            with patch.object(c, "get_text", return_value=body):
                with self.assertRaises(ValueError):
                    c.discover(CONFIG)

    def test_branding_requires_title_or_metadata(self):
        for html, expected in (("<title>Utopia | Home</title>", True),
                               ('<meta property="og:site_name" content="Utopia">', True),
                               ("<p>A discussion about Utopia</p>", False),
                               ("<title>Utopian</title>", False)):
            parser = c.BrandingParser()
            parser.feed(html)
            self.assertEqual(parser.matches(), expected)

    def test_dns_mismatch_does_not_fetch_page(self):
        with patch.object(c, "resolves_to_target", return_value=False), patch.object(c, "fetch_page") as fetch:
            self.assertFalse(c.verify("one.example", CONFIG)[0])
            fetch.assert_not_called()

    def test_verification_failures_are_unverified(self):
        with patch.object(c, "resolves_to_target", return_value=True):
            with patch.object(c, "fetch_page", side_effect=OSError("TLS failed")):
                self.assertFalse(c.verify("one.example", CONFIG)[0])
            with patch.object(c, "fetch_page", return_value="<title>Utopia</title>"):
                self.assertTrue(c.verify("one.example", CONFIG)[0])

    def test_scan_twice_only_appends_verified_new_links(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.txt"
            original = b"https://old.example/\nhttps://old.example/\n"
            path.write_bytes(original)
            with patch.object(c, "discover", return_value=["old.example", "new.example", "bad.example"]), \
                 patch.object(c, "verify", side_effect=lambda host, config: (host != "bad.example", "test")), \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                c.scan(CONFIG, path)
                c.scan(CONFIG, path)
            self.assertEqual(path.read_bytes(), original + b"https://new.example/\n")
            self.assertIn("Already saved: 2 | Rejected/unverified: 1 | Newly appended: 0", output.getvalue())

    def test_dry_run_creates_no_links_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.txt"
            with patch.object(c, "discover", return_value=["new.example"]), \
                 patch.object(c, "verify", return_value=(True, "test")), patch("sys.stdout", new_callable=io.StringIO):
                c.scan(CONFIG, path, dry_run=True)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
