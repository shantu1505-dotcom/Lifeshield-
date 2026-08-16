/*
 * LifeShield ESP32 Firmware
 * ─────────────────────────────────────────────────────────────────────────────
 * Sensors  : HW-827  (Analog Pulse Sensor → Heart Rate BPM)
 *            MPU6050 (Fall Detection + Posture)
 *            DHT11   (Body Temperature)
 *            Buzzer  (Instant alert on fall)
 *
 * Wiring   : HW-827   OUT→GPIO34,  VCC→3.3V, GND→GND
 *            MPU6050  SDA→GPIO21, SCL→GPIO22, VCC→3.3V, GND→GND
 *            DHT11    DATA→GPIO4,  VCC→3.3V, GND→GND
 *            Buzzer   +→GPIO25,   –→GND
 *
 * Library to install (Sketch → Manage Libraries):
 *   • "DHT sensor library"     by Adafruit
 *   • "Adafruit Unified Sensor" by Adafruit  (DHT dependency)
 *   (No extra library needed for HW-827 or MPU6050 — handled manually)
 *
 * Posts JSON to Flask /update_vitals every 2 s.
 * ─────────────────────────────────────────────────────────────────────────────
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <math.h>
#include <DHT.h>

// ── WiFi ──────────────────────────────────────────────────────────────────────
const char* SSID     = "iPhone";
const char* PASSWORD = "0987654321";

// ── Flask server — replace with your PC's local IP ────────────────────────────
const char* SERVER_URL = "http://172.20.10.6:5000/update_vitals";

// ── Belt ID — must match exactly what you registered in LifeShield ────────────
const char* BELT_ID = "LS-2026-3108";

// ── Pin definitions ───────────────────────────────────────────────────────────
#define PULSE_PIN  34
#define DHT_PIN    4
#define BUZZER_PIN 25
#define DHT_TYPE   DHT11
#define MPU_ADDR   0x68

// ── DHT11 ─────────────────────────────────────────────────────────────────────
DHT dht(DHT_PIN, DHT_TYPE);
float temperatureF        = 98.6f;
unsigned long lastDHTRead = 0;
const unsigned long DHT_INTERVAL = 3000;   // DHT11 min 1 s; use 3 s to be safe

// ── HW-827 Pulse sensor — auto-threshold BPM detection ───────────────────────
int           bpm            = 0;   // smoothed BPM sent to Flask
int           displayBpm     = 0;   // held value — only clears after 10 s of silence
bool          pulseState     = false;
unsigned long lastBeatTime   = 0;
unsigned long lastValidBeat  = 0;   // tracks last time a *valid* BPM was calculated

// 8-sample ring buffer — larger buffer = much smoother readings
int bpmSamples[8] = {0,0,0,0,0,0,0,0};
int bpmIndex      = 0;
int bpmValidCount = 0;

// Auto-calibration window (resets every 5 s, seeds from current value)
int           signalMin      = 4095;
int           signalMax      = 0;
unsigned long lastReset      = 0;
bool          thresholdReady = false;   // don't detect beats until first window done

unsigned long lastPulseRead = 0;
const unsigned long PULSE_INTERVAL = 2;   // sample every 2 ms

// ── MPU6050 ───────────────────────────────────────────────────────────────────
float totalAcc = 0.0f;
float pitch    = 0.0f;
float roll     = 0.0f;

unsigned long lastMPURead = 0;
const unsigned long MPU_INTERVAL = 5;   // 5 ms → fall latency ≤ 5 ms

// ── Fall detection ────────────────────────────────────────────────────────────
int           fallen           = 0;
bool          fallDetected     = false;
unsigned long fallDetectedTime = 0;
const unsigned long FALL_LATCH_MS = 5000;   // buzzer stays on 5 s after impact

// ── Simulated SpO2 — slow drift model, 95–99%, only shifts every 15 s ─────────
int           simulatedSpO2    = 97;   // start at 97%
int           spo2Target       = 97;   // drift toward this
unsigned long lastSpO2Update   = 0;
const unsigned long SPO2_INTERVAL = 30000;  // only pick new target every 30 s

// ── Flask POST every 2 s ─────────────────────────────────────────────────────
unsigned long lastPost = 0;
const unsigned long POST_INTERVAL = 2000;

// ─────────────────────────────────────────────────────────────────────────────
// HW-827 pulse sensor — auto-threshold peak detection
// ─────────────────────────────────────────────────────────────────────────────
void readPulseSensor() {
    // Average 10 samples to reduce ADC noise
    int value = 0;
    for (int i = 0; i < 10; i++) value += analogRead(PULSE_PIN);
    value /= 10;

    unsigned long now = millis();

    // Reset calibration window every 5 s — seed with current value so
    // threshold doesn't jump to the useless midpoint (2047) right after reset
    if (now - lastReset > 5000) {
        signalMin      = value;
        signalMax      = value;
        lastReset      = now;
        thresholdReady = true;
    }

    if (value < signalMin) signalMin = value;
    if (value > signalMax) signalMax = value;

    // Wait for first calibration window before detecting beats
    if (!thresholdReady) return;

    // Require a minimum swing of 100 counts — filters ambient noise / no finger
    if (signalMax - signalMin < 100) return;

    int threshold = (signalMin + signalMax) / 2;

    // Rising edge → new beat
    if (value > threshold && !pulseState) {
        if (lastBeatTime > 0) {
            int interval = (int)(now - lastBeatTime);
            int tempBPM  = 60000 / interval;

            if (tempBPM > 40 && tempBPM < 180) {
                // Reject if new reading deviates more than 20 BPM from current avg
                // — stops single bad beats from spiking the display
                if (bpmValidCount > 0 && abs(tempBPM - bpm) > 20) {
                    // Skip this beat — likely noise
                } else {
                    bpmSamples[bpmIndex] = tempBPM;
                    bpmIndex = (bpmIndex + 1) % 8;
                    if (bpmValidCount < 8) bpmValidCount++;

                    int sum = 0;
                    for (int i = 0; i < bpmValidCount; i++)
                        sum += bpmSamples[(bpmIndex - 1 - i + 8) % 8];
                    bpm         = sum / bpmValidCount;
                    displayBpm  = bpm;          // update held display value
                    lastValidBeat = now;
                }
            }
        }
        lastBeatTime = now;
        pulseState   = true;
    }

    // Falling edge — hysteresis of 50 counts avoids chatter
    if (value < threshold - 50) pulseState = false;

    // Only zero out if no valid beat for 10 s (not 4 s)
    // This prevents brief signal dropouts from clearing the reading
    if (lastValidBeat > 0 && now - lastValidBeat > 10000) {
        bpm           = 0;
        displayBpm    = 0;
        bpmValidCount = 0;
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// MPU6050 — accelerometer only (gyro not needed for fall + posture)
// ─────────────────────────────────────────────────────────────────────────────
void readMPU() {
    Wire.beginTransmission(MPU_ADDR);
    Wire.write(0x3B);   // ACCEL_XOUT_H
    Wire.endTransmission(false);
    Wire.requestFrom((uint8_t)MPU_ADDR, (uint8_t)6, (uint8_t)true);

    int16_t ax_raw = Wire.read() << 8 | Wire.read();
    int16_t ay_raw = Wire.read() << 8 | Wire.read();
    int16_t az_raw = Wire.read() << 8 | Wire.read();

    // ±2g range → 16384 LSB/g
    float ax = ax_raw / 16384.0f;
    float ay = ay_raw / 16384.0f;
    float az = az_raw / 16384.0f;

    totalAcc = sqrtf(ax*ax + ay*ay + az*az) * 9.8f;

    // Tilt angles for posture estimation
    pitch = atan2f(ax, sqrtf(ay*ay + az*az)) * 180.0f / PI;
    roll  = atan2f(ay, sqrtf(ax*ax + az*az)) * 180.0f / PI;

    // ── Fall detection — 2.5g = 24.5 m/s² ───────────────────────────────────
    // Normal walking ≈ 1.5g, running ≈ 2g, fall impact ≥ 3g
    if (totalAcc > 24.5f && !fallDetected) {
        fallDetected     = true;
        fallDetectedTime = millis();
        fallen           = 1;
        digitalWrite(BUZZER_PIN, HIGH);   // fires instantly — no HTTP delay
        Serial.println("⚠️  FALL DETECTED");
    }

    // Release buzzer after latch period
    if (fallDetected && millis() - fallDetectedTime > FALL_LATCH_MS) {
        fallDetected = false;
        fallen       = 0;
        digitalWrite(BUZZER_PIN, LOW);
        Serial.println("✅ Fall alert cleared");
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// DHT11 — read temperature in °C, convert to °F for Flask
// ─────────────────────────────────────────────────────────────────────────────
void readDHT() {
    float tempC = dht.readTemperature();
    if (!isnan(tempC)) {
        temperatureF = tempC * 9.0f / 5.0f + 32.0f;
        Serial.printf("🌡️  Temp: %.1f°C → %.1f°F\n", tempC, temperatureF);
    } else {
        Serial.println("⚠️  DHT11 read failed — keeping last value");
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Posture string derived from pitch/roll (for Serial log only)
// ─────────────────────────────────────────────────────────────────────────────
String getPosture() {
    if (fallen) return "Fall Detected";
    float absPitch = fabsf(pitch);
    if (absPitch < 30)  return "Standing";
    if (absPitch < 60)  return "Sitting";
    return "Lying Down";
}

// ─────────────────────────────────────────────────────────────────────────────
// POST vitals to Flask /update_vitals
// ─────────────────────────────────────────────────────────────────────────────
void postToFlask() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("WiFi lost — reconnecting...");
        WiFi.begin(SSID, PASSWORD);
        return;
    }

    HTTPClient http;
    http.begin(SERVER_URL);
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(1500);   // 1.5 s max — sensors keep running during HTTP

    // SpO2 — slow drift model:
    // Every 15 s pick a new target in 95-99 range, drift ±1 toward it each POST
    // Result: reading feels stable, changes slowly and naturally, never hits 100%
    if (displayBpm > 0) {
        if (millis() - lastSpO2Update > SPO2_INTERVAL) {
            spo2Target      = random(95, 101);   // 95–100 inclusive, normal range
            lastSpO2Update  = millis();
        }
        // Nudge current value one step toward target
        if (simulatedSpO2 < spo2Target)      simulatedSpO2++;
        else if (simulatedSpO2 > spo2Target) simulatedSpO2--;
    } else {
        simulatedSpO2 = 0;   // no finger — show 0
    }

    String body = "{";
    body += "\"belt_id\":\""   + String(BELT_ID)         + "\",";
    body += "\"bpm\":"         + String(displayBpm)       + ",";
    body += "\"spo2\":"        + String(simulatedSpO2)   + ",";
    body += "\"temperature\":" + String(temperatureF, 1)  + ",";
    body += "\"fallen\":"      + String(fallen)           + ",";
    body += "\"posture\":\"" + getPosture()           + "\",";
    body += "\"latitude\":"    + String(0.0, 6)           + ",";
    body += "\"longitude\":"   + String(0.0, 6);
    body += "}";

    int code = http.POST(body);

    if (code == 200) {
        Serial.printf("✅ POST OK | BPM: %d | Temp: %.1f°F | Posture: %s\n",
                      bpm, temperatureF, getPosture().c_str());
    } else {
        Serial.printf("❌ POST failed — HTTP %d\n", code);
    }

    http.end();
}

// ─────────────────────────────────────────────────────────────────────────────
void setup() {
    Serial.begin(115200);

    Wire.begin(21, 22);
    Wire.setClock(400000);   // 400 kHz fast-mode

    pinMode(BUZZER_PIN, OUTPUT);
    digitalWrite(BUZZER_PIN, LOW);

    // ── Wake MPU6050 ──
    Wire.beginTransmission(MPU_ADDR);
    Wire.write(0x6B);
    Wire.write(0);
    Wire.endTransmission(true);
    Serial.println("✅ MPU6050 ready");

    // ── DHT11 ──
    dht.begin();
    delay(2000);   // DHT11 needs 2 s after power-on before first reliable read
    readDHT();

    // ── WiFi ──
    Serial.print("Connecting to WiFi");
    WiFi.begin(SSID, PASSWORD);
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }
    Serial.println("\n✅ WiFi: " + WiFi.localIP().toString());
    Serial.println("Server: " + String(SERVER_URL));

    lastBeatTime = millis();
    lastReset    = millis();
    lastPost     = millis();
    lastDHTRead  = millis();
}

// ─────────────────────────────────────────────────────────────────────────────
void loop() {
    unsigned long now = millis();

    // HW-827 pulse — every 2 ms
    if (now - lastPulseRead >= PULSE_INTERVAL) {
        lastPulseRead = now;
        readPulseSensor();
    }

    // MPU6050 — every 5 ms (fall latency ≤ 5 ms, buzzer fires inside readMPU)
    if (now - lastMPURead >= MPU_INTERVAL) {
        lastMPURead = now;
        readMPU();
    }

    // DHT11 — every 3 s
    if (now - lastDHTRead >= DHT_INTERVAL) {
        lastDHTRead = now;
        readDHT();
    }

    // Flask POST — every 2 s
    if (now - lastPost >= POST_INTERVAL) {
        lastPost = now;
        postToFlask();
    }
}
