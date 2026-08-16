import os, json, math, smtplib, threading, traceback, requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, url_for, redirect, session, flash, jsonify
from flask_mysqldb import MySQL
import MySQLdb.cursors
from werkzeug.security import generate_password_hash, check_password_hash
from email.mime.text import MIMEText

try:
    from pywebpush import webpush, WebPushException
except ImportError:
    webpush = None
    WebPushException = Exception

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"), static_folder=os.path.join(BASE_DIR, "static"), static_url_path="/static")
app.secret_key = os.environ.get("LIFESHIELD_SECRET", "lifeshield-dev-secret")

app.config["MYSQL_HOST"] = os.environ.get("MYSQL_HOST", "localhost")
app.config["MYSQL_PORT"] = int(os.environ.get("MYSQL_PORT", "3306"))
app.config["MYSQL_USER"] = os.environ.get("MYSQL_USER", "root")
app.config["MYSQL_PASSWORD"] = os.environ.get("MYSQL_PASSWORD", "")
app.config["MYSQL_DB"] = os.environ.get("MYSQL_DB", "lifeshield_db")
app.config["MYSQL_CURSORCLASS"] = "DictCursor"
# Aiven (and most cloud MySQL providers) require SSL. Skip for plain localhost dev.
if app.config["MYSQL_HOST"] != "localhost":
    app.config["MYSQL_SSL"] = {"ssl": {"ssl-mode": "REQUIRED"}}
mysql = MySQL(app)

# FIX 1: Added missing Twilio / Google / Ntfy configs
app.config["MAIL_SERVER"] = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", "587"))
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME", "")
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD", "")
app.config["MAIL_USE_TLS"] = True
app.config["TWILIO_ACCOUNT_SID"] = os.environ.get("TWILIO_ACCOUNT_SID", "")
app.config["TWILIO_AUTH_TOKEN"] = os.environ.get("TWILIO_AUTH_TOKEN", "")
app.config["TWILIO_FROM_NUMBER"] = os.environ.get("TWILIO_FROM_NUMBER", "")
# Node.js calling microservice (server.js) - handles actual Twilio Voice calls
app.config["CALL_SERVICE_URL"] = os.environ.get("CALL_SERVICE_URL", "http://localhost:4000")
app.config["CALL_SERVICE_SECRET"] = os.environ.get("CALL_SERVICE_SECRET", "")
app.config["GOOGLE_PLACES_API_KEY"] = os.environ.get("GOOGLE_PLACES_API_KEY", "")
app.config["NTFY_SERVER_URL"] = os.environ.get("NTFY_SERVER_URL", "https://ntfy.sh").rstrip("/")
app.config["NTFY_TOPIC"] = os.environ.get("NTFY_TOPIC", "")
app.config["VAPID_PUBLIC_KEY"] = os.environ.get("VAPID_PUBLIC_KEY", "")
app.config["VAPID_PRIVATE_KEY"] = os.environ.get("VAPID_PRIVATE_KEY", "")
app.config["VAPID_CLAIMS_EMAIL"] = os.environ.get("VAPID_CLAIMS_EMAIL", app.config.get("MAIL_USERNAME", ""))

def safe_float(v, d=None):
    try:
        if v is None: return d
        return float(v)
    except: return d

def safe_int(v, d=0):
    try:
        if v is None: return d
        return int(float(v))
    except: return d

def valid_coordinates(lat, lon):
    lat = safe_float(lat); lon = safe_float(lon)
    if lat is None or lon is None: return False
    if not -90 <= lat <= 90: return False
    if not -180 <= lon <= 180: return False
    if lat == 0 and lon == 0: return False
    return True

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    rlat1 = math.radians(float(lat1)); rlon1 = math.radians(float(lon1))
    rlat2 = math.radians(float(lat2)); rlon2 = math.radians(float(lon2))
    dlat = rlat2 - rlat1; dlon = rlon2 - rlon1
    a = math.sin(dlat/2)**2 + math.cos(rlat1)*math.cos(rlat2)*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def normalize_phone(phone):
    if not phone: return ""
    s = str(phone).strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 10: return "+91" + digits
    if len(digits) == 11 and digits.startswith("0"): return "+91" + digits[1:]
    if len(digits) == 12 and digits.startswith("91"): return "+" + digits
    if s.startswith("+"): return "+" + digits
    return "+" + digits if digits else ""

def send_emergency_email(recipient_email, subject, body):
    if not recipient_email: return False
    username = app.config["MAIL_USERNAME"]; password = app.config["MAIL_PASSWORD"]
    if not username or not password:
        print("MAIL not configured - skipping email")
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject; msg["From"] = username; msg["To"] = recipient_email
        with smtplib.SMTP(app.config["MAIL_SERVER"], app.config["MAIL_PORT"]) as server:
            server.starttls(); server.login(username, password); server.send_message(msg)
        print(f"EMAIL SENT -> {recipient_email}")
        return True
    except Exception as e:
        print(f"EMAIL ERROR -> {e}"); return False

def send_hospital_sms(phone, body):
    phone = normalize_phone(phone)
    sid = app.config.get("TWILIO_ACCOUNT_SID", "")
    token = app.config.get("TWILIO_AUTH_TOKEN", "")
    from_number = app.config.get("TWILIO_FROM_NUMBER", "")
    if not sid or not token or not from_number:
        print(f"MOCK SMS (Twilio not configured) -> {phone} | Body: {body[:80]}...")
        return True
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"To": phone, "From": from_number, "Body": body},
            auth=(sid, token), timeout=15
        )
        resp.raise_for_status()
        print(f"SMS SENT -> {phone}")
        return True
    except Exception as e:
        print(f"SMS ERROR -> {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Twilio response: {e.response.text}")
        return False

def make_emergency_call(phone, message):
    """Trigger an emergency phone call via the Node.js call microservice
    (server.js), which talks to Twilio Voice. Runs alongside email/SMS as
    an extra notification channel - failure here never blocks the rest."""
    phone = normalize_phone(phone)
    if not phone:
        return False
    call_url = app.config.get("CALL_SERVICE_URL", "").rstrip("/")
    secret = app.config.get("CALL_SERVICE_SECRET", "")
    if not call_url:
        print(f"MOCK CALL (call service not configured) -> {phone}")
        return True
    try:
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-Call-Service-Secret"] = secret
        resp = requests.post(
            f"{call_url}/call",
            json={"to": phone, "message": message},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        print(f"CALL TRIGGERED -> {phone} | {data}")
        return bool(data.get("ok"))
    except Exception as e:
        print(f"CALL ERROR -> {phone} | {e}")
        return False

def log_notification(cursor, alert_id, recipient_name, recipient_type, channel, status):
    """Record the outcome of a single notification attempt (Requirement 16)."""
    try:
        cursor.execute(
            "INSERT INTO ls_notifications (alert_id, recipient_name, recipient_type, channel, status) VALUES (%s,%s,%s,%s,%s)",
            (alert_id, recipient_name, recipient_type, channel, status)
        )
    except Exception as e:
        print(f"NOTIFICATION LOG ERROR -> {e}")

_NTFY_KEYWORDS = {
    "FALL_DETECTED": "FALL_DETECTED",
    "MANUAL_SOS": "SOS_DETECTED",
    "LOW_OXYGEN": "LOW_OXYGEN",
    "ABNORMAL_HEART_RATE": "ABNORMAL_HEART_RATE",
}

def send_ntfy_push(alert_type, hospital_phone):
    """
    Pushes an Android notification via ntfy.sh so MacroDroid on the patient's
    (or guardian's) phone can catch it and auto-dial the hospital. See
    MACRODROID_SETUP.md for the exact macro this pairs with. Body format is
    fixed as `KEYWORD|hospital_phone` per that doc - do not change it without
    updating the macro too.
    """
    server = app.config.get("NTFY_SERVER_URL", "")
    topic = app.config.get("NTFY_TOPIC", "")
    if not server or not topic:
        print("NTFY not configured - skipping push notification")
        return False
    keyword = _NTFY_KEYWORDS.get(alert_type, "EMERGENCY_DETECTED")
    body = f"{keyword}|{hospital_phone or ''}"
    try:
        resp = requests.post(
            f"{server}/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": "LifeShield Emergency", "Priority": "urgent", "Tags": "rotating_light"},
            timeout=10
        )
        resp.raise_for_status()
        print(f"NTFY PUSH SENT -> topic={topic} body={body}")
        return True
    except Exception as e:
        print(f"NTFY PUSH ERROR -> {e}")
        return False

def send_web_push_to_patient(cursor, belt_id, alert_id, alert_type, patient_name, patient_location, hospital_name):
    """Send a browser/PWA push notification to every phone subscribed to this patient."""
    public_key = app.config.get("VAPID_PUBLIC_KEY", "")
    private_key = app.config.get("VAPID_PRIVATE_KEY", "")
    claims_email = app.config.get("VAPID_CLAIMS_EMAIL", "")
    if not webpush or not public_key or not private_key:
        print("WEB PUSH not configured - skipping browser push")
        return 0

    cursor.execute(
        "SELECT subscription_id, endpoint, p256dh, auth FROM ls_push_subscriptions WHERE belt_id_ref=%s",
        (belt_id,)
    )
    subscriptions = cursor.fetchall()
    if not subscriptions:
        print(f"WEB PUSH: no subscribed phones for {belt_id}")
        return 0

    payload = json.dumps({
        "title": "🚨 LifeShield Emergency",
        "body": f"{alert_type.replace('_', ' ')}: {patient_name}. Tap to view location.",
        "url": f"/dashboard?alert_id={alert_id}",
        "tag": f"lifeshield-{alert_id}",
        "alert_id": alert_id,
        "location": patient_location,
        "hospital": hospital_name,
    })

    sent = 0
    stale_ids = []
    for sub in subscriptions:
        try:
            subscription_info = {
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}
            }
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": claims_email if claims_email.startswith("mailto:") else f"mailto:{claims_email}"}
            )
            sent += 1
            log_notification(cursor, alert_id, f"Browser Push ({sub['subscription_id']})", "phone_push", "WEB_PUSH", "SENT")
        except Exception as e:
            print(f"WEB PUSH ERROR -> subscription {sub['subscription_id']}: {e}")
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            if status_code in (404, 410):
                stale_ids.append(sub["subscription_id"])
            log_notification(cursor, alert_id, f"Browser Push ({sub['subscription_id']})", "phone_push", "WEB_PUSH", "FAILED")

    if stale_ids:
        placeholders = ",".join(["%s"] * len(stale_ids))
        cursor.execute(f"DELETE FROM ls_push_subscriptions WHERE subscription_id IN ({placeholders})", tuple(stale_ids))
    return sent

def find_nearest_hospital(lat, lon):
    if not valid_coordinates(lat, lon): return None
    cursor = None
    try:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute(
            "SELECT hospital_id, hospital_name, phone, hospital_email, address, latitude, longitude FROM ls_hospitals "
            "WHERE COALESCE(active,1)=1 AND COALESCE(emergency_available,1)=1"
        )
        nearest = None; nearest_dist = float("inf")
        for h in cursor.fetchall():
            hlat = safe_float(h.get("latitude")); hlon = safe_float(h.get("longitude"))
            if not valid_coordinates(hlat, hlon): continue
            d = calculate_distance(float(lat), float(lon), hlat, hlon)
            if d < nearest_dist:
                nearest_dist = d
                nearest = dict(h)
                nearest["distance_km"] = round(d,2)
                nearest["google_maps_uri"] = f"https://www.google.com/maps?q={hlat},{hlon}"
        return nearest
    except Exception as e:
        print(f"HOSPITAL SEARCH ERROR -> {e}"); return None
    finally:
        if cursor: cursor.close()

def get_active_alert(cursor, belt_id):
    """Requirement 22: find an existing NEW/ACKNOWLEDGED/RESPONDING alert for this patient, if any."""
    cursor.execute(
        "SELECT alert_id, alert_type, status FROM ls_alerts WHERE belt_id_ref=%s AND status IN ('NEW','ACKNOWLEDGED','RESPONDING') ORDER BY created_at DESC LIMIT 1",
        (belt_id,)
    )
    return cursor.fetchone()


def notify_macrodroid_hospital_call(alert_type, hospital):
    """
    Send the nearest hospital phone to MacroDroid through ntfy.
    Safe no-op when no hospital/phone is available.
    """
    if not hospital:
        print("MACRODROID: no nearest hospital")
        return False

    phone = ""
    if isinstance(hospital, dict):
        phone = (
            hospital.get("phone")
            or hospital.get("hospital_phone")
            or hospital.get("contact")
            or ""
        )
    else:
        phone = str(hospital)

    if not phone:
        print("MACRODROID: hospital has no phone number")
        return False

    return send_ntfy_push(alert_type, phone)


def process_emergency(belt_id, alert_type, location=None):
    """
    Shared emergency processor (Requirement 21).
    Both trigger_fall() and trigger_sos() funnel into this single function:
      1. Patient identification
      2. Location (GPS if provided, else last known)
      3. Emergency creation (or reuse of an active one - duplicate protection)
      4. Hospital selection
      5. Contact retrieval
      6. Notification sending (contacts + hospital, independently, failures isolated)
      7. Hospital notification
      8. Notification logging
      9. Database updates
      10. Response to frontend
    Runs in a background thread so the endpoint returns immediately; the
    caller endpoint separately returns a fast synchronous ack to the frontend.
    """
    def _run():
        with app.app_context():
            cursor = None
            try:
                cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

                # 1. Patient identification
                cursor.execute(
                    "SELECT belt_id, full_name, email, last_bpm, last_spo2, last_temperature, posture, latitude, longitude "
                    "FROM ls_patients WHERE TRIM(belt_id)=%s", (belt_id.strip(),)
                )
                patient = cursor.fetchone()
                if not patient:
                    print(f"PATIENT NOT FOUND IN ALERT -> {belt_id}"); return
                real_belt_id = patient.get("belt_id")

                # 2. Location: use fresh GPS if given & valid, else fall back to patient's last known location
                lat = lon = None
                location_is_last_known = False
                if location and valid_coordinates(location.get("latitude"), location.get("longitude")):
                    lat = safe_float(location.get("latitude")); lon = safe_float(location.get("longitude"))
                    cursor.execute("UPDATE ls_patients SET latitude=%s, longitude=%s WHERE belt_id=%s", (lat, lon, real_belt_id))
                elif valid_coordinates(patient.get("latitude"), patient.get("longitude")):
                    lat = patient.get("latitude"); lon = patient.get("longitude")
                    location_is_last_known = True
                # else: no GPS available at all - lat/lon stay None, never faked (Requirement 10)

                patient_location = f"https://www.google.com/maps?q={lat},{lon}" if valid_coordinates(lat, lon) else "Location unavailable"
                if valid_coordinates(lat, lon) and location_is_last_known:
                    patient_location += " (last known location)"

                # 3. Emergency creation, with duplicate protection (Requirement 22)
                existing = get_active_alert(cursor, real_belt_id)
                if existing:
                    alert_id = existing["alert_id"]
                    cursor.execute(
                        "UPDATE ls_alerts SET latitude=%s, longitude=%s WHERE alert_id=%s",
                        (lat, lon, alert_id)
                    )
                    mysql.connection.commit()
                    print(f"DUPLICATE EMERGENCY SUPPRESSED -> reusing active alert #{alert_id} for {real_belt_id}")
                else:
                    cursor.execute(
                        "INSERT INTO ls_alerts (belt_id_ref, alert_type, message, latitude, longitude, status) "
                        "VALUES (%s,%s,%s,%s,%s,'NEW')",
                        (real_belt_id, alert_type, "", lat, lon)
                    )
                    mysql.connection.commit()
                    alert_id = cursor.lastrowid

                cursor.execute("UPDATE ls_patients SET is_emergency=1 WHERE belt_id=%s", (real_belt_id,))
                mysql.connection.commit()

                # 4. Hospital selection (nearest, by GPS)
                hospital = None
                hospital_lookup_failed = False
                if valid_coordinates(lat, lon):
                    hospital = find_nearest_hospital(lat, lon)
                    if not hospital:
                        hospital_lookup_failed = True
                else:
                    hospital_lookup_failed = True

                if hospital:
                    hospital_name = hospital.get("hospital_name"); hospital_phone = hospital.get("phone") or "Unavailable"
                    hospital_email = hospital.get("hospital_email"); hospital_dist = hospital.get("distance_km")
                    hospital_map = hospital.get("google_maps_uri")
                    cursor.execute("UPDATE ls_alerts SET hospital_id=%s WHERE alert_id=%s", (hospital.get("hospital_id"), alert_id))
                    mysql.connection.commit()
                else:
                    hospital_name = "No hospital found"; hospital_phone = "Unavailable"
                    hospital_email = None; hospital_dist = "Unavailable"; hospital_map = "Unavailable"

                # 5. Contact retrieval - all active contacts + registered guardians (Requirement 4, 5)
                cursor.execute(
                    "SELECT guardian_name, guardian_email, guardian_phone FROM ls_guardians WHERE TRIM(linked_belt_id)=%s",
                    (real_belt_id,)
                )
                guardians = cursor.fetchall()
                cursor.execute(
                    "SELECT contact_name, contact_phone, contact_email, relationship FROM ls_contacts "
                    "WHERE TRIM(belt_id_ref)=%s AND active=1",
                    (real_belt_id,)
                )
                contacts = cursor.fetchall()

                recipients = []
                for g in guardians:
                    recipients.append({"name": g.get("guardian_name"), "email": g.get("guardian_email"), "phone": g.get("guardian_phone")})
                for c in contacts:
                    recipients.append({"name": c.get("contact_name"), "email": c.get("contact_email"), "phone": c.get("contact_phone")})

                unique = []; seen = set()
                for r in recipients:
                    key = (r.get("email"), r.get("phone"))
                    if key in seen: continue
                    if not r.get("email") and not r.get("phone"): continue
                    seen.add(key); unique.append(r)

                when = patient.get("last_updated")
                timestamp_str = when.strftime("%Y-%m-%d %H:%M:%S") if hasattr(when, "strftime") else str(when or "")

                contact_message = (
                    f"LIFESHIELD EMERGENCY ALERT\n"
                    f"Patient: {patient.get('full_name')}\n"
                    f"Emergency: {alert_type}\n"
                    f"Time: {timestamp_str}\n"
                    f"Location: {patient_location}\n"
                    f"Nearest Hospital: {hospital_name}\n"
                    f"Please respond immediately."
                )

                print(f"\nEMERGENCY #{alert_id}: {patient.get('full_name')} ({real_belt_id}) - {alert_type} - Contacts: {len(unique)}")

                # 6. Notify every contact independently - one failure never blocks the rest (Requirement 4, 16, 23)
                for rec in unique:
                    if rec.get("email"):
                        ok = send_emergency_email(rec["email"], f"LIFESHIELD - {alert_type}", contact_message)
                        log_notification(cursor, alert_id, rec.get("name") or rec["email"], "contact", "EMAIL", "SENT" if ok else "FAILED")
                    if rec.get("phone"):
                        ok = send_hospital_sms(rec["phone"], contact_message)
                        log_notification(cursor, alert_id, rec.get("name") or rec["phone"], "contact", "SMS", "SENT" if ok else "FAILED")
                        call_ok = make_emergency_call(rec["phone"], contact_message)
                        log_notification(cursor, alert_id, rec.get("name") or rec["phone"], "contact", "CALL", "SENT" if call_ok else "FAILED")

                # 7. Notify the nearest hospital directly (Requirement 9)
                if hospital:
                    hospital_message = (
                        f"LIFESHIELD MEDICAL EMERGENCY\n"
                        f"Patient: {patient.get('full_name')}\n"
                        f"Emergency Type: {alert_type}\n"
                        f"Time: {timestamp_str}\n"
                        f"Patient Location: {patient_location}"
                    )
                    if hospital_email:
                        ok = send_emergency_email(hospital_email, f"LIFESHIELD MEDICAL EMERGENCY - {alert_type}", hospital_message)
                        log_notification(cursor, alert_id, hospital_name, "hospital", "EMAIL", "SENT" if ok else "FAILED")
                    if hospital.get("phone"):
                        ok = send_hospital_sms(hospital.get("phone"), hospital_message)
                        log_notification(cursor, alert_id, hospital_name, "hospital", "SMS", "SENT" if ok else "FAILED")
                        call_ok = make_emergency_call(hospital.get("phone"), hospital_message)
                        log_notification(cursor, alert_id, hospital_name, "hospital", "CALL", "SENT" if call_ok else "FAILED")
                    if not hospital_email and not hospital.get("phone"):
                        log_notification(cursor, alert_id, hospital_name, "hospital", "NONE", "FAILED")
                else:
                    # Requirement 24: hospital lookup failure should not block contact notifications,
                    # which have already been sent above; just record that lookup failed.
                    log_notification(cursor, alert_id, "No hospital found", "hospital", "NONE", "FAILED")

                # 7b. Push an Android notification via ntfy so MacroDroid on a phone can
                # catch it and auto-dial the hospital (see MACRODROID_SETUP.md). This is
                # the "alert on my phone" path, separate from SMS/email above.
                push_phone = hospital.get("phone") if hospital else ""
                ok = send_ntfy_push(alert_type, push_phone)
                log_notification(cursor, alert_id, "MacroDroid (ntfy push)", "phone_push", "NTFY", "SENT" if ok else "FAILED")

                # Browser/PWA push: real OS notification popup on every subscribed phone.
                send_web_push_to_patient(
                    cursor, real_belt_id, alert_id, alert_type,
                    patient.get("full_name") or "Patient", patient_location, hospital_name
                )

                # 8/9. Persist final message + commit notification log
                cursor.execute("UPDATE ls_alerts SET message=%s WHERE alert_id=%s", (contact_message, alert_id))
                mysql.connection.commit()
                print(f"ALERT #{alert_id} SAVED (hospital_lookup_failed={hospital_lookup_failed})")
            except Exception as e:
                print(f"BACKGROUND ERROR: {e}"); traceback.print_exc()
            finally:
                if cursor: cursor.close()
    threading.Thread(target=_run, daemon=True).start()

# Backwards-compatible alias - existing call sites use this name.
def trigger_logic_alerts(belt_id, alert_type, notify_hospital=True, location=None):
    process_emergency(belt_id, alert_type, location=location)

def _ensure_column(cursor, table, column, ddl):
    """Add a column to an existing table if it doesn't already exist (safe schema migration)."""
    try:
        cursor.execute(
            "SELECT COUNT(*) AS c FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
            (table, column)
        )
        row = cursor.fetchone()
        exists = (row["c"] if isinstance(row, dict) else row[0]) > 0
        if not exists:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            mysql.connection.commit()
            print(f"MIGRATED -> added {table}.{column}")
    except Exception as e:
        mysql.connection.rollback()
        print(f"MIGRATION WARNING ({table}.{column}) -> {e}")

def init_db():
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute("""CREATE TABLE IF NOT EXISTS ls_patients (belt_id VARCHAR(50) PRIMARY KEY, full_name VARCHAR(100) NOT NULL, email VARCHAR(100) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL, medical_condition TEXT, last_bpm INT DEFAULT 0, last_spo2 INT DEFAULT 0, last_temperature FLOAT DEFAULT 98.6, fall_status INT DEFAULT 0, posture VARCHAR(30) DEFAULT 'Standing', is_emergency INT DEFAULT 0, latitude DOUBLE DEFAULT NULL, longitude DOUBLE DEFAULT NULL, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS ls_guardians (id INT AUTO_INCREMENT PRIMARY KEY, guardian_name VARCHAR(100) NOT NULL, guardian_email VARCHAR(100) UNIQUE NOT NULL, guardian_password VARCHAR(255) NOT NULL, guardian_phone VARCHAR(30), linked_belt_id VARCHAR(50), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (linked_belt_id) REFERENCES ls_patients(belt_id) ON DELETE SET NULL)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS ls_contacts (contact_id INT AUTO_INCREMENT PRIMARY KEY, belt_id_ref VARCHAR(50) NOT NULL, contact_name VARCHAR(100) NOT NULL, contact_phone VARCHAR(30) NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (belt_id_ref) REFERENCES ls_patients(belt_id) ON DELETE CASCADE)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS ls_vitals (id INT AUTO_INCREMENT PRIMARY KEY, belt_id_ref VARCHAR(50) NOT NULL, bpm INT, spo2 INT, temperature FLOAT, recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (belt_id_ref) REFERENCES ls_patients(belt_id) ON DELETE CASCADE)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS ls_alerts (alert_id INT AUTO_INCREMENT PRIMARY KEY, belt_id_ref VARCHAR(50) NOT NULL, alert_type VARCHAR(100), message TEXT, latitude DOUBLE, longitude DOUBLE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (belt_id_ref) REFERENCES ls_patients(belt_id) ON DELETE CASCADE)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS ls_hospitals (hospital_id INT AUTO_INCREMENT PRIMARY KEY, hospital_name VARCHAR(150) NOT NULL, phone VARCHAR(30), hospital_email VARCHAR(255), address TEXT, latitude DOUBLE NOT NULL, longitude DOUBLE NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS ls_notifications (notification_id INT AUTO_INCREMENT PRIMARY KEY, alert_id INT NOT NULL, recipient_name VARCHAR(150), recipient_type VARCHAR(20), channel VARCHAR(20), status VARCHAR(20) DEFAULT 'PENDING', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (alert_id) REFERENCES ls_alerts(alert_id) ON DELETE CASCADE)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS ls_push_subscriptions (
            subscription_id INT AUTO_INCREMENT PRIMARY KEY,
            belt_id_ref VARCHAR(50) NOT NULL,
            endpoint TEXT NOT NULL,
            p256dh VARCHAR(255) NOT NULL,
            auth VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_push_endpoint (endpoint(255)),
            INDEX idx_push_belt (belt_id_ref),
            FOREIGN KEY (belt_id_ref) REFERENCES ls_patients(belt_id) ON DELETE CASCADE
        )""")
        mysql.connection.commit()

        # Schema extensions on top of pre-existing tables (Requirement 17: extend, don't replace)
        _ensure_column(cursor, "ls_alerts", "status", "status VARCHAR(20) DEFAULT 'NEW'")
        _ensure_column(cursor, "ls_alerts", "hospital_id", "hospital_id INT DEFAULT NULL")
        _ensure_column(cursor, "ls_alerts", "acknowledged_at", "acknowledged_at TIMESTAMP NULL DEFAULT NULL")
        _ensure_column(cursor, "ls_alerts", "resolved_at", "resolved_at TIMESTAMP NULL DEFAULT NULL")
        _ensure_column(cursor, "ls_contacts", "contact_email", "contact_email VARCHAR(150) DEFAULT NULL")
        _ensure_column(cursor, "ls_contacts", "relationship", "relationship VARCHAR(50) DEFAULT NULL")
        _ensure_column(cursor, "ls_contacts", "active", "active TINYINT(1) DEFAULT 1")
        _ensure_column(cursor, "ls_hospitals", "emergency_available", "emergency_available TINYINT(1) DEFAULT 1")
        _ensure_column(cursor, "ls_hospitals", "active", "active TINYINT(1) DEFAULT 1")

        # Seed real Nagpur hospitals (25) so find_nearest_hospital() has a
        # realistic set to match against regardless of demo location. Emails
        # and phone numbers come from a user-supplied dataset of verified
        # public hospital contacts; coordinates were looked up separately.
        # Safe to re-run - only inserts a hospital if one with the same name
        # doesn't already exist.
        seed_hospitals = [
            ("Wockhardt Super Specialty Hospital, Nagpur", "07126624444", "callcenter.ngp@wockhardthospitals.com",
             "Daga College, 1643, North Ambazari Rd, beside Lady Amritbai, Shankar Nagar, Nagpur, Maharashtra 440033",
             21.134687, 79.05857449999999),
            ("Max Super Speciality Hospital, Nagpur", "07127120000", "vivek.dwivedi@maxhealthcare.com",
             "232, Mankapur, Koradi Road, Byramji Town, Nagpur, Maharashtra 440030",
             21.1857573, 79.0795349),
            ("KRIMS Hospitals", "07122451188", "info@krimshospitals.com",
             "275, Central Bazar Road, New Ramdaspeth, Nagpur, Maharashtra 440010",
             21.132815, 79.070385),
            ("Orange City Hospital & Research Institute", "07122238431", "ochri.ngp@gmail.com",
             "19, Khamla Road, Veer Sawarkar Square, opposite Jupiter College, Nagpur, Maharashtra 440015",
             21.1128464, 79.0654763),
            ("Arihant Multispeciality Hospital", "07126656666", "info@arihanthospitals.co.in",
             "Baidyanath Square, opposite Capitol Heights / VR Mall, Nagpur, Maharashtra 440026",
             21.1346121, 79.0968072),
            ("CARE Hospitals, Nagpur", "07123067777", "care@carehospitals.com",
             "3, Farmland Road, Panchsheel Square, Nagpur, Maharashtra 440012",
             21.138545, 79.0791634),
            ("Lata Mangeshkar Hospital", "07122530347", "info@lmhospital.com",
             "YMCA Complex, Maharajbagh Road, Sitabuldi, Nagpur, Maharashtra 440001",
             21.1450255, 79.0801726),
            ("Midas Multispeciality Hospital Pvt. Ltd.", "07122430511", "info@midashospital.com",
             "Midas Heights, 7, Central Bazar Road, Ramdaspeth, Nagpur, Maharashtra 440010",
             21.133973800000003, 79.0755831),
            ("Synergy Multispeciality Hospital", "7447889998", None,
             "Plot 42 & 43, near Palloti School, Gorewada, Mankapur Ring Road, Nagpur, Maharashtra 440013",
             21.1879901, 79.0586673),
            ("Integrity Hospital", "9801980100", None,
             "Plot No. 05, Vinoba Nagar Samasya Nivaran Cooperative Housing Society Layout, Dighori, Nagpur, Maharashtra 440024",
             21.1211143, 79.13623679999999),
            ("Varunam Superspeciality Hospital", "7447799000", None,
             "Aditya Enclave, 20-A, Central Bazar Road, opposite Somalwar High School, Ramdaspeth, Nagpur, Maharashtra 440010",
             21.133684799999997, 79.07459),
            ("Vedanta Super Speciality Hospital", "07122999962", None,
             "First Floor, Shree Radhey Heights, opposite Neeti-Gaurav Complex, Ramdaspeth, Nagpur, Maharashtra 440010",
             21.1347623, 79.077426),
            ("Abhinav Multispeciality Hospital", "07122641715", None,
             "10 No. Puliya, opposite Swastik School, Lashkari Bagh, Nagpur, Maharashtra 440017",
             21.169109199999998, 79.09749440000002),
            ("Star Superspeciality Hospital", "7276500684", None,
             "Besides Hardeo Hotel, Sitabuldi, Nagpur, Maharashtra 440012",
             21.1406368, 79.0856482),
            ("Super Speciality Hospital", "07122750123", None,
             "Hanuman Nagar, Manewada Road, near Rashtra Sant Tukdoji Maharaj GMC, Wanjari Nagar, Nagpur, Maharashtra 440003",
             21.1239573, 79.1024418),
            ("Suretech Hospital", "9922522345", "suretechhospital@gmail.com",
             "13/A Bannerjee Road, Dhantoli, Nagpur, Maharashtra 440012",
             21.137287600000004, 79.0799918),
            ("Platina Heart Hospital", "07122566555", "platinahearthospitalnagpur@gmail.com",
             "2nd & 3rd Floor, Dhanashree Commercial Complex, near Hotel Hardeo, Sitabuldi, Nagpur, Maharashtra 440012",
             21.1405408, 79.08542369999999),
            ("Criticare Hospital & Research Institute", "07122522281", "criticarehospital2008@gmail.com",
             "4th Floor, Dhanshree Complex, near Hotel Hardeo, Sitabuldi, Nagpur, Maharashtra 440012",
             21.140557599999998, 79.0854809),
            ("Government Medical College & Hospital", "07122701642", "deangmc2@gmail.com",
             "Medical Square, Nagpur, Maharashtra 440009",
             21.126003, 79.0970024),
            ("Indira Gandhi Government Medical College & Hospital", "07122725274", "igmcn@rediffmail.com",
             "Central Avenue Road, Nagpur, Maharashtra 440018",
             21.1536811, 79.0936979),
            ("Daga Memorial Hospital", "07122729333", "msdaga_womenhosp@rediffmail.com",
             "Near Agrasen Square, Gandhibagh, Nagpur, Maharashtra 440018",
             21.1531162, 79.1034421),
            ("Government Dental College & Hospital", "07122744496", "dean.gdcngp@gmail.com",
             "Medical College Premises, Medical Square, Nagpur, Maharashtra 440009",
             21.1274308, 79.0963764),
            ("Government Ayurvedic College & Hospital", "07122449198", "govtayurcollegenagpur@gmail.com",
             "Chota Tajbag Road, opposite Kamala Nehru College, Sakkardara Square, Nagpur, Maharashtra 440024",
             21.1269465, 79.1135087),
            ("New Era Hospital & Research Institute", "07122764544", "newerahospitalngp@gmail.com",
             "Central Avenue Road, near Telephone Exchange Chowk, Queta Colony, near Jalaram Mandir, Nagpur, Maharashtra 440008",
             21.1516843, 79.0879028),
            ("Niramay Hospital", "07122745931", "niramayhospi@gmail.com",
             "518, Untkhana Medical College Road, Nagpur, Maharashtra 440009",
             21.133999, 79.1008222),
        ]
        for name, phone, email, address, lat, lon in seed_hospitals:
            cursor.execute("SELECT hospital_id FROM ls_hospitals WHERE hospital_name=%s", (name,))
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO ls_hospitals (hospital_name, phone, hospital_email, address, latitude, longitude, emergency_available, active) "
                    "VALUES (%s,%s,%s,%s,%s,%s,1,1)",
                    (name, phone, email, address, lat, lon)
                )
        mysql.connection.commit()

        print("DATABASE INITIALIZED")
    except Exception as ex:
        mysql.connection.rollback()
        print(f"DB INIT ERROR: {ex}")
    finally:
        cursor.close()

with app.app_context():
    try: init_db()
    except Exception as e: print(f"INIT ERROR: {e}")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/get_vitals/<belt_id>", methods=["GET"])
def get_vitals(belt_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute("SELECT belt_id, full_name AS patient_name, last_bpm AS bpm, last_spo2 AS spo2, last_temperature AS temperature, fall_status AS fallen, posture, is_emergency, latitude, longitude, last_updated FROM ls_patients WHERE TRIM(belt_id)=%s", (belt_id.strip(),))
        patient = cursor.fetchone()
        if not patient: return jsonify({"status":"error","message":"Patient not found"}),404
        if patient.get("last_updated"): patient["last_updated"] = patient["last_updated"].strftime("%Y-%m-%d %H:%M:%S")
        return jsonify(patient),200
    finally: cursor.close()

@app.route("/update_location", methods=["POST"])
def update_location():
    data = request.get_json(silent=True) or {}
    belt_id = str(data.get("belt_id","")).strip()
    lat = safe_float(data.get("latitude")); lon = safe_float(data.get("longitude"))
    if not belt_id: return jsonify({"status":"error","message":"Belt ID required"}),400
    if not valid_coordinates(lat, lon): return jsonify({"status":"error","message":"Invalid GPS"}),400
    cursor = None
    try:
        cursor = mysql.connection.cursor()
        cursor.execute("UPDATE ls_patients SET latitude=%s, longitude=%s WHERE TRIM(belt_id)=%s", (lat, lon, belt_id.strip()))
        if cursor.rowcount == 0: return jsonify({"status":"error","message":f"Patient not found: {belt_id}"}),404
        mysql.connection.commit()
        return jsonify({"status":"success","belt_id":belt_id}),200
    except Exception as e:
        if cursor: mysql.connection.rollback()
        return jsonify({"status":"error","message":str(e)}),500
    finally:
        if cursor: cursor.close()

@app.route("/update_vitals", methods=["POST"])
def update_vitals():
    data = request.get_json(silent=True) or {}
    belt_id = str(data.get("belt_id","")).strip()
    bpm = safe_int(data.get("bpm"),0); spo2 = safe_int(data.get("spo2"),0); temperature = safe_float(data.get("temperature"),98.6)
    fallen = safe_int(data.get("fallen"),0); posture = str(data.get("posture","Standing"))
    latitude = safe_float(data.get("latitude")); longitude = safe_float(data.get("longitude"))
    if not belt_id: return jsonify({"status":"error","message":"belt_id required"}),400
    cursor = None
    try:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT belt_id, fall_status FROM ls_patients WHERE TRIM(belt_id)=%s", (belt_id,))
        patient = cursor.fetchone()
        if not patient: return jsonify({"status":"error","message":f"Patient not found: {belt_id}"}),404
        previous_fall = safe_int(patient.get("fall_status"),0)
        real_belt_id = patient.get("belt_id")
        gps_valid = valid_coordinates(latitude, longitude)
        if gps_valid:
            cursor.execute("UPDATE ls_patients SET last_bpm=%s, last_spo2=%s, last_temperature=%s, fall_status=%s, posture=%s, latitude=%s, longitude=%s WHERE TRIM(belt_id)=%s", (bpm, spo2, temperature, fallen, posture, latitude, longitude, belt_id))
        else:
            cursor.execute("UPDATE ls_patients SET last_bpm=%s, last_spo2=%s, last_temperature=%s, fall_status=%s, posture=%s WHERE TRIM(belt_id)=%s", (bpm, spo2, temperature, fallen, posture, belt_id))
        # FIX 2: Corrected vitals insert logic
        cursor.execute("INSERT INTO ls_vitals (belt_id_ref, bpm, spo2, temperature) VALUES (%s,%s,%s,%s)", (real_belt_id, bpm, spo2, temperature))
        mysql.connection.commit()
        if fallen == 1 and previous_fall == 0:
            cursor.execute("UPDATE ls_patients SET is_emergency=1, fall_status=1, posture='Fallen' WHERE TRIM(belt_id)=%s", (belt_id,))
            mysql.connection.commit()
            process_emergency(real_belt_id, "FALL_DETECTED", location={"latitude": latitude, "longitude": longitude} if gps_valid else None)
        return jsonify({"status":"success"}),200
    except Exception as e:
        if cursor: mysql.connection.rollback()
        traceback.print_exc()
        return jsonify({"status":"error","message":str(e)}),500
    finally:
        if cursor: cursor.close()

def _resolve_belt_id(data):
    belt_id = str(data.get("belt_id","")).strip()
    if not belt_id:
        belt_id = str(session.get("linked_belt_id") or session.get("user_id") or "").strip()
    return belt_id

@app.route("/trigger_sos", methods=["POST"])
def trigger_sos():
    """Manual SOS button (Requirement 3, 19). Existing SOS button already POSTs here."""
    data = request.get_json(silent=True) or {}
    if not data:
        try: data = json.loads(request.get_data(as_text=True) or "{}")
        except: data = {}
    belt_id = _resolve_belt_id(data)
    if not belt_id:
        return jsonify({"status":"error","message":"Belt ID required"}),400

    cursor = None
    try:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT belt_id FROM ls_patients WHERE TRIM(belt_id)=%s", (belt_id,))
        found = cursor.fetchone()
        if not found:
            cursor.execute("SELECT belt_id FROM ls_patients")
            all_ids = [r["belt_id"] for r in cursor.fetchall()]
            print(f"PATIENT NOT FOUND: '{belt_id}' Existing: {all_ids}")
            return jsonify({"status":"error","message":f"Patient not found: {belt_id}. Existing IDs: {all_ids}"}),404

        real_id = found["belt_id"]
        cursor.execute("UPDATE ls_patients SET is_emergency=1 WHERE belt_id=%s", (real_id,))
        mysql.connection.commit()

        lat = data.get("latitude"); lon = data.get("longitude")
        location = {"latitude": lat, "longitude": lon} if valid_coordinates(lat, lon) else None
        process_emergency(real_id, "MANUAL_SOS", location=location)
        return jsonify({"status":"SOS_SENT","message":f"SOS sent for {real_id}","belt_id":real_id}),200
    except Exception as e:
        if cursor: mysql.connection.rollback()
        traceback.print_exc()
        return jsonify({"status":"error","message":str(e)}),500
    finally:
        if cursor: cursor.close()

@app.route("/trigger_fall", methods=["POST"])
def trigger_fall():
    """
    Explicit fall-trigger endpoint (Requirement 18), for manual demo/testing
    or any device integration that prefers a dedicated route instead of
    reporting through /update_vitals. Runs the same shared emergency pipeline.
    """
    data = request.get_json(silent=True) or {}
    belt_id = _resolve_belt_id(data)
    if not belt_id:
        return jsonify({"status":"error","message":"Belt ID required"}),400

    cursor = None
    try:
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cursor.execute("SELECT belt_id FROM ls_patients WHERE TRIM(belt_id)=%s", (belt_id,))
        found = cursor.fetchone()
        if not found:
            return jsonify({"status":"error","message":f"Patient not found: {belt_id}"}),404

        real_id = found["belt_id"]
        cursor.execute("UPDATE ls_patients SET is_emergency=1, fall_status=1, posture='Fallen' WHERE belt_id=%s", (real_id,))
        mysql.connection.commit()

        lat = data.get("latitude"); lon = data.get("longitude")
        location = {"latitude": lat, "longitude": lon} if valid_coordinates(lat, lon) else None
        process_emergency(real_id, "FALL_DETECTED", location=location)
        return jsonify({"status":"FALL_SENT","message":f"Fall alert sent for {real_id}","belt_id":real_id}),200
    except Exception as e:
        if cursor: mysql.connection.rollback()
        traceback.print_exc()
        return jsonify({"status":"error","message":str(e)}),500
    finally:
        if cursor: cursor.close()

@app.route("/emergency/<int:alert_id>", methods=["GET"])
def get_emergency(alert_id):
    """Retrieve full detail for one emergency event (Requirement 18)."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute(
            "SELECT a.*, p.full_name AS patient_name, h.hospital_name, h.phone AS hospital_phone "
            "FROM ls_alerts a "
            "LEFT JOIN ls_patients p ON p.belt_id = a.belt_id_ref "
            "LEFT JOIN ls_hospitals h ON h.hospital_id = a.hospital_id "
            "WHERE a.alert_id=%s", (alert_id,)
        )
        alert = cursor.fetchone()
        if not alert: return jsonify({"status":"error","message":"Emergency not found"}),404
        for k in ("created_at","acknowledged_at","resolved_at"):
            if alert.get(k) and hasattr(alert[k], "strftime"): alert[k] = alert[k].strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "SELECT recipient_name, recipient_type, channel, status, created_at FROM ls_notifications WHERE alert_id=%s ORDER BY created_at",
            (alert_id,)
        )
        notifications = cursor.fetchall()
        for n in notifications:
            if n.get("created_at") and hasattr(n["created_at"], "strftime"): n["created_at"] = n["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        alert["notifications"] = notifications
        if alert.get("latitude") and alert.get("longitude"):
            alert["map_link"] = f"https://www.google.com/maps?q={alert['latitude']},{alert['longitude']}"
        return jsonify({"status":"success","emergency":alert}),200
    finally: cursor.close()

@app.route("/emergency/active", methods=["GET"])
def list_active_emergencies():
    """Retrieve all currently-active emergencies (Requirement 18), optionally filtered by belt_id."""
    belt_id = request.args.get("belt_id","").strip()
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        if belt_id:
            cursor.execute(
                "SELECT a.*, p.full_name AS patient_name FROM ls_alerts a "
                "LEFT JOIN ls_patients p ON p.belt_id = a.belt_id_ref "
                "WHERE a.status IN ('NEW','ACKNOWLEDGED','RESPONDING') AND TRIM(a.belt_id_ref)=%s ORDER BY a.created_at DESC",
                (belt_id,)
            )
        else:
            cursor.execute(
                "SELECT a.*, p.full_name AS patient_name FROM ls_alerts a "
                "LEFT JOIN ls_patients p ON p.belt_id = a.belt_id_ref "
                "WHERE a.status IN ('NEW','ACKNOWLEDGED','RESPONDING') ORDER BY a.created_at DESC"
            )
        rows = cursor.fetchall()
        for r in rows:
            if r.get("created_at") and hasattr(r["created_at"], "strftime"): r["created_at"] = r["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        return jsonify({"status":"success","active_emergencies":rows}),200
    finally: cursor.close()

@app.route("/emergency/history/<belt_id>", methods=["GET"])
def emergency_history(belt_id):
    """Retrieve full emergency history for a patient (Requirement 18)."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute(
            "SELECT alert_id, alert_type, status, latitude, longitude, created_at, resolved_at FROM ls_alerts "
            "WHERE TRIM(belt_id_ref)=%s ORDER BY created_at DESC LIMIT 100",
            (belt_id.strip(),)
        )
        rows = cursor.fetchall()
        for r in rows:
            for k in ("created_at","resolved_at"):
                if r.get(k) and hasattr(r[k], "strftime"): r[k] = r[k].strftime("%Y-%m-%d %H:%M:%S")
        return jsonify({"status":"success","history":rows}),200
    finally: cursor.close()

@app.route("/emergency/<int:alert_id>/status", methods=["POST"])
def update_emergency_status(alert_id):
    """Update emergency status (Requirement 14): NEW/ACKNOWLEDGED/RESPONDING/RESOLVED/CANCELLED."""
    data = request.get_json(silent=True) or {}
    new_status = str(data.get("status","")).strip().upper()
    valid_statuses = {"NEW","ACKNOWLEDGED","RESPONDING","RESOLVED","CANCELLED"}
    if new_status not in valid_statuses:
        return jsonify({"status":"error","message":f"status must be one of {sorted(valid_statuses)}"}),400
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute("SELECT belt_id_ref FROM ls_alerts WHERE alert_id=%s", (alert_id,))
        row = cursor.fetchone()
        if not row: return jsonify({"status":"error","message":"Emergency not found"}),404

        if new_status == "ACKNOWLEDGED":
            cursor.execute("UPDATE ls_alerts SET status=%s, acknowledged_at=CURRENT_TIMESTAMP WHERE alert_id=%s", (new_status, alert_id))
        elif new_status in ("RESOLVED","CANCELLED"):
            cursor.execute("UPDATE ls_alerts SET status=%s, resolved_at=CURRENT_TIMESTAMP WHERE alert_id=%s", (new_status, alert_id))
            # Clear the patient's active-emergency flag once resolved (Requirement 22: allows a new emergency afterwards)
            cursor.execute("UPDATE ls_patients SET is_emergency=0 WHERE belt_id=%s", (row["belt_id_ref"],))
        else:
            cursor.execute("UPDATE ls_alerts SET status=%s WHERE alert_id=%s", (new_status, alert_id))
        mysql.connection.commit()
        return jsonify({"status":"success","alert_id":alert_id,"new_status":new_status}),200
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"status":"error","message":str(e)}),500
    finally: cursor.close()

@app.route("/test_hospital/<belt_id>", methods=["GET"])
def test_hospital(belt_id):
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute("SELECT belt_id, full_name, latitude, longitude FROM ls_patients WHERE TRIM(belt_id)=%s", (belt_id.strip(),))
        patient = cursor.fetchone()
        if not patient: return jsonify({"status":"error","message":f"Patient not found: {belt_id}"}),404
        if not valid_coordinates(patient.get("latitude"), patient.get("longitude")):
            return jsonify({"status":"error","message":"Patient GPS unavailable"}),400
        hospital = find_nearest_hospital(patient.get("latitude"), patient.get("longitude"))
        if not hospital: return jsonify({"status":"error","message":"No hospitals found"}),404
        return jsonify({"status":"success","patient":patient,"nearest_hospital":hospital}),200
    finally: cursor.close()

@app.route("/register/patient", methods=["GET","POST"])
def register_patient():
    if request.method == "POST":
        belt_id = request.form.get("patient_id","").strip()
        name = request.form.get("full_name","").strip()
        email = request.form.get("email","").strip()
        password = request.form.get("password","")
        if not all([belt_id,name,email,password]):
            flash("Fill all fields","danger"); return render_template("register.html")
        cursor = mysql.connection.cursor()
        try:
            cursor.execute("INSERT INTO ls_patients (belt_id, full_name, email, password_hash) VALUES (%s,%s,%s,%s)", (belt_id, name, email, generate_password_hash(password)))
            mysql.connection.commit()
            flash("Patient registered","success"); return redirect(url_for("login"))
        except Exception as e:
            mysql.connection.rollback(); flash(f"Error: {e}","danger")
        finally: cursor.close()
    return render_template("register.html")

@app.route("/register/guardian", methods=["GET","POST"])
def register_guardian():
    if request.method == "POST":
        name = request.form.get("g_full_name","").strip()
        email = request.form.get("g_email","").strip()
        belt_id = request.form.get("g_patient_id","").strip()
        password = request.form.get("g_password","")
        phone = request.form.get("g_phone","").strip()
        if not all([name,email,belt_id,password]):
            flash("Fill all fields","danger"); return render_template("register.html")
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            cursor.execute("SELECT belt_id FROM ls_patients WHERE TRIM(belt_id)=%s", (belt_id,))
            if not cursor.fetchone():
                flash(f"Patient ID {belt_id} does not exist","danger"); return render_template("register.html")
            cursor.execute("INSERT INTO ls_guardians (guardian_name, guardian_email, guardian_password, guardian_phone, linked_belt_id) VALUES (%s,%s,%s,%s,%s)", (name, email, generate_password_hash(password), phone, belt_id))
            mysql.connection.commit()
            flash("Guardian registered","success"); return redirect(url_for("login"))
        except Exception as e:
            mysql.connection.rollback(); flash(f"Error: {e}","danger")
        finally: cursor.close()
    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email","").strip()
        password = request.form.get("password","")
        cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            cursor.execute("SELECT * FROM ls_patients WHERE email=%s", (email,))
            user = cursor.fetchone(); user_type = "patient" if user else None
            if not user:
                cursor.execute("SELECT * FROM ls_guardians WHERE guardian_email=%s", (email,))
                user = cursor.fetchone(); user_type = "guardian" if user else None
            if user:
                ph = user["password_hash"] if user_type=="patient" else user["guardian_password"]
                uid = user["belt_id"] if user_type=="patient" else user["id"]
                uname = user["full_name"] if user_type=="patient" else user["guardian_name"]
                if check_password_hash(ph, password):
                    session.clear()
                    session["user_type"] = user_type; session["user_id"] = uid; session["user_name"] = uname
                    if user_type == "guardian": session["linked_belt_id"] = user.get("linked_belt_id")
                    return redirect(url_for("dashboard"))
            flash("Invalid login","danger")
        finally: cursor.close()
    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session: return redirect(url_for("login"))
    user_type = session["user_type"]
    belt_id = session["user_id"] if user_type=="patient" else session.get("linked_belt_id")
    if not belt_id: return redirect(url_for("logout"))
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute("SELECT * FROM ls_patients WHERE TRIM(belt_id)=%s", (str(belt_id).strip(),))
        patient = cursor.fetchone()
        if not patient: flash(f"Patient {belt_id} not found","danger"); return redirect(url_for("logout"))
        cursor.execute("SELECT contact_id, contact_name, contact_phone, contact_email, relationship FROM ls_contacts WHERE TRIM(belt_id_ref)=%s AND active=1", (str(belt_id).strip(),))
        manual = cursor.fetchall()
        cursor.execute("SELECT id AS contact_id, guardian_name AS contact_name, guardian_phone AS contact_phone, guardian_email FROM ls_guardians WHERE TRIM(linked_belt_id)=%s", (str(belt_id).strip(),))
        guardians = cursor.fetchall()
        contacts = guardians + manual
        tpl = "relative_dashboard.html" if user_type=="guardian" else "patient_dashboard.html"
        return render_template(tpl, patient=patient, contacts=contacts)
    finally: cursor.close()

@app.route("/settings", methods=["GET","POST"])
def settings():
    if "user_id" not in session: return redirect(url_for("login"))
    user_type = session["user_type"]
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        if request.method == "POST":
            if user_type == "patient":
                cursor.execute("UPDATE ls_patients SET medical_condition=%s WHERE belt_id=%s", (request.form.get("medical_condition","").strip(), session["user_id"]))
            else:
                cursor.execute("UPDATE ls_guardians SET guardian_phone=%s WHERE id=%s", (request.form.get("phone","").strip(), session["user_id"]))
            mysql.connection.commit()
            flash("Settings saved","success")
            return redirect(url_for("settings"))

        if user_type == "patient":
            belt_id = session["user_id"]
            cursor.execute("SELECT * FROM ls_patients WHERE belt_id=%s", (belt_id,))
            user_data = cursor.fetchone()
            cursor.execute("SELECT contact_id, contact_name, contact_phone, contact_email, relationship FROM ls_contacts WHERE belt_id_ref=%s AND active=1", (belt_id,))
            manual = cursor.fetchall()
            cursor.execute("SELECT id, guardian_name, guardian_email, guardian_phone, linked_belt_id FROM ls_guardians WHERE linked_belt_id=%s", (belt_id,))
            guardians = cursor.fetchall()
            contacts = []
            for g in guardians:
                contacts.append({"contact_id": g["id"], "contact_name": g["guardian_name"], "contact_phone": g.get("guardian_phone") or g.get("guardian_email"), "contact_type": "Registered Guardian", "is_registered": True})
            for c in manual:
                contacts.append({"contact_id": c["contact_id"], "contact_name": c["contact_name"], "contact_phone": c["contact_phone"], "contact_type": "Emergency Contact", "is_registered": False})
        else:
            cursor.execute("SELECT id, guardian_name, guardian_email, guardian_phone, linked_belt_id FROM ls_guardians WHERE id=%s", (session["user_id"],))
            user_data = cursor.fetchone()
            contacts = []
        return render_template("settings.html", user_type=user_type, user_data=user_data, contacts=contacts)
    except Exception as e:
        mysql.connection.rollback()
        print(f"SETTINGS ERROR -> {e}")
        flash("Settings error","danger")
        return redirect(url_for("dashboard"))
    finally: cursor.close()

@app.route("/add_guardian", methods=["POST"])
def add_guardian():
    if "user_id" not in session or session.get("user_type")!="patient":
        return jsonify({"status":"error","message":"Unauthorized"}),403
    data = request.get_json(silent=True) or request.form
    name = str(data.get("name","")).strip(); phone = str(data.get("phone","")).strip()
    email = str(data.get("email","")).strip() or None
    relationship = str(data.get("relationship","")).strip() or None
    if not name or not phone: return jsonify({"status":"error","message":"Name and phone required"}),400
    cursor = None
    try:
        cursor = mysql.connection.cursor()
        cursor.execute("SELECT contact_id FROM ls_contacts WHERE belt_id_ref=%s AND contact_phone=%s", (session["user_id"], phone))
        if cursor.fetchone(): return jsonify({"status":"error","message":"Contact already exists"}),409
        cursor.execute(
            "INSERT INTO ls_contacts (belt_id_ref, contact_name, contact_phone, contact_email, relationship, active) VALUES (%s,%s,%s,%s,%s,1)",
            (session["user_id"], name, phone, email, relationship)
        )
        mysql.connection.commit()
        return jsonify({"status":"success","message":"Contact added"}),200
    except Exception as e:
        if cursor: mysql.connection.rollback()
        return jsonify({"status":"error","message":str(e)}),500
    finally:
        if cursor: cursor.close()

@app.route("/remove_guardian/<int:contact_id>", methods=["POST","DELETE"])
def remove_guardian(contact_id):
    """
    Supports both POST (classic form/redirect flow) and DELETE (the JSON-based
    call already used by settings.html's removeContact()) so the existing
    frontend button keeps working without any markup changes.
    """
    if "user_id" not in session:
        if request.method == "DELETE":
            return jsonify({"status":"error","message":"Unauthorized"}),401
        return redirect(url_for("login"))
    cursor = mysql.connection.cursor()
    try:
        cursor.execute("DELETE FROM ls_contacts WHERE contact_id=%s AND belt_id_ref=%s", (contact_id, session["user_id"]))
        removed = cursor.rowcount > 0
        mysql.connection.commit()
        if request.method == "DELETE":
            if removed:
                return jsonify({"status":"success"}),200
            return jsonify({"status":"error","message":"Contact not found"}),404
        flash("Contact removed" if removed else "Unable to remove","success" if removed else "danger")
    except Exception as e:
        mysql.connection.rollback()
        if request.method == "DELETE":
            return jsonify({"status":"error","message":str(e)}),500
        flash("Unable to remove","danger")
    finally: cursor.close()
    return redirect(url_for("settings"))

@app.route("/clear_emergency/<belt_id>", methods=["POST"])
def clear_emergency(belt_id):
    """Manually clear a patient's emergency flag and resolve their active alert (Requirement 14)."""
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute("UPDATE ls_patients SET is_emergency=0, fall_status=0, posture='Standing' WHERE TRIM(belt_id)=%s", (belt_id.strip(),))
        active = get_active_alert(cursor, belt_id.strip())
        if active:
            cursor.execute("UPDATE ls_alerts SET status='RESOLVED', resolved_at=CURRENT_TIMESTAMP WHERE alert_id=%s", (active["alert_id"],))
        mysql.connection.commit()
        return jsonify({"status":"success"}),200
    except Exception as e:
        mysql.connection.rollback(); return jsonify({"status":"error","message":str(e)}),500
    finally: cursor.close()

@app.route("/hospitals", methods=["GET","POST"])
def hospitals():
    """
    Minimal hospital admin endpoint (Requirement 7, 8, 13). No dedicated
    hospital dashboard exists in the current frontend, so this is exposed as
    a JSON API only - reuse it from any existing admin screen if one is added.
    """
    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            name = str(data.get("hospital_name","")).strip()
            lat = safe_float(data.get("latitude")); lon = safe_float(data.get("longitude"))
            if not name or not valid_coordinates(lat, lon):
                return jsonify({"status":"error","message":"hospital_name, latitude and longitude are required"}),400
            cursor.execute(
                "INSERT INTO ls_hospitals (hospital_name, phone, hospital_email, address, latitude, longitude, emergency_available, active) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,1)",
                (name, data.get("phone"), data.get("hospital_email"), data.get("address"), lat, lon,
                 1 if data.get("emergency_available", True) else 0)
            )
            mysql.connection.commit()
            return jsonify({"status":"success","hospital_id":cursor.lastrowid}),201
        cursor.execute("SELECT * FROM ls_hospitals ORDER BY hospital_name")
        return jsonify({"status":"success","hospitals":cursor.fetchall()}),200
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"status":"error","message":str(e)}),500
    finally: cursor.close()

@app.route("/push/public-key", methods=["GET"])
def push_public_key():
    key = app.config.get("VAPID_PUBLIC_KEY", "")
    if not key:
        return jsonify({"status":"error","message":"Web push is not configured"}), 503
    return jsonify({"status":"success","publicKey":key}), 200

@app.route("/push/subscribe", methods=["POST"])
def push_subscribe():
    if "user_id" not in session:
        return jsonify({"status":"error","message":"Login required"}), 401
    user_type = session.get("user_type")
    belt_id = session["user_id"] if user_type == "patient" else session.get("linked_belt_id")
    if not belt_id:
        return jsonify({"status":"error","message":"No linked patient"}), 400

    data = request.get_json(silent=True) or {}
    sub = data.get("subscription") or {}
    endpoint = str(sub.get("endpoint","")).strip()
    keys = sub.get("keys") or {}
    p256dh = str(keys.get("p256dh","")).strip()
    auth = str(keys.get("auth","")).strip()
    if not endpoint or not p256dh or not auth:
        return jsonify({"status":"error","message":"Invalid push subscription"}), 400

    cursor = None
    try:
        cursor = mysql.connection.cursor()
        cursor.execute(
            """INSERT INTO ls_push_subscriptions (belt_id_ref, endpoint, p256dh, auth)
               VALUES (%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE belt_id_ref=VALUES(belt_id_ref), p256dh=VALUES(p256dh), auth=VALUES(auth)""",
            (belt_id, endpoint, p256dh, auth)
        )
        mysql.connection.commit()
        return jsonify({"status":"success","message":"This phone is registered for LifeShield emergency popups."}), 200
    except Exception as e:
        if cursor: mysql.connection.rollback()
        print(f"PUSH SUBSCRIBE ERROR -> {e}")
        return jsonify({"status":"error","message":str(e)}), 500
    finally:
        if cursor: cursor.close()

@app.route("/service-worker.js", methods=["GET"])
def service_worker():
    js = """
self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) {}
  const title = data.title || "LifeShield Emergency";
  const options = {
    body: data.body || "Emergency alert received.",
    icon: "/static/images/patient_image.png",
    badge: "/static/images/patient_image.png",
    tag: data.tag || "lifeshield-emergency",
    renotify: true,
    requireInteraction: true,
    vibrate: [300, 150, 300, 150, 700],
    data: { url: data.url || "/dashboard", location: data.location || "" },
    actions: [
      { action: "open", title: "Open LifeShield" },
      { action: "location", title: "View Location" }
    ]
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const data = event.notification.data || {};
  let target = data.url || "/dashboard";
  if (event.action === "location" && data.location) target = data.location;
  event.waitUntil(clients.matchAll({type:"window", includeUncontrolled:true}).then(list => {
    for (const client of list) {
      if ("focus" in client) {
        client.navigate(target);
        return client.focus();
      }
    }
    return clients.openWindow(target);
  }));
});
"""
    return app.response_class(js, mimetype="application/javascript", headers={"Cache-Control":"no-cache"})

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/health")
def health():
    return jsonify({"status":"ok"}),200

if __name__ == "__main__":
    print("\n LIFESHIELD SERVER http://localhost:5000 \n")
    app.run(host="0.0.0.0", port=5000, debug=True)