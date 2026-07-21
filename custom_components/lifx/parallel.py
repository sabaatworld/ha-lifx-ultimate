"""Low-skew LIFX LAN dispatch for virtual parallel groups."""

from collections import deque
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum, auto
import gc
import multiprocessing as mp
from multiprocessing.connection import Connection, wait
import secrets
import signal
import socket
import struct
import threading
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import _LOGGER

DEFAULT_PORT = 56700
HEADER = struct.Struct("<HHI8s6sBBQHH")
COLOR_PAYLOAD = struct.Struct("<BHHHHI")
POWER_PAYLOAD = struct.Struct("<HI")
WAVEFORM_OPTIONAL_PAYLOAD = struct.Struct("<BBHHHHIfhBBBBB")
MULTIZONE_EFFECT_PAYLOAD = struct.Struct("<IB2xIQII32s")
TILE_EFFECT_PAYLOAD = struct.Struct("<2xIBIQII32sB128s")
HEADER_SIZE = HEADER.size

GET_SERVICE = 2
STATE_SERVICE = 3
SET_COLOR = 102
SET_POWER = 117
SET_WAVEFORM_OPTIONAL = 119
ACKNOWLEDGEMENT = 45
ECHO_REQUEST = 58
ECHO_RESPONSE = 59
ECHO_PAYLOAD_SIZE = 64
SET_REBOOT = 38
SET_MULTIZONE_EFFECT = 508
SET_TILE_EFFECT = 719


class _ParallelPreparationError(HomeAssistantError):
    """A worker failed before the shared send deadline."""


class _ParallelAckTimeout(HomeAssistantError):
    """A required acknowledgement did not arrive from every member."""


class _ParallelPreempted(HomeAssistantError):
    """A newer request replaced the active staged dispatch."""


class ParallelDispatchOutcome(Enum):
    """A safe result for one Device Group transport request."""

    COMPLETED = auto()
    SUPERSEDED = auto()
    UNAVAILABLE = auto()
    FAILED = auto()


@dataclass(frozen=True, slots=True)
class ParallelDispatchResult:
    """The non-exceptional result of one Device Group transport request."""

    outcome: ParallelDispatchOutcome
    request_id: int
    failed_member_indexes: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class ParallelCommand:
    """One already-resolved instruction for a single worker."""

    kind: str
    payload: tuple[Any, ...]
    second: ParallelCommand | None = None
    pad_before: int = 0

    @property
    def stages(self) -> tuple[ParallelCommand | None, ...]:
        """Return this command's ordered dependency stages."""
        command = ParallelCommand(self.kind, self.payload)
        stages = (command,) if self.second is None else (command, *self.second.stages)
        return (None,) * self.pad_before + stages


@dataclass(slots=True)
class _Worker:
    host: str
    process: Any
    pipe: Connection


@dataclass(slots=True)
class _DispatchRequest:
    """One supersedable request owned by the dispatcher thread."""

    request_id: int
    commands: tuple[ParallelCommand, ...]
    done: threading.Event
    health: bool = False
    result: ParallelDispatchResult | None = None


def _header(
    packet_type: int,
    source: int,
    sequence: int,
    target: bytes,
    payload_size: int,
    *,
    tagged: bool = False,
    ack_required: bool = False,
) -> bytes:
    """Build an addressable LIFX header with an optional acknowledgement."""
    frame = 1024 | (1 << 12) | ((1 << 13) if tagged else 0)
    return HEADER.pack(
        HEADER_SIZE + payload_size,
        frame,
        source,
        target,
        bytes(6),
        2 if ack_required else 0,
        sequence,
        0,
        packet_type,
        0,
    )


def _get_service(source: int, sequence: int) -> bytes:
    return _header(GET_SERVICE, source, sequence, bytes(8), 0, tagged=True)


def _echo_request(source: int, sequence: int, target: bytes, token: bytes) -> bytes:
    """Build an EchoRequest with an exact response token."""
    if len(token) != ECHO_PAYLOAD_SIZE:
        raise ValueError("LIFX EchoRequest token must be 64 bytes")
    return _header(ECHO_REQUEST, source, sequence, target, len(token)) + token


def _set_color(
    source: int,
    sequence: int,
    target: bytes,
    payload: tuple[Any, ...],
    *,
    ack_required: bool = False,
) -> bytes:
    hue, saturation, brightness, kelvin, duration = payload
    body = COLOR_PAYLOAD.pack(0, hue, saturation, brightness, kelvin, duration)
    return (
        _header(
            SET_COLOR, source, sequence, target, len(body), ack_required=ack_required
        )
        + body
    )


def _set_power(
    source: int,
    sequence: int,
    target: bytes,
    payload: tuple[Any, ...],
    *,
    ack_required: bool = False,
) -> bytes:
    power, duration = payload
    body = POWER_PAYLOAD.pack(65535 if power else 0, duration)
    return (
        _header(
            SET_POWER, source, sequence, target, len(body), ack_required=ack_required
        )
        + body
    )


def _set_waveform_optional(
    source: int,
    sequence: int,
    target: bytes,
    payload: tuple[Any, ...],
    *,
    ack_required: bool = False,
) -> bytes:
    (
        transient,
        hue,
        saturation,
        brightness,
        kelvin,
        period,
        cycles,
        skew_ratio,
        waveform,
        set_hue,
        set_saturation,
        set_brightness,
        set_kelvin,
    ) = payload
    body = WAVEFORM_OPTIONAL_PAYLOAD.pack(
        0,
        transient,
        hue,
        saturation,
        brightness,
        kelvin,
        period,
        cycles,
        skew_ratio,
        waveform,
        set_hue,
        set_saturation,
        set_brightness,
        set_kelvin,
    )
    return (
        _header(
            SET_WAVEFORM_OPTIONAL,
            source,
            sequence,
            target,
            len(body),
            ack_required=ack_required,
        )
        + body
    )


def _set_reboot(
    source: int, sequence: int, target: bytes, *, ack_required: bool = False
) -> bytes:
    return _header(SET_REBOOT, source, sequence, target, 0, ack_required=ack_required)


def _set_multizone_effect(
    source: int,
    sequence: int,
    target: bytes,
    payload: tuple[Any, ...],
    *,
    ack_required: bool = False,
) -> bytes:
    """Build a SetMultiZoneEffect packet."""
    effect, speed, direction = payload
    parameters = struct.pack("<II6I", 0, direction, 0, 0, 0, 0, 0, 0)
    body = MULTIZONE_EFFECT_PAYLOAD.pack(
        secrets.randbits(32), effect, speed, 0, 0, 0, parameters
    )
    return (
        _header(
            SET_MULTIZONE_EFFECT,
            source,
            sequence,
            target,
            len(body),
            ack_required=ack_required,
        )
        + body
    )


def _set_tile_effect(
    source: int,
    sequence: int,
    target: bytes,
    payload: tuple[Any, ...],
    *,
    ack_required: bool = False,
) -> bytes:
    """Build a SetTileEffect packet."""
    effect, speed, sky_type, cloud_saturation_min, cloud_saturation_max, palette = (
        payload
    )
    parameters = bytes(
        (
            sky_type,
            0,
            0,
            0,
            cloud_saturation_min,
            0,
            0,
            0,
            cloud_saturation_max,
        )
    ) + bytes(23)
    colors = tuple(palette)[:16]
    packed_palette = b"".join(struct.pack("<HHHH", *color) for color in colors)
    packed_palette += bytes(128 - len(packed_palette))
    body = TILE_EFFECT_PAYLOAD.pack(
        secrets.randbits(32),
        effect,
        speed,
        0,
        0,
        0,
        parameters,
        len(colors),
        packed_palette,
    )
    return (
        _header(
            SET_TILE_EFFECT,
            source,
            sequence,
            target,
            len(body),
            ack_required=ack_required,
        )
        + body
    )


def _send_packet(udp: socket.socket, packet: bytes, message: str) -> None:
    """Send a complete datagram or raise a useful socket error."""
    if udp.send(packet) != len(packet):
        raise OSError(message)


def _parse_header(data: bytes) -> tuple[int, int, bytes, int]:
    if len(data) < HEADER_SIZE:
        raise ValueError("LIFX response is shorter than its header")
    size, _frame, source, target, _reserved, _flags, sequence, _r2, packet_type, _r3 = (
        HEADER.unpack_from(data)
    )
    if size != len(data):
        raise ValueError("LIFX response has an invalid size")
    return source, sequence, target, packet_type


def _preflight(
    udp: socket.socket,
    source: int,
    next_sequence: Callable[[], int],
    *,
    pipe: Connection | None = None,
    current_generation: Any = None,
    request_id: int | None = None,
    deferred_controls: deque[tuple[Any, ...]] | None = None,
) -> bytes | None:
    """Resolve the target and prove the member can answer before dispatching."""
    if (
        request_id is not None
        and current_generation is not None
        and current_generation.value != request_id
    ):
        return None
    sequence = next_sequence()
    udp.send(_get_service(source, sequence))
    deadline = time.monotonic() + 1.0
    while (remaining := deadline - time.monotonic()) > 0:
        if (
            request_id is not None
            and current_generation is not None
            and current_generation.value != request_id
        ):
            return None
        if pipe is not None and pipe.poll(0):
            control = pipe.recv()
            if control[0] in {"SHUTDOWN", "CANCEL"}:
                return None
            if deferred_controls is not None:
                deferred_controls.append(control)
            continue
        udp.settimeout(min(0.01, remaining))
        try:
            data = udp.recv(2048)
            response_source, response_sequence, target, packet_type = _parse_header(data)
        except (struct.error, ValueError):
            continue
        if (
            response_source == source
            and response_sequence == sequence
            and packet_type == STATE_SERVICE
            and len(data) == HEADER_SIZE + 5
        ):
            try:
                _service, port = struct.unpack_from("<BI", data, HEADER_SIZE)
            except struct.error:
                continue
            if not 1 <= port <= 65535:
                raise ValueError("LIFX member reported an invalid UDP port")
            return target, port
    raise TimeoutError("Timed out waiting for LIFX service response")


def _build_packet(
    command: ParallelCommand,
    source: int,
    target: bytes,
    next_sequence: Callable[[], int],
    *,
    ack_required: bool,
) -> tuple[bytes, int]:
    """Build one packet and retain the sequence needed for its ACK."""
    packet_sequence = next_sequence()
    if command.kind == "color":
        packet = _set_color(
            source,
            packet_sequence,
            target,
            command.payload,
            ack_required=ack_required,
        )
    elif command.kind == "power":
        packet = _set_power(
            source,
            packet_sequence,
            target,
            command.payload,
            ack_required=ack_required,
        )
    elif command.kind == "echo":
        packet = _echo_request(source, packet_sequence, target, command.payload[0])
    elif command.kind == "waveform_optional":
        packet = _set_waveform_optional(
            source,
            packet_sequence,
            target,
            command.payload,
            ack_required=ack_required,
        )
    elif command.kind == "reboot":
        packet = _set_reboot(
            source, packet_sequence, target, ack_required=ack_required
        )
    elif command.kind == "multizone_effect":
        packet = _set_multizone_effect(
            source,
            packet_sequence,
            target,
            command.payload,
            ack_required=ack_required,
        )
    elif command.kind == "tile_effect":
        packet = _set_tile_effect(
            source,
            packet_sequence,
            target,
            command.payload,
            ack_required=ack_required,
        )
    else:
        raise ValueError(f"unsupported command: {command.kind}")
    return packet, packet_sequence


def _wait_for_ack(
    udp: socket.socket,
    pipe: Connection,
    source: int,
    target: bytes,
    sequence: int,
    request_id: int,
    stage: int,
    attempt: int,
    current_generation: Any,
    stage_deadline: float,
    deferred_controls: deque[tuple[Any, ...]] | None = None,
) -> bool | None:
    """Wait for a matching ACK, or abandon a request superseded in flight."""
    deadline = min(time.monotonic() + 3.0, stage_deadline)
    while (remaining := deadline - time.monotonic()) > 0:
        if current_generation.value != request_id:
            return None
        if pipe.poll(0):
            control = pipe.recv()
            if control[:3] == ("CANCEL", request_id, stage):
                return None
            if deferred_controls is not None:
                deferred_controls.append(control)
        try:
            udp.settimeout(min(0.01, remaining))
        except OSError:
            return False
        try:
            data = udp.recv(2048)
        except TimeoutError:
            continue
        except OSError:
            return False
        try:
            response_source, response_sequence, response_target, packet_type = _parse_header(
                data
            )
        except (struct.error, ValueError):
            continue
        if (
            response_source == source
            and response_sequence == sequence
            and response_target == target
            and packet_type == ACKNOWLEDGEMENT
            and len(data) == HEADER_SIZE
        ):
            return True
    return False


def _wait_for_echo(
    udp: socket.socket,
    pipe: Connection,
    source: int,
    target: bytes,
    sequence: int,
    token: bytes,
    request_id: int,
    stage: int,
    attempt: int,
    current_generation: Any,
    stage_deadline: float,
    deferred_controls: deque[tuple[Any, ...]] | None = None,
) -> bool | None:
    """Wait for the exact EchoResponse, or abandon a superseded request."""
    deadline = min(time.monotonic() + 3.0, stage_deadline)
    while (remaining := deadline - time.monotonic()) > 0:
        if current_generation.value != request_id:
            return None
        if pipe.poll(0):
            control = pipe.recv()
            if control[:3] == ("CANCEL", request_id, stage):
                return None
            if deferred_controls is not None:
                deferred_controls.append(control)
        try:
            udp.settimeout(min(0.01, remaining))
            data = udp.recv(2048)
        except TimeoutError:
            continue
        except OSError:
            return False
        try:
            response_source, response_sequence, response_target, packet_type = _parse_header(
                data
            )
        except (struct.error, ValueError):
            continue
        if (
            response_source == source
            and response_sequence == sequence
            and response_target == target
            and packet_type == ECHO_RESPONSE
            and len(data) == HEADER_SIZE + ECHO_PAYLOAD_SIZE
            and data[HEADER_SIZE:] == token
        ):
            return True
    return False


def _wait_for_dispatch_release(
    pipe: Connection,
    request_id: int,
    stage: int,
    attempt: int,
    current_generation: Any,
    stop_event: Any,
    deferred_controls: deque[tuple[Any, ...]] | None,
) -> float | None:
    """Wait for the parent release deadline, or report a cancelled request."""
    while True:
        if stop_event.is_set() or current_generation.value != request_id:
            return None
        if not pipe.poll(0.01):
            continue
        control = pipe.recv()
        if control[:3] == ("CANCEL", request_id, stage):
            return None
        if control[:4] != ("DISPATCH", request_id, stage, attempt):
            if deferred_controls is not None:
                deferred_controls.append(control)
            continue
        deadline_ns = control[4]
        stage_deadline = control[5]
        while (remaining := deadline_ns - time.monotonic_ns()) > 1_000_000:
            if current_generation.value != request_id or pipe.poll(0):
                break
            time.sleep(remaining / 1_000_000_000)
        if current_generation.value != request_id or pipe.poll(0):
            continue
        while time.monotonic_ns() < deadline_ns:
            pass
        return stage_deadline


def _report_dispatch_result(
    udp: socket.socket,
    pipe: Connection,
    packet: bytes,
    command: ParallelCommand,
    source: int,
    target: bytes,
    sequence: int,
    request_id: int,
    stage: int,
    attempt: int,
    ack_required: bool,
    stage_deadline: float,
    current_generation: Any,
    deferred_controls: deque[tuple[Any, ...]] | None,
) -> None:
    """Send one packet and return its ACK, Echo, or send outcome."""
    try:
        _send_packet(udp, packet, "short UDP send")
    except (OSError, ValueError) as err:
        pipe.send(("ERROR", request_id, stage, attempt, str(err)))
        return
    if command.kind == "echo":
        echo = _wait_for_echo(
            udp,
            pipe,
            source,
            target,
            sequence,
            command.payload[0],
            request_id,
            stage,
            attempt,
            current_generation,
            stage_deadline,
            deferred_controls,
        )
        pipe.send(
            (
                "ECHOED"
                if echo is True
                else "CANCELLED"
                if echo is None
                else "ECHO_TIMEOUT",
                request_id,
                stage,
                attempt,
            )
        )
        return
    if not ack_required:
        pipe.send(
            ("SENT", request_id, stage, attempt, command.kind, target.hex(), sequence)
        )
        return
    ack = _wait_for_ack(
        udp,
        pipe,
        source,
        target,
        sequence,
        request_id,
        stage,
        attempt,
        current_generation,
        stage_deadline,
        deferred_controls,
    )
    if ack is True:
        pipe.send(
            ("ACKED", request_id, stage, attempt, command.kind, target.hex(), sequence)
        )
    elif ack is None:
        pipe.send(("CANCELLED", request_id, stage, attempt))
    else:
        pipe.send(("ACK_TIMEOUT", request_id, stage, attempt))


def _dispatch_prepared(
    udp: socket.socket,
    source: int,
    target: bytes,
    pipe: Connection,
    request_id: int,
    stage: int,
    attempt: int,
    command: ParallelCommand,
    ack_required: bool,
    stage_deadline: float,
    current_generation: Any,
    stop_event: Any,
    next_sequence: Callable[[], int],
    gc_was_enabled: bool,
    deferred_controls: deque[tuple[Any, ...]] | None = None,
) -> bool:
    """Send one staged packet after its request-scoped dispatch message."""
    try:
        packet, sequence = _build_packet(
            command, source, target, next_sequence, ack_required=ack_required
        )
    except (TypeError, ValueError) as err:
        pipe.send(("ERROR", request_id, stage, attempt, str(err)))
        return True
    if gc_was_enabled:
        gc.disable()
    try:
        pipe.send(
            (
                "READY",
                request_id,
                stage,
                attempt,
                command.kind,
                target.hex(),
                sequence,
                time.monotonic_ns(),
            )
        )
        stage_deadline = _wait_for_dispatch_release(
            pipe,
            request_id,
            stage,
            attempt,
            current_generation,
            stop_event,
            deferred_controls,
        )
        if stage_deadline is None:
            pipe.send(("CANCELLED", request_id, stage, attempt))
            return True
        if (ack_required or command.kind == "echo") and time.monotonic() >= stage_deadline:
            pipe.send(("ACK_TIMEOUT", request_id, stage, attempt))
            return True
        _report_dispatch_result(
            udp,
            pipe,
            packet,
            command,
            source,
            target,
            sequence,
            request_id,
            stage,
            attempt,
            ack_required,
            stage_deadline,
            current_generation,
            deferred_controls,
        )
    finally:
        if gc_was_enabled and not gc.isenabled():
            gc.enable()
    return True


def _worker(
    host: str,
    source: int,
    pipe: Connection,
    current_generation: Any,
    stop_event: Any,
    port: int,
) -> None:
    """Own a socket for one light and wait for request-scoped control messages."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    udp: socket.socket | None = None
    service_port = port
    sequence = 0
    gc_was_enabled = gc.isenabled()
    deferred_controls: deque[tuple[Any, ...]] = deque()

    def next_sequence() -> int:
        nonlocal sequence
        value = sequence
        sequence = (sequence + 1) & 0xFF
        return value

    def reconnect(new_host: str, request_id: int | None = None) -> bytes | None:
        """Replace only this worker's UDP socket and resolve its target."""
        nonlocal udp
        if udp is not None:
            udp.close()
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.connect((new_host, service_port))
        response = _preflight(
            udp,
            source,
            next_sequence,
            pipe=pipe if request_id is not None else None,
            current_generation=current_generation,
            request_id=request_id,
            deferred_controls=deferred_controls,
        )
        if response is None:
            return None
        target, member_port = response
        udp.connect((new_host, member_port))
        return target

    try:
        target = reconnect(host)
        pipe.send(("PREFLIGHT_OK",))

        while command := (
            deferred_controls.popleft() if deferred_controls else pipe.recv()
        ):
            if command[0] == "SHUTDOWN":
                break
            if command[0] == "CANCEL":
                _kind, request_id, stage = command
                pipe.send(("CANCELLED", request_id, stage))
                continue
            if command[0] == "RECONNECT":
                _kind, reconnect_host, reconnect_request_id = command
                try:
                    new_target = reconnect(reconnect_host, reconnect_request_id)
                except (OSError, TimeoutError, ValueError, struct.error) as err:
                    pipe.send(("RECONNECT_ERROR", reconnect_request_id, str(err)))
                else:
                    if new_target is None:
                        pipe.send(("RECONNECT_CANCELLED", reconnect_request_id))
                    else:
                        target = new_target
                        pipe.send(("RECONNECTED", reconnect_request_id))
                continue
            _kind, request_id, stage, attempt, staged_command, ack_required, stage_deadline = (
                command
            )
            assert udp is not None
            if not _dispatch_prepared(
                udp,
                source,
                target,
                pipe,
                request_id,
                stage,
                attempt,
                staged_command,
                ack_required,
                stage_deadline,
                current_generation,
                stop_event,
                next_sequence,
                gc_was_enabled,
                deferred_controls,
            ):
                break
    except (EOFError, OSError, ValueError) as err:
        with suppress(BrokenPipeError, EOFError, OSError):
            pipe.send(("PREFLIGHT_ERROR", str(err)))
    finally:
        if gc_was_enabled and not gc.isenabled():
            gc.enable()
        if udp is not None:
            udp.close()
        pipe.close()


class LIFXParallelRuntime:
    """A warmed, process-isolated LIFX dispatcher for one virtual group."""

    def __init__(
        self, hass: HomeAssistant, hosts: Iterable[str], *, port: int = DEFAULT_PORT
    ) -> None:
        """Initialize the process supervisor."""
        self.hass = hass
        self.hosts = tuple(hosts)
        self.port = port
        self._workers: list[_Worker] = []
        self._stop_event: Any = None
        self._current_generation: Any = None
        self._context: Any = None
        self._source: int | None = None
        self._request_id = 0
        self._lock = threading.Lock()
        self._dispatch_condition = threading.Condition()
        self._pending_dispatch: _DispatchRequest | None = None
        self._active_dispatch: _DispatchRequest | None = None
        self._active_stage: tuple[int, int, tuple[_Worker, ...]] | None = None
        self._worker_events: dict[Connection, deque[tuple[Any, ...]]] = {}
        self._dispatcher_stopping = False
        self._dispatcher: threading.Thread | None = None

    @property
    def available(self) -> bool:
        """Return whether every persistent worker remains alive."""
        return bool(self._workers) and all(
            worker.process.is_alive() for worker in self._workers
        )

    @property
    def worker_health(self) -> tuple[bool, ...]:
        """Return non-sensitive liveness for the persistent worker pool."""
        return tuple(worker.process.is_alive() for worker in self._workers)

    async def async_start(self) -> None:
        """Start and preflight all member workers outside the event loop."""
        await self.hass.async_add_executor_job(self._start)

    def _start(self) -> None:
        if not self.hosts:
            raise HomeAssistantError("A parallel LIFX group needs at least one member")
        self._context = mp.get_context("spawn")
        self._stop_event = self._context.Event()
        self._current_generation = self._context.Value("Q", 0)
        self._source = secrets.randbelow(0xFFFFFFFE) + 2
        try:
            for index, host in enumerate(self.hosts):
                self._workers.append(self._spawn_worker(index, host))
            self._collect("PREFLIGHT_OK", 5.0)
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop,
                name="lifx-parallel-dispatcher",
                daemon=True,
            )
            self._dispatcher.start()
        except Exception:
            self._stop()
            raise

    async def async_stop(self) -> None:
        """Stop every worker outside the event loop."""
        await self.hass.async_add_executor_job(self._stop)

    def _stop(self) -> None:
        with self._dispatch_condition:
            self._dispatcher_stopping = True
            if self._pending_dispatch is not None:
                self._pending_dispatch.result = ParallelDispatchResult(
                    ParallelDispatchOutcome.UNAVAILABLE,
                    self._pending_dispatch.request_id,
                )
                self._pending_dispatch.done.set()
                self._pending_dispatch = None
            self._dispatch_condition.notify_all()
        if self._dispatcher is not None:
            self._dispatcher.join(timeout=1.0)
            self._dispatcher = None
        with self._lock:
            if self._stop_event is not None:
                self._stop_event.set()
            for worker in self._workers:
                if worker.process.is_alive():
                    with suppress(BrokenPipeError, EOFError, OSError):
                        worker.pipe.send(("SHUTDOWN",))
            for worker in self._workers:
                worker.process.join(timeout=1.0)
                if worker.process.is_alive():
                    worker.process.terminate()
                    worker.process.join(timeout=1.0)
                worker.pipe.close()
            self._workers.clear()

    def _spawn_worker(self, index: int, host: str) -> _Worker:
        """Start one worker in its fixed member slot."""
        assert self._context is not None
        assert self._source is not None
        assert self._stop_event is not None
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=_worker,
            args=(
                host,
                self._source,
                child,
                self._current_generation,
                self._stop_event,
                self.port,
            ),
            name=f"lifx-parallel-{index}-{host}",
        )
        process.start()
        child.close()
        return _Worker(host, process, parent)

    def _replace_dead_worker(self, index: int) -> None:
        """Replace an exact worker slot only after confirmed process death."""
        worker = self._workers[index]
        if worker.process.is_alive():
            return
        worker.process.join(timeout=0)
        worker.pipe.close()
        replacement = self._spawn_worker(index, worker.host)
        self._workers[index] = replacement
        self._collect_selected(
            {replacement.pipe: replacement}, "PREFLIGHT_OK", 5.0, None
        )

    def _replace_dead_workers(self) -> None:
        """Restore only the slots whose process has genuinely exited."""
        for index in range(len(self._workers)):
            self._replace_dead_worker(index)

    async def async_dispatch(
        self,
        commands: tuple[ParallelCommand, ...],
    ) -> ParallelDispatchResult:
        """Dispatch the latest request, preempting any older staged request."""
        return await self.hass.async_add_executor_job(self._queue_dispatch, commands)

    async def async_keepalive(self) -> ParallelDispatchResult:
        """Synchronize one cancellable Echo health check across every worker."""
        commands = tuple(
            ParallelCommand("echo", (secrets.token_bytes(ECHO_PAYLOAD_SIZE),))
            for _worker in self._workers
        )
        return await self.hass.async_add_executor_job(
            self._queue_dispatch, commands, True
        )

    async def async_cancel_health(self) -> None:
        """Discard an active idle health request without waiting for cleanup."""
        await self.hass.async_add_executor_job(self._cancel_health)

    def _cancel_health(self) -> None:
        with self._dispatch_condition:
            request = self._pending_dispatch
            if request is not None and request.health:
                request.result = ParallelDispatchResult(
                    ParallelDispatchOutcome.SUPERSEDED, request.request_id
                )
                request.done.set()
                self._pending_dispatch = None
            request = self._active_dispatch
            if request is None or not request.health:
                return
            request.result = ParallelDispatchResult(
                ParallelDispatchOutcome.SUPERSEDED, request.request_id
            )
            request.done.set()
            self._request_id += 1
            self._current_generation.value = self._request_id
            if self._active_stage is not None:
                request_id, stage, workers = self._active_stage
                for worker in workers:
                    with suppress(BrokenPipeError, EOFError, OSError):
                        worker.pipe.send(("CANCEL", request_id, stage))

    def _queue_dispatch(
        self, commands: tuple[ParallelCommand, ...], health: bool = False
    ) -> ParallelDispatchResult:
        """Immediately supersede an active wait and replace unsent work."""
        if len(commands) != len(self._workers):
            return ParallelDispatchResult(ParallelDispatchOutcome.UNAVAILABLE, 0)
        with self._dispatch_condition:
            if self._dispatcher_stopping:
                return ParallelDispatchResult(ParallelDispatchOutcome.UNAVAILABLE, 0)
            self._request_id += 1
            request = _DispatchRequest(
                self._request_id, commands, threading.Event(), health
            )
            if self._pending_dispatch is not None:
                self._pending_dispatch.result = ParallelDispatchResult(
                    ParallelDispatchOutcome.SUPERSEDED,
                    self._pending_dispatch.request_id,
                )
                self._pending_dispatch.done.set()
            if self._active_dispatch is not None:
                self._active_dispatch.result = ParallelDispatchResult(
                    ParallelDispatchOutcome.SUPERSEDED,
                    self._active_dispatch.request_id,
                )
                self._active_dispatch.done.set()
            self._pending_dispatch = request
            self._current_generation.value = request.request_id
            if self._active_stage is not None:
                active_request_id, stage, workers = self._active_stage
                for worker in workers:
                    with suppress(BrokenPipeError, EOFError, OSError):
                        worker.pipe.send(("CANCEL", active_request_id, stage))
            self._dispatch_condition.notify()
        request.done.wait()
        return request.result or ParallelDispatchResult(
            ParallelDispatchOutcome.FAILED, request.request_id
        )

    def _dispatch_loop(self) -> None:
        """Serialize pipe ownership while allowing active work to be superseded."""
        while True:
            with self._dispatch_condition:
                while self._pending_dispatch is None and not self._dispatcher_stopping:
                    self._dispatch_condition.wait()
                if self._dispatcher_stopping:
                    return
                request = self._pending_dispatch
                self._pending_dispatch = None
                self._active_dispatch = request
            assert request is not None
            try:
                failed_member_indexes = self._dispatch(
                    request.request_id, request.commands, health=request.health
                )
                request.result = ParallelDispatchResult(
                    ParallelDispatchOutcome.COMPLETED,
                    request.request_id,
                    frozenset(failed_member_indexes),
                )
            except _ParallelPreempted:
                request.result = ParallelDispatchResult(
                    ParallelDispatchOutcome.SUPERSEDED, request.request_id
                )
            except HomeAssistantError:
                request.result = ParallelDispatchResult(
                    ParallelDispatchOutcome.FAILED, request.request_id
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("LIFX Device Group worker dispatcher failed")
                request.result = ParallelDispatchResult(
                    ParallelDispatchOutcome.FAILED, request.request_id
                )
            finally:
                request.done.set()
                with self._dispatch_condition:
                    if self._active_dispatch is request:
                        self._active_dispatch = None

    async def async_request_reconnect(
        self, index: int, host: str
    ) -> ParallelDispatchResult:
        """Ask one living worker to re-probe its member without replacing it."""
        return await self.hass.async_add_executor_job(
            self._request_reconnect, index, host
        )

    def _request_reconnect(self, index: int, host: str) -> ParallelDispatchResult:
        request_id = self._current_generation.value
        with self._lock:
            if index >= len(self._workers):
                return ParallelDispatchResult(ParallelDispatchOutcome.UNAVAILABLE, request_id)
            self._replace_dead_worker(index)
            worker = self._workers[index]
            try:
                worker.pipe.send(("RECONNECT", host, request_id))
                self._collect_selected(
                    {worker.pipe: worker}, "RECONNECTED", 1.5, request_id
                )
                worker.host = host
            except _ParallelPreempted:
                return ParallelDispatchResult(ParallelDispatchOutcome.SUPERSEDED, request_id)
            except (BrokenPipeError, EOFError, OSError, HomeAssistantError):
                return ParallelDispatchResult(ParallelDispatchOutcome.FAILED, request_id)
        return ParallelDispatchResult(ParallelDispatchOutcome.COMPLETED, request_id)

    def _dispatch(
        self,
        request_id: int,
        commands: tuple[ParallelCommand, ...],
        *,
        health: bool = False,
    ) -> set[int]:
        if len(commands) != len(self._workers):
            raise HomeAssistantError("The LIFX Device Group transport is unavailable")
        with self._lock:
            self._replace_dead_workers()
            stages = tuple(command.stages for command in commands)
            stage_count = max(len(member_stages) for member_stages in stages)
            failed_member_indexes: set[int] = set()
            for stage in range(stage_count):
                targets = tuple(
                    (worker, member_stages[stage])
                    for worker, member_stages in zip(self._workers, stages, strict=True)
                    if stage < len(member_stages) and member_stages[stage] is not None
                )
                if not targets:
                    continue
                ack_required = not health
                try:
                    with self._dispatch_condition:
                        self._active_stage = (
                            request_id,
                            stage,
                            tuple(worker for worker, _command in targets),
                        )
                    unresolved = targets
                    stage_deadline = time.monotonic() + (3.0 if health else 15.0)
                    for attempt in range(1 if health else 5):
                        unresolved = self._dispatch_stage(
                            request_id,
                            stage,
                            attempt,
                            unresolved,
                            ack_required,
                            stage_deadline,
                        )
                        if not unresolved or health:
                            break
                        if attempt == 4:
                            raise _ParallelAckTimeout(
                                "Timed out waiting for LIFX Device Group acknowledgements"
                            )
                    if health:
                        failed_member_indexes.update(
                            self._workers.index(worker)
                            for worker, _command in unresolved
                        )
                finally:
                    with self._dispatch_condition:
                        if self._active_stage is not None and self._active_stage[:2] == (
                            request_id,
                            stage,
                        ):
                            self._active_stage = None
            return failed_member_indexes

    def _dispatch_stage(
        self,
        request_id: int,
        stage: int,
        attempt: int,
        targets: tuple[tuple[_Worker, ParallelCommand], ...],
        ack_required: bool,
        stage_deadline: float | None,
    ) -> tuple[tuple[_Worker, ParallelCommand], ...]:
        """Dispatch one stage and return members that require an ACK retry."""
        if self._current_generation.value != request_id:
            raise _ParallelPreempted("LIFX Device Group command superseded")
        pending = {worker.pipe: (worker, command) for worker, command in targets}
        try:
            _LOGGER.debug(
                "LIFX Device Group request %s stage %s preparing: ack_required=%s "
                "member_count=%s",
                request_id,
                stage,
                ack_required,
                len(targets),
            )
            for worker, command in targets:
                worker.pipe.send(
                    (
                        "PREPARE",
                        request_id,
                        stage,
                        attempt,
                        command,
                        ack_required,
                        0.0,
                    )
                )
            self._collect_selected(
                {pipe: worker for pipe, (worker, _command) in pending.items()},
                "READY",
                0.25,
                request_id,
                stage=stage,
                attempt=attempt,
            )
        except _ParallelPreempted:
            raise
        except (BrokenPipeError, EOFError, HomeAssistantError, OSError) as err:
            self._cancel_stage(request_id, stage, targets)
            raise _ParallelPreparationError(str(err)) from err
        if self._current_generation.value != request_id:
            raise _ParallelPreempted("LIFX Device Group command superseded")
        attempt_deadline = time.monotonic() + 3.0
        try:
            self._dispatch_at_deadline(
                request_id,
                stage,
                attempt,
                (worker for worker, _command in targets),
                attempt_deadline,
            )
        except (BrokenPipeError, EOFError, OSError) as err:
            self._cancel_stage(request_id, stage, targets)
            raise _ParallelPreparationError(str(err)) from err
        if targets[0][1].kind == "echo":
            return self._collect_stage_acks(
                request_id,
                stage,
                attempt,
                pending,
                attempt_deadline,
                success_event="ECHOED",
            )
        if not ack_required:
            self._collect_selected(
                {pipe: worker for pipe, (worker, _command) in pending.items()},
                "SENT",
                1.0,
                request_id,
            )
            return ()
        return self._collect_stage_acks(
            request_id, stage, attempt, pending, attempt_deadline
        )

    def _collect_stage_acks(
        self,
        request_id: int,
        stage: int,
        attempt: int,
        pending: dict[Connection, tuple[_Worker, ParallelCommand]],
        stage_deadline: float,
        *,
        success_event: str = "ACKED",
    ) -> tuple[tuple[_Worker, ParallelCommand], ...]:
        """Collect one ACK outcome per worker without treating a timeout as fatal."""
        pending = pending.copy()
        unresolved: list[tuple[_Worker, ParallelCommand]] = []
        deadline = min(time.monotonic() + 3.05, stage_deadline)
        while pending:
            if self._current_generation.value != request_id:
                raise _ParallelPreempted("LIFX Device Group command superseded")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                unresolved.extend(pending.values())
                break
            messages = {
                pipe: event
                for pipe in pending
                if (
                    event := self._pop_worker_event(
                        pipe, request_id, stage, attempt
                    )
                )
                is not None
            }
            if messages:
                ready = set(messages)
            else:
                ready = wait(tuple(pending), timeout=min(0.05, remaining))
            if not ready:
                if any(not worker.process.is_alive() for worker, _command in pending.values()):
                    raise HomeAssistantError("A LIFX parallel worker exited")
                continue
            for pipe in ready:
                worker, command = pending[pipe]
                if message := messages.get(pipe):
                    pass
                else:
                    try:
                        message = pipe.recv()
                    except EOFError as err:
                        raise HomeAssistantError(
                            f"LIFX worker for {worker.host} closed unexpectedly"
                        ) from err
                if (
                    len(message) < 4
                    or message[1] != request_id
                    or message[2] != stage
                    or message[3] != attempt
                ):
                    self._store_worker_event(pipe, message)
                    continue
                pending.pop(pipe)
                if message[0] == success_event:
                    continue
                if message[0] == "CANCELLED":
                    raise _ParallelPreempted("LIFX Device Group command superseded")
                unresolved.append((worker, command))
        return tuple(unresolved)

    def _pop_worker_event(
        self,
        pipe: Connection,
        request_id: int,
        stage: int | None = None,
        attempt: int | None = None,
    ) -> tuple[Any, ...] | None:
        """Return a matching deferred event without dropping a newer request's data."""
        events = self._worker_events.get(pipe)
        if events is None:
            return None
        for index, event in enumerate(events):
            if len(event) < 2 or event[1] != request_id:
                continue
            if stage is not None and (len(event) < 3 or event[2] != stage):
                continue
            if attempt is not None and (len(event) < 4 or event[3] != attempt):
                continue
            del events[index]
            return event
        return None

    def _store_worker_event(self, pipe: Connection, event: tuple[Any, ...]) -> None:
        """Keep an out-of-order worker event for its owning request."""
        if (
            self._current_generation is not None
            and len(event) > 1
            and isinstance(event[1], int)
            and event[1] < self._current_generation.value
        ):
            _LOGGER.debug(
                "LIFX Device Group discarded superseded worker event: event=%s request=%s stage=%s",
                event[0] if event else None,
                event[1],
                event[2] if len(event) > 2 else None,
            )
            return
        self._worker_events.setdefault(pipe, deque()).append(event)
        _LOGGER.debug(
            "LIFX Device Group deferred stale worker event: event=%s request=%s stage=%s",
            event[0] if event else None,
            event[1] if len(event) > 1 else None,
            event[2] if len(event) > 2 else None,
        )

    @staticmethod
    def _dispatch_at_deadline(
        request_id: int,
        stage: int,
        attempt: int,
        workers: Iterable[_Worker],
        attempt_deadline: float,
    ) -> None:
        """Send each ready worker one request-scoped common deadline."""
        workers = tuple(workers)
        deadline = time.monotonic_ns() + 5_000_000
        _LOGGER.debug(
            "LIFX Device Group request %s stage %s releasing %s workers at "
            "monotonic_ns=%s (lead_ms=%.3f)",
            request_id,
            stage,
            len(workers),
            deadline,
            (deadline - time.monotonic_ns()) / 1_000_000,
        )
        for worker in workers:
            worker.pipe.send(
                (
                    "DISPATCH",
                    request_id,
                    stage,
                    attempt,
                    deadline,
                    attempt_deadline,
                )
            )

    def _cancel_stage(
        self,
        request_id: int,
        stage: int,
        targets: tuple[tuple[_Worker, ParallelCommand], ...],
    ) -> None:
        """Cancel a stage and wait until every worker has returned to idle."""
        pending = {worker.pipe: worker for worker, _command in targets}
        _LOGGER.debug(
            "LIFX Device Group request %s stage %s cancellation requested for %s workers",
            request_id,
            stage,
            len(pending),
        )
        for worker in pending.values():
            with suppress(BrokenPipeError, EOFError, OSError):
                worker.pipe.send(("CANCEL", request_id, stage))
        deadline = time.monotonic() + 1.0
        while pending:
            if (
                request_id is not None
                and self._current_generation is not None
                and self._current_generation.value != request_id
            ):
                raise _ParallelPreempted("LIFX Device Group command superseded")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HomeAssistantError("Timed out cancelling LIFX Device Group command")
            ready = wait(tuple(pending), timeout=min(0.01, remaining))
            for pipe in ready:
                worker = pending[pipe]
                try:
                    message = pipe.recv()
                except EOFError as err:
                    raise HomeAssistantError(
                        f"LIFX worker for {worker.host} closed unexpectedly"
                    ) from err
                if message[:3] == ("CANCELLED", request_id, stage):
                    pending.pop(pipe)

    def _collect(
        self,
        expected: str,
        timeout: float,
        request_id: int | None = None,
        on_message: Callable[[_Worker, tuple[Any, ...]], None] | None = None,
    ) -> dict[Connection, tuple[Any, ...]]:
        return self._collect_selected(
            {worker.pipe: worker for worker in self._workers},
            expected,
            timeout,
            request_id,
            on_message,
        )

    def _collect_selected(
        self,
        pending: dict[Connection, _Worker],
        expected: str,
        timeout: float,
        request_id: int | None = None,
        on_message: Callable[[_Worker, tuple[Any, ...]], None] | None = None,
        *,
        stage: int | None = None,
        attempt: int | None = None,
    ) -> dict[Connection, tuple[Any, ...]]:
        pending = pending.copy()
        responses: dict[Connection, tuple[Any, ...]] = {}
        errors: list[str] = []
        deadline = time.monotonic() + timeout
        while pending:
            if (
                request_id is not None
                and self._current_generation is not None
                and self._current_generation.value != request_id
            ):
                raise _ParallelPreempted("LIFX Device Group command superseded")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HomeAssistantError(
                    f"Timed out waiting for LIFX workers to {expected.lower()}"
                )
            messages = (
                {
                    pipe: event
                    for pipe in pending
                    if (
                        event := self._pop_worker_event(
                            pipe, request_id, stage, attempt
                        )
                    )
                    is not None
                }
                if request_id is not None
                else {}
            )
            ready = set(messages) or wait(tuple(pending), timeout=min(0.05, remaining))
            if not ready:
                if any(not worker.process.is_alive() for worker in pending.values()):
                    raise HomeAssistantError("A LIFX parallel worker exited")
                continue
            for pipe in ready:
                worker = pending[pipe]
                if message := messages.get(pipe):
                    pass
                else:
                    try:
                        message = pipe.recv()
                    except EOFError as err:
                        raise HomeAssistantError(
                            f"LIFX worker for {worker.host} closed unexpectedly"
                        ) from err
                if request_id is not None and (
                    len(message) < 2 or message[1] != request_id
                ):
                    self._store_worker_event(pipe, message)
                    continue
                if stage is not None and (len(message) < 3 or message[2] != stage):
                    self._store_worker_event(pipe, message)
                    continue
                if attempt is not None and (
                    len(message) < 4 or message[3] != attempt
                ):
                    self._store_worker_event(pipe, message)
                    continue
                if (
                    message[0] == "CANCELLED"
                    and request_id is not None
                    and self._current_generation is not None
                    and self._current_generation.value != request_id
                ):
                    raise _ParallelPreempted("LIFX Device Group command superseded")
                _LOGGER.debug(
                    "LIFX Device Group worker event: event=%s request=%s stage=%s",
                    message[0],
                    message[1] if len(message) > 1 else None,
                    message[2] if len(message) > 2 else None,
                )
                pending.pop(pipe)
                if message[0] != expected:
                    detail = message[-1] if len(message) > 1 else message[0]
                    errors.append(f"LIFX worker for {worker.host}: {detail}")
                    continue
                responses[pipe] = message
                if on_message is not None:
                    on_message(worker, message)
        if errors:
            raise HomeAssistantError("; ".join(errors))
        return responses
