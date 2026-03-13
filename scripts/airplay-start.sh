#!/usr/bin/env bash
# airplay-start.sh — shairport-sync hook: before_play_begins
#
# When AirPlay playback begins, this script:
#   1. Connects the BT device (retries until success)
#   2. Waits for the A2DP sink to appear in PulseAudio
#   3. Sends an MQTT command to bt-classic-mqtt (device-specific, via env var)
#   4. Routes shairport-sync audio to the BT sink
#
# Required environment variables (set in /etc/default/shairport-sync):
#   BT_MAC              Bluetooth MAC address of the device
#   MQTT_HOST           MQTT broker host
#
# Optional environment variables:
#   MQTT_PORT           MQTT broker port (default: 1883)
#   MQTT_USERNAME       MQTT username
#   MQTT_PASSWORD       MQTT password
#   MQTT_TOPIC          MQTT command topic (default: bt-classic-mqtt/command)
#   MQTT_COMMAND        JSON payload to send on playback start
#                       (default: '{"power": true}')
#                       e.g. '{"input": "Bluetooth", "sound_mode": "Music"}'
#   BT_MAX_ATTEMPTS     max bluetoothctl connect attempts (default: 10)
#   BT_RETRY_DELAY      seconds between retries (default: 3)
#   SINK_MAX_WAIT       max seconds to wait for bluez_sink (default: 15)
#   SPP_SETTLE_TIME     seconds to wait for SPP socket after BT connect (default: 3)

set -euo pipefail

log() { echo "[airplay-start] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
BT_MAC="${BT_MAC:?BT_MAC is required}"
MQTT_HOST="${MQTT_HOST:?MQTT_HOST is required}"
MQTT_PORT="${MQTT_PORT:-1883}"
MQTT_TOPIC="${MQTT_TOPIC:-bt-classic-mqtt/command}"
MQTT_COMMAND="${MQTT_COMMAND:-{\"power\": true}}"

BT_MAX_ATTEMPTS="${BT_MAX_ATTEMPTS:-10}"
BT_RETRY_DELAY="${BT_RETRY_DELAY:-3}"
SINK_MAX_WAIT="${SINK_MAX_WAIT:-15}"
SPP_SETTLE_TIME="${SPP_SETTLE_TIME:-3}"

BT_SINK="bluez_sink.${BT_MAC//:/_}.a2dp_sink"

# Build mosquitto_pub auth args
MQTT_ARGS=(-h "$MQTT_HOST" -p "$MQTT_PORT" -t "$MQTT_TOPIC")
[ -n "${MQTT_USERNAME:-}" ] && MQTT_ARGS+=(-u "$MQTT_USERNAME")
[ -n "${MQTT_PASSWORD:-}" ] && MQTT_ARGS+=(-P "$MQTT_PASSWORD")

# ------------------------------------------------------------------
# 1. Connect Bluetooth — retry until successful
# ------------------------------------------------------------------
log "Connecting to $BT_MAC …"
connected=false
for ((i=1; i<=BT_MAX_ATTEMPTS; i++)); do
    output=$(bluetoothctl connect "$BT_MAC" 2>&1 || true)
    if echo "$output" | grep -q "Connection successful"; then
        log "Connected (attempt $i)"
        connected=true
        break
    fi
    log "Attempt $i/$BT_MAX_ATTEMPTS failed — retrying in ${BT_RETRY_DELAY}s"
    sleep "$BT_RETRY_DELAY"
done
$connected || die "Failed to connect after $BT_MAX_ATTEMPTS attempts"

# Wait for SPP socket — device needs time to open it after BT connect
sleep "$SPP_SETTLE_TIME"

# ------------------------------------------------------------------
# 2. Wait for bluez_sink to appear in PulseAudio
# ------------------------------------------------------------------
log "Waiting for $BT_SINK …"
waited=0
while ! pactl list sinks short 2>/dev/null | grep -q "$BT_SINK"; do
    if ((waited >= SINK_MAX_WAIT)); then
        die "Timed out waiting for $BT_SINK"
    fi
    sleep 1
    ((waited++))
done
log "Sink ready after ${waited}s"

# ------------------------------------------------------------------
# 3. Send MQTT command (before routing — triggers bt-classic-mqtt reconnect)
# ------------------------------------------------------------------
log "Sending MQTT command: $MQTT_COMMAND"
mosquitto_pub "${MQTT_ARGS[@]}" -m "$MQTT_COMMAND" || \
    log "WARNING: mosquitto_pub failed (bridge may still reconnect)"

# ------------------------------------------------------------------
# 4. Route shairport-sync audio to BT sink
# ------------------------------------------------------------------
SINK_INPUT=$(pactl list sink-inputs short 2>/dev/null | grep -i shairport | awk '{print $1}' | head -1 || true)
if [ -n "$SINK_INPUT" ]; then
    pactl move-sink-input "$SINK_INPUT" "$BT_SINK" && \
        log "Moved sink-input $SINK_INPUT → $BT_SINK"
fi
pactl set-default-sink "$BT_SINK" && \
    log "Set default sink → $BT_SINK"

log "Done"