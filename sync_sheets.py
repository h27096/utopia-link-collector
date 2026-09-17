"""Append saved links to an existing Google Sheets tab without changing old rows."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote

from collector import saved_urls, url_key

API = "https://sheets.googleapis.com/v4/spreadsheets/"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
BATCH_SIZE = 500


class SyncError(RuntimeError):
    pass


def read_links(path):
    # Preserve first occurrence order without changing the source file.
    links = []
    seen = set()
    for line in path.read_bytes().splitlines():
        for url in sorted(saved_urls(line)):
            if url not in seen:
                seen.add(url)
                links.append(url)
    return links


def response_json(response, operation):
    if not 200 <= response.status_code < 300:
        # Do not echo HTTP bodies, tokens, or credential material into public logs.
        raise SyncError(f"{operation} failed (HTTP {response.status_code}). Check sheet access, API enablement, and quota.")
    return response.json()


def sync(session, spreadsheet_id, tab_name, links, dry_run=False):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id):
        raise SyncError("GOOGLE_SHEET_ID must be the ID from the sheet URL, not the full URL.")
    base = API + spreadsheet_id
    metadata = response_json(session.get(base, params={
        "fields": "sheets.properties(sheetId,title,index)"}, timeout=30), "Read sheet metadata")
    if not tab_name.strip():
        tabs = sorted(metadata.get("sheets", []), key=lambda item: item["properties"].get("index", 0))
        if not tabs:
            raise SyncError("The spreadsheet has no tabs.")
        tab_name = tabs[0]["properties"]["title"]
    matches = [item["properties"] for item in metadata.get("sheets", [])
               if item["properties"]["title"] == tab_name]
    if not matches:
        raise SyncError("Configured tab does not exist. Create it or correct GOOGLE_SHEET_TAB.")
    sheet_id = matches[0]["sheetId"]
    # Quoting avoids ambiguity with named ranges and supports spaces/apostrophes.
    a1 = "'" + tab_name.replace("'", "''") + "'"
    source = list(dict.fromkeys(url_key(url) for url in links))
    appended = already = would_append = 0
    for start in range(0, len(source), BATCH_SIZE):
        # Re-read the whole tab before every batch, including manually added URLs.
        values = response_json(session.get(base + "/values/" + quote(a1, safe=""),
            params={"valueRenderOption": "FORMATTED_VALUE"}, timeout=30), "Read existing links")
        existing = set()
        for row in values.get("values", []):
            for cell in row:
                existing.update(saved_urls(str(cell).encode("utf-8")))
        batch = source[start:start + BATCH_SIZE]
        missing = [url for url in batch if url_key(url) not in existing]
        already += len(batch) - len(missing)
        if dry_run:
            would_append += len(missing)
        elif missing:
            # appendCells adds below the last populated row across the entire tab,
            # unlike logical-table detection, which can stop at a blank row.
            # Never update, clear, delete, sort, or insert into existing rows.
            response_json(session.post(base + ":batchUpdate", json={"requests": [{
                "appendCells": {"sheetId": sheet_id, "fields": "userEnteredValue",
                    "rows": [{"values": [{"userEnteredValue": {"stringValue": url}}]}
                             for url in missing]}}]}, timeout=30), "Append links")
            appended += len(missing)
    print(f"Sheets: {len(source)} saved URLs | Already present: {already} | "
          f"Newly appended: {appended} | Would append: {would_append}", flush=True)
    return appended


def make_session(credentials_json):
    try:
        from google.oauth2.service_account import Credentials
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        raise SyncError("Install the optional dependencies: python -m pip install -r requirements-sheets.txt") from None
    try:
        info = json.loads(credentials_json)
        if info.get("type") != "service_account" or info.get("token_uri") != "https://oauth2.googleapis.com/token":
            raise ValueError("Unexpected credential type or token endpoint")
        credentials = Credentials.from_service_account_info(info, scopes=SCOPES)
        return AuthorizedSession(credentials, refresh_timeout=30)
    except Exception:
        raise SyncError("GOOGLE_SERVICE_ACCOUNT_JSON must contain a valid Google service-account JSON key.") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--if-configured", action="store_true", help="Skip uploads until credentials are supplied")
    args = parser.parse_args()
    credentials_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if args.if_configured and not credentials_json:
        print("Sheets upload pending setup: add the GOOGLE_SERVICE_ACCOUNT_JSON GitHub secret. links.txt is still saved.")
        return 0
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        sheet_config = config.get("google_sheets", {})
        spreadsheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip() or sheet_config.get("spreadsheet_id", "")
        tab_name = os.environ.get("GOOGLE_SHEET_TAB", "").strip() or sheet_config.get("tab_name", "")
        if not spreadsheet_id or not credentials_json:
            raise SyncError("Set a spreadsheet ID in config.json (or GOOGLE_SHEET_ID) and the GOOGLE_SERVICE_ACCOUNT_JSON secret.")
        links = read_links(args.config.resolve().parent / config["links_file"])
        with make_session(credentials_json) as session:
            sync(session, spreadsheet_id, tab_name, links, args.dry_run)
        return 0
    except SyncError as error:
        print(f"Sheets sync failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Sheets sync failed ({type(error).__name__}). No automatic write retry; the next run re-reads the sheet.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
