import copy
import io
import unittest
from unittest.mock import patch

import sync_docs as d


def paragraph(text):
    return {"paragraph": {"elements": [{"textRun": {"content": text}}]}}


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return copy.deepcopy(self.data)


class FakeSession:
    def __init__(self, text):
        self.document = {"revisionId": "1", "tabs": [{"tabProperties": {"tabId": "first"},
            "documentTab": {"body": {"content": [paragraph(text)]}}}]}
        self.posts = []
        self.conflict = False
        self.lose_response = False
        self.reads = 0

    @property
    def content(self):
        return self.document["tabs"][0]["documentTab"]["body"]["content"]

    def get(self, url, **kwargs):
        assert kwargs["params"]["includeTabsContent"] == "true"
        self.reads += 1
        return Response(self.document)

    def post(self, url, json, **kwargs):
        self.posts.append(copy.deepcopy(json))
        assert json["writeControl"]["requiredRevisionId"] == self.document["revisionId"]
        if self.conflict:
            return Response({}, 400)
        request = json["requests"][0]
        assert list(request) == ["insertText"]
        location = request["insertText"]["endOfSegmentLocation"]
        assert set(location) == {"tabId"}
        target = next(node for node in d.nodes(self.document["tabs"])
                      if node.get("tabProperties", {}).get("tabId") == location["tabId"])
        target["documentTab"]["body"]["content"].append(paragraph(request["insertText"]["text"]))
        self.document["revisionId"] = str(int(self.document["revisionId"]) + 1)
        if self.lose_response:
            raise TimeoutError("Response lost after insert")
        return Response({})


class DocsTests(unittest.TestCase):
    def setUp(self):
        output = patch("sys.stdout", new_callable=io.StringIO)
        self.output = output.start()
        self.addCleanup(output.stop)

    def test_preserves_existing_duplicates_and_notes(self):
        session = FakeSession("My list\nhttps://old.example/\nhttps://old.example/\n")
        before = copy.deepcopy(session.content)
        self.assertEqual(d.sync(session, "doc_id", ["https://old.example/", "https://new.example/"]), 1)
        self.assertEqual(session.content[:len(before)], before)
        self.assertEqual(session.posts[0]["requests"][0]["insertText"]["text"], "\nhttps://new.example/\n")

    def test_repeat_run_is_noop(self):
        session = FakeSession("\n")
        links = ["https://new.example/", "https://NEW.example:443"]
        self.assertEqual(d.sync(session, "doc_id", links), 1)
        self.assertEqual(d.sync(session, "doc_id", links), 0)
        self.assertEqual(len(session.posts), 1)

    def test_formatted_runs_and_hidden_links(self):
        doc = {"body": {"content": [{"paragraph": {"elements": [
            {"textRun": {"content": "https://split."}},
            {"textRun": {"content": "example/\n"}},
            {"textRun": {"content": "Click here", "textStyle": {"link": {"url": "https://hidden.example/"}}}}
        ]}}]}}
        self.assertEqual(d.document_urls(doc), {"https://split.example/", "https://hidden.example/"})

    def test_urls_in_other_tabs_and_tables_are_found(self):
        session = FakeSession("\n")
        session.document["tabs"].append({"tabProperties": {"tabId": "other"}, "documentTab": {
            "body": {"content": [{"table": {"tableRows": [{"tableCells": [{"content": [
                paragraph("https://table.example/\n")]}]}]}}]}}})
        self.assertEqual(d.sync(session, "doc_id", ["https://table.example/"], tab_id="other"), 0)
        self.assertFalse(session.posts)

    def test_configured_nested_tab_scopes_checks_and_every_insert(self):
        session = FakeSession("Main notes\nhttps://main.example/\n")
        target = {"tabProperties": {"tabId": "t.1r7w8t8rjblc"}, "documentTab": {
            "body": {"content": [paragraph("Notes\nhttps://old.example/\nhttps://old.example/\n")]}}}
        session.document["tabs"].append({"tabProperties": {"tabId": "parent"},
            "documentTab": {"body": {"content": [paragraph("https://parent.example/\n")]}},
            "childTabs": [target]})
        main_before = copy.deepcopy(session.document["tabs"][0])
        parent_before = copy.deepcopy(session.document["tabs"][1]["documentTab"])
        before = copy.deepcopy(target["documentTab"]["body"]["content"])
        links = ["https://old.example/", "https://main.example/", "https://parent.example/"]
        with patch.object(d, "BATCH_SIZE", 1):
            self.assertEqual(d.sync(session, "doc_id", links, tab_id="t.1r7w8t8rjblc"), 2)
        self.assertEqual(session.document["tabs"][0], main_before)
        self.assertEqual(session.document["tabs"][1]["documentTab"], parent_before)
        self.assertEqual(target["documentTab"]["body"]["content"][:len(before)], before)
        for post in session.posts:
            self.assertEqual(post["requests"][0]["insertText"]["endOfSegmentLocation"],
                             {"tabId": "t.1r7w8t8rjblc"})
        self.assertEqual(d.sync(session, "doc_id", links, tab_id="t.1r7w8t8rjblc"), 0)
        self.assertIn("using tab ID t.1r7w8t8rjblc", self.output.getvalue())

    def test_missing_tabs_never_use_legacy_main_body(self):
        session = FakeSession("\n")
        session.document = {"revisionId": "1", "body": {"content": [paragraph("main\n")]}}
        for tab_id in ("t.1r7w8t8rjblc", ""):
            with self.assertRaises(d.SyncError):
                d.sync(session, "doc_id", ["https://new.example/"], tab_id=tab_id)
        self.assertFalse(session.posts)

    def test_environment_tab_overrides_config_without_logging_credentials(self):
        session = FakeSession("\n")
        session.document["tabs"][0]["tabProperties"]["tabId"] = "t.1r7w8t8rjblc"
        config = '{"links_file":"links.txt","google_docs":{"tab_id":"wrong"}}'
        with patch.dict("os.environ", {"GOOGLE_DOC_ID": "doc_id",
                "GOOGLE_DOC_TAB_ID": "t.1r7w8t8rjblc",
                "GOOGLE_SERVICE_ACCOUNT_JSON": "secret-not-for-logs"}, clear=True), \
                patch("sys.argv", ["sync_docs.py"]), \
                patch.object(d.Path, "read_text", return_value=config), \
                patch.object(d, "read_links", return_value=["https://new.example/"]), \
                patch.object(d, "make_session") as factory:
            factory.return_value.__enter__.return_value = session
            self.assertEqual(d.main(), 0)
        self.assertEqual(len(session.posts), 1)
        self.assertIn("using tab ID t.1r7w8t8rjblc", self.output.getvalue())
        self.assertNotIn("secret-not-for-logs", self.output.getvalue())

    def test_revision_conflict_never_overwrites(self):
        session = FakeSession("notes\n")
        before = copy.deepcopy(session.content)
        session.conflict = True
        with self.assertRaises(d.SyncError):
            d.sync(session, "doc_id", ["https://new.example/"])
        self.assertEqual(session.content, before)
        self.assertEqual(len(session.posts), 1)

    def test_lost_response_reconciled_on_next_run(self):
        session = FakeSession("\n")
        session.lose_response = True
        with self.assertRaises(TimeoutError):
            d.sync(session, "doc_id", ["https://new.example/"])
        session.lose_response = False
        self.assertEqual(d.sync(session, "doc_id", ["https://new.example/"]), 0)
        self.assertEqual(len(session.posts), 1)

    def test_every_batch_reads_new_revision(self):
        session = FakeSession("\n")
        with patch.object(d, "BATCH_SIZE", 1):
            d.sync(session, "doc_id", ["https://one.example/", "https://two.example/"])
        self.assertEqual(session.reads, 2)
        self.assertEqual(session.posts[1]["writeControl"]["requiredRevisionId"], "2")

    def test_dry_run_never_writes(self):
        session = FakeSession("\n")
        d.sync(session, "doc_id", ["https://new.example/"], dry_run=True)
        self.assertFalse(session.posts)

    def test_missing_revision_and_wrong_tab_fail_closed(self):
        session = FakeSession("\n")
        del session.document["revisionId"]
        with self.assertRaises(d.SyncError):
            d.sync(session, "doc_id", ["https://new.example/"])
        with self.assertRaises(d.SyncError):
            d.sync(session, "doc_id", ["https://new.example/"], tab_id="unknown")
        self.assertFalse(session.posts)

    def test_unconfigured_job_skips_upload(self):
        with patch.dict("os.environ", {}, clear=True), patch("sys.argv", ["sync_docs.py", "--if-configured"]):
            self.assertEqual(d.main(), 0)


if __name__ == "__main__":
    unittest.main()
