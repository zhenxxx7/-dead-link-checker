"""Tests for generating the multi-sheet Excel report across all websites."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

import build_excel_report as report_builder


class BuildExcelReportTests(unittest.TestCase):
    def test_builds_summary_and_one_dedicated_sheet_per_website(self):
        sites = [
            {
                "name": "emservices",
                "sheet": "emservices",
                "target": "https://www.emservices.com.sg/",
                "min_checked": 800,
                "max_timeout_rows": 10,
            },
            {
                "name": "commercial.yoha",
                "sheet": "commercial.yoha",
                "target": "https://www.commercial.yoha.com.sg/",
                "min_checked": 200,
                "max_timeout_rows": 10,
            },
            {
                "name": "emengineering",
                "sheet": "emengineering",
                "target": "https://www.emengineering.com.sg/",
                "min_checked": 150,
                "max_timeout_rows": 10,
            },
            {
                "name": "emre",
                "sheet": "emre",
                "target": "https://www.emre.com.sg/",
                "min_checked": 150,
                "max_timeout_rows": 10,
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            # Site 1: passing scan with 1 error (and an older inconclusive attempt that should be ignored).
            bad_dir = reports_dir / "emservices" / "2026-11-20_09-00-00_SGT_attempt-1"
            bad_dir.mkdir(parents=True)
            (bad_dir / "scan_status.json").write_text(
                json.dumps(
                    {
                        "target": "https://www.emservices.com.sg/",
                        "sheet_name": "emservices",
                        "scan_complete": True,
                        "quality_passed": False,
                        "state": "inconclusive",
                        "percent": 100,
                        "checked": 300,
                        "errors": 40,
                        "quality_issues": ["only 300 URLs checked; expected at least 800"],
                    }
                ),
                encoding="utf-8",
            )
            good_dir = reports_dir / "emservices" / "2026-11-20_09-10-00_SGT_attempt-2"
            good_dir.mkdir(parents=True)
            (good_dir / "scan_status.json").write_text(
                json.dumps(
                    {
                        "target": "https://www.emservices.com.sg/",
                        "sheet_name": "emservices",
                        "scan_complete": True,
                        "quality_passed": True,
                        "state": "complete",
                        "completed_at_sgt": "2026-11-20T09:20:00+08:00",
                        "percent": 100,
                        "checked": 1150,
                        "errors": 1,
                        "provider_statistics": "100% scanned - 1150/1150 URLs checked, 1149 OK, 1 failed",
                    }
                ),
                encoding="utf-8",
            )
            with (good_dir / "provider-results.csv").open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["Status", "URL", "Source link text"])
                w.writerow(["403 Forbidden", "https://www.emservices.com.sg/xmlrpc.php?rsd", "link/href"])

            # Site 2: passing scan with 2 errors.
            yoha_dir = reports_dir / "commercial.yoha" / "2026-11-20_09-30-00_SGT_attempt-1"
            yoha_dir.mkdir(parents=True)
            (yoha_dir / "scan_status.json").write_text(
                json.dumps(
                    {
                        "target": "https://www.commercial.yoha.com.sg/",
                        "sheet_name": "commercial.yoha",
                        "scan_complete": True,
                        "quality_passed": True,
                        "state": "complete",
                        "completed_at_sgt": "2026-11-20T09:35:00+08:00",
                        "percent": 100,
                        "checked": 308,
                        "errors": 2,
                    }
                ),
                encoding="utf-8",
            )
            with (yoha_dir / "provider-results.csv").open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["Status", "URL", "Source link text"])
                w.writerow(["404 Not Found", "http://www.eemusic.com.sg/contact-us/", "Contact us now!"])
                w.writerow(["403 Forbidden", "https://www.commercial.yoha.com.sg/xmlrpc.php?rsd", "link/href"])

            # Site 3: inconclusive scan (suspect rows must be withheld from table).
            eng_dir = reports_dir / "emengineering" / "2026-11-20_09-40-00_SGT_attempt-1"
            eng_dir.mkdir(parents=True)
            (eng_dir / "scan_status.json").write_text(
                json.dumps(
                    {
                        "target": "https://www.emengineering.com.sg/",
                        "sheet_name": "emengineering",
                        "scan_complete": True,
                        "quality_passed": False,
                        "state": "inconclusive",
                        "completed_at_sgt": "2026-11-20T09:45:00+08:00",
                        "percent": 100,
                        "checked": 40,
                        "errors": 25,
                        "quality_issues": ["only 40 URLs checked; expected at least 150"],
                    }
                ),
                encoding="utf-8",
            )
            with (eng_dir / "provider-results.csv").open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["Status", "URL", "Source link text"])
                w.writerow(["-1 Timeout", "https://www.emengineering.com.sg/", "<No Text>"])

            # Site 4 (emre): no artifact present -> NOT RUN.
            out_path = Path(tmp) / "dead-link-report.xlsx"
            report_builder.build_workbook(sites, reports_dir, out_path)

            wb = load_workbook(out_path)
            self.assertEqual(
                wb.sheetnames,
                ["Summary", "emservices", "commercial.yoha", "emengineering", "emre"],
            )

            summary = wb["Summary"]
            self.assertEqual(summary["A5"].value, "emservices")
            self.assertEqual(summary["C5"].value, "SCAN COMPLETE")
            self.assertEqual(summary["F5"].value, 1150)
            self.assertEqual(summary["G5"].value, 1)

            self.assertEqual(summary["A6"].value, "commercial.yoha")
            self.assertEqual(summary["C6"].value, "SCAN COMPLETE")
            self.assertEqual(summary["F6"].value, 308)
            self.assertEqual(summary["G6"].value, 2)

            self.assertEqual(summary["A7"].value, "emengineering")
            self.assertEqual(summary["C7"].value, "SCAN INCONCLUSIVE")

            self.assertEqual(summary["A8"].value, "emre")
            self.assertEqual(summary["C8"].value, "NOT RUN")

            # Verify site isolation: emservices only has emservices links, commercial.yoha only its own.
            ws_em = wb["emservices"]
            self.assertEqual(ws_em["C7"].value, "https://www.emservices.com.sg/xmlrpc.php?rsd")
            self.assertIsNone(ws_em["C8"].value)

            ws_yoha = wb["commercial.yoha"]
            self.assertEqual(ws_yoha["C7"].value, "http://www.eemusic.com.sg/contact-us/")
            self.assertEqual(ws_yoha["C8"].value, "https://www.commercial.yoha.com.sg/xmlrpc.php?rsd")

            # Inconclusive site withholds raw timeout rows.
            ws_eng = wb["emengineering"]
            self.assertIn("Report withheld (SCAN INCONCLUSIVE)", ws_eng["A7"].value)

            # Never expose GitHub Actions URLs anywhere in the workbook.
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    for cell in row:
                        self.assertNotIn("github.com", str(cell or "").lower())


if __name__ == "__main__":
    unittest.main()
