# Dead Link Checker Site Check

`provider_site_check.py` uses the free [Dead Link Checker Site Check](https://www.deadlinkchecker.com/website-dead-link-checker.asp) to check `https://www.emservices.com.sg/`. It does not crawl the target site itself. The old direct crawler, `dlc_sheets.py`, remains in the repository for reference but is not part of the scheduled client check.

## GitHub Action

[Provider Site Check](.github/workflows/provider_site_check.yml) attempts one scan daily at **00:30 Singapore time / WITA** (16:30 UTC on the previous day). You can also start it with **Actions → Provider Site Check → Run workflow**. It opens the provider's page, uses best-effort OCR for the site's image code, and saves its result or diagnostic files under the run's `provider-site-check-*` artifact. When `SHEET_WEBHOOK_URL` is configured, completed results can also be sent to the Sheets webhook.

A successful Action means the provider reported **100% complete**. A CAPTCHA rejection, scan timeout, or partial result makes the Action fail; a diagnostic artifact is not a completed report. OCR may fail because the free site requires an image code. GitHub scheduled runs may start late, and the provider controls scan duration. **A report by 06:00 SGT/WITA is not guaranteed.** The provider's [Auto Check](https://www.deadlinkchecker.com/automatic-broken-link-check.asp) subscription is its supported unattended reporting service.

## Manual fallback

Run on a computer with a desktop browser. Complete the image code yourself in the browser that opens:

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
py provider_site_check.py --target https://www.emservices.com.sg/ --output-dir reports --max-minutes 120
```

Wait for the provider's scan to reach 100%. Save the completed report for the client; an incomplete or failed scan is not a report.
