"""Offline checks for accepting a provider-generated Site Check report."""

import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

import provider_site_check as checker


classify_snapshot = checker.classify_snapshot


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


class ScanRetryTests(unittest.TestCase):
    def args(self, *, ocr_captcha=True, scan_attempts=3):
        return SimpleNamespace(
            ocr_captcha=ocr_captcha,
            scan_attempts=scan_attempts,
            retry_delay_seconds=20,
        )

    def test_retries_captcha_failure_then_succeeds(self):
        args = self.args()
        with patch.object(checker, "run", side_effect=[(1, True), (0, False)]) as run:
            with patch.object(checker.time, "sleep") as sleep:
                self.assertEqual(checker.run_with_retries(args), 0)
        self.assertEqual(run.call_args_list, [call(args, 1), call(args, 2)])
        sleep.assert_called_once_with(20)

    def test_stops_after_bounded_captcha_retries(self):
        args = self.args()
        with patch.object(checker, "run", return_value=(1, True)) as run:
            with patch.object(checker.time, "sleep") as sleep:
                self.assertEqual(checker.run_with_retries(args), 1)
        self.assertEqual(run.call_args_list, [call(args, 1), call(args, 2), call(args, 3)])
        self.assertEqual(sleep.call_args_list, [call(20), call(20)])

    def test_does_not_retry_other_failure(self):
        args = self.args()
        with patch.object(checker, "run", return_value=(1, False)) as run:
            with patch.object(checker.time, "sleep") as sleep:
                self.assertEqual(checker.run_with_retries(args), 1)
        run.assert_called_once_with(args, 1)
        sleep.assert_not_called()

    def test_does_not_retry_without_ocr_mode(self):
        args = self.args(ocr_captcha=False)
        with patch.object(checker, "run", return_value=(1, True)) as run:
            with patch.object(checker.time, "sleep") as sleep:
                self.assertEqual(checker.run_with_retries(args), 1)
        run.assert_called_once_with(args, 1)
        sleep.assert_not_called()

    def test_success_does_not_retry(self):
        args = self.args()
        with patch.object(checker, "run", return_value=(0, False)) as run:
            with patch.object(checker.time, "sleep") as sleep:
                self.assertEqual(checker.run_with_retries(args), 0)
        run.assert_called_once_with(args, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
