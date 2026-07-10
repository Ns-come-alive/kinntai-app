/**
 * 勤怠アプリ → スプレッドシート連携用 Google Apps Script
 *
 * 使い方:
 *  1. 連携したい Google スプレッドシートを開く
 *  2. 上部メニュー「拡張機能」→「Apps Script」を開く
 *  3. このファイルの中身をすべて貼り付けて保存
 *  4. 必要なら下の SECRET を設定（アプリ側の SHEETS_WEBHOOK_SECRET と同じ値）
 *  5. 「デプロイ」→「新しいデプロイ」→ 種類「ウェブアプリ」
 *       - 実行するユーザー: 自分
 *       - アクセスできるユーザー: 全員
 *  6. 表示された「ウェブアプリのURL（/exec で終わる）」をアプリ側の
 *     環境変数 SHEETS_WEBHOOK_URL に設定
 */

// アプリ側の SHEETS_WEBHOOK_SECRET と同じ合言葉を設定（空なら照合しない）
var SECRET = "";

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

    return ContentService.createTextOutput("ok");
  } catch (err) {
    return ContentService.createTextOutput("error: " + err);
  }
}

// 見た目の設定
var COLOR_TITLE_BG = "#4472C4";   // 見出し帯（濃い青）
var COLOR_TITLE_FG = "#ffffff";   // 見出し文字（白）
var COLOR_HEADER_BG = "#d9e1f2";  // 表ヘッダー（薄い青）
var COLOR_LABEL_BG = "#eef2f9";   // 集計ラベル（うすいグレー青）
var COLOR_BORDER = "#9fb3d1";     // 枠線

// 文字幅（全角=2, 半角=1）に合わせて列幅を決める
function fitColumns_(sh, numCols) {
  var lastRow = sh.getLastRow();
  if (lastRow < 1) return;
  var values = sh.getRange(1, 1, lastRow, numCols).getValues();
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
    var width = Math.max(70, maxLen * 8 + 24);
    sh.setColumnWidth(c + 1, width);
  }
}

function writeCastTab_(ss, cast) {
  var sh = ss.getSheetByName(cast.tab) || ss.insertSheet(cast.tab);
  sh.clear();
  sh.clearFormats();

  var row = 1;

  // 月間集計 見出し
  sh.getRange(row, 1, 1, 2).merge()
    .setValue("月間集計")
    .setFontWeight("bold").setFontSize(12)
    .setBackground(COLOR_TITLE_BG).setFontColor(COLOR_TITLE_FG)
    .setHorizontalAlignment("center");
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

  // 打刻履歴 見出し
  var header = cast.history_header || [];
  var width = Math.max(1, header.length);
  sh.getRange(row, 1, 1, width).merge()
    .setValue("打刻履歴")
    .setFontWeight("bold").setFontSize(12)
    .setBackground(COLOR_TITLE_BG).setFontColor(COLOR_TITLE_FG)
    .setHorizontalAlignment("center");
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

  fitColumns_(sh, width);
}

function writeSummaryTab_(ss, summary) {
  var sh = ss.getSheetByName(summary.tab) || ss.insertSheet(summary.tab);
  sh.clear();
  sh.clearFormats();

  var rows = summary.rows || [];
  if (rows.length > 0) {
    var w = rows[0].length;
    var range = sh.getRange(1, 1, rows.length, w);
    range.setValues(rows)
      .setBorder(true, true, true, true, true, true, COLOR_BORDER, SpreadsheetApp.BorderStyle.SOLID)
      .setHorizontalAlignment("center");
    sh.getRange(1, 1, 1, w).setFontWeight("bold")
      .setBackground(COLOR_TITLE_BG).setFontColor(COLOR_TITLE_FG);
    sh.setFrozenRows(1);
    fitColumns_(sh, w);
  }
}
