/**
 * Google Apps Script Web App webhook for Dead Link Checker reports.
 *
 * Routes each monitored website to its own dedicated tab (1 sheet = 1 website)
 * and keeps Sheet1 untouched as historical archive.
 *
 * Deployment update (keeps the same SHEET_WEBHOOK_URL):
 *   1. Open the Google Sheet -> Extensions -> Apps Script.
 *   2. Replace Code.gs with this file and Save.
 *   3. Click Deploy -> Manage deployments -> Edit (pencil icon)
 *      -> Version: New version -> Deploy.
 */

var HEADERS = ["Timestamp", "Status", "URL", "Source Link Text"];

function ensureReportSheet_(ss, sheetName) {
  var sheet = ss.getSheetByName(sheetName);
  if (!sheet) {
    sheet = ss.insertSheet(sheetName);
  }
  if (sheet.getLastRow() === 0) {
    var headerRange = sheet.getRange(1, 1, 1, HEADERS.length);
    headerRange.setValues([HEADERS]);
    headerRange
      .setBackground("#243B5D")
      .setFontColor("#FFFFFF")
      .setFontWeight("bold")
      .setHorizontalAlignment("center")
      .setVerticalAlignment("middle");
    sheet.setFrozenRows(1);
    sheet.setColumnWidth(1, 210);
    sheet.setColumnWidth(2, 220);
    sheet.setColumnWidth(3, 480);
    sheet.setColumnWidth(4, 420);
  }
  return sheet;
}

function normalizeRows_(rawRows) {
  var width = HEADERS.length;
  return rawRows.map(function (row) {
    var cells = Array.isArray(row) ? row.slice(0, width) : [String(row)];
    while (cells.length < width) {
      cells.push("");
    }
    return cells;
  });
}

function styleAppendedRows_(sheet, startRow, rows) {
  for (var i = 0; i < rows.length; i++) {
    var rowNum = startRow + i;
    var status = String(rows[i][1] || "").trim();
    var rowRange = sheet.getRange(rowNum, 1, 1, HEADERS.length);

    if (!status) {
      // Blank visual separator between scan blocks.
      rowRange.setBackground(null).setFontWeight("normal").setFontColor("#000000");
      sheet.setRowHeight(rowNum, 10);
    } else if (status.indexOf("SCAN START") === 0) {
      rowRange.setBackground("#E8EEF5").setFontWeight("bold").setFontColor("#1E3A5F");
    } else if (status === "SCAN COMPLETE") {
      rowRange.setBackground("#E6F4EA").setFontWeight("bold").setFontColor("#137333");
    } else if (status === "SCAN INCONCLUSIVE") {
      rowRange.setBackground("#FCE8E6").setFontWeight("bold").setFontColor("#A50E0E");
    } else {
      rowRange.setBackground("#FFFFFF").setFontWeight("normal").setFontColor("#1F2937");
      sheet.getRange(rowNum, 2).setFontColor("#B00000").setFontWeight("bold");
    }
  }
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var payload = JSON.parse((e && e.postData && e.postData.contents) || "{}");
    var rawRows = payload.rows;
    if (!Array.isArray(rawRows) || rawRows.length === 0) {
      return jsonResponse_({ success: false, error: "rows must be a non-empty array" });
    }

    var sheetName = String(payload.sheet || "Sheet1").trim() || "Sheet1";
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ensureReportSheet_(ss, sheetName);
    var rows = normalizeRows_(rawRows);

    var startRow = sheet.getLastRow() + 1;
    // Skip leading separator if this is the very first data block right under header row 1.
    if (startRow === 2 && rows.length > 1 && String(rows[0][1] || "").trim() === "") {
      rows = rows.slice(1);
    }

    sheet.getRange(startRow, 1, rows.length, HEADERS.length).setValues(rows);
    styleAppendedRows_(sheet, startRow, rows);

    return jsonResponse_({
      success: true,
      sheet: sheetName,
      appended: rows.length
    });
  } catch (err) {
    return jsonResponse_({
      success: false,
      error: String(err && err.message ? err.message : err)
    });
  } finally {
    lock.releaseLock();
  }
}

function jsonResponse_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
