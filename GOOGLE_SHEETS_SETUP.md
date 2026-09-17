# Connect your Google Sheet

**Superseded:** the hourly workflow now uploads to Google Docs. Follow [Google Docs setup](GOOGLE_DOCS_SETUP.md). The Sheets information below is retained for the optional standalone Sheets script; it no longer describes the active hourly workflow.

The uploader and hourly GitHub workflow are ready. Your sheet ID is already in `config.json`:

[Open your destination sheet](https://docs.google.com/spreadsheets/d/1mEvVF0RansevGDPLpYiKL16NBuZQ6i4AkxLWMa04HWI/edit)

Google access is the one remaining setup step. Until it is configured, the job keeps saving `links.txt` and reports that Sheets upload is pending setup.

## One-time setup

1. In [Google Cloud Console](https://console.cloud.google.com/), select or create a project. Enable the **Google Sheets API** in **APIs & Services > Library**.
2. Go to **IAM & Admin > Service Accounts > Create service account**. Name it `utopia-link-collector`. No project-wide role is needed for writing to a sheet shared with this account.
3. Open that service account and copy its email address (ending in `iam.gserviceaccount.com`). In your Google Sheet, click **Share**, add this email as **Editor**, and save. You do not need to make the sheet public.
4. In the service account's **Keys** tab, choose **Add key > Create new key > JSON**. Google downloads a JSON key file. If your organization forbids keys, ask its administrator about Workload Identity Federation instead; this uploader currently expects a JSON service-account key.
5. Open [this repository's Actions secrets](https://github.com/h27096/utopia-link-collector/settings/secrets/actions). Choose **New repository secret**. Name it exactly `GOOGLE_SERVICE_ACCOUNT_JSON`. Paste the entire downloaded JSON file as the value and save. Do not paste the key into chat, upload it as a repository file, or put it in `config.json`.
6. Open [GitHub Actions](https://github.com/h27096/utopia-link-collector/actions), select **Collect Utopia links**, then **Run workflow** on `main`. After the scan, the **Append saved links to Google Sheets** step reports how many links were added. Subsequent runs use the existing hourly schedule.

Google's official [service-account setup instructions](https://developers.google.com/workspace/guides/create-credentials#service-account) describe credential creation. The secret authorizes only what the service account can access; keep its access limited to this sheet.

## Where links go

By default the uploader uses the first tab, placing one URL per row in column A after the last row containing data. It does not create a tab or add/change a header. If you want a header, add `Link` in A1 yourself before the first sync. To lock the destination to a particular tab, set `google_sheets.tab_name` in `config.json` to its exact name. A blank name means the first tab at each run, so choose a name before rearranging tabs.

Optional repository Actions variables `GOOGLE_SHEET_ID` and `GOOGLE_SHEET_TAB` override the config. The sheet ID is the portion between `/d/` and `/edit` in its URL, not the `gid`.

## Append-only behavior

- Every sync reads **all saved links**, so the first sync imports previous discoveries and later runs retry any links missed during an outage.
- Before each batch of up to 500 additions, it reads the existing tab and skips URLs already visible anywhere in its cells. Existing duplicates, notes, blank rows, and ordering are left alone.
- New links go after the last data row using Google's [appendCells request](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/request#AppendCellsRequest). There are no clear, delete, sort, or update requests. `links.txt` remains unchanged by this uploader.
- Use full visible URLs for manual entries. A hyperlink hidden behind a display label is not detected as an existing URL. URL comparison uses the same root-path, hostname-case, and default-port normalization as the collector.
- The GitHub workflow prevents overlapping runs. Avoid running another uploader or adding the same URLs manually during sync: Sheets does not offer an atomic uniqueness constraint across independent writers.
- Write requests are not automatically retried after ambiguous network failures. The next run reads the sheet again, preventing a normal retry from re-adding a batch that Google already accepted. An upload error does not remove local or repository links.

## Optional local use

Install only the extra upload dependencies:

```powershell
python -m pip install -r requirements-sheets.txt
$env:GOOGLE_SERVICE_ACCOUNT_JSON = Get-Content -Raw 'C:\secure-folder\your-key.json'
python sync_sheets.py --dry-run
python sync_sheets.py
```

Keep the key outside the repository. `python collector.py` still works without Google dependencies or credentials. `python sync_sheets.py` fails visibly when not configured; the hourly workflow uses `--if-configured` to keep basic collection operational during setup.

## Troubleshooting

- **Pending setup:** the Actions secret is absent or empty.
- **HTTP 403:** check that the Sheets API is enabled and the service account email has Editor access. Quotas can also cause failures.
- **HTTP 404:** check the sheet ID and sharing permissions.
- **Tab does not exist:** correct the configured tab name or leave it blank to use the first tab.
- **HTTP 429:** API quota was reached; a later scheduled run rechecks the remaining links.
- **Authentication error:** confirm that the secret contains the complete JSON key and that the key is still active.

Validation: 21 offline tests pass, including preserving existing sheet duplicates, repeat uploads, dry runs, and recovery after a write response is lost. A live Sheets upload has not been tested because service-account access has not yet been configured.
