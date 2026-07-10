"""シフト表画像を Gemini で読み取るモジュール

環境変数:
  GEMINI_API_KEY  Google AI Studio で取得した API キー
  GEMINI_MODEL    使用するモデル名（任意。未設定なら候補を順に試す）
"""

import base64
import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# GEMINI_MODEL が未設定の場合、上から順に試す
FALLBACK_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.5-flash",
    "gemini-1.5-flash",
]


def is_configured():
    return bool(os.environ.get("GEMINI_API_KEY"))


def _build_prompt(cast_names, year):
    names_str = "、".join(cast_names) if cast_names else "(登録なし)"
    return (
        "この画像はキャストの勤務シフト表です。1枚に複数日が含まれることがあります。\n"
        "各キャストの、各日の出勤開始時刻を正確に読み取ってください。\n"
        f"登録されているキャスト名: {names_str}\n"
        f"対象の年は {year} 年です。日付に年が書かれていない場合はこの年として扱ってください。\n"
        "ルール:\n"
        "- 出勤する日のみ含めてください。休み・空欄・×印の日は含めないでください。\n"
        "- 時刻は24時間表記(HH:MM)にしてください。例: 21時→21:00、22時半→22:30。\n"
        "- キャスト名は、上の登録名の中で最も近いものに必ず合わせてください。\n"
        "- 日付は YYYY-MM-DD 形式にしてください。\n"
        "次のJSON形式のみで返してください（説明文は不要）:\n"
        '{"shifts": [{"date": "YYYY-MM-DD", "name": "キャスト名", "start": "HH:MM"}]}'
    )


def parse_shift_image(image_bytes, mime_type, cast_names, year):
    """画像からシフト情報を抽出して [{date, name, start}, ...] を返す。"""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY が設定されていません。RenderのEnvironmentに設定してください。")

    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/jpeg"

    prompt = _build_prompt(cast_names, year)
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }},
            ]
        }],
        "generationConfig": {"response_mime_type": "application/json"},
    }

    configured = os.environ.get("GEMINI_MODEL", "").strip()
    models = [configured] if configured else FALLBACK_MODELS

    last_error = None
    for model in models:
        url = ENDPOINT.format(model=model) + f"?key={api_key}"
        try:
            resp = requests.post(url, json=payload, timeout=90)
        except requests.RequestException as e:
            last_error = f"通信エラー: {e}"
            continue

        if resp.status_code == 404 or resp.status_code == 400:
            # モデル名が使えない等 → 次の候補へ
            last_error = f"モデル {model} 応答エラー ({resp.status_code}): {resp.text[:200]}"
            logger.warning(last_error)
            continue

        if resp.status_code != 200:
            last_error = f"APIエラー ({resp.status_code}): {resp.text[:200]}"
            logger.warning(last_error)
            continue

        try:
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            return parsed.get("shifts", [])
        except (KeyError, IndexError, ValueError) as e:
            last_error = f"応答の解析に失敗しました: {e}"
            logger.warning(last_error)
            continue

    raise RuntimeError(last_error or "画像の読み取りに失敗しました。")
