"""Compile complete recovery images using public LIFX packet serializers."""

from dataclasses import replace
from typing import TYPE_CHECKING

from lifx.protocol import packets
from lifx.protocol.base import Packet
from lifx.protocol.protocol_types import (
    LightHsbk,
    MultiZoneApplicationRequest,
    TileBufferRect,
)

from .parallel import ParallelCommand, ParallelPacket
from .virtual_power import VirtualPowerImage, image_matches_layout

if TYPE_CHECKING:
    from .coordinator import LIFXState


def _payload(packet: Packet) -> ParallelPacket:
    return ParallelPacket(packet.PKT_TYPE, packet.pack())


def _batch(payloads: list[ParallelPacket]) -> tuple[ParallelCommand, ...]:
    # Keep sequences unique within the outstanding ACK window.
    return tuple(
        ParallelCommand("packet_batch", (tuple(payloads[offset : offset + 64]),))
        for offset in range(0, len(payloads), 64)
    )


def compile_image_stages(
    image: VirtualPowerImage,
    state: LIFXState,
    *,
    duration_ms: int,
    power_on: bool | None,
) -> tuple[ParallelCommand, ...]:
    """Buffer full patterns, then release visible work at one final stage."""
    if not image_matches_layout(image, state):
        raise ValueError("Recovery image does not match the current device layout")
    hidden = power_on is True and state.power == 0
    duration = 0 if hidden else duration_ms
    if image.zones:
        stages = _zone_stages(image, state, duration)
    elif image.tiles:
        stages = _tile_stages(image, duration, hidden=hidden)
    else:
        color = image.color.to_protocol()
        stages = (
            ParallelCommand(
                "color",
                (color.hue, color.saturation, color.brightness, color.kelvin, duration),
            ),
        )
    if hidden:
        return (*stages, ParallelCommand("power", (True, duration_ms)))
    return stages


def _zone_stages(
    image: VirtualPowerImage, state: LIFXState, duration: int
) -> tuple[ParallelCommand, ...]:
    colors = [color.to_protocol() for color in image.zones]
    prepare: list[ParallelPacket] = []
    empty = LightHsbk(0, 0, 0, 3500)
    if state.capabilities.has_extended_multizone:
        for index in range(0, len(colors), 82):
            chunk = colors[index : index + 82]
            prepare.append(
                _payload(
                    packets.MultiZone.SetExtendedColorZones(
                        duration=duration,
                        apply=MultiZoneApplicationRequest.NO_APPLY,
                        index=index,
                        colors_count=len(chunk),
                        colors=[*chunk, *([empty] * (82 - len(chunk)))],
                    )
                )
            )
        release = packets.MultiZone.SetExtendedColorZones(
            duration=duration,
            apply=MultiZoneApplicationRequest.APPLY_ONLY,
            index=0,
            colors_count=0,
            colors=[empty] * 82,
        )
    else:
        if len(colors) > 256:
            raise ValueError("Legacy multizone cannot address more than 256 zones")
        for index, color in enumerate(colors):
            prepare.append(
                _payload(
                    packets.MultiZone.SetColorZones(
                        start_index=index,
                        end_index=index,
                        color=color,
                        duration=duration,
                        apply=MultiZoneApplicationRequest.NO_APPLY,
                    )
                )
            )
        release = packets.MultiZone.SetColorZones(
            start_index=0,
            end_index=0,
            color=empty,
            duration=duration,
            apply=MultiZoneApplicationRequest.APPLY_ONLY,
        )
    return (*_batch(prepare), *_batch([_payload(release)]))


def _tile_stages(
    image: VirtualPowerImage, duration: int, *, hidden: bool
) -> tuple[ParallelCommand, ...]:
    prepare: list[ParallelPacket] = []
    visible: list[ParallelPacket] = []
    for tile in image.tiles:
        buffered = not hidden and (tile.frame_buffers > 0 or len(tile.colors) > 64)
        target = prepare if buffered else visible
        for x in range(0, tile.width, 64):
            width = min(64, tile.width - x)
            rows = max(1, 64 // width)
            for y in range(0, tile.height, rows):
                height = min(rows, tile.height - y)
                colors = [
                    tile.colors[row * tile.width + column].to_protocol()
                    for row in range(y, y + height)
                    for column in range(x, x + width)
                ]
                target.append(
                    _payload(
                        packets.Tile.Set64(
                            tile_index=tile.index,
                            length=1,
                            rect=TileBufferRect(
                                fb_index=int(buffered), x=x, y=y, width=width
                            ),
                            duration=0 if buffered else duration,
                            colors=[
                                *colors,
                                *([LightHsbk(0, 0, 0, 3500)] * (64 - len(colors))),
                            ],
                        )
                    )
                )
        if buffered:
            visible.append(
                _payload(
                    packets.Tile.CopyFrameBuffer(
                        tile_index=tile.index,
                        length=1,
                        src_fb_index=1,
                        dst_fb_index=0,
                        src_x=0,
                        src_y=0,
                        dst_x=0,
                        dst_y=0,
                        width=tile.width,
                        height=tile.height,
                        duration=duration,
                    )
                )
            )
    return (*_batch(prepare), *_batch(visible))


def align_visible_stages(
    stages: tuple[tuple[ParallelCommand, ...], ...],
) -> tuple[ParallelCommand | None, ...]:
    """Pad preparations so each member's last stage shares a release gate."""
    count = max((len(member) for member in stages), default=0)
    result: list[ParallelCommand | None] = []
    for member in stages:
        command = None
        for stage in reversed(member):
            command = replace(stage, second=command)
        result.append(
            replace(command, pad_before=count - len(member))
            if command is not None
            else None
        )
    return tuple(result)
