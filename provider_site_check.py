"""Save a report from Dead Link Checker's free Site Check page.

The default mode leaves the site's image code for a person to enter. The
--ocr-captcha mode makes a bounded, best-effort OCR attempt for scheduled runs.
Only the provider's own completed scan is accepted as a successful report.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright


SITE_CHECK_URL = "https://www.deadlinkchecker.com/website-dead-link-checker.asp"
DEFAULT_TARGET = "https://www.emservices.com.sg/"
SGT = timezone(timedelta(hours=8), "SGT")
POLL_SECONDS = 2
CAPTCHA_RESPONSE_SECONDS = 30


class ScanFailure(RuntimeError):
    pass


def now_sgt() -> str:
    return datetime.now(SGT).isoformat(timespec="seconds")


def browser_snapshot(page) -> dict:
    """Read the provider's own scan state; do not infer completion from rows."""
    return page.evaluate(
        """() => {
            const cap = document.querySelector('#capdiv');
            const image = document.querySelector('#captcha');
            const stat = document.querySelector('#stat');
            const statistics = document.querySelector('#statistics');
            const s = typeof stats === 'undefined' ? null : stats;
            return {
                state: typeof sitestate === 'undefined' ? null : sitestate,
                percent: s ? s.percent : null,
                checked: s ? s.checked : null,
                failed: s ? s.failed : null,
                denied: s ? s.denied : null,
                total: s ? s.total : null,
                status_text: stat ? stat.innerText.trim() : '',
                statistics_text: statistics ? statistics.innerText.trim() : '',
                captcha_visible: !!(cap && cap.getClientRects().length),
                captcha_src: image ? image.src : '',
                limited: typeof limit !== 'undefined' && !!limit,
                concurrent: typeof concurrent !== 'undefined' && !!concurrent,
                report_ready: typeof reporttext !== 'undefined'
                    && reporttext.includes('DeadLinkChecker.com - report'),
                scanned_url: typeof initurl !== 'undefined' ? initurl : '',
                current_page: location.href
            };
        }"""
    )


def classify_snapshot(snapshot: dict) -> str:
    """Return pending, complete, or a failure reason."""
    status = snapshot.get("status_text", "").lower()
    if "scan canceled" in status:
        return "canceled"
    if snapshot.get("concurrent"):
        return "concurrent_scan"
    if snapshot.get("captcha_visible"):
        return "pending"
    if snapshot.get("state") == 3 and "scan completed" in status:
        if snapshot.get("percent") != 100:
            return "incomplete"
        if snapshot.get("limited"):
            return "link_limit"
        if snapshot.get("report_ready"):
            return "complete"
    return "pending"


def captcha_code(image_bytes: bytes) -> str:
    """Read the displayed code with several local image treatments."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    import pytesseract

    original = Image.open(io.BytesIO(image_bytes)).convert("L")
    enlarged = ImageOps.autocontrast(original).resize(
        (original.width * 4, original.height * 4), Image.Resampling.LANCZOS
    )
    boosted = ImageEnhance.Contrast(enlarged).enhance(2.5)
    variants = (
        enlarged,
        boosted,
        boosted.filter(ImageFilter.MedianFilter(size=3)),
        boosted.point(lambda pixel: 255 if pixel > 135 else 0),
        boosted.point(lambda pixel: 255 if pixel > 170 else 0),
    )
    votes: Counter[str] = Counter()
    config = (
        "--oem 3 -c tessedit_char_whitelist="
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    )
    for variant in variants:
        for psm in (7, 8, 13):
            raw = pytesseract.image_to_string(variant, config=f"{config} --psm {psm}")
            candidate = re.sub(r"[^A-Z0-9]", "", raw.upper())
            if 3 <= len(candidate) <= 8:
                votes[candidate] += 1
    if not votes:
        raise ScanFailure("OCR could not read the provider's image code.")
    return votes.most_common(1)[0][0]


def result_rows(page) -> list[list[str]]:
    return page.locator("#results tr").evaluate_all(
        """rows => rows.slice(1).map(row =>
            Array.from(row.cells).slice(0, 3).map(cell => cell.innerText.trim())
        ).filter(row => row.length === 3)"""
    )


def save_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def save_browser_evidence(page, run_dir: Path, *, completed: bool) -> None:
    if page is None or page.is_closed():
        return
    prefix = "provider" if completed else "diagnostic"
    try:
        page.screenshot(path=str(run_dir / f"{prefix}-screenshot.png"), full_page=True)
    except Exception as exc:
        print(f"Could not save screenshot: {exc}", flush=True)
    try:
        (run_dir / f"{prefix}-page.html").write_text(page.content(), encoding="utf-8")
    except Exception as exc:
        print(f"Could not save page HTML: {exc}", flush=True)


def send_to_sheets(target: str, snapshot: dict, rows: list[list[str]]) -> None:
    webhook = os.environ.get("SHEET_WEBHOOK_URL")
    if not webhook:
        print("No SHEET_WEBHOOK_URL; report saved in Action artifact only.", flush=True)
        return
    import requests

    stamp = now_sgt()
    summary = [
        stamp,
        "SCAN COMPLETE",
        target,
        (
            "Dead Link Checker Site Check; "
            f"{snapshot['percent']}% scanned; {snapshot['checked']} checked; "
            f"{snapshot['failed'] + snapshot['denied']} errors"
        ),
    ]
    sheet_rows = [summary] + [[stamp, *row] for row in rows]
    response = requests.post(webhook, json={"rows": sheet_rows}, timeout=30)
    response.raise_for_status()
    print(f"Sheets webhook accepted {len(sheet_rows)} rows.", flush=True)


def run(args: argparse.Namespace) -> int:
    started = datetime.now(SGT)
    run_dir = Path(args.output_dir) / started.strftime("%Y-%m-%d_%H-%M-%S_SGT")
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "provider": SITE_CHECK_URL,
        "target": args.target,
        "started_at_sgt": started.isoformat(timespec="seconds"),
        "completed_at_sgt": None,
        "scan_complete": False,
        "delivery_complete": False,
        "ocr_captcha": args.ocr_captcha,
        "captcha_attempts": 0,
        "state": "starting",
        "error": None,
    }
    page = None
    browser = None
    snapshot: dict = {}
    exit_code = 1
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=args.ocr_captcha and not args.headed
            )
            context = browser.new_context(
                timezone_id="Asia/Singapore",
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()
            print(f"Open {SITE_CHECK_URL}", flush=True)
            page.goto(SITE_CHECK_URL, wait_until="domcontentloaded", timeout=45000)
            page.locator("#url").fill(args.target)
            page.locator("#scantypefull").check()
            page.wait_for_function(
                "() => document.querySelector('#btn0')?.innerText.trim().toLowerCase() === 'check'",
                timeout=15000,
            )
            page.locator("#btn0").click()
            print(f"Provider scan requested for {args.target}", flush=True)
            metadata["state"] = "scanning"

            deadline = time.monotonic() + args.max_minutes * 60
            last_progress_log = 0.0
            submitted_sources: set[str] = set()
            last_submission = 0.0
            last_manual_prompt = ""
            done_since = None
            while time.monotonic() < deadline:
                if page.is_closed():
                    raise ScanFailure("Provider browser page closed before scan completed.")
                snapshot = browser_snapshot(page)
                classification = classify_snapshot(snapshot)
                if classification == "complete":
                    scanned = snapshot.get("scanned_url", "").rstrip("/")
                    if scanned.lower() != args.target.rstrip("/").lower():
                        raise ScanFailure(f"Provider scanned unexpected URL: {scanned!r}")
                    metadata["scan_complete"] = True
                    metadata["state"] = "complete"
                    break
                if classification != "pending":
                    raise ScanFailure(f"Provider scan ended: {classification}.")
                if snapshot["state"] == 3:
                    done_since = done_since or time.monotonic()
                    if time.monotonic() - done_since > 15:
                        raise ScanFailure(
                            "Provider entered DONE state without a complete 100% report."
                        )
                if snapshot["current_page"].split("?")[0] != SITE_CHECK_URL:
                    raise ScanFailure("Provider left the Site Check page before completion.")

                if snapshot["captcha_visible"]:
                    src = snapshot["captcha_src"]
                    if args.ocr_captcha and "sec.asp" in src:
                        if src not in submitted_sources:
                            if metadata["captcha_attempts"] >= args.captcha_attempts:
                                raise ScanFailure(
                                    f"Image code rejected after {args.captcha_attempts} attempts."
                                )
                            metadata["captcha_attempts"] += 1
                            page.wait_for_function(
                                "() => { const image = document.querySelector('#captcha'); "
                                "return image && image.src.includes('/sec.asp') "
                                "&& image.complete && image.naturalWidth > 0; }",
                                timeout=15000,
                            )
                            png = page.locator("#captcha").screenshot()
                            (run_dir / f"captcha-{metadata['captcha_attempts']}.png").write_bytes(png)
                            code = captcha_code(png)
                            print(
                                f"Image code attempt {metadata['captcha_attempts']}: "
                                f"OCR read {len(code)} characters.",
                                flush=True,
                            )
                            field = page.locator("#captchatxt")
                            field.fill("")
                            field.press_sequentially(code, delay=50)
                            page.locator("#btn0").click(timeout=10000)
                            submitted_sources.add(src)
                            last_submission = time.monotonic()
                        elif time.monotonic() - last_submission > CAPTCHA_RESPONSE_SECONDS:
                            raise ScanFailure("Provider did not advance after image code entry.")
                    elif not args.ocr_captcha and src != last_manual_prompt:
                        print(
                            "Enter the displayed image code in the browser, then click "
                            "check or resume. This may recur during the scan.",
                            flush=True,
                        )
                        last_manual_prompt = src

                now = time.monotonic()
                if now - last_progress_log >= 30:
                    print(
                        f"Provider progress: {snapshot['percent']}% scanned, "
                        f"{snapshot['checked']} checked; "
                        f"{snapshot['statistics_text'][:160]}",
                        flush=True,
                    )
                    last_progress_log = now
                page.wait_for_timeout(POLL_SECONDS * 1000)
            else:
                raise ScanFailure(f"Provider scan did not finish in {args.max_minutes} minutes.")

            report_html = page.evaluate("reporttext")
            if "DeadLinkChecker.com - report" not in report_html:
                raise ScanFailure("Provider did not generate its full report.")
            (run_dir / "provider-report.html").write_text(report_html, encoding="utf-8")
            rows = result_rows(page)
            with (run_dir / "provider-results.csv").open("w", newline="", encoding="utf-8-sig") as out:
                writer = csv.writer(out)
                writer.writerow(["Status", "URL", "Source link text"])
                writer.writerows(rows)
            save_browser_evidence(page, run_dir, completed=True)
            metadata.update(
                {
                    "completed_at_sgt": now_sgt(),
                    "percent": snapshot["percent"],
                    "checked": snapshot["checked"],
                    "errors": snapshot["failed"] + snapshot["denied"],
                    "result_rows": len(rows),
                    "provider_status": snapshot["status_text"],
                    "provider_statistics": snapshot["statistics_text"],
                }
            )
            save_json(run_dir / "scan_status.json", metadata)
            send_to_sheets(args.target, snapshot, rows)
            metadata["delivery_complete"] = True
            exit_code = 0
            print(f"Complete provider report: {run_dir.resolve()}", flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        metadata["error"] = f"{type(exc).__name__}: {exc}"
        if not metadata["scan_complete"]:
            metadata["state"] = "failed"
        print(f"ERROR: {metadata['error']}", file=sys.stderr, flush=True)
    finally:
        metadata["completed_at_sgt"] = metadata["completed_at_sgt"] or now_sgt()
        if snapshot:
            metadata.setdefault("percent", snapshot.get("percent"))
            metadata.setdefault("checked", snapshot.get("checked"))
            metadata.setdefault("provider_status", snapshot.get("status_text"))
            metadata.setdefault("provider_statistics", snapshot.get("statistics_text"))
        save_json(run_dir / "scan_status.json", metadata)
    return exit_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--max-minutes", type=int, default=120)
    parser.add_argument("--ocr-captcha", action="store_true")
    parser.add_argument("--captcha-attempts", type=int, default=6)
    parser.add_argument("--headed", action="store_true", help="Show browser during OCR mode")
    args = parser.parse_args()
    if args.max_minutes < 1 or args.captcha_attempts < 1:
        parser.error("Timeout and CAPTCHA attempt count must be positive.")
    if not args.target.startswith(("https://", "http://")):
        parser.error("Target must include http:// or https://.")
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
