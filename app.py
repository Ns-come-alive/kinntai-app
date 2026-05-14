import os
import csv
import io
import math
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, make_response
)

from database import get_db, init_db, ADMIN_USERNAME, ADMIN_PASSWORD

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "kintai-app-dev-secret-key")

BUSINESS_DAY_START_HOUR = 20  # 20:00
BUSINESS_DAY_END_HOUR = 9    # 09:00
SITE_ACCESS_CODE = os.environ.get("SITE_ACCESS_CODE", "Gift-0723")


def get_business_date(dt=None):
    """営業日を取得。20:00～翌09:00 を1日とする。
    20:00以降 → その日の日付が営業日
    00:00～08:59 → 前日の日付が営業日
    09:00～19:59 → 営業時間外（当日を返すが通常は使わない）
    """
    if dt is None:
        dt = datetime.now()
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
    init_db()


# --------------- Site Gate ---------------

@app.route("/gate", methods=["GET", "POST"])
def site_gate():
    if request.cookies.get("site_access") == SITE_ACCESS_CODE:
        return redirect(url_for("login"))

    if request.method == "POST":
        code = request.form.get("access_code", "").strip()
        if code == SITE_ACCESS_CODE:
            resp = make_response(redirect(url_for("login")))
            resp.set_cookie("site_access", SITE_ACCESS_CODE, max_age=60*60*24*30, httponly=True, samesite="Lax")
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
    now = datetime.now()
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


LATE_REASONS = ["交通機関の遅延", "体調不良", "その他"]


@app.route("/clock-in", methods=["POST"])
@login_required
def clock_in():
    punch_type = request.form.get("punch_type", "normal")
    db = get_db()
    user_id = session["user_id"]
    now = datetime.now()
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
    now = datetime.now()
    business_date = get_business_date(now)
    clock_time = now.strftime("%H:%M:%S")

    active = db.execute(
        "SELECT * FROM attendance WHERE user_id = ? AND business_date = ? AND clock_out IS NULL ORDER BY id DESC LIMIT 1",
        (user_id, business_date),
    ).fetchone()

    if not active:
        flash("出勤記録がありません。", "error")
        return redirect(url_for("dashboard"))

    clock_in_str = active["clock_in"]
    try:
        ci = datetime.strptime(clock_in_str, "%H:%M:%S")
        co = datetime.strptime(clock_time, "%H:%M:%S")
        # 日跨ぎ: 退勤が出勤より小さい場合は翌日
        if co < ci:
            diff = (co + timedelta(days=1) - ci).total_seconds()
        else:
            diff = (co - ci).total_seconds()
        hours = round(diff / 3600, 2)
    except (ValueError, TypeError):
        hours = 0

    db.execute(
        "UPDATE attendance SET clock_out = ? WHERE id = ?",
        (clock_time, active["id"]),
    )
    db.commit()
    flash("退勤しました。お疲れ様でした。", "success")
    return redirect(url_for("dashboard"))


# --------------- Cast History ---------------

@app.route("/history")
@login_required
def history():
    db = get_db()
    user_id = session["user_id"]

    year = request.args.get("year", date.today().year, type=int)
    month = request.args.get("month", date.today().month, type=int)

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
    now = datetime.now()
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
    )


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
    total_work_seconds = 0

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

        if r["clock_in"] and r["clock_out"]:
            try:
                ci = datetime.strptime(r["clock_in"], "%H:%M:%S")
                co = datetime.strptime(r["clock_out"], "%H:%M:%S")
                if co < ci:
                    diff = (co + timedelta(days=1) - ci).total_seconds()
                else:
                    diff = (co - ci).total_seconds()
                total_work_seconds += diff
            except (ValueError, TypeError):
                pass

    return {
        "total_days": len(working_dates),
        "total_work_hours": round(total_work_seconds / 3600, 1),
        "total_late_hours": total_late_hours,
        "absent_days": len(absent_dates),
    }


@app.route("/admin/history")
@admin_required
def admin_history():
    db = get_db()

    year = request.args.get("year", date.today().year, type=int)
    month = request.args.get("month", date.today().month, type=int)
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
    )


@app.route("/admin/check-absent", methods=["POST"])
@admin_required
def check_absent():
    """営業日終了後に当欠チェックを実行"""
    db = get_db()
    business_date = request.form.get("business_date", get_business_date())

    casts = db.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY id").fetchall()
    count = 0
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
            count += 1

    db.commit()
    flash(f"当欠チェック完了。{count}件の当欠を記録しました。", "success")
    return redirect(url_for("admin_dashboard"))


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
    year = request.args.get("year", date.today().year, type=int)
    month = request.args.get("month", date.today().month, type=int)
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
