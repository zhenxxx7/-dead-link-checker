# Scheduled dead link checker

GitHub Actions scans `https://www.emservices.com.sg/` daily at 03:00 UTC and on manual dispatch. It reads the site's advertised sitemap, follows links on same-site HTML pages, and checks linked assets and external URLs without crawling external sites. Results with HTTP errors or request failures go to the `SHEET_WEBHOOK_URL` repository secret as `{"rows": [[timestamp, status, url, source_text], ...]}`.

The `scan-report` Action artifact contains `scan_report.csv` and `scan_status.json`. Check `scan_complete` in the JSON file before using a report. The workflow fails if it cannot finish within 750 pages, 5,000 URLs, or 25 minutes, or if the Sheets webhook rejects the rows.

Run locally without sending Sheets rows:

```sh
python -m pip install -r requirements.txt
python dlc_sheets.py --dry-run
```

Set `TARGET_URL` to audit a different public site. The checker reads server-rendered HTML; links created only by JavaScript are outside its scope. HTTP 403, 429, and network failures need manual review because some sites block automated checks.
