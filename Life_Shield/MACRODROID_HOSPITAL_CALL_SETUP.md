# LifeShield + MacroDroid Hospital Call

## Emergency flow

Patient login -> SOS button OR fall detection
-> LifeShield emergency pipeline
-> emergency contacts
-> nearest hospital email/SMS
-> NTFY command
-> Android ntfy
-> MacroDroid
-> Make Phone Call to nearest hospital

## NTFY command format

Manual SOS:
SOS_DETECTED|+91XXXXXXXXXX

Fall:
FALL_DETECTED|+91XXXXXXXXXX

MacroDroid should use the text after `|` as the phone number.

## MacroDroid macro

Trigger:
- Notification received
- App: ntfy
- Notification title: LifeShield Emergency

Actions:
1. Read notification body into a local variable.
2. Split the text using `|`.
3. Extract item 2 (the phone number).
4. Make Phone Call using that phone number.
5. Optionally show a confirmation notification: "LifeShield: Calling nearest hospital".
6. Optionally add a short delay before the call if desired.

Android permissions:
- Phone permission for MacroDroid
- Notification access for MacroDroid
- ntfy notifications enabled
- Battery optimization disabled for ntfy and MacroDroid where necessary

## Server .env

NTFY_SERVER_URL=https://ntfy.sh
NTFY_TOPIC=YOUR_PRIVATE_LIFESHIELD_TOPIC

Do not commit `.env` or API credentials to GitHub.

## Important

The call is made by the Android phone running MacroDroid. It is not a Twilio Voice call.
For a real emergency deployment, use a properly authorized emergency/medical communications setup.
