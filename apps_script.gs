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

function writeCastTab_(ss, cast) {
  var sh = ss.getSheetByName(cast.tab) || ss.insertSheet(cast.tab);
  sh.clear();

  var row = 1;

  // 月間集計ブロック
  sh.getRange(row, 1).setValue("■ 月間集計").setFontWeight("bold");
  row++;
  if (cast.summary && cast.summary.length > 0) {
    sh.getRange(row, 1, cast.summary.length, 2).setValues(cast.summary);
    row += cast.summary.length;
  }

  row++; // 空行

  // 打刻履歴ブロック
  sh.getRange(row, 1).setValue("■ 打刻履歴").setFontWeight("bold");
  row++;

  var header = cast.history_header || [];
  if (header.length > 0) {
    sh.getRange(row, 1, 1, header.length).setValues([header]).setFontWeight("bold");
    row++;
  }

  var history = cast.history || [];
  if (history.length > 0) {
    sh.getRange(row, 1, history.length, header.length).setValues(history);
  }

  sh.autoResizeColumns(1, Math.max(2, header.length));
}

function writeSummaryTab_(ss, summary) {
  var sh = ss.getSheetByName(summary.tab) || ss.insertSheet(summary.tab);
  sh.clear();

  var rows = summary.rows || [];
  if (rows.length > 0) {
    sh.getRange(1, 1, rows.length, rows[0].length).setValues(rows);
    sh.getRange(1, 1, 1, rows[0].length).setFontWeight("bold");
    sh.autoResizeColumns(1, rows[0].length);
  }
}
