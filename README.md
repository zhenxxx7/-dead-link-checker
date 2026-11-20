# Dead Link Checker Site Check

`provider_site_check.py` uses the free [Dead Link Checker Site Check](https://www.deadlinkchecker.com/website-dead-link-checker.asp) to check every website configured in [`sites.json`](sites.json). It does not crawl the target sites itself. The old direct crawler, `dlc_sheets.py`, remains in the repository for reference but is not part of the scheduled client check.

## Monitored Websites

Each website is checked in its own separate job and reported to its own dedicated sheet (1 sheet = 1 website):

| Sheet / Site Key | Target URL | Min Checked URLs | Max Timeout Rows |
| --- | --- | ---: | ---: |
| `emservices` | `https://www.emservices.com.sg/` | 800 | 10 |
| `commercial.yoha` | `https://www.commercial.yoha.com.sg/` | 200 | 10 |
| `emengineering` | `https://www.emengineering.com.sg/` | 150 | 10 |
| `emre` | `https://www.emre.com.sg/` | 150 | 10 |

## GitHub Action

[Provider Site Check](.github/workflows/provider_site_check.yml) starts daily at **06:00, 13:00, and 19:00 Singapore time / WITA** (UTC+8). The corresponding UTC schedule is 22:00 on the previous day, 05:00, and 11:00. You can also start it manually with **Actions → Provider Site Check → Run workflow**, choosing either `all` or a single website.

- **Separate job per site, sequential execution (`max-parallel: 1`, `fail-fast: false`)**: all four sites share the same origin servers, so running one site at a time prevents timeout storms while ensuring one site's failure never blocks the others.
- **Improved CAPTCHA OCR & per-challenge retry budget**: background-flattened grayscale + min-RGB preprocessing with white border padding and per-challenge consecutive retry limits so mid-scan CAPTCHAs do not abort healthy scans.
- **In-place timeout recovery (`--dead-link-retries 4`, 300s cooldown)**: if a 100% provider scan finishes with signs of a truncated crawl (too few checked URLs, too many timeouts, or homepage timeout), the script pauses 5 minutes for origin rate limits to cool down and clicks the provider's own **Retry dead links** button to re-check timed-out pages and continue crawling incrementally without starting over from zero (stopping early if a retry makes no progress). If still inconclusive, it falls back to fresh provider scans (up to 3 total attempts).
- **Combined Excel workbook (`.xlsx`)**: after all site jobs finish, `build_excel_report.py` combines all results into a formatted multi-sheet Excel artifact (`dead-link-report-<run_id>.xlsx`) containing a `Summary` tab plus one dedicated tab per website.

## Google Sheets Setup (1 Sheet per Website)

`Sheet1` is preserved untouched as historical archive. New reports route to dedicated tabs (`emservices`, `commercial.yoha`, `emengineering`, `emre`), summarized automatically on the `Summary` tab.

To route webhook payloads from `SHEET_WEBHOOK_URL` to each website's own tab:
1. Open the Google Sheet → **Extensions → Apps Script**.
2. Replace `Code.gs` with [`apps_script/Code.gs`](apps_script/Code.gs) and save.
3. Click **Deploy → Manage deployments → Edit (pencil icon) → Version: New version → Deploy** (this keeps the exact same `SHEET_WEBHOOK_URL`).

## Manual Fallback

Run on a computer with a desktop browser and complete the image code yourself:

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
py provider_site_check.py --target https://www.emservices.com.sg/ --sheet-name emservices --output-dir reports/emservices --min-checked 800 --max-timeout-rows 10
py build_excel_report.py --sites sites.json --reports-dir reports --output dead-link-report.xlsx
```

Wait for the provider's scan to reach 100%. Save the completed report for the client; an incomplete or failed scan is not a report.
