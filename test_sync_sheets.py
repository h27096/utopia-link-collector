import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sync_sheets as s


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return copy.deepcopy(self.data)


class FakeSession:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)
        self.posts = []
        self.reads = 0
        self.lose_response = False

    def get(self, url, **kwargs):
        if "/values/" in url:
            self.reads += 1
            return Response({"values": self.rows})
        return Response({"sheets": [{"properties": {"title": "Links", "sheetId": 42, "index": 0}}]})

    def post(self, url, json, **kwargs):
        self.posts.append(copy.deepcopy(json))
        request = json["requests"][0]
        assert list(request) == ["appendCells"]
        assert request["appendCells"]["sheetId"] == 42
        for row in request["appendCells"]["rows"]:
            self.rows.append([row["values"][0]["userEnteredValue"]["stringValue"]])
        if self.lose_response:
            raise TimeoutError("Server appended, response was lost")
        return Response({"replies": [{}]})


class SheetsTests(unittest.TestCase):
    def setUp(self):
        self.output = patch("sys.stdout", new_callable=io.StringIO)
        self.output.start()
        self.addCleanup(self.output.stop)

    def test_preserves_duplicates_blank_rows_notes_and_order(self):
        original = [["Link"], ["https://old.example/"], [], ["https://old.example/", "keep this"]]
        session = FakeSession(original)
        self.assertEqual(s.sync(session, "sheet_id", "Links", ["https://old.example/", "https://new.example/"]), 1)
        self.assertEqual(session.rows, original + [["https://new.example/"]])

    def test_second_run_and_repeated_source_do_not_duplicate(self):
        session = FakeSession([])
        links = ["https://new.example/", "https://NEW.example:443"]
        self.assertEqual(s.sync(session, "sheet_id", "Links", links), 1)
        self.assertEqual(s.sync(session, "sheet_id", "Links", links), 0)
        self.assertEqual(len(session.posts), 1)

    def test_manual_url_in_other_column_recognized(self):
        session = FakeSession([["note", "Saved: https://NEW.example:443"]])
        self.assertEqual(s.sync(session, "sheet_id", "Links", ["https://new.example/"]), 0)
        self.assertFalse(session.posts)

    def test_batch_reads_sheet_each_time(self):
        session = FakeSession([])
        with patch.object(s, "BATCH_SIZE", 1):
            s.sync(session, "sheet_id", "Links", ["https://one.example/", "https://two.example/"])
        self.assertEqual(session.reads, 2)

    def test_timeout_after_append_does_not_duplicate_next_run(self):
        session = FakeSession([])
        session.lose_response = True
        with self.assertRaises(TimeoutError):
            s.sync(session, "sheet_id", "Links", ["https://new.example/"])
        session.lose_response = False
        self.assertEqual(s.sync(session, "sheet_id", "Links", ["https://new.example/"]), 0)
        self.assertEqual(len(session.rows), 1)

    def test_dry_run_never_writes(self):
        session = FakeSession([])
        s.sync(session, "sheet_id", "Links", ["https://new.example/"], dry_run=True)
        self.assertFalse(session.posts)

    def test_missing_tab_never_writes(self):
        session = FakeSession([])
        with self.assertRaises(s.SyncError):
            s.sync(session, "sheet_id", "Wrong", ["https://new.example/"])
        self.assertFalse(session.posts)

    def test_blank_tab_selects_first_tab(self):
        session = FakeSession([])
        self.assertEqual(s.sync(session, "sheet_id", "", ["https://new.example/"]), 1)

    def test_read_source_preserves_bytes_and_first_occurrence_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.txt"
            original = b"https://z.example/\r\nhttps://a.example/\r\nhttps://z.example/"
            path.write_bytes(original)
            self.assertEqual(s.read_links(path), ["https://z.example/", "https://a.example/"])
            self.assertEqual(path.read_bytes(), original)

    def test_bad_http_status_stops_upload(self):
        session = FakeSession([])
        with patch.object(session, "get", return_value=Response({}, 403)):
            with self.assertRaises(s.SyncError):
                s.sync(session, "sheet_id", "Links", ["https://new.example/"])
        self.assertFalse(session.posts)

    def test_unconfigured_hourly_job_keeps_collecting(self):
        with patch.dict("os.environ", {}, clear=True), patch("sys.argv", ["sync_sheets.py", "--if-configured"]):
            self.assertEqual(s.main(), 0)


if __name__ == "__main__":
    unittest.main()
