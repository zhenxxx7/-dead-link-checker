"""Build a formatted multi-sheet Excel (.xlsx) report across all monitored websites.

Creates one Summary sheet plus one dedicated sheet per website so each link's
dead-link report stays cleanly separated.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


SGT = timezone(timedelta(hours=8), "SGT")

NAVY_FILL = PatternFill("solid", fgColor="243B5D")
SUBHEADER_FILL = PatternFill("solid", fgColor="E8EEF5")
ZEBRA_FILL = PatternFill("solid", fgColor="F8FAFC")
WHITE_FILL = PatternFill("solid", fgColor="FFFFFF")
GREEN_FILL = PatternFill("solid", fgColor="E6F4EA")
AMBER_FILL = PatternFill("solid", fgColor="FEF7E0")
RED_FILL = PatternFill("solid", fgColor="FCE8E6")
TOTAL_FILL = PatternFill("solid", fgColor="EEF2F7")

TITLE_FONT = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
LABEL_FONT = Font(name="Calibri", size=10, bold=True, color="1E3A5F")
BODY_FONT = Font(name="Calibri", size=10, color="1F2937")
BOLD_FONT = Font(name="Calibri", size=10, bold=True, color="1F2937")
LINK_FONT = Font(name="Calibri", size=10, color="1155CC", underline="single")
GREEN_FONT = Font(name="Calibri", size=10, bold=True, color="137333")
AMBER_FONT = Font(name="Calibri", size=10, bold=True, color="B06000")
RED_FONT = Font(name="Calibri", size=10, bold=True, color="B00000")

THIN_BORDER = Border(
    left=Side(style="thin", color="D1D5DB"),
    right=Side(style="thin", color="D1D5DB"),
    top=Side(style="thin", color="D1D5DB"),
    bottom=Side(style="thin", color="D1D5DB"),
)


def now_sgt() -> str:
    return datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT")


def normalize_target(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def load_sites(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"Expected non-empty site list in {path}")
    return data


def discover_site_reports(reports_dir: Path) -> list[tuple[Path, dict]]:
    """Find every scan_status.json under reports_dir."""
    if not reports_dir.exists():
        return []
    found: list[tuple[Path, dict]] = []
    for status_file in sorted(reports_dir.rglob("scan_status.json")):
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("target"):
            found.append((status_file.parent, payload))
    return found


def select_best_attempt(
    site: dict, attempts: list[tuple[Path, dict]]
) -> tuple[Path | None, dict | None]:
    """Pick the passing report if available, else latest completed, else latest attempt."""
    target_key = normalize_target(site["target"])
    matching = [
        (folder, meta)
        for folder, meta in attempts
        if normalize_target(meta.get("target", "")) == target_key
        or meta.get("sheet_name") == site["sheet"]
    ]
    if not matching:
        return None, None

    def rank(item: tuple[Path, dict]) -> tuple[int, int, str, int]:
        _, meta = item
        return (
            1 if meta.get("quality_passed") else 0,
            1 if meta.get("scan_complete") else 0,
            str(meta.get("completed_at_sgt") or meta.get("started_at_sgt") or ""),
            int(meta.get("scan_attempt") or 0),
        )

    return max(matching, key=rank)


def read_result_rows(run_dir: Path | None) -> list[list[str]]:
    if run_dir is None:
        return []
    csv_path = run_dir / "provider-results.csv"
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        rows = list(reader)
    return rows[1:] if len(rows) > 1 else []


def status_label_and_style(meta: dict | None) -> tuple[str, PatternFill, Font]:
    if not meta:
        return "NOT RUN", RED_FILL, RED_FONT
    if meta.get("quality_passed"):
        return "SCAN COMPLETE", GREEN_FILL, GREEN_FONT
    if meta.get("state") == "inconclusive":
        return "SCAN INCONCLUSIVE", AMBER_FILL, AMBER_FONT
    return "SCAN FAILED", RED_FILL, RED_FONT


def format_notes(meta: dict | None) -> str:
    if not meta:
        return "No scan artifact found for this website."
    issues = meta.get("quality_issues") or []
    if issues:
        return "; ".join(str(item) for item in issues)
    if meta.get("error"):
        return str(meta["error"])
    retries = int(meta.get("dead_link_retries") or 0)
    if retries > 0:
        return f"Passed after {retries} in-place dead-link retry pass(es)"
    return "OK"


def build_workbook(sites: list[dict], reports_dir: Path, output_path: Path) -> Path:
    attempts = discover_site_reports(reports_dir)
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    summary.views.sheetView[0].showGridLines = True

    # Title banner on Summary sheet.
    summary.merge_cells("A1:I1")
    summary["A1"] = "Dead Link Checker — Multi-Site Report Summary"
    summary["A1"].fill = NAVY_FILL
    summary["A1"].font = TITLE_FONT
    summary["A1"].alignment = Alignment(horizontal="left", vertical="center", indent=1)
    summary.row_dimensions[1].height = 30

    summary.merge_cells("A2:I2")
    summary["A2"] = f"Generated: {now_sgt()} | 1 dedicated sheet per website"
    summary["A2"].fill = SUBHEADER_FILL
    summary["A2"].font = LABEL_FONT
    summary["A2"].alignment = Alignment(horizontal="left", vertical="center", indent=1)
    summary.row_dimensions[2].height = 20

    summary_headers = [
        "Website",
        "Target URL",
        "Status",
        "Completed At (SGT)",
        "% Scanned",
        "URLs Checked",
        "Errors",
        "Notes / Quality Issues",
        "Report Sheet",
    ]
    header_row = 4
    summary.row_dimensions[header_row].height = 24
    for col_idx, heading in enumerate(summary_headers, start=1):
        cell = summary.cell(header_row, col_idx, heading)
        cell.fill = NAVY_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = THIN_BORDER

    total_checked = 0
    total_errors = 0
    for idx, site in enumerate(sites):
        row_num = header_row + 1 + idx
        run_dir, meta = select_best_attempt(site, attempts)
        rows = read_result_rows(run_dir)
        label, st_fill, st_font = status_label_and_style(meta)
        checked = meta.get("checked") if meta and isinstance(meta.get("checked"), int) else 0
        errors = (
            meta.get("errors")
            if meta and isinstance(meta.get("errors"), int)
            else len(rows)
        )
        percent = meta.get("percent") if meta and meta.get("percent") is not None else 0
        completed_at = (meta or {}).get("completed_at_sgt") or "-"
        notes = format_notes(meta)
        sheet_name = str(site["sheet"])[:31]

        total_checked += checked
        total_errors += errors

        values = [
            site["name"],
            site["target"],
            label,
            completed_at,
            f"{percent}%" if meta else "-",
            checked if meta else "-",
            errors if meta else "-",
            notes,
            sheet_name,
        ]
        summary.row_dimensions[row_num].height = 24
        row_fill = ZEBRA_FILL if idx % 2 else WHITE_FILL
        for col_idx, value in enumerate(values, start=1):
            cell = summary.cell(row_num, col_idx, value)
            cell.border = THIN_BORDER
            cell.fill = row_fill
            cell.font = BODY_FONT
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            if col_idx == 1:
                cell.font = BOLD_FONT
            elif col_idx == 2:
                cell.hyperlink = site["target"]
                cell.font = LINK_FONT
            elif col_idx == 3:
                cell.fill = st_fill
                cell.font = st_font
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif col_idx in (5, 6, 7):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif col_idx == 9:
                cell.hyperlink = f"#'{sheet_name}'!A1"
                cell.font = LINK_FONT
                cell.alignment = Alignment(horizontal="center", vertical="center")

        # Build dedicated per-site sheet (1 sheet = 1 website).
        populate_site_sheet(wb, site, meta, rows)

    total_row = header_row + 1 + len(sites)
    summary.row_dimensions[total_row].height = 24
    totals = [
        "TOTAL",
        f"{len(sites)} websites monitored",
        "",
        "",
        "",
        total_checked,
        total_errors,
        "",
        "",
    ]
    for col_idx, value in enumerate(totals, start=1):
        cell = summary.cell(total_row, col_idx, value)
        cell.fill = TOTAL_FILL
        cell.font = BOLD_FONT
        cell.border = THIN_BORDER
        cell.alignment = Alignment(
            horizontal="center" if col_idx in (5, 6, 7) else "left",
            vertical="center",
        )

    summary.freeze_panes = "A5"
    summary_widths = [18, 38, 20, 26, 12, 14, 10, 44, 18]
    for col_idx, width in enumerate(summary_widths, start=1):
        summary.column_dimensions[get_column_letter(col_idx)].width = width

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return output_path


def populate_site_sheet(
    wb: Workbook, site: dict, meta: dict | None, rows: list[list[str]]
) -> None:
    sheet_name = str(site["sheet"])[:31]
    ws = wb.create_sheet(title=sheet_name)
    ws.views.sheetView[0].showGridLines = True

    label, st_fill, st_font = status_label_and_style(meta)

    ws.merge_cells("A1:D1")
    ws["A1"] = f"Dead Link Report — {site['target']}"
    ws["A1"].fill = NAVY_FILL
    ws["A1"].font = TITLE_FONT
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 28

    info_rows = [
        ("Status", label),
        ("Completed At (SGT)", (meta or {}).get("completed_at_sgt") or "-"),
        (
            "Provider Summary",
            (meta or {}).get("provider_statistics", "").replace("\n", " | ")
            or format_notes(meta),
        ),
    ]
    for offset, (key, val) in enumerate(info_rows, start=2):
        ws.row_dimensions[offset].height = 20
        k_cell = ws.cell(offset, 1, key)
        k_cell.fill = SUBHEADER_FILL
        k_cell.font = LABEL_FONT
        k_cell.border = THIN_BORDER
        k_cell.alignment = Alignment(vertical="center")

        ws.merge_cells(start_row=offset, start_column=2, end_row=offset, end_column=4)
        v_cell = ws.cell(offset, 2, val)
        v_cell.border = THIN_BORDER
        v_cell.alignment = Alignment(vertical="center", wrap_text=True)
        for extra_col in (3, 4):
            ws.cell(offset, extra_col).border = THIN_BORDER
        if key == "Status":
            v_cell.fill = st_fill
            v_cell.font = st_font
        else:
            v_cell.font = BODY_FONT

    table_header_row = 6
    ws.row_dimensions[table_header_row].height = 24
    headers = ["No", "Status", "URL", "Source Link Text"]
    for col_idx, heading in enumerate(headers, start=1):
        cell = ws.cell(table_header_row, col_idx, heading)
        cell.fill = NAVY_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = THIN_BORDER

    if not meta or not meta.get("quality_passed"):
        ws.merge_cells(start_row=7, start_column=1, end_row=7, end_column=4)
        notice = ws.cell(
            7,
            1,
            f"Report withheld ({label}): {format_notes(meta)}",
        )
        notice.fill = AMBER_FILL if meta and meta.get("state") == "inconclusive" else RED_FILL
        notice.font = AMBER_FONT if meta and meta.get("state") == "inconclusive" else RED_FONT
        notice.alignment = Alignment(vertical="center", wrap_text=True)
        notice.border = THIN_BORDER
        for extra_col in (2, 3, 4):
            ws.cell(7, extra_col).border = THIN_BORDER
        ws.row_dimensions[7].height = 26
    elif not rows:
        ws.merge_cells(start_row=7, start_column=1, end_row=7, end_column=4)
        ok_cell = ws.cell(7, 1, "No broken links found in this completed scan.")
        ok_cell.fill = GREEN_FILL
        ok_cell.font = GREEN_FONT
        ok_cell.alignment = Alignment(vertical="center")
        ok_cell.border = THIN_BORDER
        for extra_col in (2, 3, 4):
            ws.cell(7, extra_col).border = THIN_BORDER
        ws.row_dimensions[7].height = 24
    else:
        for idx, row in enumerate(rows, start=1):
            r = table_header_row + idx
            ws.row_dimensions[r].height = 22
            status_val = row[0] if len(row) > 0 else ""
            url_val = row[1] if len(row) > 1 else ""
            src_val = row[2] if len(row) > 2 else ""
            fill = ZEBRA_FILL if idx % 2 == 0 else WHITE_FILL

            c_no = ws.cell(r, 1, idx)
            c_st = ws.cell(r, 2, status_val)
            c_url = ws.cell(r, 3, url_val)
            c_src = ws.cell(r, 4, src_val)

            for cell in (c_no, c_st, c_url, c_src):
                cell.fill = fill
                cell.border = THIN_BORDER
                cell.alignment = Alignment(vertical="center", wrap_text=True)

            c_no.font = BODY_FONT
            c_no.alignment = Alignment(horizontal="center", vertical="center")
            c_st.font = AMBER_FONT if ("403" in status_val or "429" in status_val) else RED_FONT
            if url_val.startswith(("http://", "https://")):
                c_url.hyperlink = url_val
                c_url.font = LINK_FONT
            else:
                c_url.font = BODY_FONT
            c_src.font = BODY_FONT

        ws.auto_filter.ref = f"A{table_header_row}:D{table_header_row + len(rows)}"

    ws.freeze_panes = f"A{table_header_row + 1}"
    col_widths = [8, 28, 62, 50]
    for col_idx, width in enumerate(col_widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sites", default="sites.json", type=Path)
    parser.add_argument("--site", help="Optional single site name from sites.json to include")
    parser.add_argument("--reports-dir", default="reports", type=Path)
    parser.add_argument("--output", default="dead-link-report.xlsx", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sites = load_sites(args.sites)
    if args.site:
        sites = [s for s in sites if s["name"] == args.site]
        if not sites:
            raise ValueError(f"Unknown site name: {args.site!r}")
    out = build_workbook(sites, args.reports_dir, args.output)
    print(f"Wrote Excel report: {out.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
