"""Durable, network-independent virtual-power recovery images and intents."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import math
from typing import TYPE_CHECKING, Any, Literal

from lifx import HSBK, MatrixLightState, MultiZoneLightState

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .util import HSBKChanges, replace_hsbk

if TYPE_CHECKING:
    from .coordinator import LIFXState


@dataclass(frozen=True, slots=True)
class VirtualPowerTile:
    """One completely addressed tile, including Ceiling component pixels."""

    index: int
    width: int
    height: int
    frame_buffers: int
    colors: tuple[HSBK, ...]


@dataclass(frozen=True, slots=True)
class VirtualPowerImage:
    """A detached recovery image; an overall-only image is a legacy baseline."""

    color: HSBK
    zones: tuple[HSBK, ...] = ()
    tiles: tuple[VirtualPowerTile, ...] = ()


@dataclass(frozen=True, slots=True)
class VirtualPowerToken:
    """The identity of one serial's current user request."""

    serial: str
    revision: int


@dataclass(frozen=True, slots=True)
class VirtualPowerRequest:
    """Caller-supplied intent, without a caller-assigned revision."""

    kind: Literal["virtual_off", "actual_off", "on", "prepare", "temporary"]
    before_image: VirtualPowerImage | None
    target_image: VirtualPowerImage | None
    before_power_on: bool
    before_virtual_off: bool
    before_resume_required: bool
    duration: float
    recovery_image: VirtualPowerImage | None = None


@dataclass(frozen=True, slots=True)
class VirtualPowerIntent:
    """Persisted candidate, with a restart-safe transition bound."""

    request: VirtualPowerRequest
    token: VirtualPowerToken
    delivery: Literal["prepared", "uncertain"] = "prepared"
    settle_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class VirtualPowerRecord:
    """Confirmed classification is separate from pending recovery protection."""

    virtual_off: bool = False
    resume_required: bool = False
    image: VirtualPowerImage | None = None
    revision: int = 0
    pending: VirtualPowerIntent | None = None
    transition_until: datetime | None = None


def _copy_color(color: HSBK) -> HSBK:
    return _load_color(color.as_dict)


def _load_color(data: Mapping[str, Any]) -> HSBK:
    values = {key: data[key] for key in ("hue", "saturation", "brightness", "kelvin")}
    if any(
        type(value) not in (int, float) or not math.isfinite(value)
        for value in values.values()
    ):
        raise ValueError("Non-numeric HSBK component")
    return HSBK(**values)


def capture_image(state: LIFXState) -> VirtualPowerImage:
    """Copy complete public state, refusing incomplete address coverage."""
    color = _copy_color(state.color)
    if isinstance(state, MultiZoneLightState):
        if state.zone_count <= 0 or len(state.zones) != state.zone_count:
            raise ValueError("Incomplete LIFX zone coverage")
        return VirtualPowerImage(
            color, tuple(_copy_color(zone) for zone in state.zones)
        )
    if isinstance(state, MatrixLightState):
        if (
            not state.chain
            or len(state.chain) != state.tile_count
            or len(state.tile_colors) != sum(tile.total_zones for tile in state.chain)
        ):
            raise ValueError("Incomplete LIFX tile coverage")
        tiles = []
        offset = 0
        for tile in state.chain:
            tiles.append(
                VirtualPowerTile(
                    tile.tile_index,
                    tile.width,
                    tile.height,
                    tile.supported_frame_buffers,
                    tuple(
                        _copy_color(pixel)
                        for pixel in state.tile_colors[
                            offset : offset + tile.total_zones
                        ]
                    ),
                )
            )
            offset += tile.total_zones
        image = VirtualPowerImage(color, tiles=tuple(tiles))
        _validate_image(image)
        return image
    return VirtualPowerImage(color)


def merge_image(
    image: VirtualPowerImage,
    changes: HSBKChanges,
    *,
    zones: tuple[int, ...] | None = None,
) -> VirtualPowerImage:
    """Merge explicit components while retaining all untargeted pixels."""
    return VirtualPowerImage(
        replace_hsbk(image.color, changes) if zones is None else image.color,
        tuple(
            replace_hsbk(color, changes) if zones is None or index in zones else color
            for index, color in enumerate(image.zones)
        ),
        tuple(
            replace(
                tile,
                colors=tuple(
                    replace_hsbk(color, changes)
                    if zones is None or offset + index in zones
                    else color
                    for index, color in enumerate(tile.colors)
                ),
            )
            for offset, tile in _tile_offsets(image)
        ),
    )


def _tile_offsets(image: VirtualPowerImage) -> list[tuple[int, VirtualPowerTile]]:
    offset = 0
    result = []
    for tile in image.tiles:
        result.append((offset, tile))
        offset += len(tile.colors)
    return result


def darken_image(image: VirtualPowerImage) -> VirtualPowerImage:
    """Zero brightness only, preserving every other saved component."""
    return merge_image(
        image, HSBKChanges(hue=None, saturation=None, brightness=0.0, kelvin=None)
    )


def image_matches_layout(image: VirtualPowerImage, state: LIFXState) -> bool:
    """Require complete matching addressing before reusing a saved pattern."""
    try:
        observed = capture_image(state)
        _validate_image(image)
    except KeyError, TypeError, ValueError:
        return False
    return len(image.zones) == len(observed.zones) and tuple(
        (tile.index, tile.width, tile.height, tile.frame_buffers, len(tile.colors))
        for tile in image.tiles
    ) == tuple(
        (tile.index, tile.width, tile.height, tile.frame_buffers, len(tile.colors))
        for tile in observed.tiles
    )


def resolve_recovery_image(
    record: VirtualPowerRecord,
    state: LIFXState,
    changes: HSBKChanges,
    *,
    zones: tuple[int, ...] | None = None,
) -> VirtualPowerImage:
    """Resolve explicit, compatible saved, legacy, then observed recovery data."""
    observed = capture_image(state)
    derived = record.virtual_off or record.resume_required or record.pending is not None
    image = record.image if derived else None
    if image is not None and image_matches_layout(image, state):
        base = image
    elif image is not None and not image.zones and not image.tiles:
        base = merge_image(
            observed,
            HSBKChanges(
                hue=image.color.hue,
                saturation=image.color.saturation,
                brightness=image.color.brightness,
                kelvin=image.color.kelvin,
            ),
        )
    else:
        base = observed
        colors = _image_colors(base)
        if (
            derived
            and not any(color.brightness > 0 for color in colors)
            and changes["brightness"] is None
        ):
            base = merge_image(
                base,
                HSBKChanges(hue=None, saturation=None, brightness=1.0, kelvin=None),
            )
    return merge_image(base, changes, zones=zones)


def _image_colors(image: VirtualPowerImage) -> tuple[HSBK, ...]:
    return (
        image.zones
        or tuple(color for tile in image.tiles for color in tile.colors)
        or (image.color,)
    )


def _validate_image(image: VirtualPowerImage) -> None:
    _copy_color(image.color)
    if image.zones and image.tiles:
        raise ValueError("Mixed image layouts")
    indexes = set()
    for tile in image.tiles:
        if (
            any(
                type(value) is not int
                for value in (tile.index, tile.width, tile.height, tile.frame_buffers)
            )
            or tile.index < 0
            or tile.index in indexes
            or tile.width <= 0
            or tile.height <= 0
            or tile.frame_buffers < 0
            or len(tile.colors) != tile.width * tile.height
        ):
            raise ValueError("Invalid tile layout")
        indexes.add(tile.index)
    for color in _image_colors(image):
        _copy_color(color)


def _dump_image(image: VirtualPowerImage | None) -> dict[str, Any] | None:
    if image is None:
        return None
    return {
        "color": image.color.as_dict,
        "zones": [color.as_dict for color in image.zones],
        "tiles": [
            {
                "index": tile.index,
                "width": tile.width,
                "height": tile.height,
                "frame_buffers": tile.frame_buffers,
                "colors": [color.as_dict for color in tile.colors],
            }
            for tile in image.tiles
        ],
    }


def _load_image(data: dict[str, Any] | None) -> VirtualPowerImage | None:
    if data is None:
        return None
    image = VirtualPowerImage(
        _load_color(data["color"]),
        tuple(_load_color(color) for color in data.get("zones", [])),
        tuple(
            VirtualPowerTile(
                tile["index"],
                tile["width"],
                tile["height"],
                tile["frame_buffers"],
                tuple(_load_color(color) for color in tile["colors"]),
            )
            for tile in data.get("tiles", [])
        ),
    )
    _validate_image(image)
    return image


class LIFXVirtualPowerStore:
    """Integration-owned recovery authority; no lock is held across network I/O."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize one serial-keyed authority and serialized storage boundary."""
        self._hass = hass
        self._store = Store[dict[str, object]](hass, 1, "lifx.virtual_power")
        self._write_lock = asyncio.Lock()
        self._records: dict[str, VirtualPowerRecord] = {}
        self._loaded = False
        self._active_revisions: dict[str, int] = {}

    async def async_load(self) -> None:
        """Load once, isolating malformed records from valid neighbors."""
        async with self._write_lock:
            if self._loaded:
                return
            data = await self._store.async_load()
            if isinstance(data, dict) and isinstance(
                records := data.get("records"), dict
            ):
                for serial, raw in records.items():
                    try:
                        self._records[serial] = self._load_record(serial, raw)
                    except KeyError, TypeError, ValueError, AttributeError:
                        continue
            self._loaded = True

    @callback
    def async_get_record(self, serial: str) -> VirtualPowerRecord:
        """Return the current immutable record without creating legacy authority."""
        return self._records.get(serial, VirtualPowerRecord())

    @callback
    def async_is_current(self, token: VirtualPowerToken) -> bool:
        """Check ownership after every suspension in the caller's command path."""
        return self.async_get_record(token.serial).revision == token.revision

    @callback
    def async_reserve(self, serial: str) -> VirtualPowerToken:
        """Order a user request before awaiting effect cleanup or a device read."""
        record = self.async_get_record(serial)
        token = VirtualPowerToken(serial, record.revision + 1)
        pending = (
            replace(record.pending, token=token, delivery="uncertain")
            if record.pending
            else None
        )
        self._records[serial] = replace(
            record,
            revision=token.revision,
            pending=pending,
            resume_required=record.resume_required
            or bool(record.pending and record.pending.request.kind == "virtual_off"),
        )
        self._active_revisions[serial] = token.revision
        return token

    @callback
    def async_release(self, token: VirtualPowerToken) -> None:
        """Release an unfinished caller without changing a newer request's state."""
        if self._active_revisions.get(token.serial) == token.revision:
            self._active_revisions.pop(token.serial, None)

    async def async_begin_many(
        self,
        requests: Mapping[str, VirtualPowerRequest],
        *,
        expected: Mapping[str, VirtualPowerToken] | None = None,
    ) -> dict[str, VirtualPowerToken]:
        """Reserve all revisions before suspension and await one durable barrier."""
        tokens = {}
        previous = {}
        for serial, request in requests.items():
            if expected is not None and (
                serial not in expected or not self.async_is_current(expected[serial])
            ):
                continue
            record = self.async_get_record(serial)
            previous[serial] = record
            token = (
                expected[serial]
                if expected is not None
                else VirtualPowerToken(serial, record.revision + 1)
            )
            intent = VirtualPowerIntent(
                request,
                token,
                settle_until=dt_util.utcnow() + timedelta(seconds=request.duration),
            )
            image = (
                record.image
                if record.image is not None
                and (record.resume_required or record.pending is not None)
                else request.before_image
            )
            self._records[serial] = replace(
                record,
                image=request.recovery_image or image or record.image,
                resume_required=record.resume_required
                or (
                    record.pending is not None
                    and record.pending.request.kind == "virtual_off"
                ),
                revision=token.revision,
                pending=intent,
            )
            self._active_revisions[serial] = token.revision
            tokens[serial] = token
        if not tokens:
            return {}
        try:
            await self._async_save()
        except HomeAssistantError:
            for serial, token in tokens.items():
                if self.async_is_current(token):
                    before = previous[serial]
                    revision = token.revision + 1
                    pending = (
                        replace(
                            before.pending, token=VirtualPowerToken(serial, revision)
                        )
                        if before.pending
                        else None
                    )
                    self._records[serial] = replace(
                        before, revision=revision, pending=pending
                    )
                    self._active_revisions.pop(serial, None)
            raise
        return {
            serial: token
            for serial, token in tokens.items()
            if self.async_is_current(token)
        }

    async def async_update_prepared(
        self,
        token: VirtualPowerToken,
        before: VirtualPowerImage,
        target: VirtualPowerImage,
    ) -> bool:
        """Persist the same freshly merged image that the adapter will write."""
        record = self.async_get_record(token.serial)
        if not self.async_is_current(token) or record.pending is None:
            return False
        request = replace(
            record.pending.request, before_image=before, target_image=target
        )
        self._records[token.serial] = replace(
            record,
            image=record.image
            if record.resume_required or record.virtual_off
            else before,
            pending=replace(record.pending, request=request),
        )
        await self._async_save()
        return self.async_is_current(token)

    async def async_finish(
        self,
        token: VirtualPowerToken,
        outcome: Literal["completed", "uncertain", "not_sent"],
    ) -> bool:
        """Commit only the current operation; retain uncertain candidates."""
        record = self.async_get_record(token.serial)
        if not self.async_is_current(token) or record.pending is None:
            return False
        intent = record.pending
        request = intent.request
        self._active_revisions.pop(token.serial, None)
        if outcome == "uncertain":
            updated = replace(record, pending=replace(intent, delivery="uncertain"))
        elif outcome == "not_sent":
            updated = replace(
                record,
                virtual_off=request.before_virtual_off,
                resume_required=record.resume_required
                or request.before_resume_required,
                pending=None,
            )
        elif request.kind == "virtual_off":
            updated = replace(
                record,
                virtual_off=request.before_power_on,
                resume_required=True,
                pending=None,
            )
        elif request.kind == "actual_off":
            updated = replace(
                record,
                virtual_off=False,
                resume_required=record.resume_required
                or request.before_virtual_off
                or request.before_resume_required
                or request.recovery_image is not None,
                pending=None,
            )
        elif request.kind == "prepare":
            updated = replace(
                record,
                image=request.target_image or record.image,
                resume_required=True,
                pending=None,
            )
        elif request.kind == "temporary":
            updated = replace(
                record, virtual_off=False, resume_required=True, pending=None
            )
        else:
            updated = replace(
                record,
                virtual_off=False,
                resume_required=False,
                image=request.target_image or record.image,
                pending=None,
            )
        self._records[token.serial] = replace(
            updated,
            transition_until=dt_util.utcnow()
            + timedelta(seconds=request.duration + 0.3)
            if outcome == "completed" and request.kind != "prepare"
            else None,
        )
        await self._async_save()
        return self.async_is_current(token)

    async def async_reconcile(
        self, serial: str, state: LIFXState, *, now: datetime
    ) -> None:
        """Resolve only complete settled observations, preserving partial candidates."""
        record = self.async_get_record(serial)
        if self._active_revisions.get(serial) == record.revision or (
            record.transition_until is not None and now < record.transition_until
        ):
            return
        try:
            observed = capture_image(state)
        except ValueError:
            return
        visible = any(color.brightness > 0 for color in _image_colors(observed))
        if intent := record.pending:
            if intent.settle_until is not None and now < intent.settle_until:
                return
            request = intent.request
            if state.power == 0:
                self._records[serial] = replace(
                    record,
                    virtual_off=False,
                    resume_required=record.resume_required
                    or request.kind == "virtual_off"
                    or request.before_resume_required
                    or request.before_virtual_off,
                    pending=None,
                )
                await self._async_save()
            elif request.kind == "virtual_off" and not visible:
                await self.async_finish(intent.token, "completed")
            elif (
                observed == request.before_image
                and bool(state.power) == request.before_power_on
            ):
                await self.async_finish(intent.token, "not_sent")
            elif request.kind in ("on", "prepare") and observed == request.target_image:
                await self.async_finish(intent.token, "completed")
            else:
                if visible:
                    self._records[serial] = replace(record, virtual_off=False)
                await self.async_finish(intent.token, "uncertain")
            return
        if state.power == 0:
            updated = replace(
                record,
                virtual_off=False,
                resume_required=record.resume_required or record.virtual_off,
                image=record.image
                if record.resume_required or record.virtual_off
                else observed,
                transition_until=None,
            )
        elif visible:
            updated = replace(
                record,
                virtual_off=False,
                resume_required=False,
                image=observed,
                transition_until=None,
            )
        else:
            return
        if updated != record:
            self._records[serial] = updated
            await self._async_save()

    async def async_import_legacy(
        self, serial: str, extra: Mapping[str, object]
    ) -> bool:
        """Import a legacy overall HSBK once, even when its light is disabled."""
        if serial in self._records:
            return False
        try:
            saved_color = extra.get("resume_hsbk")
            color = _load_color(saved_color) if isinstance(saved_color, dict) else None
        except KeyError, TypeError, ValueError:
            color = None
        virtual_off = extra.get("virtual_off") is True
        self._records[serial] = VirtualPowerRecord(
            virtual_off, virtual_off, VirtualPowerImage(color) if color else None
        )
        await self._async_save()
        return True

    @callback
    def async_set_compatibility(
        self,
        serial: str,
        *,
        virtual_off: bool,
        image: VirtualPowerImage | None = None,
        resume_required: bool | None = None,
    ) -> None:
        """Bridge existing successful command callbacks during staged migration."""
        record = self.async_get_record(serial)
        self._records[serial] = replace(
            record,
            virtual_off=virtual_off,
            image=image or record.image,
            resume_required=record.resume_required
            if resume_required is None
            else resume_required,
            revision=record.revision + 1,
            pending=None,
        )
        self._hass.async_create_task(
            self._async_save(), "lifx-virtual-power-compatibility-save"
        )

    async def _async_save(self) -> None:
        async with self._write_lock:
            payload = self._serialize_records()
            try:
                await self._store.async_save(payload)
                # Store logs some disk failures without raising; verify the barrier.
                persisted = await Store[dict[str, object]](
                    self._hass, 1, "lifx.virtual_power"
                ).async_load()
            except (OSError, HomeAssistantError) as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="recovery_save_failed"
                ) from err
            if persisted != payload:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="recovery_save_failed"
                )

    @callback
    def _serialize_records(self) -> dict[str, object]:
        records = {}
        for serial, record in self._records.items():
            pending = None
            if intent := record.pending:
                request = intent.request
                pending = {
                    "request": {
                        "kind": request.kind,
                        "before_image": _dump_image(request.before_image),
                        "target_image": _dump_image(request.target_image),
                        "before_power_on": request.before_power_on,
                        "before_virtual_off": request.before_virtual_off,
                        "before_resume_required": request.before_resume_required,
                        "duration": request.duration,
                        "recovery_image": _dump_image(request.recovery_image),
                    },
                    "revision": intent.token.revision,
                    "delivery": intent.delivery,
                    "settle_until": intent.settle_until.isoformat()
                    if intent.settle_until
                    else None,
                }
            records[serial] = {
                "virtual_off": record.virtual_off,
                "resume_required": record.resume_required,
                "image": _dump_image(record.image),
                "revision": record.revision,
                "pending": pending,
                "transition_until": record.transition_until.isoformat()
                if record.transition_until
                else None,
            }
        return {"records": records}

    @staticmethod
    def _load_record(serial: str, raw: dict[str, Any]) -> VirtualPowerRecord:
        revision = raw["revision"]
        if (
            type(revision) is not int
            or revision < 0
            or type(raw["virtual_off"]) is not bool
            or type(raw["resume_required"]) is not bool
        ):
            raise ValueError("Invalid virtual-power record")
        try:
            image = _load_image(raw["image"])
        except KeyError, TypeError, ValueError:
            image = None
        pending = None
        if data := raw.get("pending"):
            request = data["request"]
            if (
                request["kind"]
                not in ("virtual_off", "actual_off", "on", "prepare", "temporary")
                or data["delivery"] not in ("prepared", "uncertain")
                or data["revision"] != revision
                or any(
                    type(request[key]) is not bool
                    for key in (
                        "before_power_on",
                        "before_virtual_off",
                        "before_resume_required",
                    )
                )
                or type(request["duration"]) not in (int, float)
                or not math.isfinite(request["duration"])
                or request["duration"] < 0
            ):
                raise ValueError("Invalid virtual-power intent")
            bound = (
                dt_util.parse_datetime(data["settle_until"])
                if data.get("settle_until")
                else None
            )
            if bound is not None and bound.tzinfo is None:
                raise ValueError("Non-wall-clock settle bound")
            pending = VirtualPowerIntent(
                VirtualPowerRequest(
                    **{
                        **request,
                        "before_image": _load_image(request["before_image"]),
                        "target_image": _load_image(request["target_image"]),
                        "recovery_image": _load_image(request.get("recovery_image")),
                    }
                ),
                VirtualPowerToken(serial, revision),
                data["delivery"],
                bound,
            )
        transition_until = (
            dt_util.parse_datetime(raw["transition_until"])
            if raw.get("transition_until")
            else None
        )
        if transition_until is not None and transition_until.tzinfo is None:
            raise ValueError("Invalid transition bound")
        return VirtualPowerRecord(
            raw["virtual_off"],
            raw["resume_required"],
            image,
            revision,
            pending,
            transition_until,
        )
