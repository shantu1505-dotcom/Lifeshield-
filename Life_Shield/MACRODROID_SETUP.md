# MacroDroid emergency-call bridge

The Flask server publishes an Android notification through ntfy when an emergency is detected.

## Notification body

The body is exactly:

`FALL_DETECTED|+919876543210`

The first field is the emergency keyword and the second field is the nearest hospital phone number returned by Google Places.

Other keywords are:

- `SOS_DETECTED`
- `LOW_OXYGEN`
- `ABNORMAL_HEART_RATE`
- `EMERGENCY_DETECTED`

## Server configuration

Set these environment variables before starting Flask:

```text
GOOGLE_PLACES_API_KEY=your_google_places_api_key
NTFY_SERVER_URL=https://ntfy.sh
NTFY_TOPIC=your_private_lifeshield_topic
```

Google Places API (New) Nearby Search is used with hospital type and `DISTANCE` ranking. The server already uses the patient's continuously updated `/update_location` coordinates.

## MacroDroid logic

Create a MacroDroid macro that:

1. Triggers when a notification is received from the ntfy app.
2. Checks that the notification title is `LifeShield Emergency`.
3. Reads the notification body.
4. Splits the body at `|`.
5. Uses field 1 as the emergency keyword.
6. Uses field 2 as the hospital phone number.
7. If the phone number is non-empty and the keyword is an emergency keyword, start the phone call action to that number.

Do not hard-code the hospital number; it must come from the notification body.
