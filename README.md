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

## GitHub Actions (Separate Workflows per Website)

Each website has its own independent workflow scheduled 3x daily (Singapore time / WITA, UTC+8) — staggered 10 minutes apart so all four sites finish inside ~45 minutes without triggering origin rate limits on their shared AWS Singapore servers (`13.251.10.125`, `3.1.11.209`) — and can also be triggered individually anytime:

- [`Check — emre`](.github/workflows/check_emre.yml) — `06:00`, `13:00`, `19:00` SGT
- [`Check — emengineering`](.github/workflows/check_emengineering.yml) — `06:10`, `13:10`, `19:10` SGT
- [`Check — commercial.yoha`](.github/workflows/check_commercial_yoha.yml) — `06:20`, `13:20`, `19:20` SGT
- [`Check — emservices`](.github/workflows/check_emservices.yml) — `06:30`, `13:30`, `19:30` SGT
- [`Provider Site Check (All Sites — Manual Parallel)`](.github/workflows/provider_site_check.yml) — manual trigger to run all 4 sites in parallel and download a single combined multi-sheet Excel workbook (`dead-link-report-<run_id>.xlsx`).

- **Parallel & independent execution**: all four websites run concurrently in separate workflows so one slow or failing website never delays or fails the other three.
- **Improved CAPTCHA OCR & per-challenge retry budget**: background-flattened grayscale + min-RGB preprocessing with white border padding and per-challenge consecutive retry limits so mid-scan CAPTCHAs do not abort healthy scans.
- **In-place timeout recovery (`--dead-link-retries 4`, 300s cooldown)**: if a 100% provider scan finishes with signs of a truncated crawl (too few checked URLs, too many timeouts, or homepage timeout), the script pauses 5 minutes for origin rate limits to cool down and clicks the provider's own **Retry dead links** button to re-check timed-out pages and continue crawling incrementally without starting over from zero (stopping early if a retry makes no progress). If still inconclusive, it falls back to fresh provider scans (up to 3 total attempts).
- **Excel reports (`.xlsx`)**: every workflow run also produces a formatted `.xlsx` workbook via `build_excel_report.py` (with `Summary` + dedicated site sheet(s), free of GitHub Actions links).

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
