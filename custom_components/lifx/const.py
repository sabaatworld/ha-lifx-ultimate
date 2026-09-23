"""Const for LIFX."""

import logging
from typing import TYPE_CHECKING

from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from .manager import LIFXManager

DOMAIN = "lifx"
DATA_LIFX_MANAGER: HassKey[LIFXManager] = HassKey(DOMAIN)

CONF_GROUP = "group"

PHYSICAL_LIGHT_POLL_INTERVAL = 10
DEVICE_GROUP_OPTIMISTIC_STATE_EXPIRY = PHYSICAL_LIGHT_POLL_INTERVAL * 1.5
DEVICE_GROUP_KEEPALIVE_INTERVAL = 120
DEVICE_GROUP_KEEPALIVE_MAX_CONSECUTIVE_FAILURES = 2
DEVICE_GROUP_MEMBER_RECONNECT_INTERVAL = 60

CONF_LABEL = "label"
CONF_MAC_ADDRESS = "mac_address"
CONF_SERIAL = "serial"
CONF_TITLE = "title"
CONF_ENTRY_TYPE = "entry_type"
CONF_GROUP_ID = "group_id"
CONF_MEMBERS = "members"

ENTRY_TYPE_PARALLEL_GROUP = "parallel_group"

IDENTIFY = "identify"
RESTART = "restart"

ATTR_DURATION = "duration"
ATTR_INFRARED = "infrared"
ATTR_POWER = "power"
ATTR_RSSI = "rssi"
ATTR_ZONES = "zones"

ATTR_THEME = "theme"
TRANSITION_OFF_DURATION = "transition_off_duration"
TRANSITION_ON_DURATION = "transition_on_duration"
TRANSITION_CROSS_DURATION = "transition_cross_duration"

ATTR_CHANGE = "change"
ATTR_CLOUD_SATURATION_MAX = "cloud_saturation_max"
ATTR_CLOUD_SATURATION_MIN = "cloud_saturation_min"
ATTR_CYCLES = "cycles"
ATTR_DIRECTION = "direction"
ATTR_PALETTE = "palette"
ATTR_PERIOD = "period"
ATTR_POWER_ON = "power_on"
ATTR_SATURATION_MAX = "saturation_max"
ATTR_SATURATION_MIN = "saturation_min"
ATTR_SKY_TYPE = "sky_type"
ATTR_SPEED = "speed"
ATTR_SPREAD = "spread"

SERVICE_EFFECT_COLORLOOP = "effect_colorloop"
SERVICE_EFFECT_FLAME = "effect_flame"
SERVICE_EFFECT_MORPH = "effect_morph"
SERVICE_EFFECT_MOVE = "effect_move"
SERVICE_EFFECT_PULSE = "effect_pulse"
SERVICE_EFFECT_SKY = "effect_sky"
SERVICE_EFFECT_STOP = "effect_stop"
SERVICE_PAINT_THEME = "paint_theme"
SERVICE_SET_HEV_CYCLE_STATE = "set_hev_cycle_state"
SERVICE_SET_STATE = "set_state"

HEV_CYCLE_STATE = "hev_cycle_state"
LIFX_IDENTIFY_DELAY = 3.0
INFRARED_BRIGHTNESS = "infrared_brightness"
INFRARED_LEVELS = {
    "Disabled": 0.0,
    "25%": 0.25,
    "50%": 0.5,
    "100%": 1.0,
}

LOGGER = logging.getLogger(__package__)
