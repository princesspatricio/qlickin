import os, io, re, sys, csv, json, time, hmac, hashlib, base64, difflib, mimetypes, random, secrets, smtplib, threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from html import escape
import urllib.error, urllib.request
from zoneinfo import ZoneInfo

import qrcode
import firebase_admin
from dotenv import load_dotenv
from firebase_admin import credentials, firestore
from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.base_query import FieldFilter
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, send_file, abort, Response)
from werkzeug.security import generate_password_hash, check_password_hash
import icons

load_dotenv()
app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]

# ---------- Firestore connection ----------
_json = (os.getenv("FIREBASE_CREDENTIALS_JSON") or "").strip()
_path = os.getenv("FIREBASE_CREDENTIALS", "serviceAccountKey.json").strip()
try:
    _cred = credentials.Certificate(json.loads(_json) if _json.startswith("{") else (_json or _path))
    firebase_admin.initialize_app(_cred)
    db = firestore.client()
except Exception as _e:      # show a clear reason in the Render logs
    sys.exit(f"STARTUP ERROR: could not connect to Firebase -> {type(_e).__name__}: {_e}. "
             "Check FIREBASE_CREDENTIALS_JSON (or the secret file) on Render.")

TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Manila"))
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000").rstrip("/")
COURSES = ["BSIT", "BSA", "BSOA", "BSHM", "BSED"]
MAJORS = ["Major in Math", "Major in English", "Major in Filipino"]
STATUSES = ("present", "late", "absent", "excused")
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SETTING_DEFAULTS = {"rotate_qr": True, "rotate_seconds": 120, "email_on_present": False,
                    "email_quiz_student": True, "email_quiz_teacher": False, "pass_percent": 75}
MAX_QUESTIONS = 35
MAX_FILE = 10 * 1024 * 1024            # 10 MB per uploaded file
CHUNK = 700_000                        # Firestore documents are limited to 1 MiB, so files are split
ALLOWED_EXT = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt", "csv", "png", "jpg", "jpeg",
               "gif", "webp", "zip", "mp3", "mp4"}
INLINE_OK = {"pdf", "png", "jpg", "jpeg", "gif", "webp", "txt"}     # can be opened in the browser
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024


# ---------- Firestore helpers ----------
def _dict(d):
    return {**d.to_dict(), "id": d.id}

def find(col, **eq):
    """All documents in `col` where every field equals the given value."""
    q = db.collection(col)
    for k, v in eq.items():
        q = q.where(filter=FieldFilter(k, "==", v))
    return [_dict(d) for d in q.stream()]

def get(col, doc_id):
    if not doc_id:                      # Firestore rejects empty document names
        return None
    d = db.collection(col).document(doc_id).get()
    return _dict(d) if d.exists else None

def settings():
    d = db.collection("settings").document("main").get()
    return {**SETTING_DEFAULTS, **(d.to_dict() if d.exists else {})}

def log(action, detail):
    db.collection("activity").add({"action": action, "detail": detail, "at": now().isoformat(),
                                   "by": session.get("name", "system")})

def notify(student_id, message):
    db.collection("notifications").add({"student_id": student_id, "message": message,
                                        "created_at": now().isoformat(), "read": False})


# ---------- general helpers ----------
def now():
    return datetime.now(TZ)          # server time, so students cannot fake it

def today_str():
    return now().date().isoformat()

def t12(hms):
    return datetime.strptime(hms[:8], "%H:%M:%S").strftime("%I:%M %p") if hms else "-"

def hhmm12(hhmm):
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")

def sec(s):
    return s["major"] if s["course"] == "BSED" else s["section"]

def label(s):
    tail = s["major"].replace("Major in ", "") if s["course"] == "BSED" else s["section"]
    return f"{s['course']} {s['year_level']}-{tail}"

def class_key_of(s):
    return f"{s['course']}|{s['year_level']}|{sec(s)}"

def full_name(s):
    return f"{s['last_name']}, {s['first_name']} {s['middle_initial']}."

def sess_label(x):
    return f"{x['subject']} - {x['class_label']} ({x['day']} {hhmm12(x['start_time'])})"

def ans_text(r):
    if r.get("status", "present") in ("absent", "excused"):
        return "-"
    return "No answer" if r.get("is_correct") is None else ("Correct" if r["is_correct"] else "Incorrect")

def summarize(rows):
    c = {k: 0 for k in STATUSES}
    for r in rows:
        c[r.get("status", "present")] += 1
    base = c["present"] + c["late"] + c["absent"]          # excused days are not counted against the student
    c["rate"] = round((c["present"] + c["late"]) * 100 / base) if base else None
    return c

def per_student(rows, desc=False, acts=(), subs=None):
    g = defaultdict(list)
    for r in rows:
        g[r["student_id"]].append(r)
    out = []
    for rs in g.values():
        c = summarize(rs)
        c["student"] = rs[0]["students"]
        c["act"] = activity_totals(c["student"], acts, subs or {})
        out.append(c)
    out.sort(key=lambda x: x["student"]["last_name"].lower(), reverse=desc)
    return out

app.jinja_env.globals.update(sec=sec, label=label, full_name=full_name, sess_label=sess_label,
                             hhmm12=hhmm12, courses=COURSES, majors=MAJORS, days=DAYS)

@app.context_processor
def inject_counts():
    n = todo = 0
    if session.get("role") == "student":
        uid = session["uid"]
        n = len(find("notifications", student_id=uid, read=False))
        st = get("students", uid)
        if st:
            done = {x["activity_id"] for x in find("submissions", student_id=uid)}
            todo = sum(1 for x in find("activities") if visible_to(st, x) and x["id"] not in done)
    return {"unread": n, "todo": todo}

def login_required(role):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            if session.get("role") != role:
                return redirect(url_for("login"))
            return fn(*a, **k)
        return wrapper
    return deco

def safe_next(default):
    nxt = request.form.get("next", "")
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else default


# ---------- rotating QR code ----------
def _sig(token, step):
    return hmac.new(app.secret_key.encode(), f"{token}.{step}".encode(), hashlib.sha256).hexdigest()[:12]

def qr_remaining(cfg):
    return cfg["rotate_seconds"] - int(time.time() % cfg["rotate_seconds"])

def qr_payload(st):
    cfg = settings()
    if not cfg["rotate_qr"]:
        return st["qr_token"]
    step = int(time.time() // cfg["rotate_seconds"])
    return f"{st['qr_token']}.{step}.{_sig(st['qr_token'], step)}"

def resolve_qr(text):
    """Return (student, error_message)."""
    cfg, parts = settings(), text.strip().split(".")
    if cfg["rotate_qr"]:
        if len(parts) != 3:
            return None, "Outdated QR code. Ask the student to open the portal and show the live QR code."
        try:
            step = int(parts[1])
        except ValueError:
            return None, "Invalid QR code."
        if not hmac.compare_digest(parts[2], _sig(parts[0], step)):
            return None, "Invalid QR code."
        if abs(int(time.time() // cfg["rotate_seconds"]) - step) > 1:
            return None, "This QR code has expired. Ask the student to let it refresh."
    s = find("students", qr_token=parts[0])
    return (s[0], None) if s else (None, "Invalid QR code.")


# ---------- email ----------
def _send_resend(api_key, to, subject, text, html, images, attachments):
    """Send through Resend's HTTPS API. It works on Render's free plan, which blocks SMTP ports."""
    sender = (os.getenv("MAIL_FROM") or "").strip() or "QlickIn <onboarding@resend.dev>"
    files = [{"filename": f"{cid}.png", "content": base64.b64encode(data).decode(), "content_id": cid}
             for cid, data in images.items()]                      # inline images (cid:logo, cid:qrcode)
    files += [{"filename": name, "content": base64.b64encode(data).decode()}
              for name, data in (attachments or {}).items()]
    payload = {"from": sender, "to": [to], "subject": subject, "text": text}
    if html:
        payload["html"] = html
    if files:
        payload["attachments"] = files
    req = urllib.request.Request("https://api.resend.com/emails", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                                          "User-Agent": "QlickIn/1.0"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as ex:
        print("Resend error", ex.code, ex.read().decode("utf-8", "ignore")[:400])
        return False
    except Exception as ex:
        print("Resend error:", ex)
        return False

def send_mail(to, subject, text, html=None, images=None, attachments=None):
    """Send an HTML email with inline images. Returns True if sent (or printed in dev mode).
    Uses Resend when RESEND_API_KEY is set, otherwise Gmail SMTP, otherwise prints a preview."""
    images = images or {}
    api_key = (os.getenv("RESEND_API_KEY") or "").strip()
    if api_key:
        return _send_resend(api_key, to, subject, text, html, images, attachments)
    user, pw = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
    if not user or not pw:                      # dev mode: print + save a preview instead of sending
        print(f"\n[EMAIL to {to}] {subject}\n{text}\n")
        if html:
            prev = html
            for cid, data in images.items():
                prev = prev.replace(f"cid:{cid}", "data:image/png;base64," + base64.b64encode(data).decode())
            with open("email_preview.html", "w", encoding="utf-8") as fh:
                fh.write(prev)
            print("Email design preview saved as email_preview.html - open it in your browser.\n")
        return True
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = f"QlickIn <{user}>", to, subject
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")
        part = m.get_payload()[1]
        for cid, data in images.items():
            part.add_related(data, "image", "png", cid=f"<{cid}>")
    for name, data in (attachments or {}).items():
        m.add_attachment(data, maintype="image", subtype="png", filename=name)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(user, pw)
            srv.send_message(m)
        return True
    except Exception as ex:
        print("Email error:", ex)
        return False

def qr_png(token):
    buf = io.BytesIO()
    qrcode.make(token, box_size=8, border=2).save(buf, "PNG")
    return buf.getvalue()

def logo_png():
    """Draw the QlickIn logo as a PNG (Gmail does not show SVG images)."""
    from PIL import Image, ImageDraw
    size, gold, wine = 256, (212, 175, 55), (10, 34, 70)
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((6, 6, size - 6, size - 6), radius=56, fill=wine, outline=gold, width=10)
    for x, y in ((40, 40), (150, 40), (40, 150)):
        d.rectangle((x, y, x + 66, y + 66), outline=gold, width=10)
        d.rectangle((x + 22, y + 22, x + 44, y + 44), fill=gold)
    d.line([(144, 184), (170, 210), (214, 152)], fill=(255, 255, 255), width=20, joint="curve")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()

def email_html(first_name, title, intro, button_text, button_url, portal_url,
               details=(), steps=(), qr_block=""):
    e = escape
    rows = "".join(
        f'<tr><td style="padding:5px 14px 5px 0;color:#5f6b85;font-size:14px">{e(k)}</td>'
        f'<td style="padding:5px 0;color:#0e1a33;font-size:14px;font-weight:bold">{e(v)}</td></tr>'
        for k, v in details)
    details_box = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 24px"><tr>'
        '<td style="padding:12px 16px;background:#fffcf2;border-left:4px solid #d4af37">'
        f'<table role="presentation" cellpadding="0" cellspacing="0">{rows}</table></td></tr></table>') if rows else ""
    items = "".join(f'<li style="margin:0 0 6px">{e(x)}</li>' for x in steps)
    steps_box = (
        '<p style="margin:26px 0 8px;color:#0a2246;font-weight:bold;font-size:15px">How to use QlickIn</p>'
        f'<ol style="margin:0;padding-left:20px;color:#0e1a33;font-size:14px;line-height:1.5">{items}</ol>') if items else ""
    return f"""<!doctype html><html><body style="margin:0;padding:0;background:#f3eedd;font-family:'Segoe UI',Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3eedd"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden">
<tr><td style="background:#0a2246;padding:24px 28px;border-bottom:5px solid #d4af37">
<table role="presentation" cellpadding="0" cellspacing="0"><tr>
<td><img src="cid:logo" width="64" height="64" alt="QlickIn" style="display:block;border:0"></td>
<td style="padding-left:16px"><div style="font-family:Georgia,serif;font-size:32px;color:#ffffff;line-height:1">QlickIn</div>
<div style="color:#f1d675;font-size:13px;letter-spacing:1px;margin-top:5px">Scan. Answer. Present.</div></td>
</tr></table></td></tr>
<tr><td style="padding:30px 28px">
<h2 style="font-family:Georgia,serif;color:#0a2246;margin:0 0 12px;font-size:24px">{e(title)}</h2>
<p style="color:#0e1a33;font-size:15px;line-height:1.6;margin:0 0 20px">Hello {e(first_name)},<br>{e(intro)}</p>
{details_box}
<table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:6px auto 4px"><tr>
<td style="background:#0a2246;border:2px solid #d4af37;border-radius:8px">
<a href="{e(button_url)}" style="display:inline-block;padding:14px 30px;color:#ffffff;font-weight:bold;font-size:16px;text-decoration:none">{e(button_text)}</a>
</td></tr></table>
<p style="text-align:center;color:#5f6b85;font-size:12px;margin:10px 0 0">Button not working? Copy this link into your browser:<br>
<a href="{e(button_url)}" style="color:#0a2246;word-break:break-all">{e(button_url)}</a></p>
{qr_block}
{steps_box}
<p style="margin:24px 0 0;color:#0e1a33;font-size:14px">Your portal: <a href="{e(portal_url)}" style="color:#0a2246;font-weight:bold">{e(portal_url)}</a></p>
</td></tr>
<tr><td style="background:#f3eedd;padding:16px 28px;color:#5f6b85;font-size:12px;text-align:center;border-top:1px solid #e6dcc0">
QlickIn Attendance System &middot; This email was sent automatically, please do not reply.</td></tr>
</table></td></tr></table></body></html>"""

QR_IMG_BLOCK = """<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:28px 0 0"><tr>
<td align="center" style="padding:22px;background:#fffcf2;border:2px solid #d4af37;border-radius:12px">
<div style="font-family:Georgia,serif;color:#0a2246;font-size:18px;font-weight:bold;margin-bottom:12px">Your personal QR code</div>
<img src="cid:qrcode" width="220" height="220" alt="Your QlickIn QR code" style="display:block;margin:0 auto;border:0;background:#ffffff;padding:8px">
<div style="color:#5f6b85;font-size:13px;line-height:1.5;margin-top:12px">Show this QR code to your teacher to be marked present.<br>
It is also attached to this email and saved on your portal Home page.</div></td></tr></table>"""

def qr_note_block(seconds):
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:28px 0 0"><tr>'
            '<td style="padding:18px 20px;background:#fffcf2;border:2px solid #d4af37;border-radius:12px;color:#0e1a33;font-size:14px;line-height:1.6">'
            '<b style="color:#0a2246">Your QR code is live.</b><br>For security it refreshes automatically every '
            f'{max(1, seconds // 60)} minute(s), so a screenshot cannot be shared. Open your portal and show the QR code on the '
            'Home page when your teacher takes attendance.</td></tr></table>')

def send_simple_email(who, subject, title, intro, button_text, url):
    """`who` needs first_name and email (works for students and the teacher)."""
    portal = f"{BASE_URL}/login"
    text = f"Hello {who['first_name']},\n\n{intro}\n\n{button_text}: {url}\n"
    html = email_html(who["first_name"], title, intro, button_text, url, portal)
    return send_mail(who["email"], subject, text, html, {"logo": logo_png()})

def send_welcome_email(st, activation_token=None, password=None, resend=False):
    """Email with the portal link and the student's QR code."""
    cfg, portal = settings(), f"{BASE_URL}/login"
    details = [("Name", full_name(st)), ("Class", label(st)), ("Login email", st["email"])]
    if password:
        details.append(("Password", password))
    if activation_token:
        url, btn = f"{BASE_URL}/activate/{activation_token}", "Activate & open my portal"
        title = "Your account was approved"
        subject = "QlickIn: your account was approved"
        intro = ("Great news! Your teacher approved your registration. Press the button below to activate "
                 "your account and you will go straight to your QlickIn portal.")
    else:
        url, btn = portal, "Open QlickIn portal"
        title = "Your QlickIn portal link" if resend else "Your QlickIn account is ready"
        subject = "QlickIn: your portal link" if resend else "QlickIn: your account is ready"
        intro = ("Here is your QlickIn portal link again." if resend else
                 "Your teacher registered you in QlickIn. Your account is already active - log in with the details below.")
    steps = ["Press the button above to open your portal.",
             "Open the Home page to see your QR code.",
             "Show your QR code to your teacher when attendance is taken, then answer the question."]
    show_qr = not cfg["rotate_qr"]
    qr = qr_png(st["qr_token"]) if show_qr else None
    block = QR_IMG_BLOCK if show_qr else qr_note_block(cfg["rotate_seconds"])
    text = (f"Hello {st['first_name']},\n\n{intro}\n\n{btn}: {url}\nYour portal: {portal}\n"
            + (f"Email: {st['email']}\nPassword: {password}\n" if password else "")
            + ("\nYour QR code is attached (QlickIn-QR.png). Show it to your teacher to be marked present.\n" if show_qr
               else "\nYour QR code refreshes automatically - open your portal Home page to show it.\n"))
    html = email_html(st["first_name"], title, intro, btn, url, portal, details, steps, block)
    images = {"logo": logo_png()}
    if show_qr:
        images["qrcode"] = qr
    return send_mail(st["email"], subject, text, html, images, {"QlickIn-QR.png": qr} if show_qr else None)


# ---------- attendance logic ----------
def status_for(sess, n):
    h, m = map(int, sess["start_time"].split(":"))
    start = n.replace(hour=h, minute=m, second=0, microsecond=0)
    return "late" if n > start + timedelta(minutes=int(sess["late_minutes"])) else "present"

def att_doc(student, sess, status, date, time_=None, method="auto", question_id=None, note=""):
    return {"student_id": student["id"], "teacher_id": session.get("uid"), "session_id": sess["id"],
            "session_label": sess["subject"], "question_id": question_id, "scan_date": date,
            "scan_time": time_, "class_label": sess["class_label"], "status": status, "method": method,
            "answer_given": None, "is_correct": None, "note": note}

def low_attendance_check(st, sess):
    """Email the student when they have missed 3 (or 6, 9...) classes in a row in this subject."""
    rows = sorted(find("attendance", student_id=st["id"], session_id=sess["id"]),
                  key=lambda a: a["scan_date"], reverse=True)
    streak = 0
    for r in rows:
        if r["status"] == "absent":
            streak += 1
        elif r["status"] == "excused":
            continue
        else:
            break
    if streak >= 3 and streak % 3 == 0:
        msg = f"You have missed {streak} classes in a row in {sess['subject']}. Please talk to your teacher."
        notify(st["id"], "Low attendance alert: " + msg)
        send_simple_email(st, f"QlickIn: low attendance alert - {sess['subject']}", "Low attendance alert",
                          msg, "Open my attendance", f"{BASE_URL}/login")

def finalize(sess, date):
    """Mark every approved student of the session's class who has no record that day as Absent."""
    have = {a["student_id"] for a in find("attendance", session_id=sess["id"], scan_date=date)}
    n = 0
    for st in find("students", status="approved"):
        if class_key_of(st) != sess["class_key"] or st["id"] in have:
            continue
        try:
            db.collection("attendance").document(f"{st['id']}_{sess['id']}_{date}") \
                .create(att_doc(st, sess, "absent", date))
        except AlreadyExists:
            continue
        n += 1
        notify(st["id"], f"You were marked absent in {sess['subject']} on {date}.")
        low_attendance_check(st, sess)
    db.collection("session_days").document(f"{sess['id']}_{date}") \
        .set({"session_id": sess["id"], "date": date, "finalized_at": now().isoformat()})
    return n

_LAST_AUTO = {"t": 0}
def auto_finalize():
    """For past days where a class was held, mark the students who never scanned as Absent."""
    if time.time() - _LAST_AUTO["t"] < 300:
        return
    _LAST_AUTO["t"] = time.time()
    today = today_str()
    done = {d.id for d in db.collection("session_days").stream()}
    sessions = {x["id"]: x for x in find("sessions")}
    seen = set()
    for a in find("attendance"):
        sid, key = a.get("session_id"), f"{a.get('session_id')}_{a['scan_date']}"
        if sid in sessions and a["scan_date"] < today and key not in done and key not in seen:
            seen.add(key)
            finalize(sessions[sid], a["scan_date"])

def pick_question(class_key):
    pool = [q for q in find("questions", is_active=True)]
    mine = [q for q in pool if q.get("class_key", "ALL") == class_key] \
        or [q for q in pool if q.get("class_key", "ALL") == "ALL"]
    return random.choice(mine) if mine else None


# ---------- question generation from uploaded files ----------
def extract_text_bytes(filename, data):
    name = (filename or "").lower()
    if name.endswith(".txt"):
        return data.decode("utf-8", "ignore")
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        return " ".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
    if name.endswith(".docx"):
        import docx
        return " ".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)
    if name.endswith(".pptx"):
        from pptx import Presentation
        return " ".join(sh.text_frame.text for sl in Presentation(io.BytesIO(data)).slides
                        for sh in sl.shapes if sh.has_text_frame)
    return ""

def extract_text(file):
    return extract_text_bytes(file.filename, file.read())

def generate_questions(text, n):
    """Fill-in-the-blank generator (up to n questions). Uses extra words of the same sentences when
    the file has fewer sentences than questions asked. Replace with an AI call for smarter ones."""
    text = re.sub(r"\s+", " ", text)
    sentences = [x for x in re.split(r"(?<=[.!?])\s", text) if 40 < len(x) < 220]
    random.shuffle(sentences)
    pool = list({w for w in re.findall(r"[A-Za-z]{6,}", text)})
    words_of = {}
    for x in sentences:
        ws = list(dict.fromkeys(re.findall(r"[A-Za-z]{6,}", x)))
        random.shuffle(ws)
        words_of[x] = ws
    order = [(x, words_of[x][rnd]) for rnd in range(8) for x in sentences if rnd < len(words_of[x])]
    out = []
    for sentence, answer in order:
        others = [w for w in pool if w.lower() != answer.lower()]
        if len(others) < 3:
            continue
        choices = random.sample(others, 3) + [answer]
        random.shuffle(choices)
        out.append({"question_text": "Fill in the blank: " + sentence.replace(answer, "_____", 1),
                    "choices": choices, "correct_answer": answer})
        if len(out) >= n:
            break
    return out

def class_from_form(f):
    """Return (class_key, class_label) for a class chosen in a form, or (None, None)."""
    course = f.get("course", "ALL")
    if course == "ALL":
        return "ALL", "All classes"
    bsed = course == "BSED"
    year = f.get("year", "")
    part = f.get("major") if bsed else f.get("section")
    if course not in COURSES or year not in ("1", "2", "3", "4") \
            or part not in (MAJORS if bsed else ["A", "B", "C"]):
        return None, None
    tail = part.replace("Major in ", "") if bsed else part
    return f"{course}|{year}|{part}", f"{course} {year}-{tail}"


# ---------- public ----------
@app.route("/")
def index():
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        pw = request.form["password"]
        t = find("teachers", email=email)
        if t and check_password_hash(t[0]["password_hash"], pw):
            session.clear()
            session.update(role="teacher", uid=t[0]["id"], name=t[0]["name"])
            return redirect(url_for("t_home"))
        s = find("students", email=email)
        if s and check_password_hash(s[0]["password_hash"], pw):
            s = s[0]
            if s["status"] == "pending":
                flash("Waiting for teacher approval.")
            elif s["status"] == "declined":
                flash("Your registration was declined.")
            elif not s["activated"]:
                flash("Please open the activation link sent to your Gmail first.")
            else:
                session.clear()
                session.update(role="student", uid=s["id"], name=full_name(s))
                return redirect(url_for("s_home"))
        else:
            flash("Wrong email or password.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/register/<token>", methods=["GET", "POST"])
def register(token):
    t = find("teachers", reg_token=token)
    if not t or not t[0]["reg_open"]:
        abort(404)
    if request.method == "POST":
        f = request.form
        email, course = f["email"].strip().lower(), f["course"]
        bsed = course == "BSED"
        err = None
        if not email.endswith("@gmail.com"):
            err = "Use a valid @gmail.com address."
        elif len(f["password"]) < 8:
            err = "Password must be at least 8 characters."
        elif f["password"] != f["confirm"]:
            err = "Passwords do not match."
        elif course not in COURSES or (bsed and f.get("major") not in MAJORS) \
                or (not bsed and f.get("section") not in ("A", "B", "C")):
            err = "Please choose a valid course, section or major."
        elif find("students", email=email):
            err = "That email is already registered."
        if err:
            flash(err)
        else:
            db.collection("students").add({
                "teacher_id": t[0]["id"],
                "last_name": f["last_name"].strip(), "first_name": f["first_name"].strip(),
                "middle_initial": f["mi"].strip().upper()[:1], "email": email,
                "password_hash": generate_password_hash(f["password"]),
                "course": course, "year_level": int(f["year"]),
                "section": None if bsed else f["section"],
                "major": f["major"] if bsed else None,
                "status": "pending", "activated": False, "activation_token": None,
                "qr_token": secrets.token_hex(16), "created_at": now().isoformat(),
            })
            flash("Registration sent! Wait for your teacher's approval. Check your Gmail afterwards.")
            return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/activate/<token>")
def activate(token):
    s = [x for x in find("students", activation_token=token) if x["status"] == "approved"]
    if not s:
        flash("This activation link is invalid or was already used. Please log in below.")
        return redirect(url_for("login"))
    s = s[0]
    db.collection("students").document(s["id"]).update({"activated": True, "activation_token": None})
    session.clear()                                   # one-time link: activate AND open the portal
    session.update(role="student", uid=s["id"], name=full_name(s))
    flash("Welcome to QlickIn! Your account is activated.")
    return redirect(url_for("s_home"))

@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        for col in ("students", "teachers"):
            u = find(col, email=email)
            if u:
                tok = secrets.token_urlsafe(24)
                db.collection(col).document(u[0]["id"]).update(
                    {"reset_token": tok, "reset_expires": (now() + timedelta(hours=1)).isoformat()})
                who = {"first_name": u[0].get("first_name") or u[0].get("name", ""), "email": email}
                send_simple_email(who, "QlickIn: reset your password", "Reset your password",
                                  "We received a request to reset your password. This link works for 1 hour.",
                                  "Choose a new password", f"{BASE_URL}/reset/{tok}")
                break
        flash("If that email is registered, a password reset link was sent.")
        return redirect(url_for("login"))
    return render_template("forgot.html")

@app.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    acct = None
    for col in ("students", "teachers"):
        r = find(col, reset_token=token)
        if r:
            acct = (col, r[0])
            break
    if not acct or acct[1].get("reset_expires", "") < now().isoformat():
        flash("This reset link is invalid or has expired. Please request a new one.")
        return redirect(url_for("forgot"))
    if request.method == "POST":
        pw = request.form["password"]
        if len(pw) < 8:
            flash("Password must be at least 8 characters.")
        elif pw != request.form["confirm"]:
            flash("Passwords do not match.")
        else:
            db.collection(acct[0]).document(acct[1]["id"]).update(
                {"password_hash": generate_password_hash(pw), "reset_token": None, "reset_expires": None})
            flash("Password changed. You can log in now.")
            return redirect(url_for("login"))
    return render_template("reset.html")


# ---------- teacher: dashboard ----------
def rate_of(rows):
    return summarize(rows)["rate"]

@app.route("/teacher")
@login_required("teacher")
def t_home():
    auto_finalize()
    me = get("teachers", session["uid"])
    studs = find("students")
    smap = {s["id"]: s for s in studs}
    att = [a for a in find("attendance") if a["student_id"] in smap]
    today = today_str()
    tod = [a for a in att if a["scan_date"] == today]
    groups = defaultdict(list)
    for a in att:
        groups[a.get("class_label", "-")].append(a)
    class_rates = [{"label": k, "pct": rate_of(v)} for k, v in sorted(groups.items()) if rate_of(v) is not None]
    monday = now().date() - timedelta(days=now().date().weekday())
    weeks = []
    for i in range(7, -1, -1):
        start = monday - timedelta(weeks=i)
        end = start + timedelta(days=6)
        rows = [a for a in att if start.isoformat() <= a["scan_date"] <= end.isoformat()]
        weeks.append({"label": start.strftime("%b %d"), "pct": rate_of(rows)})
    absences = defaultdict(int)
    for a in att:
        if a.get("status") == "absent":
            absences[a["student_id"]] += 1
    top_abs = [{"student": smap[k], "count": v} for k, v in sorted(absences.items(), key=lambda x: -x[1])[:5]]
    return render_template("t_home.html",
        link=f"{BASE_URL}/register/{me['reg_token']}", reg_open=me["reg_open"],
        pending=sum(s["status"] == "pending" for s in studs),
        approved=sum(s["status"] == "approved" for s in studs),
        present=sum(a.get("status", "present") in ("present", "late") for a in tod),
        absent=sum(a.get("status") == "absent" for a in tod),
        class_rates=class_rates, weeks=weeks, top_abs=top_abs)

@app.post("/teacher/reglink/toggle")
@login_required("teacher")
def t_toggle():
    me = get("teachers", session["uid"])
    db.collection("teachers").document(me["id"]).update({"reg_open": not me["reg_open"]})
    return redirect(url_for("t_home"))


# ---------- teacher: registrations, add / edit / delete / resend ----------
@app.route("/teacher/registrations")
@login_required("teacher")
def t_regs():
    f = {k: request.args.get(k, "") for k in ("status", "course", "year", "section")}
    order = request.args.get("order", "asc")
    rows = [s for s in find("students")
            if (not f["status"] or s["status"] == f["status"])
            and (not f["course"] or s["course"] == f["course"])
            and (not f["year"] or str(s["year_level"]) == f["year"])
            and (not f["section"] or sec(s) == f["section"])]
    rows.sort(key=lambda s: s["last_name"].lower(), reverse=(order == "desc"))
    return render_template("t_regs.html", rows=rows, f=f, order=order)

@app.post("/teacher/registrations/<sid>/<action>")
@login_required("teacher")
def t_decide(sid, action):
    s = get("students", sid)
    if action not in ("approve", "decline") or not s:
        abort(400)
    ref = db.collection("students").document(sid)
    if action == "approve":
        tok = secrets.token_urlsafe(24)
        ref.update({"status": "approved", "activation_token": tok})
        sent = send_welcome_email(s, activation_token=tok)
        log("Approved student", full_name(s))
        flash(f"Approved {full_name(s)}. " + ("Email with the portal link was sent."
              if sent else "The email could not be sent - check your email settings (RESEND_API_KEY and MAIL_FROM)."))
    else:
        ref.update({"status": "declined"})
        log("Declined student", full_name(s))
        flash("Registration declined.")
    return redirect(url_for("t_regs"))

@app.post("/teacher/students/<sid>/resend")
@login_required("teacher")
def t_resend(sid):
    st = get("students", sid)
    if not st or st["status"] != "approved":
        abort(404)
    if st["activated"]:
        sent = send_welcome_email(st, resend=True)
    else:
        tok = secrets.token_urlsafe(24)
        db.collection("students").document(sid).update({"activation_token": tok})
        sent = send_welcome_email(st, activation_token=tok)
    log("Resent email", full_name(st))
    flash(f"Email resent to {st['email']}." if sent else "The email could not be sent - check your email settings (RESEND_API_KEY and MAIL_FROM).")
    return redirect(url_for("t_regs"))

def parse_student_form(f, self_id=None):
    """Validate the student form. Returns (data, error)."""
    email, course = f.get("email", "").strip().lower(), f.get("course", "")
    bsed = course == "BSED"
    if not (f.get("last_name", "").strip() and f.get("first_name", "").strip() and f.get("mi", "").strip()):
        return None, "Please fill in last name, first name and middle initial."
    if not email.endswith("@gmail.com"):
        return None, "Use a valid @gmail.com address."
    if course not in COURSES or f.get("year") not in ("1", "2", "3", "4") \
            or (bsed and f.get("major") not in MAJORS) or (not bsed and f.get("section") not in ("A", "B", "C")):
        return None, "Please choose a valid course, year, and section or major."
    if any(x["id"] != self_id for x in find("students", email=email)):
        return None, "That email is already registered."
    return {"last_name": f["last_name"].strip(), "first_name": f["first_name"].strip(),
            "middle_initial": f["mi"].strip().upper()[:1], "email": email, "course": course,
            "year_level": int(f["year"]), "section": None if bsed else f["section"],
            "major": f["major"] if bsed else None}, None

def form_values(src=None):
    base = {"last_name": "", "first_name": "", "mi": "", "email": "", "course": "BSIT",
            "year": "1", "section": "A", "major": MAJORS[0], "status": "approved"}
    if not src:
        return base
    if "middle_initial" in src:       # a student document from Firestore
        return {**base, "last_name": src["last_name"], "first_name": src["first_name"],
                "mi": src["middle_initial"], "email": src["email"], "course": src["course"],
                "year": str(src["year_level"]), "section": src["section"] or "A",
                "major": src["major"] or MAJORS[0], "status": src["status"]}
    return {k: src.get(k, v) for k, v in base.items()}   # submitted form

@app.route("/teacher/students/add", methods=["GET", "POST"])
@login_required("teacher")
def t_add_student():
    if request.method == "POST":
        f = request.form
        data, err = parse_student_form(f)
        pw = f.get("password", "").strip()
        if not err and pw and len(pw) < 8:
            err = "Password must be at least 8 characters."
        if err:
            flash(err)
        else:
            pw = pw or secrets.token_urlsafe(6)
            qr_token = secrets.token_hex(16)
            sent = False
            db.collection("students").add({
                **data, "teacher_id": session["uid"], "password_hash": generate_password_hash(pw),
                "status": "approved", "activated": True, "activation_token": None,
                "qr_token": qr_token, "created_at": now().isoformat()})
            if f.get("send_email"):
                sent = send_welcome_email({**data, "qr_token": qr_token}, password=pw)
            log("Registered student", f"{data['last_name']}, {data['first_name']}")
            flash(f"Student registered and approved. Login password: {pw}  (write it down - it is shown only once)"
                  + (" Email sent." if sent else ""))
            return redirect(url_for("t_regs"))
        return render_template("t_student_form.html", mode="add", v=form_values(f))
    return render_template("t_student_form.html", mode="add", v=form_values())

@app.route("/teacher/students/<sid>/edit", methods=["GET", "POST"])
@login_required("teacher")
def t_edit_student(sid):
    st = get("students", sid)
    if not st:
        abort(404)
    if request.method == "POST":
        f = request.form
        data, err = parse_student_form(f, self_id=sid)
        pw, status = f.get("password", "").strip(), f.get("status", st["status"])
        if not err and pw and len(pw) < 8:
            err = "Password must be at least 8 characters."
        if not err and status not in ("pending", "approved", "declined"):
            err = "Invalid status."
        if err:
            flash(err)
            return render_template("t_student_form.html", mode="edit", v=form_values(f), sid=sid)
        data["status"] = status
        if status == "approved":
            data["activated"] = True          # approved by the teacher directly
        if pw:
            data["password_hash"] = generate_password_hash(pw)
        db.collection("students").document(sid).update(data)
        log("Edited student", full_name({**st, **data}))
        flash(f"Updated {full_name({**st, **data})}.")
        return redirect(url_for("t_regs"))
    return render_template("t_student_form.html", mode="edit", v=form_values(st), sid=sid)

@app.post("/teacher/students/<sid>/delete")
@login_required("teacher")
def t_delete_student(sid):
    st = get("students", sid)
    if not st:
        abort(404)
    for sub in find("submissions", student_id=sid):
        for fl in sub.get("files", []):
            delete_file(fl["id"])
        db.collection("submissions").document(sub["id"]).delete()
    for att in find("quiz_attempts", student_id=sid):
        db.collection("quiz_attempts").document(att["id"]).delete()
    batch = db.batch()
    for col, key in (("attendance", "student_id"), ("notifications", "student_id")):
        for d in find(col, **{key: sid}):
            batch.delete(db.collection(col).document(d["id"]))
    batch.delete(db.collection("students").document(sid))
    batch.commit()
    log("Deleted student", full_name(st))
    flash(f"Deleted {full_name(st)} and their attendance records.")
    return redirect(url_for("t_regs"))


# ---------- teacher: subject sessions ----------
@app.route("/teacher/sessions", methods=["GET", "POST"])
@login_required("teacher")
def t_sessions():
    if request.method == "POST":
        f = request.form
        key, lbl = class_from_form(f)
        subject, day, start = f.get("subject", "").strip(), f.get("day"), f.get("start_time", "")
        try:
            datetime.strptime(start, "%H:%M")
            late = int(f.get("late_minutes", "15"))
        except ValueError:
            key = None
        if not key or key == "ALL" or not subject or day not in DAYS or not 0 <= late <= 180:
            flash("Please complete the form: subject, class, day, start time and late minutes.")
        else:
            db.collection("sessions").add({"subject": subject, "class_key": key, "class_label": lbl,
                "day": day, "start_time": start, "late_minutes": late,
                "teacher_id": session["uid"], "created_at": now().isoformat()})
            log("Created session", f"{subject} - {lbl}")
            flash(f"Session created: {subject} for {lbl}.")
        return redirect(url_for("t_sessions"))
    rows = sorted(find("sessions"), key=lambda x: (x["class_label"], x["subject"]))
    return render_template("t_sessions.html", rows=rows)

@app.post("/teacher/sessions/<sid>/delete")
@login_required("teacher")
def t_session_delete(sid):
    x = get("sessions", sid)
    if x:
        db.collection("sessions").document(sid).delete()
        log("Deleted session", f"{x['subject']} - {x['class_label']}")
    return redirect(url_for("t_sessions"))

@app.post("/teacher/sessions/<sid>/finalize")
@login_required("teacher")
def t_finalize(sid):
    x = get("sessions", sid)
    if not x:
        abort(404)
    n = finalize(x, today_str())
    log("Ended class", f"{x['subject']} - {n} marked absent")
    flash(f"Class ended. {n} student(s) marked absent.")
    return redirect(url_for("t_scan", session=sid))


# ---------- teacher: question pool ----------
@app.route("/teacher/questions", methods=["GET", "POST"])
@login_required("teacher")
def t_questions():
    if request.method == "POST":
        key, lbl = class_from_form(request.form)
        file = request.files.get("file")
        try:
            count = max(1, min(MAX_QUESTIONS, int(request.form.get("count", "10"))))
        except ValueError:
            count = 10
        qs = generate_questions(extract_text(file), count) if key and file and file.filename else []
        if not key:
            flash("Please choose a valid class.")
        elif not qs:
            flash("Could not generate questions. Use a TXT/PDF/DOCX/PPTX file with enough readable text.")
        else:
            for q in qs:
                db.collection("questions").add({**q, "teacher_id": session["uid"], "source_file": file.filename,
                    "class_key": key, "class_label": lbl, "is_active": bool(request.form.get("add_pool")),
                    "created_at": now().isoformat()})
            flash(f"{len(qs)} question(s) generated for {lbl}."
                  + (f" You asked for {count}, but the file only had enough text for {len(qs)}. Upload a longer file for more."
                     if len(qs) < count else ""))
        return redirect(url_for("t_questions"))
    rows = find("questions")
    for q in rows:
        q.setdefault("class_label", "All classes")
    rows.sort(key=lambda q: q["created_at"], reverse=True)
    return render_template("t_questions.html", rows=rows)

@app.post("/teacher/questions/<qid>/toggle")
@login_required("teacher")
def t_qtoggle(qid):
    q = get("questions", qid)
    if not q:
        abort(404)
    db.collection("questions").document(qid).update({"is_active": not q.get("is_active")})
    return redirect(url_for("t_questions"))

@app.route("/teacher/questions/<qid>/edit", methods=["GET", "POST"])
@login_required("teacher")
def t_qedit(qid):
    q = get("questions", qid)
    if not q:
        abort(404)
    if request.method == "POST":
        f = request.form
        text = f.get("question_text", "").strip()
        raw = [f.get(f"c{i}", "").strip() for i in range(1, 5)]
        choices = [c for c in raw if c]
        try:
            correct = raw[int(f.get("correct", "1")) - 1]
        except (ValueError, IndexError):
            correct = ""
        if not text or len(choices) < 2 or len(set(choices)) != len(choices) or not correct:
            flash("Fill in the question, at least 2 different choices, and pick a correct choice that is not empty.")
        else:
            db.collection("questions").document(qid).update(
                {"question_text": text, "choices": choices, "correct_answer": correct})
            flash("Question updated.")
            return redirect(url_for("t_questions"))
    choices = (q["choices"] + ["", "", "", ""])[:4]
    return render_template("t_question_edit.html", q=q, choices=choices,
                           correct=(q["choices"].index(q["correct_answer"]) + 1) if q["correct_answer"] in q["choices"] else 1)

@app.post("/teacher/questions/<qid>/delete")
@login_required("teacher")
def t_qdelete(qid):
    if get("questions", qid):
        db.collection("questions").document(qid).delete()
    return redirect(url_for("t_questions"))


# ---------- teacher: scan + manual attendance ----------
@app.route("/teacher/scan")
@login_required("teacher")
def t_scan():
    auto_finalize()
    sessions = sorted(find("sessions"), key=lambda x: (x["class_label"], x["subject"]))
    studs = sorted(find("students", status="approved"), key=lambda s: s["last_name"].lower())
    for s in studs:
        s["ckey"] = class_key_of(s)
    return render_template("t_scan.html", sessions=sessions, studs=studs,
                           sel=request.args.get("session", ""), today=today_str())

@app.post("/api/scan")
@login_required("teacher")
def api_scan():
    d = request.get_json()
    sess = get("sessions", d.get("session_id", ""))
    if not sess:
        return jsonify(ok=False, error="Choose a subject session first."), 400
    s, err = resolve_qr(d.get("qr_token", ""))
    if err:
        return jsonify(ok=False, error=err), 400
    if s["status"] != "approved" or not s["activated"]:
        return jsonify(ok=False, error="Student account is not approved/activated."), 400
    if class_key_of(s) != sess["class_key"]:
        return jsonify(ok=False, error=f"{full_name(s)} is not in this class ({label(s)})."), 400
    n = now()
    today = n.date().isoformat()
    ref = db.collection("attendance").document(f"{s['id']}_{sess['id']}_{today}")
    cur = ref.get()
    if cur.exists and cur.to_dict().get("status") != "absent":
        return jsonify(ok=False, error=f"{full_name(s)} is already marked {cur.to_dict().get('status')} today."), 400
    q = pick_question(sess["class_key"])
    if not q:
        return jsonify(ok=False, error=f"No question in the pool for {sess['class_label']}. "
                                       "Open Question Manager and add questions for this class."), 400
    status = status_for(sess, n)
    ref.set(att_doc(s, sess, status, today, n.strftime("%H:%M:%S"), "qr", q["id"]))
    notify(s["id"], f"You were marked {status} in {sess['subject']} at {n.strftime('%I:%M %p')}.")
    if settings()["email_on_present"]:
        send_simple_email(s, f"QlickIn: marked {status} in {sess['subject']}", f"You are marked {status}",
                          f"Your teacher scanned your QR code for {sess['subject']} at {n.strftime('%I:%M %p')}.",
                          "Open my attendance", f"{BASE_URL}/login")
    return jsonify(ok=True, attendance_id=ref.id, student=full_name(s), time=n.strftime("%I:%M %p"),
                   status=status, question=q["question_text"], choices=random.sample(q["choices"], len(q["choices"])))

@app.post("/api/answer")
@login_required("teacher")
def api_answer():
    d = request.get_json()
    a = get("attendance", d["attendance_id"])
    if not a or a.get("answer_given"):
        return jsonify(ok=False, error="Already answered or not found."), 400
    q = get("questions", a["question_id"])
    correct = d["answer"] == q["correct_answer"]
    db.collection("attendance").document(a["id"]).update({"answer_given": d["answer"], "is_correct": correct})
    return jsonify(ok=True, correct=correct, correct_answer=q["correct_answer"])

@app.post("/teacher/attendance/manual")
@login_required("teacher")
def t_manual():
    f = request.form
    sess, st = get("sessions", f.get("session_id", "")), get("students", f.get("student_id", ""))
    status, date = f.get("status"), f.get("date") or today_str()
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        sess = None
    if not sess or not st or status not in STATUSES:
        flash("Choose a session, a student, a date and a status.")
        return redirect(url_for("t_scan"))
    if class_key_of(st) != sess["class_key"]:
        flash(f"{full_name(st)} is not in the class of this session.")
        return redirect(url_for("t_scan", session=sess["id"]))
    t = now().strftime("%H:%M:%S") if status in ("present", "late") and date == today_str() else None
    db.collection("attendance").document(f"{st['id']}_{sess['id']}_{date}") \
        .set(att_doc(st, sess, status, date, t, "manual", note=f.get("note", "").strip()))
    notify(st["id"], f"Your teacher marked you {status} in {sess['subject']} on {date}.")
    log("Manual attendance", f"{full_name(st)} - {status} - {sess['subject']} - {date}")
    flash(f"{full_name(st)} marked {status}.")
    return redirect(url_for("t_scan", session=sess["id"]))

@app.post("/teacher/attendance/<aid>/status")
@login_required("teacher")
def t_status(aid):
    a, status = get("attendance", aid), request.form.get("status")
    if not a or status not in STATUSES:
        abort(400)
    db.collection("attendance").document(aid).update({"status": status, "method": "manual"})
    st = get("students", a["student_id"])
    if st:
        notify(st["id"], f"Your teacher changed your attendance to {status} ({a.get('session_label', '')}, {a['scan_date']}).")
        log("Changed status", f"{full_name(st)} - {status} - {a['scan_date']}")
    return redirect(safe_next(url_for("t_records")))


# ---------- teacher: records, export ----------
def load_records(args):
    smap = {x["id"]: x for x in find("students")}
    acts = find("activities")
    subs = {(x["activity_id"], x["student_id"]): x for x in find("submissions")}
    f = {k: args.get(k, "") for k in ("date", "month", "course", "year", "section", "status", "subject", "q")}
    order = args.get("order", "asc")
    rows = []
    for r in find("attendance"):
        st = smap.get(r["student_id"])
        if not st:
            continue
        r.update(students=st, status=r.get("status", "present"), subject=r.get("session_label", "-"),
                 time12=t12(r.get("scan_time")), activity=activity_cell(st, r["scan_date"], acts, subs))
        if ((not f["date"] or r["scan_date"] == f["date"])
                and (not f["month"] or r["scan_date"].startswith(f["month"]))
                and (not f["course"] or st["course"] == f["course"])
                and (not f["year"] or str(st["year_level"]) == f["year"])
                and (not f["section"] or sec(st) == f["section"])
                and (not f["status"] or r["status"] == f["status"])
                and (not f["subject"] or r["subject"] == f["subject"])
                and (not f["q"] or f["q"].lower() in full_name(st).lower())):
            rows.append(r)
    rows.sort(key=lambda r: (r["students"]["last_name"].lower(), r["students"]["first_name"].lower(), r["scan_date"]),
              reverse=(order == "desc"))
    return rows, f, order, acts, subs

@app.route("/teacher/records")
@login_required("teacher")
def t_records():
    auto_finalize()
    rows, f, order, acts, subs = load_records(request.args)
    return render_template("t_records.html", rows=rows, f=f, order=order, summary=summarize(rows),
                           students=per_student(rows, order == "desc", acts, subs),
                           subjects=sorted({x["subject"] for x in find("sessions")}),
                           printed=now().strftime("%B %d, %Y"), qs=request.query_string.decode(),
                           next=request.full_path.rstrip("?"))

@app.route("/teacher/records/export/<fmt>")
@login_required("teacher")
def t_export(fmt):
    rows, f, order, acts, subs = load_records(request.args)
    head = ["#", "Surname", "First name", "MI", "Course", "Year", "Section/Major", "Subject", "Date", "Time", "Status", "Activity"]
    body = [[i, r["students"]["last_name"], r["students"]["first_name"], r["students"]["middle_initial"],
             r["students"]["course"], r["students"]["year_level"], sec(r["students"]), r["subject"],
             r["scan_date"], r["time12"], r["status"].capitalize(), r["activity"]] for i, r in enumerate(rows, 1)]
    shead = ["#", "Surname", "First name", "Course", "Year", "Section/Major", "Present", "Late", "Absent", "Excused", "Activity score", "Rate %"]
    sbody = [[i, x["student"]["last_name"], x["student"]["first_name"], x["student"]["course"], x["student"]["year_level"],
              sec(x["student"]), x["present"], x["late"], x["absent"], x["excused"],
              f"{num(x['act']['earned'])}/{num(x['act']['possible'])} ({x['act']['pct']}%)" if x["act"]["possible"] else "-",
              "-" if x["rate"] is None else x["rate"]]
             for i, x in enumerate(per_student(rows, order == "desc", acts, subs), 1)]
    buf = io.BytesIO()
    if fmt == "xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        wb = Workbook()
        for ws, title, h, b in ((wb.active, "Records", head, body), (wb.create_sheet("Summary"), "Summary", shead, sbody)):
            ws.title = title
            ws.append(h)
            for c in ws[1]:
                c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="5B1230")
            for r in b:
                ws.append(r)
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(40, max(len(str(c.value or "")) for c in col) + 3)
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name="QlickIn-attendance.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if fmt == "pdf":
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        ss = getSampleStyleSheet()
        def tbl(h, b):
            t = Table([h] + [[Paragraph(str(c), ss["BodyText"]) if isinstance(c, str) and len(c) > 18 else c for c in row] for row in b], repeatRows=1)
            t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0a2246")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f1ee")])]))
            return t
        els = [Paragraph("QlickIn Attendance Form", ss["Title"]),
               Paragraph(f"Printed {now().strftime('%B %d, %Y')} - Teacher: {session['name']}", ss["Normal"]), Spacer(1, 10),
               tbl(head, body), Spacer(1, 16), Paragraph("Attendance summary per student", ss["Heading2"]),
               tbl(shead, sbody), Spacer(1, 40), Paragraph("Signature: ______________________", ss["Normal"])]
        SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24).build(els)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name="QlickIn-attendance.pdf", mimetype="application/pdf")
    abort(404)


# ---------- teacher: activity log + settings ----------
@app.route("/teacher/activity")
@login_required("teacher")
def t_activity():
    rows = sorted(find("activity"), key=lambda x: x["at"], reverse=True)[:300]
    for r in rows:
        r["when"] = datetime.fromisoformat(r["at"]).strftime("%b %d, %Y %I:%M %p")
    return render_template("t_activity.html", rows=rows)

@app.route("/teacher/settings", methods=["GET", "POST"])
@login_required("teacher")
def t_settings():
    if request.method == "POST":
        try:
            secs = int(request.form.get("rotate_seconds", "120"))
        except ValueError:
            secs = 120
        try:
            pass_pct = max(1, min(100, int(request.form.get("pass_percent", "75"))))
        except ValueError:
            pass_pct = 75
        db.collection("settings").document("main").set({
            "rotate_qr": bool(request.form.get("rotate_qr")),
            "rotate_seconds": secs if secs in (60, 120, 180, 300) else 120,
            "email_on_present": bool(request.form.get("email_on_present")),
            "email_quiz_student": bool(request.form.get("email_quiz_student")),
            "email_quiz_teacher": bool(request.form.get("email_quiz_teacher")),
            "pass_percent": pass_pct})
        log("Changed settings", "QR / notification settings")
        flash("Settings saved.")
        return redirect(url_for("t_settings"))
    return render_template("t_settings.html", cfg=settings())


# ---------- student ----------
@app.route("/student")
@login_required("student")
def s_home():
    st = get("students", session["uid"])
    ann = sorted([x for x in find("announcements") if visible_to(st, x)], key=lambda x: x["created_at"], reverse=True)[:3]
    done = {x["activity_id"] for x in find("submissions", student_id=st["id"])}
    todo_list = sorted([x for x in find("activities") if visible_to(st, x) and x["id"] not in done],
                       key=lambda x: x.get("due_date") or "9999")[:5]
    return render_template("s_home.html", s=st, sm=summarize(find("attendance", student_id=st["id"])),
                           ann=ann, todo_list=todo_list, today=today_str())

@app.route("/student/qr.png")
@login_required("student")
def s_qr():
    st, cfg = get("students", session["uid"]), settings()
    buf = io.BytesIO()
    qrcode.make(qr_payload(st), box_size=10, border=2).save(buf, "PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Rotating"] = "1" if cfg["rotate_qr"] else "0"
    resp.headers["X-Remaining"] = str(qr_remaining(cfg))
    return resp

@app.route("/student/attendance")
@login_required("student")
def s_att():
    month = request.args.get("month", "")
    st = get("students", session["uid"])
    acts = find("activities")
    subs = {(x["activity_id"], x["student_id"]): x for x in find("submissions", student_id=st["id"])}
    rows = [r for r in find("attendance", student_id=st["id"]) if not month or r["scan_date"].startswith(month)]
    rows.sort(key=lambda r: (r["scan_date"], r.get("scan_time") or ""), reverse=True)
    for r in rows:
        r.update(time12=t12(r.get("scan_time")), answer=ans_text(r), status=r.get("status", "present"),
                 subject=r.get("session_label", "-"), activity=activity_cell(st, r["scan_date"], acts, subs))
    return render_template("s_att.html", rows=rows, sm=summarize(rows), month=month)

@app.route("/student/notifications")
@login_required("student")
def s_notifs():
    rows = sorted(find("notifications", student_id=session["uid"]), key=lambda n: n["created_at"], reverse=True)[:100]
    for n in rows:
        n["when"] = datetime.fromisoformat(n["created_at"]).strftime("%b %d, %I:%M %p")
    shown = [dict(n) for n in rows]
    for n in rows:
        if not n["read"]:
            db.collection("notifications").document(n["id"]).update({"read": True})
    return render_template("s_notifs.html", rows=shown)

@app.route("/student/profile", methods=["GET", "POST"])
@login_required("student")
def s_profile():
    st = get("students", session["uid"])
    if request.method == "POST":
        f = request.form
        if f.get("action") == "password":
            if not check_password_hash(st["password_hash"], f.get("current", "")):
                flash("Your current password is wrong.")
            elif len(f.get("new", "")) < 8:
                flash("New password must be at least 8 characters.")
            elif f["new"] != f.get("confirm"):
                flash("New passwords do not match.")
            else:
                db.collection("students").document(st["id"]).update({"password_hash": generate_password_hash(f["new"])})
                flash("Password changed.")
        else:
            email = f.get("email", "").strip().lower()
            if not (f.get("last_name", "").strip() and f.get("first_name", "").strip() and f.get("mi", "").strip()):
                flash("Please fill in your name.")
            elif not email.endswith("@gmail.com"):
                flash("Use a valid @gmail.com address.")
            elif any(x["id"] != st["id"] for x in find("students", email=email)):
                flash("That email is already used by another account.")
            else:
                data = {"last_name": f["last_name"].strip(), "first_name": f["first_name"].strip(),
                        "middle_initial": f["mi"].strip().upper()[:1], "email": email}
                db.collection("students").document(st["id"]).update(data)
                session["name"] = full_name({**st, **data})
                flash("Profile updated.")
        return redirect(url_for("s_profile"))
    return render_template("s_profile.html", s=st)


# ---------- files stored in Firestore (split in chunks) ----------
def clean_name(name):
    name = os.path.basename((name or "").replace("\\", "/"))
    return re.sub(r'[\x00-\x1f"<>|:*?]', "", name).strip()[:120]

def save_upload(file, class_key="ALL", owner_student=None):
    """Store an uploaded file in Firestore. Returns (file_info, error_message)."""
    name = clean_name(file.filename)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED_EXT:
        return None, "That file type is not allowed. Use PDF, Word, PowerPoint, Excel, TXT, CSV, images, ZIP, MP3 or MP4."
    data = file.read()
    if not data:
        return None, "The file is empty."
    if len(data) > MAX_FILE:
        return None, "The file is too big (maximum 10 MB)."
    parts = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]
    ref = db.collection("files").document()
    for i, part in enumerate(parts):
        db.collection("file_chunks").document(f"{ref.id}_{i}").set({"file_id": ref.id, "i": i, "data": part})
    meta = {"name": name, "ext": ext, "size": len(data), "chunks": len(parts),
            "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "class_key": class_key, "owner_student": owner_student, "created_at": now().isoformat()}
    ref.set(meta)
    return {**meta, "id": ref.id}, None

def delete_file(fid):
    f = get("files", fid)
    if not f:
        return
    for i in range(f["chunks"]):
        db.collection("file_chunks").document(f"{fid}_{i}").delete()
    db.collection("files").document(fid).delete()

@app.route("/files/<fid>")
def get_file(fid):
    role = session.get("role")
    if role not in ("teacher", "student"):
        return redirect(url_for("login"))
    f = get("files", fid)
    if not f:
        abort(404)
    if role == "student":
        st = get("students", session["uid"])
        if f.get("owner_student"):
            allowed = f["owner_student"] == st["id"]            # a student's own submission
        else:
            allowed = f.get("class_key", "ALL") in ("ALL", class_key_of(st))
        if not allowed:
            abort(403)
    data = b"".join(db.collection("file_chunks").document(f"{fid}_{i}").get().to_dict()["data"]
                    for i in range(f["chunks"]))
    inline = request.args.get("view") == "1" and f["ext"] in INLINE_OK
    resp = send_file(io.BytesIO(data), mimetype=f["mime"], as_attachment=not inline, download_name=f["name"])
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

@app.errorhandler(413)
def too_big(_):
    flash("That file is too big (maximum 10 MB).")
    return redirect(request.referrer or url_for("index"))

# ---------- helpers for materials / activities ----------
def visible_to(st, item):
    return item.get("class_key", "ALL") in ("ALL", class_key_of(st))

def notify_class(class_key, message):
    for st in find("students", status="approved"):
        if class_key == "ALL" or class_key_of(st) == class_key:
            notify(st["id"], message)

def num(x):
    return "" if x is None else "%g" % x

def filesize(n):
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, round(n / 1024))} KB"

app.jinja_env.filters.update(num=num, filesize=filesize, book_color=icons.book_color)
app.jinja_env.globals.update(icon=icons.icon, icon_sprite=icons.sprite)
app.jinja_env.globals.update(inline_ext=INLINE_OK)

def activity_cell(st, date, acts, subs):
    """Text for the Activity column: the activity(ies) given on that day and the student's score."""
    ck, parts = class_key_of(st), []
    for a in acts:
        if a["date"] == date and a["class_key"] in ("ALL", ck):
            sub = subs.get((a["id"], st["id"]))
            if sub and sub.get("score") is not None:
                parts.append(f"{a['title']} ({num(sub['score'])}/{num(a['max_score'])})")
            elif sub and sub.get("submitted_at"):
                parts.append(f"{a['title']} (submitted)")
            else:
                parts.append(f"{a['title']} (no score)")
    return "; ".join(parts) or "-"

def activity_totals(st, acts, subs):
    ck, earned, possible = class_key_of(st), 0, 0
    for a in acts:
        sub = subs.get((a["id"], st["id"]))
        if a["class_key"] in ("ALL", ck) and sub and sub.get("score") is not None:
            earned += sub["score"]
            possible += a["max_score"]
    return {"earned": earned, "possible": possible, "pct": round(earned * 100 / possible) if possible else None}


# ---------- teacher: learning materials ----------
@app.route("/teacher/materials", methods=["GET", "POST"])
@login_required("teacher")
def t_materials():
    if request.method == "POST":
        f, file = request.form, request.files.get("file")
        title = f.get("label", "").strip()
        key, lbl = class_from_form(f)
        if not title or not key or not file or not file.filename:
            flash("Please type a label, choose who can see it, and choose a file.")
        else:
            meta, err = save_upload(file, key)
            if err:
                flash(err)
            else:
                db.collection("materials").add({"label": title, "description": f.get("description", "").strip(),
                    "file_id": meta["id"], "file_name": meta["name"], "ext": meta["ext"], "size": meta["size"],
                    "class_key": key, "class_label": "All students" if key == "ALL" else lbl,
                    "uploaded_at": now().isoformat()})
                notify_class(key, f"New learning material: {title}")
                log("Uploaded material", title)
                flash(f"Uploaded: {title}")
        return redirect(url_for("t_materials"))
    q = request.args.get("q", "").strip()
    rows = [m for m in find("materials")
            if not q or q.lower() in f"{m['label']} {m['file_name']} {m.get('description', '')}".lower()]
    rows.sort(key=lambda m: m["uploaded_at"], reverse=True)
    return render_template("t_materials.html", rows=rows, q=q,
                           quizzes=sorted(find("quizzes"), key=lambda x: x["created_at"], reverse=True),
                           week_ago=(now().date() - timedelta(days=7)).isoformat())

@app.post("/teacher/materials/<mid>/delete")
@login_required("teacher")
def t_material_delete(mid):
    m = get("materials", mid)
    if m:
        delete_file(m.get("file_id"))
        db.collection("materials").document(mid).delete()
        log("Deleted material", m["label"])
        flash("Material deleted.")
    return redirect(url_for("t_materials"))

@app.route("/student/materials")
@login_required("student")
def s_materials():
    st, q = get("students", session["uid"]), request.args.get("q", "").strip()
    rows = [m for m in find("materials") if visible_to(st, m)
            and (not q or q.lower() in f"{m['label']} {m['file_name']} {m.get('description', '')}".lower())]
    rows.sort(key=lambda m: m["uploaded_at"], reverse=True)
    return render_template("s_materials.html", rows=rows, q=q,
                           week_ago=(now().date() - timedelta(days=7)).isoformat())


# ---------- teacher: activities / to-do list + checking ----------
@app.route("/teacher/activities", methods=["GET", "POST"])
@login_required("teacher")
def t_activities():
    if request.method == "POST":
        f = request.form
        key, lbl = class_from_form(f)
        title = f.get("title", "").strip()
        due = f.get("due_date", "").strip()
        try:
            date = f.get("date") or today_str()
            datetime.strptime(date, "%Y-%m-%d")
            if due:
                datetime.strptime(due, "%Y-%m-%d")
            points = float(f.get("max_score", "10"))
        except ValueError:
            key = None
        if not key or not title or not 0 < points <= 1000:
            flash("Please type a title, choose the class, a valid date and the total points.")
            return redirect(url_for("t_activities"))
        file_id = file_name = None
        file = request.files.get("file")
        if file and file.filename:
            meta, err = save_upload(file, key)
            if err:
                flash(err)
                return redirect(url_for("t_activities"))
            file_id, file_name = meta["id"], meta["name"]
        db.collection("activities").add({"title": title, "description": f.get("description", "").strip(),
            "class_key": key, "class_label": "All students" if key == "ALL" else lbl, "date": date,
            "due_date": due, "max_score": points, "file_id": file_id, "file_name": file_name,
            "created_at": now().isoformat()})
        notify_class(key, f"New activity: {title}" + (f" (due {due})" if due else ""))
        log("Posted activity", f"{title} - {lbl}")
        flash(f"Activity posted: {title}")
        return redirect(url_for("t_activities"))
    studs, subs = find("students", status="approved"), find("submissions")
    rows = sorted(find("activities"), key=lambda a: (a["date"], a["created_at"]), reverse=True)
    for a in rows:
        mine = [x for x in subs if x["activity_id"] == a["id"]]
        a["total"] = sum(1 for s in studs if a["class_key"] == "ALL" or class_key_of(s) == a["class_key"])
        a["submitted"] = sum(1 for x in mine if x.get("submitted_at"))
        a["checked"] = sum(1 for x in mine if x.get("score") is not None)
    return render_template("t_activities.html", rows=rows, today=today_str())

@app.route("/teacher/activities/<aid>", methods=["GET", "POST"])
@login_required("teacher")
def t_activity_check(aid):
    a = get("activities", aid)
    if not a:
        abort(404)
    studs = sorted([s for s in find("students", status="approved")
                    if a["class_key"] == "ALL" or class_key_of(s) == a["class_key"]],
                   key=lambda s: s["last_name"].lower())
    subs = {x["student_id"]: x for x in find("submissions", activity_id=aid)}
    if request.method == "POST":
        f, changed = request.form, 0
        for s in studs:
            sub = subs.get(s["id"])
            if f.get("action") == "full":                        # full marks for everyone who handed in work
                if not (sub and sub.get("submitted_at") and sub.get("score") is None):
                    continue
                new = a["max_score"]
            else:
                raw = f.get(f"score_{s['id']}", "").strip()
                if raw == "":
                    continue
                try:
                    new = max(0, min(a["max_score"], round(float(raw), 2)))
                except ValueError:
                    continue
            if sub and sub.get("score") == new:
                continue
            ref = db.collection("submissions").document(f"{aid}_{s['id']}")
            data = {"score": new, "checked_at": now().isoformat()}
            if sub:
                ref.update(data)
            else:                                                # teacher checked paper work with no online submission
                ref.set({"activity_id": aid, "student_id": s["id"], "text": "", "files": [],
                         "submitted_at": None, **data})
            notify(s["id"], f"Your activity \"{a['title']}\" was checked: {num(new)}/{num(a['max_score'])}.")
            changed += 1
        log("Checked activity", f"{a['title']} - {changed} score(s)")
        flash(f"{changed} score(s) saved. They now show in Records and on each student's page.")
        return redirect(url_for("t_activity_check", aid=aid))
    for s in studs:
        s["sub"] = subs.get(s["id"])
    return render_template("t_activity_check.html", a=a, studs=studs)

def delete_activity(a):
    aid = a["id"]
    for sub in find("submissions", activity_id=aid):
        for fl in sub.get("files", []):
            delete_file(fl["id"])
        db.collection("submissions").document(sub["id"]).delete()
    for att in find("quiz_attempts", activity_id=aid):
        db.collection("quiz_attempts").document(att["id"]).delete()
    delete_file(a.get("file_id"))
    db.collection("activities").document(aid).delete()
    if a.get("quiz_id") and get("quizzes", a["quiz_id"]):          # the quiz goes back to a draft
        db.collection("quizzes").document(a["quiz_id"]).update({"status": "draft", "activity_id": None})

@app.post("/teacher/activities/<aid>/delete")
@login_required("teacher")
def t_activity_delete(aid):
    a = get("activities", aid)
    if a:
        delete_activity(a)
        log("Deleted activity", a["title"])
        flash("Activity and its scores were deleted.")
    return redirect(url_for("t_activities"))

@app.route("/student/activities")
@login_required("student")
def s_activities():
    st, today = get("students", session["uid"]), today_str()
    subs = {x["activity_id"]: x for x in find("submissions", student_id=st["id"])}
    rows = []
    for a in find("activities"):
        if not visible_to(st, a):
            continue
        sub = subs.get(a["id"])
        if sub and sub.get("score") is not None:
            a["status"] = "checked"
        elif sub and sub.get("submitted_at"):
            a["status"] = "submitted"
        elif a.get("due_date") and today > a["due_date"]:
            a["status"] = "overdue"
        else:
            a["status"] = "todo"
        a["sub"] = sub
        rows.append(a)
    rank = {"todo": 0, "overdue": 1, "submitted": 2, "checked": 3}
    rows.sort(key=lambda a: (rank[a["status"]], a.get("due_date") or "9999", a["date"]))
    return render_template("s_activities.html", rows=rows)

@app.route("/student/activities/<aid>", methods=["GET", "POST"])
@login_required("student")
def s_activity(aid):
    st, a = get("students", session["uid"]), get("activities", aid)
    if not a or not visible_to(st, a):
        abort(404)
    if a.get("kind") == "quiz":
        return redirect(url_for("s_quiz", aid=aid))
    sub = get("submissions", f"{aid}_{st['id']}")
    if request.method == "POST":
        if sub and sub.get("score") is not None:
            flash("This activity was already checked, so it can no longer be changed.")
            return redirect(url_for("s_activity", aid=aid))
        text = request.form.get("text", "").strip()
        uploads = [x for x in request.files.getlist("file") if x and x.filename]
        remove = set(request.form.getlist("remove"))
        old = (sub or {}).get("files", [])
        kept = [x for x in old if x["id"] not in remove]
        if not text and not uploads and not kept:
            flash("Write your answer or add a photo / file first.")
            return redirect(url_for("s_activity", aid=aid))
        if len(kept) + len(uploads) > 10:
            flash("You can attach up to 10 photos / files.")
            return redirect(url_for("s_activity", aid=aid))
        added = []
        for x in uploads:
            meta, err = save_upload(x, "ALL", owner_student=st["id"])
            if err:
                for n in added:
                    delete_file(n["id"])
                flash(err)
                return redirect(url_for("s_activity", aid=aid))
            added.append({"id": meta["id"], "name": meta["name"], "ext": meta["ext"]})
        for x in old:
            if x["id"] in remove:
                delete_file(x["id"])
        db.collection("submissions").document(f"{aid}_{st['id']}").set({
            "activity_id": aid, "student_id": st["id"], "text": text, "files": kept + added,
            "submitted_at": now().isoformat(), "score": None, "checked_at": None})
        flash("Your work was submitted. Your teacher will check it.")
        return redirect(url_for("s_activity", aid=aid))
    late = bool(sub and sub.get("submitted_at") and a.get("due_date") and sub["submitted_at"][:10] > a["due_date"])
    return render_template("s_activity.html", a=a, sub=sub, late=late,
                           locked=bool(sub and sub.get("score") is not None))


# ---------- announcements ----------
@app.route("/teacher/announcements", methods=["GET", "POST"])
@login_required("teacher")
def t_announce():
    if request.method == "POST":
        f = request.form
        title, msg = f.get("title", "").strip(), f.get("message", "").strip()
        key, lbl = class_from_form(f)
        if not title or not msg or not key:
            flash("Please type a title, a message and choose who it is for.")
        else:
            db.collection("announcements").add({"title": title, "message": msg, "class_key": key,
                "class_label": "All students" if key == "ALL" else lbl, "created_at": now().isoformat()})
            notify_class(key, f"New announcement: {title}")
            log("Posted announcement", title)
            flash("Announcement posted. Students can see it now.")
        return redirect(url_for("t_announce"))
    rows = sorted(find("announcements"), key=lambda a: a["created_at"], reverse=True)
    return render_template("t_announce.html", rows=rows)

@app.post("/teacher/announcements/<nid>/delete")
@login_required("teacher")
def t_announce_delete(nid):
    if get("announcements", nid):
        db.collection("announcements").document(nid).delete()
        log("Deleted announcement", nid)
    return redirect(url_for("t_announce"))

@app.route("/student/announcements")
@login_required("student")
def s_announce():
    st = get("students", session["uid"])
    rows = sorted([a for a in find("announcements") if visible_to(st, a)], key=lambda a: a["created_at"], reverse=True)
    return render_template("s_announce.html", rows=rows)


# ================= QUIZ GENERATOR (made from learning materials) =================
QUIZ_TYPES = [("mc", "Multiple choice", "Choose the letter of the correct answer."),
              ("tf", "True or False", "Write TRUE if the statement is correct, FALSE if it is not."),
              ("id", "Identification", "Write the correct term on the blank."),
              ("fib", "Fill in the blank", "Write the missing word on the blank."),
              ("match", "Matching type", "Match each term in Column A with its description in Column B.")]
TYPE_NAMES = {k: v for k, v, _ in QUIZ_TYPES}
QUIZ_EXT = {"pdf", "docx", "pptx", "txt"}
MAX_QUIZ_ITEMS = 50
LEVEL_MIN = {"easy": 5, "medium": 6, "hard": 8}
STOP = set("""about above after again against almost along already also although always among another around because
been before being below between both cannot could does doing down during each either else ever every from further have
having here however into itself just keep known later least less like made make many might more most much must never next
once only other over perhaps same several shall should since some such than that their theirs them then there these they
this those though through thus under until upon very were what when where whether which while whom whose will with within
without would your it he she who the and for are was has had but not you all can any one our out""".split())
DEF_RE = re.compile(r"^(?:(?:A|An|The)\s+)?(?P<term>[A-Za-z][A-Za-z0-9 \-/']{2,40}?)\s+"
                    r"(?:is|are|refers to|means|is defined as|can be defined as)\s+(?P<defn>.{25,})$")
app.jinja_env.globals.update(type_names=TYPE_NAMES, quiz_types=QUIZ_TYPES, quiz_ext=QUIZ_EXT)

class QuizGen:
    """Rule-based quiz maker. It reads the material text and builds items - always review before publishing."""
    def __init__(self, text, level="medium"):
        text = re.sub(r"\s+", " ", text)
        seen, self.sents = set(), []
        for x in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text):
            x = x.strip()
            if 40 <= len(x) <= 260 and x.count(" ") >= 6 and x.lower() not in seen:
                seen.add(x.lower())
                self.sents.append(x)
        self.level, self.min = level, LEVEL_MIN.get(level, 6)
        self.vocab = sorted({w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", text)
                             if len(w) >= self.min - 1 and w.lower() not in STOP})
        self.use, self.used, self.def_used = Counter(), set(), set()
        self.defs = self._definitions()

    def _definitions(self):
        out, seen = [], set()
        for x in self.sents:
            m = DEF_RE.match(x.rstrip(".!?"))
            if not m:
                continue
            term, defn = m.group("term").strip(), m.group("defn").strip()
            first = term.split()[0].lower()
            if len(term.split()) > 4 or first in STOP or term.lower() in seen:
                continue
            seen.add(term.lower())
            out.append((term, defn[:200], x))
        random.shuffle(out)
        return out

    def _order(self):
        return sorted(self.sents, key=lambda x: (self.use[x], random.random()))

    def _pick(self, sent):
        ws = [w for w in dict.fromkeys(re.findall(r"[A-Za-z][A-Za-z\-]+", sent))
              if len(w) >= self.min and w.lower() not in STOP and (sent, w) not in self.used]
        random.shuffle(ws)
        ws.sort(key=len, reverse=True)                      # long words are usually the key terms
        return random.choice(ws[:3]) if ws else None

    @staticmethod
    def _blank(sent, w):
        return re.sub(r"\b" + re.escape(w) + r"\b", "_____", sent, count=1)

    @staticmethod
    def _like(word, ans):
        return word.upper() if ans.isupper() and len(ans) > 1 else word.capitalize() if ans[:1].isupper() else word.lower()

    def _distractors(self, ans, k=3):
        pool = [w for w in self.vocab if w != ans.lower()]
        if self.level == "easy":
            random.shuffle(pool)
        else:                                                # similar length = more convincing wrong answers
            pool.sort(key=lambda w: (abs(len(w) - len(ans)), random.random()))
            pool = pool[:14]
            random.shuffle(pool)
        return [self._like(w, ans) for w in pool[:k]]

    def mc(self, n):
        out = []
        for _ in range(4):
            for x in self._order():
                if len(out) >= n:
                    return out
                w = self._pick(x)
                d = self._distractors(w) if w else []
                if len(d) < 3:
                    continue
                self.used.add((x, w)); self.use[x] += 1
                ch = [w] + d
                random.shuffle(ch)
                out.append({"type": "mc", "q": "Choose the word that best completes the sentence: " + self._blank(x, w),
                            "choices": ch, "answer": w})
        return out[:n]

    def fib(self, n):
        out = []
        for _ in range(4):
            for x in self._order():
                if len(out) >= n:
                    return out
                w = self._pick(x)
                if not w:
                    continue
                self.used.add((x, w)); self.use[x] += 1
                out.append({"type": "fib", "q": "Fill in the blank: " + self._blank(x, w), "answer": w})
        return out[:n]

    def tf(self, n):
        flags = [True] * ((n + 1) // 2) + [False] * (n // 2)
        random.shuffle(flags)
        out = []
        for _ in range(4):
            for x in self._order():
                if len(out) >= n:
                    return out
                if flags[len(out)]:
                    self.use[x] += 1
                    out.append({"type": "tf", "q": x, "answer": "True", "note": ""})
                    continue
                w = self._pick(x)
                d = self._distractors(w, 1) if w else []
                if not d:
                    continue
                self.used.add((x, w)); self.use[x] += 1
                out.append({"type": "tf", "q": re.sub(r"\b" + re.escape(w) + r"\b", d[0], x, count=1),
                            "answer": "False", "note": f"Original word: {w}"})
        return out[:n]

    def match(self, n):
        pairs = [(t, d) for t, d, _ in self.defs[:n]]
        if len(pairs) < 3:
            return []
        groups = [pairs[i:i + 10] for i in range(0, len(pairs), 10)]
        if len(groups) > 1 and len(groups[-1]) < 3:
            groups[-2] += groups.pop()
        self.def_used = {t for t, _ in pairs}
        return [{"type": "match", "q": "Match each term in Column A with its description in Column B.",
                 "pairs": [{"term": t, "defn": d} for t, d in g]} for g in groups]

    def idq(self, n):
        out = []
        for term, defn, x in self.defs:
            if len(out) >= n:
                break
            if term in self.def_used:
                continue
            self.use[x] += 1
            out.append({"type": "id", "q": f"What term is described? \"{defn}\"", "answer": term})
        for _ in range(4):
            if len(out) >= n:
                break
            for x in self._order():
                if len(out) >= n:
                    break
                w = self._pick(x)
                if not w:
                    continue
                self.used.add((x, w)); self.use[x] += 1
                out.append({"type": "id", "q": "Identify the missing term: " + self._blank(x, w), "answer": w})
        return out[:n]

def item_count(it):
    return len(it["pairs"]) if it["type"] == "match" else 1

def quiz_total(items):
    return round(sum(it["points"] * item_count(it) for it in items), 2)

def quiz_numbering(items):
    n = 1
    for it in items:
        it["no"] = n
        n += item_count(it)
    return n - 1

def build_quiz(text, counts, points, level):
    g = QuizGen(text, level)
    if len(g.sents) < 5:
        return [], []
    built = {}
    for t, fn in (("match", g.match), ("id", g.idq), ("mc", g.mc), ("fib", g.fib), ("tf", g.tf)):   # matching first: it uses the definitions
        if counts.get(t):
            built[t] = fn(counts[t])
    items, warns = [], []
    for t, name, _ in QUIZ_TYPES:
        got = 0
        for it in built.get(t, []):
            it["points"] = points.get(t, 1)
            items.append(it)
            got += item_count(it)
        if counts.get(t) and got < counts[t]:
            warns.append(f"{name}: only {got} of {counts[t]} items could be made from this file.")
    return items, warns

def material_text(m):
    f = get("files", m.get("file_id"))
    if not f:
        return ""
    data = b"".join(db.collection("file_chunks").document(f"{f['id']}_{i}").get().to_dict()["data"]
                    for i in range(f["chunks"]))
    return extract_text_bytes(f["name"], data)

def norm(x):
    x = re.sub(r"[^\w\s]", " ", (x or "").lower())
    return " ".join(re.sub(r"\b(the|a|an)\b", " ", x).split())

def answer_matches(kind, given, correct):
    if kind in ("mc", "tf"):
        return given == correct
    a, b = norm(given), norm(correct)
    if not a:
        return False
    return a == b or (len(b) >= 5 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.88)   # forgives small typos

def grade_quiz(quiz, form):
    total, detail = 0, []
    for i, it in enumerate(quiz["items"]):
        if it["type"] == "match":
            rows, got = [], 0
            for k, pr in enumerate(it["pairs"]):
                given = form.get(f"ans_{i}_{k}", "")
                ok = given == pr["defn"]
                got += it["points"] if ok else 0
                rows.append({"term": pr["term"], "given": given, "correct": pr["defn"], "ok": ok})
            detail.append({"i": i, "type": "match", "q": it["q"], "rows": rows, "earned": got,
                           "max": it["points"] * len(rows)})
            total += got
        else:
            given = form.get(f"ans_{i}", "")
            ok = answer_matches(it["type"], given, it["answer"])
            got = it["points"] if ok else 0
            detail.append({"i": i, "type": it["type"], "q": it["q"], "given": given, "correct": it["answer"],
                           "ok": ok, "earned": got, "max": it["points"]})
            total += got
    return round(total, 2), detail

def student_view(quiz, aid, sid):
    """The quiz as a student sees it - never includes the answers."""
    rnd = random.Random(f"{aid}:{sid}")
    qs = []
    for idx, it in enumerate(quiz["items"]):
        v = {"idx": idx, "type": it["type"], "q": it["q"], "points": it["points"]}
        if it["type"] == "mc":
            v["choices"] = list(it["choices"])
            if quiz.get("shuffle"):
                rnd.shuffle(v["choices"])
        elif it["type"] == "match":
            v["terms"] = [pr["term"] for pr in it["pairs"]]
            v["defs"] = [pr["defn"] for pr in it["pairs"]]
            rnd.shuffle(v["defs"])
        qs.append(v)
    if quiz.get("shuffle"):                                   # shuffle inside each type, keep the sections
        out = []
        for t, _, _ in QUIZ_TYPES:
            grp = [q for q in qs if q["type"] == t]
            rnd.shuffle(grp)
            out += grp
        qs = out
    n = 1
    for q in qs:
        q["pos"] = n
        n += len(q["terms"]) if q["type"] == "match" else 1
    return qs

def quiz_stats(quiz):
    atts = [x for x in find("quiz_attempts", quiz_id=quiz["id"]) if x.get("submitted_at")]
    if not atts:
        return None
    scores = [x["score"] for x in atts]
    rates = []
    for i, it in enumerate(quiz["items"]):
        mx = it["points"] * item_count(it) * len(atts)
        got = sum(d["earned"] for x in atts for d in x.get("detail", []) if d["i"] == i)
        rates.append(round(got * 100 / mx) if mx else 0)
    return {"n": len(atts), "avg": round(sum(scores) / len(scores), 2), "high": max(scores), "low": min(scores), "items": rates}

def quiz_pdf(quiz, qid, key=False):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    ss = getSampleStyleSheet()
    P = lambda t, st="BodyText": Paragraph(escape(str(t)).replace("\n", "<br/>"), ss[st])
    quiz_numbering(quiz["items"])
    els = [Paragraph(escape(quiz["title"]) + (" - ANSWER KEY" if key else ""), ss["Title"])]
    if not key:
        els += [P(f"Name: ______________________________   Date: ______________   Score: ______ / {num(quiz['total'])}"), Spacer(1, 10)]
    letters, roman = "ABCDEFGHIJKLMNOP", ["I", "II", "III", "IV", "V"]
    for ti, (t, name, instr) in enumerate([x for x in QUIZ_TYPES if any(i["type"] == x[0] for i in quiz["items"])]):
        group = [(i, it) for i, it in enumerate(quiz["items"]) if it["type"] == t]
        els += [Paragraph(f"{roman[ti]}. {escape(name)}", ss["Heading3"]),
                P(f"{instr} ({num(group[0][1]['points'])} point(s) each{' pair' if t == 'match' else ''})"), Spacer(1, 4)]
        for i, it in group:
            no = it["no"]
            if t == "match":
                order = list(range(len(it["pairs"])))
                random.Random(f"{qid}:{i}").shuffle(order)
                if key:
                    for k, pr in enumerate(it["pairs"]):
                        els.append(P(f"{no + k}. {letters[order.index(k)]}  ({pr['term']})"))
                else:
                    rows = [[P(f"{no + k}. ______ {pr['term']}"), P(f"{letters[k]}. {it['pairs'][order[k]]['defn']}")]
                            for k, pr in enumerate(it["pairs"])]
                    tb = Table(rows, colWidths=[200, 330])
                    tb.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
                    els.append(tb)
            elif key:
                ans = f"{letters[it['choices'].index(it['answer'])]}. {it['answer']}" if t == "mc" else it["answer"]
                els.append(P(f"{no}. {ans}" + (f"   ({it['note']})" if it.get("note") else "")))
            elif t == "mc":
                els.append(P(f"{no}. {it['q']}"))
                els += [P(f"      {letters[j]}. {c}") for j, c in enumerate(it["choices"])]
            elif t == "tf":
                els.append(P(f"{no}. ______ {it['q']}"))
            else:
                els += [P(f"{no}. {it['q']}"), P("      Answer: ______________________________")]
            els.append(Spacer(1, 5))
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=40, rightMargin=40, topMargin=40, bottomMargin=40).build(els)
    buf.seek(0)
    return buf


# ---------- teacher: generate / edit / publish quizzes ----------
@app.route("/teacher/materials/<mid>/quiz", methods=["GET", "POST"])
@login_required("teacher")
def t_quiz_new(mid):
    m = get("materials", mid)
    if not m:
        abort(404)
    if m["ext"] not in QUIZ_EXT:
        flash("A quiz can be made from PDF, Word, PowerPoint and TXT files only.")
        return redirect(url_for("t_materials"))
    if request.method == "POST":
        f, counts, points = request.form, {}, {}
        try:
            for t, _, _ in QUIZ_TYPES:
                if f.get(f"use_{t}"):
                    n, pt = int(f.get(f"n_{t}", "0")), float(f.get(f"p_{t}", "1"))
                    if not 1 <= n <= MAX_QUIZ_ITEMS or not 0 < pt <= 100:
                        raise ValueError
                    counts[t], points[t] = n, pt
        except ValueError:
            counts = {}
        if not counts or sum(counts.values()) > MAX_QUIZ_ITEMS:
            flash(f"Tick at least one quiz type. A quiz can have up to {MAX_QUIZ_ITEMS} items in total.")
        else:
            level = f.get("level") if f.get("level") in LEVEL_MIN else "medium"
            items, warns = build_quiz(material_text(m), counts, points, level)
            if not items:
                flash("This file does not have enough readable text to make a quiz. (Scanned PDFs and pictures cannot be read.)")
            else:
                ref = db.collection("quizzes").document()
                ref.set({"title": f.get("title", "").strip() or f"Quiz - {m['label']}", "material_id": mid,
                         "material_label": m["label"], "items": items, "total": quiz_total(items),
                         "n_items": sum(item_count(i) for i in items), "level": level,
                         "config": {"counts": counts, "points": points}, "status": "draft", "activity_id": None,
                         "shuffle": True, "created_at": now().isoformat()})
                log("Generated quiz", f"{m['label']} - {len(items)} item group(s)")
                if warns:
                    flash(" ".join(warns))
                flash("Quiz generated. Review it, edit anything you like, then publish it or download the PDF.")
                return redirect(url_for("t_quiz", qid=ref.id))
    return render_template("t_quiz_new.html", m=m, max_items=MAX_QUIZ_ITEMS)

@app.route("/teacher/quizzes/<qid>")
@login_required("teacher")
def t_quiz(qid):
    quiz = get("quizzes", qid)
    if not quiz:
        abort(404)
    quiz_numbering(quiz["items"])
    return render_template("t_quiz.html", quiz=quiz, today=today_str(),
                           act=get("activities", quiz.get("activity_id")),
                           stats=quiz_stats(quiz) if quiz.get("activity_id") else None)

@app.post("/teacher/quizzes/<qid>/save")
@login_required("teacher")
def t_quiz_save(qid):
    quiz = get("quizzes", qid)
    if not quiz:
        abort(404)
    f, items = request.form, []
    for i, it in enumerate(quiz["items"]):
        if f.get(f"del_{i}"):
            continue
        try:
            pts = float(f.get(f"p_{i}", it["points"]))
            if not 0 <= pts <= 100:
                raise ValueError
        except ValueError:
            pts = it["points"]
        t, q, new = it["type"], f.get(f"q_{i}", "").strip(), {"type": it["type"], "points": pts}
        bad = None
        if t == "match":
            pairs = [{"term": f.get(f"term_{i}_{k}", "").strip(), "defn": f.get(f"defn_{i}_{k}", "").strip()}
                     for k in range(len(it["pairs"]))]
            pairs = [x for x in pairs if x["term"] and x["defn"]]
            if len(pairs) < 2:
                bad = "A matching group needs at least 2 complete pairs."
            new.update(q=q or it["q"], pairs=pairs)
        elif not q:
            bad = "Every question needs its text."
        elif t == "mc":
            raw = [f.get(f"c_{i}_{j}", "").strip() for j in range(len(it["choices"]))]
            choices = [c for c in raw if c]
            try:
                ans = raw[int(f.get(f"a_{i}", "0"))]
            except (ValueError, IndexError):
                ans = ""
            if len(choices) < 2 or len(set(choices)) != len(choices) or not ans:
                bad = "Multiple choice items need 2+ different choices and a correct one that is not empty."
            new.update(q=q, choices=choices, answer=ans)
        elif t == "tf":
            ans = f.get(f"a_{i}", "")
            if ans not in ("True", "False"):
                bad = "True/False answers must be True or False."
            new.update(q=q, answer=ans, note=it.get("note", ""))
        else:
            ans = f.get(f"a_{i}", "").strip()
            if not ans:
                bad = "Identification and fill-in-the-blank items need an answer."
            new.update(q=q, answer=ans)
        if bad:
            flash(f"Item {i + 1}: {bad} Nothing was saved.")
            return redirect(url_for("t_quiz", qid=qid))
        items.append(new)
    if not items:
        flash("A quiz needs at least one item. Nothing was saved.")
        return redirect(url_for("t_quiz", qid=qid))
    title = f.get("title", "").strip() or quiz["title"]
    db.collection("quizzes").document(qid).update({"items": items, "total": quiz_total(items), "title": title,
                                                    "n_items": sum(item_count(i) for i in items)})
    if quiz.get("activity_id") and get("activities", quiz["activity_id"]):
        db.collection("activities").document(quiz["activity_id"]).update({"max_score": quiz_total(items), "title": title})
    flash("Quiz saved.")
    return redirect(url_for("t_quiz", qid=qid))

@app.post("/teacher/quizzes/<qid>/regenerate")
@login_required("teacher")
def t_quiz_regen(qid):
    quiz = get("quizzes", qid)
    if not quiz:
        abort(404)
    m = get("materials", quiz.get("material_id"))
    if quiz.get("activity_id"):
        flash("This quiz is already published. Delete the published activity first to regenerate.")
    elif not m:
        flash("The learning material was deleted, so the quiz cannot be regenerated.")
    else:
        cfg = quiz["config"]
        items, warns = build_quiz(material_text(m), cfg["counts"], cfg["points"], quiz.get("level", "medium"))
        if items:
            db.collection("quizzes").document(qid).update({"items": items, "total": quiz_total(items),
                "n_items": sum(item_count(i) for i in items)})
            flash("New items generated." + (" " + " ".join(warns) if warns else ""))
    return redirect(url_for("t_quiz", qid=qid))

@app.post("/teacher/quizzes/<qid>/publish")
@login_required("teacher")
def t_quiz_publish(qid):
    quiz = get("quizzes", qid)
    if not quiz:
        abort(404)
    if quiz.get("activity_id"):
        flash("This quiz is already published.")
        return redirect(url_for("t_quiz", qid=qid))
    f = request.form
    key, lbl = class_from_form(f)
    due = f.get("due_date", "").strip()
    try:
        date = f.get("date") or today_str()
        datetime.strptime(date, "%Y-%m-%d")
        if due:
            datetime.strptime(due, "%Y-%m-%d")
        limit = int(f.get("time_limit", "0") or 0)
        if not 0 <= limit <= 300:
            raise ValueError
    except ValueError:
        key = None
    if not key:
        flash("Please choose who takes the quiz, valid dates, and a time limit of 0 to 300 minutes.")
        return redirect(url_for("t_quiz", qid=qid))
    ref = db.collection("activities").document()
    ref.set({"title": quiz["title"], "description": f"Online quiz - {quiz['n_items']} items, {num(quiz['total'])} points.",
             "class_key": key, "class_label": "All students" if key == "ALL" else lbl, "date": date, "due_date": due,
             "max_score": quiz["total"], "file_id": None, "file_name": None, "created_at": now().isoformat(),
             "kind": "quiz", "quiz_id": qid, "time_limit": limit, "show_answers": bool(f.get("show_answers"))})
    db.collection("quizzes").document(qid).update({"status": "published", "activity_id": ref.id,
                                                    "shuffle": bool(f.get("shuffle"))})
    notify_class(key, f"New quiz: {quiz['title']}" + (f" (due {due})" if due else ""))
    log("Published quiz", f"{quiz['title']} - {lbl}")
    flash("Quiz published. Students can take it from their Activities page. Scores are checked and recorded automatically.")
    return redirect(url_for("t_quiz", qid=qid))

@app.post("/teacher/quizzes/<qid>/delete")
@login_required("teacher")
def t_quiz_delete(qid):
    quiz = get("quizzes", qid)
    if quiz:
        a = get("activities", quiz.get("activity_id"))
        if a:
            delete_activity(a)
        db.collection("quizzes").document(qid).delete()
        log("Deleted quiz", quiz["title"])
        flash("Quiz deleted.")
    return redirect(url_for("t_materials"))

@app.route("/teacher/quizzes/<qid>/pdf")
@login_required("teacher")
def t_quiz_pdf(qid):
    quiz = get("quizzes", qid)
    if not quiz:
        abort(404)
    key = request.args.get("key") == "1"
    name = re.sub(r"[^A-Za-z0-9]+", "-", quiz["title"]).strip("-") or "quiz"
    return send_file(quiz_pdf(quiz, qid, key), mimetype="application/pdf", as_attachment=True,
                     download_name=f"{name}{'-answer-key' if key else ''}.pdf")


# ---------- student: take a quiz ----------
def _quiz_context(aid):
    st, a = get("students", session["uid"]), get("activities", aid)
    if not a or a.get("kind") != "quiz" or not visible_to(st, a):
        abort(404)
    quiz = get("quizzes", a.get("quiz_id"))
    if not quiz:
        abort(404)
    return st, a, quiz, get("quiz_attempts", f"{aid}_{st['id']}")

@app.route("/student/quiz/<aid>")
@login_required("student")
def s_quiz(aid):
    st, a, quiz, att = _quiz_context(aid)
    if att and att.get("submitted_at"):
        sub = get("submissions", f"{aid}_{st['id']}")
        final = sub["score"] if sub and sub.get("score") is not None else att["score"]
        pct = pct_of(final, a["max_score"])
        return render_template("s_quiz.html", mode="result", a=a, quiz=quiz, att=att, final=final, pct=pct,
                               remark=remark_of(pct, settings()), taken=mmss(att.get("duration")))
    if att:
        limit = int(a.get("time_limit") or 0)
        left = max(0, limit * 60 - int((now() - datetime.fromisoformat(att["started_at"])).total_seconds())) if limit else None
        return render_template("s_quiz.html", mode="take", a=a, quiz=quiz, qs=student_view(quiz, aid, st["id"]), left=left)
    return render_template("s_quiz.html", mode="intro", a=a, quiz=quiz)

@app.post("/student/quiz/<aid>/start")
@login_required("student")
def s_quiz_start(aid):
    st, a, quiz, att = _quiz_context(aid)
    if not att:
        db.collection("quiz_attempts").document(f"{aid}_{st['id']}").set({
            "activity_id": aid, "quiz_id": quiz["id"], "student_id": st["id"], "started_at": now().isoformat(),
            "submitted_at": None, "score": None, "detail": [], "overtime": False})
    return redirect(url_for("s_quiz", aid=aid))

def pct_of(score, total):
    return round(score * 100 / total) if total else 0

def remark_of(pct, cfg):
    return "Passed" if pct >= int(cfg.get("pass_percent", 75)) else "Needs improvement"

def seconds_between(a_iso, b_iso):
    try:
        return max(0, int((datetime.fromisoformat(b_iso) - datetime.fromisoformat(a_iso)).total_seconds()))
    except (TypeError, ValueError):
        return None

def mmss(sec_):
    return "-" if sec_ is None else f"{sec_ // 60}:{sec_ % 60:02d}"

def send_quiz_result_emails(st, a, att_id, score, over, submitted_iso, cfg):
    """Runs in a background thread so the student never waits for the email. Emails go out through Resend."""
    try:
        total, link = a["max_score"], f"{BASE_URL}/student/quiz/{a['id']}"
        pct = pct_of(score, total)
        details = [("Quiz", a["title"]), ("Score", f"{num(score)} / {num(total)}"), ("Percentage", f"{pct}%"),
                   ("Remarks", remark_of(pct, cfg)), ("Submitted", submitted_iso[:16].replace("T", " "))]
        if over:
            details.append(("Note", "Submitted after the time limit"))
        status = "not_sent"
        if cfg.get("email_quiz_student") and st.get("email"):
            intro = "Your quiz was checked automatically and your score is now recorded."
            text = f"Hello {st['first_name']},\n\n{intro}\n" + "\n".join(f"{k}: {v}" for k, v in details) + f"\n\nView your result: {link}\n"
            html = email_html(st["first_name"], "Your quiz result", intro, "View my result", link, f"{BASE_URL}/login", details=details)
            status = "sent" if send_mail(st["email"], f"QlickIn: your result for {a['title']}", text, html, {"logo": logo_png()}) else "failed"
        if cfg.get("email_quiz_teacher"):
            for t in find("teachers"):
                if t.get("email"):
                    who = {"first_name": t.get("name", "Teacher"), "email": t["email"]}
                    d2 = [("Student", full_name(st)), ("Class", label(st))] + details
                    intro = f"{full_name(st)} just finished the quiz. It was checked and recorded automatically."
                    text = intro + "\n" + "\n".join(f"{k}: {v}" for k, v in d2) + "\n"
                    html = email_html(who["first_name"], "A student finished a quiz", intro, "Open quiz results",
                                      f"{BASE_URL}/teacher/quizzes/{a['quiz_id']}/results", f"{BASE_URL}/login", details=d2)
                    send_mail(who["email"], f"QlickIn: {full_name(st)} finished {a['title']}", text, html, {"logo": logo_png()})
        db.collection("quiz_attempts").document(att_id).update({"email_status": status})
    except Exception as ex:                         # never let an email problem break a quiz
        print("Quiz result email error:", ex)

@app.post("/student/quiz/<aid>/submit")
@login_required("student")
def s_quiz_submit(aid):
    st, a, quiz, att = _quiz_context(aid)
    if not att or att.get("submitted_at"):
        return redirect(url_for("s_quiz", aid=aid))
    score, detail = grade_quiz(quiz, request.form)                 # 1) check every answer automatically
    limit, t = int(a.get("time_limit") or 0), now()
    taken = seconds_between(att["started_at"], t.isoformat())
    over = bool(limit and taken is not None and taken > limit * 60 + 90)
    cfg, att_id = settings(), f"{aid}_{st['id']}"
    db.collection("quiz_attempts").document(att_id).update(        # 2) record the attempt
        {"submitted_at": t.isoformat(), "score": score, "detail": detail, "overtime": over, "duration": taken,
         "pct": pct_of(score, a["max_score"]), "email_status": "queued"})
    db.collection("submissions").document(att_id).set({            # 3) the score goes straight into Records
        "activity_id": aid, "student_id": st["id"], "text": "Quiz taken online" + (" (over the time limit)" if over else ""),
        "files": [], "submitted_at": t.isoformat(), "score": score, "checked_at": t.isoformat()})
    notify(st["id"], f"Your quiz \"{a['title']}\" was checked automatically: {num(score)}/{num(a['max_score'])}.")
    threading.Thread(target=send_quiz_result_emails,               # 4) email the result (does not slow the page)
                     args=(st, a, att_id, score, over, t.isoformat(), cfg), daemon=True).start()
    flash("Quiz submitted and checked!")
    return redirect(url_for("s_quiz", aid=aid))


# ---------- teacher: quiz results, export, retake ----------
def _results_context(qid):
    quiz = get("quizzes", qid)
    a = get("activities", quiz.get("activity_id")) if quiz else None
    if not quiz or not a:
        abort(404)
    studs = sorted([x for x in find("students", status="approved")
                    if a["class_key"] == "ALL" or class_key_of(x) == a["class_key"]], key=lambda x: x["last_name"].lower())
    atts = {x["student_id"]: x for x in find("quiz_attempts", activity_id=a["id"])}
    subs = {x["student_id"]: x for x in find("submissions", activity_id=a["id"])}
    cfg, rows = settings(), []
    for x in studs:
        at, sub = atts.get(x["id"]), subs.get(x["id"])
        done = bool(at and at.get("submitted_at"))
        score = (sub["score"] if sub and sub.get("score") is not None else at["score"]) if done else None
        pct = pct_of(score, a["max_score"]) if done else None
        rows.append({"s": x, "status": "submitted" if done else ("progress" if at else "none"), "score": score, "pct": pct,
                     "remark": remark_of(pct, cfg) if done else "-", "taken": mmss(at.get("duration")) if done else "-",
                     "over": bool(at and at.get("overtime")), "email": (at or {}).get("email_status", "-") if done else "-",
                     "at": at["submitted_at"][:16].replace("T", " ") if done else "-"})
    return quiz, a, rows, cfg

@app.route("/teacher/quizzes/<qid>/results")
@login_required("teacher")
def t_quiz_results(qid):
    quiz, a, rows, cfg = _results_context(qid)
    done = [r for r in rows if r["status"] == "submitted"]
    summary = {"total": len(rows), "done": len(done), "progress": sum(r["status"] == "progress" for r in rows),
               "avg": round(sum(r["pct"] for r in done) / len(done)) if done else None,
               "passed": sum(r["pct"] >= int(cfg["pass_percent"]) for r in done), "pass_percent": cfg["pass_percent"]}
    return render_template("t_quiz_results.html", quiz=quiz, a=a, rows=rows, sm=summary)

@app.route("/teacher/quizzes/<qid>/results.csv")
@login_required("teacher")
def t_quiz_results_csv(qid):
    quiz, a, rows, cfg = _results_context(qid)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Last name", "First name", "Class", "Status", f"Score (/{num(a['max_score'])})", "Percent", "Remarks",
                "Time taken (min:sec)", "Over time limit", "Submitted"])
    for r in rows:
        s_ = r["s"]
        w.writerow([s_["last_name"], s_["first_name"], label(s_), r["status"].replace("none", "not taken").replace("progress", "not submitted"),
                    "" if r["score"] is None else num(r["score"]), "" if r["pct"] is None else r["pct"], r["remark"] if r["pct"] is not None else "",
                    r["taken"] if r["status"] == "submitted" else "", "yes" if r["over"] else "", r["at"] if r["status"] == "submitted" else ""])
    name = re.sub(r"[^A-Za-z0-9]+", "-", quiz["title"]).strip("-") or "quiz"
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}-results.csv"'})

@app.route("/teacher/quizzes/<qid>/results/<sid>")
@login_required("teacher")
def t_quiz_attempt(qid, sid):
    quiz, a, rows, cfg = _results_context(qid)
    row = next((r for r in rows if r["s"]["id"] == sid), None)
    att = get("quiz_attempts", f"{a['id']}_{sid}")
    if not row or not att or not att.get("submitted_at"):
        abort(404)
    return render_template("t_quiz_attempt.html", quiz=quiz, a=a, row=row, att=att)

@app.post("/teacher/quizzes/<qid>/retake/<sid>")
@login_required("teacher")
def t_quiz_retake(qid, sid):
    quiz, a, rows, cfg = _results_context(qid)
    st = get("students", sid)
    if st:
        db.collection("quiz_attempts").document(f"{a['id']}_{sid}").delete()
        db.collection("submissions").document(f"{a['id']}_{sid}").delete()
        notify(sid, f"Your teacher allowed you to retake the quiz \"{a['title']}\".")
        log("Quiz retake allowed", f"{a['title']} - {full_name(st)}")
        flash(f"{full_name(st)} can now take the quiz again.")
    return redirect(url_for("t_quiz_results", qid=qid))


if __name__ == "__main__":
    # Create the (single) teacher account:  python app.py create-teacher "Name" email@gmail.com password
    if len(sys.argv) == 5 and sys.argv[1] == "create-teacher":
        _, _, name, email, pw = sys.argv
        if find("teachers"):
            print("A teacher account already exists. QlickIn is set up for ONE teacher only.")
        else:
            db.collection("teachers").add({
                "name": name, "email": email.lower(), "password_hash": generate_password_hash(pw),
                "reg_token": secrets.token_hex(12), "reg_open": True, "created_at": now().isoformat()})
            print("Teacher account created.")
    else:
        app.run(debug=True)
