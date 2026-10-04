#include <Arduino.h>
#include <ESP8266HTTPUpdateServer.h>
#include <ESP8266WebServer.h>
#include <ESP8266WiFi.h>
#include <WiFiUdp.h>

#if __has_include("wifi_credentials.h")
#include "wifi_credentials.h"
#else
#error "Copy include/wifi_credentials.example.h to include/wifi_credentials.h and set a private password"
#endif

// Air75 II Matrix 1S 5IN1 II receiver bridge.
//
// This firmware replaces only the ESP8285 ExpressLRS receiver firmware. It
// sends CRSF RC frames through the ESP8285 UART TX pin (GPIO1) to Betaflight's
// receiver UART. It must never be flashed to the STM32 flight controller.

namespace {

constexpr const char *AP_SSID = AIR75_WIFI_SSID;
constexpr const char *AP_PASSWORD = AIR75_WIFI_PASSWORD;
static_assert(sizeof(AIR75_WIFI_PASSWORD) >= 9,
              "AIR75_WIFI_PASSWORD must contain at least eight characters");
constexpr uint8_t AP_CHANNEL = 6;
constexpr uint16_t CONTROL_PORT = 4210;
constexpr uint16_t DIAGNOSTIC_PORT = 4211;
constexpr const char *FIRMWARE_ID = "air75-diag-3";

constexpr uint32_t CRSF_BAUD = 420000;
constexpr uint32_t CRSF_FRAME_PERIOD_US = 10000;  // 100 Hz
constexpr uint32_t LINK_TIMEOUT_MS = 250;
constexpr uint32_t DISARM_BURST_MS = 500;
constexpr uint16_t ARM_MAX_THROTTLE_US = 1050;

constexpr uint8_t FLAG_ARM = 0x01;
constexpr uint8_t FLAG_KILL = 0x02;
constexpr uint8_t PROTOCOL_VERSION = 1;
constexpr size_t CONTROL_PACKET_SIZE = 28;
constexpr size_t ACK_PACKET_SIZE = 20;

constexpr uint8_t CRSF_ADDRESS_FLIGHT_CONTROLLER = 0xC8;
constexpr uint8_t CRSF_FRAMETYPE_RC_CHANNELS_PACKED = 0x16;
constexpr uint8_t CRSF_CHANNEL_COUNT = 16;
constexpr uint8_t CRSF_PAYLOAD_SIZE = 22;
constexpr uint8_t CRSF_LENGTH_FIELD = 24;  // type + payload + CRC
constexpr uint8_t CRSF_TOTAL_FRAME_SIZE = 26;

enum Channel : uint8_t {
    CHANNEL_ROLL = 0,
    CHANNEL_PITCH = 1,
    CHANNEL_THROTTLE = 2,
    CHANNEL_YAW = 3,
    CHANNEL_ARM = 4,    // AUX1
    CHANNEL_ANGLE = 5,  // AUX2
};

enum class LinkState : uint8_t {
    Waiting = 0,
    Disarmed = 1,
    Armed = 2,
    FailsafeLatched = 3,
};

WiFiUDP udp;
WiFiUDP diagnosticUdp;
ESP8266WebServer maintenanceServer(80);
ESP8266HTTPUpdateServer httpUpdater;
LinkState linkState = LinkState::Waiting;

IPAddress controllerIp;
uint16_t controllerPort = 0;
uint32_t activeSession = 0;
uint32_t lastSequence = 0;
uint32_t lastValidPacketMs = 0;
uint32_t disarmBurstUntilMs = 0;
uint32_t lastCrsfFrameUs = 0;
bool haveSession = false;
bool armLowSeen = false;

struct TimingDiagnostics {
    uint32_t accepted = 0;
    uint32_t invalid = 0;
    uint32_t rejected = 0;
    uint32_t maxPacketGapMs = 0;
    uint32_t maxLoopUs = 0;
    uint32_t maxUdpUs = 0;
    uint32_t maxUartDrainUs = 0;
    uint32_t maxCrsfWriteUs = 0;
    uint32_t maxYieldUs = 0;
    uint32_t uartBytes = 0;
};
TimingDiagnostics timing;
TimingDiagnostics faultTiming;
uint32_t bootId = 0;
uint32_t previousLoopUs = 0;
uint32_t lastAcceptedActualMs = 0;
bool haveAcceptedActual = false;
uint32_t faultAtMs = 0;
uint32_t faultPacketAgeMs = 0;
uint32_t faultSequence = 0;
uint32_t faultSession = 0;
uint32_t faultFreeHeap = 0;
uint16_t faultThrottle = 1000;
uint8_t faultStations = 0;
uint8_t faultCause = 0;  // 0 none, 1 armed packet timeout, 2 explicit KILL

void recordMaximum(uint32_t &maximum, uint32_t value) {
    if (value > maximum) maximum = value;
}

void appendNumber(String &json, const char *name, uint32_t value) {
    json += '\"'; json += name; json += "\":"; json += String(value); json += ',';
}

String timingJson(const TimingDiagnostics &value) {
    String json;
    json.reserve(320);
    json = "{";
    appendNumber(json, "accepted", value.accepted);
    appendNumber(json, "invalid", value.invalid);
    appendNumber(json, "rejected", value.rejected);
    appendNumber(json, "max_packet_gap_ms", value.maxPacketGapMs);
    appendNumber(json, "max_loop_us", value.maxLoopUs);
    appendNumber(json, "max_udp_us", value.maxUdpUs);
    appendNumber(json, "max_uart_drain_us", value.maxUartDrainUs);
    appendNumber(json, "max_crsf_write_us", value.maxCrsfWriteUs);
    appendNumber(json, "max_yield_us", value.maxYieldUs);
    appendNumber(json, "uart_bytes", value.uartBytes);
    json.remove(json.length() - 1);
    json += '}';
    return json;
}

void serveDiagnostics() {
    // Never allocate JSON or service diagnostic traffic during active control
    // or the disarm burst. Process at most one queued request per loop.
    if (linkState != LinkState::Waiting &&
        !(linkState == LinkState::FailsafeLatched &&
          static_cast<int32_t>(millis() - disarmBurstUntilMs) >= 0)) return;
    const int length = diagnosticUdp.parsePacket();
    if (length <= 0) return;
    const IPAddress ip = diagnosticUdp.remoteIP();
    const uint16_t port = diagnosticUdp.remotePort();
    char request[4];
    const int count = diagnosticUdp.read(request, sizeof(request));
    diagnosticUdp.flush();
    if (length != 4 || count != 4 || memcmp(request, "A75D", 4) != 0) return;
    String json;
    json.reserve(1400);
    json = "{\"firmware\":\""; json += FIRMWARE_ID; json += "\",";
    appendNumber(json, "boot_id", bootId);
    appendNumber(json, "uptime_ms", millis());
    appendNumber(json, "reset_reason_code", ESP.getResetInfoPtr()->reason);
    appendNumber(json, "state", static_cast<uint8_t>(linkState));
    appendNumber(json, "free_heap", ESP.getFreeHeap());
    appendNumber(json, "stations", WiFi.softAPgetStationNum());
    json += "\"timing\":"; json += timingJson(timing); json += ',';
    json += "\"fault\":{";
    appendNumber(json, "cause", faultCause);
    appendNumber(json, "at_ms", faultAtMs);
    appendNumber(json, "packet_age_ms", faultPacketAgeMs);
    appendNumber(json, "session", faultSession);
    appendNumber(json, "sequence", faultSequence);
    appendNumber(json, "throttle", faultThrottle);
    appendNumber(json, "free_heap", faultFreeHeap);
    appendNumber(json, "stations", faultStations);
    json += "\"timing\":"; json += timingJson(faultTiming); json += "}}";
    diagnosticUdp.beginPacket(ip, port);
    diagnosticUdp.write(reinterpret_cast<const uint8_t *>(json.c_str()), json.length());
    diagnosticUdp.endPacket();
}

uint16_t channelsUs[CRSF_CHANNEL_COUNT] = {
    1500, 1500, 1000, 1500, 1000, 2000,
    1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500,
};

uint16_t readLe16(const uint8_t *p) {
    return static_cast<uint16_t>(p[0]) |
           (static_cast<uint16_t>(p[1]) << 8);
}

uint32_t readLe32(const uint8_t *p) {
    return static_cast<uint32_t>(p[0]) |
           (static_cast<uint32_t>(p[1]) << 8) |
           (static_cast<uint32_t>(p[2]) << 16) |
           (static_cast<uint32_t>(p[3]) << 24);
}

void writeLe16(uint8_t *p, uint16_t value) {
    p[0] = static_cast<uint8_t>(value);
    p[1] = static_cast<uint8_t>(value >> 8);
}

void writeLe32(uint8_t *p, uint32_t value) {
    p[0] = static_cast<uint8_t>(value);
    p[1] = static_cast<uint8_t>(value >> 8);
    p[2] = static_cast<uint8_t>(value >> 16);
    p[3] = static_cast<uint8_t>(value >> 24);
}

uint32_t crc32Ieee(const uint8_t *data, size_t length) {
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < length; ++i) {
        crc ^= data[i];
        for (uint8_t bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ ((crc & 1u) ? 0xEDB88320u : 0u);
        }
    }
    return ~crc;
}

uint8_t crc8DvbS2(const uint8_t *data, size_t length) {
    uint8_t crc = 0;
    for (size_t i = 0; i < length; ++i) {
        crc ^= data[i];
        for (uint8_t bit = 0; bit < 8; ++bit) {
            crc = (crc & 0x80u) ? static_cast<uint8_t>((crc << 1) ^ 0xD5u)
                                : static_cast<uint8_t>(crc << 1);
        }
    }
    return crc;
}

uint16_t clampChannel(uint16_t value) {
    if (value < 1000) return 1000;
    if (value > 2000) return 2000;
    return value;
}

uint16_t microsecondsToCrsf(uint16_t microseconds) {
    // Inverse of Betaflight's legacy CRSF conversion. The exact endpoints are
    // CRSF 172..1811 for approximately 988..2012 us; control input is clamped
    // to the conventional 1000..2000 us range before conversion.
    const float raw = (static_cast<float>(microseconds) - 881.0f) / 0.62477120195241f;
    if (raw < 172.0f) return 172;
    if (raw > 1811.0f) return 1811;
    return static_cast<uint16_t>(raw + 0.5f);
}

void setDisarmedChannels() {
    channelsUs[CHANNEL_ROLL] = 1500;
    channelsUs[CHANNEL_PITCH] = 1500;
    channelsUs[CHANNEL_THROTTLE] = 1000;
    channelsUs[CHANNEL_YAW] = 1500;
    channelsUs[CHANNEL_ARM] = 1000;
    channelsUs[CHANNEL_ANGLE] = 2000;
}

void latchFailsafe(uint32_t nowMs, uint8_t cause) {
    // Freeze the first fault until a new arm cycle. Follow-up KILL packets from
    // the PC must not overwrite the original packet-timeout evidence.
    if (faultCause == 0) {
        faultTiming = timing;
        faultCause = cause;
        faultAtMs = millis();
        faultPacketAgeMs = haveAcceptedActual ? faultAtMs - lastAcceptedActualMs : 0;
        faultSession = activeSession;
        faultSequence = lastSequence;
        faultThrottle = channelsUs[CHANNEL_THROTTLE];
        faultFreeHeap = ESP.getFreeHeap();
        faultStations = WiFi.softAPgetStationNum();
    }
    // Never leave Betaflight holding the last armed stick/throttle frame while
    // it waits for receiver-loss timing. Send an explicit disarm burst first,
    // then stop CRSF so Betaflight also enters its configured RXLOSS failsafe.
    setDisarmedChannels();
    linkState = LinkState::FailsafeLatched;
    disarmBurstUntilMs = nowMs + DISARM_BURST_MS;
    armLowSeen = false;
}

void sendCrsfChannels() {
    uint8_t frame[CRSF_TOTAL_FRAME_SIZE] = {};
    frame[0] = CRSF_ADDRESS_FLIGHT_CONTROLLER;
    frame[1] = CRSF_LENGTH_FIELD;
    frame[2] = CRSF_FRAMETYPE_RC_CHANNELS_PACKED;

    uint32_t bitBuffer = 0;
    uint8_t bitCount = 0;
    uint8_t outputIndex = 3;

    for (uint8_t channel = 0; channel < CRSF_CHANNEL_COUNT; ++channel) {
        bitBuffer |= static_cast<uint32_t>(microsecondsToCrsf(channelsUs[channel])) << bitCount;
        bitCount += 11;
        while (bitCount >= 8) {
            frame[outputIndex++] = static_cast<uint8_t>(bitBuffer & 0xFFu);
            bitBuffer >>= 8;
            bitCount -= 8;
        }
    }

    frame[25] = crc8DvbS2(&frame[2], 1 + CRSF_PAYLOAD_SIZE);
    Serial.write(frame, sizeof(frame));
}

bool sequenceIsNewer(uint32_t candidate, uint32_t previous) {
    return static_cast<int32_t>(candidate - previous) > 0;
}

bool sameController(const IPAddress &ip, uint16_t port) {
    return ip == controllerIp && port == controllerPort;
}

void sendAck(const IPAddress &ip, uint16_t port, uint32_t session, uint32_t sequence) {
    uint8_t ack[ACK_PACKET_SIZE] = {};
    ack[0] = 'A'; ack[1] = '7'; ack[2] = '5'; ack[3] = 'A';
    ack[4] = PROTOCOL_VERSION;
    ack[5] = static_cast<uint8_t>(linkState);
    writeLe16(&ack[6], ACK_PACKET_SIZE);
    writeLe32(&ack[8], session);
    writeLe32(&ack[12], sequence);
    writeLe32(&ack[16], crc32Ieee(ack, 16));

    udp.beginPacket(ip, port);
    udp.write(ack, sizeof(ack));
    udp.endPacket();
}

bool packetHasValidEnvelope(const uint8_t *packet, size_t length) {
    if (length != CONTROL_PACKET_SIZE) return false;
    if (packet[0] != 'A' || packet[1] != '7' || packet[2] != '5' || packet[3] != 'W') return false;
    if (packet[4] != PROTOCOL_VERSION) return false;
    if (readLe16(&packet[6]) != CONTROL_PACKET_SIZE) return false;
    return readLe32(&packet[24]) == crc32Ieee(packet, 24);
}

void acceptControlPacket(const uint8_t *packet, const IPAddress &sourceIp,
                         uint16_t sourcePort, uint32_t nowMs) {
    const uint8_t flags = packet[5];
    const bool armRequested = (flags & FLAG_ARM) != 0;
    const bool killRequested = (flags & FLAG_KILL) != 0;
    const uint32_t session = readLe32(&packet[8]);
    const uint32_t sequence = readLe32(&packet[12]);

    if (!haveSession || session != activeSession || !sameController(sourceIp, sourcePort)) {
        // A new PC process may take control only while requesting disarm. This
        // prevents a restarted or second client from inheriting an armed craft.
        if (linkState == LinkState::Armed || armRequested) { ++timing.rejected; return; }
        activeSession = session;
        controllerIp = sourceIp;
        controllerPort = sourcePort;
        lastSequence = sequence;
        haveSession = true;
        armLowSeen = true;
        linkState = LinkState::Disarmed;
    } else {
        if (!sequenceIsNewer(sequence, lastSequence)) { ++timing.rejected; return; }
        lastSequence = sequence;
    }

    // Only a fully validated, current packet refreshes the watchdog.
    lastValidPacketMs = nowMs;
    const uint32_t actualMs = millis();
    if (haveAcceptedActual) recordMaximum(timing.maxPacketGapMs, actualMs - lastAcceptedActualMs);
    lastAcceptedActualMs = actualMs;
    haveAcceptedActual = true;
    ++timing.accepted;

    if (killRequested) {
        latchFailsafe(nowMs, 2);
        sendAck(sourceIp, sourcePort, session, sequence);
        return;
    }

    const uint16_t roll = clampChannel(readLe16(&packet[16]));
    const uint16_t pitch = clampChannel(readLe16(&packet[18]));
    const uint16_t throttle = clampChannel(readLe16(&packet[20]));
    const uint16_t yaw = clampChannel(readLe16(&packet[22]));

    if (!armRequested) {
        setDisarmedChannels();
        armLowSeen = true;
        linkState = LinkState::Disarmed;
    } else if (linkState == LinkState::Disarmed && armLowSeen &&
               throttle <= ARM_MAX_THROTTLE_US) {
        linkState = LinkState::Armed;
        armLowSeen = false;
        timing = TimingDiagnostics{};
        faultCause = 0;
        faultTiming = TimingDiagnostics{};
        faultAtMs = faultPacketAgeMs = faultSequence = faultSession = 0;
        faultFreeHeap = faultStations = 0;
        faultThrottle = 1000;
        previousLoopUs = micros();
    }

    if (linkState == LinkState::Armed) {
        channelsUs[CHANNEL_ROLL] = roll;
        channelsUs[CHANNEL_PITCH] = pitch;
        channelsUs[CHANNEL_THROTTLE] = throttle;
        channelsUs[CHANNEL_YAW] = yaw;
        channelsUs[CHANNEL_ARM] = 2000;
        channelsUs[CHANNEL_ANGLE] = 2000;
    }

    sendAck(sourceIp, sourcePort, session, sequence);
}

void readUdpPackets(uint32_t nowMs) {
    int packetLength = 0;
    while ((packetLength = udp.parsePacket()) > 0) {
        uint8_t packet[CONTROL_PACKET_SIZE];
        const IPAddress sourceIp = udp.remoteIP();
        const uint16_t sourcePort = udp.remotePort();
        const int bytesRead = udp.read(packet, sizeof(packet));

        // Drain oversized datagrams. They are never considered valid.
        while (udp.available() > 0) udp.read();

        if (packetLength == static_cast<int>(CONTROL_PACKET_SIZE) &&
            bytesRead == static_cast<int>(CONTROL_PACKET_SIZE) &&
            packetHasValidEnvelope(packet, sizeof(packet))) {
            acceptControlPacket(packet, sourceIp, sourcePort, nowMs);
        } else {
            ++timing.invalid;
        }
    }
}

void updateFailsafe(uint32_t nowMs) {
    if ((linkState == LinkState::Armed || linkState == LinkState::Disarmed) &&
        nowMs - lastValidPacketMs > LINK_TIMEOUT_MS) {
        if (linkState == LinkState::Armed) {
            latchFailsafe(nowMs, 1);
        } else {
            linkState = LinkState::Waiting;
            armLowSeen = false;
        }
    }
}

bool shouldSendCrsf(uint32_t nowMs) {
    if (linkState == LinkState::Armed || linkState == LinkState::Disarmed) return true;
    if (linkState == LinkState::FailsafeLatched &&
        static_cast<int32_t>(disarmBurstUntilMs - nowMs) > 0) return true;
    return false;
}

}  // namespace

void setup() {
    bootId = ESP.random();
    // GPIO1/GPIO3 are the standard ESP8285 receiver UART pins used by the
    // BETAFPV AIO ExpressLRS target. Do not print debug text on Serial.
    Serial.setRxBufferSize(64);
    Serial.begin(CRSF_BAUD, SERIAL_8N1);

    WiFi.persistent(false);
    WiFi.mode(WIFI_OFF);
    delay(20);
    WiFi.mode(WIFI_AP);
    WiFi.setSleepMode(WIFI_NONE_SLEEP);
    // The SDK keeps the PHY mode in flash across firmware updates. Set the
    // default explicitly so an earlier 802.11b-only test build cannot linger.
    WiFi.setPhyMode(WIFI_PHY_MODE_11N);
    WiFi.softAP(AP_SSID, AP_PASSWORD, AP_CHANNEL, false, 1);
    udp.begin(CONTROL_PORT);
    diagnosticUdp.begin(DIAGNOSTIC_PORT);

    maintenanceServer.on("/", HTTP_GET, []() {
        if (!maintenanceServer.authenticate("air75", AP_PASSWORD)) {
            maintenanceServer.requestAuthentication();
            return;
        }
        maintenanceServer.send(
            200,
            "text/html",
            "<!doctype html><title>Air75 maintenance</title>"
            "<h1>Air75 Wi-Fi bridge</h1>"
            "<p>Controller traffic must be stopped before updating.</p>"
            "<p><a href='/update'>Upload firmware</a></p>"
        );
    });
    httpUpdater.setup(&maintenanceServer, "/update", "air75", AP_PASSWORD);
    maintenanceServer.begin();
}

void loop() {
    const uint32_t loopUs = micros();
    if (previousLoopUs) recordMaximum(timing.maxLoopUs, loopUs - previousLoopUs);
    previousLoopUs = loopUs;
    const uint32_t nowMs = millis();
    readUdpPackets(nowMs);
    recordMaximum(timing.maxUdpUs, micros() - loopUs);
    updateFailsafe(nowMs);

    // Keep maintenance traffic out of the real-time control path. Stop the PC
    // client and wait for WAITING before opening or uploading from /update.
    if (linkState == LinkState::Waiting) maintenanceServer.handleClient();

    // Betaflight may transmit CRSF telemetry on the return wire. This bridge
    // currently ignores it, but drains the UART receive buffer.
    const uint32_t drainStartUs = micros();
    while (Serial.available() > 0) { Serial.read(); ++timing.uartBytes; }
    recordMaximum(timing.maxUartDrainUs, micros() - drainStartUs);

    const uint32_t nowUs = micros();
    if (shouldSendCrsf(nowMs) && nowUs - lastCrsfFrameUs >= CRSF_FRAME_PERIOD_US) {
        lastCrsfFrameUs = nowUs;
        sendCrsfChannels();
        recordMaximum(timing.maxCrsfWriteUs, micros() - nowUs);
    }

    serveDiagnostics();
    const uint32_t yieldStartUs = micros();
    delay(0);  // service the ESP8266 Wi-Fi stack and watchdog
    recordMaximum(timing.maxYieldUs, micros() - yieldStartUs);
}
