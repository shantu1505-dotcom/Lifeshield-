# LifeShield Emergency Workflow — Changes

The existing frontend (all templates, layout, colors, navigation) is unchanged
except for a handful of minimal additions listed below. Everything else was
done in `app.py` (backend only).

## Backend (`app.py`)

- **Shared emergency processor** — `process_emergency(belt_id, alert_type, location)`
  is now the single function both fall detection and SOS funnel into, matching
  the spec's `process_emergency("FALL_DETECTED")` / `process_emergency("MANUAL_SOS")`
  pattern. `trigger_logic_alerts(...)` is kept as a thin backward-compatible
  wrapper around it.
- **Hospital is now actually notified directly** — previously the hospital's
  name/phone were only mentioned inside the message sent to contacts; the
  hospital itself never received anything. Now, if the hospital has an email
  or phone on file, it gets its own "LIFESHIELD MEDICAL EMERGENCY" message.
- **Notification delivery logging** — new `ls_notifications` table records
  SENT/FAILED per recipient, per channel, per emergency (`/emergency/<id>`
  returns this log).
- **Duplicate-emergency protection** — a new SOS press or fall event while an
  emergency is already NEW/ACKNOWLEDGED/RESPONDING updates the existing
  `ls_alerts` row instead of creating a new one.
- **Emergency status lifecycle** — `ls_alerts.status` (NEW, ACKNOWLEDGED,
  RESPONDING, RESOLVED, CANCELLED) with `acknowledged_at`/`resolved_at`
  timestamps, updatable via `POST /emergency/<id>/status`.
- **New endpoints**:
  - `POST /trigger_fall` — explicit fall-trigger route (the ESP32 still
    reports through `/update_vitals`, which also flows into the same
    processor; this is for manual testing/demo or other device integrations)
  - `GET /emergency/<id>` — full emergency detail + notification log
  - `GET /emergency/active` — all currently active emergencies
  - `GET /emergency/history/<belt_id>` — a patient's emergency history
  - `POST /emergency/<id>/status` — update status
  - `GET/POST /hospitals` — list/add hospitals (no dedicated hospital admin
    UI existed, so this is exposed as JSON only for now)
- **Contact schema extended** (not replaced) — `ls_contacts` gained
  `contact_email`, `relationship`, and `active` columns via safe migration
  (`_ensure_column`, runs once at startup, no-op if already present).
- **Bug fix**: `/remove_guardian/<id>` now accepts both `POST` and `DELETE`.
  The existing `settings.html` JS was calling it with `DELETE` and expecting
  a JSON response, but the route only accepted `POST` and redirected — so
  contact removal was silently broken. Fixed without touching the JS.
- **Security fix**: the MySQL password was hardcoded in `app.py`
  (`MyLifeShield@2026`) instead of coming from `.env` like every other
  credential in the file. Moved to `MYSQL_HOST` / `MYSQL_USER` /
  `MYSQL_PASSWORD` / `MYSQL_DB` environment variables.
- **GPS fallback made explicit** — `process_emergency` now falls back to the
  patient's last known coordinates when live GPS isn't supplied, and labels
  the resulting map link "(last known location)" so recipients know. It
  never fabricates coordinates.

## Frontend (minimal, additive only)

- `templates/relative_dashboard.html` — the "Nearest Hospital" card existed
  but was always a static placeholder. Added `id`s to the existing markup and
  ~25 lines of JS that reuse the same `/test_hospital/<belt_id>` endpoint the
  patient dashboard already calls, so guardians now see live hospital info
  too. No layout/visual changes.
- `templates/settings.html` — added two optional fields (Relationship, Email)
  to the existing "Add Emergency Contact" modal, and show the relationship
  next to a contact's name in the existing list. No new screens.

## Not changed

- No new pages, no new navigation, no redesign.
- `patient_dashboard.html`'s SOS button and its JS were already correctly
  wired to `/trigger_sos` with GPS capture — left as-is.
- ESP32 firmware — untouched; it already reports falls through
  `/update_vitals`, which now flows into the shared processor.

## GPS bug found and fixed (this session)

The patient dashboard had **two separate `<script>` blocks**, each declaring
its own `BELT_ID`:

- The main script (vitals, SOS, hospital lookup) correctly used
  `{{ patient.belt_id }}`.
- A second script, responsible for actually sending the phone's GPS via
  `navigator.geolocation` → `POST /update_location`, had it hardcoded to a
  leftover test value: `const BELT_ID = "XYZ130"`.

Effect: every real patient's phone was silently sending its GPS to a
patient record called `XYZ130` (which doesn't exist, or isn't the logged-in
patient), while their own dashboard's `latitude`/`longitude` stayed `NULL`
forever — showing "Waiting for patient GPS..." and blocking hospital lookup,
because there's no GPS hardware on the ESP32 belt (its firmware hardcodes
`latitude: 0.0, longitude: 0.0`, which the server correctly treats as
invalid/no-location). Since the browser-GPS script never wrote to the right
record, the whole emergency pipeline had no location to work with.

Fixed by pointing both scripts at the same real `{{ patient.belt_id }}`, and
by having the SOS button request one fresh GPS fix (up to 5s) immediately
before sending, rather than relying only on the 10-second background poll.

This means: **location now comes from the patient's phone browser**, not
the belt. If real GPS hardware is added to the belt later, the ESP32
firmware also needs a real GPS module (e.g. NEO-6M) wired in and the
hardcoded `0.0, 0.0` in the `.ino` file replaced with real readings — the
server-side logic already accepts either source.

## Known limitation

This sandbox has no MySQL server available (no apt access, no mysql_config
for building `flask-mysqldb`), so the code was verified by:
- `py_compile` / AST parse of `app.py`
- Importing `app.py` against stub `flask_mysqldb`/`MySQLdb` modules and
  confirming every route registers correctly
- Unit-testing the pure logic (`valid_coordinates`, `calculate_distance`
  against a known Nagpur–Mumbai distance, `normalize_phone`, etc.)
- Jinja2 template parsing for all six HTML templates

It has **not** been run against a live MySQL instance. Before your
demo, run it once locally (`python app.py`) against your real
`lifeshield_db` — the schema migrations in `init_db()` are additive
and safe to run against your existing database.
