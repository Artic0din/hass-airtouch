"""Auxiliary relay outputs on an AirTouch 5 console.

Status report 0x5D is the only authority for each output's type and contact.
While answering it, the console can broadcast an FF 04 block whose records
carry native names. Its state bytes are never used. Trigger 0x5C carries one
action byte per output. No reply to it is known, so only a later 0x5D report
can show its effect.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Final, TypeVar, cast

from homeassistant.config_entries import current_entry
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from pyairtouch import AirTouchModel
from pyairtouch.at5.api import DEFAULT_PORT_NUMBER

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from pyairtouch import AirTouch

from .console import (
    DEFAULT_DEADLINE,
    ConsoleClient,
    ConsoleProtocolError,
    ConsoleSendUncertainError,
)

_LOGGER = logging.getLogger(__name__)

_OWNERS_KEY: Final = "airtouch_io_outputs"
_SUBHEADER: Final = struct.Struct("!BxHHH")
_STATUS_ID: Final = 0x5D
_TRIGGER_ID: Final = 0x5C
_QUERY: Final = _SUBHEADER.pack(_STATUS_ID, 0, 0, 0)
_NAMES_PREFIX: Final = b"\xff\x04"
_OUTPUT_COUNT: Final = 8
_MINIMUM_NORMAL: Final = 2
_MINIMUM_REPEAT: Final = 3
_INPUT_FLAG: Final = 0x80
_CHANNEL_MASK: Final = 0x07
_PARAMETER_MASK: Final = 0x0F
_UNUSED_TYPE: Final = 0
_TOGGLE_TYPE: Final = 2
_PULSE_TYPE: Final = 3
_PULSE_STEP_MS: Final = 50
_PULSE_STEPS: Final = range(1, 8)
_PULSE_DEFAULT_MS: Final = 350
_CLOSE: Final = 1
_OPEN: Final = 2
_PULSE: Final = 1
_NAME_HEADER: Final = 3
_NAME_RECORD_HEADER: Final = 3
_OUTPUT_KIND: Final = 0
_INPUT_KIND: Final = 1
_UPDATE_INTERVAL: Final = 60
# shortcut: settling is unmeasured; calibrate from configured-output traces.
_UNMEASURED_TOGGLE_SETTLING_READS: Final = 3
_UNMEASURED_TOGGLE_SETTLING_INTERVAL: Final = 0.5


class IoParseError(Exception):
    """The output report was truncated or internally inconsistent."""


class IoOutputError(Exception):
    """An output read or action failed."""


class IoExchangeError(IoOutputError):
    """A read or connection failed before any trigger was written."""


class IoRejectedError(IoOutputError):
    """The action was refused before any trigger was written."""


class IoUncertainError(IoOutputError):
    """A trigger may have reached the console, and no readback confirmed it."""


class IoUnconfirmedError(IoOutputError):
    """Valid readbacks after a trigger did not show the requested state."""


@dataclass(frozen=True, slots=True)
class ToggleOutput:
    """A latching output on channel 0 to 7. Closed is the reported contact."""

    channel: int
    name: str | None
    closed: bool


@dataclass(frozen=True, slots=True)
class PulseOutput:
    """An output the console pulses for width_ms per trigger. It reports no state."""

    channel: int
    name: str | None
    width_ms: int


RelayOutput = ToggleOutput | PulseOutput
_OutputT = TypeVar("_OutputT", ToggleOutput, PulseOutput)
_KIND_LABELS: Final[dict[type[RelayOutput], str]] = {
    ToggleOutput: "Toggle",
    PulseOutput: "Pulse",
}


@dataclass(frozen=True, slots=True)
class IoSnapshot:
    """The toggle and pulse outputs described by one report."""

    outputs: tuple[RelayOutput, ...]

    def output(self, channel: int) -> RelayOutput | None:
        """Return the output on this channel, or None when it is not usable."""
        return next((item for item in self.outputs if item.channel == channel), None)


def parse_io_report(payload: bytes) -> tuple[RelayOutput, ...]:
    """Parse a 0x5D report into its toggle and pulse outputs, without names.

    Inputs, unused outputs, and unknown types are left out. A channel the
    report does not describe is absent rather than unused. A repeated output
    id is an error even when one copy has an unknown type.
    """
    if len(payload) < _SUBHEADER.size:
        raise IoParseError("truncated output report header")
    sub_id, normal_length, repeat_length, repeat_count = _SUBHEADER.unpack_from(payload)
    if sub_id != _STATUS_ID:
        raise IoParseError("not an output report")
    if normal_length < _MINIMUM_NORMAL or (
        repeat_count > 0 and repeat_length < _MINIMUM_REPEAT
    ):
        raise IoParseError("output report sections are too short")
    start = _SUBHEADER.size + normal_length
    if len(payload) != start + repeat_length * repeat_count:
        raise IoParseError("output report length mismatch")
    closed_bits = payload[_SUBHEADER.size]
    seen: set[int] = set()
    outputs: list[RelayOutput] = []
    for index in range(repeat_count):
        offset = start + index * repeat_length
        record_id, parameter, output_type = payload[offset : offset + 3]
        if record_id & _INPUT_FLAG:
            continue
        channel = record_id & _CHANNEL_MASK
        if channel in seen:
            raise IoParseError("output report repeats an output")
        seen.add(channel)
        output = _decode_output(channel, parameter, output_type, closed_bits)
        if output is not None:
            outputs.append(output)
    return tuple(outputs)


def parse_io_names(body: bytes) -> dict[int, str] | None:
    """Return output names from the bytes after FF 04, or None for a bad layout.

    The writer sends the name length, record count, and stride, then records
    of kind, index, state, and name. An empty name means no native name.
    """
    try:
        return _parse_names(body)
    except IoParseError as err:
        _LOGGER.debug("Ignoring output names: %s", err)
        return None


class IoOutputsCoordinator(DataUpdateCoordinator[IoSnapshot]):
    """Own one console's outputs for both platforms.

    Polls and actions share one lock, so each action reads fresh types before
    any trigger and publishes every valid report it reads. Nothing is
    published or discovered after shutdown begins.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        airtouch: AirTouch,
        *,
        port: int = DEFAULT_PORT_NUMBER,
        deadline: float = DEFAULT_DEADLINE,
    ) -> None:
        self._client = ConsoleClient(airtouch.host, port, deadline=deadline)
        self._transaction_lock = asyncio.Lock()
        self._closed = False
        self._discovery: list[Callable[[], None]] = []
        token = current_entry.set(entry)
        try:
            super().__init__(
                hass,
                _LOGGER,
                name="AirTouch relay outputs",
                update_interval=timedelta(seconds=_UPDATE_INTERVAL),
            )
        finally:
            current_entry.reset(token)

    @callback
    def async_add_discovery(
        self, kind: type[RelayOutput], add: Callable[[list[int]], None]
    ) -> Callable[[], None]:
        """Pass add each channel the first time a report shows it as this kind.

        The listener also keeps polling alive with no entities. The returned
        remover can be called again, and shutdown calls it too.
        """
        known: set[int] = set()

        @callback
        def discover() -> None:
            if not self.last_update_success or self.data is None:
                return
            new = [
                item.channel
                for item in self.data.outputs
                if isinstance(item, kind) and item.channel not in known
            ]
            if new:
                known.update(new)
                add(new)

        unsubscribe = self.async_add_listener(discover)
        removed = False

        @callback
        def remove() -> None:
            nonlocal removed
            if not removed:
                removed = True
                unsubscribe()

        self._discovery.append(remove)
        discover()
        return remove

    async def async_set_closed(self, channel: int, *, closed: bool) -> None:
        """Close or open a toggle output and wait for a report that shows it.

        A matching contact sends nothing. Otherwise one trigger is sent and
        the report is read up to three times. The trigger is never repeated.
        """
        async with self._transaction_lock:
            output = await self._fresh(channel, ToggleOutput)
            if output.closed == closed:
                return
            await self._send(_trigger(channel, _CLOSE if closed else _OPEN))
            try:
                for attempt in range(_UNMEASURED_TOGGLE_SETTLING_READS):
                    if attempt:
                        await asyncio.sleep(_UNMEASURED_TOGGLE_SETTLING_INTERVAL)
                    try:
                        snapshot = await self._read()
                    except IoExchangeError as err:
                        raise self._uncertain(
                            f"output {channel + 1} readback failed after its trigger, "
                            "which was not sent again",
                            err,
                        ) from err
                    self._publish(snapshot)
                    readback = snapshot.output(channel)
                    if not isinstance(readback, ToggleOutput):
                        raise IoUnconfirmedError(f"output {channel + 1} changed type")
                    if readback.closed == closed:
                        return
                raise IoUnconfirmedError(
                    f"output {channel + 1} did not report the requested state"
                )
            except asyncio.CancelledError:
                self._uncertain("toggle confirmation was cancelled after its trigger")
                raise

    async def async_pulse(self, channel: int) -> None:
        """Send one pulse trigger and return once it is written.

        Returning does not prove the pulse happened, and nothing is retried.
        """
        async with self._transaction_lock:
            await self._fresh(channel, PulseOutput)
            await self._send(_trigger(channel, _PULSE))

    async def async_shutdown(self) -> None:
        """Refuse new work, detach discovery, and drain an admitted transaction."""
        self._closed = True
        for remove in self._discovery:
            remove()
        self._discovery.clear()
        await super().async_shutdown()
        async with self._transaction_lock:
            pass

    async def _async_update_data(self) -> IoSnapshot:
        async with self._transaction_lock:
            if self._closed:
                raise UpdateFailed("output owner is closed")
            try:
                snapshot = await self._read()
            except IoExchangeError as err:
                raise UpdateFailed(str(err)) from err
            if self._closed:
                raise UpdateFailed("output owner is closed")
            return snapshot

    async def _fresh(self, channel: int, kind: type[_OutputT]) -> _OutputT:
        if self._closed:
            raise IoRejectedError("output owner is closed")
        try:
            snapshot = await self._read()
        except IoExchangeError as err:
            if not self._closed:
                self.async_set_update_error(err)
            raise
        self._publish(snapshot)
        output = snapshot.output(channel)
        if not isinstance(output, kind):
            label = _KIND_LABELS[kind]
            raise IoRejectedError(f"output {channel + 1} is not a {label} output")
        return output

    async def _read(self) -> IoSnapshot:
        try:
            report, names = await self._client.query_status(
                _QUERY, broadcast_prefix=_NAMES_PREFIX
            )
            outputs = parse_io_report(report)
        except (ConsoleProtocolError, IoParseError, OSError, TimeoutError) as err:
            raise IoExchangeError("output read failed") from err
        hints = None if names is None else parse_io_names(names)
        return IoSnapshot(tuple(self._named(item, hints) for item in outputs))

    def _named(self, item: RelayOutput, hints: dict[int, str] | None) -> RelayOutput:
        if hints is not None and item.channel in hints:
            return replace(item, name=hints[item.channel] or None)
        previous = None if self.data is None else self.data.output(item.channel)
        return replace(item, name=None if previous is None else previous.name)

    async def _send(self, payload: bytes) -> None:
        try:
            await self._client.send_control(payload)
        except asyncio.CancelledError:
            self._uncertain("trigger submission was cancelled and may have arrived")
            raise
        except ConsoleSendUncertainError as err:
            raise self._uncertain(
                "trigger may have reached the console and was not sent again", err
            ) from err
        except (ConsoleProtocolError, OSError, TimeoutError) as err:
            raise IoExchangeError("trigger was not sent") from err

    def _uncertain(
        self, message: str, err: Exception | None = None
    ) -> IoUncertainError:
        uncertain = IoUncertainError(message)
        if not self._closed:
            self.async_set_update_error(uncertain if err is None else err)
        return uncertain

    def _publish(self, snapshot: IoSnapshot) -> None:
        if not self._closed:
            self.async_set_updated_data(snapshot)


class IoOutputEntity(CoordinatorEntity[IoOutputsCoordinator]):
    """One channel, available while the latest report shows it as this kind."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IoOutputsCoordinator,
        kind: type[RelayOutput],
        unique_id: str,
        device_info: DeviceInfo,
        channel: int,
    ) -> None:
        super().__init__(coordinator)
        self._kind: type[RelayOutput] = kind
        self._channel = channel
        self._attr_unique_id = f"{unique_id}_output_{channel + 1}"
        self._attr_device_info = device_info

    @property
    def name(self) -> str:
        output = self._reported()
        if output is not None and output.name:
            return output.name
        return f"Output {self._channel + 1}"

    @property
    def available(self) -> bool:
        return super().available and self._current() is not None

    def _current(self) -> RelayOutput | None:
        output = self._reported()
        return output if isinstance(output, self._kind) else None

    def _reported(self) -> RelayOutput | None:
        data: IoSnapshot | None = self.coordinator.data
        return None if data is None else data.output(self._channel)

    async def _act(self, action: Awaitable[None]) -> None:
        try:
            await action
        except IoOutputError as err:
            raise HomeAssistantError(str(err)) from err


def async_install_io_outputs(
    hass: HomeAssistant,
    entry: ConfigEntry,
    airtouch: AirTouch,
    *,
    port: int = DEFAULT_PORT_NUMBER,
    deadline: float = DEFAULT_DEADLINE,
) -> IoOutputsCoordinator | None:
    """Install one AirTouch 5 output owner without reading the console.

    A second install is rejected. Reload installs a new owner after release.
    """
    if airtouch.model != AirTouchModel.AIRTOUCH_5:
        return None
    owners = _owners(hass)
    if entry.entry_id in owners:
        raise RuntimeError("output owner is already installed")
    coordinator = IoOutputsCoordinator(
        hass, entry, airtouch, port=port, deadline=deadline
    )
    owners[entry.entry_id] = coordinator
    return coordinator


def get_io_outputs(hass: HomeAssistant, entry_id: str) -> IoOutputsCoordinator:
    """Return the output owner installed for this config entry."""
    stored = hass.data.get(_OWNERS_KEY)
    if isinstance(stored, dict):
        coordinator = stored.get(entry_id)
        if isinstance(coordinator, IoOutputsCoordinator):
            return coordinator
    raise RuntimeError("output owner is not installed")


async def async_release_io_outputs(hass: HomeAssistant, entry_id: str) -> None:
    """Stop and forget this entry's output owner. A missing owner is left absent."""
    stored = hass.data.get(_OWNERS_KEY)
    if not isinstance(stored, dict):
        return
    owners = cast("dict[str, IoOutputsCoordinator]", stored)
    coordinator = owners.pop(entry_id, None)
    if not owners:
        hass.data.pop(_OWNERS_KEY, None)
    if isinstance(coordinator, IoOutputsCoordinator):
        await coordinator.async_shutdown()


def _owners(hass: HomeAssistant) -> dict[str, IoOutputsCoordinator]:
    stored = hass.data.get(_OWNERS_KEY)
    if stored is None:
        created: dict[str, IoOutputsCoordinator] = {}
        hass.data[_OWNERS_KEY] = created
        return created
    if not isinstance(stored, dict):
        raise TypeError("output owner map is invalid")
    return cast("dict[str, IoOutputsCoordinator]", stored)


def _decode_output(
    channel: int, parameter: int, output_type: int, closed_bits: int
) -> RelayOutput | None:
    if output_type == _TOGGLE_TYPE:
        return ToggleOutput(channel, None, bool(closed_bits >> channel & 1))
    if output_type == _PULSE_TYPE:
        return PulseOutput(channel, None, _pulse_width(parameter))
    if output_type != _UNUSED_TYPE:
        _LOGGER.debug("Ignoring output %s type %s", channel + 1, output_type)
    return None


def _parse_names(body: bytes) -> dict[int, str]:
    if len(body) < _NAME_HEADER:
        raise IoParseError("truncated header")
    name_length, count, stride = body[:_NAME_HEADER]
    if stride < _NAME_RECORD_HEADER + name_length:
        raise IoParseError("stride shorter than a record")
    if len(body) != _NAME_HEADER + count * stride:
        raise IoParseError("length mismatch")
    names: dict[int, str] = {}
    for index in range(count):
        offset = _NAME_HEADER + index * stride
        kind, number = body[offset : offset + 2]
        if kind == _INPUT_KIND:
            continue
        if kind != _OUTPUT_KIND or number >= _OUTPUT_COUNT or number in names:
            raise IoParseError("invalid record")
        name_start = offset + _NAME_RECORD_HEADER
        raw = body[name_start : name_start + name_length].split(b"\x00", 1)[0]
        names[number] = raw.decode("utf-8", errors="replace")
    return names


def _pulse_width(parameter: int) -> int:
    steps = parameter & _PARAMETER_MASK
    return steps * _PULSE_STEP_MS if steps in _PULSE_STEPS else _PULSE_DEFAULT_MS


def _trigger(channel: int, action: int) -> bytes:
    actions = bytearray(_OUTPUT_COUNT)
    actions[channel] = action
    return _SUBHEADER.pack(_TRIGGER_ID, _OUTPUT_COUNT, 0, 0) + actions
