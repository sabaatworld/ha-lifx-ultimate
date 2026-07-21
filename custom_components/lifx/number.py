"""Number entities for LIFX devices."""

from typing import override

from homeassistant.components.number import NumberEntityDescription, RestoreNumber
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    TRANSITION_CROSS_DURATION,
    TRANSITION_OFF_DURATION,
    TRANSITION_ON_DURATION,
)
from .coordinator import LIFXConfigEntry, LIFXUpdateCoordinator
from .entity import LIFXEntity
from .parallel_group import async_add_parallel_group_entities

TRANSITION_DURATION_ENTITIES = (
    NumberEntityDescription(
        key=TRANSITION_ON_DURATION,
        translation_key=TRANSITION_ON_DURATION,
        entity_category=EntityCategory.CONFIG,
        native_min_value=0,
        native_max_value=300,
        native_step=0.1,
        native_unit_of_measurement="s",
    ),
    NumberEntityDescription(
        key=TRANSITION_OFF_DURATION,
        translation_key=TRANSITION_OFF_DURATION,
        entity_category=EntityCategory.CONFIG,
        native_min_value=0,
        native_max_value=300,
        native_step=0.1,
        native_unit_of_measurement="s",
    ),
    NumberEntityDescription(
        key=TRANSITION_CROSS_DURATION,
        translation_key=TRANSITION_CROSS_DURATION,
        entity_category=EntityCategory.CONFIG,
        native_min_value=0,
        native_max_value=300,
        native_step=0.1,
        native_unit_of_measurement="s",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LIFXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up LIFX number entities."""
    if not isinstance(entry.runtime_data, LIFXUpdateCoordinator):
        async_add_parallel_group_entities(entry, async_add_entities, Platform.NUMBER)
        return

    coordinator = entry.runtime_data
    async_add_entities(
        LIFXTransitionDurationNumber(coordinator, description)
        for description in TRANSITION_DURATION_ENTITIES
    )


class LIFXTransitionDurationNumber(LIFXEntity, RestoreNumber):
    """LIFX transition duration configuration entity."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: LIFXUpdateCoordinator,
        description: NumberEntityDescription,
    ) -> None:
        """Initialize the transition duration entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.serial_number}_{description.key}"
        self._attr_native_value = 0.0

    @override
    async def async_added_to_hass(self) -> None:
        """Restore the transition duration."""
        await super().async_added_to_hass()
        if (
            (last_data := await self.async_get_last_number_data()) is not None
            and (value := last_data.native_value) is not None
            and self.native_min_value <= value <= self.native_max_value
        ):
            self._attr_native_value = value
        self._async_set_coordinator_value(self.native_value)

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Set the transition duration."""
        self._attr_native_value = value
        self._async_set_coordinator_value(value)
        self.async_write_ha_state()

    def _async_set_coordinator_value(self, value: float | None) -> None:
        """Update the transition duration stored on the coordinator."""
        assert value is not None
        if self.entity_description.key == TRANSITION_ON_DURATION:
            self.coordinator.transition_on_duration = value
        elif self.entity_description.key == TRANSITION_OFF_DURATION:
            self.coordinator.transition_off_duration = value
        else:
            self.coordinator.transition_cross_duration = value
