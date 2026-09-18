"""Append missing saved URLs to Google Docs, preserving all existing content."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

from collector import saved_urls, url_key
from sync_sheets import SyncError, make_session, read_links

API = "https://docs.googleapis.com/v1/documents/"
SCOPES = ["https://www.googleapis.com/auth/documents"]
BATCH_SIZE = 500


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def document_urls(document):
    existing = set()
    for node in nodes(document):
        paragraph = node.get("paragraph")
        if isinstance(paragraph, dict):
            # A single URL may be split across differently formatted text runs.
            text = "".join(element.get("textRun", {}).get("content", "")
                           for element in paragraph.get("elements", []))
            existing.update(saved_urls(text.encode("utf-8")))
        # Also recognize hyperlink targets behind labels and rich-link chips.
        for field in ("url", "uri"):
            if isinstance(node.get(field), str):
                existing.update(saved_urls(node[field].encode("utf-8")))
    return existing


def target_location(document, tab_id):
    tabs = [node for node in nodes(document.get("tabs", [])) if "documentTab" in node]
    if tabs:
        target = next((tab for tab in tabs if tab.get("tabProperties", {}).get("tabId") == tab_id), None) if tab_id else tabs[0]
        if target is None:
            raise SyncError("GOOGLE_DOC_TAB_ID does not match a tab in this document.")
        selected_id = target.get("tabProperties", {}).get("tabId")
        content = target["documentTab"]
        if not selected_id or not isinstance(content.get("body"), dict):
            raise SyncError("Google returned no writable body for the selected tab.")
        return {"tabId": selected_id}, content
    # includeTabsContent=true must return tabs. Never issue an unscoped insert.
    raise SyncError("The requested document tabs are not available; refusing to write.")


def checked(response, operation):
    if not 200 <= response.status_code < 300:
        raise SyncError(f"{operation} failed (HTTP {response.status_code}). Check Docs API access, document sharing, "
                        "and quota. If the document changed during upload, the next run will re-read it.")
    return response.json()


def sync(session, document_id, links, tab_id="", dry_run=False):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", document_id):
        raise SyncError("Set GOOGLE_DOC_ID to the ID between /d/ and /edit in the document URL.")
    source = list(dict.fromkeys(url_key(url) for url in links))
    appended = already = would_append = 0
    selected_id = tab_id.strip()
    if selected_id:
        print(f"Docs: configured tab ID {selected_id}", flush=True)
    # Read even an empty source to validate access/configuration.
    for start in range(0, max(len(source), 1), BATCH_SIZE):
        document = checked(session.get(API + document_id, params={
            "includeTabsContent": "true", "suggestionsViewMode": "PREVIEW_WITHOUT_SUGGESTIONS"}, timeout=30), "Read document")
        location, tab_content = target_location(document, selected_id)
        selected_id = location["tabId"]  # Pin the same tab across every batch.
        if start == 0:
            print(f"Docs: using tab ID {selected_id} for duplicate checks and appends", flush=True)
        body = tab_content["body"]
        existing = document_urls(tab_content)
        batch = source[start:start + BATCH_SIZE]
        missing = [url for url in batch if url not in existing]
        already += len(batch) - len(missing)
        if dry_run:
            would_append += len(missing)
        elif missing:
            revision = document.get("revisionId")
            if not revision:
                raise SyncError("No revision ID returned; refusing an unprotected append. Confirm Editor access.")
            body_text = "".join(node.get("textRun", {}).get("content", "") for node in nodes(body))
            separator = "\n" if body_text.strip() else ""
            # Insert before the final document newline. Never delete or replace
            # existing text. Revision guard prevents races with manual editors.
            checked(session.post(API + document_id + ":batchUpdate", json={
                "writeControl": {"requiredRevisionId": revision},
                "requests": [{"insertText": {"endOfSegmentLocation": location,
                    "text": separator + "\n".join(missing) + "\n"}}]}, timeout=30), "Append document links")
            appended += len(missing)
    print(f"Docs: {len(source)} saved URLs | Already present: {already} | Newly appended: {appended} | "
          f"Would append: {would_append}", flush=True)
    return appended


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--if-configured", action="store_true")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        target = config.get("google_docs", {})
        document_id = os.environ.get("GOOGLE_DOC_ID", "").strip() or target.get("document_id", "")
        tab_id = os.environ.get("GOOGLE_DOC_TAB_ID", "").strip() or target.get("tab_id", "")
        credentials = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if not document_id or not credentials:
            message = "Set the Google Doc ID and GOOGLE_SERVICE_ACCOUNT_JSON secret; uploads are pending setup."
            if args.if_configured:
                print(message + " links.txt collection continues.")
                return 0
            raise SyncError(message)
        links = read_links(args.config.resolve().parent / config["links_file"])
        with make_session(credentials, scopes=SCOPES) as session:
            sync(session, document_id, links, tab_id=tab_id, dry_run=args.dry_run)
        return 0
    except SyncError as error:
        print(f"Docs sync failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Docs sync failed ({type(error).__name__}). No automatic write retry; next run re-reads the document.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
