/**
 * 勤怠アプリ → スプレッドシート連携用 Google Apps Script
 *
 * 使い方:
 *  1. 連携したい Google スプレッドシートを開く
 *  2. 上部メニュー「拡張機能」→「Apps Script」を開く
 *  3. このファイルの中身をすべて貼り付けて保存
 *  4. 「デプロイ」→「デプロイを管理」→ 鉛筆 → バージョン「新バージョン」→ デプロイ
 *  5. ブラウザでウェブアプリURL(/exec)を開き、バージョン表示を確認
 */

// アプリ側の SHEETS_WEBHOOK_SECRET と同じ合言葉（空なら照合しない）
var SECRET = "";

// このコードのバージョン（デプロイ確認用）
var VERSION = "v7-summary-history";

// 見た目の設定
var COLOR_TITLE_BG = "#4472C4";   // 見出し帯（濃い青）
var COLOR_TITLE_FG = "#ffffff";   // 見出し文字（白）
var COLOR_HEADER_BG = "#d9e1f2";  // 表ヘッダー（薄い青）
var COLOR_LABEL_BG = "#eef2f9";   // 集計ラベル（うすいグレー青）
var COLOR_BORDER = "#9fb3d1";     // 枠線

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    if (SECRET && data.secret !== SECRET) {
      return ContentService.createTextOutput("forbidden");
    }
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    if (data.cast) {
      writeCastTab_(ss, data.cast);
    }
    if (data.summary) {
      writeSummaryTab_(ss, data.summary);
    }
    return ContentService.createTextOutput("ok " + VERSION);
  } catch (err) {
    return ContentService.createTextOutput("error: " + err);
  }
}

// ブラウザ確認用（GETでバージョン表示）
function doGet(e) {
  return ContentService.createTextOutput("kintai sheets webhook " + VERSION);
}

// 色帯の見出し行（セル結合はしない）
function bandRow_(sh, row, numCols, text) {
  sh.getRange(row, 1, 1, numCols).setBackground(COLOR_TITLE_BG);
  sh.getRange(row, 1).setValue(text)
    .setFontWeight("bold").setFontSize(12).setFontColor(COLOR_TITLE_FG);
}

// 文字幅（全角=2, 半角=1）に合わせて列幅を決める
function fitColumns_(sh, numCols) {
  var lastRow = sh.getLastRow();
  if (lastRow < 1) return;
  // getDisplayValues() は画面表示どおりの文字列を返す。
  // getValues() だと日付/時刻セルが Date オブジェクトになり、
  // "Thu Jul 09 2026 ..." のような長い文字列で幅を誤計算してしまう。
  var values = sh.getRange(1, 1, lastRow, numCols).getDisplayValues();
  for (var c = 0; c < numCols; c++) {
    var maxLen = 0;
    for (var r = 0; r < values.length; r++) {
      var v = values[r][c];
      if (v === null || v === "") continue;
      var s = String(v);
      var len = 0;
      for (var i = 0; i < s.length; i++) {
        len += (s.charCodeAt(i) > 255) ? 2 : 1;
      }
      if (len > maxLen) maxLen = len;
    }
    var width = Math.max(70, maxLen * 8 + 20);
    sh.setColumnWidth(c + 1, width);
  }
}

// 過去バージョンで作られたセル結合を解除してからクリア
function resetSheet_(sh) {
  try { sh.getRange(1, 1, sh.getMaxRows(), sh.getMaxColumns()).breakApart(); } catch (e) {}
  sh.clear();
  sh.clearFormats();
}

function writeCastTab_(ss, cast) {
  var sh = ss.getSheetByName(cast.tab) || ss.insertSheet(cast.tab);
  resetSheet_(sh);

  var header = cast.history_header || [];
  var width = Math.max(2, header.length);
  var row = 1;

  // 月間集計 見出し（結合なしの色帯）
  bandRow_(sh, row, width, "月間集計");
  row++;

  var summary = cast.summary || [];
  if (summary.length > 0) {
    var sumRange = sh.getRange(row, 1, summary.length, 2);
    sumRange.setValues(summary);
    sh.getRange(row, 1, summary.length, 1).setFontWeight("bold").setBackground(COLOR_LABEL_BG);
    sumRange.setBorder(true, true, true, true, true, true, COLOR_BORDER, SpreadsheetApp.BorderStyle.SOLID);
    row += summary.length;
  }

  row += 1; // 空行

  // 打刻履歴 見出し（結合なしの色帯）
  bandRow_(sh, row, width, "打刻履歴");
  row++;

  var tableTop = row;
  if (header.length > 0) {
    sh.getRange(row, 1, 1, header.length).setValues([header])
      .setFontWeight("bold").setBackground(COLOR_HEADER_BG)
      .setHorizontalAlignment("center");
    row++;
  }

  var history = cast.history || [];
  if (history.length > 0) {
    sh.getRange(row, 1, history.length, header.length).setValues(history);
    row += history.length;
  }

  var tableRows = row - tableTop;
  if (tableRows > 0 && header.length > 0) {
    sh.getRange(tableTop, 1, tableRows, header.length)
      .setBorder(true, true, true, true, true, true, COLOR_BORDER, SpreadsheetApp.BorderStyle.SOLID)
      .setHorizontalAlignment("center");
  }

  SpreadsheetApp.flush();
  fitColumns_(sh, width);
}

function writeSummaryTab_(ss, summary) {
  var sh = ss.getSheetByName(summary.tab) || ss.insertSheet(summary.tab);
  resetSheet_(sh);

  var rows = summary.rows || [];        // 全員集計（1行目が見出し）
  var header = summary.history_header || [];
  var history = summary.history || [];
  var sumCols = rows.length > 0 ? rows[0].length : 0;
  var maxCols = Math.max(sumCols, header.length, 2);
  var row = 1;

  // 月間集計 見出し帯
  bandRow_(sh, row, maxCols, "月間集計");
  row++;

  if (rows.length > 0) {
    var range = sh.getRange(row, 1, rows.length, sumCols);
    range.setValues(rows)
      .setBorder(true, true, true, true, true, true, COLOR_BORDER, SpreadsheetApp.BorderStyle.SOLID)
      .setHorizontalAlignment("center");
    sh.getRange(row, 1, 1, sumCols).setFontWeight("bold").setBackground(COLOR_HEADER_BG);
    row += rows.length;
  }

  row += 1; // 空行

  // 打刻履歴 見出し帯
  bandRow_(sh, row, maxCols, "打刻履歴");
  row++;

  var tableTop = row;
  if (header.length > 0) {
    sh.getRange(row, 1, 1, header.length).setValues([header])
      .setFontWeight("bold").setBackground(COLOR_HEADER_BG)
      .setHorizontalAlignment("center");
    row++;
  }
  if (history.length > 0) {
    sh.getRange(row, 1, history.length, header.length).setValues(history);
    row += history.length;
  }
  var tableRows = row - tableTop;
  if (tableRows > 0 && header.length > 0) {
    sh.getRange(tableTop, 1, tableRows, header.length)
      .setBorder(true, true, true, true, true, true, COLOR_BORDER, SpreadsheetApp.BorderStyle.SOLID)
      .setHorizontalAlignment("center");
  }

  SpreadsheetApp.flush();
  fitColumns_(sh, maxCols);
}
