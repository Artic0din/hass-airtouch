"""Saved AirTouch 5 weekly programs.

One FF 41 query is answered by an FF 41 base report and an FF 45 extension.
The base report holds each program's times, days, and air conditioner and zone
settings. The extension holds Beam settings, joined to programs by ID. The
console runs programs. These values are its saved configuration, not a
prediction of what it will do next.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import time, timedelta
from enum import IntEnum
from typing import TYPE_CHECKING, Final, cast

from homeassistant.config_entries import current_entry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pyairtouch import AirTouchModel
from pyairtouch.at5.api import DEFAULT_PORT_NUMBER

if TYPE_CHECKING:
    from collections.abc import Hashable, Sequence

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from pyairtouch import AirTouch

from .console import DEFAULT_DEADLINE, ConsoleClient, ConsoleProtocolError

_LOGGER = logging.getLogger(__name__)

_QUERY: Final = b"\xff\x41"
_EXTENSION_PREFIX: Final = b"\xff\x45"
_OWNERS_KEY: Final = "airtouch_programs"
_UPDATE_INTERVAL: Final = timedelta(minutes=15)
_AGE_SIZE: Final = 8
_BASE_HEADER_SIZE: Final = 16
_PROGRAM_FIELDS: Final = 7
_AC_FIELDS: Final = 3
_ZONE_FIELDS: Final = 7
_BEAM_ID_SIZE: Final = 64
_BEAM_FIELDS: Final = 4
_NO_END_HOUR: Final = 0xFF
_HOURS: Final = 24
_MINUTES: Final = 60
_ID_MASK: Final = 0x7F
_INDEX_MASK: Final = 0x0F
_NIBBLE: Final = 4
_TEMPERATURE_OFFSET: Final = 10
_TEMPERATURE_SCALE: Final = 10
_ENABLED: Final = 0x80
_EXCLUDE_HOLIDAYS: Final = 0x80
_AFFECTS_ACS: Final = 0x80
_AFFECTS_ZONES: Final = 0x40
_AC_POWER: Final = 0x80
_AC_ADVANCED: Final = 0x40
_ZONE_POWER: Final = 0x80
_ZONE_TURBO: Final = 0x40
_ZONE_TEMPERATURE: Final = 0x04
_ZONE_ADVANCED: Final = 0x02
_ZONE_SEPARATE_TIMES: Final = 0x01
_BEAM_ADVANCED: Final = 0x80
_BEAM_POWER: Final = 0x01

_MODES: Final = {0: "auto", 1: "heat", 2: "dry", 3: "fan", 4: "cool"}
_FANS: Final = {
    0: "auto",
    1: "quiet",
    2: "low",
    3: "medium",
    4: "high",
    5: "powerful",
    6: "turbo",
}
# The ducted decoder reads 8..14 as intelligent. The Beam writer encodes
# intelligent as 8 but its loader indexes the fan enum, so only 0..6 agree.
_DUCTED_FANS: Final = {**_FANS, **dict.fromkeys(range(8, 15), "intelligent")}


class ProgramParseError(Exception):
    """The program reports cannot form one complete, unambiguous schedule."""


class Weekday(IntEnum):
    """A day of the week, numbered by its bit in the program record."""

    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6


@dataclass(frozen=True)
class ProgramTimes:
    """A saved start time and an end time, which is None when disabled."""

    start: time
    end: time | None


@dataclass(frozen=True)
class Celsius:
    """A zone setpoint in degrees Celsius."""

    value: int


@dataclass(frozen=True)
class DamperPercent:
    """A zone damper opening percentage."""

    value: int


@dataclass(frozen=True)
class AcSetting:
    """One air conditioner's saved program setting.

    advanced is the stored bit. The console applies it only while power is on.
    """

    index: int
    power: bool
    advanced: bool
    mode_code: int
    fan_code: int
    setpoint_c: int

    @property
    def mode(self) -> str | None:
        """The mode label, or None for a code with no known meaning."""
        return _MODES.get(self.mode_code)

    @property
    def fan(self) -> str | None:
        """The fan label, or None for a code with no known meaning."""
        return _DUCTED_FANS.get(self.fan_code)


@dataclass(frozen=True)
class ZoneSetting:
    """One zone's saved program setting, including its own saved window.

    The window applies only when the zone is on and separate_times is set.
    It is kept either way because it is part of the saved configuration.
    """

    index: int
    power: bool
    turbo: bool
    advanced: bool
    separate_times: bool
    target: Celsius | DamperPercent
    times: ProgramTimes


@dataclass(frozen=True)
class BeamSetting:
    """One Beam's saved program setting. The setpoint is direct Celsius."""

    beam_id: str
    power: bool
    advanced: bool
    mode_code: int
    fan_code: int
    setpoint_c: int

    @property
    def mode(self) -> str | None:
        """The mode label, or None for a code with no known meaning."""
        return _MODES.get(self.mode_code)

    @property
    def fan(self) -> str | None:
        """The fan label for codes the Beam writer and loader agree on."""
        return _FANS.get(self.fan_code)


@dataclass(frozen=True)
class Program:
    """One saved program.

    beams is None when the extension did not report this program. A Beam
    missing from a reported tuple was not reported, which does not mean off.
    """

    program_id: int
    name: str
    enabled: bool
    times: ProgramTimes
    days: frozenset[Weekday]
    exclude_holidays: bool
    affects_acs: bool
    affects_zones: bool
    acs: tuple[AcSetting, ...]
    zones: tuple[ZoneSetting, ...]
    beams: tuple[BeamSetting, ...] | None


@dataclass(frozen=True)
class ProgramSchedule:
    """Programs in report order and the Beam IDs the extension named."""

    programs: tuple[Program, ...]
    beam_ids: tuple[str, ...]

    def attributes(self) -> dict[str, object]:
        """Return the state attributes for this schedule."""
        return {
            "programs": [_program_attributes(program) for program in self.programs],
            "reported_beam_ids": list(self.beam_ids),
        }


def parse_programs(base_body: bytes, extension_body: bytes) -> ProgramSchedule:
    """Parse and join the bodies that follow the FF 41 and FF 45 prefixes.

    Nothing is returned unless both bodies are complete and agree.
    """
    programs = _parse_base(base_body)
    beam_ids, assignments = _parse_extension(extension_body)
    if assignments is not None:
        if set(assignments) != {program.program_id for program in programs}:
            raise ProgramParseError("program reports disagree")
        programs = [
            replace(program, beams=assignments[program.program_id])
            for program in programs
        ]
    return ProgramSchedule(tuple(programs), beam_ids)


class _Cursor:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def take(self, size: int) -> bytes:
        end = self._offset + size
        if end > len(self._data):
            raise ProgramParseError("report is truncated")
        chunk = self._data[self._offset : end]
        self._offset = end
        return chunk

    def byte(self) -> int:
        return self.take(1)[0]

    def finish(self) -> None:
        if self._offset != len(self._data):
            raise ProgramParseError("report has trailing bytes")


def _parse_base(body: bytes) -> list[Program]:
    cursor = _Cursor(body)
    cursor.take(_AGE_SIZE)
    count, capacity, header, name_size, ac_count, ac_size, zone_count, zone_size = (
        cursor.take(_BASE_HEADER_SIZE - _AGE_SIZE)
    )
    if count > capacity:
        raise ProgramParseError("more programs than capacity")
    if count == 0:
        # The writer leaves the unused record sizes at 0xFF.
        cursor.finish()
        return []
    if (
        header < _PROGRAM_FIELDS + name_size
        or (ac_count and ac_size < _AC_FIELDS)
        or (zone_count and zone_size < _ZONE_FIELDS)
    ):
        raise ProgramParseError("record sizes are too small")
    record_size = header + ac_count * ac_size + zone_count * zone_size
    if len(body) != _BASE_HEADER_SIZE + count * record_size:
        raise ProgramParseError("length does not match the record sizes")
    programs: dict[int, Program] = {}
    for _ in range(count):
        record = cursor.take(header)
        acs = [cursor.take(ac_size) for _ in range(ac_count)]
        zones = [cursor.take(zone_size) for _ in range(zone_count)]
        program_id = record[0] & _ID_MASK
        if program_id == 0:
            continue
        if program_id in programs:
            raise ProgramParseError("duplicate program")
        programs[program_id] = _program(record, name_size, acs, zones)
    return list(programs.values())


def _program(
    record: bytes, name_size: int, acs: list[bytes], zones: list[bytes]
) -> Program:
    days = record[5]
    affects = record[6]
    ac_settings = tuple(_ac(item) for item in acs)
    zone_settings = tuple(_zone(item) for item in zones)
    _require_unique([setting.index for setting in ac_settings], "air conditioner")
    _require_unique([setting.index for setting in zone_settings], "zone")
    return Program(
        program_id=record[0] & _ID_MASK,
        name=_text(record[_PROGRAM_FIELDS : _PROGRAM_FIELDS + name_size]),
        enabled=bool(record[0] & _ENABLED),
        times=_times(record[1:5]),
        days=frozenset(day for day in Weekday if days >> day & 1),
        exclude_holidays=bool(days & _EXCLUDE_HOLIDAYS),
        affects_acs=bool(affects & _AFFECTS_ACS),
        affects_zones=bool(affects & _AFFECTS_ZONES),
        acs=ac_settings,
        zones=zone_settings,
        beams=None,
    )


def _ac(record: bytes) -> AcSetting:
    flags, codes, setpoint = record[:_AC_FIELDS]
    return AcSetting(
        index=flags & _INDEX_MASK,
        power=bool(flags & _AC_POWER),
        advanced=bool(flags & _AC_ADVANCED),
        mode_code=codes >> _NIBBLE,
        fan_code=codes & _INDEX_MASK,
        setpoint_c=_ducted_celsius(setpoint),
    )


def _zone(record: bytes) -> ZoneSetting:
    index, flags, target = record[:3]
    return ZoneSetting(
        index=index & _INDEX_MASK,
        power=bool(flags & _ZONE_POWER),
        turbo=bool(flags & _ZONE_TURBO),
        advanced=bool(flags & _ZONE_ADVANCED),
        separate_times=bool(flags & _ZONE_SEPARATE_TIMES),
        target=(
            Celsius(_ducted_celsius(target))
            if flags & _ZONE_TEMPERATURE
            else DamperPercent(target)
        ),
        times=_times(record[3:_ZONE_FIELDS]),
    )


def _parse_extension(
    body: bytes,
) -> tuple[tuple[str, ...], dict[int, tuple[BeamSetting, ...]] | None]:
    """Return the named Beam IDs and each program's Beams.

    The assignments are None when the extension reports no program slots,
    which is how the writer answers when its first program slot is empty.
    """
    cursor = _Cursor(body)
    cursor.take(_AGE_SIZE)
    dictionary = [_beam_id(cursor) for _ in range(cursor.byte())]
    named = [beam_id for beam_id in dictionary if beam_id is not None]
    _require_unique(named, "Beam ID")
    slots = cursor.byte()
    assignments: dict[int, tuple[BeamSetting, ...]] = {}
    for _ in range(slots):
        header = cursor.byte()
        if header == 0:
            continue
        program_id = cursor.take(header)[0]
        beams = tuple(
            beam
            for beam in (_beam(cursor, dictionary) for _ in range(cursor.byte()))
            if beam is not None
        )
        _require_unique([beam.beam_id for beam in beams], "Beam assignment")
        _skip_groups(cursor)
        if program_id == 0:
            continue
        if program_id in assignments:
            raise ProgramParseError("duplicate extension program")
        assignments[program_id] = beams
    cursor.finish()
    return tuple(named), assignments if slots else None


def _beam_id(cursor: _Cursor) -> str | None:
    size = cursor.byte()
    if size == 0:
        return None
    if size < _BEAM_ID_SIZE:
        raise ProgramParseError("Beam ID slot is too small")
    beam_id = _text(cursor.take(size)[:_BEAM_ID_SIZE])
    if not beam_id:
        raise ProgramParseError("Beam ID is empty")
    return beam_id


def _beam(cursor: _Cursor, dictionary: list[str | None]) -> BeamSetting | None:
    size = cursor.byte()
    if size == 0:
        return None
    if size < _BEAM_FIELDS:
        raise ProgramParseError("Beam item is too small")
    index, flags, codes, setpoint = cursor.take(size)[:_BEAM_FIELDS]
    beam_id = dictionary[index] if index < len(dictionary) else None
    if beam_id is None:
        raise ProgramParseError("Beam item names no Beam ID")
    return BeamSetting(
        beam_id=beam_id,
        power=bool(flags & _BEAM_POWER),
        advanced=bool(flags & _BEAM_ADVANCED),
        mode_code=codes >> _NIBBLE,
        fan_code=codes & _INDEX_MASK,
        setpoint_c=setpoint,
    )


def _skip_groups(cursor: _Cursor) -> None:
    while count := cursor.byte():
        for _ in range(count):
            cursor.take(cursor.byte())


def _times(fields: bytes) -> ProgramTimes:
    start_hour, start_minute, end_hour, end_minute = fields
    return ProgramTimes(
        start=_clock(start_hour, start_minute),
        end=None if end_hour == _NO_END_HOUR else _clock(end_hour, end_minute),
    )


def _clock(hour: int, minute: int) -> time:
    if hour >= _HOURS or minute >= _MINUTES:
        raise ProgramParseError("invalid time")
    return time(hour, minute)


def _ducted_celsius(raw: int) -> int:
    return _TEMPERATURE_OFFSET + raw // _TEMPERATURE_SCALE


def _text(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def _require_unique(keys: Sequence[Hashable], kind: str) -> None:
    if len(set(keys)) != len(keys):
        raise ProgramParseError(f"duplicate {kind}")


def _program_attributes(program: Program) -> dict[str, object]:
    beams = program.beams
    return {
        "id": program.program_id,
        "name": program.name,
        "enabled": program.enabled,
        **_window(program.times),
        "days": [day.name.lower() for day in Weekday if day in program.days],
        "exclude_holidays": program.exclude_holidays,
        "affects_air_conditioners": program.affects_acs,
        "affects_zones": program.affects_zones,
        "air_conditioners": [
            {"index": setting.index, **_unit_attributes(setting)}
            for setting in program.acs
        ],
        "zones": [_zone_attributes(setting) for setting in program.zones],
        "beams": None
        if beams is None
        else [
            {"beam_id": setting.beam_id, **_unit_attributes(setting)}
            for setting in beams
        ],
    }


def _unit_attributes(setting: AcSetting | BeamSetting) -> dict[str, object]:
    return {
        "power": setting.power,
        "advanced": setting.advanced,
        "mode": setting.mode,
        "mode_code": setting.mode_code,
        "fan": setting.fan,
        "fan_code": setting.fan_code,
        "setpoint": setting.setpoint_c,
    }


def _zone_attributes(setting: ZoneSetting) -> dict[str, object]:
    target = setting.target
    return {
        "index": setting.index,
        "power": setting.power,
        "turbo": setting.turbo,
        "advanced": setting.advanced,
        "setpoint": target.value if isinstance(target, Celsius) else None,
        "open_percentage": target.value if isinstance(target, DamperPercent) else None,
        "separate_times": setting.separate_times,
        **_window(setting.times),
    }


def _window(times: ProgramTimes) -> dict[str, str | None]:
    return {
        "start": times.start.isoformat("minutes"),
        "end": None if times.end is None else times.end.isoformat("minutes"),
    }


class ProgramCoordinator(DataUpdateCoordinator[ProgramSchedule]):
    """Poll one console's saved programs on a short-lived connection.

    Reads run one at a time. Shutdown cancels only the owner's own read,
    waits for it, and keeps any later read from connecting or publishing.
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
        self._read_lock = asyncio.Lock()
        self._read: asyncio.Task[ProgramSchedule] | None = None
        self._closed = False
        token = current_entry.set(entry)
        try:
            super().__init__(
                hass,
                _LOGGER,
                name="AirTouch programs",
                update_interval=_UPDATE_INTERVAL,
            )
        finally:
            current_entry.reset(token)

    async def async_shutdown(self) -> None:
        """Stop polling, cancel the owner's read, and wait for it to end."""
        self._closed = True
        await super().async_shutdown()
        if self._read is not None:
            self._read.cancel()
        async with self._read_lock:
            pass
        # A drained refresh reschedules itself while listeners remain.
        await super().async_shutdown()

    async def _async_update_data(self) -> ProgramSchedule:
        async with self._read_lock:
            if self._closed:
                raise UpdateFailed("program owner is closed")
            read = asyncio.create_task(self._read_schedule())
            self._read = read
            try:
                schedule = await read
            except asyncio.CancelledError:
                caller = asyncio.current_task()
                if not self._closed or (caller is not None and caller.cancelling()):
                    raise
                raise UpdateFailed("program owner is closed") from None
            finally:
                self._read = None
            if self._closed:
                raise UpdateFailed("program owner is closed")
            return schedule

    async def _read_schedule(self) -> ProgramSchedule:
        try:
            base, extension = await self._client.request_with_extension(
                _QUERY, extension_prefix=_EXTENSION_PREFIX
            )
            return parse_programs(base, extension)
        except (ConsoleProtocolError, ProgramParseError, OSError, TimeoutError) as err:
            raise UpdateFailed("program read failed") from err


def async_install_programs(
    hass: HomeAssistant,
    entry: ConfigEntry,
    airtouch: AirTouch,
    *,
    port: int = DEFAULT_PORT_NUMBER,
    deadline: float = DEFAULT_DEADLINE,
) -> ProgramCoordinator | None:
    """Install one AirTouch 5 program owner without reading the console.

    A second install is rejected. Reload installs a new owner after release.
    """
    if airtouch.model != AirTouchModel.AIRTOUCH_5:
        return None
    owners = _owners(hass)
    if entry.entry_id in owners:
        raise RuntimeError("program owner is already installed")
    coordinator = ProgramCoordinator(
        hass, entry, airtouch, port=port, deadline=deadline
    )
    owners[entry.entry_id] = coordinator
    return coordinator


def get_programs(hass: HomeAssistant, entry_id: str) -> ProgramCoordinator:
    """Return the program owner installed for this config entry."""
    stored = hass.data.get(_OWNERS_KEY)
    if isinstance(stored, dict):
        coordinator = stored.get(entry_id)
        if isinstance(coordinator, ProgramCoordinator):
            return coordinator
    raise RuntimeError("program owner is not installed")


async def async_release_programs(hass: HomeAssistant, entry_id: str) -> None:
    """Stop and forget this entry's program owner. A missing owner is left absent."""
    stored = hass.data.get(_OWNERS_KEY)
    if not isinstance(stored, dict):
        return
    owners = cast("dict[str, ProgramCoordinator]", stored)
    coordinator = owners.pop(entry_id, None)
    if not owners:
        hass.data.pop(_OWNERS_KEY, None)
    if isinstance(coordinator, ProgramCoordinator):
        await coordinator.async_shutdown()


def _owners(hass: HomeAssistant) -> dict[str, ProgramCoordinator]:
    stored = hass.data.get(_OWNERS_KEY)
    if stored is None:
        created: dict[str, ProgramCoordinator] = {}
        hass.data[_OWNERS_KEY] = created
        return created
    if not isinstance(stored, dict):
        raise TypeError("program owner map is invalid")
    return cast("dict[str, ProgramCoordinator]", stored)
