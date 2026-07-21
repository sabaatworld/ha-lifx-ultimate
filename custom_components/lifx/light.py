"""Support for LIFX lights."""

import asyncio
from dataclasses import asdict, dataclass
from typing import Any, override

import aiolifx_effects as aiolifx_effects_module
import voluptuous as vol

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_BRIGHTNESS_STEP,
    ATTR_BRIGHTNESS_STEP_PCT,
    ATTR_EFFECT,
    ATTR_TRANSITION,
    LIGHT_TURN_ON_SCHEMA,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.typing import VolDictType

from .const import (
    _LOGGER,
    ATTR_DURATION,
    ATTR_INFRARED,
    ATTR_POWER,
    ATTR_ZONES,
    DATA_LIFX_MANAGER,
    DOMAIN,
    INFRARED_BRIGHTNESS,
    LIFX_CEILING_PRODUCT_IDS,
    LIFX_STATE_SETTLE_DELAY,
)
from .coordinator import FirmwareEffect, LIFXConfigEntry, LIFXUpdateCoordinator
from .entity import LIFXEntity
from .manager import (
    SERVICE_EFFECT_COLORLOOP,
    SERVICE_EFFECT_FLAME,
    SERVICE_EFFECT_MORPH,
    SERVICE_EFFECT_MOVE,
    SERVICE_EFFECT_PULSE,
    SERVICE_EFFECT_SKY,
    SERVICE_EFFECT_STOP,
    LIFXManager,
)
from .parallel_group import async_add_parallel_group_entities
from .util import convert_8_to_16, convert_16_to_8, find_hsbk, lifx_features, merge_hsbk

SERVICE_LIFX_SET_STATE = "set_state"

LIFX_SET_STATE_SCHEMA: VolDictType = {
    **LIGHT_TURN_ON_SCHEMA,
    ATTR_INFRARED: vol.All(vol.Coerce(int), vol.Clamp(min=0, max=255)),
    ATTR_ZONES: vol.All(cv.ensure_list, [cv.positive_int]),
    ATTR_POWER: cv.boolean,
}


SERVICE_LIFX_SET_HEV_CYCLE_STATE = "set_hev_cycle_state"

LIFX_SET_HEV_CYCLE_STATE_SCHEMA: VolDictType = {
    vol.Required(ATTR_POWER): cv.boolean,
    ATTR_DURATION: vol.All(vol.Coerce(float), vol.Clamp(min=0, max=86400)),
}

HSBK_HUE = 0
HSBK_SATURATION = 1
HSBK_BRIGHTNESS = 2
HSBK_KELVIN = 3


@dataclass(frozen=True, slots=True)
class LIFXVirtualPowerStoredData(ExtraStoredData):
    """Stored whole-light virtual power state."""

    virtual_off: bool
    resume_hsbk: tuple[int, int | None, int, int] | None

    @override
    def as_dict(self) -> dict[str, Any]:
        """Return JSON-serializable virtual power data."""
        return asdict(self)

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> LIFXVirtualPowerStoredData:
        """Restore state from Home Assistant storage."""
        color = restored.get("resume_hsbk")
        return cls(
            bool(restored.get("virtual_off")),
            tuple(color) if color is not None and len(color) == 4 else None,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LIFXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up LIFX from a config entry."""
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_LIFX_SET_STATE,
        LIFX_SET_STATE_SCHEMA,
        "set_state",
    )
    if not isinstance(entry.runtime_data, LIFXUpdateCoordinator):
        async_add_parallel_group_entities(entry, async_add_entities, Platform.LIGHT)
        return

    coordinator = entry.runtime_data
    manager = hass.data[DATA_LIFX_MANAGER]
    device = coordinator.device
    platform.async_register_entity_service(
        SERVICE_LIFX_SET_HEV_CYCLE_STATE,
        LIFX_SET_HEV_CYCLE_STATE_SCHEMA,
        "set_hev_cycle_state",
    )
    if lifx_features(device)["matrix"]:
        if device.product in LIFX_CEILING_PRODUCT_IDS:
            entity: LIFXLight = LIFXCeiling(coordinator, manager, entry)
        else:
            entity = LIFXMatrix(coordinator, manager, entry)
    elif lifx_features(device)["extended_multizone"]:
        entity = LIFXExtendedMultiZone(coordinator, manager, entry)
    elif lifx_features(device)["multizone"]:
        entity = LIFXMultiZone(coordinator, manager, entry)
    elif lifx_features(device)["color"]:
        entity = LIFXColor(coordinator, manager, entry)
    else:
        entity = LIFXWhite(coordinator, manager, entry)
    async_add_entities([entity])


class LIFXLight(LIFXEntity, LightEntity, RestoreEntity):
    """Representation of a LIFX light."""

    _attr_supported_features = LightEntityFeature.TRANSITION | LightEntityFeature.EFFECT
    _attr_name = None

    def __init__(
        self,
        coordinator: LIFXUpdateCoordinator,
        manager: LIFXManager,
        entry: LIFXConfigEntry,
    ) -> None:
        """Initialize the light."""
        super().__init__(coordinator)

        self.mac_addr = self.bulb.mac_addr
        bulb_features = lifx_features(self.bulb)
        self.manager = manager
        self.effects_conductor: aiolifx_effects_module.Conductor = (
            manager.effects_conductor
        )
        self.entry = entry
        self._attr_unique_id = self.coordinator.serial_number
        self._attr_min_color_temp_kelvin = bulb_features["min_kelvin"]
        self._attr_max_color_temp_kelvin = bulb_features["max_kelvin"]
        if bulb_features["min_kelvin"] != bulb_features["max_kelvin"]:
            color_mode = ColorMode.COLOR_TEMP
        else:
            color_mode = ColorMode.BRIGHTNESS

        self._attr_color_mode = color_mode
        self._attr_supported_color_modes = {color_mode}
        self._attr_effect = None

    @property
    @override
    def brightness(self) -> int:
        """Return the brightness of this light between 0..255."""
        if self.coordinator.virtual_off:
            return 0
        fade = self.bulb.power_level / 65535
        return convert_16_to_8(int(fade * self.bulb.color[HSBK_BRIGHTNESS]))

    @property
    @override
    def color_temp_kelvin(self) -> int | None:
        """Return the color temperature of this light in kelvin."""
        return int(self.bulb.color[HSBK_KELVIN])

    @property
    @override
    def is_on(self) -> bool:
        """Return true if light is on."""
        return self.coordinator.actual_power_on and not self.coordinator.virtual_off

    @property
    @override
    def effect(self) -> str | None:
        """Return the name of the currently running effect."""
        if effect := self.effects_conductor.effect(self.bulb):
            return f"effect_{effect.name}"
        if effect := self.coordinator.async_get_active_effect():
            return f"effect_{FirmwareEffect(effect).name.lower()}"
        return None

    async def update_during_transition(self, when: int) -> None:
        """Update state at the start and end of a transition."""
        self.async_write_ha_state()
        await self.coordinator.async_schedule_post_command_refresh(when)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        await self.set_state(**{**kwargs, ATTR_POWER: True})

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self.set_state(**{**kwargs, ATTR_POWER: False})

    def _default_transition_duration(
        self,
        power_on: bool,
        physical_power_off: bool,
        hsbk: list[float | int | None] | None,
    ) -> float:
        """Return the configured duration for a state request."""
        if physical_power_off:
            return self.coordinator.transition_off_duration
        if self.coordinator.virtual_off or power_on:
            return self.coordinator.transition_on_duration
        if hsbk:
            return self.coordinator.transition_cross_duration
        return self.coordinator.transition_on_duration

    async def set_state(self, **kwargs: Any) -> None:
        """Set a color on the light and turn it on/off."""
        self.coordinator.async_set_updated_data(None)
        # Cancel any pending refreshes
        bulb = self.bulb

        await self.effects_conductor.stop([bulb])

        if ATTR_EFFECT in kwargs:
            await self.default_effect(**kwargs)
            return

        if ATTR_INFRARED in kwargs:
            infrared_entity_id = self.coordinator.async_get_entity_id(
                Platform.SELECT, INFRARED_BRIGHTNESS
            )
            _LOGGER.warning(
                (
                    "The 'infrared' attribute of 'lifx.set_state' is deprecated:"
                    " call 'select.select_option' targeting '%s' instead"
                ),
                infrared_entity_id,
            )
            bulb.set_infrared(convert_8_to_16(kwargs[ATTR_INFRARED]))

        if ATTR_BRIGHTNESS_STEP in kwargs or ATTR_BRIGHTNESS_STEP_PCT in kwargs:
            brightness = self.brightness if self.is_on and self.brightness else 0

            if ATTR_BRIGHTNESS_STEP in kwargs:
                brightness += kwargs.pop(ATTR_BRIGHTNESS_STEP)

            else:
                brightness_pct = round(brightness / 255 * 100)
                brightness = round(
                    (brightness_pct + kwargs.pop(ATTR_BRIGHTNESS_STEP_PCT)) / 100 * 255
                )

            kwargs[ATTR_BRIGHTNESS] = max(0, min(255, brightness))

        power_on = kwargs.get(ATTR_POWER, False)
        power_off = not kwargs.get(ATTR_POWER, True)
        physical_power_off = kwargs.get(ATTR_POWER) is False
        hsbk = find_hsbk(self.hass, **kwargs)

        has_transition = ATTR_TRANSITION in kwargs
        if has_transition:
            fade = int(kwargs[ATTR_TRANSITION] * 1000)
        else:
            fade = int(
                self._default_transition_duration(power_on, physical_power_off, hsbk)
                * 1000
            )

        if (
            power_on
            and self.coordinator.virtual_off
            and self.coordinator.resume_hsbk is not None
        ):
            hsbk = merge_hsbk(
                list(self.coordinator.resume_hsbk), hsbk or [None] * 4
            )

        if physical_power_off:
            self.coordinator.async_clear_virtual_off()
            if not self.is_on:
                if has_transition:
                    await self.set_power(False, duration=0)
                else:
                    await self.set_power(False)
                if hsbk:
                    await self.set_color(hsbk, kwargs, duration=fade)
            else:
                if hsbk:
                    await self.set_color(hsbk, kwargs, duration=fade)
                await self.set_power(False, duration=fade)
        elif self.coordinator.virtual_off:
            target = tuple(
                merge_hsbk(list(self.coordinator.display_color), hsbk or [None] * 4)
            )
            if self.coordinator.actual_power_on:
                await self.set_color(list(target), kwargs, duration=fade)
            else:
                await self.set_color(list(target), kwargs)
                await self.set_power(True, duration=fade)
            self.coordinator.async_record_virtual_on(target)
        elif not self.is_on:
            if power_off:
                if has_transition:
                    await self.set_power(False, duration=0)
                else:
                    await self.set_power(False)
            # If fading on with color, set color immediately
            if hsbk and power_on:
                await self.set_color(hsbk, kwargs)
                await self.set_power(True, duration=fade)
            elif hsbk:
                await self.set_color(hsbk, kwargs, duration=fade)
            elif power_on:
                await self.set_power(True, duration=fade)
        else:
            if power_on:
                await self.set_power(True)
            if hsbk:
                await self.set_color(hsbk, kwargs, duration=fade)
            if power_off:
                await self.set_power(False, duration=fade)

        # Update when the transition starts and ends
        await self.update_during_transition(fade)

    async def set_hev_cycle_state(
        self, power: bool, duration: int | None = None
    ) -> None:
        """Set the state of the HEV LEDs on a LIFX Clean bulb."""
        if lifx_features(self.bulb)["hev"] is False:
            raise HomeAssistantError(
                "This device does not support setting HEV cycle state"
            )

        await self.coordinator.async_set_hev_cycle_state(power, duration or 0)
        await self.update_during_transition(duration or 0)

    async def set_power(
        self,
        pwr: bool,
        duration: int = 0,
    ) -> None:
        """Send a power change to the bulb."""
        try:
            await self.coordinator.async_set_power(pwr, duration)
        except TimeoutError as ex:
            raise HomeAssistantError(f"Timeout setting power for {self.name}") from ex

    async def set_color(
        self,
        hsbk: list[float | int | None],
        kwargs: dict[str, Any],
        duration: int = 0,
    ) -> None:
        """Send a color change to the bulb."""
        try:
            await self.transform(hsbk, kwargs=kwargs, duration=duration / 1000)
        except TimeoutError as ex:
            raise HomeAssistantError(f"Timeout setting color for {self.name}") from ex

    async def transform(
        self,
        hsbk: list[float | int | None],
        kwargs: dict[str, Any] | None = None,
        duration: float = 0,
        rapid: bool = False,
    ) -> None:
        """Transform the bulb using a waveform optional message."""
        set_hue = hsbk[HSBK_HUE] is not None
        set_saturation = hsbk[HSBK_SATURATION] is not None
        set_brightness = hsbk[HSBK_BRIGHTNESS] is not None
        set_kelvin = hsbk[HSBK_KELVIN] is not None
        color = merge_hsbk(self.bulb.color, hsbk)

        msg = {
            "transient": False,
            "color": color,
            "cycles": 1,
            "skew_ratio": 0,
            "waveform": 0,
            "period": round(duration * 1000),
            "set_hue": set_hue,
            "set_saturation": set_saturation,
            "set_brightness": set_brightness,
            "set_kelvin": set_kelvin,
        }

        await self.coordinator.async_set_waveform_optional(msg, rapid)

    async def get_color(
        self,
    ) -> None:
        """Send a get color message to the bulb."""
        try:
            await self.coordinator.async_get_color()
        except TimeoutError as ex:
            raise HomeAssistantError(
                f"Timeout setting getting color for {self.name}"
            ) from ex

    async def default_effect(self, **kwargs: Any) -> None:
        """Start an effect with default parameters."""
        await self.hass.services.async_call(
            DOMAIN,
            kwargs[ATTR_EFFECT],
            {ATTR_ENTITY_ID: self.entity_id},
            context=self._context,
        )

    @override
    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        self.async_on_remove(
            self.manager.async_register_entity(self.entity_id, self.coordinator)
        )
        await super().async_added_to_hass()
        if (last_data := await self.async_get_last_extra_data()) is not None:
            data = LIFXVirtualPowerStoredData.from_dict(last_data.as_dict())
            self.coordinator.virtual_off = data.virtual_off
            self.coordinator.resume_hsbk = data.resume_hsbk
        self.coordinator.async_reconcile_virtual_power()

    @override
    @property
    def extra_restore_state_data(self) -> LIFXVirtualPowerStoredData:
        """Return virtual power state persisted by Home Assistant."""
        return LIFXVirtualPowerStoredData(
            self.coordinator.virtual_off, self.coordinator.resume_hsbk
        )

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        self.coordinator.async_cancel_post_command_refresh()
        return await super().async_will_remove_from_hass()


class LIFXWhite(LIFXLight):
    """Representation of a white-only LIFX light."""

    _attr_effect_list = [SERVICE_EFFECT_PULSE, SERVICE_EFFECT_STOP]


class LIFXColor(LIFXLight):
    """Representation of a color LIFX light."""

    _attr_effect_list = [
        SERVICE_EFFECT_COLORLOOP,
        SERVICE_EFFECT_PULSE,
        SERVICE_EFFECT_STOP,
    ]

    @property
    @override
    def supported_color_modes(self) -> set[ColorMode]:
        """Return the supported color modes."""
        return {ColorMode.COLOR_TEMP, ColorMode.HS}

    @property
    @override
    def color_mode(self) -> ColorMode:
        """Return the color mode of the light."""
        has_sat = self.bulb.color[HSBK_SATURATION]
        return ColorMode.HS if has_sat else ColorMode.COLOR_TEMP

    @property
    @override
    def hs_color(self) -> tuple[float, float] | None:
        """Return the hs value."""
        hue, sat, _, _ = self.bulb.color
        hue = hue / 65535 * 360
        sat = sat / 65535 * 100
        return (hue, sat) if sat else None


class LIFXMultiZone(LIFXColor):
    """Representation of a legacy LIFX multizone device."""

    _attr_effect_list = [
        SERVICE_EFFECT_COLORLOOP,
        SERVICE_EFFECT_PULSE,
        SERVICE_EFFECT_MOVE,
        SERVICE_EFFECT_STOP,
    ]

    @override
    async def transform(
        self,
        hsbk: list[float | int | None],
        kwargs: dict[str, Any] | None = None,
        duration: float = 0,
        rapid: bool = False,
    ) -> None:
        """Transform the bulb color, including per-zone updates."""
        bulb = self.bulb
        color_zones = bulb.color_zones
        num_zones = self.coordinator.get_number_of_zones()
        zone_kwargs = kwargs or {}
        duration_ms = round(duration * 1000)

        # Zone brightness is not reported when powered off
        if not self.coordinator.actual_power_on and hsbk[HSBK_BRIGHTNESS] is None:
            await self.set_power(True)
            await asyncio.sleep(LIFX_STATE_SETTLE_DELAY)
            await self.update_color_zones()
            await self.set_power(False)

        if (zones := zone_kwargs.get(ATTR_ZONES)) is None:
            # Fast track: setting all zones to the same brightness and color
            # can be treated as a single-zone bulb.
            first_zone = color_zones[0]
            first_zone_brightness = first_zone[HSBK_BRIGHTNESS]
            all_zones_have_same_brightness = all(
                color_zones[zone][HSBK_BRIGHTNESS] == first_zone_brightness
                for zone in range(num_zones)
            )
            all_zones_are_the_same = all(
                color_zones[zone] == first_zone for zone in range(num_zones)
            )
            if (
                all_zones_have_same_brightness or hsbk[HSBK_BRIGHTNESS] is not None
            ) and (all_zones_are_the_same or hsbk[HSBK_KELVIN] is not None):
                await super().transform(
                    hsbk, kwargs=zone_kwargs, duration=duration, rapid=rapid
                )
                return

            zones = list(range(num_zones))
        else:
            zones = [x for x in set(zones) if x < num_zones]

        # Send new color to each zone
        for index, zone in enumerate(zones):
            zone_hsbk = merge_hsbk(color_zones[zone], hsbk)
            apply = 1 if (index == len(zones) - 1) else 0
            try:
                await self.coordinator.async_set_color_zones(
                    zone, zone, zone_hsbk, duration_ms, apply
                )
            except TimeoutError as ex:
                raise HomeAssistantError(
                    f"Timeout setting color zones for {self.name}"
                ) from ex

        # set_color_zones does not update the
        # state of the device, so we need to do that
        await self.get_color()

    async def update_color_zones(
        self,
    ) -> None:
        """Send a get color zones message to the device."""
        try:
            await self.coordinator.async_get_color_zones()
        except TimeoutError as ex:
            raise HomeAssistantError(
                f"Timeout getting color zones from {self.name}"
            ) from ex


class LIFXExtendedMultiZone(LIFXMultiZone):
    """Representation of a LIFX device that supports extended multizone messages."""

    @override
    async def transform(
        self,
        hsbk: list[float | int | None],
        kwargs: dict[str, Any] | None = None,
        duration: float = 0,
        rapid: bool = False,
    ) -> None:
        """Set colors on all zones of the device."""
        zone_kwargs = kwargs or {}

        # trigger an update of all zone values before merging new values
        await self.coordinator.async_get_extended_color_zones()

        color_zones = self.bulb.color_zones
        if (zones := zone_kwargs.get(ATTR_ZONES)) is None:
            # merge the incoming hsbk across all zones
            for index, zone in enumerate(color_zones):
                color_zones[index] = merge_hsbk(zone, hsbk)
        else:
            # merge the incoming HSBK with only the specified zones
            for index, zone in enumerate(color_zones):
                if index in zones:
                    color_zones[index] = merge_hsbk(zone, hsbk)

        # send the updated color zones list to the device
        try:
            await self.coordinator.async_set_extended_color_zones(
                color_zones, duration=round(duration * 1000)
            )
        except TimeoutError as ex:
            raise HomeAssistantError(
                f"Timeout setting color zones on {self.name}"
            ) from ex

        # set_extended_color_zones does not update the
        # state of the device, so we need to do that
        await self.get_color()


class LIFXMatrix(LIFXColor):
    """Representation of a LIFX matrix device."""

    _attr_effect_list = [
        SERVICE_EFFECT_COLORLOOP,
        SERVICE_EFFECT_FLAME,
        SERVICE_EFFECT_PULSE,
        SERVICE_EFFECT_MORPH,
        SERVICE_EFFECT_STOP,
    ]


class LIFXCeiling(LIFXMatrix):
    """Representation of a LIFX Ceiling device."""

    _attr_effect_list = [
        SERVICE_EFFECT_COLORLOOP,
        SERVICE_EFFECT_FLAME,
        SERVICE_EFFECT_PULSE,
        SERVICE_EFFECT_MORPH,
        SERVICE_EFFECT_SKY,
        SERVICE_EFFECT_STOP,
    ]
