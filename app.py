import os
import csv
import io
import math
import logging
import threading
from datetime import datetime, date, timedelta, timezone
from functools import wraps

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


def now_jst():
    return datetime.now(JST)

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, make_response
)

from database import get_db, new_db, init_db, ADMIN_USERNAME, ADMIN_PASSWORD
import sheets
from shift_import import (
    parse_shift_image, parse_driver_shift_image, is_configured as gemini_configured
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "kintai-app-dev-secret-key")

BUSINESS_DAY_START_HOUR = 20  # 20:00
BUSINESS_DAY_END_HOUR = 9    # 09:00
# 店舗名（複数店舗で同じコードを使い回すときに画面表示を切り替える）。
STORE_NAME = os.environ.get("STORE_NAME", "").strip()
# 本店の表示名。STORE_NAME 未設定なら既定で GIFT を使う。
HOME_STORE_LABEL = STORE_NAME or "GIFT"
SITE_ACCESS_CODE = os.environ.get("SITE_ACCESS_CODE", "Gift-0723")
# 実質ほぼ無期限（秒）。ブラウザにより Max-Age の上限あり（例: Chrome は約400日で打ち切り）
SITE_ACCESS_COOKIE_MAX_AGE = int(os.environ.get("SITE_ACCESS_COOKIE_MAX_AGE", str(60 * 60 * 24 * 365 * 20)))
# 当欠自動判定の外部トリガー用トークン（未設定なら /cron/check-absent は無効）
CRON_SECRET = os.environ.get("CRON_SECRET", "")


def get_business_date(dt=None):
    """営業日を取得。20:00～翌09:00 を1日とする。
    20:00以降 → その日の日付が営業日
    00:00～08:59 → 前日の日付が営業日
    09:00～19:59 → 営業時間外（当日を返すが通常は使わない）
    """
    if dt is None:
        dt = now_jst()
    if dt.hour < BUSINESS_DAY_END_HOUR:
        return (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        return dt.strftime("%Y-%m-%d")


def site_access_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.cookies.get("site_access") == SITE_ACCESS_CODE:
            return f(*args, **kwargs)
        return redirect(url_for("site_gate"))
    return decorated


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.cookies.get("site_access") != SITE_ACCESS_CODE:
            return redirect(url_for("site_gate"))
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.cookies.get("site_access") != SITE_ACCESS_CODE:
            return redirect(url_for("site_gate"))
        if not session.get("is_admin"):
            flash("管理者権限が必要です。", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.context_processor
def inject_store_name():
    """全テンプレートで店舗名を使えるようにする。

    STORE_NAME 未設定なら既定の GIFT を表示する。
    """
    return {"store_name": HOME_STORE_LABEL}


def _is_home_store(store):
    """本店（GIFT）所属かどうか。store が空、または本店名と一致なら本店扱い。"""
    s = (store or "").strip()
    return (not s) or (s == STORE_NAME) or (s == HOME_STORE_LABEL)


def _cast_display_name(name, store):
    """スプレッドシート等での表示名。ヘルプ店舗のキャストは『名前（店舗）』にする。"""
    if _is_home_store(store):
        return name
    return f"{name}（{(store or '').strip()}）"


def _group_casts_by_store(casts):
    """ログイン画面のタブ用に、キャストを店舗ごとに分ける。
    先頭が本店（GIFT）、以降はヘルプ店舗（BlueBell 等）を名前順で並べる。"""
    home_label = HOME_STORE_LABEL
    home = []
    helps = {}
    for c in casts:
        if _is_home_store(c["store"]):
            home.append(c)
        else:
            helps.setdefault(c["store"].strip(), []).append(c)
    groups = [{"store": "", "label": home_label, "casts": home}]
    for store_name in sorted(helps):
        groups.append({"store": store_name, "label": store_name, "casts": helps[store_name]})
    return groups


@app.before_request
def before_request():
    if request.path == "/healthz":
        return
    init_db()


@app.route("/healthz")
def healthz():
    return "ok", 200


# --------------- Site Gate ---------------

@app.route("/gate", methods=["GET", "POST"])
def site_gate():
    if request.cookies.get("site_access") == SITE_ACCESS_CODE:
        return redirect(url_for("login"))

    if request.method == "POST":
        code = request.form.get("access_code", "").strip()
        if code == SITE_ACCESS_CODE:
            resp = make_response(redirect(url_for("login")))
            secure = request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"
            resp.set_cookie(
                "site_access",
                SITE_ACCESS_CODE,
                max_age=SITE_ACCESS_COOKIE_MAX_AGE,
                httponly=True,
                samesite="Lax",
                secure=secure,
            )
            return resp
        else:
            flash("アクセスコードが正しくありません。", "error")
            return redirect(url_for("site_gate"))

    return render_template("gate.html")


# --------------- Auth ---------------

@app.route("/login", methods=["GET"])
@site_access_required
def login():
    db = get_db()
    casts = db.execute(
        "SELECT * FROM users WHERE is_admin = 0 ORDER BY id"
    ).fetchall()
    store_groups = _group_casts_by_store(casts)
    return render_template("login.html", store_groups=store_groups)


@app.route("/login/cast/<int:user_id>", methods=["POST"])
@site_access_required
def login_cast(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ? AND is_admin = 0", (user_id,)).fetchone()
    if not user:
        flash("ユーザーが見つかりません。", "error")
        return redirect(url_for("login"))

    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    session["is_admin"] = False
    return redirect(url_for("dashboard"))


@app.route("/login/admin", methods=["POST"])
@site_access_required
def login_admin():
    password = request.form.get("password", "")
    if password != ADMIN_PASSWORD:
        flash("パスワードが正しくありません。", "error")
        return redirect(url_for("login"))

    db = get_db()
    admin = db.execute("SELECT * FROM users WHERE name = ? AND is_admin = 1", (ADMIN_USERNAME,)).fetchone()
    if not admin:
        flash("管理者アカウントが見つかりません。", "error")
        return redirect(url_for("login"))

    session["user_id"] = admin["id"]
    session["user_name"] = "管理者"
    session["is_admin"] = True
    return redirect(url_for("admin_dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------- Cast Dashboard ---------------

@app.route("/")
@login_required
def dashboard():
    if session.get("is_admin"):
        return redirect(url_for("admin_dashboard"))

    db = get_db()
    user_id = session["user_id"]
    now = now_jst()
    business_date = get_business_date(now)

    records = db.execute(
        "SELECT * FROM attendance WHERE user_id = ? AND business_date = ? ORDER BY id",
        (user_id, business_date),
    ).fetchall()

    shift = db.execute(
        "SELECT * FROM shifts WHERE user_id = ? AND business_date = ?",
        (user_id, business_date),
    ).fetchone()

    currently_working = False
    if records:
        last = records[-1]
        if last["clock_out"] is None:
            currently_working = True

    # 送迎: 本日ドライバーが出勤していれば送迎ボタンを表示。1営業日1回まで。
    driver_available = db.execute(
        "SELECT 1 FROM driver_shifts WHERE business_date = ? LIMIT 1",
        (business_date,),
    ).fetchone() is not None
    pickup_done = db.execute(
        "SELECT 1 FROM pickups WHERE user_id = ? AND business_date = ? LIMIT 1",
        (user_id, business_date),
    ).fetchone() is not None

    return render_template(
        "dashboard.html",
        records=records,
        shift=shift,
        business_date=business_date,
        currently_working=currently_working,
        driver_available=driver_available,
        pickup_done=pickup_done,
    )


# --------------- Clock Actions ---------------

def _determine_status(db, user_id, business_date, clock_in_time, punch_type="normal"):
    """シフトに基づいてステータスを判定。
    同伴出勤の場合はシフト開始から1時間以内なら遅刻にしない。
    """
    shift = db.execute(
        "SELECT * FROM shifts WHERE user_id = ? AND business_date = ?",
        (user_id, business_date),
    ).fetchone()

    if not shift:
        return "シフト未登録"

    shift_start = shift["shift_start"]
    try:
        shift_dt = datetime.strptime(shift_start, "%H:%M")
        clock_dt = datetime.strptime(clock_in_time, "%H:%M:%S")
        shift_dt = shift_dt.replace(second=0)
        clock_compare = clock_dt.replace(year=2000, month=1, day=1)
        shift_compare = shift_dt.replace(year=2000, month=1, day=1)

        clock_h = clock_compare.hour
        shift_h = shift_compare.hour
        if clock_h < BUSINESS_DAY_END_HOUR:
            clock_h += 24
        if shift_h < BUSINESS_DAY_END_HOUR:
            shift_h += 24

        clock_total = clock_h * 60 + clock_compare.minute
        shift_total = shift_h * 60 + shift_compare.minute

        diff = clock_total - shift_total

        if diff <= 0:
            return "出勤"

        if punch_type == "douhan" and diff <= 60:
            return "出勤"

        return "遅刻"
    except (ValueError, TypeError):
        return "出勤"


def _calc_late_hours(shift_start, clock_in_time, punch_type="normal"):
    """遅刻時間を30分刻みで算出。1分でも過ぎたら0.5時間。"""
    try:
        shift_dt = datetime.strptime(shift_start, "%H:%M")
        clock_dt = datetime.strptime(clock_in_time, "%H:%M:%S")

        shift_h = shift_dt.hour
        clock_h = clock_dt.hour
        if clock_h < BUSINESS_DAY_END_HOUR:
            clock_h += 24
        if shift_h < BUSINESS_DAY_END_HOUR:
            shift_h += 24

        clock_total = clock_h * 60 + clock_dt.minute
        shift_total = shift_h * 60 + shift_dt.minute

        if punch_type == "douhan":
            shift_total += 60

        diff_min = clock_total - shift_total
        if diff_min <= 0:
            return 0.0

        return math.ceil(diff_min / 30) * 0.5
    except (ValueError, TypeError):
        return 0.0


def _work_minutes(clock_in, clock_out):
    """出勤〜退勤の勤務分数を返す（日跨ぎ対応）。計算不可なら None。"""
    if not clock_in or not clock_out:
        return None
    try:
        ci = datetime.strptime(clock_in, "%H:%M:%S")
        co = datetime.strptime(clock_out, "%H:%M:%S")
        if co < ci:
            co += timedelta(days=1)
        return int((co - ci).total_seconds() // 60)
    except (ValueError, TypeError):
        return None


def _calc_work_hours(clock_in, clock_out):
    """勤務時間を15分刻み（切り捨て）で算出。
    例: 1時間10分 → 1.0時間、1時間20分 → 1.25時間。"""
    minutes = _work_minutes(clock_in, clock_out)
    if minutes is None:
        return None
    return (minutes // 15) * 0.25


LATE_REASONS = ["交通機関の遅延", "体調不良", "その他"]


@app.route("/clock-in", methods=["POST"])
@login_required
def clock_in():
    punch_type = request.form.get("punch_type", "normal")
    db = get_db()
    user_id = session["user_id"]
    now = now_jst()
    business_date = get_business_date(now)
    clock_time = now.strftime("%H:%M:%S")

    active = db.execute(
        "SELECT id FROM attendance WHERE user_id = ? AND business_date = ? AND clock_out IS NULL",
        (user_id, business_date),
    ).fetchone()

    if active:
        flash("現在勤務中です。先に退勤してください。", "warning")
        return redirect(url_for("dashboard"))

    status = _determine_status(db, user_id, business_date, clock_time, punch_type)
    is_late = (status == "遅刻")

    if punch_type == "douhan":
        status = "同伴" if not is_late else "同伴・遅刻"

    if is_late:
        session["pending_clock"] = {
            "business_date": business_date,
            "clock_time": clock_time,
            "punch_type": punch_type,
            "status": status,
        }
        return redirect(url_for("late_reason"))

    db.execute(
        "INSERT INTO attendance (user_id, business_date, clock_in, punch_type, status) VALUES (?, ?, ?, ?, ?)",
        (user_id, business_date, clock_time, punch_type, status),
    )
    db.commit()

    _sync_sheets(db, user_id, business_date)

    label = "同伴出勤" if punch_type == "douhan" else "出勤"
    flash(f"{label}しました。", "success")
    return redirect(url_for("dashboard"))


@app.route("/late-reason", methods=["GET", "POST"])
@login_required
def late_reason():
    pending = session.get("pending_clock")
    if not pending:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        reason = request.form.get("late_reason", "")
        db = get_db()
        user_id = session["user_id"]

        db.execute(
            "INSERT INTO attendance (user_id, business_date, clock_in, punch_type, status, late_reason) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, pending["business_date"], pending["clock_time"],
             pending["punch_type"], pending["status"], reason),
        )
        db.commit()

        _sync_sheets(db, user_id, pending["business_date"])
        session.pop("pending_clock", None)

        label = "同伴出勤" if pending["punch_type"] == "douhan" else "出勤"
        flash(f"{label}しました（遅刻: {reason}）。", "warning")
        return redirect(url_for("dashboard"))

    return render_template("late_reason.html", reasons=LATE_REASONS, pending=pending)


@app.route("/clock-out", methods=["POST"])
@login_required
def clock_out():
    db = get_db()
    user_id = session["user_id"]
    now = now_jst()
    business_date = get_business_date(now)
    clock_time = now.strftime("%H:%M:%S")

    active = db.execute(
        "SELECT * FROM attendance WHERE user_id = ? AND business_date = ? AND clock_out IS NULL ORDER BY id DESC LIMIT 1",
        (user_id, business_date),
    ).fetchone()

    if not active:
        flash("出勤記録がありません。", "error")
        return redirect(url_for("dashboard"))

    db.execute(
        "UPDATE attendance SET clock_out = ? WHERE id = ?",
        (clock_time, active["id"]),
    )
    db.commit()

    _sync_sheets(db, user_id, business_date)

    flash("退勤しました。お疲れ様でした。", "success")
    return redirect(url_for("dashboard"))


# --------------- 送迎 ---------------

@app.route("/pickup", methods=["POST"])
@login_required
def pickup_toggle():
    """送迎の記録／取り消し（1営業日1回まで）。"""
    db = get_db()
    user_id = session["user_id"]
    now = now_jst()
    business_date = get_business_date(now)

    driver_available = db.execute(
        "SELECT 1 FROM driver_shifts WHERE business_date = ? LIMIT 1",
        (business_date,),
    ).fetchone()
    if not driver_available:
        flash("本日は送迎ドライバーが出勤していないため、送迎を記録できません。", "warning")
        return redirect(url_for("dashboard"))

    existing = db.execute(
        "SELECT id FROM pickups WHERE user_id = ? AND business_date = ?",
        (user_id, business_date),
    ).fetchone()

    if existing:
        db.execute("DELETE FROM pickups WHERE id = ?", (existing["id"],))
        db.commit()
        flash("送迎を取り消しました。", "success")
    else:
        db.execute(
            "INSERT INTO pickups (user_id, business_date, clock_time) VALUES (?, ?, ?)",
            (user_id, business_date, now.strftime("%H:%M:%S")),
        )
        db.commit()
        flash("送迎を記録しました。", "success")

    _sync_sheets(db, user_id, business_date)
    return redirect(url_for("dashboard"))


# --------------- Cast History ---------------

@app.route("/history")
@login_required
def history():
    db = get_db()
    user_id = session["user_id"]

    year = request.args.get("year", now_jst().date().year, type=int)
    month = request.args.get("month", now_jst().date().month, type=int)

    start_date = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end_date = f"{year + 1:04d}-01-01"
    else:
        end_date = f"{year:04d}-{month + 1:02d}-01"

    records = db.execute(
        """SELECT * FROM attendance
           WHERE user_id = ? AND business_date >= ? AND business_date < ?
           ORDER BY business_date, id""",
        (user_id, start_date, end_date),
    ).fetchall()

    summary = _calc_cast_summary(db, user_id, start_date, end_date)

    return render_template(
        "history.html",
        records=records,
        year=year,
        month=month,
        total_days=summary["total_days"],
        total_work_hours=summary["total_work_hours"],
        total_late_hours=summary["total_late_hours"],
        absent_days=summary["absent_days"],
        pickup_count=summary["pickup_count"],
        pickup_amount=summary["pickup_amount"],
    )


# --------------- Admin ---------------

@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    now = now_jst()
    business_date = get_business_date(now)

    casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()

    cast_data = []
    for c in casts:
        records = db.execute(
            "SELECT * FROM attendance WHERE user_id = ? AND business_date = ? ORDER BY id",
            (c["id"], business_date),
        ).fetchall()
        shift = db.execute(
            "SELECT * FROM shifts WHERE user_id = ? AND business_date = ?",
            (c["id"], business_date),
        ).fetchone()
        cast_data.append({
            "cast": c,
            "records": records,
            "shift": shift,
        })

    return render_template(
        "admin_dashboard.html",
        cast_data=cast_data,
        business_date=business_date,
    )


@app.route("/admin/shifts", methods=["GET", "POST"])
@admin_required
def admin_shifts():
    db = get_db()

    if request.method == "POST":
        business_date = request.form.get("business_date")
        if not business_date:
            flash("営業日を指定してください。", "error")
            return redirect(url_for("admin_shifts"))

        casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()
        for c in casts:
            shift_time = request.form.get(f"shift_{c['id']}", "").strip()
            if shift_time:
                db.execute(
                    """INSERT INTO shifts (user_id, business_date, shift_start)
                       VALUES (?, ?, ?)
                       ON CONFLICT(user_id, business_date)
                       DO UPDATE SET shift_start = excluded.shift_start""",
                    (c["id"], business_date, shift_time),
                )
            else:
                db.execute(
                    "DELETE FROM shifts WHERE user_id = ? AND business_date = ?",
                    (c["id"], business_date),
                )
        db.commit()
        flash("シフトを保存しました。", "success")
        return redirect(url_for("admin_shifts", date=business_date))

    view_date = request.args.get("date", get_business_date())
    casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()

    try:
        view_dt = datetime.strptime(view_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        view_dt = datetime.strptime(get_business_date(), "%Y-%m-%d")
        view_date = view_dt.strftime("%Y-%m-%d")
    prev_date = (view_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (view_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    shifts = {}
    for c in casts:
        s = db.execute(
            "SELECT * FROM shifts WHERE user_id = ? AND business_date = ?",
            (c["id"], view_date),
        ).fetchone()
        shifts[c["id"]] = s["shift_start"] if s else ""

    return render_template(
        "admin_shifts.html",
        casts=casts,
        shifts=shifts,
        view_date=view_date,
        prev_date=prev_date,
        next_date=next_date,
        gemini_ready=gemini_configured(),
    )


@app.route("/admin/shifts/import", methods=["POST"])
@admin_required
def admin_shifts_import():
    file = request.files.get("shift_image")
    if not file or not file.filename:
        flash("画像またはPDFを選択してください。", "error")
        return redirect(url_for("admin_shifts"))

    if not gemini_configured():
        flash("読み取り機能が未設定です（GEMINI_API_KEY）。管理者に連絡してください。", "error")
        return redirect(url_for("admin_shifts"))

    image_bytes = file.read()
    # PDF はブラウザが MIME を送らないことがあるため拡張子でも判定する。
    mime = (file.mimetype or "").lower()
    fname = (file.filename or "").lower()
    if fname.endswith(".pdf") or mime == "application/pdf":
        mime = "application/pdf"
    elif not mime.startswith("image/"):
        mime = "image/jpeg"

    db = get_db()
    casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()
    cast_names = [c["name"] for c in casts]
    name_to_id = {c["name"]: c["id"] for c in casts}
    year = now_jst().year

    try:
        shifts = parse_shift_image(image_bytes, mime, cast_names, year)
    except Exception as e:
        flash(f"読み取りに失敗しました: {e}", "error")
        return redirect(url_for("admin_shifts"))

    parsed_rows = []
    for s in shifts:
        name = (s.get("name") or "").strip()
        date = (s.get("date") or "").strip()
        start = (s.get("start") or "").strip()
        if name in name_to_id and date and start:
            parsed_rows.append({
                "user_id": name_to_id[name],
                "name": name,
                "date": date,
                "start": start,
            })

    if not parsed_rows:
        flash("シフトを読み取れませんでした。画像・PDFが鮮明か、キャスト名が登録名と一致しているか確認してください。", "warning")
        return redirect(url_for("admin_shifts"))

    parsed_rows.sort(key=lambda r: (r["date"], r["user_id"]))
    session["import_shifts"] = parsed_rows
    return redirect(url_for("admin_shifts_import_preview"))


@app.route("/admin/shifts/import/preview")
@admin_required
def admin_shifts_import_preview():
    rows = session.get("import_shifts")
    if not rows:
        return redirect(url_for("admin_shifts"))
    db = get_db()
    casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()
    return render_template("admin_shifts_import.html", rows=rows, casts=casts)


@app.route("/admin/shifts/import/save", methods=["POST"])
@admin_required
def admin_shifts_import_save():
    db = get_db()
    count = request.form.get("row_count", 0, type=int)
    saved = 0
    for i in range(count):
        if not request.form.get(f"include_{i}"):
            continue
        user_id = request.form.get(f"user_id_{i}", type=int)
        s_date = request.form.get(f"date_{i}", "").strip()
        start = request.form.get(f"start_{i}", "").strip()
        if not user_id or not s_date or not start:
            continue
        db.execute(
            """INSERT INTO shifts (user_id, business_date, shift_start)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, business_date)
               DO UPDATE SET shift_start = excluded.shift_start""",
            (user_id, s_date, start),
        )
        saved += 1
    db.commit()
    session.pop("import_shifts", None)
    flash(f"{saved}件のシフトを登録しました。", "success")
    return redirect(url_for("admin_shifts"))


# --------------- 送迎ドライバーのシフト ---------------

@app.route("/admin/drivers")
@admin_required
def admin_drivers():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM driver_shifts ORDER BY business_date DESC, driver_name"
    ).fetchall()
    return render_template(
        "admin_drivers.html",
        rows=rows,
        today=get_business_date(),
        gemini_ready=gemini_configured(),
    )


@app.route("/admin/drivers/add", methods=["POST"])
@admin_required
def admin_driver_add():
    business_date = request.form.get("business_date", "").strip()
    driver_name = request.form.get("driver_name", "").strip()
    if not business_date:
        flash("日付を指定してください。", "error")
        return redirect(url_for("admin_drivers"))

    db = get_db()
    db.execute(
        """INSERT INTO driver_shifts (business_date, driver_name)
           VALUES (?, ?)
           ON CONFLICT(business_date, driver_name) DO NOTHING""",
        (business_date, driver_name),
    )
    db.commit()
    flash(f"{business_date} の送迎を登録しました。", "success")
    return redirect(url_for("admin_drivers"))


@app.route("/admin/drivers/delete/<int:driver_shift_id>", methods=["POST"])
@admin_required
def admin_driver_delete(driver_shift_id):
    db = get_db()
    db.execute("DELETE FROM driver_shifts WHERE id = ?", (driver_shift_id,))
    db.commit()
    flash("送迎の登録を削除しました。", "success")
    return redirect(url_for("admin_drivers"))


@app.route("/admin/drivers/import", methods=["POST"])
@admin_required
def admin_drivers_import():
    file = request.files.get("shift_image")
    if not file or not file.filename:
        flash("画像またはPDFを選択してください。", "error")
        return redirect(url_for("admin_drivers"))

    if not gemini_configured():
        flash("読み取り機能が未設定です（GEMINI_API_KEY）。管理者に連絡してください。", "error")
        return redirect(url_for("admin_drivers"))

    file_bytes = file.read()
    mime = (file.mimetype or "").lower()
    fname = (file.filename or "").lower()
    if fname.endswith(".pdf") or mime == "application/pdf":
        mime = "application/pdf"
    elif not mime.startswith("image/"):
        mime = "image/jpeg"
    year = now_jst().year

    try:
        shifts = parse_driver_shift_image(file_bytes, mime, year)
    except Exception as e:
        flash(f"画像の読み取りに失敗しました: {e}", "error")
        return redirect(url_for("admin_drivers"))

    parsed_rows = []
    seen = set()
    for s in shifts:
        s_date = (s.get("date") or "").strip()
        name = (s.get("name") or "").strip()
        if not s_date:
            continue
        key = (s_date, name)
        if key in seen:
            continue
        seen.add(key)
        parsed_rows.append({"date": s_date, "name": name})

    if not parsed_rows:
        flash("送迎シフトを読み取れませんでした。画像が鮮明か確認してください。", "warning")
        return redirect(url_for("admin_drivers"))

    parsed_rows.sort(key=lambda r: (r["date"], r["name"]))
    session["import_driver_shifts"] = parsed_rows
    return redirect(url_for("admin_drivers_import_preview"))


@app.route("/admin/drivers/import/preview")
@admin_required
def admin_drivers_import_preview():
    rows = session.get("import_driver_shifts")
    if not rows:
        return redirect(url_for("admin_drivers"))
    return render_template("admin_drivers_import.html", rows=rows)


@app.route("/admin/drivers/import/save", methods=["POST"])
@admin_required
def admin_drivers_import_save():
    db = get_db()
    count = request.form.get("row_count", 0, type=int)
    saved = 0
    for i in range(count):
        if not request.form.get(f"include_{i}"):
            continue
        s_date = request.form.get(f"date_{i}", "").strip()
        name = request.form.get(f"name_{i}", "").strip()
        if not s_date:
            continue
        db.execute(
            """INSERT INTO driver_shifts (business_date, driver_name)
               VALUES (?, ?)
               ON CONFLICT(business_date, driver_name) DO NOTHING""",
            (s_date, name),
        )
        saved += 1
    db.commit()
    session.pop("import_driver_shifts", None)
    flash(f"{saved}件の送迎シフトを登録しました。", "success")
    return redirect(url_for("admin_drivers"))


def _calc_cast_summary(db, user_id, start_date, end_date):
    """キャスト1人分の月間集計を計算"""
    records = db.execute(
        """SELECT a.* FROM attendance a
           WHERE a.user_id = ? AND a.business_date >= ? AND a.business_date < ?
           ORDER BY a.business_date, a.id""",
        (user_id, start_date, end_date),
    ).fetchall()

    working_dates = set()
    absent_dates = set()
    total_late_hours = 0.0
    total_work_hours = 0.0

    for r in records:
        if r["punch_type"] == "absent":
            absent_dates.add(r["business_date"])
            continue

        working_dates.add(r["business_date"])

        if "遅刻" in (r["status"] or "") and r["clock_in"]:
            shift = db.execute(
                "SELECT shift_start FROM shifts WHERE user_id = ? AND business_date = ?",
                (user_id, r["business_date"]),
            ).fetchone()
            if shift:
                total_late_hours += _calc_late_hours(shift["shift_start"], r["clock_in"], r["punch_type"])

        wh = _calc_work_hours(r["clock_in"], r["clock_out"])
        if wh is not None:
            total_work_hours += wh

    pickup_count = db.execute(
        """SELECT COUNT(*) as cnt FROM pickups
           WHERE user_id = ? AND business_date >= ? AND business_date < ?""",
        (user_id, start_date, end_date),
    ).fetchone()["cnt"]

    fee_row = db.execute("SELECT pickup_fee FROM users WHERE id = ?", (user_id,)).fetchone()
    pickup_fee = fee_row["pickup_fee"] if fee_row and fee_row["pickup_fee"] is not None else 1000

    return {
        "total_days": len(working_dates),
        "total_work_hours": round(total_work_hours, 2),
        "total_late_hours": total_late_hours,
        "absent_days": len(absent_dates),
        "pickup_count": pickup_count,
        "pickup_fee": pickup_fee,
        "pickup_amount": pickup_count * pickup_fee,
    }


# --------------- スプレッドシート同期 ---------------

def _month_label(business_date):
    """'2026-07-05' → '2026年07月'"""
    return f"{business_date[:4]}年{business_date[5:7]}月"


def _month_range(business_date):
    """business_date が属する月の [開始日, 翌月開始日) を返す。"""
    year = int(business_date[:4])
    month = int(business_date[5:7])
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"
    return start, end


def _punch_type_label(punch_type):
    if punch_type == "douhan":
        return "同伴"
    if punch_type == "absent":
        return "当欠"
    return "通常"


def _sync_sheets(db, user_id, business_date):
    """スプレッドシート同期。打刻をブロックしないよう、集計の組み立てと送信を
    まるごと別スレッドに逃がす（引数 db / user_id は互換のため残すが未使用）。"""
    if not sheets.is_configured():
        return
    threading.Thread(
        target=_build_and_push_sheets, args=(business_date,), daemon=True
    ).start()


def _build_and_push_sheets(business_date):
    """全員分の月間集計タブ（集計＋打刻履歴）を組み立てて送信する。
    バックグラウンドスレッドで動くため、リクエストとは別のDB接続を開く。"""
    db = None
    try:
        db = new_db()
        start_date, end_date = _month_range(business_date)
        label = _month_label(business_date)

        casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()
        summary_rows = [[
            "キャスト", "出勤日数", "総稼働時間(h)", "遅刻時間(h)", "欠勤日数",
            "送迎回数", "送迎料金(円)",
        ]]
        history_rows = []
        for c in casts:
            display_name = _cast_display_name(c["name"], c["store"])
            s = _calc_cast_summary(db, c["id"], start_date, end_date)
            summary_rows.append([
                display_name, s["total_days"], s["total_work_hours"],
                s["total_late_hours"], s["absent_days"],
                s["pickup_count"], s["pickup_amount"],
            ])

            records = db.execute(
                """SELECT * FROM attendance
                   WHERE user_id = ? AND business_date >= ? AND business_date < ?
                   ORDER BY business_date, id""",
                (c["id"], start_date, end_date),
            ).fetchall()
            for r in records:
                wh = _calc_work_hours(r["clock_in"], r["clock_out"])
                history_rows.append([
                    r["business_date"],
                    display_name,
                    r["clock_in"] or "",
                    r["clock_out"] or "",
                    wh if wh is not None else "",
                    _punch_type_label(r["punch_type"]),
                    r["status"] or "",
                    r["late_reason"] or "",
                ])

        # 営業日→キャスト名の順に並べ替え
        history_rows.sort(key=lambda row: (row[0], row[1]))

        payload = {
            "month_label": label,
            "summary": {
                "tab": f"月間集計 {label}",
                "rows": summary_rows,
                "history_header": [
                    "営業日", "キャスト", "出勤", "退勤",
                    "稼働(h)", "種別", "ステータス", "遅刻理由",
                ],
                "history": history_rows,
            },
        }
        sheets.push(payload)
    except Exception:
        logger.exception("スプレッドシート同期の準備に失敗しました")
    finally:
        if db is not None:
            db.close()


@app.route("/admin/history")
@admin_required
def admin_history():
    db = get_db()

    year = request.args.get("year", now_jst().date().year, type=int)
    month = request.args.get("month", now_jst().date().month, type=int)
    cast_id = request.args.get("cast_id", 0, type=int)

    start_date = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end_date = f"{year + 1:04d}-01-01"
    else:
        end_date = f"{year:04d}-{month + 1:02d}-01"

    casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()

    cast_summaries = {}
    for c in casts:
        cast_summaries[c["id"]] = _calc_cast_summary(db, c["id"], start_date, end_date)

    records = []
    if cast_id:
        records = db.execute(
            """SELECT a.*, u.name as cast_name FROM attendance a
               JOIN users u ON a.user_id = u.id
               WHERE a.user_id = ? AND a.business_date >= ? AND a.business_date < ?
               ORDER BY a.business_date, a.id""",
            (cast_id, start_date, end_date),
        ).fetchall()
    else:
        records = db.execute(
            """SELECT a.*, u.name as cast_name FROM attendance a
               JOIN users u ON a.user_id = u.id
               WHERE u.is_admin = 0 AND a.business_date >= ? AND a.business_date < ?
               ORDER BY a.business_date, u.name, a.id""",
            (start_date, end_date),
        ).fetchall()

    return render_template(
        "admin_history.html",
        casts=casts,
        store_groups=_group_casts_by_store(casts),
        records=records,
        year=year,
        month=month,
        cast_id=cast_id,
        cast_summaries=cast_summaries,
        sheets_enabled=sheets.is_configured(),
        is_home_store=_is_home_store,
    )


def _run_absent_check(db, business_date):
    """シフト登録済みで打刻が1件もないキャストを当欠として記録し、件数を返す。
    既に打刻や当欠がある場合は対象外（何度実行しても二重登録されない）。"""
    casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()
    affected = []
    for c in casts:
        shift = db.execute(
            "SELECT * FROM shifts WHERE user_id = ? AND business_date = ?",
            (c["id"], business_date),
        ).fetchone()
        if not shift:
            continue

        record = db.execute(
            "SELECT id FROM attendance WHERE user_id = ? AND business_date = ?",
            (c["id"], business_date),
        ).fetchone()
        if not record:
            db.execute(
                "INSERT INTO attendance (user_id, business_date, clock_in, punch_type, status) VALUES (?, ?, '', 'absent', '当欠')",
                (c["id"], business_date),
            )
            affected.append(c["id"])

    db.commit()

    # _sync_sheets は全キャストをまとめて再集計するので、1回だけ呼べば十分。
    if affected:
        _sync_sheets(db, affected[0], business_date)

    return len(affected)


@app.route("/admin/check-absent", methods=["POST"])
@admin_required
def check_absent():
    """営業日終了後に当欠チェックを手動実行"""
    db = get_db()
    business_date = request.form.get("business_date", get_business_date())
    count = _run_absent_check(db, business_date)
    flash(f"当欠チェック完了。{count}件の当欠を記録しました。", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/cron/check-absent", methods=["GET", "POST"])
def cron_check_absent():
    """外部スケジューラ（GitHub Actions 等）から毎日叩く当欠自動判定。
    CRON_SECRET と一致するトークンが必要。対象は実行時点の営業日。"""
    token = request.args.get("token", "") or request.headers.get("X-Cron-Token", "")
    if not CRON_SECRET or token != CRON_SECRET:
        return "forbidden", 403

    db = get_db()
    # 毎朝9:00頃の実行を想定。直近で終了した「前夜の営業日」を対象にする。
    # get_business_date() は 09:00 ちょうど以降だと当日を返してしまい、
    # かつ外部スケジューラは遅延しがちなので、明示的に前日（前夜のセッション）を対象にする。
    business_date = (now_jst() - timedelta(days=1)).strftime("%Y-%m-%d")
    count = _run_absent_check(db, business_date)
    logger.info("cron当欠判定: %s に %d 件を記録", business_date, count)
    return jsonify({"ok": True, "business_date": business_date, "absent_added": count}), 200


# --------------- 勤怠の編集（管理者） ---------------

@app.route("/admin/edit")
@admin_required
def admin_edit():
    """キャスト×営業日ごとの勤怠編集画面。"""
    db = get_db()
    casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()

    cast_id = request.args.get("cast_id", type=int)
    if not cast_id and casts:
        cast_id = casts[0]["id"]

    date = request.args.get("date", "").strip()
    try:
        view_dt = datetime.strptime(date, "%Y-%m-%d")
    except (ValueError, TypeError):
        view_dt = datetime.strptime(get_business_date(), "%Y-%m-%d")
    date = view_dt.strftime("%Y-%m-%d")
    prev_date = (view_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (view_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    cast = None
    records = []
    is_absent = False
    pickup_on = False
    shift = None
    driver_available = False
    if cast_id:
        cast = db.execute(
            "SELECT * FROM users WHERE id = ? AND is_admin = 0", (cast_id,)
        ).fetchone()
    if cast:
        all_recs = db.execute(
            "SELECT * FROM attendance WHERE user_id = ? AND business_date = ? ORDER BY id",
            (cast_id, date),
        ).fetchall()
        for r in all_recs:
            if r["punch_type"] == "absent":
                is_absent = True
            else:
                records.append(r)
        pickup_on = db.execute(
            "SELECT 1 FROM pickups WHERE user_id = ? AND business_date = ? LIMIT 1",
            (cast_id, date),
        ).fetchone() is not None
        shift = db.execute(
            "SELECT * FROM shifts WHERE user_id = ? AND business_date = ?",
            (cast_id, date),
        ).fetchone()
        driver_available = db.execute(
            "SELECT 1 FROM driver_shifts WHERE business_date = ? LIMIT 1", (date,)
        ).fetchone() is not None

    return render_template(
        "admin_edit.html",
        casts=casts,
        cast=cast,
        cast_id=cast_id,
        date=date,
        prev_date=prev_date,
        next_date=next_date,
        records=records,
        is_absent=is_absent,
        pickup_on=pickup_on,
        shift=shift,
        driver_available=driver_available,
        reasons=LATE_REASONS,
    )


@app.route("/admin/edit/save", methods=["POST"])
@admin_required
def admin_edit_save():
    db = get_db()
    cast_id = request.form.get("cast_id", type=int)
    date = request.form.get("date", "").strip()

    cast = db.execute(
        "SELECT * FROM users WHERE id = ? AND is_admin = 0", (cast_id,)
    ).fetchone() if cast_id else None
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except (ValueError, TypeError):
        cast = None
    if not cast:
        flash("編集対象が正しくありません。", "error")
        return redirect(url_for("admin_edit"))

    make_absent = bool(request.form.get("make_absent"))

    if make_absent:
        # 当欠にする: その日の打刻をすべて置き換えて当欠1件だけにする。
        db.execute(
            "DELETE FROM attendance WHERE user_id = ? AND business_date = ?",
            (cast_id, date),
        )
        db.execute(
            "INSERT INTO attendance (user_id, business_date, clock_in, punch_type, status) VALUES (?, ?, '', 'absent', '当欠')",
            (cast_id, date),
        )
    else:
        # 当欠の解除（当欠チェックで付いた記録を消す）
        db.execute(
            "DELETE FROM attendance WHERE user_id = ? AND business_date = ? AND punch_type = 'absent'",
            (cast_id, date),
        )
        count = request.form.get("row_count", 0, type=int)
        for i in range(count):
            rec_id = request.form.get(f"rec_id_{i}", type=int)
            if request.form.get(f"delete_{i}"):
                if rec_id:
                    db.execute(
                        "DELETE FROM attendance WHERE id = ? AND user_id = ?",
                        (rec_id, cast_id),
                    )
                continue

            cin = request.form.get(f"in_{i}", "").strip()
            if not cin:
                # 出勤時刻が空の行は無視（新規の空行など）
                if rec_id:
                    db.execute(
                        "DELETE FROM attendance WHERE id = ? AND user_id = ?",
                        (rec_id, cast_id),
                    )
                continue

            cout = request.form.get(f"out_{i}", "").strip()
            ptype = request.form.get(f"type_{i}", "normal")
            if ptype not in ("normal", "douhan"):
                ptype = "normal"
            reason = request.form.get(f"reason_{i}", "").strip()

            clock_in = cin + ":00"
            clock_out = (cout + ":00") if cout else None

            status = _determine_status(db, cast_id, date, clock_in, ptype)
            is_late = (status == "遅刻")
            if ptype == "douhan":
                status = "同伴" if not is_late else "同伴・遅刻"
            if not is_late:
                reason = ""

            if rec_id:
                db.execute(
                    """UPDATE attendance
                       SET clock_in = ?, clock_out = ?, punch_type = ?, status = ?, late_reason = ?
                       WHERE id = ? AND user_id = ?""",
                    (clock_in, clock_out, ptype, status, reason, rec_id, cast_id),
                )
            else:
                db.execute(
                    """INSERT INTO attendance
                       (user_id, business_date, clock_in, clock_out, punch_type, status, late_reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (cast_id, date, clock_in, clock_out, ptype, status, reason),
                )

    # 送迎の有無
    pickup_on = bool(request.form.get("pickup_on"))
    existing_pickup = db.execute(
        "SELECT id FROM pickups WHERE user_id = ? AND business_date = ?",
        (cast_id, date),
    ).fetchone()
    if pickup_on and not existing_pickup:
        db.execute(
            """INSERT INTO pickups (user_id, business_date, clock_time)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, business_date) DO NOTHING""",
            (cast_id, date, now_jst().strftime("%H:%M:%S")),
        )
    elif not pickup_on and existing_pickup:
        db.execute("DELETE FROM pickups WHERE id = ?", (existing_pickup["id"],))

    db.commit()
    _sync_sheets(db, cast_id, date)

    flash("勤怠を保存しました。", "success")
    return redirect(url_for("admin_edit", cast_id=cast_id, date=date))


@app.route("/admin/sheets/export", methods=["POST"])
@admin_required
def admin_sheets_export():
    if not sheets.is_configured():
        flash("スプレッドシート連携が未設定です。", "error")
        return redirect(url_for("admin_history"))

    year = request.form.get("year", now_jst().year, type=int)
    month = request.form.get("month", now_jst().month, type=int)
    any_date = f"{year:04d}-{month:02d}-15"

    # 全キャストまとめて1回で書き出す（内部で全員分を再集計する）。
    _sync_sheets(None, None, any_date)

    flash(f"{year}年{month:02d}月のデータをスプレッドシートへ書き出しました（反映まで少し時間がかかることがあります）。", "success")
    return redirect(url_for("admin_history", year=year, month=month))


# --------------- Cast Management ---------------

@app.route("/admin/casts")
@admin_required
def admin_casts():
    db = get_db()
    casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()
    home_label = HOME_STORE_LABEL
    # 追加フォームの候補（既に登録済みのヘルプ店舗名）
    help_stores = sorted({
        c["store"].strip() for c in casts
        if not _is_home_store(c["store"])
    })
    return render_template(
        "admin_casts.html",
        casts=casts,
        home_label=home_label,
        help_stores=help_stores,
        is_home_store=_is_home_store,
    )


@app.route("/admin/casts/add", methods=["POST"])
@admin_required
def admin_cast_add():
    name = request.form.get("name", "").strip()
    if not name:
        flash("名前を入力してください。", "error")
        return redirect(url_for("admin_casts"))

    store = request.form.get("store", "").strip()
    # 本店を指す入力（空・店舗名一致）は空欄に正規化する。
    if _is_home_store(store):
        store = ""

    pickup_fee = request.form.get("pickup_fee", 1000, type=int)
    if pickup_fee not in (500, 1000):
        pickup_fee = 1000

    db = get_db()
    existing = db.execute(
        "SELECT id FROM users WHERE name = ? AND store = ?", (name, store)
    ).fetchone()
    if existing:
        where = HOME_STORE_LABEL if not store else store
        flash(f"「{name}」は{where}に既に登録されています。", "error")
        return redirect(url_for("admin_casts"))

    db.execute(
        "INSERT INTO users (name, is_admin, pickup_fee, store) VALUES (?, 0, ?, ?)",
        (name, pickup_fee, store),
    )
    db.commit()
    where = HOME_STORE_LABEL if not store else store
    flash(f"「{name}」を{where}に追加しました。", "success")
    return redirect(url_for("admin_casts"))


@app.route("/admin/casts/fee/<int:cast_id>", methods=["POST"])
@admin_required
def admin_cast_fee(cast_id):
    pickup_fee = request.form.get("pickup_fee", type=int)
    if pickup_fee not in (500, 1000):
        flash("送迎料金は500円か1000円を選択してください。", "error")
        return redirect(url_for("admin_casts"))

    db = get_db()
    cast = db.execute("SELECT * FROM users WHERE id = ? AND is_admin = 0", (cast_id,)).fetchone()
    if not cast:
        flash("キャストが見つかりません。", "error")
        return redirect(url_for("admin_casts"))

    db.execute("UPDATE users SET pickup_fee = ? WHERE id = ?", (pickup_fee, cast_id))
    db.commit()
    flash(f"「{cast['name']}」の送迎料金を{pickup_fee}円に設定しました。", "success")
    return redirect(url_for("admin_casts"))


@app.route("/admin/casts/delete/<int:cast_id>", methods=["POST"])
@admin_required
def admin_cast_delete(cast_id):
    db = get_db()
    cast = db.execute("SELECT * FROM users WHERE id = ? AND is_admin = 0", (cast_id,)).fetchone()
    if not cast:
        flash("キャストが見つかりません。", "error")
        return redirect(url_for("admin_casts"))

    # users を参照している行を先に削除しないと外部キー制約違反になる。
    db.execute("DELETE FROM attendance WHERE user_id = ?", (cast_id,))
    db.execute("DELETE FROM shifts WHERE user_id = ?", (cast_id,))
    db.execute("DELETE FROM pickups WHERE user_id = ?", (cast_id,))
    db.execute("DELETE FROM users WHERE id = ?", (cast_id,))
    db.commit()
    flash(f"「{cast['name']}」を削除しました。", "success")
    return redirect(url_for("admin_casts"))


@app.route("/admin/export")
@admin_required
def admin_export():
    db = get_db()
    year = request.args.get("year", now_jst().date().year, type=int)
    month = request.args.get("month", now_jst().date().month, type=int)
    cast_id = request.args.get("cast_id", 0, type=int)

    start_date = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end_date = f"{year + 1:04d}-01-01"
    else:
        end_date = f"{year:04d}-{month + 1:02d}-01"

    if cast_id:
        records = db.execute(
            """SELECT a.*, u.name as cast_name FROM attendance a
               JOIN users u ON a.user_id = u.id
               WHERE a.user_id = ? AND a.business_date >= ? AND a.business_date < ?
               ORDER BY a.business_date, a.id""",
            (cast_id, start_date, end_date),
        ).fetchall()
    else:
        records = db.execute(
            """SELECT a.*, u.name as cast_name FROM attendance a
               JOIN users u ON a.user_id = u.id
               WHERE u.is_admin = 0 AND a.business_date >= ? AND a.business_date < ?
               ORDER BY a.business_date, u.name, a.id""",
            (start_date, end_date),
        ).fetchall()

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(["キャスト", "営業日", "出勤時刻", "退勤時刻", "種別", "ステータス", "遅刻理由"])

    for r in records:
        punch = "同伴" if r["punch_type"] == "douhan" else ("当欠" if r["punch_type"] == "absent" else "通常")
        writer.writerow([
            r["cast_name"],
            r["business_date"],
            r["clock_in"] or "",
            r["clock_out"] or "",
            punch,
            r["status"] or "",
            r["late_reason"] or "",
        ])

    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8-sig"
    resp.headers["Content-Disposition"] = f"attachment; filename=kintai_{year}{month:02d}.csv"
    return resp


if __name__ == "__main__":
    import os
    debug = os.environ.get("FLASK_ENV") != "production"
    app.run(debug=debug, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
