// LifeShield Node.js Call Service
// -------------------------------
// A small, standalone Node.js/Express microservice with one job:
// place an emergency phone call via Twilio Voice when the Flask
// backend (app.py) detects a fall or SOS.
//
// Flow:
//   Flask app.py  --POST /call-->  this Node service  --Twilio Voice API-->  phone rings
//
// Run:
//   npm install
//   cp .env.example .env   (fill in real values)
//   npm start

require("dotenv").config();
const express = require("express");
const twilio = require("twilio");

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 4000;
const TWILIO_ACCOUNT_SID = process.env.TWILIO_ACCOUNT_SID || "";
const TWILIO_AUTH_TOKEN = process.env.TWILIO_AUTH_TOKEN || "";
const TWILIO_FROM_NUMBER = process.env.TWILIO_FROM_NUMBER || "";
const CALL_SERVICE_SECRET = process.env.CALL_SERVICE_SECRET || "";

let twilioClient = null;
if (TWILIO_ACCOUNT_SID && TWILIO_AUTH_TOKEN) {
  twilioClient = twilio(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN);
} else {
  console.warn("Twilio not configured - calls will be mocked (logged, not actually placed).");
}

function normalizePhone(raw) {
  if (!raw) return "";
  const digits = String(raw).replace(/\D/g, "");
  if (digits.length === 10) return "+91" + digits; // default India country code, same as app.py
  if (digits.length === 12 && digits.startsWith("91")) return "+" + digits;
  if (String(raw).startsWith("+")) return "+" + digits;
  return digits ? "+" + digits : "";
}

// Simple shared-secret check so random people on the network can't
// trigger emergency calls against your Twilio account.
function requireSecret(req, res, next) {
  if (!CALL_SERVICE_SECRET) return next(); // no secret configured -> skip check (dev only)
  const provided = req.headers["x-call-service-secret"];
  if (provided !== CALL_SERVICE_SECRET) {
    return res.status(401).json({ ok: false, error: "unauthorized" });
  }
  next();
}

app.get("/health", (req, res) => {
  res.json({ ok: true, service: "lifeshield-call-service", twilioConfigured: !!twilioClient });
});

// POST /call
// body: { to: "+919999999999", message: "LIFESHIELD EMERGENCY: ..." }
app.post("/call", requireSecret, async (req, res) => {
  const { to, message } = req.body || {};
  const toNumber = normalizePhone(to);

  if (!toNumber) {
    return res.status(400).json({ ok: false, error: "missing or invalid 'to' phone number" });
  }
  const spokenMessage = message || "This is a LifeShield emergency alert. Please check on the patient immediately.";

  // Trial accounts block ALL custom call instructions (both <Say> and <Play>
  // TwiML, and custom Url webhooks) with error code 0. CONFIRMED WORKING
  // WORKAROUND: Twilio's own pre-approved template webhook IS allowed on
  // trial accounts. This plays Twilio's generic text-to-speech demo message
  // (not your custom emergency text) - upgrade the account to unlock custom
  // messages. See TRIAL_TEMPLATE_URL below.
  const TRIAL_TEMPLATE_URL = "https://webhooks.twilio.com/v1/Voice/Template/voice_text_to_speech";
  const useTrialTemplate = process.env.TWILIO_TRIAL_MODE !== "false"; // default true

  if (!twilioClient) {
    console.log(`MOCK CALL (Twilio not configured) -> ${toNumber} | "${spokenMessage.slice(0, 80)}..."`);
    return res.json({ ok: true, mocked: true, to: toNumber });
  }

  try {
    const callParams = useTrialTemplate
      ? { to: toNumber, from: TWILIO_FROM_NUMBER, url: TRIAL_TEMPLATE_URL }
      : { to: toNumber, from: TWILIO_FROM_NUMBER, twiml: `<Response><Say>${escapeXml(spokenMessage)}</Say></Response>` };

    const call = await twilioClient.calls.create(callParams);
    console.log(`CALL PLACED -> ${toNumber} | sid=${call.sid} | status=${call.status} | trialTemplate=${useTrialTemplate}`);
    if (useTrialTemplate) {
      console.log(`  NOTE: trial mode plays Twilio's generic demo message, not: "${spokenMessage.slice(0, 60)}..."`);
    }
    return res.json({ ok: true, sid: call.sid, status: call.status, to: toNumber, trialTemplate: useTrialTemplate });
  } catch (err) {
    console.error(`CALL ERROR -> ${toNumber} | ${err.message}`);
    if (err.code) console.error(`  Twilio error code: ${err.code}`);
    if (err.moreInfo) console.error(`  More info: ${err.moreInfo}`);
    if (err.status) console.error(`  HTTP status: ${err.status}`);
    return res.status(502).json({ ok: false, error: err.message, code: err.code || null });
  }
});

function escapeXml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

app.listen(PORT, () => {
  console.log(`LifeShield Call Service running on http://localhost:${PORT}`);
  console.log(`Twilio configured: ${!!twilioClient}`);
});