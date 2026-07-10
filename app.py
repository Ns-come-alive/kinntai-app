import os
import csv
import io
import math
import logging
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

from database import get_db, init_db, ADMIN_USERNAME, ADMIN_PASSWORD
import sheets
from shift_import import parse_shift_image, is_configured as gemini_configured

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "kintai-app-dev-secret-key")

BUSINESS_DAY_START_HOUR = 20  # 20:00
BUSINESS_DAY_END_HOUR = 9    # 09:00
SITE_ACCESS_CODE = os.environ.get("SITE_ACCESS_CODE", "Gift-0723")
# 実質ほぼ無期限（秒）。ブラウザにより Max-Age の上限あり（例: Chrome は約400日で打ち切り）
SITE_ACCESS_COOKIE_MAX_AGE = int(os.environ.get("SITE_ACCESS_COOKIE_MAX_AGE", str(60 * 60 * 24 * 365 * 20)))


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
    return render_template("login.html", casts=casts)


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

    return render_template(
        "dashboard.html",
        records=records,
        shift=shift,
        business_date=business_date,
        currently_working=currently_working,
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
        flash("画像を選択してください。", "error")
        return redirect(url_for("admin_shifts"))

    if not gemini_configured():
        flash("画像読み取りが未設定です（GEMINI_API_KEY）。管理者に連絡してください。", "error")
        return redirect(url_for("admin_shifts"))

    image_bytes = file.read()
    mime = file.mimetype or "image/jpeg"

    db = get_db()
    casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()
    cast_names = [c["name"] for c in casts]
    name_to_id = {c["name"]: c["id"] for c in casts}
    year = now_jst().year

    try:
        shifts = parse_shift_image(image_bytes, mime, cast_names, year)
    except Exception as e:
        flash(f"画像の読み取りに失敗しました: {e}", "error")
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
        flash("シフトを読み取れませんでした。画像が鮮明か、キャスト名が登録名と一致しているか確認してください。", "warning")
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

    return {
        "total_days": len(working_dates),
        "total_work_hours": round(total_work_hours, 2),
        "total_late_hours": total_late_hours,
        "absent_days": len(absent_dates),
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


def _cast_history_rows(db, user_id, start_date, end_date):
    """キャスト1人の月間打刻履歴を行データにして返す。"""
    records = db.execute(
        """SELECT * FROM attendance
           WHERE user_id = ? AND business_date >= ? AND business_date < ?
           ORDER BY business_date, id""",
        (user_id, start_date, end_date),
    ).fetchall()

    rows = []
    for r in records:
        wh = _calc_work_hours(r["clock_in"], r["clock_out"])
        work = wh if wh is not None else ""
        rows.append([
            r["business_date"],
            r["clock_in"] or "",
            r["clock_out"] or "",
            work,
            _punch_type_label(r["punch_type"]),
            r["status"] or "",
            r["late_reason"] or "",
        ])
    return rows


def _sync_sheets(db, user_id, business_date):
    """指定キャスト・該当月のタブと、全員月間集計タブを最新化して送信する。"""
    if not sheets.is_configured():
        return
    try:
        start_date, end_date = _month_range(business_date)
        label = _month_label(business_date)

        cast = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not cast:
            return

        summary = _calc_cast_summary(db, user_id, start_date, end_date)
        history = _cast_history_rows(db, user_id, start_date, end_date)

        casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()
        summary_rows = [["キャスト", "出勤日数", "総稼働時間(h)", "遅刻時間(h)", "欠勤日数"]]
        for c in casts:
            s = _calc_cast_summary(db, c["id"], start_date, end_date)
            summary_rows.append([
                c["name"], s["total_days"], s["total_work_hours"],
                s["total_late_hours"], s["absent_days"],
            ])

        payload = {
            "month_label": label,
            "cast": {
                "tab": f"{cast['name']} {label}",
                "summary": [
                    ["出勤日数", summary["total_days"]],
                    ["総稼働時間(h)", summary["total_work_hours"]],
                    ["遅刻時間(h)", summary["total_late_hours"]],
                    ["欠勤日数", summary["absent_days"]],
                ],
                "history_header": ["営業日", "出勤", "退勤", "稼働(h)", "種別", "ステータス", "遅刻理由"],
                "history": history,
            },
            "summary": {
                "tab": f"月間集計 {label}",
                "rows": summary_rows,
            },
        }
        sheets.push(payload)
    except Exception:
        logger.exception("スプレッドシート同期の準備に失敗しました")


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
        records=records,
        year=year,
        month=month,
        cast_id=cast_id,
        cast_summaries=cast_summaries,
        sheets_enabled=sheets.is_configured(),
    )


@app.route("/admin/check-absent", methods=["POST"])
@admin_required
def check_absent():
    """営業日終了後に当欠チェックを実行"""
    db = get_db()
    business_date = request.form.get("business_date", get_business_date())

    casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()
    count = 0
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
            count += 1

    db.commit()

    for uid in affected:
        _sync_sheets(db, uid, business_date)

    flash(f"当欠チェック完了。{count}件の当欠を記録しました。", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/sheets/export", methods=["POST"])
@admin_required
def admin_sheets_export():
    if not sheets.is_configured():
        flash("スプレッドシート連携が未設定です。", "error")
        return redirect(url_for("admin_history"))

    db = get_db()
    year = request.form.get("year", now_jst().year, type=int)
    month = request.form.get("month", now_jst().month, type=int)
    any_date = f"{year:04d}-{month:02d}-15"

    casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()
    for c in casts:
        _sync_sheets(db, c["id"], any_date)

    flash(f"{year}年{month:02d}月のデータをスプレッドシートへ書き出しました（反映まで少し時間がかかることがあります）。", "success")
    return redirect(url_for("admin_history", year=year, month=month))


# --------------- Cast Management ---------------

@app.route("/admin/casts")
@admin_required
def admin_casts():
    db = get_db()
    casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()
    return render_template("admin_casts.html", casts=casts)


@app.route("/admin/casts/add", methods=["POST"])
@admin_required
def admin_cast_add():
    name = request.form.get("name", "").strip()
    if not name:
        flash("名前を入力してください。", "error")
        return redirect(url_for("admin_casts"))

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    if existing:
        flash(f"「{name}」は既に登録されています。", "error")
        return redirect(url_for("admin_casts"))

    db.execute("INSERT INTO users (name, is_admin) VALUES (?, 0)", (name,))
    db.commit()
    flash(f"「{name}」を追加しました。", "success")
    return redirect(url_for("admin_casts"))


@app.route("/admin/casts/delete/<int:cast_id>", methods=["POST"])
@admin_required
def admin_cast_delete(cast_id):
    db = get_db()
    cast = db.execute("SELECT * FROM users WHERE id = ? AND is_admin = 0", (cast_id,)).fetchone()
    if not cast:
        flash("キャストが見つかりません。", "error")
        return redirect(url_for("admin_casts"))

    db.execute("DELETE FROM attendance WHERE user_id = ?", (cast_id,))
    db.execute("DELETE FROM shifts WHERE user_id = ?", (cast_id,))
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
