"""DentScan — vehicle damage detection, repair estimates and insurance claims.

Run:  python app.py      (then open http://127.0.0.1:5000)
"""
import csv
import io
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta
from functools import wraps

import cv2
from dotenv import load_dotenv
from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

load_dotenv()

import database as dbm  # noqa: E402
import detector  # noqa: E402
import documents  # noqa: E402
import estimator  # noqa: E402
from database import PARTS, add_event, db, get_settings  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "instance"))
STATIC_DIR = os.path.join(BASE_DIR, "static")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
DOC_EXT = IMAGE_EXT | {".pdf"}
MAX_PHOTOS = 6
APP_NAME = os.getenv("APP_NAME", "DentScan")

app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_MB", "40")) * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)


def _load_secret_key():
    """Use SECRET_KEY from the environment, otherwise a random key persisted in instance/."""
    if os.getenv("SECRET_KEY"):
        return os.getenv("SECRET_KEY")
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.secret_key = _load_secret_key()
os.makedirs(DATA_DIR, exist_ok=True)
dbm.init_db()


# =====================================================================
# Template helpers
# =====================================================================
STATUS_LABELS = {
    "draft": "Estimate",
    "pending": "Submitted",
    "under_review": "In review",
    "approved": "Approved",
    "rejected": "Rejected",
}
EVENT_LABELS = {
    "created": "Estimate created",
    "edited": "Line items updated",
    "submitted": "Claim submitted",
    "under_review": "Assessor started review",
    "approved": "Claim approved",
    "rejected": "Claim rejected",
    "pending": "Moved back to submitted",
    "reopened": "Claim reopened",
}


@app.template_filter("inr")
def inr(value):
    """Indian digit grouping: 1234567 -> 12,34,567"""
    try:
        n = int(round(float(value or 0)))
    except (TypeError, ValueError):
        return value
    neg, s = n < 0, str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = f"{head},{tail}"
    return ("-" if neg else "") + "₹" + s


@app.template_filter("when")
def when(value, fmt="%d %b %Y"):
    if not value:
        return ""
    if isinstance(value, str):
        for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
            try:
                value = datetime.strptime(value, f)
                break
            except ValueError:
                continue
        else:
            return value
    return value.strftime(fmt)


@app.template_filter("ago")
def ago(value):
    if not value:
        return ""
    try:
        dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value
    secs = (datetime.now() - dt).total_seconds()
    if secs < 0:  # sqlite CURRENT_TIMESTAMP is UTC on legacy rows
        secs = 0
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = int(secs // size)
            return f"{n} {unit}{'s' if n > 1 else ''} ago"
    return "just now"


@app.context_processor
def inject_globals():
    open_count = 0
    if session.get("admin_email"):
        with db() as conn:
            open_count = conn.execute(
                "SELECT COUNT(*) FROM claims WHERE status IN ('pending','under_review')").fetchone()[0]
    return {
        "open_count": open_count,
        "csrf_token": csrf_token,
        "STATUS_LABELS": STATUS_LABELS,
        "EVENT_LABELS": EVENT_LABELS,
        "PARTS": PARTS,
        "current_user": getattr(g, "user", None),
        "is_admin": bool(session.get("admin_email")),
        "detector_status": detector.status(),
        "now_year": datetime.now().year,
        "APP_NAME": APP_NAME,
    }


# =====================================================================
# Security: CSRF + auth decorators
# =====================================================================
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
        if not token or not secrets.compare_digest(token, session.get("_csrf", "")):
            flash("Your session expired. Please try that again.", "error")
            return redirect(request.referrer or url_for("home"))


@app.before_request
def load_user():
    g.user = None
    email = session.get("user_email")
    if email:
        with db() as conn:
            g.user = conn.execute("SELECT * FROM user_info WHERE email=?", (email,)).fetchone()
        if g.user is None or g.user["status"] == "suspended":
            session.pop("user_email", None)
            if g.user is not None:
                flash("Your account has been suspended. Contact support to restore access.", "error")
            g.user = None


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user is None:
            flash("Log in to continue.", "info")
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not session.get("admin_email"):
            return redirect(url_for("admin_login"))
        return view(*a, **kw)
    return wrapped


def _safe_next(target):
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


# =====================================================================
# File storage
# =====================================================================
def _store(rel_path, data: bytes):
    full = os.path.join(DATA_DIR, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)
    return "data:" + rel_path.replace(os.sep, "/")


def _store_image(rel_path, img):
    full = os.path.join(DATA_DIR, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    cv2.imwrite(full, img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return "data:" + rel_path.replace(os.sep, "/")


def _resolve(stored):
    if not stored:
        return None
    if stored.startswith("data:"):
        root, rel = DATA_DIR, stored[5:]
    elif stored.startswith("static:"):
        root, rel = STATIC_DIR, stored[7:]
    else:
        root, rel = STATIC_DIR, stored
    full = os.path.realpath(os.path.join(root, rel))
    if not full.startswith(os.path.realpath(root) + os.sep):
        return None
    return full if os.path.exists(full) else None


def _remove_files(paths):
    for p in paths:
        full = _resolve(p)
        if full and full.startswith(os.path.realpath(DATA_DIR)):
            try:
                os.remove(full)
            except OSError:
                pass


# =====================================================================
# Claim helpers
# =====================================================================
def load_claim(conn, report_id, owner_email=None):
    q = "SELECT * FROM claims WHERE report_id=?"
    args = [report_id]
    if owner_email:
        q += " AND email=?"
        args.append(owner_email)
    claim = conn.execute(q, args).fetchone()
    if not claim:
        return None
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM claim_items WHERE report_id=? ORDER BY included DESC, id", (report_id,))]
    images = []
    for r in conn.execute("SELECT * FROM claim_images WHERE report_id=? ORDER BY position", (report_id,)):
        d = dict(r)
        d["detections"] = json.loads(d["detections"]) if d["detections"] else []
        images.append(d)
    events = conn.execute(
        "SELECT * FROM claim_events WHERE report_id=? ORDER BY created_at, id", (report_id,)).fetchall()
    return {"claim": claim, "items": items, "images": images, "events": events}


def recalc_claim(conn, report_id):
    claim = conn.execute("SELECT * FROM claims WHERE report_id=?", (report_id,)).fetchone()
    settings = get_settings(conn)
    items = [dict(r) for r in conn.execute("SELECT * FROM claim_items WHERE report_id=?", (report_id,))]
    for it in items:
        estimator.reprice_item(it, settings)
        conn.execute("UPDATE claim_items SET unit_price=?, labour=?, total=? WHERE id=?",
                     (it["unit_price"], it["labour"], it["total"], it["id"]))
    t = estimator.compute_totals(items, settings, claim["coverage_pct"], claim["deductible"])
    conn.execute(
        """UPDATE claims SET parts_cost=?, labour_cost=?, gst=?, total_estimate=?,
           insurance_coverage=?, customer_pays=?, updated_at=? WHERE report_id=?""",
        (t["parts_cost"], t["labour_cost"], t["gst"], t["total_estimate"], t["insurance_coverage"],
         t["customer_pays"], dbm.now(), report_id),
    )


def apply_item_edits(conn, report_id, form):
    """Update action / instances / included for each line item, and add a manual part."""
    changed = False
    for it in conn.execute("SELECT * FROM claim_items WHERE report_id=?", (report_id,)).fetchall():
        key = f"item-{it['id']}-"
        if key + "action" not in form:
            continue
        action = form.get(key + "action")
        action = action if action in ("repair", "replace") else it["action"]
        try:
            inst = min(10, max(1, int(form.get(key + "instances", it["instances"]))))
        except ValueError:
            inst = it["instances"]
        included = 1 if form.get(key + "included") else 0
        if (action, inst, included) != (it["action"], it["instances"], it["included"]):
            conn.execute("UPDATE claim_items SET action=?, instances=?, included=? WHERE id=?",
                         (action, inst, included, it["id"]))
            changed = True

    new_part = form.get("new_part")
    if new_part in PARTS:
        claim = conn.execute("SELECT car_brand, car_model FROM claims WHERE report_id=?", (report_id,)).fetchone()
        base, _ = dbm.part_price(conn, claim["car_brand"], claim["car_model"], new_part)
        action = form.get("new_action") if form.get("new_action") in ("repair", "replace") else "repair"
        conn.execute(
            """INSERT INTO claim_items(report_id, part, instances, confidence, severity, action, base_price)
               VALUES (?,?,?,?,?,?,?)""",
            (report_id, new_part, 1, None, "manual", action, base))
        changed = True
    if changed:
        recalc_claim(conn, report_id)
    return changed


# =====================================================================
# Public pages
# =====================================================================
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"ok": True, "detector": detector.status()})


@app.route("/api/models")
def api_models():
    with db() as conn:
        cat = dbm.get_catalogue(conn)
    brand = (request.args.get("brand") or "").upper()
    if brand:
        return jsonify(sorted(cat.get(brand, {}).keys()))
    return jsonify({b: sorted(m.keys()) for b, m in cat.items()})


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("dashboard"))
    with db() as conn:
        catalogue = {b: sorted(m.keys()) for b, m in dbm.get_catalogue(conn).items()}
    form = request.form
    if request.method == "POST":
        fields = ["name", "email", "password", "phoneNumber", "address", "vehicleId", "carBrand", "carModel"]
        values = {f: (form.get(f) or "").strip() for f in fields}
        if values["carBrand"] == "__other":
            values["carBrand"] = (form.get("carBrandOther") or "").strip().upper()
        if values["carModel"] == "__other" or not values["carModel"]:
            values["carModel"] = (form.get("carModelOther") or "").strip()
        error = None
        if not all(values.values()):
            error = "Fill in every field to create your account."
        elif not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", values["email"]):
            error = "Enter a valid email address."
        elif len(values["password"]) < 8:
            error = "Use a password of at least 8 characters."
        elif values["password"] != form.get("confirmPassword", ""):
            error = "The two passwords don't match."
        elif not re.match(r"^\+?[\d\s\-]{10,15}$", values["phoneNumber"]):
            error = "Enter a 10-digit phone number."
        if error:
            flash(error, "error")
            return render_template("signup.html", catalogue=catalogue, form=form)
        try:
            with db() as conn:
                conn.execute(
                    """INSERT INTO user_info (name, password, email, vehicle_id, contact_number, address,
                       car_brand, model, registration_date, status) VALUES (?,?,?,?,?,?,?,?,?, 'active')""",
                    (values["name"], dbm.hash_password(values["password"]), values["email"].lower(),
                     values["vehicleId"].upper().replace(" ", ""), values["phoneNumber"], values["address"],
                     values["carBrand"].upper(), values["carModel"], dbm.now()))
        except dbm.sqlite3.IntegrityError:
            flash("That email or registration number is already registered. Log in instead.", "error")
            return render_template("signup.html", catalogue=catalogue, form=form)
        session.clear()
        session["user_email"] = values["email"].lower()
        session.permanent = True
        flash(f"Welcome, {values['name'].split()[0]}. Your account is ready.", "success")
        return redirect(url_for("dashboard"))
    return render_template("signup.html", catalogue=catalogue, form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        with db() as conn:
            user = conn.execute("SELECT * FROM user_info WHERE LOWER(email)=?", (email,)).fetchone()
        if user and dbm.check_password(password, user["password"]):
            if user["status"] == "suspended":
                flash("This account is suspended. Contact support to restore access.", "error")
            else:
                session.pop("user_email", None)
                session["user_email"] = user["email"]
                session.permanent = bool(request.form.get("remember"))
                return redirect(_safe_next(request.args.get("next")) or url_for("dashboard"))
        else:
            flash("That email and password don't match an account.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You're logged out.", "info")
    return redirect(url_for("login"))


# =====================================================================
# User area
# =====================================================================
@app.route("/dashboard")
@login_required
def dashboard():
    email = g.user["email"]
    with db() as conn:
        stats = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(status='draft') AS drafts,
                      SUM(status IN ('pending','under_review')) AS open,
                      SUM(status='approved') AS approved,
                      COALESCE(SUM(CASE WHEN status='approved' THEN insurance_coverage END),0) AS paid
               FROM claims WHERE email=?""", (email,)).fetchone()
        recent = conn.execute(
            """SELECT c.*, (SELECT det_path FROM claim_images i WHERE i.report_id=c.report_id
                            ORDER BY position LIMIT 1) AS thumb,
                      (SELECT GROUP_CONCAT(part, ', ') FROM claim_items t WHERE t.report_id=c.report_id
                            AND included=1) AS parts
               FROM claims c WHERE email=? ORDER BY created_at DESC LIMIT 5""", (email,)).fetchall()
    return render_template("dashboard.html", stats=stats, recent=recent)


@app.route("/scan", methods=["GET", "POST"])
@login_required
def scan():
    settings = get_settings()
    if request.method == "GET":
        return render_template("scan.html", settings=settings)

    files = [f for f in request.files.getlist("images") if f and f.filename]
    if not files:
        flash("Add at least one photo of the damage.", "error")
        return redirect(url_for("scan"))
    if len(files) > MAX_PHOTOS:
        flash(f"Add up to {MAX_PHOTOS} photos per estimate. Only the first {MAX_PHOTOS} were used.", "info")
        files = files[:MAX_PHOTOS]

    st = detector.status()
    if not st["ready"]:
        flash(f"The damage detector isn't available: {st['message']}", "error")
        return redirect(url_for("scan"))

    try:
        conf = float(request.form.get("confidence", settings["min_confidence"]))
    except ValueError:
        conf = settings["min_confidence"]
    conf = min(0.9, max(0.05, conf))

    report_id = uuid.uuid4().hex[:8]
    processed, skipped, rejected = [], [], []
    for idx, f in enumerate(files):
        ext = os.path.splitext(secure_filename(f.filename))[1].lower()
        if ext not in IMAGE_EXT:
            skipped.append(f"{f.filename} (not a JPG, PNG or WebP image)")
            continue
        img = detector.read_image(f.read())
        if img is None:
            skipped.append(f"{f.filename} (couldn't be opened)")
            continue
        try:
            dets = detector.detect(img, conf=conf)
        except detector.DetectorUnavailable as e:
            flash(f"The damage detector couldn't start: {e}", "error")
            return redirect(url_for("scan"))
        if not dets and settings["vehicle_check"] and detector.contains_vehicle(img) is False:
            rejected.append(f.filename)
            continue
        processed.append((f.filename, img, dets))

    for s in skipped:
        flash(f"Skipped {s}.", "error")
    if rejected:
        flash("No vehicle found in: " + ", ".join(rejected) + ". Use photos that show the car.", "error")
    if not any(d for _, _, d in processed):
        if processed:
            flash("No damage was found in these photos. Try closer shots of the damaged area, "
                  "or lower the detection sensitivity.", "info")
        return redirect(url_for("scan"))

    user = g.user
    with db() as conn:
        items = estimator.aggregate([d for _, _, d in processed])
        settings = get_settings(conn)
        totals = estimator.price_items(conn, items, user["car_brand"], user["model"], settings)
        conn.execute(
            """INSERT INTO claims (report_id, email, car_brand, car_model, vehicle_id, title, status,
               parts_cost, labour_cost, gst, total_estimate, insurance_coverage, customer_pays,
               coverage_pct, deductible, created_at, updated_at)
               VALUES (?,?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,?,?)""",
            (report_id, user["email"], user["car_brand"], user["model"], user["vehicle_id"],
             (request.form.get("title") or "").strip()[:80] or None,
             totals["parts_cost"], totals["labour_cost"], totals["gst"], totals["total_estimate"],
             totals["insurance_coverage"], totals["customer_pays"], totals["coverage_pct"],
             totals["deductible"], dbm.now(), dbm.now()))
        for pos, (_, img, dets) in enumerate(processed):
            orig = _store_image(f"uploads/{report_id}/orig_{pos}.jpg", img)
            det = _store_image(f"uploads/{report_id}/det_{pos}.jpg", detector.annotate(img, dets))
            h, w = img.shape[:2]
            conn.execute(
                """INSERT INTO claim_images(report_id, position, orig_path, det_path, width, height, detections)
                   VALUES (?,?,?,?,?,?,?)""", (report_id, pos, orig, det, w, h, json.dumps(dets)))
        for it in items:
            conn.execute(
                """INSERT INTO claim_items(report_id, part, instances, confidence, severity, action,
                   base_price, unit_price, labour, total) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (report_id, it["part"], it["instances"], it["confidence"], it["severity"], it["action"],
                 it["base_price"], it["unit_price"], it["labour"], it["total"]))
        add_event(conn, report_id, user["email"], "created",
                  f"{len(processed)} photo{'s' if len(processed) != 1 else ''} analysed")
    if not totals["catalogue_match"]:
        flash(f"{user['car_brand'].title()} {user['model']} isn't in the price list yet, so standard "
              "prices were used for some parts.", "info")
    return redirect(url_for("claim_detail", report_id=report_id, new=1))


@app.route("/claims")
@login_required
def claims():
    status = request.args.get("status", "")
    q = (request.args.get("q") or "").strip()
    sql = """SELECT c.*, (SELECT det_path FROM claim_images i WHERE i.report_id=c.report_id
                          ORDER BY position LIMIT 1) AS thumb,
                    (SELECT GROUP_CONCAT(part, ', ') FROM claim_items t WHERE t.report_id=c.report_id
                          AND included=1) AS parts,
                    (SELECT COUNT(*) FROM claim_images i WHERE i.report_id=c.report_id) AS photo_count
             FROM claims c WHERE email=?"""
    args = [g.user["email"]]
    if status == "open":
        sql += " AND status IN ('pending','under_review')"
    elif status in dbm.CLAIM_STATUSES:
        sql += " AND status=?"
        args.append(status)
    if q:
        sql += " AND (report_id LIKE ? OR title LIKE ? OR policy_number LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY created_at DESC"
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return render_template("claims.html", claims=rows, status=status, q=q)


@app.route("/claims/<report_id>")
@login_required
def claim_detail(report_id):
    with db() as conn:
        data = load_claim(conn, report_id, g.user["email"])
    if not data:
        flash("That claim doesn't exist or belongs to another account.", "error")
        return redirect(url_for("claims"))
    return render_template("claim_detail.html", **data, settings=get_settings(), admin_view=False,
                           just_created=request.args.get("new"))


@app.route("/claims/<report_id>/items", methods=["POST"])
@login_required
def claim_update_items(report_id):
    with db() as conn:
        claim = conn.execute("SELECT * FROM claims WHERE report_id=? AND email=?",
                             (report_id, g.user["email"])).fetchone()
        if not claim:
            abort(404)
        if claim["status"] != "draft":
            flash("This claim is already submitted, so its line items are locked.", "error")
        elif apply_item_edits(conn, report_id, request.form):
            add_event(conn, report_id, g.user["email"], "edited")
            flash("Estimate updated.", "success")
    return redirect(url_for("claim_detail", report_id=report_id) + "#estimate")


@app.route("/claims/<report_id>/title", methods=["POST"])
@login_required
def claim_rename(report_id):
    with db() as conn:
        conn.execute("UPDATE claims SET title=? WHERE report_id=? AND email=?",
                     ((request.form.get("title") or "").strip()[:80] or None, report_id, g.user["email"]))
    return redirect(url_for("claim_detail", report_id=report_id))


@app.route("/submit_claim_documents/<report_id>", methods=["POST"])
@app.route("/claims/<report_id>/submit", methods=["POST"])
@login_required
def claim_submit(report_id):
    with db() as conn:
        claim = conn.execute("SELECT * FROM claims WHERE report_id=? AND email=?",
                             (report_id, g.user["email"])).fetchone()
        if not claim:
            abort(404)
        if claim["status"] not in ("draft", "rejected") and claim["rc_book_path"]:
            flash("This claim has already been submitted.", "info")
            return redirect(url_for("claim_detail", report_id=report_id))
        if claim["total_estimate"] <= 0:
            flash("Include at least one damaged part before submitting.", "error")
            return redirect(url_for("claim_detail", report_id=report_id))

        rc = request.files.get("rc_book")
        ins = request.files.get("insurance_copy")
        if not (rc and rc.filename and ins and ins.filename):
            flash("Attach both your RC book and your insurance policy to submit.", "error")
            return redirect(url_for("claim_detail", report_id=report_id) + "#submit")
        saved = {}
        for key, f in (("rc", rc), ("ins", ins)):
            ext = os.path.splitext(secure_filename(f.filename))[1].lower()
            if ext not in DOC_EXT:
                flash(f"{f.filename} isn't supported. Upload a PDF, JPG or PNG.", "error")
                return redirect(url_for("claim_detail", report_id=report_id) + "#submit")
            data = f.read()
            saved[key] = (data, _store(f"documents/{report_id}/{key}{ext}", data), f.filename)

        typed = re.sub(r"[^A-Za-z0-9/\-]", "", request.form.get("policy_number") or "").upper()
        if typed:
            policy, source = typed, "user"
        else:
            policy, source = documents.extract_policy_number(saved["ins"][0], saved["ins"][2])
        incident_date = request.form.get("incident_date") or None
        note = (request.form.get("incident_note") or "").strip()[:1000] or None
        conn.execute(
            """UPDATE claims SET status='pending', rc_book_path=?, insurance_copy_path=?, policy_number=?,
               policy_source=?, incident_date=?, incident_note=?, submitted_at=?, updated_at=?
               WHERE report_id=?""",
            (saved["rc"][1], saved["ins"][1], policy, source, incident_date, note, dbm.now(), dbm.now(),
             report_id))
        add_event(conn, report_id, g.user["email"], "submitted",
                  f"Policy {policy}" if policy else "Policy number to be checked by the assessor")
    flash("Claim submitted. You'll see the assessor's decision here.", "success")
    return redirect(url_for("claim_detail", report_id=report_id))


@app.route("/claims/<report_id>/delete", methods=["POST"])
@login_required
def claim_delete(report_id):
    with db() as conn:
        claim = conn.execute("SELECT * FROM claims WHERE report_id=? AND email=?",
                             (report_id, g.user["email"])).fetchone()
        if not claim:
            abort(404)
        if claim["status"] != "draft":
            flash("Only estimates that haven't been submitted can be deleted.", "error")
            return redirect(url_for("claim_detail", report_id=report_id))
        imgs = conn.execute("SELECT orig_path, det_path FROM claim_images WHERE report_id=?", (report_id,)).fetchall()
        conn.execute("DELETE FROM claims WHERE report_id=?", (report_id,))
    _remove_files([p for r in imgs for p in r])
    flash("Estimate deleted.", "success")
    return redirect(url_for("claims"))


@app.route("/claims/<report_id>/print")
def claim_print(report_id):
    owner = None if session.get("admin_email") else (g.user["email"] if g.user else None)
    if not owner and not session.get("admin_email"):
        return redirect(url_for("login"))
    with db() as conn:
        data = load_claim(conn, report_id, owner)
    if not data:
        abort(404)
    return render_template("claim_print.html", **data)


@app.route("/claims/<report_id>/file/<kind>")
@app.route("/claims/<report_id>/file/<kind>/<int:idx>")
def claim_file(report_id, kind, idx=0):
    """Serve photos and documents only to the claim's owner or an admin."""
    owner = None if session.get("admin_email") else (g.user["email"] if g.user else None)
    if not owner and not session.get("admin_email"):
        abort(403)
    with db() as conn:
        claim = conn.execute("SELECT * FROM claims WHERE report_id=?" + (" AND email=?" if owner else ""),
                             (report_id, owner) if owner else (report_id,)).fetchone()
        if not claim:
            abort(404)
        if kind in ("orig", "det"):
            row = conn.execute("SELECT * FROM claim_images WHERE report_id=? AND position=?",
                               (report_id, idx)).fetchone()
            stored = row[f"{kind}_path"] if row else None
        elif kind == "rc":
            stored = claim["rc_book_path"]
        elif kind == "insurance":
            stored = claim["insurance_copy_path"]
        else:
            abort(404)
    path = _resolve(stored)
    if not path:
        if kind in ("orig", "det"):
            return send_file(os.path.join(STATIC_DIR, "img", "missing.svg"), mimetype="image/svg+xml")
        abort(404)
    resp = send_file(path, conditional=True)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    with db() as conn:
        catalogue = {b: sorted(m.keys()) for b, m in dbm.get_catalogue(conn).items()}
        if request.method == "POST":
            f = request.form
            vals = {k: (f.get(k) or "").strip() for k in
                    ("name", "phoneNumber", "address", "vehicleId", "carBrand", "carModel")}
            if vals["carBrand"] == "__other":
                vals["carBrand"] = (f.get("carBrandOther") or "").strip()
            if vals["carModel"] in ("__other", ""):
                vals["carModel"] = (f.get("carModelOther") or "").strip()
            if not all(vals.values()):
                flash("Every field is required.", "error")
            else:
                try:
                    conn.execute(
                        """UPDATE user_info SET name=?, contact_number=?, address=?, vehicle_id=?,
                           car_brand=?, model=? WHERE user_id=?""",
                        (vals["name"], vals["phoneNumber"], vals["address"],
                         vals["vehicleId"].upper().replace(" ", ""), vals["carBrand"].upper(),
                         vals["carModel"], g.user["user_id"]))
                    flash("Profile saved. New estimates will use these vehicle details.", "success")
                    return redirect(url_for("profile"))
                except dbm.sqlite3.IntegrityError:
                    flash("That registration number belongs to another account.", "error")
        counts = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(total_estimate),0) v FROM claims WHERE email=?",
            (g.user["email"],)).fetchone()
    return render_template("profile.html", catalogue=catalogue, counts=counts,
                           editing=request.args.get("edit") or request.method == "POST")


@app.route("/profile/password", methods=["POST"])
@login_required
def change_password():
    cur, new, conf = (request.form.get(k) or "" for k in ("current", "new", "confirm"))
    if not dbm.check_password(cur, g.user["password"]):
        flash("Your current password is incorrect.", "error")
    elif len(new) < 8:
        flash("Use a new password of at least 8 characters.", "error")
    elif new != conf:
        flash("The new passwords don't match.", "error")
    else:
        with db() as conn:
            conn.execute("UPDATE user_info SET password=? WHERE user_id=?",
                         (dbm.hash_password(new), g.user["user_id"]))
        flash("Password changed.", "success")
    return redirect(url_for("profile") + "#security")


# ----- old URLs keep working -----
@app.route("/view_reports")
def view_reports():
    return redirect(url_for("claims"))


@app.route("/view_report/<report_id>")
def report_details(report_id):
    return redirect(url_for("claim_detail", report_id=report_id))


@app.route("/view_profile")
def view_profile():
    return redirect(url_for("profile"))


@app.route("/edit_profile")
def edit_profile():
    return redirect(url_for("profile", edit=1))


# =====================================================================
# Admin
# =====================================================================
@app.route("/Adminlogin", methods=["GET", "POST"])
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_email"):
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        email = (request.form.get("username") or "").strip().lower()
        pw = request.form.get("password") or ""
        with db() as conn:
            row = conn.execute("SELECT * FROM admins WHERE LOWER(email)=?", (email,)).fetchone()
        if row and dbm.check_password(pw, row["password"]):
            session.clear()
            session["admin_email"] = row["email"]
            session["admin_default_pw"] = pw == "admin123"
            return redirect(url_for("admin_dashboard"))
        flash("Those admin credentials aren't recognised.", "error")
    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin_dashboard")
@app.route("/admin")
@admin_required
def admin_dashboard():
    with db() as conn:
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM claims GROUP BY status")}
        money = conn.execute(
            """SELECT COALESCE(SUM(total_estimate),0) claimed,
                      COALESCE(SUM(CASE WHEN status='approved' THEN insurance_coverage END),0) paid,
                      COALESCE(AVG(NULLIF(total_estimate,0)),0) avg
               FROM claims WHERE status!='draft'""").fetchone()
        users = conn.execute(
            "SELECT COUNT(*) n, SUM(status='suspended') s FROM user_info").fetchone()
        start = (datetime.now() - timedelta(days=13)).strftime("%Y-%m-%d")
        daily = {r["d"]: r["n"] for r in conn.execute(
            "SELECT substr(created_at,1,10) d, COUNT(*) n FROM claims WHERE created_at>=? GROUP BY d", (start,))}
        days = [(datetime.now() - timedelta(days=13 - i)).strftime("%Y-%m-%d") for i in range(14)]
        series = [{"date": d, "n": daily.get(d, 0)} for d in days]
        parts = conn.execute(
            """SELECT part, COUNT(*) n, SUM(total) v FROM claim_items WHERE included=1
               GROUP BY part ORDER BY n DESC""").fetchall()
        queue = conn.execute(
            """SELECT c.*, u.name FROM claims c LEFT JOIN user_info u ON u.email=c.email
               WHERE c.status IN ('pending','under_review') ORDER BY c.submitted_at, c.created_at LIMIT 8""").fetchall()
        recent_users = conn.execute(
            "SELECT * FROM user_info ORDER BY registration_date DESC LIMIT 5").fetchall()
    return render_template("admin/dashboard.html", by_status=by_status, money=money, users=users,
                           series=series, parts=parts, queue=queue, recent_users=recent_users,
                           default_pw=session.get("admin_default_pw"))


def _admin_claims_query(args):
    status = args.get("status", "")
    q = (args.get("q") or "").strip()
    sql = """SELECT c.*, u.name, (SELECT GROUP_CONCAT(part, ', ') FROM claim_items t
                    WHERE t.report_id=c.report_id AND included=1) AS parts
             FROM claims c LEFT JOIN user_info u ON u.email=c.email WHERE 1=1"""
    params = []
    if status == "open":
        sql += " AND c.status IN ('pending','under_review')"
    elif status in dbm.CLAIM_STATUSES:
        sql += " AND c.status=?"
        params.append(status)
    else:
        sql += " AND c.status!='draft'"
    if q:
        sql += """ AND (c.report_id LIKE ? OR c.email LIKE ? OR u.name LIKE ? OR c.policy_number LIKE ?
                   OR c.vehicle_id LIKE ?)"""
        params += [f"%{q}%"] * 5
    sql += " ORDER BY c.created_at DESC"
    return sql, params, status, q


@app.route("/admin/claims")
@admin_required
def admin_claims():
    sql, params, status, q = _admin_claims_query(request.args)
    page = max(1, request.args.get("page", 1, type=int))
    per = 20
    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
        rows = conn.execute(sql + " LIMIT ? OFFSET ?", params + [per, (page - 1) * per]).fetchall()
    return render_template("admin/claims.html", claims=rows, status=status, q=q, page=page,
                           pages=max(1, -(-total // per)), total=total)


@app.route("/admin/claims.csv")
@admin_required
def admin_claims_csv():
    sql, params, _, _ = _admin_claims_query(request.args)
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["report_id", "status", "name", "email", "vehicle_id", "car_brand", "car_model", "parts",
            "parts_cost", "labour_cost", "gst", "total_estimate", "insurance_coverage", "customer_pays",
            "policy_number", "incident_date", "created_at", "submitted_at", "admin_note"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r[c] if c in r.keys() else "" for c in cols])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=claims_{datetime.now():%Y%m%d}.csv"})


@app.route("/admin/view_report_v2/<report_id>")
@app.route("/admin/claims/<report_id>")
@admin_required
def admin_claim(report_id):
    with db() as conn:
        data = load_claim(conn, report_id)
        if not data:
            flash("Claim not found.", "error")
            return redirect(url_for("admin_claims"))
        claimant = conn.execute("SELECT * FROM user_info WHERE email=?", (data["claim"]["email"],)).fetchone()
        history = conn.execute(
            "SELECT report_id, status, total_estimate, created_at FROM claims WHERE email=? AND report_id!=? "
            "ORDER BY created_at DESC LIMIT 5", (data["claim"]["email"], report_id)).fetchall()
    return render_template("admin/claim.html", **data, claimant=claimant, history=history,
                           settings=get_settings(), admin_view=True)


@app.route("/admin/claims/<report_id>/items", methods=["POST"])
@admin_required
def admin_claim_items(report_id):
    with db() as conn:
        claim = conn.execute("SELECT * FROM claims WHERE report_id=?", (report_id,)).fetchone()
        if not claim:
            abort(404)
        if claim["status"] in ("approved", "rejected"):
            flash("Reopen the claim before changing its line items.", "error")
        elif claim["status"] == "draft":
            flash("The customer hasn't submitted this estimate yet.", "error")
        elif apply_item_edits(conn, report_id, request.form):
            add_event(conn, report_id, "admin", "edited", "Adjusted by assessor")
            flash("Line items updated.", "success")
    return redirect(url_for("admin_claim", report_id=report_id))


@app.route("/update_report_status/<report_id>/<status>", methods=["POST"])
@app.route("/admin/claims/<report_id>/status", methods=["POST"])
@admin_required
def admin_claim_status(report_id, status=None):
    status = status or request.form.get("status")
    if status not in ("under_review", "approved", "rejected", "pending"):
        flash("Unknown status.", "error")
        return redirect(url_for("admin_claim", report_id=report_id))
    note = (request.form.get("note") or "").strip()[:1000] or None
    with db() as conn:
        claim = conn.execute("SELECT * FROM claims WHERE report_id=?", (report_id,)).fetchone()
        if not claim:
            abort(404)
        if status == "rejected" and not note:
            flash("Add a reason so the customer knows why the claim was rejected.", "error")
            return redirect(url_for("admin_claim", report_id=report_id) + "#decision")
        if status == "approved":
            try:
                cov = float(request.form.get("coverage_pct", claim["coverage_pct"] or 85))
                ded = int(float(request.form.get("deductible", claim["deductible"] or 0)))
            except ValueError:
                cov, ded = claim["coverage_pct"] or 85, claim["deductible"] or 0
            conn.execute("UPDATE claims SET coverage_pct=?, deductible=? WHERE report_id=?",
                         (min(100, max(0, cov)), max(0, ded), report_id))
            if not claim["legacy"]:
                recalc_claim(conn, report_id)
        conn.execute("UPDATE claims SET status=?, admin_note=COALESCE(?, admin_note), updated_at=? "
                     "WHERE report_id=?", (status, note, dbm.now(), report_id))
        event = "reopened" if status == "pending" and claim["status"] in ("approved", "rejected") else status
        add_event(conn, report_id, session["admin_email"], event, note)
    flash(f"Claim #{report_id} marked as {STATUS_LABELS[status].lower()}.", "success")
    return redirect(request.form.get("back") or url_for("admin_claim", report_id=report_id))


@app.route("/admin/users")
@admin_required
def user_manager():
    q = (request.args.get("q") or "").strip()
    sql = """SELECT u.*, (SELECT COUNT(*) FROM claims c WHERE c.email=u.email) AS claim_count
             FROM user_info u"""
    params = []
    if q:
        sql += " WHERE name LIKE ? OR email LIKE ? OR vehicle_id LIKE ? OR contact_number LIKE ?"
        params = [f"%{q}%"] * 4
    sql += " ORDER BY registration_date DESC"
    with db() as conn:
        users = conn.execute(sql, params).fetchall()
    return render_template("admin/users.html", users=users, q=q)


@app.route("/admin/suspend_user/<int:user_id>", methods=["POST"])
@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def suspend_user(user_id):
    with db() as conn:
        u = conn.execute("SELECT * FROM user_info WHERE user_id=?", (user_id,)).fetchone()
        if not u:
            flash("User not found.", "error")
        else:
            new = "active" if u["status"] == "suspended" else "suspended"
            conn.execute("UPDATE user_info SET status=? WHERE user_id=?", (new, user_id))
            flash(f"{u['name']} is now {'suspended' if new == 'suspended' else 'active again'}.", "success")
    return redirect(request.referrer or url_for("user_manager"))


@app.route("/admin/delete_user/<int:user_id>", methods=["POST"])
@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    with db() as conn:
        u = conn.execute("SELECT * FROM user_info WHERE user_id=?", (user_id,)).fetchone()
        if u:
            conn.execute("DELETE FROM user_info WHERE user_id=?", (user_id,))
            flash(f"Deleted {u['name']}. Their claim records were kept for audit.", "success")
    return redirect(url_for("user_manager"))


@app.route("/admin/prices", methods=["GET", "POST"])
@admin_required
def admin_prices():
    with db() as conn:
        if request.method == "POST":
            action = request.form.get("action")
            brand = (request.form.get("brand") or "").strip().upper()
            model = (request.form.get("model") or "").strip()
            if not brand or not model:
                flash("Brand and model are both required.", "error")
            elif action == "delete":
                conn.execute("DELETE FROM car_models WHERE brand=? AND model=?", (brand, model))
                flash(f"Removed {brand.title()} {model} from the price list.", "success")
                return redirect(url_for("admin_prices"))
            else:
                try:
                    prices = {p: int(float(request.form.get(f"price_{p}") or 0)) for p in PARTS}
                except ValueError:
                    flash("Prices must be whole numbers.", "error")
                    return redirect(url_for("admin_prices", brand=brand, model=model))
                for p, v in prices.items():
                    conn.execute("INSERT OR REPLACE INTO car_models(brand, model, part, price) VALUES (?,?,?,?)",
                                 (brand, model, p, max(0, v)))
                flash(f"Saved prices for {brand.title()} {model}.", "success")
            return redirect(url_for("admin_prices", brand=brand, model=model))
        catalogue = dbm.get_catalogue(conn)
    sel_brand = request.args.get("brand") or (next(iter(catalogue), ""))
    sel_model = request.args.get("model") or next(iter(catalogue.get(sel_brand, {})), "")
    return render_template("admin/prices.html", catalogue=catalogue, sel_brand=sel_brand, sel_model=sel_model,
                           fallback=dbm.FALLBACK_PRICES, new=request.args.get("new"))


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        form = request.form
        if form.get("form") == "password":
            with db() as conn:
                row = conn.execute("SELECT * FROM admins WHERE email=?", (session["admin_email"],)).fetchone()
                new = form.get("new") or ""
                if not dbm.check_password(form.get("current") or "", row["password"]):
                    flash("Your current password is incorrect.", "error")
                elif len(new) < 8 or new != form.get("confirm"):
                    flash("Use matching new passwords of at least 8 characters.", "error")
                else:
                    conn.execute("UPDATE admins SET password=? WHERE id=?", (dbm.hash_password(new), row["id"]))
                    session["admin_default_pw"] = False
                    flash("Admin password changed.", "success")
            return redirect(url_for("admin_settings"))
        limits = {"coverage_pct": (0, 100), "deductible": (0, 10 ** 7), "gst_pct": (0, 50),
                  "labour_pct": (0, 200), "repair_pct": (0, 100), "min_confidence": (0.05, 0.9)}
        with db() as conn:
            for k, (lo, hi) in limits.items():
                try:
                    v = min(hi, max(lo, float(form.get(k, ""))))
                except ValueError:
                    continue
                conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (k, f"{v:g}"))
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('vehicle_check', ?)",
                         ("1" if form.get("vehicle_check") else "0",))
        flash("Settings saved. They apply to new estimates.", "success")
        return redirect(url_for("admin_settings"))
    return render_template("admin/settings.html", settings=get_settings())


# =====================================================================
# Errors
# =====================================================================
@app.errorhandler(RequestEntityTooLarge)
def too_large(_):
    flash(f"That upload is too large. Keep the total under {app.config['MAX_CONTENT_LENGTH'] // 1048576} MB.",
          "error")
    return redirect(request.referrer or url_for("dashboard"))


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


@app.errorhandler(403)
def forbidden(_):
    return render_template("404.html", forbidden=True), 403


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"\n  DentScan running at http://127.0.0.1:{port}\n  Detector: {detector.status()['message']}\n")
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=port, debug=os.getenv("FLASK_DEBUG") == "1")
