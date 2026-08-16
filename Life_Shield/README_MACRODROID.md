# LifeShield MacroDroid Integration

This version keeps the existing emergency pipeline and adds the MacroDroid hospital-call bridge.

Manual SOS from the logged-in patient dashboard and fall detection should both reach the same emergency pipeline.

Configure `NTFY_SERVER_URL` and `NTFY_TOPIC` in `.env`, then configure MacroDroid according to `MACRODROID_HOSPITAL_CALL_SETUP.md`.
