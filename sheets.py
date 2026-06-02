"""Google Sheets 連携モジュール

環境変数:
  GOOGLE_SHEETS_CREDENTIALS_JSON  サービスアカウント JSON キーの中身（文字列）
  GOOGLE_SPREADSHEET_ID           書き込み先スプレッドシートの ID
"""

import json
import logging
import os
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = ["営業日", "キャスト名", "出勤時刻", "退勤時刻", "種別", "ステータス", "遅刻理由"]

_client = None


def _get_client():
    """gspread クライアントをシングルトンで返す。"""
    global _client
    if _client is not None:
        return _client

    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "")
    if not creds_json:
        return None

    try:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        _client = gspread.authorize(creds)
        return _client
    except Exception:
        logger.exception("Google Sheets 認証に失敗しました")
        return None


def _get_spreadsheet():
    """スプレッドシートオブジェクトを返す。"""
    client = _get_client()
    if client is None:
        return None

    spreadsheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID", "")
    if not spreadsheet_id:
        logger.warning("GOOGLE_SPREADSHEET_ID が設定されていません")
        return None

    try:
        return client.open_by_key(spreadsheet_id)
    except Exception:
        logger.exception("スプレッドシートを開けませんでした")
        return None


def _get_or_create_monthly_sheet(spreadsheet, business_date):
    """月別シートを取得。なければ作成してヘッダーを書き込む。

    シート名: "2026年05月" のような形式
    """
    dt = datetime.strptime(business_date, "%Y-%m-%d")
    sheet_title = f"{dt.year}年{dt.month:02d}月"

    try:
        return spreadsheet.worksheet(sheet_title)
    except gspread.exceptions.WorksheetNotFound:
        pass

    try:
        worksheet = spreadsheet.add_worksheet(title=sheet_title, rows=500, cols=len(HEADERS))
        worksheet.append_row(HEADERS, value_input_option="USER_ENTERED")
        worksheet.format("1:1", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.95},
        })
        worksheet.set_basic_filter()
        return worksheet
    except Exception:
        logger.exception("月別シートの作成に失敗しました: %s", sheet_title)
        return None


def _punch_type_label(punch_type):
    if punch_type == "douhan":
        return "同伴"
    if punch_type == "absent":
        return "当欠"
    return "通常"


# ── 公開 API ──────────────────────────────────────────────


def sync_clock_in(cast_name, business_date, clock_in, punch_type, status, late_reason=""):
    """出勤打刻をスプレッドシートに追記する。"""
    try:
        spreadsheet = _get_spreadsheet()
        if spreadsheet is None:
            return

        ws = _get_or_create_monthly_sheet(spreadsheet, business_date)
        if ws is None:
            return

        row = [
            business_date,
            cast_name,
            clock_in or "",
            "",
            _punch_type_label(punch_type),
            status or "",
            late_reason or "",
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        logger.info("Sheets 出勤記録追加: %s %s", cast_name, business_date)
    except Exception:
        logger.exception("Sheets 出勤同期に失敗しました")


def sync_clock_out(cast_name, business_date, clock_in, clock_out):
    """退勤時刻をスプレッドシートの該当行に反映する。"""
    try:
        spreadsheet = _get_spreadsheet()
        if spreadsheet is None:
            return

        ws = _get_or_create_monthly_sheet(spreadsheet, business_date)
        if ws is None:
            return

        all_values = ws.get_all_values()
        target_row = None
        for i, row in enumerate(all_values):
            if i == 0:
                continue
            if (row[0] == business_date
                    and row[1] == cast_name
                    and row[2] == clock_in
                    and row[3] == ""):
                target_row = i + 1
                break

        if target_row:
            ws.update_cell(target_row, 4, clock_out)
            logger.info("Sheets 退勤記録更新: %s %s", cast_name, business_date)
        else:
            logger.warning("Sheets 退勤: 該当行が見つかりません (%s, %s, %s)", cast_name, business_date, clock_in)
    except Exception:
        logger.exception("Sheets 退勤同期に失敗しました")


def sync_absent(cast_name, business_date):
    """当欠レコードをスプレッドシートに追記する。"""
    try:
        spreadsheet = _get_spreadsheet()
        if spreadsheet is None:
            return

        ws = _get_or_create_monthly_sheet(spreadsheet, business_date)
        if ws is None:
            return

        row = [
            business_date,
            cast_name,
            "",
            "",
            "当欠",
            "当欠",
            "",
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        logger.info("Sheets 当欠記録追加: %s %s", cast_name, business_date)
    except Exception:
        logger.exception("Sheets 当欠同期に失敗しました")
