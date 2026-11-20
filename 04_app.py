"""หลังบ้านวิถีจรชะตา ทำนาย

ติดตั้ง: python -m pip install -r requirements.txt
เริ่มงาน: python -m streamlit run 04_app.py --server.port 4174
รัน 01_unified_schema.sql ในฐานทดสอบก่อนใช้ข้อมูลจริง

ตัวอย่าง .streamlit/secrets.toml ป้อนค่าจริงในเครื่องที่ควบคุมได้:
[auth]
redirect_uri = "http://localhost:4174/oauth2callback"
cookie_secret = "SET_A_LONG_RANDOM_VALUE"
client_id = "SET_GOOGLE_LOGIN_CLIENT_ID"
client_secret = "SET_GOOGLE_LOGIN_CLIENT_SECRET"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

[wichi]
ENVIRONMENT = "production"
SUPABASE_URL = "https://trxyvprcmyvmpjcuhfyz.supabase.co"
SUPABASE_SECRET_KEY = "SET_SERVER_SIDE_KEY"
GMAIL_CLIENT_ID = "SET_GMAIL_OAUTH_CLIENT_ID"
GMAIL_CLIENT_SECRET = "SET_GMAIL_OAUTH_CLIENT_SECRET"
GMAIL_REFRESH_TOKEN = "SET_GMAIL_MAILBOX_REFRESH_TOKEN"
OPERATORS = ["wichiforecast.service@gmail.com"]
PREDICTION_BUCKET = "prediction-reports-private"

วาง Sarabun.ttf หรือ Sarabun-Regular.ttf ข้างสคริปต์ หรือเพิ่ม FONT_REGULAR/FONT_BOLD ใน [wichi]
Gmail OAuth client ต้องรับ openid, email และ https://www.googleapis.com/auth/gmail.send
redirect_uri ใน Google Cloud OAuth ต้องตรงกับ URL ของแอปที่ลงทะเบียน
ตั้ง ENVIRONMENT เป็น staging สำหรับโปรเจกต์ทดสอบซึ่งอีเมลส่งได้เฉพาะบัญชีบริการ
หากไม่ตั้ง [wichi] หน้าทดสอบใช้ข้อมูลตัวอย่างและไม่เชื่อมต่อบริการภายนอก
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from email.utils import format_datetime
from hashlib import sha256
from importlib import import_module
import base64
import json
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import uuid4

import streamlit as st

pdf = import_module("03_pdf_generator")
SERVICE_EMAIL = "wichiforecast.service@gmail.com"
PROJECT_URL = "https://trxyvprcmyvmpjcuhfyz.supabase.co"
BUCKET = "prediction-reports-private"
REPORT_BUCKETS = {"prediction-pdfs", "prediction-reports-private"}
CHANNELS = ("Social Media", "Fastwork", "LINE Official")
PAIR_CODES = {"LG-2190", "LG-2990"}
NOTIFY_SCOPE = "https://www.googleapis.com/auth/gmail.send"
LOG = logging.getLogger("wichi")
EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
HEX_RE = re.compile(r"[0-9a-f]{64}")


class WorkflowError(RuntimeError):
    """ข้อความที่แสดงต่อเจ้าหน้าที่ได้โดยไม่มีคีย์หรือข้อมูลดิบจากปลายทาง"""


class RemoteError(WorkflowError):
    def __init__(self, name: str, status: int, code: str = ""):
        self.status = status
        self.code = code
        super().__init__(f"{name}: HTTP {status}" + (f" ({code})" if code else ""))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def request(name, method, url, headers=None, payload=None, raw=None, binary=False):
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/json")
    if payload is not None:
        headers["Content-Type"] = "application/json"
        raw = json.dumps(payload, ensure_ascii=False, default=json_default).encode("utf-8")
    try:
        opener = build_opener(NoRedirect())
        with opener.open(Request(url, headers=headers, method=method, data=raw), timeout=25) as res:
            body = res.read(25_000_001)
    except HTTPError as exc:
        body = exc.read(30_000)
        code = ""
        try:
            parsed = json.loads(body)
            raw_code = parsed.get("code") or parsed.get("error") or parsed.get("statusCode") or ""
            code = re.sub(r"[^a-zA-Z0-9_]", "", str(raw_code))[:25] if isinstance(raw_code, (str, int)) else ""
        except (ValueError, AttributeError):
            pass
        raise RemoteError(name, exc.code, code) from None
    except (URLError, TimeoutError, OSError):
        raise WorkflowError(f"{name}: การเชื่อมต่อไม่ยืนยันผล") from None
    if len(body) > 25_000_000:
        raise WorkflowError(f"{name}: ผลตอบกลับใหญ่เกินขนาด")
    if binary or not body:
        return body if binary else None
    try:
        return json.loads(body, parse_float=Decimal)
    except ValueError:
        raise WorkflowError(f"{name}: ผลตอบกลับอ่านไม่ได้") from None


class Database:
    def __init__(self, url, key, staging=False, bucket=BUCKET):
        url = url.rstrip("/")
        if (
            not re.fullmatch(r"https://[a-z0-9]{20}\.supabase\.co", url)
            or (url == PROJECT_URL) == staging
            or not key.startswith("sb_secret_")
        ):
            raise WorkflowError("ตรวจ URL โปรเจกต์และ Supabase Secret Key ในการตั้งค่าฝั่งเซิร์ฟเวอร์")
        if bucket not in REPORT_BUCKETS:
            raise WorkflowError("เลือก bucket ที่อยู่ใน SQL ของระบบ")
        self.url = url
        self.bucket = bucket
        self.headers = {"apikey": key}

    def rest(self, method, path, params=None, payload=None, representation=False):
        headers = self.headers | ({"Prefer": "return=representation"} if representation else {})
        url = self.url + "/rest/v1/" + path
        if params:
            url += "?" + urlencode(params)
        return request("Supabase", method, url, headers, payload=payload)

    def find(self, code):
        rows = self.rest("GET", "wichi_orders", {"order_code": "eq." + code, "limit": 2})
        if len(rows) != 1:
            raise WorkflowError("ไม่พบรหัสออเดอร์ที่ใช้ได้ หรือรหัสซ้ำ")
        return rows[0]

    def patch(self, order, updates, **extra_filters):
        filters = {"order_code": "eq." + str(order["order_code"])}
        if order.get("updated_at") is not None:
            filters["updated_at"] = "eq." + order["updated_at"]
        else:
            filters["updated_at"] = "is.null"
        filters.update(extra_filters)
        rows = self.rest("PATCH", "wichi_orders", filters, updates, representation=True)
        if not isinstance(rows, list) or len(rows) != 1:
            raise WorkflowError("ข้อมูลออเดอร์เปลี่ยนระหว่างทำงาน ให้โหลดใหม่ก่อนดำเนินการ")
        return rows[0]

    def upload(self, path, data):
        url = self.url + "/storage/v1/object/" + self.bucket + "/" + quote(path, safe="/")
        request(
            "Supabase Storage", "POST", url,
            self.headers | {"Content-Type": "application/pdf", "x-upsert": "false"}, raw=data,
        )

    def download(self, path, bucket=None):
        if not re.fullmatch(r"reports/[0-9a-f]{24}/[0-9a-f]{64}\.pdf", path or ""):
            raise WorkflowError("เส้นทางไฟล์รายงานไม่ตรงรูปแบบ")
        target = bucket or self.bucket
        if target not in REPORT_BUCKETS:
            raise WorkflowError("bucket ของรายงานอยู่นอกชุดที่รองรับ")
        url = self.url + "/storage/v1/object/authenticated/" + target + "/" + quote(path, safe="/")
        return request("Supabase Storage", "GET", url, self.headers, binary=True)

    def validate_bucket(self):
        bucket = request("Supabase Storage", "GET", self.url + "/storage/v1/bucket/" + self.bucket, self.headers)
        if not isinstance(bucket, dict) or bucket.get("id") != self.bucket or type(bucket.get("public")) is not bool:
            raise WorkflowError("ตรวจ Storage bucket สำหรับรายงานใน Supabase")
        return bucket["public"]


def gmail_access(config):
    form = urlencode({
        "grant_type": "refresh_token",
        "client_id": config["GMAIL_CLIENT_ID"],
        "client_secret": config["GMAIL_CLIENT_SECRET"],
        "refresh_token": config["GMAIL_REFRESH_TOKEN"],
    }).encode("ascii")
    response = request(
        "Google OAuth", "POST", "https://oauth2.googleapis.com/token",
        {"Content-Type": "application/x-www-form-urlencoded"}, raw=form,
    )
    token = response.get("access_token") if isinstance(response, dict) else None
    if not token:
        raise WorkflowError("Google OAuth ไม่มี access token")
    scope = response.get("scope")
    if scope and NOTIFY_SCOPE not in scope.split():
        raise WorkflowError("Google OAuth ต้องมี scope gmail.send")
    identity = request(
        "Google Identity", "GET", "https://openidconnect.googleapis.com/v1/userinfo",
        {"Authorization": "Bearer " + token},
    )
    if str(identity.get("email", "")).lower() != SERVICE_EMAIL or identity.get("email_verified") is not True:
        raise WorkflowError("ตัวตนผู้ส่ง Gmail ไม่ตรงกับบัญชีบริการที่ยืนยันแล้ว")
    return token


def email_ok(value):
    return isinstance(value, str) and len(value) <= 254 and EMAIL_RE.fullmatch(value) is not None


def gmail_send(token, recipient, order_code, rfc_id, issued, data, stamp, wallpaper=None):
    if not email_ok(recipient):
        raise WorkflowError("อีเมลลูกค้าไม่ถูกต้อง")
    message = EmailMessage()
    message["From"] = SERVICE_EMAIL
    message["To"] = recipient
    message["Subject"] = "รายงานวิถีจรชะตา ทำนาย " + order_code
    message["Date"] = format_datetime(issued)
    message["Message-ID"] = rfc_id
    message.set_content(
        "วิถีจรชะตา ทำนาย ได้จัดทำรายงานสำหรับออเดอร์ " + order_code +
        "\nกรุณาเปิดไฟล์แนบเพื่ออ่านรายงานของคุณ\n\n" + stamp
    )
    message.add_attachment(data, maintype="application", subtype="pdf", filename=stamp + ".pdf")
    if wallpaper:
        message.add_attachment(wallpaper, maintype="image", subtype="png", filename=stamp + "-wallpaper.png")
    sent = request(
        "Gmail", "POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        {"Authorization": "Bearer " + token},
        payload={"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")},
    )
    if not isinstance(sent, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{4,180}", str(sent.get("id", ""))):
        raise WorkflowError("Gmail ไม่คืน provider Message ID ที่ตรวจสอบได้")
    return sent["id"]


def check_ready(order, review_ok):
    if not review_ok:
        raise WorkflowError("ตรวจเนื้อหา ลำดับวัน และภาพประกอบตามแพ็กเกจก่อนส่ง")
    if order.get("payment_status") != "verified" or not order.get("payment_evidence") or not order.get("verified_at"):
        raise WorkflowError("ต้องยืนยันการชำระจากหลักฐานในระบบก่อน")
    if order.get("intake_issues") or order.get("prediction_status") == "intake_review":
        raise WorkflowError("ข้อมูลออเดอร์ยังอยู่ระหว่างตรวจ")
    if order.get("delivery_status") != "ready" or order.get("sent_at"):
        raise WorkflowError("ออเดอร์นี้เริ่มส่งแล้วหรือส่งเสร็จแล้ว")
    package = order.get("package_tier")
    if package not in pdf.PACKAGES:
        raise WorkflowError("ออเดอร์ยังไม่มีรหัสแพ็กเกจที่รองรับ")
    if order.get("channel") not in CHANNELS or (
        (order["channel"] == "Fastwork") != package.startswith(("FW-", "LG-"))
    ):
        raise WorkflowError("ช่องทางและแพ็กเกจขัดกัน")
    if package in PAIR_CODES:
        partner = order.get("partner_birth_data") or {}
        if not isinstance(partner, dict) or not all(
            (partner.get("consent") is True, partner.get("name"), partner.get("birth_date"),
             partner.get("birth_province"), partner.get("birth_time_status"))
        ):
            raise WorkflowError("ตรวจความยินยอมและข้อมูลคนที่ 2 ก่อน")
    return pdf.PACKAGES[package]


def issue_report(db, config, order, sections, artwork, review_ok, send_email=None):
    spec = check_ready(order, review_ok)
    fastwork = order.get("channel") == "Fastwork"
    email_route = (not fastwork and not spec.chat) if send_email is None else send_email
    recipient = order.get("client_email") if email_route else None
    if email_route and not email_ok(recipient):
        raise WorkflowError("ตรวจอีเมลผู้รับของออเดอร์")
    if email_route and config["STAGING"] and recipient.lower() != SERVICE_EMAIL:
        raise WorkflowError("ระบบทดสอบส่งได้เฉพาะอีเมลบัญชีบริการ")
    db.validate_bucket()
    issued = datetime.now(timezone.utc)
    data, stamp = pdf.render_pdf(order, sections, artwork, issued, config["FONT_REGULAR"], config["FONT_BOLD"])
    wallpaper = artwork.get("wallpaper") if order["package_tier"] == "SM-149" else None
    hashes = {"pdf": sha256(data).hexdigest()}
    if wallpaper:
        hashes["wallpaper_png"] = sha256(wallpaper).hexdigest()
    path = "reports/" + uuid4().hex[:24] + "/" + hashes["pdf"] + ".pdf"
    token = str(uuid4())
    rfc_id = f"<wichi.{token}@gmail.com>" if email_route else None
    access = gmail_access(config) if email_route else None
    claimed = db.patch(order, {
        "delivery_status": "preparing", "delivery_token": token, "delivery_started_at": utc_now(),
        "delivery_recipient": recipient, "delivery_rfc_id": rfc_id, "pdf_sha256": hashes["pdf"],
        "pdf_object_path": path, "copyright_sig": stamp, "delivery_attachment_hashes": hashes,
        "prediction_text": pdf.chat_text(order, sections, stamp), "report_sections": dict(sections),
    }, delivery_status="eq.ready", payment_status="eq.verified", sent_at="is.null")
    try:
        db.upload(path, data)
    except WorkflowError:
        # อ่านไฟล์ปลายทางยืนยัน SHA-256 เมื่อ HTTP จากการอัปโหลดมีผลไม่ชัดเจน
        try:
            stored_ok = sha256(db.download(path)).hexdigest() == hashes["pdf"]
        except WorkflowError:
            stored_ok = False
        if not stored_ok:
            try:
                db.patch(claimed, {"delivery_status": "ready", "delivery_note": "storage_failed_before_send"},
                         delivery_status="eq.preparing", delivery_token="eq." + token)
            except WorkflowError:
                pass
            raise WorkflowError("ยืนยันไฟล์ที่จัดเก็บไม่ได้ ยังไม่เริ่มส่ง Gmail") from None

    stored_fields = {"pdf_url": f"storage://{db.bucket}/{path}", "prediction_status": "generated"}
    if not email_route:
        return db.patch(claimed, stored_fields | {"delivery_status": "manual_ready"},
                        delivery_status="eq.preparing", delivery_token="eq." + token)
    started = db.patch(claimed, stored_fields | {"delivery_status": "sending"},
                       delivery_status="eq.preparing", delivery_token="eq." + token)
    try:
        gmail_id = gmail_send(access, recipient, str(order["order_code"]), rfc_id, issued, data, stamp, wallpaper)
    except WorkflowError as exc:
        try:
            db.patch(started, {"delivery_status": "unknown", "delivery_note": "gmail_outcome_unconfirmed"},
                     delivery_status="eq.sending", delivery_token="eq." + token)
        except WorkflowError:
            pass
        raise WorkflowError("ผล Gmail ยังไม่ยืนยัน ตรวจ Sent ด้วยรหัส RFC Message-ID ก่อน") from exc
    try:
        return db.patch(started, {
            "delivery_status": "accepted", "delivery_message_id": gmail_id,
            "sent_at": utc_now(), "prediction_status": "sent", "delivery_note": None,
        }, delivery_status="eq.sending", delivery_token="eq." + token)
    except WorkflowError:
        try:
            latest = db.find(order["order_code"])
            if latest.get("delivery_status") == "accepted" and latest.get("delivery_message_id") == gmail_id:
                return latest
        except WorkflowError:
            pass
        raise WorkflowError(f"Gmail ตอบรับด้วย Message ID {gmail_id} การบันทึกผลยังไม่ยืนยัน ตรวจ Sent ก่อนดำเนินการ") from None


def config_from_secrets():
    try:
        data = st.secrets.get("wichi")
    except FileNotFoundError:
        return None
    if not data:
        return None
    return {
        "STAGING": data.get("ENVIRONMENT") == "staging",
        "SUPABASE_URL": str(data.get("SUPABASE_URL", PROJECT_URL)),
        "SUPABASE_SECRET_KEY": str(data.get("SUPABASE_SECRET_KEY", "")),
        "GMAIL_CLIENT_ID": str(data.get("GMAIL_CLIENT_ID", "")),
        "GMAIL_CLIENT_SECRET": str(data.get("GMAIL_CLIENT_SECRET", "")),
        "GMAIL_REFRESH_TOKEN": str(data.get("GMAIL_REFRESH_TOKEN", "")),
        "FONT_REGULAR": data.get("FONT_REGULAR"),
        "FONT_BOLD": data.get("FONT_BOLD"),
        "PREDICTION_BUCKET": str(data.get("PREDICTION_BUCKET", BUCKET)),
        "OPERATORS": {str(value).strip().lower() for value in data.get("OPERATORS", [SERVICE_EMAIL])},
    }


def render_local_preview():
    st.info("โหมดทดลองในเครื่อง ใช้ข้อมูลตัวอย่าง")
    try:
        if st.button("สร้าง PDF ตัวอย่างภาษาไทย"):
            order = {"order_code": "WJ-DEMO-LOCAL", "package_tier": "SM-99",
                     "package_name": "Social แพ็กเกจเปิดใจ", "client_name": "ผู้รับรายงานทดสอบ"}
            sections = {pdf.BASE: "ตัวอย่างการจัดรายงานภาษาไทย", pdf.QUESTIONS: "ข้อความทดสอบระบบ"}
            rendered, stamp = pdf.render_pdf(order, sections, {}, datetime.now(timezone.utc))
            st.session_state["demo_report"] = (rendered, stamp)
        if demo := st.session_state.get("demo_report"):
            st.download_button("ดาวน์โหลด PDF ตัวอย่าง", demo[0], demo[1] + ".pdf", "application/pdf")
    except ValueError as exc:
        st.error(str(exc))


def payment_reference(order):
    """Prefer the dedicated route field while supporting historical Fastwork slips."""
    submitted = order.get("payment_reference_input")
    if order.get("payment_method") == "fastwork_escrow" or order.get("channel") == "Fastwork":
        return str(order.get("fastwork_order_no") or submitted or order.get("slip_trans_ref") or "")
    return str(order.get("slip_trans_ref") or submitted or "")


def render_revenue_dashboard(db):
    with st.container():
        st.subheader("ยอดออเดอร์และยอดชำระที่ตรวจแล้ว")
        current = datetime.now(pdf.BANGKOK).date()
        with st.form("revenue_summary"):
            starts = st.date_input("ตั้งแต่วันที่ (เวลาไทย)", current - timedelta(days=29))
            ends = st.date_input("ถึงวันที่ (เวลาไทย)", current)
            refresh = st.form_submit_button("ดูยอดตามช่วงวันที่")
        if refresh:
            if starts < date(2000, 1, 1) or ends < starts or (ends - starts).days > 365:
                st.error("เลือกช่วงวันที่เรียงตามลำดับและไม่เกิน 366 วัน")
                return
            try:
                rows = db.rest("POST", "rpc/wichi_revenue_summary", payload={
                    "p_start": starts.isoformat(), "p_end": ends.isoformat(),
                })
            except WorkflowError as exc:
                st.error(str(exc))
                return
            st.session_state["wichi_revenue"] = (starts, ends, rows)
        saved = st.session_state.get("wichi_revenue")
        if saved:
            starts, ends, rows = saved
            st.caption(f"{starts:%d/%m/%Y} – {ends:%d/%m/%Y} ตามเวลาไทย")
            st.dataframe([{
                "ช่องทาง": row["channel"], "รับออเดอร์": row["received_orders"],
                "ยอดตามออเดอร์ (บาท)": f'{Decimal(str(row["expected_thb"])):,.2f}',
                "ยืนยันชำระแล้ว": row["verified_orders"],
                "ยอดที่ยืนยันแล้ว (บาท)": f'{Decimal(str(row["verified_thb"])):,.2f}',
            } for row in rows], hide_index=True)
            st.caption("ยอดยืนยันอิงวันที่ตรวจชำระ รวม Fastwork Escrow ตามมูลค่าออเดอร์ ส่วนค่าธรรมเนียม เงินคืน และยอดเบิกสุทธิให้กระทบยอดจากต้นทาง")


def delivery_chat_copy(order):
    code = str(order["order_code"])
    stamp = str(order.get("copyright_sig") or "")
    if order.get("channel") == "Fastwork":
        return (
            f"วิถีจรชะตา ทำนาย ส่งรายงานออเดอร์ {code} แล้วครับ\n"
            f"กรุณาเปิด PDF ที่แนบในหน้าส่งมอบงาน Fastwork\nรหัสรายงาน {stamp}\n"
            "หากมีคำถามในขอบเขตแพ็กเกจ พิมพ์ตอบในหน้าออเดอร์นี้ได้เลยครับ"
        )
    customer_text = str(order.get("prediction_text") or "")
    return customer_text or f"วิถีจรชะตา ทำนาย ส่งคำพยากรณ์ออเดอร์ {code} แล้วครับ\nรหัสรายงาน {stamp}"


def render_order_context(order, package=None):
    with st.expander("ข้อมูลเจ้าชะตาและข้อมูลติดต่อ", expanded=True):
        st.text("ชื่อ " + str(order.get("client_name") or "รอตรวจ"))
        st.text("วันเกิด " + str(order.get("birth_date") or "รอตรวจ"))
        state = order.get("birth_time_status") or "unconfirmed"
        st.text("เวลาเกิด " + str(order.get("birth_time") or "ไม่ระบุ") + " · " + state)
        st.text("สถานที่เกิด " + str(order.get("birth_province") or "รอตรวจ"))
        st.text("ที่อยู่ปัจจุบัน " + str(order.get("current_residence") or "ไม่ระบุ"))
        st.text("อีเมล " + str(order.get("client_email") or "รอตรวจ"))
        st.text("แชต " + str(order.get("client_line_id") or "ไม่ระบุ"))
        st.text("ยอดตามออเดอร์ " + str(order.get("expected_amount") or "รอตรวจ") + " บาท")
        if order.get("channel_input"):
            st.text("ช่องทางจากฟอร์ม " + str(order["channel_input"]))
        if order.get("focus_topic"):
            st.text("หัวข้อเจาะลึก " + str(order["focus_topic"]))
        if payment_reference(order):
            st.text("เลขอ้างอิงการชำระ " + payment_reference(order))
        if package:
            st.caption("สิ่งที่ได้รับตามแพ็กเกจ")
            st.text(str(package.get("deliverables") or "รอตรวจแค็ตตาล็อก"))
        if order.get("package_tier") in PAIR_CODES:
            partner = order.get("partner_birth_data") or {}
            if isinstance(partner, dict) and partner.get("consent") is True:
                st.markdown("**ข้อมูลคนที่ 2 ที่มีความยินยอม**")
                st.text("ชื่อ " + str(partner.get("name") or "รอตรวจ"))
                st.text("วันเกิด " + str(partner.get("birth_date") or "รอตรวจ"))
                st.text("เวลาเกิด " + str(partner.get("birth_time") or "ไม่ระบุ")
                        + " · " + str(partner.get("birth_time_status") or "unconfirmed"))
                st.text("สถานที่เกิด " + str(partner.get("birth_province") or "รอตรวจ"))
            else:
                st.info("รอความยินยอมและข้อมูลคนที่ 2")


def render_intake(db, order, packages, operator):
    needs_resolution = (
        order.get("intake_issues") or not order.get("package_tier") or
        order.get("payment_method") not in ("promptpay", "fastwork_escrow") or
        order.get("payment_status") == "duplicate_review"
    )
    if not needs_resolution or order.get("delivery_status") != "ready" or order.get("sent_at"):
        return
    with st.expander("ตรวจแก้ข้อมูลที่เข้าคิว"):
        st.text(", ".join(order.get("intake_issues") or []) or "ตรวจรายการเดิมหรือเลขอ้างอิงซ้ำ")
        st.caption("เทียบข้อมูลกับต้นทางและบันทึกเหตุผลก่อนเริ่มตรวจยอด")
        with st.form("intake_" + str(order["order_code"])):
            channel = st.selectbox("ช่องทาง", CHANNELS,
                index=CHANNELS.index(order["channel"]) if order.get("channel") in CHANNELS else (0 if order.get("channel") in ("Facebook / เพจวิถีจรชะตา", "TikTok") else None))
            codes = tuple(packages)
            chosen = st.selectbox("แพ็กเกจ", codes,
                index=codes.index(order["package_tier"]) if order.get("package_tier") in codes else None,
                format_func=lambda code: f"{code} · {packages[code]['package_name']}")
            name = st.text_input("ชื่อ", order.get("client_name") or "")
            address = st.text_input("อีเมล", order.get("client_email") or "")
            dob = st.text_input("วันเกิด ค.ศ. (YYYY-MM-DD)", str(order.get("birth_date") or ""))
            province = st.text_input("จังหวัดหรือเมืองและประเทศเกิด", order.get("birth_province") or "")
            residence = st.text_input("สถานที่ปัจจุบัน", order.get("current_residence") or "")
            time_states = ("unknown", "exact", "approximate", "unconfirmed")
            time_state = st.selectbox("สถานะเวลาเกิด", time_states,
                index=time_states.index(order["birth_time_status"]) if order.get("birth_time_status") in time_states else 0)
            birth_time = st.text_input("เวลาเกิด HH:MM", str(order.get("birth_time") or "")[:5])
            partner = order.get("partner_birth_data") or {}
            if not isinstance(partner, dict):
                partner = {}
            partner_name = st.text_input("ชื่อคนที่ 2", partner.get("name") or "")
            partner_dob = st.text_input("วันเกิดคนที่ 2 (YYYY-MM-DD)", partner.get("birth_date") or "")
            partner_province = st.text_input("จังหวัดหรือเมืองและประเทศเกิดคนที่ 2", partner.get("birth_province") or "")
            partner_state = st.selectbox("สถานะเวลาเกิดคนที่ 2", time_states,
                index=time_states.index(partner["birth_time_status"]) if partner.get("birth_time_status") in time_states else 0)
            partner_time = st.text_input("เวลาเกิดคนที่ 2 HH:MM", str(partner.get("birth_time") or "")[:5])
            consent = st.checkbox("ตรวจคำยินยอมจากคนที่ 2 แล้ว", value=partner.get("consent") is True)
            reference = st.text_input("เลขอ้างอิงจากหลักฐานล่าสุด", payment_reference(order))
            reason = st.text_area("เหตุผลและที่มาของการแก้ไข")
            save = st.form_submit_button("บันทึกข้อมูลที่ผ่านการตรวจ")
        if not save:
            return
        if not channel or not chosen or ((channel == "Fastwork") != chosen.startswith(("FW-", "LG-"))):
            raise WorkflowError("เลือกแพ็กเกจที่ตรงช่องทาง")
        if not name.strip() or not province.strip() or not email_ok(address.strip()) or len(reason.strip()) < 8:
            raise WorkflowError("ตรวจชื่อ อีเมล สถานที่เกิด และเหตุผล")
        main_date = checked_date(dob)
        main_time = checked_time(birth_time, time_state)
        pair_data = None
        if chosen in PAIR_CODES:
            if not consent or not partner_name.strip() or not partner_province.strip():
                raise WorkflowError("ดวงคู่ต้องมีความยินยอม ชื่อ และสถานที่เกิดคนที่ 2")
            pair_data = {
                "consent": True, "name": partner_name.strip(), "birth_date": checked_date(partner_dob),
                "birth_province": partner_province.strip(), "birth_time": checked_time(partner_time, partner_state),
                "birth_time_status": partner_state,
            }
        changed_ref = re.sub(r"\s+", "", reference).upper()
        if order.get("payment_status") == "duplicate_review" and (
            not changed_ref or changed_ref == re.sub(r"\s+", "", payment_reference(order)).upper()
        ):
            raise WorkflowError("รายการซ้ำต้องมีเลขอ้างอิงใหม่จากหลักฐานที่ตรวจได้")
        pkg = packages[chosen]
        updates = {
            "channel": channel, "package_tier": chosen, "package_name": pkg["package_name"],
            "expected_amount": pkg["price_thb"], "client_name": name.strip(), "client_email": address.strip(),
            "birth_date": main_date, "birth_time": main_time, "birth_time_status": time_state,
            "birth_province": province.strip(), "current_residence": residence.strip() or None,
            "partner_birth_data": pair_data, "payment_reference_input": reference.strip() or None,
            "payment_method": "fastwork_escrow" if channel == "Fastwork" else "promptpay",
            "intake_issues": [], "prediction_status": "pending",
            "intake_resolution_note": f"{utc_now()} {operator} {reason.strip()[:1000]}",
        }
        if order.get("payment_status") == "verified":
            raise WorkflowError("ข้อมูลการชำระยืนยันแล้ว ต้องทบทวนหลักฐานก่อนแก้ไขรายการ")
        updates["payment_status"] = "pending_verification" if changed_ref else "awaiting_reference"
        existing_method = order.get("payment_method")
        reference_unchanged = changed_ref == re.sub(r"\s+", "", payment_reference(order)).upper()
        if not reference_unchanged or existing_method != updates["payment_method"]:
            updates["slip_trans_ref"] = None
            updates["fastwork_order_no"] = None
        db.patch(order, updates, delivery_status="eq.ready", sent_at="is.null")
        st.rerun()


def checked_date(value):
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise WorkflowError("กรอกวันเกิดเป็น YYYY-MM-DD") from None
    if parsed.year < 1850 or parsed > datetime.now(pdf.BANGKOK).date():
        raise WorkflowError("ตรวจปีและวันเกิด")
    return parsed.isoformat()


def checked_time(value, state):
    if state == "unknown":
        if value:
            raise WorkflowError("ลบตัวเลขเวลาเกิดเมื่อเลือกไม่ทราบเวลา")
        return None
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value or ""):
        raise WorkflowError("กรอกเวลาเกิด HH:MM")
    return value + ":00"


def render_payment(db, order, operator):
    if order.get("payment_evidence") and order.get("payment_status") == "verified":
        st.success("ยืนยันชำระเงินแล้ว")
        return
    method = order.get("payment_method")
    if method not in ("promptpay", "fastwork_escrow") or order.get("intake_issues"):
        st.info("ตรวจข้อมูลออเดอร์ก่อนยืนยันการชำระเงิน")
        return
    heading = "ตรวจหน้าคำสั่งซื้อ Fastwork" if method == "fastwork_escrow" else "ตรวจรายการรับเงินจริง / ผู้ให้บริการตรวจสลิป"
    if order.get("payment_status") == "duplicate_review":
        st.warning("เลขอ้างอิงซ้ำ กรุณาตรวจข้อมูลที่เข้าคิวและหลักฐานรายการใหม่")
        return
    with st.form("pay_" + str(order["order_code"])):
        st.markdown("**" + heading + "**")
        st.caption("ยอดคาดชำระ " + str(order.get("expected_amount")) + " บาท")
        amount = st.text_input("ยอดที่ตรวจพบจริง")
        reference = st.text_input("เลขอ้างอิงที่ตรวจจากต้นทาง", payment_reference(order))
        evidence = st.text_area("หลักฐานที่ตรวจสอบได้", placeholder="แหล่งที่ตรวจ สถานะ และเวลาโดยย่อ")
        attested = st.checkbox("ตรวจผู้รับเงินหรือสถานะ escrow และยอดตามออเดอร์แล้ว")
        submitted = st.form_submit_button("บันทึกการยืนยันชำระเงิน")
    if submitted:
        if not attested:
            raise WorkflowError("ยืนยันจากหลักฐานต้นทางก่อน")
        if not re.fullmatch(r"\d{1,10}(?:\.\d{1,2})?", amount.strip()):
            raise WorkflowError("ยอดเงินจริงต้องเป็นจำนวนบวกและมีทศนิยมได้ไม่เกิน 2 หลัก")
        result = db.rest("POST", "rpc/wichi_verify_payment", payload={
            "p_order_code": order["order_code"], "p_amount": Decimal(amount.strip()),
            "p_reference": reference.strip(), "p_evidence": evidence.strip(), "p_operator": operator,
        })
        if result.get("payment_status") != "verified":
            raise WorkflowError("การยืนยันชำระยังอ่านผลไม่ได้")
        st.rerun()


def render_delivery(db, config, order, operator):
    code = str(order["order_code"])
    state = order.get("delivery_status")
    st.text("สถานะการส่ง " + str(state))
    if state in ("accepted", "manual_ready", "sending", "unknown", "manual_sent") and order.get("pdf_object_path"):
        if st.button("โหลด PDF ที่จัดเก็บเพื่อตรวจหรือแนบในแชต"):
            stored_url = str(order.get("pdf_url") or "")
            stored_bucket = stored_url.removeprefix("storage://").split("/", 1)[0] if stored_url.startswith("storage://") else None
            content = db.download(order["pdf_object_path"], stored_bucket)
            if not order.get("pdf_sha256") or not HEX_RE.fullmatch(order["pdf_sha256"]) or sha256(content).hexdigest() != order["pdf_sha256"]:
                raise WorkflowError("SHA-256 รายงานปลายทางไม่ตรงกับทะเบียน")
            st.download_button("ดาวน์โหลด PDF", content, file_name=order["copyright_sig"] + ".pdf", mime="application/pdf")
    if state == "accepted":
        st.text("Gmail Message ID " + str(order.get("delivery_message_id")))
        st.code(delivery_chat_copy(order), language=None)
        if order.get("channel") == "Fastwork":
            st.caption("สำเนาทางอีเมลพร้อมแล้ว ส่งมอบ PDF ผ่านหน้าออเดอร์ Fastwork ด้วย")
        return
    if state == "manual_sent":
        st.success("บันทึกหลักฐานส่งงานผ่านแชตแล้ว")
        return
    if state == "manual_ready":
        route = "Fastwork" if order.get("channel") == "Fastwork" else "แชต Social / LINE"
        st.caption("ส่งจากหน้าออเดอร์เดิมทาง " + route)
        st.code(delivery_chat_copy(order), language=None)
        sends_only_text = pdf.PACKAGES.get(str(order.get("package_tier"))) is not None and pdf.PACKAGES[str(order["package_tier"])].chat
        if sends_only_text and order.get("channel") != "Fastwork":
            evidence_label = "ตรวจว่าได้ส่งข้อความให้ผู้รับที่ถูกต้องแล้ว"
        else:
            evidence_label = "ตรวจในหน้าออเดอร์แล้วว่าข้อความและไฟล์ถึงปลายทาง"
        with st.form("manual_" + code):
            evidence = st.text_input("เลขอ้างอิงหรือเวลาของข้อความส่งมอบที่ตรวจแล้ว")
            confirmed = st.checkbox(evidence_label)
            done = st.form_submit_button("บันทึกส่งมอบผ่านแชต")
        if done:
            if not confirmed or len(evidence.strip()) < 6:
                raise WorkflowError("ต้องมีหลักฐานการส่งมอบและยืนยันผล")
            db.patch(order, {
                "delivery_status": "manual_sent", "prediction_status": "sent", "sent_at": utc_now(),
                "delivery_note": f"{operator} {route} {evidence.strip()[:500]}",
            }, delivery_status="eq.manual_ready", sent_at="is.null")
            st.rerun()
        return
    if state in ("unknown", "sending"):
        st.warning("ตรวจ Gmail Sent และไฟล์แนบก่อนสรุปผล ระบบระงับการส่งซ้ำในสถานะนี้")
        st.code("rfc822msgid:" + str(order.get("delivery_rfc_id") or "").strip("<>"), language=None)
        st.text("ผู้รับ " + str(order.get("delivery_recipient")) + " | SHA-256 " + str(order.get("pdf_sha256")))
        with st.form("reconcile_" + code):
            gmail_id = st.text_input("Gmail API Message ID ที่ตรวจจากข้อความเดิม")
            confirmed = st.checkbox("ยืนยันข้อความใน Sent ตรงผู้รับ รหัสออเดอร์ และไฟล์แนบ")
            done = st.form_submit_button("กระทบยอดข้อความที่พบ")
        if done:
            if not confirmed or not re.fullmatch(r"[A-Za-z0-9_-]{4,180}", gmail_id.strip()):
                raise WorkflowError("ต้องยืนยันข้อความเดิมและระบุ Gmail API Message ID")
            db.patch(order, {
                "delivery_status": "accepted", "prediction_status": "sent", "sent_at": utc_now(),
                "delivery_message_id": gmail_id.strip(), "delivery_note": "reconciled by " + operator,
            }, delivery_status="eq." + state, sent_at="is.null")
            st.rerun()
        return
    if state == "preparing":
        start = datetime.fromisoformat(order["delivery_started_at"])
        if datetime.now(timezone.utc) - start > timedelta(minutes=15):
            if st.button("คืนคิวที่หยุดก่อนเริ่มส่ง"):
                db.patch(order, {"delivery_status": "ready", "delivery_note": "recovered before Gmail by " + operator},
                         delivery_status="eq.preparing", sent_at="is.null")
                st.rerun()
        return
    if state != "ready" or order.get("payment_status") != "verified":
        return
    if order.get("sent_at"):
        st.warning("รายการนี้มีเวลาส่งมอบเดิม จึงอยู่ระหว่างตรวจประวัติ")
        return
    spec = pdf.PACKAGES.get(order.get("package_tier"))
    if spec is None:
        return
    old_sections = order.get("report_sections") or {}
    st.markdown("**คำพยากรณ์และไฟล์ประกอบที่ตรวจแล้ว**")
    if spec.question_count:
        st.caption(f"สิทธิ์ตอบตามแพ็กเกจ {spec.question_count} คำถาม ตรวจให้ครบก่อนส่ง")
    if order.get("questions"):
        st.text("โจทย์จากฟอร์ม " + str(order["questions"]))
    selected_images = spec.images
    if order.get("package_tier") == "SM-149":
        bonus = st.radio("ไฟล์เสริมตามที่ตกลงกับลูกค้า", ("wallpaper", "personal_yantra"),
                         format_func=lambda key: pdf.IMAGE_LABELS[key])
        selected_images = (bonus,)
    with st.form("report_" + code):
        sections = {title: st.text_area(title, value=old_sections.get(title, ""), height=115, max_chars=80_000,
                    help=(
                        "ใส่ 7 บรรทัด วันที่ | ช่วงเวลา | คำแนะนำ" if title == pdf.WINDOWS else
                        "หนึ่งบรรทัดต่อด้าน รูปแบบ มิติสมพงศ์ | คนที่ 1 | คนที่ 2 | ข้อสรุปร่วม" if title == pdf.PAIR else None
                    ))
                    for title in spec.sections}
        files = {key: st.file_uploader(pdf.IMAGE_LABELS[key], type=["png"] if key == "wallpaper" else ["png", "jpg", "jpeg"])
                 for key in selected_images}
        review = st.checkbox("ตรวจคำพยากรณ์ หัวข้อ ระยะเวลา และภาพทั้งหมดแล้ว")
        save = st.form_submit_button("เก็บร่างข้อความ")
        route_manual = spec.chat or order.get("channel") == "Fastwork"
        request_email = st.form_submit_button("สร้าง PDF + อัปโหลด + ส่งอีเมลแนบไฟล์")
        prepare_chat = st.form_submit_button("สร้าง PDF + อัปโหลด + เตรียมข้อความแชต") if route_manual else False
    if save:
        db.patch(order, {"report_sections": sections, "prediction_status": "draft"},
                 delivery_status="eq.ready", sent_at="is.null")
        st.rerun()
    if request_email or prepare_chat:
        images = {key: uploaded.getvalue() for key, uploaded in files.items() if uploaded is not None}
        result = issue_report(db, config, order, sections, images, review, send_email=request_email)
        st.session_state["wichi_notice"] = "Gmail รับข้อความแล้ว" if result.get("delivery_status") == "accepted" else "สร้าง PDF และเตรียมข้อความส่งทางแชตแล้ว"
        st.rerun()


def order_label(row):
    parts = [row.get("order_code"), row.get("package_name"), row.get("payment_status")]
    return " · ".join(str(value) for value in parts if value)


def main():
    st.set_page_config(page_title="ออเดอร์ | วิถีจรชะตา ทำนาย", page_icon="✦",
                       layout="centered", initial_sidebar_state="collapsed")
    st.title("วิถีจรชะตา ทำนาย")
    st.caption("จัดการออเดอร์ ตรวจชำระ และจัดส่งรายงาน")
    try:
        config = config_from_secrets()
        if config is None:
            render_local_preview()
            return
        if not st.user.is_logged_in:
            if st.button("เข้าสู่ระบบด้วย Google"):
                st.login()
            st.stop()
        operator = str(st.user.get("email", "")).lower()
        if st.user.get("email_verified") is not True or operator not in config["OPERATORS"]:
            st.error("บัญชีนี้ไม่มีสิทธิ์เข้าหลังบ้าน")
            if st.button("ออกจากระบบ"):
                st.logout()
            st.stop()
        if st.sidebar.button("ออกจากระบบ"):
            st.logout()
        if notice := st.session_state.pop("wichi_notice", None):
            st.success(notice)
        db = Database(config["SUPABASE_URL"], config["SUPABASE_SECRET_KEY"],
                      staging=config["STAGING"], bucket=config["PREDICTION_BUCKET"])
        view = st.radio("เลือกงาน", ("ออเดอร์", "สรุปรายรับ"), horizontal=True)
        if view == "สรุปรายรับ":
            render_revenue_dashboard(db)
            return
        rows = db.rest("GET", "wichi_packages", {"select": "package_code,package_name,price_thb,deliverables", "order": "sort_order.asc"})
        packages = {row["package_code"]: row for row in rows if row["package_code"] in pdf.PACKAGES}
        recent = db.rest("GET", "wichi_orders", {
            "select": "order_code,channel,package_name,payment_status,prediction_status,delivery_status,created_at",
            "order": "created_at.desc.nullslast,order_code.desc", "limit": 50,
        })
        by_code = {str(row["order_code"]): row for row in recent if row.get("order_code")}
        selected = st.selectbox("เลือกออเดอร์ล่าสุด", tuple(by_code), index=None,
                                format_func=lambda key: order_label(by_code[key]))
        lookup = st.text_input("ค้นด้วยรหัสออเดอร์", placeholder="กรอกรหัสขึ้นต้น WJ-")
        with st.expander("ดูตารางออเดอร์ทั้งหมดในหน้านี้"):
            st.dataframe(recent, hide_index=True)
        code = lookup.strip() or selected
        if not code:
            return
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", code.strip()):
            raise WorkflowError("รหัสออเดอร์ใช้ตัวอักษร ตัวเลข ขีด และขีดล่าง")
        order = db.find(code.strip())
        st.subheader(code.strip())
        st.caption(f"{order.get('channel')} · {order.get('package_name')} · {order.get('payment_status')}")
        render_order_context(order, packages.get(order.get("package_tier")))
        render_intake(db, order, packages, operator)
        render_payment(db, order, operator)
        render_delivery(db, config, order, operator)
    except (WorkflowError, ValueError, InvalidOperation) as exc:
        if isinstance(exc, (WorkflowError, ValueError)):
            st.error(str(exc))
        else:
            st.error("ตรวจค่าตัวเลขของออเดอร์")
    except Exception as exc:
        LOG.error("Workflow stopped: %s", type(exc).__name__)
        st.error("ระบบหยุดก่อนยืนยันผล กรุณาตรวจการตั้งค่าและสถานะของออเดอร์")


if __name__ == "__main__":
    main()
