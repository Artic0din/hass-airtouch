from __future__ import annotations

import asyncio
import contextlib
import json
import struct
import unittest
from datetime import time, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator, Sequence

    from homeassistant.core import EventStateChangedData, StateInfo

from homeassistant.components.recorder.db_schema import StateAttributes
from homeassistant.const import CONF_HOST, EVENT_STATE_CHANGED
from homeassistant.core import Event, State
from pyairtouch import AirTouchModel
from pyairtouch.at5.comms.hdr import At5Header, HeaderDecoder, HeaderEncoder
from pyairtouch.comms.crc16 import Crc16Modbus

from custom_components.airtouch import (
    async_setup_entry as async_setup_integration,
)
from custom_components.airtouch import (
    async_unload_entry as async_unload_integration,
)
from custom_components.airtouch import programs as programs_module
from custom_components.airtouch.console import DEFAULT_DEADLINE, ConsoleClient
from custom_components.airtouch.const import CONF_AIRTOUCH_MODEL, DOMAIN
from custom_components.airtouch.const import AirTouchModel as SelectedModel
from custom_components.airtouch.programs import (
    Celsius,
    DamperPercent,
    ProgramCoordinator,
    ProgramParseError,
    ProgramTimes,
    Weekday,
    async_install_programs,
    async_release_programs,
    get_programs,
    parse_programs,
)
from custom_components.airtouch.sensor import (
    ProgramsEntity,
    async_collect_program_entities,
)

_OUTER = b"\x55\x55\x55\xab"
_ENCODER = HeaderEncoder()
_DECODER = HeaderDecoder()
_CRC = Crc16Modbus()
_QUERY = bytes.fromhex("ff41")
_EXTENSION_PREFIX = bytes.fromhex("ff45")
_DEVICE_INFO: dict[str, object] = {
    "identifiers": {("airtouch", "hub")},
    "name": "Console",
}
_AGE = bytes.fromhex("0000000000001388")
_LATER_AGE = bytes.fromhex("00000000000a1b2c")
_EMPTY_BASE = _AGE + bytes.fromhex("0010ff1001ff04ff")
_EMPTY_EXTENSION = _AGE + bytes.fromhex("0000")
_BEAM_ID = b"BEAM-TEST-1"
_OTHER_BEAM_ID = b"BEAM-TEST-2"

# One program written as CommandBuilder.createProgramResponse lays it out:
# id 3 enabled, 06:30 to 08:00, Monday to Friday, affects both, then one AC
# (on, cool, low, 22 C) and two zones (21 C with no end, and 50 % from 07:00
# to 07:30 with separate times).
_POPULATED_BASE = _AGE + bytes.fromhex(
    "01 10 17 10 01 03 02 07"
    "83 06 1e 08 00 1f c0 4d6f726e696e67 000000000000000000"
    "80 42 78"
    "00 84 6e 00 00 ff 00"
    "01 81 32 07 00 07 1e"
)
# createProgramExResponse for that program: one 64-byte Beam ID, then a
# Beam on in heat with writer fan code 8 and a direct 22 C setpoint.
_POPULATED_EXTENSION = (
    _LATER_AGE
    + bytes.fromhex("01 40")
    + _BEAM_ID.ljust(64, b"\x00")
    + bytes.fromhex("01 01 03 01 04 00 01 18 16 00")
)
_POPULATED_ATTRIBUTES: dict[str, object] = {
    "programs": [
        {
            "id": 3,
            "name": "Morning",
            "enabled": True,
            "start": "06:30",
            "end": "08:00",
            "days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
            "exclude_holidays": False,
            "affects_air_conditioners": True,
            "affects_zones": True,
            "air_conditioners": [
                {
                    "index": 0,
                    "power": True,
                    "advanced": False,
                    "mode": "cool",
                    "mode_code": 4,
                    "fan": "low",
                    "fan_code": 2,
                    "setpoint": 22,
                }
            ],
            "zones": [
                {
                    "index": 0,
                    "power": True,
                    "turbo": False,
                    "advanced": False,
                    "setpoint": 21,
                    "open_percentage": None,
                    "separate_times": False,
                    "start": "00:00",
                    "end": None,
                },
                {
                    "index": 1,
                    "power": True,
                    "turbo": False,
                    "advanced": False,
                    "setpoint": None,
                    "open_percentage": 50,
                    "separate_times": True,
                    "start": "07:00",
                    "end": "07:30",
                },
            ],
            "beams": [
                {
                    "beam_id": "BEAM-TEST-1",
                    "power": True,
                    "advanced": False,
                    "mode": "heat",
                    "mode_code": 1,
                    "fan": None,
                    "fan_code": 8,
                    "setpoint": 22,
                }
            ],
        }
    ],
    "reported_beam_ids": ["BEAM-TEST-1"],
}


def _ducted(celsius: int) -> int:
    return (celsius - 10) * 10


def _record(  # noqa: PLR0913
    program_id: int,
    *,
    enabled: bool = True,
    times: bytes = b"\x06\x1e\x08\x00",
    days: int = 0x1F,
    affects: int = 0xC0,
    name: bytes = b"Program",
    name_size: int = 16,
    tail: bytes = b"",
) -> bytes:
    first = program_id | (0x80 if enabled else 0)
    return bytes((first, *times, days, affects)) + name.ljust(name_size, b"\x00") + tail


def _ac(
    index: int, *, flags: int = 0x80, codes: int = 0x42, celsius: int = 22
) -> bytes:
    return bytes((index | flags, codes, _ducted(celsius)))


def _zone(
    index: int, *, flags: int = 0x84, target: int = 110, times: bytes = b"\0\0\xff\0"
) -> bytes:
    return bytes((index, flags, target)) + times


def _base(  # noqa: PLR0913
    records: list[bytes],
    *,
    acs: int = 1,
    ac_size: int = 3,
    zones: int = 1,
    zone_size: int = 7,
    header: int = 23,
    name_size: int = 16,
    capacity: int = 16,
    age: bytes = _AGE,
) -> bytes:
    sizes = bytes((len(records), capacity, header, name_size, acs, ac_size))
    return age + sizes + bytes((zones, zone_size)) + b"".join(records)


def _program(record: bytes) -> bytes:
    return record + _ac(0) + _zone(0)


def _item(
    index: int, *, flags: int = 0x01, codes: int = 0x18, setpoint: int = 22
) -> bytes:
    return bytes((index, flags, codes, setpoint))


def _slot(
    program_id: int,
    items: Sequence[bytes | None],
    *,
    header_tail: bytes = b"",
    groups: bytes = b"\x00",
) -> bytes:
    header = bytes((program_id,)) + header_tail
    out = bytearray((len(header),)) + header + bytes((len(items),))
    for item in items:
        out += b"\x00" if item is None else bytes((len(item),)) + item
    return bytes(out) + groups


def _extension(
    dictionary: Sequence[bytes | None],
    slots: Sequence[bytes | None],
    *,
    id_size: int = 64,
    age: bytes = _LATER_AGE,
) -> bytes:
    out = bytearray(age) + bytes((len(dictionary),))
    for beam_id in dictionary:
        if beam_id is None:
            out.append(0)
        else:
            out += bytes((id_size,)) + beam_id.ljust(id_size, b"\x00")
    out.append(len(slots))
    for slot in slots:
        out += b"\x00" if slot is None else slot
    return bytes(out)


def _oracle_escape(inner: bytes) -> bytes:
    out = bytearray(inner[:4])
    run = 0
    for byte in inner[4:]:
        out.append(byte)
        if byte == 0x55:
            run += 1
            if run == 3:
                out.append(0)
                run = 0
        else:
            run = 0
    return bytes(out)


def _frame(packet_id: int, payload: bytes) -> bytes:
    header = At5Header(
        to_address=0xB0,
        from_address=0x90,
        packet_id=packet_id,
        message_id=0x1F,
        message_length=len(payload),
    )
    encoded = _ENCODER.encode(header)
    crc = _CRC.calculate(encoded.checksum_data + payload)
    escaped = _oracle_escape(encoded.header_bytes[10:] + payload + crc)
    return (
        _OUTER + b"\x00\x00" + struct.pack("!HH", len(escaped), len(escaped)) + escaped
    )


def _oracle_decode(frame: bytes) -> tuple[At5Header, bytes]:
    escaped = frame[10:]
    logical = bytearray(escaped[:4])
    run = 0
    index = 4
    while index < len(escaped):
        logical.append(escaped[index])
        index += 1
        if logical[-1] == 0x55:
            run += 1
            if run == 3:
                index += 1
                run = 0
        else:
            run = 0
    outer = _OUTER + b"\x00\x00" + struct.pack("!HH", len(logical), len(logical))
    decoded = _DECODER.decode(outer + bytes(logical)[: _DECODER.header_length - 10])
    decoded.assert_complete()
    rest = bytes(logical)[_DECODER.header_length - 10 :]
    return decoded.header, rest[: decoded.header.message_length]


async def _read_raw(reader: asyncio.StreamReader) -> bytes:
    outer = await reader.readexactly(10)
    length = struct.unpack_from("!H", outer, 6)[0]
    return outer + await reader.readexactly(length)


def _combined_unrecorded(entity: ProgramsEntity) -> frozenset[str]:
    # Home Assistant hands this set to the recorder in each state's info.
    return cast(
        "frozenset[str]", vars(type(entity))["_Entity__combined_unrecorded_attributes"]
    )


class _Entry:
    def __init__(self, entry_id: str = "entry") -> None:
        self.entry_id = entry_id
        self.title = "programs-test"
        self.domain = DOMAIN
        self.pref_disable_polling = False
        self.unloads: list[Callable[[], object]] = []
        self.scheduled: list[str] = []

    def async_on_unload(self, func: Callable[[], object]) -> None:
        self.unloads.append(func)

    def async_create_background_task(
        self,
        hass: object,
        target: Coroutine[Any, Any, object],
        *,
        name: str,
        eager_start: bool = True,
    ) -> None:
        del hass, eager_start
        self.scheduled.append(name)
        target.close()


class _Config:
    def __init__(self) -> None:
        self.time_zone = "UTC"
        self.units: object = None


class _Hass:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.config = _Config()
        self.data: dict[str, Any] = {}
        self.config_entries: Any = None

    @property
    def is_stopping(self) -> bool:
        return False


class _AirTouch:
    def __init__(self, model: AirTouchModel = AirTouchModel.AIRTOUCH_5) -> None:
        self.host = "127.0.0.1"
        self.model = model


class _Device:
    unique_id = "hub"

    def __init__(self) -> None:
        self.device_info = _DEVICE_INFO


class _Api(_AirTouch):
    def __init__(self, model: AirTouchModel = AirTouchModel.AIRTOUCH_5) -> None:
        super().__init__(model)
        self.shutdown_calls = 0
        self.events: list[str] = []

    async def init(self) -> bool:
        return True

    async def shutdown(self) -> None:
        self.events.append("api-shutdown")
        self.shutdown_calls += 1


class _RootEntry(_Entry):
    def __init__(
        self, entry_id: str, model: str = SelectedModel.AIRTOUCH_5.value
    ) -> None:
        super().__init__(entry_id)
        self.version = 2
        self.minor_version = 2
        self.unique_id = entry_id
        self.data = {CONF_HOST: "127.0.0.1", CONF_AIRTOUCH_MODEL: model}


class _ConfigEntries:
    def __init__(
        self,
        forward: Callable[[object, object], Coroutine[Any, Any, None]] | None,
        *,
        unload_ok: bool = True,
    ) -> None:
        self._forward = forward
        self.unload_ok = unload_ok

    async def async_forward_entry_setups(
        self, entry: object, platforms: object
    ) -> None:
        if self._forward is not None:
            await self._forward(entry, platforms)

    async def async_unload_platforms(self, entry: object, platforms: object) -> bool:
        del entry, platforms
        return self.unload_ok


class ProgramParserTests(unittest.TestCase):
    def test_live_shaped_empty_reports_have_no_programs(self) -> None:
        schedule = parse_programs(_EMPTY_BASE, _EMPTY_EXTENSION)
        self.assertEqual(schedule.programs, ())
        self.assertEqual(
            schedule.attributes(), {"programs": [], "reported_beam_ids": []}
        )

    def test_populated_reports_follow_the_writer(self) -> None:
        schedule = parse_programs(_POPULATED_BASE, _POPULATED_EXTENSION)
        self.assertEqual(schedule.attributes(), _POPULATED_ATTRIBUTES)
        program = schedule.programs[0]
        self.assertEqual(program.times, ProgramTimes(time(6, 30), time(8, 0)))
        self.assertEqual(program.zones[0].target, Celsius(21))
        self.assertEqual(program.zones[1].target, DamperPercent(50))
        self.assertNotEqual(Celsius(50), DamperPercent(50))

    def test_disabled_program_keeps_its_saved_settings(self) -> None:
        base = _base(
            [
                _record(9, enabled=False, days=0x80 | 0x41, affects=0x40, name=b"Off")
                + _ac(2, flags=0x40, codes=0x30, celsius=18)
                + _zone(3, flags=0x00, target=40, times=b"\x05\x00\x05\x0f")
            ]
        )
        (program,) = parse_programs(base, _EMPTY_EXTENSION).programs
        self.assertFalse(program.enabled)
        self.assertEqual(program.days, frozenset({Weekday.MONDAY, Weekday.SUNDAY}))
        self.assertTrue(program.exclude_holidays)
        self.assertFalse(program.affects_acs)
        self.assertTrue(program.affects_zones)
        self.assertIsNone(program.beams)
        attributes = cast(
            "list[dict[str, Any]]",
            parse_programs(base, _EMPTY_EXTENSION).attributes()["programs"],
        )[0]
        self.assertEqual(attributes["name"], "Off")
        self.assertEqual(attributes["days"], ["monday", "sunday"])
        self.assertEqual(
            attributes["air_conditioners"],
            [
                {
                    "index": 2,
                    "power": False,
                    "advanced": True,
                    "mode": "fan",
                    "mode_code": 3,
                    "fan": "auto",
                    "fan_code": 0,
                    "setpoint": 18,
                }
            ],
        )
        self.assertEqual(
            attributes["zones"],
            [
                {
                    "index": 3,
                    "power": False,
                    "turbo": False,
                    "advanced": False,
                    "setpoint": None,
                    "open_percentage": 40,
                    "separate_times": False,
                    "start": "05:00",
                    "end": "05:15",
                }
            ],
        )

    def test_weekdays_serialize_monday_first(self) -> None:
        for bits, expected in (
            (0x01, ["monday"]),
            (0x40, ["sunday"]),
            (0x7F, [day.name.lower() for day in Weekday]),
            (0x80, []),
        ):
            with self.subTest(bits=bits):
                base = _base([_program(_record(1, days=bits))])
                attributes = cast(
                    "list[dict[str, Any]]",
                    parse_programs(base, _EMPTY_EXTENSION).attributes()["programs"],
                )[0]
                self.assertEqual(attributes["days"], expected)
                self.assertEqual(attributes["exclude_holidays"], bits == 0x80)

    def test_end_hour_ff_alone_disables_the_end(self) -> None:
        base = _base(
            [
                _record(1, times=b"\x17\x3b\xff\x1e")
                + _ac(0)
                + _zone(0, times=b"\x00\x00\xff\xff")
            ]
        )
        (program,) = parse_programs(base, _EMPTY_EXTENSION).programs
        self.assertEqual(program.times, ProgramTimes(time(23, 59), None))
        self.assertEqual(program.zones[0].times, ProgramTimes(time(0, 0), None))

    def test_zone_flags_and_windows_are_kept_as_transmitted(self) -> None:
        base = _base(
            [
                _record(1)
                + _ac(0)
                + _zone(0, flags=0x40, target=30, times=b"\x01\x02\x03\x04")
                + _zone(1, flags=0x80 | 0x40 | 0x04 | 0x02, target=_ducted(25))
                + _zone(2, flags=0x80 | 0x01, target=0, times=b"\x10\x00\x11\x00")
            ],
            zones=3,
        )
        zones = parse_programs(base, _EMPTY_EXTENSION).programs[0].zones
        self.assertEqual(
            [
                (zone.power, zone.turbo, zone.advanced, zone.separate_times)
                for zone in zones
            ],
            [
                (False, True, False, False),
                (True, True, True, False),
                (True, False, False, True),
            ],
        )
        self.assertEqual(
            [zone.target for zone in zones],
            [DamperPercent(30), Celsius(25), DamperPercent(0)],
        )
        self.assertEqual(zones[0].times, ProgramTimes(time(1, 2), time(3, 4)))
        self.assertEqual(zones[2].times, ProgramTimes(time(16, 0), time(17, 0)))

    def test_codes_keep_raw_values_and_only_known_labels(self) -> None:
        ac_cases = [
            (0x08, "auto", "intelligent"),
            (0x0E, "auto", "intelligent"),
            (0x07, "auto", None),
            (0x0F, "auto", None),
            (0x46, "cool", "turbo"),
            (0x75, None, "powerful"),
            (0xF1, None, "quiet"),
        ]
        for codes, mode, fan in ac_cases:
            with self.subTest(ac=hex(codes)):
                base = _base([_record(1) + _ac(0, codes=codes) + _zone(0)])
                (setting,) = parse_programs(base, _EMPTY_EXTENSION).programs[0].acs
                self.assertEqual((setting.mode, setting.fan), (mode, fan))
                self.assertEqual(
                    (setting.mode_code, setting.fan_code), (codes >> 4, codes & 15)
                )
        beam_cases = [(0x06, "turbo"), (0x07, None), (0x08, None), (0x0F, None)]
        for codes, fan in beam_cases:
            with self.subTest(beam=hex(codes)):
                extension = _extension([_BEAM_ID], [_slot(1, [_item(0, codes=codes)])])
                beams = (
                    parse_programs(_base([_program(_record(1))]), extension)
                    .programs[0]
                    .beams
                )
                assert beams is not None
                self.assertEqual((beams[0].fan, beams[0].fan_code), (fan, codes))
                self.assertEqual(beams[0].mode, "auto")

    def test_ducted_and_beam_setpoints_use_their_own_wire_forms(self) -> None:
        base = _base([_record(1) + bytes.fromhex("80 42 78") + _zone(0)])
        beam = bytes.fromhex("00 01 18 16")
        extension = _extension([_BEAM_ID], [_slot(1, [beam])])
        program = parse_programs(base, extension).programs[0]
        self.assertEqual(program.acs[0].setpoint_c, 22)
        assert program.beams is not None
        self.assertEqual(program.beams[0].setpoint_c, 22)

    def test_programs_join_by_id_in_base_order(self) -> None:
        base = _base(
            [
                _program(_record(5, name=b"Evening")),
                _program(_record(2, name=b"Morning")),
            ]
        )
        extension = _extension(
            [_BEAM_ID, _OTHER_BEAM_ID],
            [
                _slot(2, [_item(0, setpoint=20), None]),
                _slot(5, [None, _item(1, flags=0x80, setpoint=26)]),
            ],
        )
        schedule = parse_programs(base, extension)
        self.assertEqual([p.program_id for p in schedule.programs], [5, 2])
        self.assertEqual(schedule.beam_ids, ("BEAM-TEST-1", "BEAM-TEST-2"))
        evening, morning = schedule.programs
        assert evening.beams is not None
        assert morning.beams is not None
        self.assertEqual(
            [(b.beam_id, b.power, b.advanced, b.setpoint_c) for b in evening.beams],
            [("BEAM-TEST-2", False, True, 26)],
        )
        self.assertEqual(
            [(b.beam_id, b.power, b.setpoint_c) for b in morning.beams],
            [("BEAM-TEST-1", True, 20)],
        )

    def test_unreported_and_empty_beams_differ(self) -> None:
        base = _base([_program(_record(4))])
        unreported = parse_programs(base, _EMPTY_EXTENSION)
        self.assertIsNone(unreported.programs[0].beams)
        attributes = cast("list[dict[str, Any]]", unreported.attributes()["programs"])
        self.assertIsNone(attributes[0]["beams"])
        for items in ([], [None]):
            with self.subTest(items=len(items)):
                reported = parse_programs(
                    base, _extension([_BEAM_ID], [_slot(4, items)])
                )
                self.assertEqual(reported.programs[0].beams, ())
                self.assertEqual(reported.beam_ids, ("BEAM-TEST-1",))

    def test_id_zero_slots_are_consumed_and_omitted(self) -> None:
        base = _base([_program(_record(0, name=b"Unused")), _program(_record(7))])
        extension = _extension(
            [_BEAM_ID], [_slot(0, [_item(0)]), None, _slot(7, [_item(0)])]
        )
        schedule = parse_programs(base, extension)
        self.assertEqual([p.program_id for p in schedule.programs], [7])
        self.assertEqual(
            parse_programs(base, _EMPTY_EXTENSION),
            parse_programs(_base([_program(_record(7))]), _EMPTY_EXTENSION),
        )

    def test_names_stop_at_nul_and_replace_bad_utf8(self) -> None:
        for raw, expected in (
            (b"Caf\xc3\xa9\x00junk", "Café"),
            (b"\xffA", "�A"),
            (b"ExactlySixteen!!", "ExactlySixteen!!"),
        ):
            with self.subTest(name=raw):
                base = _base([_program(_record(1, name=raw))])
                self.assertEqual(
                    parse_programs(base, _EMPTY_EXTENSION).programs[0].name, expected
                )

    def test_advertised_tails_and_unknown_groups_are_skipped(self) -> None:
        base = _base(
            [_record(1, tail=b"\xaa\xbb") + _ac(0) + b"\xcc" + _zone(0) + b"\xdd"],
            header=25,
            ac_size=4,
            zone_size=8,
        )
        extension = _extension(
            [_BEAM_ID],
            [
                _slot(
                    1,
                    [_item(0) + b"\xee\xee"],
                    header_tail=b"\x99\x98",
                    groups=bytes.fromhex("02 03 aabbcc 00 01 01 ff 00"),
                )
            ],
            id_size=70,
        )
        schedule = parse_programs(base, extension)
        self.assertEqual(
            schedule,
            parse_programs(
                _base([_program(_record(1))]),
                _extension([_BEAM_ID], [_slot(1, [_item(0)])]),
            ),
        )

    def test_report_ages_are_ignored(self) -> None:
        self.assertEqual(
            parse_programs(_POPULATED_BASE, _POPULATED_EXTENSION),
            parse_programs(
                b"\xff" * 8 + _POPULATED_BASE[8:], _AGE + _POPULATED_EXTENSION[8:]
            ),
        )

    def test_malformed_base_reports_fail(self) -> None:
        one = _program(_record(3))
        cases = {
            "short header": _AGE + bytes(7),
            "empty with trailing byte": _EMPTY_BASE + b"\x00",
            "over capacity": _base([one, _program(_record(4))], capacity=1),
            "short header record": _base([one], header=22),
            "short ac record": _base([_record(3) + b"\x80\x42" + _zone(0)], ac_size=2),
            "short zone record": _base([_record(3) + _ac(0) + bytes(6)], zone_size=6),
            "truncated": _base([one])[:-1],
            "trailing byte": _base([one]) + b"\x00",
            "duplicate program": _base([one, _program(_record(3))]),
            "duplicate ac": _base([_record(3) + _ac(1) + _ac(1) + _zone(0)], acs=2),
            "duplicate zone": _base(
                [_record(3) + _ac(0) + _zone(2) + _zone(2)], zones=2
            ),
            "start hour": _base([_program(_record(3, times=b"\x18\x00\x08\x00"))]),
            "start minute": _base([_program(_record(3, times=b"\x06\x3c\x08\x00"))]),
            "end hour": _base([_program(_record(3, times=b"\x06\x00\x18\x00"))]),
            "end minute": _base([_program(_record(3, times=b"\x06\x00\x08\x3c"))]),
            "zone start": _base(
                [_record(3) + _ac(0) + _zone(0, times=b"\xff\x00\xff\x00")]
            ),
            "zone end": _base(
                [_record(3) + _ac(0) + _zone(0, times=b"\x00\x00\x17\x3c")]
            ),
        }
        for name, base in cases.items():
            with self.subTest(case=name), self.assertRaises(ProgramParseError):
                parse_programs(base, _EMPTY_EXTENSION)

    def test_malformed_extension_reports_fail(self) -> None:
        base = _base([_program(_record(3))])
        item = _item(0)
        cases = {
            "short age": _AGE[:7],
            "no program count": _AGE + b"\x00",
            "short beam id": _extension([_BEAM_ID], [_slot(3, [])], id_size=63),
            "empty beam id": _extension([b""], [_slot(3, [])]),
            "duplicate beam id": _extension([_BEAM_ID, _BEAM_ID], [_slot(3, [])]),
            "index out of range": _extension([_BEAM_ID], [_slot(3, [_item(1)])]),
            "index names a null slot": _extension([None, _BEAM_ID], [_slot(3, [item])]),
            "duplicate assignment": _extension([_BEAM_ID], [_slot(3, [item, item])]),
            "short item": _extension([_BEAM_ID], [_slot(3, [item[:3]])]),
            "short header": _extension([_BEAM_ID], [None])[:-1] + b"\x01\x02\x03",
            "missing terminator": _extension(
                [_BEAM_ID], [_slot(3, [item], groups=b"")]
            ),
            "trailing byte": _extension([_BEAM_ID], [_slot(3, [item])]) + b"\x00",
            "truncated unknown group": _extension(
                [_BEAM_ID], [_slot(3, [item], groups=b"\x01\x05\xaa\xbb")]
            ),
            "duplicate program": _extension([], [_slot(3, []), _slot(3, [])]),
            "other program": _extension([], [_slot(4, [])]),
            "missing program": _extension([], [None]),
        }
        for name, extension in cases.items():
            with self.subTest(case=name), self.assertRaises(ProgramParseError):
                parse_programs(base, extension)
        two = _base([_program(_record(3)), _program(_record(5))])
        with self.assertRaisesRegex(ProgramParseError, "disagree"):
            parse_programs(two, _extension([], [_slot(3, [])]))
        with self.assertRaisesRegex(ProgramParseError, "disagree"):
            parse_programs(base, _extension([], [_slot(3, []), _slot(5, [])]))


class _ProgramServerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.base = _EMPTY_BASE
        self.extension = _EMPTY_EXTENSION
        self.mode = "ok"
        self.payloads: list[bytes] = []
        self.other_messages = 0
        self.started = asyncio.Event()
        self.closed_while_held: list[bytes] = []
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sockets = self.server.sockets
        if not sockets:
            raise RuntimeError("test server did not bind a socket")
        self.port = sockets[0].getsockname()[1]
        self.hass = _Hass()
        self.entry = _Entry()

    async def asyncTearDown(self) -> None:
        await async_release_programs(cast("Any", self.hass), self.entry.entry_id)
        self.server.close()
        await self.server.wait_closed()

    def _owner(self, *, deadline: float = 1.0) -> ProgramCoordinator:
        owner = async_install_programs(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            port=self.port,
            deadline=deadline,
        )
        if owner is None:
            raise AssertionError("expected an AirTouch 5 program owner")
        return owner

    async def _held_closes(self) -> list[bytes]:
        for _ in range(100):
            if self.closed_while_held:
                break
            await asyncio.sleep(0.01)
        return self.closed_while_held

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            header, payload = _oracle_decode(await _read_raw(reader))
            if header.message_id != 0x1F or not payload.startswith(b"\xff\x41"):
                self.other_messages += 1
                return
            self.payloads.append(payload)
            if self.mode == "hold":
                self.started.set()
                self.closed_while_held.append(await asyncio.wait_for(reader.read(1), 2))
                return
            if self.mode == "close":
                return
            writer.write(
                _frame(header.packet_id, _QUERY + self.base)
                + _frame(header.packet_id, _EXTENSION_PREFIX + self.extension)
            )
            await writer.drain()
            await asyncio.wait_for(reader.read(1), 2)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, TimeoutError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


class ProgramCoordinatorTests(_ProgramServerCase):
    def _entity(self, owner: ProgramCoordinator) -> ProgramsEntity:
        return ProgramsEntity(owner, "hub", cast("Any", _DEVICE_INFO))

    async def test_count_and_attributes_follow_zero_then_some_then_none(self) -> None:
        owner = self._owner()
        entity = self._entity(owner)
        self.assertEqual(entity.unique_id, "hub_programs")
        self.assertEqual(entity.translation_key, "programs")
        await owner.async_refresh()
        self.assertTrue(entity.available)
        self.assertEqual(entity.native_value, 0)
        self.assertEqual(
            entity.extra_state_attributes, {"programs": [], "reported_beam_ids": []}
        )
        self.base, self.extension = _POPULATED_BASE, _POPULATED_EXTENSION
        await owner.async_refresh()
        self.assertEqual(entity.native_value, 1)
        self.assertEqual(entity.extra_state_attributes, _POPULATED_ATTRIBUTES)
        self.base, self.extension = _EMPTY_BASE, _EMPTY_EXTENSION
        await owner.async_refresh()
        self.assertEqual(entity.native_value, 0)
        self.assertEqual(
            entity.extra_state_attributes, {"programs": [], "reported_beam_ids": []}
        )
        self.assertEqual(self.payloads, [_QUERY, _QUERY, _QUERY])

    async def test_failure_after_success_keeps_data_unavailable_until_recovery(
        self,
    ) -> None:
        owner = self._owner()
        entity = self._entity(owner)
        self.base, self.extension = _POPULATED_BASE, _POPULATED_EXTENSION
        await owner.async_refresh()
        good = owner.data
        for mode, base in (("close", _POPULATED_BASE), ("ok", _POPULATED_BASE + b"\0")):
            with self.subTest(mode=mode):
                self.mode, self.base = mode, base
                await owner.async_refresh()
                self.assertFalse(entity.available)
                self.assertIs(owner.data, good)
        self.mode, self.base = "ok", _POPULATED_BASE
        await owner.async_refresh()
        self.assertTrue(entity.available)
        self.assertEqual(entity.extra_state_attributes, _POPULATED_ATTRIBUTES)

    async def test_first_failure_still_creates_an_unavailable_sensor(self) -> None:
        self._owner()
        self.mode = "close"
        entities = await async_collect_program_entities(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            cast("Any", _Device()),
        )
        self.assertEqual(len(entities), 1)
        entity = cast("ProgramsEntity", entities[0])
        self.assertFalse(entity.available)
        self.assertIsNone(entity.native_value)
        self.assertIsNone(entity.extra_state_attributes)
        self.assertEqual(self.payloads, [_QUERY])

    async def test_configured_id_zero_is_not_counted(self) -> None:
        owner = self._owner()
        entity = self._entity(owner)
        self.base = _base([_program(_record(0)), _program(_record(6, enabled=False))])
        self.extension = _extension([], [_slot(0, []), _slot(6, [])])
        await owner.async_refresh()
        self.assertEqual(entity.native_value, 1)
        attributes = cast("dict[str, Any]", entity.extra_state_attributes)
        self.assertEqual([p["id"] for p in attributes["programs"]], [6])
        self.assertEqual(attributes["programs"][0]["beams"], [])

    async def test_report_ages_do_not_change_the_snapshot(self) -> None:
        owner = self._owner()
        entity = self._entity(owner)
        self.base, self.extension = _POPULATED_BASE, _POPULATED_EXTENSION
        await owner.async_refresh()
        first = entity.extra_state_attributes
        self.base = _LATER_AGE + _POPULATED_BASE[8:]
        self.extension = bytes(8) + _POPULATED_EXTENSION[8:]
        await owner.async_refresh()
        self.assertEqual(entity.extra_state_attributes, first)
        self.assertEqual(first, _POPULATED_ATTRIBUTES)

    async def test_large_schedule_attributes_stay_out_of_the_recorder(self) -> None:
        records = [
            _record(number, name=f"Program {number:02d}".encode())
            + b"".join(_ac(index) for index in range(4))
            + b"".join(
                _zone(index, flags=0x81, target=60, times=b"\x07\x00\x08\x00")
                for index in range(16)
            )
            for number in range(1, 17)
        ]
        owner = self._owner()
        entity = self._entity(owner)
        self.base = _base(records, acs=4, zones=16)
        await owner.async_refresh()
        self.assertEqual(entity.native_value, 16)
        attributes = {
            **cast("dict[str, Any]", entity.extra_state_attributes),
            "friendly_name": "Console Programs",
        }
        self.assertGreater(len(json.dumps(attributes)), 16384)

        def recorded(state_info: StateInfo | None) -> bytes:
            state = State(
                "sensor.console_programs", "16", attributes, state_info=state_info
            )
            data: EventStateChangedData = {
                "entity_id": state.entity_id,
                "old_state": None,
                "new_state": state,
            }
            return StateAttributes.shared_attrs_bytes_from_event(
                Event(EVENT_STATE_CHANGED, data), None
            )

        unrecorded = _combined_unrecorded(entity)
        self.assertEqual(
            recorded({"unrecorded_attributes": unrecorded}),
            b'{"friendly_name":"Console Programs"}',
        )
        self.assertEqual(recorded(None), b"{}")

    async def test_airtouch_4_has_no_owner_entity_or_connection(self) -> None:
        fourth = _AirTouch(AirTouchModel.AIRTOUCH_4)
        self.assertIsNone(
            async_install_programs(
                cast("Any", self.hass), cast("Any", self.entry), cast("Any", fourth)
            )
        )
        entities = await async_collect_program_entities(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", fourth),
            cast("Any", _Device()),
        )
        self.assertEqual(entities, [])
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_programs(cast("Any", self.hass), self.entry.entry_id)
        self.assertEqual(self.entry.unloads, [])
        self.assertEqual(self.payloads, [])

    async def test_install_identity_repeated_release_and_reinstall(self) -> None:
        owner = self._owner()
        self.assertIs(get_programs(cast("Any", self.hass), self.entry.entry_id), owner)
        with self.assertRaisesRegex(RuntimeError, "already installed"):
            self._owner()
        await async_release_programs(cast("Any", self.hass), self.entry.entry_id)
        await async_release_programs(cast("Any", self.hass), self.entry.entry_id)
        await owner.async_shutdown()
        self.assertNotIn("airtouch_programs", self.hass.data)
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_programs(cast("Any", self.hass), self.entry.entry_id)
        await owner.async_refresh()
        self.assertEqual(self.payloads, [])
        fresh = self._owner()
        self.assertIsNot(fresh, owner)
        await fresh.async_refresh()
        self.assertTrue(fresh.last_update_success)
        self.assertEqual(self.payloads, [_QUERY])

    async def test_caller_cancellation_closes_the_socket_and_keeps_the_owner(
        self,
    ) -> None:
        owner = self._owner(deadline=5)
        self.mode = "hold"
        task = asyncio.create_task(owner.async_refresh())
        await asyncio.wait_for(self.started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await self._held_closes(), [b""])
        self.mode = "ok"
        await asyncio.wait_for(owner.async_refresh(), 1)
        self.assertTrue(owner.last_update_success)
        self.assertEqual(self.payloads, [_QUERY, _QUERY])

    async def test_release_cancels_a_blocked_read_without_cancelling_its_caller(
        self,
    ) -> None:
        owner = self._owner(deadline=5)
        entity = self._entity(owner)
        self.base, self.extension = _POPULATED_BASE, _POPULATED_EXTENSION
        await owner.async_refresh()
        good = owner.data
        self.mode = "hold"
        caller = asyncio.create_task(owner.async_refresh())
        await asyncio.wait_for(self.started.wait(), 1)
        await asyncio.wait_for(
            async_release_programs(cast("Any", self.hass), self.entry.entry_id), 1
        )
        self.assertTrue(caller.done())
        self.assertFalse(caller.cancelled())
        await caller
        self.assertEqual(await self._held_closes(), [b""])
        self.assertFalse(entity.available)
        self.assertIs(owner.data, good)
        self.mode = "ok"
        await owner.async_refresh()
        self.assertEqual(len(self.payloads), 2)

    async def test_queued_read_is_refused_once_shutdown_begins(self) -> None:
        owner = self._owner(deadline=5)
        self.mode = "hold"
        first = asyncio.create_task(owner.async_refresh())
        await asyncio.wait_for(self.started.wait(), 1)
        queued = asyncio.create_task(owner.async_refresh())
        await asyncio.sleep(0.05)
        self.mode = "ok"
        await asyncio.wait_for(owner.async_shutdown(), 1)
        await asyncio.wait_for(asyncio.gather(first, queued), 1)
        self.assertEqual(self.payloads, [_QUERY])
        self.assertFalse(owner.last_update_success)
        self.assertIsNone(owner.data)
        await owner.async_shutdown()

    async def test_a_read_that_finishes_after_shutdown_begins_is_not_published(
        self,
    ) -> None:
        owner = self._owner()
        self.base, self.extension = _POPULATED_BASE, _POPULATED_EXTENSION
        closing: list[asyncio.Task[None]] = []

        def parse_then_close(base: bytes, extension: bytes) -> object:
            closing.append(asyncio.create_task(owner.async_shutdown()))
            return parse_programs(base, extension)

        with patch.object(programs_module, "parse_programs", parse_then_close):
            await owner.async_refresh()
        await asyncio.wait_for(closing[0], 1)
        self.assertEqual(self.payloads, [_QUERY])
        self.assertFalse(owner.last_update_success)
        self.assertIsNone(owner.data)

    async def test_shutdown_leaves_no_scheduled_refresh_with_listeners_attached(
        self,
    ) -> None:
        with patch.object(programs_module, "_UPDATE_INTERVAL", timedelta(seconds=0.01)):
            owner = self._owner(deadline=5)
        owner.async_add_listener(lambda: None)
        for _ in range(100):
            if self.entry.scheduled:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(
            self.entry.scheduled, ["AirTouch programs - programs-test - refresh"]
        )
        self.mode = "hold"
        caller = asyncio.create_task(owner.async_refresh())
        await asyncio.wait_for(self.started.wait(), 1)
        await asyncio.wait_for(owner.async_shutdown(), 1)
        await caller
        self.entry.scheduled.clear()
        await asyncio.sleep(0.7)
        self.assertEqual(self.entry.scheduled, [])
        self.assertEqual(self.payloads, [_QUERY])


class ProgramRootTests(_ProgramServerCase):
    @contextlib.contextmanager
    def _root(self, api: _Api) -> Iterator[None]:
        def connect(*, model: object, host: str) -> _Api:
            del model, host
            return api

        def client(
            host: str, port: int, *, deadline: float = DEFAULT_DEADLINE
        ) -> ConsoleClient:
            del port
            return ConsoleClient(host, self.port, deadline=deadline)

        with (
            patch("custom_components.airtouch.pyairtouch.connect", connect),
            patch("custom_components.airtouch.smart_settings.ConsoleClient", client),
            patch("custom_components.airtouch.preferences.ConsoleClient", client),
            patch("custom_components.airtouch.io_outputs.ConsoleClient", client),
            patch("custom_components.airtouch.programs.ConsoleClient", client),
        ):
            yield

    async def _setup(self, entry: _RootEntry, api: _Api) -> bool:
        with self._root(api):
            return await async_setup_integration(
                cast("Any", self.hass), cast("Any", entry)
            )

    def _forward_sensor(
        self, api: _Api, added: list[ProgramsEntity]
    ) -> Callable[[object, object], Coroutine[Any, Any, None]]:
        async def forward(config_entry: object, platforms: object) -> None:
            del platforms
            self.assertEqual(self.payloads, [])
            entities = await async_collect_program_entities(
                cast("Any", self.hass),
                cast("Any", config_entry),
                cast("Any", api),
                cast("Any", _Device()),
            )
            added.extend(cast("list[ProgramsEntity]", entities))

        return forward

    async def test_root_installs_without_io_and_the_sensor_reads_once(self) -> None:
        api = _Api()
        entry = _RootEntry("root")
        added: list[ProgramsEntity] = []
        self.hass.config_entries = _ConfigEntries(self._forward_sensor(api, added))
        self.base, self.extension = _POPULATED_BASE, _POPULATED_EXTENSION
        self.assertTrue(await self._setup(entry, api))
        self.addAsyncCleanup(
            async_release_programs, cast("Any", self.hass), entry.entry_id
        )
        self.assertEqual(self.payloads, [_QUERY])
        self.assertEqual([entity.native_value for entity in added], [1])
        self.assertIs(
            get_programs(cast("Any", self.hass), entry.entry_id), added[0].coordinator
        )
        self.assertIs(self.hass.data[DOMAIN][entry.entry_id], api)

        self.mode = "close"
        failed = _RootEntry("failed-read")
        failed_added: list[ProgramsEntity] = []
        self.hass.config_entries = _ConfigEntries(
            self._forward_sensor(api, failed_added)
        )
        self.payloads.clear()
        self.assertTrue(await self._setup(failed, api))
        self.addAsyncCleanup(
            async_release_programs, cast("Any", self.hass), failed.entry_id
        )
        self.assertEqual(len(failed_added), 1)
        self.assertFalse(failed_added[0].available)
        self.assertIs(self.hass.data[DOMAIN][failed.entry_id], api)
        self.assertEqual(api.shutdown_calls, 0)

        fourth = _Api(AirTouchModel.AIRTOUCH_4)
        at4 = _RootEntry("at4", SelectedModel.AIRTOUCH_4.value)
        at4_added: list[ProgramsEntity] = []
        self.hass.config_entries = _ConfigEntries(
            self._forward_sensor(fourth, at4_added)
        )
        self.payloads.clear()
        self.assertTrue(await self._setup(at4, fourth))
        self.assertEqual(at4_added, [])
        self.assertEqual(self.payloads, [])
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_programs(cast("Any", self.hass), at4.entry_id)

    async def test_root_failure_and_cancellation_release_programs(self) -> None:
        api = _Api()
        for error in (RuntimeError("forward failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                entry = _RootEntry(type(error).__name__)

                async def forward(
                    config_entry: object,
                    platforms: object,
                    *,
                    failure: BaseException = error,
                ) -> None:
                    del platforms
                    get_programs(
                        cast("Any", self.hass), cast("Any", config_entry).entry_id
                    )
                    raise failure

                self.hass.config_entries = _ConfigEntries(forward)
                with self.assertRaises(type(error)):
                    await self._setup(entry, api)
                with self.assertRaisesRegex(RuntimeError, "not installed"):
                    get_programs(cast("Any", self.hass), entry.entry_id)
                self.assertIs(self.hass.data[DOMAIN][entry.entry_id], api)
        self.assertEqual(self.payloads, [])

        held = _RootEntry("held-read")
        added: list[ProgramsEntity] = []
        self.hass.config_entries = _ConfigEntries(self._forward_sensor(api, added))
        self.mode = "hold"
        with self._root(api):
            task = asyncio.create_task(
                async_setup_integration(cast("Any", self.hass), cast("Any", held))
            )
            await asyncio.wait_for(self.started.wait(), 1)
            owner = get_programs(cast("Any", self.hass), held.entry_id)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(await self._held_closes(), [b""])
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_programs(cast("Any", self.hass), held.entry_id)
        self.assertEqual(added, [])
        self.mode = "ok"
        await owner.async_refresh()
        self.assertEqual(self.payloads, [_QUERY])
        self.assertEqual(api.shutdown_calls, 0)

    async def test_release_failure_is_logged_and_keeps_the_setup_error(self) -> None:
        api = _Api()
        entry = _RootEntry("logged")

        async def bad_release(hass: object, entry_id: str) -> None:
            del hass, entry_id
            raise RuntimeError("release failed")

        async def fail_forward(config_entry: object, platforms: object) -> None:
            del config_entry, platforms
            raise RuntimeError("forward failed")

        self.hass.config_entries = _ConfigEntries(fail_forward)
        with (
            patch("custom_components.airtouch.async_release_programs", bad_release),
            self.assertLogs("custom_components.airtouch", level="ERROR") as logged,
            self.assertRaisesRegex(RuntimeError, "forward failed"),
        ):
            await self._setup(entry, api)
        self.assertIn("Error releasing AirTouch programs", "\n".join(logged.output))
        await async_release_programs(cast("Any", self.hass), entry.entry_id)
        self.assertEqual(api.shutdown_calls, 0)

    async def test_unload_false_keeps_and_success_releases_before_api_shutdown(
        self,
    ) -> None:
        api = _Api()
        entry = _RootEntry("root")
        self.hass.config_entries = _ConfigEntries(None, unload_ok=False)
        self.assertTrue(await self._setup(entry, api))
        owner = get_programs(cast("Any", self.hass), entry.entry_id)
        self.assertFalse(
            await async_unload_integration(cast("Any", self.hass), cast("Any", entry))
        )
        self.assertIs(get_programs(cast("Any", self.hass), entry.entry_id), owner)
        await owner.async_refresh()
        self.assertTrue(owner.last_update_success)
        self.assertEqual(api.shutdown_calls, 0)

        async def tracked_release(hass: object, entry_id: str) -> None:
            api.events.append("programs-release-start")
            await async_release_programs(cast("Any", hass), entry_id)
            api.events.append("programs-release-done")

        self.hass.config_entries.unload_ok = True
        with patch(
            "custom_components.airtouch.async_release_programs", tracked_release
        ):
            self.assertTrue(
                await async_unload_integration(
                    cast("Any", self.hass), cast("Any", entry)
                )
            )
        self.assertEqual(
            api.events,
            ["programs-release-start", "programs-release-done", "api-shutdown"],
        )
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_programs(cast("Any", self.hass), entry.entry_id)
        seen = list(self.payloads)
        await owner.async_refresh()
        self.assertEqual(self.payloads, seen)

        self.assertTrue(await self._setup(entry, api))
        reloaded = get_programs(cast("Any", self.hass), entry.entry_id)
        self.assertIsNot(reloaded, owner)
        await reloaded.async_refresh()
        self.assertTrue(reloaded.last_update_success)
        self.assertEqual(self.payloads, [*seen, _QUERY])


if __name__ == "__main__":
    unittest.main()
