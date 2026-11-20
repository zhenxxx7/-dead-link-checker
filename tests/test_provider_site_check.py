"""Offline checks for accepting a provider-generated Site Check report."""

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import requests
from PIL import Image

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


class SheetNameTests(unittest.TestCase):
    def test_derives_clean_sheet_names_for_monitored_sites(self):
        self.assertEqual(
            checker.default_sheet_name("https://www.emservices.com.sg/"),
            "emservices",
        )
        self.assertEqual(
            checker.default_sheet_name("https://www.commercial.yoha.com.sg/"),
            "commercial.yoha",
        )
        self.assertEqual(
            checker.default_sheet_name("https://www.emengineering.com.sg/"),
            "emengineering",
        )
        self.assertEqual(
            checker.default_sheet_name("https://www.emre.com.sg/"),
            "emre",
        )


class CaptchaPreprocessingTests(unittest.TestCase):
    def test_preprocess_captcha_variants_pads_and_normalizes(self):
        img = Image.new("RGB", (120, 40), (220, 220, 220))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        variants = checker.preprocess_captcha_variants(buf.getvalue())
        self.assertEqual(len(variants), 8)
        for variant in variants:
            # 120*4 + 40*2 = 560, 40*4 + 40*2 = 240
            self.assertEqual(variant.size, (560, 240))
            self.assertEqual(variant.mode, "L")

    def test_mid_scan_captcha_gets_fresh_consecutive_budget(self):
        snapshots = [
            # Initial CAPTCHA: 2 attempts before clearing.
            {
                "state": 4, "percent": 0, "checked": 0, "failed": 0, "denied": 0,
                "status_text": "stopped", "statistics_text": "", "captcha_visible": True,
                "captcha_src": "https://www.deadlinkchecker.com/sec.asp?r=1",
                "limited": False, "concurrent": False, "report_ready": False,
                "scanned_url": "https://www.emre.com.sg/",
                "current_page": checker.SITE_CHECK_URL,
            },
            {
                "state": 4, "percent": 0, "checked": 0, "failed": 0, "denied": 0,
                "status_text": "stopped", "statistics_text": "", "captcha_visible": True,
                "captcha_src": "https://www.deadlinkchecker.com/sec.asp?r=2",
                "limited": False, "concurrent": False, "report_ready": False,
                "scanned_url": "https://www.emre.com.sg/",
                "current_page": checker.SITE_CHECK_URL,
            },
            # Scanning progresses -> consecutive counter resets!
            {
                "state": 1, "percent": 50, "checked": 110, "failed": 0, "denied": 0,
                "status_text": "Checking", "statistics_text": "50%", "captcha_visible": False,
                "captcha_src": "", "limited": False, "concurrent": False, "report_ready": False,
                "scanned_url": "https://www.emre.com.sg/",
                "current_page": checker.SITE_CHECK_URL,
            },
            # Mid-scan CAPTCHA: 2 more attempts (would exceed captcha_attempts=2 if cumulative!).
            {
                "state": 4, "percent": 50, "checked": 110, "failed": 0, "denied": 0,
                "status_text": "stopped", "statistics_text": "", "captcha_visible": True,
                "captcha_src": "https://www.deadlinkchecker.com/sec.asp?r=3",
                "limited": False, "concurrent": False, "report_ready": False,
                "scanned_url": "https://www.emre.com.sg/",
                "current_page": checker.SITE_CHECK_URL,
            },
            {
                "state": 4, "percent": 50, "checked": 110, "failed": 0, "denied": 0,
                "status_text": "stopped", "statistics_text": "", "captcha_visible": True,
                "captcha_src": "https://www.deadlinkchecker.com/sec.asp?r=4",
                "limited": False, "concurrent": False, "report_ready": False,
                "scanned_url": "https://www.emre.com.sg/",
                "current_page": checker.SITE_CHECK_URL,
            },
            # Scan completes cleanly.
            {
                "state": 3, "percent": 100, "checked": 221, "failed": 2, "denied": 0,
                "status_text": "Scan completed with 2 errors.", "statistics_text": "100%",
                "captcha_visible": False, "captcha_src": "", "limited": False,
                "concurrent": False, "report_ready": True,
                "scanned_url": "https://www.emre.com.sg/",
                "current_page": checker.SITE_CHECK_URL,
            },
        ]
        page = MagicMock()
        page.is_closed.return_value = False
        page.locator.return_value.screenshot.return_value = b"fake-png"
        args = SimpleNamespace(
            target="https://www.emre.com.sg/",
            ocr_captcha=True,
            captcha_attempts=2,
        )
        metadata = {"captcha_attempts": 0}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(checker, "browser_snapshot", side_effect=snapshots):
                with patch.object(checker, "captcha_code", return_value="ABCD"):
                    final = checker.wait_for_scan_completion(
                        page, args, Path(tmp), metadata, set()
                    )
        self.assertEqual(final["checked"], 221)
        self.assertEqual(metadata["captcha_attempts"], 4)


class InPlaceRetryRecoveryTests(unittest.TestCase):
    def test_recovers_inconclusive_scan_via_retry_dead_links(self):
        first_snap = {
            "state": 3, "percent": 100, "checked": 403, "failed": 44, "denied": 0,
            "status_text": "Scan completed with 44 errors.",
            "statistics_text": "100% scanned - 403/403 URLs checked, 359 OK, 44 failed",
            "captcha_visible": False, "captcha_src": "", "limited": False,
            "concurrent": False, "report_ready": True,
            "scanned_url": "https://www.emservices.com.sg/",
            "current_page": checker.SITE_CHECK_URL,
        }
        second_snap = {
            "state": 3, "percent": 100, "checked": 1143, "failed": 3, "denied": 0,
            "status_text": "Scan completed with 3 errors.",
            "statistics_text": "100% scanned - 1143/1143 URLs checked, 1140 OK, 3 failed",
            "captcha_visible": False, "captcha_src": "", "limited": False,
            "concurrent": False, "report_ready": True,
            "scanned_url": "https://www.emservices.com.sg/",
            "current_page": checker.SITE_CHECK_URL,
        }
        first_rows = [["-1 Timeout", "https://www.emservices.com.sg", "<No Text>"]]
        second_rows = [
            ["403 Forbidden", "https://www.emservices.com.sg/xmlrpc.php?rsd", "link/href"]
        ]

        fake_page = MagicMock()
        fake_page.is_closed.return_value = False
        fake_page.evaluate.return_value = "<html>DeadLinkChecker.com - report</html>"
        fake_page.content.return_value = "<html>ok</html>"
        fake_context = MagicMock()
        fake_context.new_page.return_value = fake_page
        fake_browser = MagicMock()
        fake_browser.new_context.return_value = fake_context
        fake_pw = MagicMock()
        fake_pw.chromium.launch.return_value = fake_browser
        fake_cm = MagicMock()
        fake_cm.__enter__.return_value = fake_pw
        fake_cm.__exit__.return_value = False

        with tempfile.TemporaryDirectory() as tmp:
            args = checker.parse_args(
                [
                    "--target", "https://www.emservices.com.sg/",
                    "--sheet-name", "emservices",
                    "--output-dir", tmp,
                    "--min-checked", "800",
                    "--max-timeout-rows", "10",
                    "--dead-link-retries", "2",
                    "--dead-link-retry-delay-seconds", "0",
                    "--ocr-captcha",
                ]
            )
            with (
                patch.object(checker, "sync_playwright", return_value=fake_cm),
                patch.object(
                    checker, "wait_for_scan_completion", side_effect=[first_snap, second_snap]
                ) as wait_mock,
                patch.object(checker, "result_rows", side_effect=[first_rows, second_rows]),
                patch.object(checker, "try_click_retry_dead_links", return_value=True) as retry_click,
                patch.object(checker, "send_to_sheets", return_value=True) as send_mock,
            ):
                exit_code, retry_reason = checker.run(args, attempt_number=1)

        self.assertEqual((exit_code, retry_reason), (0, None))
        self.assertEqual(wait_mock.call_count, 2)
        retry_click.assert_called_once_with(fake_page)
        send_mock.assert_called_once_with(
            "https://www.emservices.com.sg/",
            second_snap,
            second_rows,
            sheet_name="emservices",
        )


class ReportQualityTests(unittest.TestCase):
    target = "https://www.emservices.com.sg/"

    def test_accepts_full_scan_with_isolated_external_timeout(self):
        issues = checker.report_quality_issues(
            self.target,
            {"checked": 1089},
            [["-1 Timeout", "http://www.sptel.com", "link"]],
            min_checked=800,
            max_timeout_rows=10,
        )
        self.assertEqual(issues, [])

    def test_rejects_truncated_scan_and_timeout_burst(self):
        rows = [["-1 Timeout", self.target.rstrip("/"), "home"]]
        rows += [
            ["-1 Timeout", f"{self.target}news/{index}/", "news"]
            for index in range(63)
        ]
        issues = checker.report_quality_issues(
            self.target,
            {"checked": 302},
            rows,
            min_checked=800,
            max_timeout_rows=10,
        )
        self.assertEqual(len(issues), 3)
        self.assertTrue(any("302 URLs" in issue for issue in issues))
        self.assertTrue(any("64 timeout" in issue for issue in issues))
        self.assertIn("target homepage timed out", issues)

    def test_rejects_homepage_timeout_even_with_full_coverage(self):
        issues = checker.report_quality_issues(
            self.target,
            {"checked": 1090},
            [["-1 Timeout", self.target.rstrip("/"), "home"]],
            min_checked=800,
            max_timeout_rows=10,
        )
        self.assertEqual(issues, ["target homepage timed out"])


class SpreadsheetBatchTests(unittest.TestCase):
    def test_separates_runs_and_identifies_source(self):
        batch = checker.spreadsheet_batch(
            "https://www.emservices.com.sg/",
            {"percent": 100, "checked": 1089, "failed": 2, "denied": 1},
            [["403 Forbidden", "https://example.com", "example"]],
            stamp="2026-10-02T14:57:32+08:00",
            run_id="36975026323",
            event_name="workflow_dispatch",
            repository="zhenxxx7/-dead-link-checker",
            status="SCAN COMPLETE",
            note="",
        )
        self.assertEqual(len(batch), 4)
        self.assertEqual([len(row) for row in batch], [4, 4, 4, 4])
        self.assertEqual(batch[0], ["\u00a0"] * 4)
        self.assertEqual(batch[1][1], "SCAN START (workflow_dispatch)")
        self.assertEqual(
            batch[1][3],
            "https://github.com/zhenxxx7/-dead-link-checker/actions/runs/36975026323",
        )
        self.assertEqual(batch[2][1], "SCAN COMPLETE")
        self.assertEqual(batch[3][1], "403 Forbidden")

    def test_webhook_receives_complete_separated_batch_and_sheet_name(self):
        snapshot = {"percent": 100, "checked": 308, "failed": 7, "denied": 0}
        with patch.dict(
            checker.os.environ,
            {
                "SHEET_WEBHOOK_URL": "https://example.test/webhook",
                "GITHUB_RUN_ID": "123",
                "GITHUB_EVENT_NAME": "schedule",
                "GITHUB_REPOSITORY": "owner/repo",
            },
        ):
            with patch("requests.post") as post:
                post.return_value.ok = True
                post.return_value.json.return_value = {
                    "success": True,
                    "sheet": "commercial.yoha",
                }
                self.assertTrue(
                    checker.send_to_sheets(
                        "https://www.commercial.yoha.com.sg/",
                        snapshot,
                        [],
                        sheet_name="commercial.yoha",
                    )
                )
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["sheet"], "commercial.yoha")
        self.assertEqual(body["target"], "https://www.commercial.yoha.com.sg/")
        payload = body["rows"]
        self.assertEqual([len(row) for row in payload], [4, 4, 4])
        self.assertEqual(payload[0], ["\u00a0"] * 4)
        self.assertEqual(payload[1][1], "SCAN START (schedule)")
        self.assertEqual(payload[2][1], "SCAN COMPLETE")

    def test_webhook_application_error_is_not_marked_delivered(self):
        snapshot = {"percent": 100, "checked": 1089, "failed": 0, "denied": 0}
        with patch.dict(checker.os.environ, {"SHEET_WEBHOOK_URL": "https://example.test/webhook"}):
            with patch("requests.post") as post:
                post.return_value.ok = True
                post.return_value.json.return_value = {"success": False}
                with self.assertRaisesRegex(checker.ScanFailure, "webhook reported an error"):
                    checker.send_to_sheets("https://example.com/", snapshot, [])

    def test_webhook_request_error_does_not_expose_url(self):
        snapshot = {"percent": 100, "checked": 1089, "failed": 0, "denied": 0}
        secret_url = "https://example.test/private-secret"
        with patch.dict(checker.os.environ, {"SHEET_WEBHOOK_URL": secret_url}):
            with patch("requests.post", side_effect=requests.Timeout(secret_url)):
                with self.assertRaises(checker.ScanFailure) as caught:
                    checker.send_to_sheets("https://example.com/", snapshot, [])
        self.assertNotIn(secret_url, str(caught.exception))


class ScanRetryTests(unittest.TestCase):
    def args(self, *, ocr_captcha=True, scan_attempts=3):
        return SimpleNamespace(
            ocr_captcha=ocr_captcha,
            scan_attempts=scan_attempts,
            retry_delay_seconds=20,
            quality_retry_delay_seconds=300,
        )

    def test_retries_captcha_failure_then_succeeds(self):
        args = self.args()
        with patch.object(checker, "run", side_effect=[(1, "captcha"), (0, None)]) as run:
            with patch.object(checker.time, "sleep") as sleep:
                self.assertEqual(checker.run_with_retries(args), 0)
        self.assertEqual(run.call_args_list, [call(args, 1), call(args, 2)])
        sleep.assert_called_once_with(20)

    def test_retries_transient_provider_failure_then_succeeds(self):
        args = self.args()
        with patch.object(checker, "run", side_effect=[(1, "provider"), (0, None)]) as run:
            with patch.object(checker.time, "sleep") as sleep:
                self.assertEqual(checker.run_with_retries(args), 0)
        self.assertEqual(run.call_args_list, [call(args, 1), call(args, 2)])
        sleep.assert_called_once_with(20)

    def test_stops_after_bounded_captcha_retries(self):
        args = self.args()
        with patch.object(checker, "run", return_value=(1, "captcha")) as run:
            with patch.object(checker.time, "sleep") as sleep:
                self.assertEqual(checker.run_with_retries(args), 1)
        self.assertEqual(run.call_args_list, [call(args, 1), call(args, 2), call(args, 3)])
        self.assertEqual(sleep.call_args_list, [call(20), call(20)])

    def test_does_not_retry_other_failure(self):
        args = self.args()
        with patch.object(checker, "run", return_value=(1, None)) as run:
            with patch.object(checker.time, "sleep") as sleep:
                self.assertEqual(checker.run_with_retries(args), 1)
        run.assert_called_once_with(args, 1)
        sleep.assert_not_called()

    def test_does_not_retry_without_ocr_mode(self):
        args = self.args(ocr_captcha=False)
        with patch.object(checker, "run", return_value=(1, "captcha")) as run:
            with patch.object(checker.time, "sleep") as sleep:
                self.assertEqual(checker.run_with_retries(args), 1)
        run.assert_called_once_with(args, 1)
        sleep.assert_not_called()

    def test_success_does_not_retry(self):
        args = self.args()
        with patch.object(checker, "run", return_value=(0, None)) as run:
            with patch.object(checker.time, "sleep") as sleep:
                self.assertEqual(checker.run_with_retries(args), 0)
        run.assert_called_once_with(args, 1)
        sleep.assert_not_called()

    def test_retries_inconclusive_report_after_longer_pause(self):
        args = self.args()
        with patch.object(checker, "run", side_effect=[(1, "quality"), (0, None)]) as run:
            with patch.object(checker.time, "sleep") as sleep:
                self.assertEqual(checker.run_with_retries(args), 0)
        self.assertEqual(run.call_args_list, [call(args, 1), call(args, 2)])
        sleep.assert_called_once_with(300)


if __name__ == "__main__":
    unittest.main()
