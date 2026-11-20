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
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


SITE_CHECK_URL = "https://www.deadlinkchecker.com/website-dead-link-checker.asp"
DEFAULT_TARGET = "https://www.emservices.com.sg/"
SGT = timezone(timedelta(hours=8), "SGT")
POLL_SECONDS = 2
CAPTCHA_RESPONSE_SECONDS = 30
COMMON_TLD_SUFFIXES = (
    ".com.sg",
    ".org.sg",
    ".net.sg",
    ".gov.sg",
    ".edu.sg",
    ".com",
    ".org",
    ".net",
    ".sg",
)


class ScanFailure(RuntimeError):
    pass


class CaptchaFailure(ScanFailure):
    """The image code could not be read or accepted."""


class InconclusiveScan(ScanFailure):
    """The provider finished, but the crawl is unreliable as a client report."""


class TransientProviderFailure(ScanFailure):
    """The provider interrupted a scan transiently (e.g. canceled or concurrent)."""


def now_sgt() -> str:
    return datetime.now(SGT).isoformat(timespec="seconds")


def default_sheet_name(target: str) -> str:
    """Derive a clean Excel / Google Sheets tab name from a target URL."""
    host = (urlsplit(target).hostname or "").lower().removeprefix("www.")
    for suffix in COMMON_TLD_SUFFIXES:
        if host.endswith(suffix) and len(host) > len(suffix):
            host = host[: -len(suffix)]
            break
    sanitized = re.sub(r"[\\/?*\[\]:]+", "_", host).strip("._ ")
    return (sanitized or "site")[:31]


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


def preprocess_captcha_variants(image_bytes: bytes) -> list:
    """Normalize shaded and colored CAPTCHAs into crisp black-on-white views."""
    from PIL import Image, ImageChops, ImageFilter, ImageOps

    rgb = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    gray = rgb.convert("L")
    r, g, b = rgb.split()
    min_rgb = ImageChops.darker(ImageChops.darker(r, g), b)

    variants = []
    for channel in (gray, min_rgb):
        # Estimate local background brightness so shaded gradients flatten cleanly
        # while medium-gray and colored letters remain dark.
        bg = channel.filter(ImageFilter.MaxFilter(size=15))
        flat_bytes = bytes(
            255 if bv == 0 else min(255, (cv * 255) // bv)
            for cv, bv in zip(channel.tobytes(), bg.tobytes())
        )
        flat = Image.frombytes("L", channel.size, flat_bytes)
        enlarged = flat.resize(
            (flat.width * 4, flat.height * 4), Image.Resampling.LANCZOS
        )
        # Generous white padding prevents edge-touching letters from being clipped.
        padded = ImageOps.expand(enlarged, border=40, fill=255)
        smooth = padded.filter(ImageFilter.MedianFilter(size=3))
        variants.extend(
            (
                ImageOps.autocontrast(padded),
                smooth.point(lambda pixel: 255 if pixel > 175 else 0),
                smooth.point(lambda pixel: 255 if pixel > 195 else 0),
                smooth.point(lambda pixel: 255 if pixel > 210 else 0),
            )
        )
    return variants


def captcha_code(image_bytes: bytes) -> str:
    """Read the displayed code with several local image treatments."""
    import pytesseract

    variants = preprocess_captcha_variants(image_bytes)

    def vote(whitelist: str, pattern: str) -> Counter[str]:
        votes: Counter[str] = Counter()
        config = f"--oem 3 -c tessedit_char_whitelist={whitelist}"
        for variant in variants:
            for psm in (7, 8, 13):
                raw = pytesseract.image_to_string(variant, config=f"{config} --psm {psm}")
                candidate = re.sub(pattern, "", raw.upper())
                if 3 <= len(candidate) <= 6:
                    votes[candidate] += 1
        return votes

    # Dead Link Checker codes are 3-5 uppercase A-Z letters; restricting to A-Z
    # avoids S/5, B/8, G/6, Z/2 confusions, with an alphanumeric fallback.
    votes = vote("ABCDEFGHIJKLMNOPQRSTUVWXYZ", r"[^A-Z]")
    if not votes:
        votes = vote("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", r"[^A-Z0-9]")
    if not votes:
        raise CaptchaFailure("OCR could not read the provider's image code.")
    return votes.most_common(1)[0][0]


def result_rows(page) -> list[list[str]]:
    return page.locator("#results tr").evaluate_all(
        """rows => rows.slice(1).map(row =>
            Array.from(row.cells).slice(0, 3).map(cell => cell.innerText.trim())
        ).filter(row => row.length === 3)"""
    )


def report_quality_issues(
    target: str,
    snapshot: dict,
    rows: list[list[str]],
    *,
    min_checked: int,
    max_timeout_rows: int | None,
) -> list[str]:
    """Reject provider-complete reports with signs of a truncated crawl."""
    issues = []
    checked = snapshot.get("checked")
    if min_checked and (not isinstance(checked, int) or checked < min_checked):
        issues.append(f"only {checked} URLs checked; expected at least {min_checked}")

    timeout_rows = [row for row in rows if row and "timeout" in row[0].lower()]
    if max_timeout_rows is not None and len(timeout_rows) > max_timeout_rows:
        issues.append(f"{len(timeout_rows)} timeout rows; maximum {max_timeout_rows}")
    if any(
        len(row) > 1 and row[1].rstrip("/").lower() == target.rstrip("/").lower()
        for row in timeout_rows
    ):
        issues.append("target homepage timed out")
    return issues


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


def spreadsheet_batch(
    target: str,
    snapshot: dict,
    rows: list[list[str]],
    *,
    stamp: str,
    run_id: str = "",
    event_name: str = "",
    repository: str = "",
    status: str = "SCAN COMPLETE",
    note: str = "",
) -> list[list[str]]:
    del run_id, repository  # Do not expose GitHub repository or Actions links in client sheets.
    details = (
        "Dead Link Checker Site Check; "
        f"{snapshot['percent']}% scanned; {snapshot['checked']} checked; "
        f"{snapshot['failed'] + snapshot['denied']} provider errors"
    )
    if note:
        details += f"; {note}"
    return [
        ["\u00a0", "\u00a0", "\u00a0", "\u00a0"],
        [stamp, f"SCAN START ({event_name or 'local'})", target, ""],
        [stamp, status, target, details],
        *[[stamp, *row] for row in rows],
    ]


def send_to_sheets(
    target: str,
    snapshot: dict,
    rows: list[list[str]],
    *,
    status: str = "SCAN COMPLETE",
    note: str = "",
    sheet_name: str | None = None,
) -> bool:
    webhook = os.environ.get("SHEET_WEBHOOK_URL")
    if not webhook:
        print("No SHEET_WEBHOOK_URL; report saved in Action artifact only.", flush=True)
        return False
    import requests

    resolved_sheet = (sheet_name or default_sheet_name(target)).strip()
    sheet_rows = spreadsheet_batch(
        target,
        snapshot,
        rows,
        stamp=now_sgt(),
        run_id=os.environ.get("GITHUB_RUN_ID", ""),
        event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
        repository=os.environ.get("GITHUB_REPOSITORY", ""),
        status=status,
        note=note,
    )
    payload = {
        "sheet": resolved_sheet,
        "target": target,
        "rows": sheet_rows,
    }
    try:
        response = requests.post(webhook, json=payload, timeout=30)
    except requests.RequestException as exc:
        raise ScanFailure(f"Sheets webhook request failed: {type(exc).__name__}") from None
    if not response.ok:
        raise ScanFailure(f"Sheets webhook returned HTTP {response.status_code}")
    try:
        result = response.json()
    except ValueError:
        result = None
    if isinstance(result, dict) and (
        result.get("success") is False
        or result.get("status") in ("error", "failed")
        or result.get("error")
    ):
        raise ScanFailure("Sheets webhook reported an error")
    if isinstance(result, dict) and result.get("sheet") and result.get("sheet") != resolved_sheet:
        print(
            f"Warning: Sheets webhook wrote to '{result.get('sheet')}' "
            f"instead of requested sheet '{resolved_sheet}'.",
            flush=True,
        )
    elif not (isinstance(result, dict) and result.get("sheet")):
        print(
            f"Note: Sheets webhook did not report a sheet name; ensure "
            f"apps_script/Code.gs is deployed so rows route to '{resolved_sheet}'.",
            flush=True,
        )
    print(
        f"Sheets webhook accepted {len(sheet_rows)} rows for sheet '{resolved_sheet}'.",
        flush=True,
    )
    return True


def wait_for_scan_completion(
    page,
    args: argparse.Namespace,
    run_dir: Path,
    metadata: dict,
    submitted_sources: set[str],
) -> dict:
    """Poll the provider page until the active scan/retry pass completes."""
    last_progress_log = 0.0
    last_submission = 0.0
    last_manual_prompt = ""
    consecutive_captcha_attempts = 0
    checked_at_last_captcha: int | None = None
    max_total_captcha_attempts = max(args.captcha_attempts * 4, 24)

    while True:
        if page.is_closed():
            raise ScanFailure("Provider browser page closed before scan completed.")
        snapshot = browser_snapshot(page)
        classification = classify_snapshot(snapshot)
        if classification == "complete":
            scanned = snapshot.get("scanned_url", "").rstrip("/")
            if scanned.lower() != args.target.rstrip("/").lower():
                raise ScanFailure(f"Provider scanned unexpected URL: {scanned!r}")
            return snapshot
        if classification in {"canceled", "concurrent_scan"}:
            raise TransientProviderFailure(f"Provider scan ended: {classification}.")
        if classification != "pending":
            raise ScanFailure(f"Provider scan ended: {classification}.")
        if snapshot["current_page"].split("?")[0] != SITE_CHECK_URL:
            raise ScanFailure("Provider left the Site Check page before completion.")

        if snapshot["captcha_visible"]:
            src = snapshot["captcha_src"]
            if args.ocr_captcha and "sec.asp" in src:
                if src not in submitted_sources:
                    if consecutive_captcha_attempts >= args.captcha_attempts:
                        raise CaptchaFailure(
                            f"Image code rejected after {args.captcha_attempts} consecutive attempts."
                        )
                    if metadata["captcha_attempts"] >= max_total_captcha_attempts:
                        raise CaptchaFailure(
                            f"Image code exceeded {max_total_captcha_attempts} total attempts."
                        )
                    consecutive_captcha_attempts += 1
                    metadata["captcha_attempts"] += 1
                    try:
                        page.wait_for_function(
                            "() => { const image = document.querySelector('#captcha'); "
                            "return image && image.src.includes('/sec.asp') "
                            "&& image.complete && image.naturalWidth > 0; }",
                            timeout=15000,
                        )
                    except PlaywrightTimeoutError as exc:
                        raise CaptchaFailure("Provider image code did not load.") from exc
                    png = page.locator("#captcha").screenshot()
                    (run_dir / f"captcha-{metadata['captcha_attempts']}.png").write_bytes(png)
                    try:
                        code = captcha_code(png)
                        print(
                            f"Image code attempt {metadata['captcha_attempts']} "
                            f"(challenge try {consecutive_captcha_attempts}/{args.captcha_attempts}): "
                            f"OCR read {len(code)} characters.",
                            flush=True,
                        )
                    except CaptchaFailure:
                        # Submit a placeholder code so the provider serves a fresh image
                        # instead of aborting the entire scan over one overlapping CAPTCHA.
                        code = "AAA"
                        print(
                            f"Image code attempt {metadata['captcha_attempts']} "
                            f"(challenge try {consecutive_captcha_attempts}/{args.captcha_attempts}): "
                            "OCR unreadable; requesting a fresh image code.",
                            flush=True,
                        )
                    field = page.locator("#captchatxt")
                    field.fill("")
                    field.press_sequentially(code, delay=50)
                    page.locator("#btn0").click(timeout=10000)
                    submitted_sources.add(src)
                    checked_at_last_captcha = snapshot.get("checked") or 0
                    last_submission = time.monotonic()
                elif time.monotonic() - last_submission > CAPTCHA_RESPONSE_SECONDS:
                    raise CaptchaFailure("Provider did not advance after image code entry.")
            elif not args.ocr_captcha and src != last_manual_prompt:
                print(
                    "Enter the displayed image code in the browser, then click "
                    "check or resume. This may recur during the scan.",
                    flush=True,
                )
                last_manual_prompt = src
        elif (
            checked_at_last_captcha is not None
            and snapshot.get("state") in (1, 2)
            and (snapshot.get("checked") or 0) > checked_at_last_captcha
        ):
            # Captcha truly cleared and the scan checked additional URLs; reset challenge counter.
            consecutive_captcha_attempts = 0
            checked_at_last_captcha = None

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


def try_click_retry_dead_links(page) -> bool:
    """Click Dead Link Checker's built-in 'Retry dead links' button if visible."""
    retry_btn = page.locator("button", has_text="Retry dead links")
    if retry_btn.count() == 0 or not retry_btn.first.is_visible():
        return False
    retry_btn.first.click(timeout=10000)
    try:
        page.wait_for_function(
            """() => {
                const cap = document.querySelector('#capdiv');
                const capVisible = !!(cap && cap.getClientRects().length);
                return capVisible || (typeof sitestate !== 'undefined' && sitestate !== 3);
            }""",
            timeout=15000,
        )
        return True
    except PlaywrightTimeoutError:
        return False


def run(args: argparse.Namespace, attempt_number: int = 1) -> tuple[int, str | None]:
    started = datetime.now(SGT)
    sheet_name = (getattr(args, "sheet_name", None) or default_sheet_name(args.target)).strip()
    dead_link_retries = getattr(args, "dead_link_retries", 4)
    dead_link_retry_delay = getattr(args, "dead_link_retry_delay_seconds", 300)
    run_dir = Path(args.output_dir) / (
        started.strftime("%Y-%m-%d_%H-%M-%S_SGT") + f"_attempt-{attempt_number}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "provider": SITE_CHECK_URL,
        "target": args.target,
        "sheet_name": sheet_name,
        "github_event": os.environ.get("GITHUB_EVENT_NAME"),
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "started_at_sgt": started.isoformat(timespec="seconds"),
        "completed_at_sgt": None,
        "scan_complete": False,
        "quality_passed": False,
        "delivery_complete": False,
        "status_delivered": False,
        "ocr_captcha": args.ocr_captcha,
        "scan_attempt": attempt_number,
        "captcha_attempts": 0,
        "dead_link_retries": 0,
        "state": "starting",
        "error": None,
    }
    page = None
    browser = None
    snapshot: dict = {}
    exit_code = 1
    retry_reason = None
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
            print(f"Provider scan requested for {args.target} (sheet: {sheet_name})", flush=True)
            metadata["state"] = "scanning"

            submitted_sources: set[str] = set()
            snapshot = wait_for_scan_completion(page, args, run_dir, metadata, submitted_sources)
            rows = result_rows(page)
            issues = report_quality_issues(
                args.target,
                snapshot,
                rows,
                min_checked=args.min_checked,
                max_timeout_rows=args.max_timeout_rows,
            )

            # In-place recovery: wait for origin rate limits to cool down, then use the
            # provider's own 'Retry dead links' button so already-OK URLs stay checked
            # while timed-out pages are retried and crawled incrementally.
            while issues and metadata["dead_link_retries"] < dead_link_retries:
                prev_checked = snapshot.get("checked") or 0
                prev_timeouts = sum(1 for row in rows if row and "timeout" in row[0].lower())
                next_retry = metadata["dead_link_retries"] + 1
                print(
                    f"Provider report inconclusive ({'; '.join(issues)}); "
                    f"pausing {dead_link_retry_delay}s before 'Retry dead links' "
                    f"({next_retry}/{dead_link_retries}).",
                    flush=True,
                )
                if dead_link_retry_delay > 0:
                    time.sleep(dead_link_retry_delay)
                if not try_click_retry_dead_links(page):
                    print("Provider 'Retry dead links' button unavailable; keeping current scan.", flush=True)
                    break
                metadata["dead_link_retries"] = next_retry
                snapshot = wait_for_scan_completion(page, args, run_dir, metadata, submitted_sources)
                rows = result_rows(page)
                issues = report_quality_issues(
                    args.target,
                    snapshot,
                    rows,
                    min_checked=args.min_checked,
                    max_timeout_rows=args.max_timeout_rows,
                )
                cur_checked = snapshot.get("checked") or 0
                cur_timeouts = sum(1 for row in rows if row and "timeout" in row[0].lower())
                if issues and cur_checked <= prev_checked and cur_timeouts >= prev_timeouts:
                    print(
                        f"In-place 'Retry dead links' made no progress "
                        f"({cur_checked} checked, {cur_timeouts} timeouts); stopping in-place retries.",
                        flush=True,
                    )
                    break

            metadata["scan_complete"] = True
            metadata["state"] = "complete"
            report_html = page.evaluate("reporttext")
            if "DeadLinkChecker.com - report" not in report_html:
                raise ScanFailure("Provider did not generate its full report.")
            (run_dir / "provider-report.html").write_text(report_html, encoding="utf-8")
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
            metadata["quality_issues"] = issues
            if issues:
                metadata["state"] = "inconclusive"
                save_json(run_dir / "scan_status.json", metadata)
                if attempt_number == args.scan_attempts:
                    metadata["status_delivered"] = send_to_sheets(
                        args.target,
                        snapshot,
                        [],
                        status="SCAN INCONCLUSIVE",
                        note="; ".join(issues),
                        sheet_name=sheet_name,
                    )
                raise InconclusiveScan("; ".join(issues))
            metadata["quality_passed"] = True
            save_json(run_dir / "scan_status.json", metadata)
            metadata["delivery_complete"] = send_to_sheets(
                args.target, snapshot, rows, sheet_name=sheet_name
            )
            exit_code = 0
            print(f"Complete provider report: {run_dir.resolve()}", flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        metadata["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, InconclusiveScan):
            retry_reason = "quality"
        elif isinstance(exc, CaptchaFailure):
            retry_reason = "captcha"
        elif isinstance(exc, TransientProviderFailure):
            retry_reason = "provider"
        if not metadata["scan_complete"]:
            metadata["state"] = "failed"
            save_browser_evidence(page, run_dir, completed=False)
        print(f"ERROR: {metadata['error']}", file=sys.stderr, flush=True)
    finally:
        metadata["completed_at_sgt"] = metadata["completed_at_sgt"] or now_sgt()
        if snapshot:
            metadata.setdefault("percent", snapshot.get("percent"))
            metadata.setdefault("checked", snapshot.get("checked"))
            metadata.setdefault("provider_status", snapshot.get("status_text"))
            metadata.setdefault("provider_statistics", snapshot.get("statistics_text"))
        save_json(run_dir / "scan_status.json", metadata)
    return exit_code, retry_reason


def run_with_retries(args: argparse.Namespace) -> int:
    for attempt_number in range(1, args.scan_attempts + 1):
        exit_code, retry_reason = run(args, attempt_number)
        if exit_code == 0:
            return 0
        if not (
            retry_reason in {"captcha", "quality", "provider"}
            and (retry_reason != "captcha" or args.ocr_captcha)
            and attempt_number < args.scan_attempts
        ):
            return exit_code
        delay = (
            args.quality_retry_delay_seconds
            if retry_reason == "quality"
            else args.retry_delay_seconds
        )
        print(
            f"Provider {retry_reason} check failed; starting fresh provider scan "
            f"{attempt_number + 1}/{args.scan_attempts} in "
            f"{delay} seconds.",
            flush=True,
        )
        time.sleep(delay)
    return 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--sheet-name", help="Target Google Sheet / Excel tab name (defaults to domain slug)")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--ocr-captcha", action="store_true")
    parser.add_argument("--captcha-attempts", type=int, default=8)
    parser.add_argument("--scan-attempts", type=int, default=3)
    parser.add_argument("--retry-delay-seconds", type=int, default=20)
    parser.add_argument("--quality-retry-delay-seconds", type=int, default=300)
    parser.add_argument("--dead-link-retries", type=int, default=4)
    parser.add_argument("--dead-link-retry-delay-seconds", type=int, default=300)
    parser.add_argument("--min-checked", type=int, default=0)
    parser.add_argument("--max-timeout-rows", type=int)
    parser.add_argument("--headed", action="store_true", help="Show browser during OCR mode")
    args = parser.parse_args(argv)
    if args.captcha_attempts < 1 or args.scan_attempts < 1:
        parser.error("Attempt counts must be positive.")
    if (
        args.retry_delay_seconds < 0
        or args.quality_retry_delay_seconds < 0
        or args.dead_link_retries < 0
        or args.dead_link_retry_delay_seconds < 0
    ):
        parser.error("Retry counts and delays cannot be negative.")
    if args.min_checked < 0 or (
        args.max_timeout_rows is not None and args.max_timeout_rows < 0
    ):
        parser.error("Report quality thresholds cannot be negative.")
    if not args.target.startswith(("https://", "http://")):
        parser.error("Target must include http:// or https://.")
    if not args.sheet_name:
        args.sheet_name = default_sheet_name(args.target)
    return args


if __name__ == "__main__":
    raise SystemExit(run_with_retries(parse_args()))
