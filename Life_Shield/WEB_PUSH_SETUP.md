# LifeShield phone popup notifications

This build adds browser Web Push notifications.

1. Log in to LifeShield on each phone that should receive alerts.
2. When the browser asks for notification permission, tap **Allow**.
3. The phone is then registered against the patient's belt ID.
4. When SOS/fall is triggered, every registered phone receives an OS-level LifeShield notification, even if the dashboard tab is closed.
5. Tapping the notification opens LifeShield.

A website cannot force a notification onto an arbitrary phone number/email without the recipient first granting notification permission. Manual emergency contacts who do not have a LifeShield login cannot receive Web Push from the browser.

Android Chrome/Edge support this. On iPhone/iPad, Web Push requires a supported iOS/iPadOS version and the LifeShield site added to the Home Screen.

For production, serve the app over HTTPS. localhost is treated as secure for development.
