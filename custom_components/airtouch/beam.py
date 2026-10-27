"""Beam wireless air conditioners paired with an AirTouch 5 console.

FF 21 reports every paired Beam. FF 22 controls one and FF 23 sets its
timers. The console sends no reply to either write, so returning from a
write means the frame was submitted, not that the Beam acted on it. The
normal poll is the only report of what the Beam later did.

A Beam is identified by the exact bytes of its 64-byte ID slot before the
first NUL. Each write reads FF 21 again first, checks the request against
that report, and is sent once. Writes are never retried or skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from dataclasses import dataclass
from datetime import time, timedelta
from enum import IntEnum
from typing import TYPE_CHECKING, Final, NewType, cast

from homeassistant.config_entries import ConfigEntryState, current_entry
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
    from collections.abc import Callable

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

_OWNERS_KEY: Final = "airtouch_beam"
_UPDATE_INTERVAL: Final = timedelta(seconds=30)
_QUERY: Final = b"\xff\x21"
_CONTROL_HEADER: Final = b"\xff\x22\x01\x47"
_TIMER_HEADER: Final = b"\xff\x23\x01\x46"
_STATUS: Final = struct.Struct("!B64s16sBB4B2xBBBBH2xH2s2s")
_CONTROL: Final = struct.Struct("!B64sBBBB2x")
_TIMER: Final = struct.Struct("!B64sB4s")
_ID_SIZE: Final = 64
_ACTIVE: Final = 1
_OFF: Final = 0
_ON: Final = 1
_OFFLINE: Final = 0xFFFE
_AUTO_HEAT: Final = 8
_AUTO_COOL: Final = 9
_KEEP_POWER: Final = 0x00
_POWER_OFF: Final = 0x02
_POWER_ON: Final = 0x03
_KEEP_SELECTION: Final = 0x0F
_KEEP_SETPOINT: Final = 0xFF
_UNUSABLE_LIMITS: Final = (0x00, 0xFF)
_MIN_CELSIUS: Final = 10
_MAX_CELSIUS: Final = 35
_SETPOINT_SCALE: Final = 10
_TIMER_DISABLED: Final = 0x80
_HOUR_MASK: Final = 0x1F
_MINUTE_MASK: Final = 0x3F
_CLEARED_TIMER: Final = b"\x80\x3f"
_RELATIVE: Final = 1
_ABSOLUTE: Final = 2
_MAX_DELAY_MINUTES: Final = 24 * 60 - 1
_MAX_HOUR: Final = 23
_MAX_MINUTE: Final = 59
_REPORT_HEADER: Final = 2

_CLOSED: Final = "Beam owner is closed"
_UNCERTAIN: Final = "the Beam write may have reached the console and was not sent again"

BeamId = NewType("BeamId", bytes)


class BeamParseError(Exception):
    """A Beam report was truncated, trailing, or named a unit ambiguously."""


class BeamError(HomeAssistantError):
    """A Beam read or write failed."""


class BeamRefusedError(BeamError):
    """The write was refused before anything was sent."""


class BeamExchangeError(BeamError):
    """A read or connection failed before the write was sent."""


class BeamUncertainError(BeamError):
    """The write may have reached the console. It was not sent again."""


class BeamMode(IntEnum):
    """A mode that can be selected. Mask bit n advertises mode n."""

    AUTO = 0
    HEAT = 1
    DRY = 2
    FAN = 3
    COOL = 4


class BeamFan(IntEnum):
    """A fan speed that can be selected. Mask bit n advertises speed n."""

    AUTO = 0
    QUIET = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4
    POWERFUL = 5
    TURBO = 6


class TimerRole(IntEnum):
    """Which timer, valued as the native relative-timer selector."""

    OFF = 0
    ON = 1


@dataclass(frozen=True, slots=True)
class TimerPair:
    """The two raw bytes of one timer, kept exactly as reported."""

    raw: bytes

    @property
    def value(self) -> time | None:
        """The console-local time, or None when disabled or out of range."""
        hour_byte, minute_byte = self.raw
        hour, minute = hour_byte & _HOUR_MASK, minute_byte & _MINUTE_MASK
        if hour_byte & _TIMER_DISABLED or hour > _MAX_HOUR or minute > _MAX_MINUTE:
            return None
        return time(hour, minute)


@dataclass(frozen=True, slots=True)
class BeamUnit:
    """One active FF 21 record with its raw codes and the meanings derived from them.

    Limits are direct Celsius. The temperature word is kept but not shown,
    because the console's own writer and reader disagree about its offset.
    """

    beam_id: BeamId
    name: str
    mode_mask: int
    fan_mask: int
    cool_limits: tuple[int, int]
    heat_limits: tuple[int, int]
    power: int
    mode: int
    fan: int
    setpoint: int
    temperature: int
    error: int
    on_timer: TimerPair
    off_timer: TimerPair

    @property
    def is_on(self) -> bool | None:
        """Whether the Beam reports power on, or None for an unknown code."""
        return {_OFF: False, _ON: True}.get(self.power)

    @property
    def offline(self) -> bool:
        """Whether the console reports that the Beam stopped answering."""
        return self.error == _OFFLINE

    @property
    def displayed_mode(self) -> BeamMode | None:
        """The reported mode, with automatic heating or cooling shown as Auto."""
        if self.mode in (_AUTO_HEAT, _AUTO_COOL):
            return BeamMode.AUTO
        with contextlib.suppress(ValueError):
            return BeamMode(self.mode)
        return None

    @property
    def reported_fan(self) -> BeamFan | None:
        """The reported fan speed, even when the fan mask advertises none."""
        with contextlib.suppress(ValueError):
            return BeamFan(self.fan)
        return None

    @property
    def selectable_modes(self) -> tuple[BeamMode, ...]:
        return tuple(mode for mode in BeamMode if self.mode_mask >> mode & 1)

    @property
    def selectable_fans(self) -> tuple[BeamFan, ...]:
        return tuple(fan for fan in BeamFan if self.fan_mask >> fan & 1)

    @property
    def target_celsius(self) -> int:
        """The setpoint as the native reader decodes it."""
        return (self.setpoint + _MIN_CELSIUS * _SETPOINT_SCALE) // _SETPOINT_SCALE

    @property
    def current_limits(self) -> tuple[int, int] | None:
        return self.limits(self.mode)

    def limits(self, mode: int) -> tuple[int, int] | None:
        """Return the settable Celsius range in mode, or None if none is usable.

        Heat uses the heat pair, Cool, Dry and Fan the cool pair, and Auto the
        outer range of whichever pairs are usable.
        """
        cool, heat = _usable(self.cool_limits), _usable(self.heat_limits)
        if mode == BeamMode.HEAT:
            return heat
        if mode in (BeamMode.COOL, BeamMode.DRY, BeamMode.FAN):
            return cool
        if mode not in (BeamMode.AUTO, _AUTO_HEAT, _AUTO_COOL):
            return None
        pairs = [pair for pair in (cool, heat) if pair is not None]
        if not pairs:
            return None
        return min(low for low, _ in pairs), max(high for _, high in pairs)

    def timer(self, role: TimerRole) -> TimerPair:
        return self.on_timer if role == TimerRole.ON else self.off_timer


@dataclass(frozen=True, slots=True)
class BeamSnapshot:
    """The active Beams in one FF 21 report, in report order."""

    units: tuple[BeamUnit, ...]

    def unit(self, beam_id: BeamId) -> BeamUnit | None:
        return next((item for item in self.units if item.beam_id == beam_id), None)


@dataclass(frozen=True, slots=True)
class BeamControl:
    """Change the selected fields. None keeps the Beam's current value."""

    power: bool | None = None
    mode: BeamMode | None = None
    fan: BeamFan | None = None
    celsius: int | None = None

    def __post_init__(self) -> None:
        if self.celsius is not None and not (
            _MIN_CELSIUS <= self.celsius <= _MAX_CELSIUS
        ):
            raise BeamRefusedError("Beam temperature must be 10 to 35 °C")


@dataclass(frozen=True, slots=True)
class SetTimer:
    """Set one timer to a console-local minute, or clear it with None."""

    role: TimerRole
    at: time | None

    def __post_init__(self) -> None:
        if self.at is not None and (
            self.at.second or self.at.microsecond or self.at.tzinfo is not None
        ):
            raise BeamRefusedError(
                "Beam timers take a whole minute without seconds or a time zone"
            )


@dataclass(frozen=True, slots=True)
class DelayTimer:
    """Start one timer this many minutes after the console's own clock."""

    role: TimerRole
    minutes: int

    def __post_init__(self) -> None:
        if not 1 <= self.minutes <= _MAX_DELAY_MINUTES:
            raise BeamRefusedError("Beam timer delay must be 1 to 1439 whole minutes")


BeamWrite = BeamControl | SetTimer | DelayTimer


def parse_beam_report(body: bytes) -> BeamSnapshot:
    """Parse the body after the FF 21 prefix.

    An empty report is one zero byte. Otherwise a count and a stride of at
    least 103 bytes precede the records. Records not marked active are
    skipped. Truncation, trailing bytes, and an empty or repeated active ID
    fail the whole report.
    """
    if body == b"\x00":
        return BeamSnapshot(())
    if len(body) < _REPORT_HEADER:
        raise BeamParseError("Beam report header is truncated")
    count, stride = body[0], body[1]
    if count == 0 or stride < _STATUS.size:
        raise BeamParseError("Beam report header is invalid")
    if len(body) != _REPORT_HEADER + count * stride:
        raise BeamParseError("Beam report length does not match its header")
    units: list[BeamUnit] = []
    for offset in range(_REPORT_HEADER, len(body), stride):
        record = _parse_record(body[offset : offset + stride])
        if record is not None:
            units.append(record)
    identities = [item.beam_id for item in units]
    if len(set(identities)) != len(identities):
        raise BeamParseError("Beam report repeats an ID")
    return BeamSnapshot(tuple(units))


def encode_write(unit: BeamUnit, write: BeamWrite) -> bytes:
    """Return the FF 22 or FF 23 payload for write after checking it against unit.

    unit must come from a fresh report. An absolute timer keeps the other
    timer's raw bytes from that report.
    """
    beam_id = unit.beam_id.ljust(_ID_SIZE, b"\x00")
    if isinstance(write, BeamControl):
        _check_control(unit, write)
        power = {None: _KEEP_POWER, False: _POWER_OFF, True: _POWER_ON}[write.power]
        return _CONTROL_HEADER + _CONTROL.pack(
            _ACTIVE,
            beam_id,
            power,
            _KEEP_SELECTION if write.mode is None else write.mode,
            _KEEP_SELECTION if write.fan is None else write.fan,
            _KEEP_SETPOINT
            if write.celsius is None
            else (write.celsius - _MIN_CELSIUS) * _SETPOINT_SCALE,
        )
    if isinstance(write, DelayTimer):
        hours, minutes = divmod(write.minutes, 60)
        body = bytes((write.role, hours, minutes, 0))
        return _TIMER_HEADER + _TIMER.pack(_ACTIVE, beam_id, _RELATIVE, body)
    pair = (
        _CLEARED_TIMER if write.at is None else bytes((write.at.hour, write.at.minute))
    )
    on = pair if write.role == TimerRole.ON else unit.on_timer.raw
    off = pair if write.role == TimerRole.OFF else unit.off_timer.raw
    return _TIMER_HEADER + _TIMER.pack(_ACTIVE, beam_id, _ABSOLUTE, on + off)


def _check_control(unit: BeamUnit, control: BeamControl) -> None:
    if control.mode is not None and control.mode not in unit.selectable_modes:
        raise BeamRefusedError("the Beam does not report that mode")
    if control.fan is not None and control.fan not in unit.selectable_fans:
        raise BeamRefusedError("the Beam does not report that fan speed")
    if control.celsius is None:
        return
    limits = unit.limits(unit.mode if control.mode is None else control.mode)
    if limits is None:
        raise BeamRefusedError("the Beam reports no usable temperature range")
    if not limits[0] <= control.celsius <= limits[1]:
        raise BeamRefusedError(
            f"Beam temperature must be {limits[0]} to {limits[1]} °C in that mode"
        )


def _parse_record(record: bytes) -> BeamUnit | None:
    (
        presence,
        id_slot,
        name_slot,
        mode_mask,
        fan_mask,
        cool_min,
        cool_max,
        heat_min,
        heat_max,
        power,
        mode,
        fan,
        setpoint,
        temperature,
        error,
        on_timer,
        off_timer,
    ) = _STATUS.unpack_from(record)
    if presence != _ACTIVE:
        return None
    beam_id = BeamId(id_slot.split(b"\x00", 1)[0])
    if not beam_id:
        raise BeamParseError("Beam report has an empty ID")
    return BeamUnit(
        beam_id=beam_id,
        name=name_slot.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
        mode_mask=mode_mask,
        fan_mask=fan_mask,
        cool_limits=(cool_min, cool_max),
        heat_limits=(heat_min, heat_max),
        power=power,
        mode=mode,
        fan=fan,
        setpoint=setpoint,
        temperature=temperature,
        error=error,
        on_timer=TimerPair(on_timer),
        off_timer=TimerPair(off_timer),
    )


def _usable(pair: tuple[int, int]) -> tuple[int, int] | None:
    low, high = pair
    if (
        low in _UNUSABLE_LIMITS
        or high in _UNUSABLE_LIMITS
        or low > high
        or high < _MIN_CELSIUS
        or low > _MAX_CELSIUS
    ):
        return None
    return max(low, _MIN_CELSIUS), min(high, _MAX_CELSIUS)


class BeamCoordinator(DataUpdateCoordinator[BeamSnapshot]):
    """Own one console's Beams for the climate and time platforms.

    One lock serializes polls and writes. A write reads FF 21, publishes it,
    checks the request against it, and sends one frame. Shutdown refuses new
    work, cancels only an owned read, and waits for an admitted send.
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
        self._entry = entry
        self._client = ConsoleClient(airtouch.host, port, deadline=deadline)
        self._lock = asyncio.Lock()
        self._read: asyncio.Task[bytes] | None = None
        self._closed = False
        self._discovery: list[Callable[[], None]] = []
        token = current_entry.set(entry)
        try:
            super().__init__(
                hass, _LOGGER, name="AirTouch Beams", update_interval=_UPDATE_INTERVAL
            )
        finally:
            current_entry.reset(token)

    @property
    def closed(self) -> bool:
        """Whether shutdown has begun."""
        return self._closed

    @callback
    def async_add_discovery(
        self, add: Callable[[list[BeamUnit]], None]
    ) -> Callable[[], None]:
        """Pass add each Beam the first time a report shows it.

        Units are added only while the entry is loading or loaded, because a
        failed unload can leave a platform reset. The listener also keeps
        polling alive with no Beams. The remover can be called again.
        """
        seen: set[BeamId] = set()

        @callback
        def discover() -> None:
            if (
                self._closed
                or not self.last_update_success
                or self.data is None
                or self._entry.state
                not in (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS)
            ):
                return
            new = [item for item in self.data.units if item.beam_id not in seen]
            if new:
                seen.update(item.beam_id for item in new)
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

    async def async_write(self, beam_id: BeamId, write: BeamWrite) -> None:
        """Check write against a fresh report and send it once.

        BeamRefusedError and BeamExchangeError mean nothing was sent.
        BeamUncertainError means it may have arrived. Returning means only
        that the frame was submitted.
        """
        async with self._lock:
            payload = encode_write(await self._fresh(beam_id), write)
            try:
                await self._client.send_extended(payload)
            except ConsoleSendUncertainError as err:
                raise BeamUncertainError(_UNCERTAIN) from err
            except (OSError, TimeoutError) as err:
                raise BeamExchangeError("the Beam write was not sent") from err

    async def async_shutdown(self) -> None:
        """Refuse new work, detach discovery, and drain an admitted send."""
        self._closed = True
        for remove in self._discovery:
            remove()
        self._discovery.clear()
        await super().async_shutdown()
        if self._read is not None:
            self._read.cancel()
        async with self._lock:
            pass
        # A drained refresh reschedules itself while entity listeners remain.
        await super().async_shutdown()

    async def _async_update_data(self) -> BeamSnapshot:
        async with self._lock:
            try:
                snapshot = await self._report()
            except BeamExchangeError as err:
                raise UpdateFailed(str(err)) from err
            if self._closed:
                raise UpdateFailed(_CLOSED)
            return snapshot

    async def _fresh(self, beam_id: BeamId) -> BeamUnit:
        try:
            snapshot = await self._report()
        except BeamExchangeError as err:
            if self._closed:
                raise BeamRefusedError(_CLOSED) from err
            self.async_set_update_error(err)
            raise
        if self._closed:
            raise BeamRefusedError(_CLOSED)
        self.async_set_updated_data(snapshot)
        unit = snapshot.unit(beam_id)
        if unit is None:
            raise BeamRefusedError("the Beam is not reported by the console")
        if unit.offline:
            raise BeamRefusedError("the console reports the Beam offline")
        return unit

    async def _report(self) -> BeamSnapshot:
        if self._closed:
            raise BeamExchangeError(_CLOSED)
        read = asyncio.create_task(self._client.request(_QUERY))
        self._read = read
        try:
            body = await read
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if not self._closed or (caller is not None and caller.cancelling()):
                raise
            raise BeamExchangeError(_CLOSED) from None
        except (ConsoleProtocolError, OSError, TimeoutError) as err:
            raise BeamExchangeError("the Beam report could not be read") from err
        finally:
            self._read = None
        try:
            return parse_beam_report(body)
        except BeamParseError as err:
            raise BeamExchangeError("the Beam report was invalid") from err


class BeamEntity(CoordinatorEntity[BeamCoordinator]):
    """An entity for one Beam, available while the latest report shows it online."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BeamCoordinator,
        beam_id: BeamId,
        unique_id: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._beam_id = beam_id
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        unit = self._unit
        return (
            super().available
            and not self.coordinator.closed
            and unit is not None
            and not unit.offline
        )

    @property
    def _unit(self) -> BeamUnit | None:
        data: BeamSnapshot | None = self.coordinator.data
        return None if data is None else data.unit(self._beam_id)

    async def _write(self, write: BeamWrite) -> None:
        await self.coordinator.async_write(self._beam_id, write)


def async_install_beam(
    hass: HomeAssistant,
    entry: ConfigEntry,
    airtouch: AirTouch,
    *,
    port: int = DEFAULT_PORT_NUMBER,
    deadline: float = DEFAULT_DEADLINE,
) -> BeamCoordinator | None:
    """Install one AirTouch 5 Beam owner without reading the console.

    A second install is rejected. Reload installs a new owner after release.
    """
    if airtouch.model != AirTouchModel.AIRTOUCH_5:
        return None
    owners = _owners(hass)
    if entry.entry_id in owners:
        raise RuntimeError("Beam owner is already installed")
    coordinator = BeamCoordinator(hass, entry, airtouch, port=port, deadline=deadline)
    owners[entry.entry_id] = coordinator
    return coordinator


def get_beam(hass: HomeAssistant, entry_id: str) -> BeamCoordinator:
    """Return the Beam owner installed for this config entry."""
    stored = hass.data.get(_OWNERS_KEY)
    if isinstance(stored, dict):
        coordinator = stored.get(entry_id)
        if isinstance(coordinator, BeamCoordinator):
            return coordinator
    raise RuntimeError("Beam owner is not installed")


async def async_release_beam(hass: HomeAssistant, entry_id: str) -> None:
    """Stop and forget this entry's Beam owner. A missing owner is left absent."""
    stored = hass.data.get(_OWNERS_KEY)
    if not isinstance(stored, dict):
        return
    owners = cast("dict[str, BeamCoordinator]", stored)
    coordinator = owners.pop(entry_id, None)
    if not owners:
        hass.data.pop(_OWNERS_KEY, None)
    if isinstance(coordinator, BeamCoordinator):
        await coordinator.async_shutdown()


def _owners(hass: HomeAssistant) -> dict[str, BeamCoordinator]:
    stored = hass.data.get(_OWNERS_KEY)
    if stored is None:
        created: dict[str, BeamCoordinator] = {}
        hass.data[_OWNERS_KEY] = created
        return created
    if not isinstance(stored, dict):
        raise TypeError("Beam owner map is invalid")
    return cast("dict[str, BeamCoordinator]", stored)
