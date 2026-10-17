"""Packet-free, target-owned LIFX virtual-power policy controls."""

from typing import Any, override

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_VIRTUAL_POWER_OFF
from .coordinator import LIFXUpdateCoordinator
from .entity import LIFXEntity
from .parallel_group import LIFXParallelGroupEntity, LIFXParallelGroupRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the physical or Device Group policy adapter."""
    if isinstance(entry.runtime_data, LIFXUpdateCoordinator):
        async_add_entities([LIFXVirtualPowerSwitch(entry)])
    else:
        async_add_entities([LIFXGroupVirtualPowerSwitch(entry, entry.runtime_data)])


class _VirtualPowerPolicySwitch(SwitchEntity):
    """Persist policy in options without device writes, reloads, or cached state."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "virtual_power_off"
    _attr_should_poll = False
    entry: ConfigEntry

    @property
    @override
    def is_on(self) -> bool:
        return bool(self.entry.options.get(CONF_VIRTUAL_POWER_OFF, True))

    async def _async_set_enabled(self, enabled: bool) -> None:
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_VIRTUAL_POWER_OFF: enabled}
        )
        self.async_write_ha_state()

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_enabled(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_enabled(False)


class LIFXVirtualPowerSwitch(LIFXEntity, _VirtualPowerPolicySwitch):
    """Physical device policy, available independently of its light entity."""

    def __init__(self, entry: ConfigEntry) -> None:
        """Bind the physical entry and existing device metadata."""
        super().__init__(entry.runtime_data)
        self.entry = entry
        self._attr_unique_id = (
            f"{self.coordinator.serial_number}_{CONF_VIRTUAL_POWER_OFF}"
        )


class LIFXGroupVirtualPowerSwitch(LIFXParallelGroupEntity, _VirtualPowerPolicySwitch):
    """Device Group policy, independent of every physical member policy."""

    def __init__(self, entry: ConfigEntry, runtime: LIFXParallelGroupRuntime) -> None:
        """Bind the group entry and existing virtual device metadata."""
        super().__init__(runtime)
        self.entry = entry
        self._attr_unique_id = f"{runtime.group_id}_{CONF_VIRTUAL_POWER_OFF}"
