"""Google スプレッドシート連携（Google Apps Script Webhook 経由）

無料の Google アカウントでもファイル内にタブを自動作成して書き込めるよう、
サービスアカウントではなく Apps Script の Web アプリ（Webhook）に送信する方式。

環境変数:
  SHEETS_WEBHOOK_URL     Apps Script Web アプリの URL（/exec で終わる）
  SHEETS_WEBHOOK_SECRET  任意。設定すると照合用の合言葉として送信
"""

import logging
import os
import threading

import requests

logger = logging.getLogger(__name__)


def is_configured():
    return bool(os.environ.get("SHEETS_WEBHOOK_URL"))


def _send(url, payload):
    try:
        requests.post(url, json=payload, timeout=30)
    except requests.RequestException:
        logger.exception("スプレッドシートへの送信に失敗しました")


def push(payload):
    """ペイロードを Apps Script に送信する（打刻をブロックしないよう別スレッド）。"""
    url = os.environ.get("SHEETS_WEBHOOK_URL", "")
    if not url:
        return
    secret = os.environ.get("SHEETS_WEBHOOK_SECRET", "")
    if secret:
        payload = dict(payload, secret=secret)
    threading.Thread(target=_send, args=(url, payload), daemon=True).start()
