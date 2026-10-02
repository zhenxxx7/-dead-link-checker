# Dead Link Checker Site Check

`provider_site_check.py` uses the free [Dead Link Checker Site Check](https://www.deadlinkchecker.com/website-dead-link-checker.asp) to check `https://www.emservices.com.sg/`. It does not crawl the target site itself. The old direct crawler, `dlc_sheets.py`, remains in the repository for reference but is not part of the scheduled client check.

## GitHub Action

[Provider Site Check](.github/workflows/provider_site_check.yml) starts daily at **06:00, 13:00, and 19:00 Singapore time / WITA** (UTC+8). The corresponding UTC schedule is 22:00 on the previous day, 05:00, and 11:00. You can also start it with **Actions → Provider Site Check → Run workflow**. It opens the provider's page, uses best-effort OCR for the site's image code, and saves its result or diagnostic files under the run's `provider-site-check-*` artifact. When `SHEET_WEBHOOK_URL` is configured, completed results can also be sent to the Sheets webhook.

A successful Action means the provider reported **100% complete** and passed a report quality check. A scheduled run on 2 October 2026 found only 302 URLs and 64 timeouts, while two manual runs found about 1,090 URLs and 3–5 errors. The workflow now requires at least 800 checked URLs, at most 10 timeout rows, and no timeout on the target homepage. An inconclusive report is kept in the Action artifact and retried with a fresh provider scan, up to three total attempts. Only a passing report sends its link rows to Sheets. If all attempts are inconclusive, Sheets receives one **SCAN INCONCLUSIVE** summary without the suspect link rows, and the Action fails. OCR failures also retry. The script has no whole-scan time limit: it waits for the provider to finish. GitHub-hosted jobs still have a [six-hour maximum](https://docs.github.com/en/actions/reference/limits). The provider sets its own per-link timeouts; removing the script limit cannot change those `-1 Timeout` results. GitHub scheduled runs may start late. Reports are produced **after** a scan finishes, not at the scheduled start time.

Each Sheets delivery starts with a blank separator and a `SCAN START` row showing whether GitHub started the run on a schedule or manually, plus a link to that run. Existing sheet rows remain as history; new scan blocks are separated. The 800-URL threshold is specific to the current site and should be reviewed if its size changes substantially.

## Manual fallback

Run on a computer with a desktop browser. Complete the image code yourself in the browser that opens:

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
py provider_site_check.py --target https://www.emservices.com.sg/ --output-dir reports --min-checked 800 --max-timeout-rows 10
```

Wait for the provider's scan to reach 100%. Save the completed report for the client; an incomplete or failed scan is not a report.
