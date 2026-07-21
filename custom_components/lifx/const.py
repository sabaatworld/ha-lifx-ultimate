"""Const for LIFX."""

import logging
from typing import TYPE_CHECKING

from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from .manager import LIFXManager

DOMAIN = "lifx"
DATA_LIFX_MANAGER: HassKey[LIFXManager] = HassKey(DOMAIN)

TARGET_ANY = "00:00:00:00:00:00"

DISCOVERY_INTERVAL = 10
# The number of seconds before we will no longer accept a response
# to a message and consider it invalid
MESSAGE_TIMEOUT = 18
# Disable the retries in the library since they are not spaced out
# enough to account for WiFi and UDP dropouts
MESSAGE_RETRIES = 1
OVERALL_TIMEOUT = 15
UNAVAILABLE_GRACE = 90

# The number of times to retry a request message
DEFAULT_ATTEMPTS = 5
# The maximum time to wait for a bulb to respond to an update
MAX_UPDATE_TIME = 90
# The number of tries to send each request message to a bulb during an update
MAX_ATTEMPTS_PER_UPDATE_REQUEST_MESSAGE = 5
LIFX_STATE_SETTLE_DELAY = 0.3
PHYSICAL_LIGHT_POLL_INTERVAL = 10
DEVICE_GROUP_OPTIMISTIC_STATE_EXPIRY = PHYSICAL_LIGHT_POLL_INTERVAL * 1.5
DEVICE_GROUP_KEEPALIVE_INTERVAL = 120
DEVICE_GROUP_KEEPALIVE_MAX_CONSECUTIVE_FAILURES = 2

CONF_LABEL = "label"
CONF_SERIAL = "serial"
CONF_ENTRY_TYPE = "entry_type"
CONF_GROUP_ID = "group_id"
CONF_MEMBERS = "members"

ENTRY_TYPE_PARALLEL_GROUP = "parallel_group"

IDENTIFY_WAVEFORM = {
    "transient": True,
    "color": [0, 0, 1, 3500],
    "skew_ratio": 0,
    "period": 1000,
    "cycles": 3,
    "waveform": 1,
    "set_hue": True,
    "set_saturation": True,
    "set_brightness": True,
    "set_kelvin": True,
}
IDENTIFY = "identify"
RESTART = "restart"

ATTR_DURATION = "duration"
ATTR_INDICATION = "indication"
ATTR_INFRARED = "infrared"
ATTR_POWER = "power"
ATTR_REMAINING = "remaining"
ATTR_RSSI = "rssi"
ATTR_ZONES = "zones"

ATTR_THEME = "theme"
TRANSITION_OFF_DURATION = "transition_off_duration"
TRANSITION_ON_DURATION = "transition_on_duration"
TRANSITION_CROSS_DURATION = "transition_cross_duration"

HEV_CYCLE_STATE = "hev_cycle_state"
INFRARED_BRIGHTNESS = "infrared_brightness"
INFRARED_BRIGHTNESS_VALUES_MAP = {
    0: "Disabled",
    16383: "25%",
    32767: "50%",
    65535: "100%",
}

LIFX_CEILING_PRODUCT_IDS = {176, 177, 201, 202}
LIFX_128ZONE_CEILING_PRODUCT_IDS = {201, 202}

_LOGGER = logging.getLogger(__package__)
