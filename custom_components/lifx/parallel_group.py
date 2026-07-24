"""Virtual, low-skew LIFX parallel group entities."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from statistics import fmean
import time
from typing import Any, Literal, override

from aiolifx_themes.themes import Theme, ThemeLibrary

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.components.light import (
    ATTR_EFFECT,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.components.number import NumberEntityDescription, RestoreNumber
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import (
    _LOGGER,
    CONF_GROUP_ID,
    CONF_MEMBERS,
    DATA_LIFX_MANAGER,
    DEVICE_GROUP_KEEPALIVE_INTERVAL,
    DEVICE_GROUP_KEEPALIVE_MAX_CONSECUTIVE_FAILURES,
    DEVICE_GROUP_MEMBER_RECONNECT_INTERVAL,
    DEVICE_GROUP_OPTIMISTIC_STATE_EXPIRY,
    DOMAIN,
    LIFX_CEILING_PRODUCT_IDS,
    TRANSITION_CROSS_DURATION,
    TRANSITION_OFF_DURATION,
    TRANSITION_ON_DURATION,
)
from .coordinator import LIFXUpdateCoordinator
from .manager import (
    ATTR_CHANGE,
    ATTR_CYCLES,
    ATTR_DIRECTION,
    ATTR_PALETTE,
    ATTR_PERIOD,
    ATTR_POWER_ON,
    ATTR_SATURATION_MAX,
    ATTR_SATURATION_MIN,
    ATTR_SKY_TYPE,
    ATTR_SPEED,
    ATTR_THEME,
    EFFECT_FLAME_DEFAULT_SPEED,
    EFFECT_MORPH_DEFAULT_SPEED,
    EFFECT_MOVE_DEFAULT_DIRECTION,
    EFFECT_MOVE_DEFAULT_SPEED,
    EFFECT_SKY_DEFAULT_CLOUD_SATURATION_MAX,
    EFFECT_SKY_DEFAULT_CLOUD_SATURATION_MIN,
    EFFECT_SKY_DEFAULT_SKY_TYPE,
    EFFECT_SKY_DEFAULT_SPEED,
    PAINT_THEME_DEFAULT_TRANSITION,
    SERVICE_EFFECT_COLORLOOP,
    SERVICE_EFFECT_FLAME,
    SERVICE_EFFECT_MORPH,
    SERVICE_EFFECT_MOVE,
    SERVICE_EFFECT_PULSE,
    SERVICE_EFFECT_SKY,
    SERVICE_EFFECT_STOP,
    SERVICE_PAINT_THEME,
)
from .parallel import (
    LIFXParallelRuntime,
    ParallelCommand,
    ParallelDispatchOutcome,
    ParallelDispatchResult,
    ParallelTransport,
)
from .util import convert_16_to_8, find_hsbk, lifx_features, merge_hsbk

GROUP_PLATFORMS = [Platform.BUTTON, Platform.LIGHT, Platform.NUMBER]


def _color_modes(coordinator: LIFXUpdateCoordinator) -> set[ColorMode]:
    features = lifx_features(coordinator.device)
    if features["color"]:
        return {ColorMode.COLOR_TEMP, ColorMode.HS}
    if features["min_kelvin"] != features["max_kelvin"]:
        return {ColorMode.COLOR_TEMP}
    return {ColorMode.BRIGHTNESS}


def _effects(coordinator: LIFXUpdateCoordinator) -> set[str]:
    features = lifx_features(coordinator.device)
    effects = {"effect_pulse", "effect_stop"}
    if features["color"]:
        effects.add("effect_colorloop")
    if features["multizone"]:
        effects.add("effect_move")
    if features["matrix"]:
        effects.update({"effect_flame", "effect_morph"})
    if coordinator.device.product in LIFX_CEILING_PRODUCT_IDS:
        effects.add("effect_sky")
    return effects


def _members_are_ready(
    entries: list[ConfigEntry], coordinators: list[LIFXUpdateCoordinator]
) -> bool:
    """Return whether at least one member is loaded with its original runtime."""
    return any(
        entry.state is ConfigEntryState.LOADED
        and getattr(entry, "runtime_data", None) is coordinator
        and coordinator.last_update_success
        for entry, coordinator in zip(entries, coordinators, strict=True)
    )


def _ensure_members_ready(
    entries: list[ConfigEntry], coordinators: list[LIFXUpdateCoordinator]
) -> None:
    """Raise a retryable error if a selected physical light is not ready."""
    if not _members_are_ready(entries, coordinators):
        raise ConfigEntryNotReady("A selected LIFX light is not loaded")


def _member_transport(coordinator: LIFXUpdateCoordinator) -> ParallelTransport:
    """Build the worker transport from one loaded physical coordinator."""
    target = bytes.fromhex(coordinator.device.mac_addr.replace(":", ""))
    if len(target) != 6:
        raise HomeAssistantError("A selected LIFX light has an invalid target")
    return ParallelTransport(
        coordinator.device.ip_addr,
        coordinator.device.port,
        target.ljust(8, b"\0"),
    )


def _raw_hsbk(
    color: tuple[float | int, float | int, float | int, int],
) -> tuple[int, int, int, int]:
    """Convert a theme HSBK value into the LIFX packet representation."""
    hue, saturation, brightness, kelvin = color
    return (
        round(hue / 360 * 65535) if hue <= 360 else round(hue),
        round(saturation / 100 * 65535) if saturation <= 100 else round(saturation),
        round(brightness / 100 * 65535) if brightness <= 100 else round(brightness),
        round(kelvin),
    )


@dataclass(frozen=True, slots=True)
class _MemberCommandState:
    """One member-specific target used only to build a LAN command."""

    color: tuple[int, int, int, int]
    power_level: int


@dataclass(frozen=True, slots=True)
class _OptimisticGroupState:
    """One requested state shown until physical coordinator data catches up."""

    color: tuple[int, int, int, int]
    is_on: bool


class LIFXParallelGroupRuntime:
    """Runtime state and dispatch policy for one virtual group config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        member_entries: tuple[ConfigEntry, ...],
        members: tuple[LIFXUpdateCoordinator, ...],
    ) -> None:
        """Initialize the virtual group runtime."""
        self.hass = hass
        self.entry = entry
        self.member_entries = member_entries
        self.members = list(members)
        self.group_id = entry.data[CONF_GROUP_ID]
        self.parallel = LIFXParallelRuntime(
            hass, (_member_transport(member) for member in members)
        )
        self.transition_on_duration = 0.0
        self.transition_off_duration = 0.0
        self.transition_cross_duration = 0.0
        self._command_lock = asyncio.Lock()
        self._availability_listeners: list[Callable[[], None]] = []
        self._software_effect: asyncio.Task[None] | None = None
        self._stopped = False
        self._command_generation = 0
        self._optimistic_state: _OptimisticGroupState | None = None
        self._optimistic_expiry_task: asyncio.Task[None] | None = None
        self._recovery_task: asyncio.Task[None] | None = None
        self._member_ready = [coordinator.last_update_success for coordinator in members]
        self._member_hosts = [coordinator.device.ip_addr for coordinator in members]
        self._member_binding_generation = [0] * len(members)
        self._member_reconnect_generation: list[int | None] = [None] * len(members)
        self._member_retry_tasks: list[asyncio.Task[None] | None] = [None] * len(members)
        self._member_retry_transports: list[ParallelTransport | None] = [None] * len(
            members
        )
        self._member_next_reconnect = [0.0] * len(members)
        self._member_listener_removers: list[CALLBACK_TYPE | None] = [
            None
        ] * len(members)
        self._member_entry_state_removers: list[CALLBACK_TYPE | None] = [
            None
        ] * len(members)
        self._keepalive_failures = [0] * len(members)
        self._keepalive_healthy = [True] * len(members)
        self._keepalive_generation = 0
        self._cancel_keepalive: CALLBACK_TYPE | None = None
        self._keepalive_task: asyncio.Task[None] | None = None

    @property
    def available(self) -> bool:
        """Return whether at least one physical member is available."""
        return any(
            entry.state is ConfigEntryState.LOADED
            and getattr(entry, "runtime_data", None) is member
            and member.last_update_success
            and self._member_ready[index]
            and self._keepalive_healthy[index]
            for index, (entry, member) in enumerate(
                zip(self.member_entries, self.members, strict=True)
            )
        )

    @property
    def stopped(self) -> bool:
        """Return whether the group has released its worker pool."""
        return self._stopped

    @property
    def member_states(self) -> tuple[_MemberCommandState, ...]:
        """Return physical state with each member's virtual power projection."""
        return tuple(
            _MemberCommandState(
                tuple(
                    member.resume_hsbk
                    if member.virtual_off and member.resume_hsbk is not None
                    else member.device.color
                ),
                65535 if member.device.power_level and not member.virtual_off else 0,
            )
            for member in self.members
        )

    @property
    def display_state(self) -> _OptimisticGroupState:
        """Return optimistic group state until it expires, then physical state."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        states = self.member_states
        return _OptimisticGroupState(
            tuple(
                round(fmean(state.color[index] for state in states))
                for index in range(4)
            ),
            any(state.power_level for state in states),
        )

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        """Return the modes available on every member."""
        return set.intersection(*(_color_modes(member) for member in self.members))

    @property
    def effect_list(self) -> list[str]:
        """Return the effects available on every member."""
        return sorted(set.intersection(*(_effects(member) for member in self.members)))

    def supports_effect(self, service: str) -> bool:
        """Return whether the group can safely apply an effect service."""
        return service in self.effect_list or service == SERVICE_PAINT_THEME

    async def async_start(self) -> None:
        """Warm workers before adding the group entities."""
        await self.parallel.async_start()
        self._async_arm_keepalive()

    async def async_stop(self) -> None:
        """Release all process resources."""
        if self._stopped:
            return
        self._stopped = True
        self._cancel_optimistic_expiry()
        self._async_cancel_keepalive()
        for task in self._member_retry_tasks:
            if task is not None:
                task.cancel()
        self._optimistic_state = None
        await self._async_stop_software_effect()
        self._async_remove_member_listeners()
        await self.parallel.async_stop()
        self.async_update_listeners()

    @callback
    def async_start_member_listeners(self) -> None:
        """Track physical entry replacement and coordinator updates."""
        for index, coordinator in enumerate(self.members):

            @callback
            def _async_member_state_change(index: int = index) -> None:
                self.async_member_entry_state_changed(index)

            self._member_entry_state_removers[index] = self.member_entries[
                index
            ].async_on_state_change(_async_member_state_change)
            self._async_set_member_listener(index, coordinator)

    @callback
    def _async_remove_member_listeners(self) -> None:
        """Remove physical member entry and coordinator listeners."""
        for removers in (
            self._member_listener_removers,
            self._member_entry_state_removers,
        ):
            for index, remove_listener in enumerate(removers):
                if remove_listener is not None:
                    remove_listener()
                    removers[index] = None

    @callback
    def _async_set_member_listener(
        self, index: int, coordinator: LIFXUpdateCoordinator
    ) -> None:
        """Listen for updates from one currently-bound physical coordinator."""
        if remove_listener := self._member_listener_removers[index]:
            remove_listener()

        @callback
        def _async_member_update() -> None:
            self.async_member_updated(index)

        self._member_listener_removers[index] = coordinator.async_add_listener(
            _async_member_update
        )

    @callback
    def async_member_entry_state_changed(self, index: int) -> None:
        """Swap a member binding when its physical entry finishes reloading."""
        member_entry = self.member_entries[index]
        if member_entry.state is not ConfigEntryState.LOADED:
            self._member_binding_generation[index] += 1
            self._member_ready[index] = False
            self.async_update_listeners()
            return

        coordinator = getattr(member_entry, "runtime_data", None)
        if not isinstance(coordinator, LIFXUpdateCoordinator):
            self._member_binding_generation[index] += 1
            self._member_ready[index] = False
            self.async_update_listeners()
            return

        if coordinator is not self.members[index]:
            self._member_binding_generation[index] += 1
            self.members[index] = coordinator
            self._member_ready[index] = False
            self._async_set_member_listener(index, coordinator)

        self.async_member_updated(index)

    @callback
    def async_request_recovery(
        self,
        member_entry: ConfigEntry | None = None,
        generation: int | None = None,
    ) -> None:
        """Stop and reload the group after a recoverable runtime failure."""
        if self._recovery_task is not None:
            return
        self._recovery_task = self.hass.async_create_background_task(
            self._async_recover(member_entry, generation),
            f"lifx-parallel-recover-{self.group_id}",
        )

    @callback
    def async_cancel_recovery(self) -> None:
        """Cancel a pending recovery during normal group unload."""
        if (
            task := self._recovery_task
        ) is not None and task is not asyncio.current_task():
            task.cancel()
        self._recovery_task = None

    async def _async_recover(
        self, member_entry: ConfigEntry | None, generation: int | None
    ) -> None:
        """Wait for a member unload to finish, then use config-entry retry."""
        try:
            if generation is None:
                await self.async_stop()
            else:
                async with self._command_lock:
                    if generation != self._command_generation:
                        return
                await self.async_stop()
            while (
                member_entry is not None
                and member_entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS
                and not self.hass.is_stopping
            ):
                await asyncio.sleep(0.05)
            if self.hass.is_stopping or self.entry.state is not ConfigEntryState.LOADED:
                return
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
        finally:
            self._recovery_task = None

    @callback
    def async_update_listeners(self) -> None:
        """Write virtual state after a member's coordinator changes."""
        for listener in tuple(self._availability_listeners):
            listener()

    @callback
    def async_note_user_mutation(self) -> None:
        """Reset idle health work before a visible Group command."""
        self._keepalive_generation += 1
        self._async_cancel_keepalive()
        self.hass.async_create_background_task(
            self.parallel.async_cancel_health(),
            f"lifx-parallel-cancel-health-{self.group_id}",
        )
        self._async_arm_keepalive()

    @callback
    def _async_arm_keepalive(self) -> None:
        """Schedule one idle health check for the current mutation generation."""
        if self._stopped or self._cancel_keepalive is not None:
            return
        generation = self._keepalive_generation

        @callback
        def _async_keepalive_due(_now: object) -> None:
            self._cancel_keepalive = None
            self._keepalive_task = self.hass.async_create_background_task(
                self._async_run_keepalive(generation),
                f"lifx-parallel-keepalive-{self.group_id}",
            )

        self._cancel_keepalive = async_call_later(
            self.hass,
            timedelta(seconds=DEVICE_GROUP_KEEPALIVE_INTERVAL),
            _async_keepalive_due,
        )

    @callback
    def _async_cancel_keepalive(self) -> None:
        """Invalidate scheduled or active health work."""
        if self._cancel_keepalive is not None:
            self._cancel_keepalive()
            self._cancel_keepalive = None
        if (
            task := self._keepalive_task
        ) is not None and task is not asyncio.current_task():
            task.cancel()
        self._keepalive_task = None

    async def _async_run_keepalive(self, generation: int) -> None:
        """Probe every warm member endpoint after an idle interval."""
        try:
            if (
                generation != self._keepalive_generation
                or self._stopped
                or self._software_effect is not None
                or not self.available
            ):
                return
            result = await self.parallel.async_keepalive(
                frozenset(
                    index
                    for index, (entry, member) in enumerate(
                        zip(self.member_entries, self.members, strict=True)
                    )
                    if entry.state is ConfigEntryState.LOADED
                    and getattr(entry, "runtime_data", None) is member
                    and member.last_update_success
                    and self._member_ready[index]
                    and self._keepalive_healthy[index]
                )
            )
            if generation != self._keepalive_generation:
                return
            if result.outcome is not ParallelDispatchOutcome.COMPLETED:
                _LOGGER.debug("LIFX Device Group keepalive cancelled or unavailable")
                return
            failed = result.failed_member_indexes
            changed = False
            for index, member in enumerate(self.members):
                if index not in failed:
                    if self._keepalive_failures[index] or not self._keepalive_healthy[index]:
                        changed = True
                    if not self._keepalive_healthy[index]:
                        _LOGGER.warning(
                            "LIFX Device Group member %s recovered after keepalive",
                            index,
                        )
                    self._keepalive_failures[index] = 0
                    self._keepalive_healthy[index] = True
                    continue
                self._keepalive_failures[index] += 1
                if (
                    self._keepalive_failures[index]
                    >= DEVICE_GROUP_KEEPALIVE_MAX_CONSECUTIVE_FAILURES
                    and self._keepalive_healthy[index]
                ):
                    self._keepalive_healthy[index] = False
                    changed = True
                    _LOGGER.warning(
                        "LIFX Device Group member %s became unavailable after keepalive failures",
                        index,
                    )
                else:
                    _LOGGER.debug(
                        "LIFX Device Group keepalive missed for member %s (failure %s)",
                        index,
                        self._keepalive_failures[index],
                    )
                self._async_schedule_member_reconnect(
                    index, _member_transport(member), self._member_binding_generation[index]
                )
            if changed:
                self.async_update_listeners()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("LIFX Device Group keepalive failed")
        finally:
            if generation == self._keepalive_generation and not self._stopped:
                self._keepalive_task = None
                self._async_arm_keepalive()

    @callback
    def async_member_updated(self, index: int) -> None:
        """Reflect physical availability and reconnect a recovered member worker."""
        member = self.members[index]
        if self.member_entries[index].state is not ConfigEntryState.LOADED:
            self._member_ready[index] = False
            self.async_update_listeners()
            return
        ready = member.last_update_success
        host = member.device.ip_addr
        reconnect = ready and (
            not self._member_ready[index] or host != self._member_hosts[index]
        )
        self._member_hosts[index] = host
        generation = self._member_binding_generation[index]
        if reconnect and self._member_reconnect_generation[index] != generation:
            self._member_ready[index] = False
            self._member_reconnect_generation[index] = generation
            self._async_schedule_member_reconnect(
                index, _member_transport(member), generation
            )
        elif not reconnect:
            self._member_ready[index] = ready
        self.async_update_listeners()

    async def _async_request_reconnect(
        self, index: int, transport: ParallelTransport, generation: int
    ) -> None:
        """Make a best-effort transport reconnect without changing availability."""
        if generation != self._member_binding_generation[index]:
            return
        try:
            result = await self.parallel.async_request_reconnect(index, transport)
        except HomeAssistantError:
            result = ParallelDispatchResult(ParallelDispatchOutcome.FAILED, 0)
        if result.outcome is not ParallelDispatchOutcome.COMPLETED:
            if generation == self._member_binding_generation[index]:
                self._member_reconnect_generation[index] = None
                self.async_update_listeners()
            return
        if generation != self._member_binding_generation[index]:
            return
        self._member_reconnect_generation[index] = None
        self._member_ready[index] = self.members[index].last_update_success
        self._keepalive_failures[index] = 0
        self._keepalive_healthy[index] = True
        self.async_update_listeners()

    @callback
    def _async_schedule_member_reconnect(
        self, index: int, transport: ParallelTransport, generation: int
    ) -> None:
        """Coalesce member recovery while retaining the latest coordinator transport."""
        self._member_retry_transports[index] = transport
        if self._member_retry_tasks[index] is not None:
            return
        self._member_retry_tasks[index] = self.hass.async_create_background_task(
            self._async_run_member_reconnect(index, generation),
            f"lifx-parallel-reconnect-{self.group_id}-{index}",
        )

    async def _async_run_member_reconnect(self, index: int, generation: int) -> None:
        """Reconnect one slot no more than once per configured interval."""
        try:
            delay = self._member_next_reconnect[index] - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if generation != self._member_binding_generation[index] or self._stopped:
                return
            member = self.members[index]
            if self.member_entries[index].state is not ConfigEntryState.LOADED:
                return
            if not member.last_update_success:
                return
            self._member_next_reconnect[index] = (
                time.monotonic() + DEVICE_GROUP_MEMBER_RECONNECT_INTERVAL
            )
            await self._async_request_reconnect(
                index, _member_transport(member), generation
            )
        finally:
            self._member_retry_tasks[index] = None
        if (
            generation == self._member_binding_generation[index]
            and not self._stopped
            and not self._member_ready[index]
            and self._member_retry_transports[index] is not None
        ):
            self._async_schedule_member_reconnect(
                index, self._member_retry_transports[index], generation
            )

    @callback
    def async_add_availability_listener(
        self, listener: Callable[[], None]
    ) -> Callable[[], None]:
        """Add a listener that reflects a member-unload worker shutdown."""
        self._availability_listeners.append(listener)

        @callback
        def _remove_listener() -> None:
            self._availability_listeners.remove(listener)

        return _remove_listener

    def _transition_ms(
        self,
        member: LIFXUpdateCoordinator,
        kind: Literal["on", "off", "cross"],
        kwargs: dict[str, Any],
    ) -> int:
        if ATTR_TRANSITION in kwargs:
            return round(kwargs[ATTR_TRANSITION] * 1000)
        if kind == "off":
            duration = self.transition_off_duration or member.transition_off_duration
        elif kind == "cross":
            duration = (
                self.transition_cross_duration or member.transition_cross_duration
            )
        else:
            duration = self.transition_on_duration or member.transition_on_duration
        return round(duration * 1000)

    def _begin_projection(self, states: tuple[_MemberCommandState, ...]) -> int:
        """Make a command target visible to the virtual light immediately."""
        self._command_generation += 1
        generation = self._command_generation
        self._cancel_optimistic_expiry()
        self._optimistic_state = _OptimisticGroupState(
            tuple(
                round(fmean(state.color[index] for state in states))
                for index in range(4)
            ),
            any(state.power_level for state in states),
        )
        self._optimistic_expiry_task = self.hass.async_create_background_task(
            self._async_expire_optimistic_state(generation),
            f"lifx-device-group-expire-{self.group_id}-{generation}",
        )
        self.async_update_listeners()
        return generation

    async def _async_expire_optimistic_state(self, generation: int) -> None:
        """Return to physical coordinator state after the settle window."""
        await asyncio.sleep(DEVICE_GROUP_OPTIMISTIC_STATE_EXPIRY)
        if generation == self._command_generation:
            self._optimistic_state = None
            self.async_update_listeners()

    def _cancel_optimistic_expiry(self) -> None:
        """Cancel an obsolete group-wide optimistic state expiry."""
        if (task := self._optimistic_expiry_task) is not None:
            if task is not asyncio.current_task():
                task.cancel()
            self._optimistic_expiry_task = None

    def _clear_projection(self, generation: int) -> None:
        """Drop a failed command's projection if it is still current."""
        if generation == self._command_generation:
            self._cancel_optimistic_expiry()
            self._optimistic_state = None
            self.async_update_listeners()

    async def async_set_state(self, **kwargs: Any) -> None:
        """Stage member-specific packets and release all visible work together."""
        self.async_note_user_mutation()
        if not self._can_accept_user_command("state"):
            return
        await self._async_set_state(**kwargs)

    def _can_accept_user_command(self, operation: str) -> bool:
        """Return whether an entity action may send packets without a service error."""
        if self._recovery_task is not None:
            _LOGGER.debug("LIFX Device Group %s command skipped: recovering", operation)
            return False
        if not self.available:
            _LOGGER.debug("LIFX Device Group %s command skipped: unavailable", operation)
            return False
        return True

    def _log_dispatch_outcome(
        self, operation: str, result: ParallelDispatchResult
    ) -> None:
        """Log one non-sensitive operational Group outcome."""
        if result.outcome is ParallelDispatchOutcome.SUPERSEDED:
            _LOGGER.debug("LIFX Device Group %s command superseded", operation)
        elif result.outcome is not ParallelDispatchOutcome.COMPLETED:
            _LOGGER.debug("LIFX Device Group %s command did not complete", operation)

    async def _async_dispatch_commands(
        self, operation: str, commands: tuple[ParallelCommand, ...]
    ) -> ParallelDispatchResult:
        """Return one safe result for unprojected Group work."""
        commands = self._commands_for_available_members(commands)
        try:
            result = await self.parallel.async_dispatch(commands)
        except HomeAssistantError:
            result = ParallelDispatchResult(ParallelDispatchOutcome.FAILED, 0)
        if result.outcome is ParallelDispatchOutcome.COMPLETED:
            self._reset_keepalive_health()
        else:
            self._log_dispatch_outcome(operation, result)
        return result

    def _commands_for_available_members(
        self, commands: tuple[ParallelCommand, ...]
    ) -> tuple[ParallelCommand | None, ...]:
        """Exclude unavailable members from one immutable dispatch snapshot."""
        return tuple(
            command
            if entry.state is ConfigEntryState.LOADED
            and getattr(entry, "runtime_data", None) is member
            and member.last_update_success
            and self._member_ready[index]
            and self._keepalive_healthy[index]
            else None
            for index, (entry, member, command) in enumerate(
                zip(self.member_entries, self.members, commands, strict=True)
            )
        )

    def _reset_keepalive_health(self) -> None:
        """Treat a completed normal Group request as endpoint reachability proof."""
        changed = False
        for index in range(len(self.members)):
            if self._keepalive_failures[index] or not self._keepalive_healthy[index]:
                changed = True
            self._keepalive_failures[index] = 0
            self._keepalive_healthy[index] = True
        if changed:
            _LOGGER.warning("LIFX Device Group recovered after a successful command")
            self.async_update_listeners()

    def _display_state_for_command(self, operation: str) -> _OptimisticGroupState:
        """Snapshot and log the displayed state used to build one operation."""
        display_state = self.display_state
        _LOGGER.debug(
            "LIFX Device Group %s state baseline: is_on=%s",
            self.group_id,
            operation,
            display_state.is_on,
        )
        return display_state

    async def _async_set_state(self, **kwargs: Any) -> None:
        """Dispatch a state change without delaying a newer request."""
        power = kwargs.get("power")
        hsbk = find_hsbk(self.hass, **kwargs)
        display_state = self._display_state_for_command("state command")
        transition_kind: Literal["on", "off", "cross"] = (
            "off"
            if power is False
            else "on"
            if power is True and not display_state.is_on
            else "cross"
        )
        display_power_level = 65535 if display_state.is_on else 0
        commands: list[ParallelCommand] = []
        states: list[_MemberCommandState] = []

        for member in self.members:
            color = tuple(merge_hsbk(display_state.color, hsbk)) if hsbk else None
            duration = self._transition_ms(member, transition_kind, kwargs)
            target_color = color or display_state.color
            target_power = (
                65535 if power is True else 0 if power is False else display_power_level
            )
            states.append(_MemberCommandState(target_color, target_power))
            if power is False:
                commands.append(
                    ParallelCommand(
                        "color",
                        (*target_color[:2], 0, target_color[3], duration),
                    )
                )
            elif power is True and not display_state.is_on:
                if member.device.power_level:
                    commands.append(
                        ParallelCommand("color", (*target_color, duration), pad_before=1)
                    )
                else:
                    commands.append(
                        ParallelCommand(
                            "color",
                            (*target_color, 0),
                            ParallelCommand("power", (True, duration)),
                        )
                    )
            elif color is not None:
                commands.append(ParallelCommand("color", (*color, duration)))
            elif power is not None:
                commands.append(ParallelCommand("color", (*target_color, duration)))
            else:
                raise ValueError("LIFX group action did not contain a state change")

        await self._async_dispatch_projected_states(
            tuple(commands),
            tuple(states),
            virtual_off=power is False,
        )

    async def _async_dispatch_projected_states(
        self,
        commands: tuple[ParallelCommand, ...],
        states: tuple[_MemberCommandState, ...],
        virtual_off: bool = False,
    ) -> bool:
        """Dispatch a command and keep its aggregate projection until polling catches up."""
        generation = self._begin_projection(states)
        try:
            result = await self.parallel.async_dispatch(
                self._commands_for_available_members(commands)
            )
        except HomeAssistantError:
            result = ParallelDispatchResult(ParallelDispatchOutcome.FAILED, 0)
        if result.outcome is not ParallelDispatchOutcome.COMPLETED:
            self._clear_projection(generation)
            self._log_dispatch_outcome("state", result)
            return False
        self._reset_keepalive_health()
        for member, state in zip(self.members, states, strict=True):
            if virtual_off:
                member.async_record_virtual_off(state.color)
            elif state.power_level:
                member.async_record_virtual_on(state.color)
            member.async_set_updated_data(None)
        return True

    def _with_power_stage(
        self, commands: tuple[ParallelCommand, ...], kwargs: dict[str, Any]
    ) -> tuple[ParallelCommand, ...]:
        """Precede dependent commands with an acknowledged power-on stage."""
        if not kwargs.get(ATTR_POWER_ON, True):
            return commands
        return tuple(
            ParallelCommand(
                "power",
                (True, self._transition_ms(member, "on", kwargs)),
                command,
            )
            for member, command in zip(self.members, commands, strict=True)
        )

    async def async_identify(self) -> None:
        """Start the LIFX identify waveform from every worker gate."""
        self.async_note_user_mutation()
        if not self._can_accept_user_command("identify"):
            return
        commands = tuple(
            ParallelCommand(
                "waveform_optional",
                (1, 0, 0, 1, 3500, 1000, 3.0, 0, 1, 1, 1, 1, 1),
            )
            for _member in self.members
        )
        await self._async_dispatch_commands("identify", commands)

    async def async_restart(self) -> None:
        """Reboot every member from the shared dispatch gate."""
        self.async_note_user_mutation()
        if not self._can_accept_user_command("restart"):
            return
        await self._async_dispatch_commands(
            "restart",
            tuple(ParallelCommand("reboot", ()) for _member in self.members)
        )

    async def async_start_effect(self, service: str, **kwargs: Any) -> None:
        """Apply an effect without falling back to member aiolifx connections."""
        self.async_note_user_mutation()
        if not self._can_accept_user_command("effect"):
            return
        async with self._command_lock:
            await self._async_stop_software_effect()
            if service == SERVICE_EFFECT_PULSE:
                self._software_effect = self.hass.async_create_background_task(
                    self._async_pulse(**kwargs),
                    f"lifx-parallel-pulse-{self.group_id}",
                )
                return
            if service == SERVICE_EFFECT_COLORLOOP:
                self._software_effect = self.hass.async_create_background_task(
                    self._async_colorloop(**kwargs),
                    f"lifx-parallel-colorloop-{self.group_id}",
                )
                return
        if service == SERVICE_EFFECT_STOP:
            await self._async_stop_firmware_effects()
            return
        if service == SERVICE_PAINT_THEME:
            await self._async_paint_theme(**kwargs)
            return
        if service == SERVICE_EFFECT_MOVE:
            await self._async_dispatch_commands(
                "effect",
                self._with_power_stage(
                    tuple(
                        ParallelCommand(
                            "multizone_effect",
                            (
                                1,
                                round(
                                    kwargs.get(ATTR_SPEED, EFFECT_MOVE_DEFAULT_SPEED)
                                    * 1000
                                ),
                                1
                                if kwargs.get(
                                    ATTR_DIRECTION, EFFECT_MOVE_DEFAULT_DIRECTION
                                )
                                == "right"
                                else 0,
                            ),
                        )
                        for _member in self.members
                    ),
                    kwargs,
                )
            )
            return
        effect, speed, sky_type, saturation_min, saturation_max = {
            SERVICE_EFFECT_FLAME: (3, EFFECT_FLAME_DEFAULT_SPEED, 0, 0, 0),
            SERVICE_EFFECT_MORPH: (2, EFFECT_MORPH_DEFAULT_SPEED, 0, 0, 0),
            SERVICE_EFFECT_SKY: (
                5,
                EFFECT_SKY_DEFAULT_SPEED,
                {"Sunrise": 0, "Sunset": 1, "Clouds": 2}[
                    kwargs.get(ATTR_SKY_TYPE, EFFECT_SKY_DEFAULT_SKY_TYPE)
                ],
                kwargs.get(
                    "cloud_saturation_min",
                    EFFECT_SKY_DEFAULT_CLOUD_SATURATION_MIN,
                ),
                kwargs.get(
                    "cloud_saturation_max",
                    EFFECT_SKY_DEFAULT_CLOUD_SATURATION_MAX,
                ),
            ),
        }[service]
        palette = self._theme_colors(**kwargs)
        await self._async_dispatch_commands(
            "effect",
            self._with_power_stage(
                tuple(
                    ParallelCommand(
                        "tile_effect",
                        (
                            effect,
                            round(kwargs.get(ATTR_SPEED, speed) * 1000),
                            sky_type,
                            saturation_min,
                            saturation_max,
                            palette,
                        ),
                    )
                    for _member in self.members
                ),
                kwargs,
            )
        )

    async def _async_stop_software_effect(self) -> None:
        """Cancel the prior software effect before changing group state."""
        if self._software_effect is None:
            return
        self._software_effect.cancel()
        with suppress(asyncio.CancelledError):
            await self._software_effect
        self._software_effect = None

    async def _async_stop_firmware_effects(self) -> None:
        """Stop any firmware effect active from this group."""
        display_state = self._display_state_for_command("stop effect")
        commands = []
        for member in self.members:
            features = lifx_features(member.device)
            if features["matrix"]:
                commands.append(ParallelCommand("tile_effect", (0, 0, 0, 0, 0, ())))
            elif features["multizone"]:
                commands.append(ParallelCommand("multizone_effect", (0, 0, 0)))
            else:
                commands.append(ParallelCommand("color", (*display_state.color, 0)))
        await self._async_dispatch_commands("stop effect", tuple(commands))

    def _theme_colors(self, **kwargs: Any) -> tuple[tuple[int, int, int, int], ...]:
        """Resolve a service palette to direct-LAN HSBK values."""
        palette = kwargs.get(ATTR_PALETTE)
        if palette is None:
            theme: Theme = ThemeLibrary().get_theme(kwargs.get(ATTR_THEME, "exciting"))
            palette = theme.colors
        return tuple(_raw_hsbk(color) for color in palette)

    async def _async_paint_theme(self, **kwargs: Any) -> None:
        """Paint one theme color per member at one common deadline."""
        color = self._theme_colors(**kwargs)[0]
        display_state = self._display_state_for_command("paint theme")
        duration = round(
            kwargs.get(ATTR_TRANSITION, PAINT_THEME_DEFAULT_TRANSITION) * 1000
        )
        states = tuple(
            _MemberCommandState(
                color,
                65535 if kwargs.get(ATTR_POWER_ON, True) else 65535
                if display_state.is_on
                else 0,
            )
            for _member in self.members
        )
        commands = tuple(
            ParallelCommand("color", (*state.color, duration)) for state in states
        )
        commands = self._with_power_stage(commands, kwargs)
        await self._async_dispatch_projected_states(
            commands,
            states,
        )

    async def _async_pulse(self, **kwargs: Any) -> None:
        """Run pulse ticks through the same warmed worker dispatcher."""
        hsbk = find_hsbk(self.hass, **kwargs)
        period = kwargs.get(ATTR_PERIOD, 1.0)
        cycles = kwargs.get(ATTR_CYCLES, 1)
        for _cycle in range(round(cycles)):
            if hsbk is None:
                await self.async_set_state(power=True, brightness=255)
            else:
                await self.async_set_state(power=True, **kwargs)
            await asyncio.sleep(period / 2)
            await self.async_set_state(
                brightness=0,
                transition=period / 2,
            )
            await asyncio.sleep(period / 2)

    async def _async_colorloop(self, **kwargs: Any) -> None:
        """Run colorloop ticks through the same warmed worker dispatcher."""
        period = kwargs.get(ATTR_PERIOD, 60)
        change = kwargs.get(ATTR_CHANGE, 20)
        transition = kwargs.get(ATTR_TRANSITION, min(period, 1))
        saturation_min = kwargs.get(ATTR_SATURATION_MIN, 80)
        saturation_max = kwargs.get(ATTR_SATURATION_MAX, 100)
        brightness = kwargs.get("brightness")
        display_state = self._display_state_for_command("colorloop")
        tick = 0
        while True:
            commands = []
            for member in self.members:
                hue = (
                    display_state.color[0] / 65535 * 360
                    + tick * change
                ) % 360
                saturation = saturation_min if tick % 2 else saturation_max
                level = (
                    display_state.color[2] if brightness is None else brightness * 257
                )
                command = ParallelCommand(
                    "color",
                    (
                        round(hue / 360 * 65535),
                        round(saturation / 100 * 65535),
                        level,
                        display_state.color[3],
                        round(transition * 1000),
                    ),
                )
                if kwargs.get(ATTR_POWER_ON, True) and tick == 0:
                    command = ParallelCommand(
                        "power",
                        (True, self._transition_ms(member, "on", kwargs)),
                        command,
                    )
                commands.append(command)
            await self._async_dispatch_commands("colorloop", tuple(commands))
            tick += 1
            await asyncio.sleep(period)


class LIFXParallelGroupEntity(Entity):
    """Common virtual device metadata."""

    _attr_has_entity_name = True

    def __init__(self, runtime: LIFXParallelGroupRuntime) -> None:
        """Initialize virtual device metadata."""
        self.runtime = runtime
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"parallel-group-{runtime.group_id}")},
            manufacturer="LIFX",
            name=runtime.entry.title,
            model="Device Group",
        )

    @property
    @override
    def available(self) -> bool:
        """Return whether the member workers and lights are available."""
        return self.runtime.available

    @override
    async def async_added_to_hass(self) -> None:
        """Keep every virtual group entity in sync with runtime availability."""
        self.async_on_remove(
            self.runtime.async_add_availability_listener(self.async_write_ha_state)
        )
        await super().async_added_to_hass()


class LIFXParallelGroupLight(LIFXParallelGroupEntity, LightEntity):
    """The virtual LIFX light backed by the parallel worker pool."""

    _attr_name = None
    _attr_should_poll = False

    def __init__(self, runtime: LIFXParallelGroupRuntime) -> None:
        """Initialize the virtual light."""
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.group_id}_light"
        self._attr_supported_color_modes = runtime.supported_color_modes
        self._attr_effect_list = runtime.effect_list
        self._attr_supported_features = LightEntityFeature.TRANSITION
        if self._attr_effect_list:
            self._attr_supported_features |= LightEntityFeature.EFFECT

    @property
    @override
    def is_on(self) -> bool:
        return self.runtime.display_state.is_on

    @property
    @override
    def brightness(self) -> int | None:
        state = self.runtime.display_state
        return convert_16_to_8(state.color[2]) if state.is_on else None

    @property
    @override
    def color_mode(self) -> ColorMode:
        if (
            ColorMode.HS in self._attr_supported_color_modes
            and self.runtime.display_state.color[1]
        ):
            return ColorMode.HS
        if ColorMode.COLOR_TEMP in self._attr_supported_color_modes:
            return ColorMode.COLOR_TEMP
        return ColorMode.BRIGHTNESS

    @property
    @override
    def hs_color(self) -> tuple[float, float] | None:
        if self.color_mode is not ColorMode.HS:
            return None
        color = self.runtime.display_state.color
        return (color[0] / 65535 * 360, color[1] / 65535 * 100)

    @property
    @override
    def color_temp_kelvin(self) -> int | None:
        if self.color_mode is not ColorMode.COLOR_TEMP:
            return None
        return self.runtime.display_state.color[3]

    @property
    @override
    def min_color_temp_kelvin(self) -> int | None:
        if ColorMode.COLOR_TEMP not in self._attr_supported_color_modes:
            return None
        return max(
            lifx_features(member.device)["min_kelvin"]
            for member in self.runtime.members
        )

    @property
    @override
    def max_color_temp_kelvin(self) -> int | None:
        if ColorMode.COLOR_TEMP not in self._attr_supported_color_modes:
            return None
        return min(
            lifx_features(member.device)["max_kelvin"]
            for member in self.runtime.members
        )

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        if effect := kwargs.pop(ATTR_EFFECT, None):
            await self.hass.services.async_call(
                DOMAIN, effect, {"entity_id": self.entity_id}, blocking=True
            )
            return
        await self.runtime.async_set_state(power=True, **kwargs)
        self.async_write_ha_state()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.async_set_state(power=False, **kwargs)
        self.async_write_ha_state()

    async def set_state(self, **kwargs: Any) -> None:
        """Handle the integration's common set_state entity service."""
        await self.runtime.async_set_state(**kwargs)
        self.async_write_ha_state()

    @override
    async def async_added_to_hass(self) -> None:
        manager = self.hass.data[DATA_LIFX_MANAGER]
        self.async_on_remove(
            manager.async_register_entity(self.entity_id, self.runtime)
        )
        await super().async_added_to_hass()


class LIFXParallelGroupTransitionNumber(LIFXParallelGroupEntity, RestoreNumber):
    """Persistent fade override for a virtual group."""

    _attr_should_poll = False

    def __init__(
        self, runtime: LIFXParallelGroupRuntime, description: NumberEntityDescription
    ) -> None:
        """Initialize a virtual fade override number."""
        super().__init__(runtime)
        self.entity_description = description
        self._attr_unique_id = f"{runtime.group_id}_{description.key}"
        self._attr_native_value = 0.0

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_data := await self.async_get_last_number_data()) is not None:
            self._attr_native_value = last_data.native_value or 0.0
        self._set_runtime_value(self._attr_native_value)

    @override
    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self._set_runtime_value(value)
        self.async_write_ha_state()

    def _set_runtime_value(self, value: float) -> None:
        if self.entity_description.key == TRANSITION_ON_DURATION:
            self.runtime.transition_on_duration = value
        elif self.entity_description.key == TRANSITION_OFF_DURATION:
            self.runtime.transition_off_duration = value
        else:
            self.runtime.transition_cross_duration = value


class LIFXParallelGroupButton(LIFXParallelGroupEntity, ButtonEntity):
    """A parallel button action."""

    _attr_should_poll = False

    def __init__(self, runtime: LIFXParallelGroupRuntime, key: str) -> None:
        """Initialize a virtual group action button."""
        super().__init__(runtime)
        self.key = key
        self._attr_unique_id = f"{runtime.group_id}_{key}"
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_device_class = (
            ButtonDeviceClass.IDENTIFY
            if key == "identify"
            else ButtonDeviceClass.RESTART
        )

    @override
    async def async_press(self) -> None:
        if self.key == "identify":
            await self.runtime.async_identify()
        else:
            await self.runtime.async_restart()


async def async_setup_parallel_group_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Set up a virtual group after all of its physical entries are ready."""
    members: list[LIFXUpdateCoordinator] = []
    member_entries: list[ConfigEntry] = []
    for entry_id in entry.data[CONF_MEMBERS]:
        member_entry = hass.config_entries.async_get_entry(entry_id)
        coordinator = getattr(member_entry, "runtime_data", None)
        if (
            member_entry is None
            or member_entry.state is not ConfigEntryState.LOADED
            or not isinstance(coordinator, LIFXUpdateCoordinator)
        ):
            raise ConfigEntryNotReady("A selected LIFX light is not loaded")
        members.append(coordinator)
        member_entries.append(member_entry)
    _ensure_members_ready(member_entries, members)
    runtime = LIFXParallelGroupRuntime(
        hass, entry, tuple(member_entries), tuple(members)
    )
    try:
        await runtime.async_start()
        _ensure_members_ready(member_entries, members)
        entry.async_on_unload(runtime.async_cancel_recovery)
        runtime.async_start_member_listeners()
        entry.runtime_data = runtime
        await hass.config_entries.async_forward_entry_setups(entry, GROUP_PLATFORMS)
    except HomeAssistantError as err:
        await runtime.async_stop()
        raise ConfigEntryNotReady(str(err)) from err
    except Exception:
        await runtime.async_stop()
        raise
    return True


async def async_unload_parallel_group_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Unload entities and release the virtual group's worker pool."""
    if unload_ok := await hass.config_entries.async_unload_platforms(
        entry, GROUP_PLATFORMS
    ):
        await entry.runtime_data.async_stop()
    return unload_ok


def async_add_parallel_group_entities(
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    platform: Platform,
) -> None:
    """Add the group entities for one supported platform."""
    runtime = entry.runtime_data
    if platform is Platform.LIGHT:
        async_add_entities([LIFXParallelGroupLight(runtime)])
    elif platform is Platform.BUTTON:
        async_add_entities(
            [
                LIFXParallelGroupButton(runtime, "restart"),
                LIFXParallelGroupButton(runtime, "identify"),
            ]
        )
    else:
        descriptions = (
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
        async_add_entities(
            LIFXParallelGroupTransitionNumber(runtime, description)
            for description in descriptions
        )
