# Connect your Google Doc

The hourly workflow now uploads to **Google Docs**, not Sheets. It keeps saving `links.txt` too.

[Your destination document](https://docs.google.com/document/d/1hKE01p0Wy2jjBw5oi0f6-PgVUj4sizWw9SZI6Cu1Jgo/edit) is configured in `config.json`. Links go at the end of the first document tab, one per line. Existing text and duplicate links are preserved.

## One-time Google access

If you already created the service account and GitHub secret for Sheets, reuse them: enable the **Google Docs API** in the same Google Cloud project, and share this **Doc** with the account as **Editor**. You do not need another key.

Otherwise:

1. Open [Google Cloud Console](https://console.cloud.google.com/), select or create a project, and enable **Google Docs API** under **APIs & Services > Library**.
2. Under **IAM & Admin > Service Accounts**, create a service account named `utopia-link-collector`. No project-wide role is needed for a document explicitly shared with it.
3. Copy the account's email ending in `iam.gserviceaccount.com`. Open your Doc, click **Share**, and add that email with **Editor** access. The document does not need to be public.
4. Open the service account's **Keys** tab and choose **Add key > Create new key > JSON**.
5. Open [GitHub Actions secrets](https://github.com/h27096/utopia-link-collector/settings/secrets/actions). Create `GOOGLE_SERVICE_ACCOUNT_JSON` and paste the complete downloaded JSON file as its value. Keep this key out of chat and repository files. If your organization blocks service-account keys, this key-based setup requires an administrator-supported alternative.
6. In [Actions](https://github.com/h27096/utopia-link-collector/actions), select **Collect Utopia links > Run workflow** on `main`. The Docs upload runs after collection. Future runs remain scheduled at minute 0 of each hour; GitHub may delay scheduled starts.

Official reference: [Google service-account credentials](https://developers.google.com/workspace/guides/create-credentials#service-account).

## What gets uploaded

Each run compares all URLs in `links.txt` with the current document. This imports earlier discoveries on the first upload and retries missing links after outages. The check covers text in all tabs, tables, headers, and footnotes returned by the Docs API, plus hyperlink destinations. URLs split across text styles are recognized. Unaccepted suggestions are excluded; comments, images, and inaccessible embedded content are not scanned.

Only missing links are appended. The uploader does not replace, delete, deduplicate, sort, or reformat existing content. Google may inherit formatting from the last paragraph for newly inserted text.

Every batch re-reads the document and uses its revision ID as a write guard. If someone edits it after that read, Google rejects the write and the next run checks again. Network failures do not trigger a blind write retry. This also prevents duplicate additions if a response is lost after Google accepted a batch.

`google_docs.document_id` in `config.json` selects the document. `google_docs.tab_id` is optional; blank uses the first tab. Repository Actions variables `GOOGLE_DOC_ID` and `GOOGLE_DOC_TAB_ID` override these settings. The old Sheets variables are no longer used by the hourly workflow.

## Local use and troubleshooting

```powershell
python -m pip install -r requirements-docs.txt
$env:GOOGLE_SERVICE_ACCOUNT_JSON = Get-Content -Raw 'C:\secure-folder\your-key.json'
python sync_docs.py --dry-run
python sync_docs.py
```

- **Pending setup:** supply the Google service-account secret. Collection still saves `links.txt`.
- **403/404:** check the Docs API is enabled, the document ID is correct, and this Doc is shared with the service-account email as Editor.
- **400 after a concurrent edit:** the revision guard may have rejected the write; the next run checks again. Other 400 errors may indicate an invalid document/tab configuration.
- **429:** quota was reached; a later run retries missing links.

Offline tests cover preservation of duplicates and notes, styled URLs, tables and multiple tabs, repeated syncs, lost responses, dry runs, and revision conflicts. Live upload still requires Google access setup; passing offline tests is not evidence that the credentials or document permissions are working.
