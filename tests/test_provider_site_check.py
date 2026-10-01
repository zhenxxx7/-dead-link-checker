"""Offline checks for accepting a provider-generated Site Check report."""

import unittest

from provider_site_check import classify_snapshot


def completed_snapshot(**changes):
    snapshot = {
        "state": 3,
        "percent": 100,
        "status_text": "Scan completed",
        "report_ready": True,
        "limited": False,
        "concurrent": False,
        "captcha_visible": False,
    }
    snapshot.update(changes)
    return snapshot


class ProviderSnapshotTests(unittest.TestCase):
    def test_accepts_only_completed_100_percent_provider_report(self):
        self.assertEqual(classify_snapshot(completed_snapshot()), "complete")
        self.assertEqual(
            classify_snapshot(completed_snapshot(report_ready=False)), "pending"
        )
        self.assertEqual(
            classify_snapshot(completed_snapshot(state=2)), "pending"
        )
        self.assertEqual(
            classify_snapshot(completed_snapshot(status_text="Scanning")), "pending"
        )
        self.assertEqual(
            classify_snapshot(completed_snapshot(captcha_visible=True)), "pending"
        )

    def test_rows_and_visible_captcha_do_not_mean_complete(self):
        snapshot = completed_snapshot(
            state=2,
            percent=87,
            status_text="Scanning",
            report_ready=False,
            captcha_visible=True,
            result_rows=14,
        )
        self.assertEqual(classify_snapshot(snapshot), "pending")

    def test_canceled_scan_is_failure(self):
        self.assertEqual(
            classify_snapshot(completed_snapshot(status_text="Scan canceled")),
            "canceled",
        )

    def test_provider_link_limit_is_not_success(self):
        self.assertEqual(
            classify_snapshot(completed_snapshot(limited=True)), "link_limit"
        )

    def test_done_below_100_percent_is_incomplete(self):
        for percent in (0, 99, None):
            with self.subTest(percent=percent):
                self.assertEqual(
                    classify_snapshot(completed_snapshot(percent=percent)),
                    "incomplete",
                )

    def test_concurrent_scan_is_failure(self):
        self.assertEqual(
            classify_snapshot(completed_snapshot(concurrent=True)),
            "concurrent_scan",
        )


if __name__ == "__main__":
    unittest.main()
