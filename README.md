# Dead Link Checker status

The client requires scans made on [Dead Link Checker Site Check](https://www.deadlinkchecker.com/website-dead-link-checker.asp) for `https://www.emservices.com.sg/`.

The free Site Check displays an image code that a person must enter for each run. To produce a provider-generated result, open the Site Check page, enter the target URL, choose **Check whole website**, complete the displayed code, and wait for the scan to finish. Start before the 06:00 Singapore time (SGT/WITA) reporting deadline; the free scan has no guaranteed completion time. Save the finished result for the client.

[Auto Check](https://www.deadlinkchecker.com/automatic-broken-link-check.asp) is the provider's subscription service for unattended daily, weekly, or monthly checks and emailed reports. The free Site Check cannot produce an unattended daily or six-hour report.

The previous GitHub Action used a direct crawler, so its output did not meet the provider-only requirement. That Action has been disabled on GitHub and its workflow removed from this repository. `dlc_sheets.py` remains as legacy source only; it is not scheduled or used for the client report.
