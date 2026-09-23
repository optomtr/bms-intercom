"""Constants for the BMS Intercom integration."""
from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "bms_intercom"

# Custom config keys (host/username/password/name use Home Assistant's own keys)
CONF_MODE = "mode"
CONF_RTSP_PORT = "rtsp_port"
CONF_HTTP_PORT = "http_port"
CONF_DOOR_NO = "door_no"
# Video channel of the panel: 101 = main stream, 102 = sub stream, 1 = legacy.
CONF_CHANNEL = "channel"
# Use the long-lived ISAPI event stream instead of polling the call status.
CONF_USE_ALERT_STREAM = "use_alert_stream"
# Seconds after which an unfinished call falls back to "idle".
CONF_CALL_TIMEOUT = "call_timeout"

# Option: HTTPS address of Home Assistant (e.g. https://10.10.10.10:8443) where
# the browser microphone is allowed (secure context). The popup routes the
# operator there when opened over plain http.
CONF_HTTPS_URL = "https_url"

# Built-in auto HTTPS endpoint (reverse proxy to HA) so the microphone works
# without any manual setup. Port is configurable but defaults out of the box.
CONF_PROXY_PORT = "proxy_port"
DEFAULT_PROXY_PORT = 8443

# Modes
MODE_DEMO = "demo"
MODE_REAL = "real"

# Defaults
DEFAULT_NAME = "Домофон"
DEFAULT_RTSP_PORT = 554
DEFAULT_HTTP_PORT = 80
DEFAULT_DOOR_NO = 1
DEFAULT_CHANNEL = 101
DEFAULT_USE_ALERT_STREAM = True
DEFAULT_CALL_TIMEOUT = 60

# How often (seconds) to poll the panel for call status when the event stream
# is off (or the model has no alertStream).
CALL_POLL_INTERVAL = 1.5

# Reconnect backoff for the alertStream connection.
ALERT_BACKOFF_START = 2
ALERT_BACKOFF_MAX = 60

# Service exposed for diagnostics: bms_intercom.probe
SERVICE_PROBE = "probe"
ATTR_TEST_DOOR = "test_door"

PLATFORMS: list[Platform] = [
    Platform.CAMERA,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
]

# RTSP main stream path (channel 101 = main, 102 = sub). Kept for compatibility;
# the live path is chosen at runtime by ISAPIClient (see endpoints.rtsp_paths).
RTSP_STREAM_PATH = "/Streaming/Channels/101"

# Dispatcher signal — entities re-render when device state changes.
SIGNAL_STATE_UPDATED = f"{DOMAIN}_state_updated_{{}}"
