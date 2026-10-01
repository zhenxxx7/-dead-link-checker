# Dead Link Checker Site Check

`provider_site_check.py` uses the free [Dead Link Checker Site Check](https://www.deadlinkchecker.com/website-dead-link-checker.asp) to check `https://www.emservices.com.sg/`. It does not crawl the target site itself. The old direct crawler, `dlc_sheets.py`, remains in the repository for reference but is not part of the scheduled client check.

## GitHub Action

[Provider Site Check](.github/workflows/provider_site_check.yml) starts daily at **06:00, 13:00, and 19:00 Singapore time / WITA** (UTC+8). The corresponding UTC schedule is 22:00 on the previous day, 05:00, and 11:00. You can also start it with **Actions → Provider Site Check → Run workflow**. It opens the provider's page, uses best-effort OCR for the site's image code, and saves its result or diagnostic files under the run's `provider-site-check-*` artifact. When `SHEET_WEBHOOK_URL` is configured, completed results can also be sent to the Sheets webhook.

A successful Action means the provider reported **100% complete**. If OCR fails to pass the image code, the script makes up to three total scan attempts. The script has no whole-scan time limit: it waits for the provider to finish. GitHub-hosted jobs still have a [six-hour maximum](https://docs.github.com/en/actions/reference/limits), so an unfinished run can end without a completed report. The provider sets its own per-link timeouts; those `-1 Timeout` rows cannot be changed by removing the script limit. A diagnostic artifact is not a completed report. GitHub scheduled runs may start late. Reports are produced **after** a scan finishes, not at the scheduled start time. The provider's [Auto Check](https://www.deadlinkchecker.com/automatic-broken-link-check.asp) subscription is its supported unattended reporting service.

## Manual fallback

Run on a computer with a desktop browser. Complete the image code yourself in the browser that opens:

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
py provider_site_check.py --target https://www.emservices.com.sg/ --output-dir reports
```

Wait for the provider's scan to reach 100%. Save the completed report for the client; an incomplete or failed scan is not a report.
