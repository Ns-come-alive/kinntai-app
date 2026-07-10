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
LIST_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

# 自動検出できなかった場合に上から順に試す候補（新しめの安定モデル）
FALLBACK_MODELS = [
    "gemini-flash-latest",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
]

# 一度成功したモデルを記憶して次回以降の検出を省く
_working_model = None


def is_configured():
    return bool(os.environ.get("GEMINI_API_KEY"))


def _discover_models(api_key):
    """API から generateContent に対応したモデル一覧を取得し、優先順に並べる。"""
    try:
        resp = requests.get(LIST_ENDPOINT, params={"key": api_key, "pageSize": 1000}, timeout=30)
        if resp.status_code != 200:
            return []
        models = resp.json().get("models", [])
    except (requests.RequestException, ValueError):
        return []

    names = []
    for m in models:
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        name = m.get("name", "").replace("models/", "")
        low = name.lower()
        # 画像生成・音声・埋め込み等の非対象モデルを除外
        if any(x in low for x in ["image", "tts", "audio", "embedding", "nano", "aqa", "imagen", "lyria"]):
            continue
        names.append(name)

    def rank(n):
        l = n.lower()
        if "flash-latest" in l:
            return 0
        if "flash" in l and "lite" not in l and "preview" not in l:
            return 1
        if "flash-lite" in l:
            return 2
        if "flash" in l:
            return 3
        if "pro" in l:
            return 4
        return 5

    names.sort(key=rank)
    return names


def _build_prompt(cast_names, year):
    names_str = "、".join(cast_names) if cast_names else "(登録なし)"
    return (
        "この画像はキャストの勤務シフト表（表形式）です。読み取り方は次の通りです。\n"
        "【表の構造】\n"
        "- 表の一番上の行に日付の数字(1〜31)、その下の行に曜日が並びます。\n"
        "- 左端の列にキャスト名が縦に並びます。\n"
        "- 表の上部のタイトルに対象期間（例: 2026/07/01 － 2026/07/15）が書かれています。年と月はここから判断してください。\n"
        "【セルの読み方】\n"
        "- 「22-L」「21-2」「23-L」のような記号は、ハイフンの前の数字が『出勤開始時刻(時)』です。"
        "例: 22-L→22:00、21-2→21:00、23-L→23:00。ハイフンの後ろ(Lや数字)は終了時刻なので無視してください。\n"
        "- セルの色は無視してください。\n"
        "- 空欄のセルはその日は休み（出勤なし）です。含めないでください。\n"
        "- 「◯」「○」「〇」など、開始時刻の数字が無い記号だけのセルは『自由出勤』です。これは含めないでください（出力しない）。\n"
        "【出力ルール】\n"
        f"- 登録されているキャスト名: {names_str}\n"
        "- キャスト名は上の登録名の中で最も近いものに必ず合わせてください。登録名に無いキャストは出力しないでください。\n"
        "- 出勤開始時刻が数字で読み取れる日のみ出力してください。\n"
        "- 日付は、上部タイトルの年月と、日付行の数字を組み合わせて YYYY-MM-DD 形式にしてください。\n"
        "- 時刻は24時間表記(HH:MM)にしてください。\n"
        f"（年月がどうしても判断できない場合のみ {year} 年として扱ってください）\n"
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

    global _working_model

    configured = os.environ.get("GEMINI_MODEL", "").strip()
    if configured:
        models = [configured]
    elif _working_model:
        models = [_working_model] + [m for m in FALLBACK_MODELS if m != _working_model]
    else:
        discovered = _discover_models(api_key)
        models = discovered + [m for m in FALLBACK_MODELS if m not in discovered]

    last_error = None
    for model in models:
        url = ENDPOINT.format(model=model) + f"?key={api_key}"
        try:
            resp = requests.post(url, json=payload, timeout=90)
        except requests.RequestException as e:
            last_error = f"通信エラー: {e}"
            continue

        if resp.status_code in (400, 404):
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
            _working_model = model
            return parsed.get("shifts", [])
        except (KeyError, IndexError, ValueError) as e:
            last_error = f"応答の解析に失敗しました: {e}"
            logger.warning(last_error)
            continue

    raise RuntimeError(last_error or "画像の読み取りに失敗しました。")
