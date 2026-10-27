from __future__ import annotations

import asyncio
import contextlib
import struct
import tempfile
import unittest
from datetime import UTC, time, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Callable

from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pyairtouch import AirTouchModel
from pyairtouch.at5.comms.hdr import At5Header, HeaderDecoder, HeaderEncoder
from pyairtouch.comms.crc16 import Crc16Modbus

from custom_components.airtouch import beam as beam_module
from custom_components.airtouch.beam import (
    BeamControl,
    BeamCoordinator,
    BeamExchangeError,
    BeamFan,
    BeamId,
    BeamMode,
    BeamParseError,
    BeamRefusedError,
    BeamSnapshot,
    BeamUncertainError,
    BeamUnit,
    DelayTimer,
    SetTimer,
    TimerPair,
    TimerRole,
    async_install_beam,
    async_release_beam,
    encode_write,
    get_beam,
    parse_beam_report,
)
from custom_components.airtouch.console import ConsoleClient
from custom_components.airtouch.const import CONF_AIRTOUCH_MODEL, DOMAIN
from custom_components.airtouch.const import AirTouchModel as SelectedModel

_OUTER = b"\x55\x55\x55\xab"
_ENCODER = HeaderEncoder()
_DECODER = HeaderDecoder()
_CRC = Crc16Modbus()
_READ = bytes.fromhex("ff21")
_CONTROL = bytes.fromhex("ff22")
_TIMER = bytes.fromhex("ff23")
_ID_A = b"BEAM-A"
_ID_B = b"BEAM-B"
_DISABLED = bytes.fromhex("803f")
_EXACT_RECORD = (
    bytes.fromhex("01")
    + b"BEAM-A".ljust(64, b"\x00")
    + b"Lounge".ljust(16, b"\x00")
    + bytes.fromhex("1d34 1018 1219 abcd 0104 0278 04e2 0000 0102 0715 803f")
)


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


def _frame(
    *, to_address: int, from_address: int, packet_id: int, payload: bytes
) -> bytes:
    header = At5Header(
        to_address=to_address,
        from_address=from_address,
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


def _request(packet_id: int, payload: bytes) -> bytes:
    return _frame(
        to_address=0x90, from_address=0xB0, packet_id=packet_id, payload=payload
    )


def _reply(packet_id: int, payload: bytes) -> bytes:
    return _frame(
        to_address=0xB0, from_address=0x90, packet_id=packet_id, payload=payload
    )


def _oracle_decode(frame: bytes) -> tuple[At5Header, bytes]:
    length = struct.unpack_from("!H", frame, 6)[0]
    escaped = frame[10 : 10 + length]
    logical = bytearray(escaped[:4])
    run = 0
    index = 4
    while index < len(escaped):
        logical.append(escaped[index])
        index += 1
        if logical[-1] == 0x55:
            run += 1
            if run == 3:
                if index >= len(escaped) or escaped[index] != 0:
                    raise AssertionError("oracle expected a stuffed zero")
                index += 1
                run = 0
        else:
            run = 0
    restored = _OUTER + b"\x00\x00" + struct.pack("!HH", len(logical), len(logical))
    restored += bytes(logical)
    decoded = _DECODER.decode(restored[: _DECODER.header_length])
    decoded.assert_complete()
    rest = bytes(logical)[10:]
    return decoded.header, rest[: decoded.header.message_length]


async def _read_raw(reader: asyncio.StreamReader) -> bytes:
    outer = await reader.readexactly(10)
    length = struct.unpack_from("!H", outer, 6)[0]
    return outer + await reader.readexactly(length)


def status(  # noqa: PLR0913
    beam_id: bytes = _ID_A,
    *,
    id_slot: bytes | None = None,
    name: bytes = b"Lounge",
    presence: int = 1,
    modes: int = 0x1F,
    fans: int = 0x7F,
    cool: tuple[int, int] = (16, 30),
    heat: tuple[int, int] = (16, 30),
    power: int = 1,
    mode: int = 4,
    fan: int = 2,
    setpoint: int = 110,
    temperature: int = 0xFFFF,
    error: int = 0,
    on_timer: bytes = _DISABLED,
    off_timer: bytes = _DISABLED,
    stride: int = 103,
) -> bytes:
    record = bytearray(range(stride))
    record[0] = presence
    record[1:65] = beam_id.ljust(64, b"\x00") if id_slot is None else id_slot
    record[65:81] = name.ljust(16, b"\x00")
    record[81:87] = bytes((modes, fans, *cool, *heat))
    record[89:93] = bytes((power, mode, fan, setpoint))
    record[93:95] = temperature.to_bytes(2, "big")
    record[97:99] = error.to_bytes(2, "big")
    record[99:103] = on_timer + off_timer
    return bytes(record)


def report(*records: bytes, stride: int = 103) -> bytes:
    if not records:
        return b"\x00"
    return bytes((len(records), stride)) + b"".join(records)


def control_payload(
    beam_id: bytes, power: int, mode: int, fan: int, setpoint: int
) -> bytes:
    return (
        bytes.fromhex("ff22014701")
        + beam_id.ljust(64, b"\x00")
        + bytes((power, mode, fan, setpoint, 0, 0))
    )


def timer_payload(beam_id: bytes, operation: int, body: bytes) -> bytes:
    return (
        bytes.fromhex("ff23014601")
        + beam_id.ljust(64, b"\x00")
        + bytes((operation,))
        + body
    )


def unit(record: bytes | None = None) -> BeamUnit:
    return parse_beam_report(report(record or status())).units[0]


class Console:
    def __init__(self) -> None:
        self.records: list[bytes] = [status()]
        self.body: bytes | None = None
        self.read_mode = "ok"
        self.frames: list[bytes] = []
        self.packets: list[tuple[int, bytes]] = []
        self.raw: list[bytes] = []
        self.other = 0
        self.read_started = asyncio.Event()
        self.write_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.release.set()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def client(self, host: str, port: int, *, deadline: float = 5.0) -> ConsoleClient:
        del host, port
        return ConsoleClient("127.0.0.1", self.port, deadline=deadline)

    def reads(self) -> int:
        return sum(frame == _READ for frame in self.frames)

    def writes(self) -> list[bytes]:
        return [frame for frame in self.frames if frame[:2] in (_CONTROL, _TIMER)]

    def ordered(self) -> list[bytes]:
        return [payload for _, payload in sorted(self.packets)]

    async def settle(self, count: int) -> None:
        """Wait for count Beam frames, since a send returns before they are read."""
        for _ in range(200):
            if len(self.frames) >= count:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"expected {count} frames, saw {len(self.frames)}")

    def clear(self) -> None:
        self.frames.clear()
        self.packets.clear()
        self.raw.clear()
        self.read_started.clear()
        self.write_seen.clear()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await _read_raw(reader)
            header, payload = _oracle_decode(raw)
            if header.message_id != 0x1F or payload[:2] not in (
                _READ,
                _CONTROL,
                _TIMER,
            ):
                self.other += 1
                return
            self.frames.append(payload)
            self.packets.append((header.packet_id, payload))
            self.raw.append(raw)
            if payload == _READ:
                if not await self._answer_read(reader):
                    return
                body = report(*self.records) if self.body is None else self.body
                writer.write(_reply(header.packet_id, _READ + body))
                await writer.drain()
            else:
                self.write_seen.set()
            await asyncio.wait_for(reader.read(1), 2)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, TimeoutError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _answer_read(self, reader: asyncio.StreamReader) -> bool:
        if self.read_mode == "close":
            return False
        if self.read_mode == "hold":
            self.read_started.set()
            released = asyncio.ensure_future(self.release.wait())
            closed = asyncio.ensure_future(reader.read(1))
            done, pending = await asyncio.wait(
                {released, closed}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return released in done and closed not in done
        return True


def stalled_send(
    delay: float = 10,
) -> Callable[[ConsoleClient, bytes], Any]:
    original = ConsoleClient.send_extended
    open_connection = asyncio.open_connection

    async def stalled_drain() -> None:
        await asyncio.sleep(delay)

    async def send(client: ConsoleClient, payload: bytes) -> None:
        sender = asyncio.current_task()
        with contextlib.ExitStack() as patches:

            async def connect(
                host: str, port: int
            ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
                reader, writer = await open_connection(host, port)
                if asyncio.current_task() is sender:
                    patches.enter_context(patch.object(writer, "drain", stalled_drain))
                return reader, writer

            patches.enter_context(patch.object(asyncio, "open_connection", connect))
            await original(client, payload)

    return send


class _Api:
    def __init__(
        self, host: str = "127.0.0.1", model: AirTouchModel = AirTouchModel.AIRTOUCH_5
    ) -> None:
        self.host = host
        self.model = model


def _entry(host: str = "console") -> config_entries.ConfigEntry:
    return config_entries.ConfigEntry(
        data={CONF_HOST: host, CONF_AIRTOUCH_MODEL: SelectedModel.AIRTOUCH_5.value},
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        minor_version=2,
        options={},
        source=config_entries.SOURCE_USER,
        subentries_data=None,
        title=host,
        unique_id=host,
        version=2,
    )


class BeamReportParseTests(unittest.TestCase):
    def test_exact_record_offsets(self) -> None:
        snapshot = parse_beam_report(bytes.fromhex("0167") + _EXACT_RECORD)
        self.assertEqual(len(_EXACT_RECORD), 103)
        self.assertEqual(
            snapshot,
            BeamSnapshot(
                (
                    BeamUnit(
                        beam_id=BeamId(b"BEAM-A"),
                        name="Lounge",
                        mode_mask=0x1D,
                        fan_mask=0x34,
                        cool_limits=(16, 24),
                        heat_limits=(18, 25),
                        power=1,
                        mode=4,
                        fan=2,
                        setpoint=0x78,
                        temperature=0x04E2,
                        error=0x0102,
                        on_timer=TimerPair(bytes.fromhex("0715")),
                        off_timer=TimerPair(_DISABLED),
                    ),
                )
            ),
        )
        found = snapshot.units[0]
        self.assertIs(found.is_on, True)  # noqa: FBT003
        self.assertIs(found.displayed_mode, BeamMode.COOL)
        self.assertEqual(
            found.selectable_modes,
            (BeamMode.AUTO, BeamMode.DRY, BeamMode.FAN, BeamMode.COOL),
        )
        self.assertEqual(
            found.selectable_fans, (BeamFan.LOW, BeamFan.HIGH, BeamFan.POWERFUL)
        )
        self.assertIs(found.reported_fan, BeamFan.LOW)
        self.assertEqual(found.target_celsius, 22)
        self.assertEqual(found.current_limits, (16, 24))
        self.assertFalse(found.offline)
        self.assertEqual(found.on_timer.value, time(7, 21))
        self.assertIsNone(found.off_timer.value)
        self.assertIs(snapshot.unit(BeamId(b"BEAM-A")), found)
        self.assertIsNone(snapshot.unit(BeamId(b"BEAM-A\x00")))
        self.assertIsNone(snapshot.unit(BeamId(b"BEAM")))

    def test_empty_report_is_one_zero_byte(self) -> None:
        self.assertEqual(parse_beam_report(b"\x00"), BeamSnapshot(()))
        for invalid in (b"", b"\x00\x67", b"\x00\x00", b"\x01"):
            with self.subTest(body=invalid.hex()), self.assertRaises(BeamParseError):
                parse_beam_report(invalid)

    def test_larger_stride_skips_opaque_tails_and_inactive_markers(self) -> None:
        records = (
            status(_ID_A, stride=110),
            status(b"", presence=0, stride=110),
            status(_ID_A, presence=2, stride=110),
            status(_ID_B, name=b"Den", stride=110),
        )
        snapshot = parse_beam_report(report(*records, stride=110))
        self.assertEqual(
            [(item.beam_id, item.name) for item in snapshot.units],
            [(b"BEAM-A", "Lounge"), (b"BEAM-B", "Den")],
        )

    def test_truncation_short_stride_and_trailers_fail(self) -> None:
        one = report(status())
        cases = {
            "header": b"\x01",
            "record": one[:-1],
            "missing record": bytes.fromhex("0267") + status(),
            "short stride": bytes.fromhex("0166") + status()[:102],
            "trailer": one + b"\x00",
        }
        for name, body in cases.items():
            with self.subTest(case=name), self.assertRaises(BeamParseError):
                parse_beam_report(body)

    def test_identity_is_bytes_before_the_first_nul(self) -> None:
        padded = (b"BEAM-A\x00" + b"\xaa" * 57).ljust(64, b"\x00")
        found = unit(status(id_slot=padded))
        self.assertEqual(found.beam_id, b"BEAM-A")
        self.assertEqual(found, unit(status(_ID_A)))
        full = bytes(range(0x41, 0x41 + 64))
        self.assertEqual(unit(status(id_slot=full)).beam_id, full)
        self.assertEqual(
            unit(status(b"\xff\xfe")).beam_id, b"\xff\xfe", "raw bytes are kept"
        )

    def test_duplicate_or_empty_active_identity_fails_the_whole_report(self) -> None:
        padded = (b"BEAM-A\x00junk").ljust(64, b"\x00")
        cases = {
            "same": (status(_ID_A), status(_ID_B), status(_ID_A, name=b"Other")),
            "padding": (status(_ID_A), status(id_slot=padded)),
            "empty": (status(_ID_A), status(b"")),
            "nul first": (status(id_slot=b"\x00BEAM-A".ljust(64, b"\x00")),),
        }
        for name, records in cases.items():
            with self.subTest(case=name), self.assertRaises(BeamParseError):
                parse_beam_report(report(*records))
        inactive = parse_beam_report(report(status(_ID_A), status(_ID_A, presence=0)))
        self.assertEqual([item.beam_id for item in inactive.units], [b"BEAM-A"])

    def test_names_stop_at_nul_and_replace_invalid_utf8(self) -> None:
        self.assertEqual(unit(status(name=b"Den\x00Hidden")).name, "Den")
        self.assertEqual(unit(status(name=b"Caf\xc3")).name, "Caf�")
        self.assertEqual(unit(status(name=b"")).name, "")

    def test_unknown_values_stay_raw_without_controls(self) -> None:
        found = unit(status(power=2, mode=0xFF, fan=0xFF, error=0x1234))
        self.assertIsNone(found.is_on)
        self.assertIsNone(found.displayed_mode)
        self.assertIsNone(found.reported_fan)
        self.assertIsNone(found.current_limits)
        self.assertEqual((found.power, found.mode, found.fan), (2, 0xFF, 0xFF))
        self.assertEqual(found.error, 0x1234)
        self.assertFalse(found.offline)
        self.assertTrue(unit(status(error=0xFFFE)).offline)
        self.assertIs(unit(status(power=0)).is_on, False)  # noqa: FBT003
        self.assertEqual(unit(status(modes=0xE0, fans=0x80)).selectable_modes, ())
        self.assertEqual(unit(status(modes=0xE0, fans=0x80)).selectable_fans, ())

    def test_automatic_submodes_display_auto_with_the_auto_limits(self) -> None:
        for mode in (0, 8, 9):
            with self.subTest(mode=mode):
                found = unit(status(mode=mode, cool=(20, 28), heat=(16, 24)))
                self.assertIs(found.displayed_mode, BeamMode.AUTO)
                self.assertEqual(found.current_limits, (16, 28))
        self.assertNotIn(8, [int(mode) for mode in BeamMode])

    def test_fan_mask_zero_keeps_the_reported_fan(self) -> None:
        found = unit(status(fans=0, fan=3))
        self.assertEqual(found.selectable_fans, ())
        self.assertIs(found.reported_fan, BeamFan.MEDIUM)

    def test_raw_temperature_word_is_kept_without_a_reading(self) -> None:
        self.assertEqual(unit(status(temperature=1250)).temperature, 1250)
        self.assertEqual(unit(status(temperature=0xFFFF)).temperature, 0xFFFF)

    def test_target_uses_native_integer_division(self) -> None:
        for raw, celsius in ((0, 10), (9, 10), (110, 21), (119, 21), (250, 35)):
            with self.subTest(raw=raw):
                self.assertEqual(unit(status(setpoint=raw)).target_celsius, celsius)

    def test_limits_use_only_usable_pairs(self) -> None:
        cases: list[tuple[dict[str, Any], tuple[int, int] | None]] = [
            ({"mode": 4, "cool": (16, 30)}, (16, 30)),
            ({"mode": 2, "cool": (17, 29)}, (17, 29)),
            ({"mode": 3, "cool": (17, 29)}, (17, 29)),
            ({"mode": 1, "heat": (15, 25)}, (15, 25)),
            ({"mode": 4, "cool": (20, 20)}, (20, 20)),
            ({"mode": 4, "cool": (0, 30)}, None),
            ({"mode": 4, "cool": (16, 0xFF)}, None),
            ({"mode": 4, "cool": (30, 16)}, None),
            ({"mode": 4, "cool": (36, 40)}, None),
            ({"mode": 4, "cool": (5, 9)}, None),
            ({"mode": 4, "cool": (5, 12)}, (10, 12)),
            ({"mode": 4, "cool": (33, 50)}, (33, 35)),
            ({"mode": 0, "cool": (20, 28), "heat": (0xFF, 0xFF)}, (20, 28)),
            ({"mode": 9, "cool": (30, 16), "heat": (16, 24)}, (16, 24)),
            ({"mode": 8, "cool": (0, 0), "heat": (0, 0)}, None),
            ({"mode": 0, "cool": (25, 30), "heat": (16, 18)}, (16, 30)),
            ({"mode": 0xFF}, None),
            ({"mode": 5}, None),
        ]
        for fields, expected in cases:
            with self.subTest(**fields):
                self.assertEqual(unit(status(**fields)).current_limits, expected)
        found = unit(status(mode=4, cool=(20, 26), heat=(16, 22)))
        self.assertEqual(found.limits(BeamMode.HEAT), (16, 22))
        self.assertEqual(found.limits(BeamMode.AUTO), (16, 26))

    def test_one_invalid_timer_pair_does_not_discard_the_other(self) -> None:
        cases = {
            "0000": time(0, 0),
            "173b": time(23, 59),
            "803f": None,
            "8a1e": None,
            "181e": None,
            "0a3c": None,
            "2a1e": time(10, 30),
            "0a5e": time(10, 30),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                found = unit(
                    status(on_timer=bytes.fromhex(raw), off_timer=bytes.fromhex("0615"))
                )
                self.assertEqual(found.on_timer.value, expected)
                self.assertEqual(found.on_timer.raw, bytes.fromhex(raw))
                self.assertEqual(found.off_timer.value, time(6, 21))
                self.assertEqual(found.timer(TimerRole.OFF), found.off_timer)


class BeamWriteEncodingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.unit = unit(
            status(
                modes=0x1D,
                fans=0x34,
                cool=(16, 24),
                heat=(18, 25),
                mode=4,
                on_timer=bytes.fromhex("8a1e"),
                off_timer=bytes.fromhex("0615"),
            )
        )

    def test_control_uses_preserve_sentinels_for_unselected_fields(self) -> None:
        literal = (
            bytes.fromhex("ff22014701")
            + b"BEAM-A"
            + bytes(58)
            + bytes.fromhex("03040f78 0000")
        )
        self.assertEqual(
            encode_write(
                self.unit, BeamControl(power=True, mode=BeamMode.COOL, celsius=22)
            ),
            literal,
        )
        self.assertEqual(len(literal), 75)
        cases = [
            (BeamControl(power=True), (3, 0x0F, 0x0F, 0xFF)),
            (BeamControl(power=False), (2, 0x0F, 0x0F, 0xFF)),
            (BeamControl(mode=BeamMode.AUTO), (0, 0, 0x0F, 0xFF)),
            (BeamControl(fan=BeamFan.POWERFUL), (0, 0x0F, 5, 0xFF)),
            (BeamControl(celsius=16), (0, 0x0F, 0x0F, 60)),
            (BeamControl(power=False, celsius=24), (2, 0x0F, 0x0F, 140)),
        ]
        for control, (power, mode, fan, setpoint) in cases:
            with self.subTest(control=control):
                self.assertEqual(
                    encode_write(self.unit, control),
                    control_payload(_ID_A, power, mode, fan, setpoint),
                )

    def test_ten_celsius_is_wire_zero_and_preserve_is_ff(self) -> None:
        wide = unit(status(cool=(10, 35)))
        self.assertEqual(
            encode_write(wide, BeamControl(celsius=10)),
            control_payload(_ID_A, 0, 0x0F, 0x0F, 0x00),
        )
        self.assertEqual(
            encode_write(wide, BeamControl(celsius=35)),
            control_payload(_ID_A, 0, 0x0F, 0x0F, 0xFA),
        )
        self.assertEqual(
            encode_write(wide, BeamControl(power=True)),
            control_payload(_ID_A, 3, 0x0F, 0x0F, 0xFF),
        )

    def test_padded_identity_is_sent_nul_padded_to_64(self) -> None:
        padded = (b"BEAM-A\x00junk").ljust(64, b"\x00")
        found = unit(status(id_slot=padded))
        self.assertEqual(
            encode_write(found, BeamControl(power=True)),
            control_payload(_ID_A, 3, 0x0F, 0x0F, 0xFF),
        )

    def test_absolute_timer_replaces_one_pair_and_keeps_the_other_raw(self) -> None:
        cases = [
            (SetTimer(TimerRole.ON, time(6, 30)), bytes.fromhex("0206 1e06 15")),
            (SetTimer(TimerRole.ON, None), bytes.fromhex("0280 3f06 15")),
            (SetTimer(TimerRole.OFF, time(23, 59)), bytes.fromhex("028a 1e17 3b")),
            (SetTimer(TimerRole.OFF, None), bytes.fromhex("028a 1e80 3f")),
            (SetTimer(TimerRole.OFF, time(0, 0)), bytes.fromhex("028a 1e00 00")),
        ]
        for write, body in cases:
            with self.subTest(write=write):
                self.assertEqual(
                    encode_write(self.unit, write),
                    timer_payload(_ID_A, body[0], body[1:]),
                )

    def test_delay_is_console_relative_whole_minutes(self) -> None:
        cases = [
            (DelayTimer(TimerRole.ON, 1), bytes.fromhex("01000100")),
            (DelayTimer(TimerRole.OFF, 1439), bytes.fromhex("00173b00")),
            (DelayTimer(TimerRole.ON, 90), bytes.fromhex("01011e00")),
        ]
        for write, body in cases:
            with self.subTest(write=write):
                self.assertEqual(
                    encode_write(self.unit, write), timer_payload(_ID_A, 1, body)
                )

    def test_invalid_commands_are_refused_before_any_device(self) -> None:
        cases: list[Callable[[], object]] = [
            lambda: BeamControl(celsius=9),
            lambda: BeamControl(celsius=36),
            lambda: DelayTimer(TimerRole.ON, 0),
            lambda: DelayTimer(TimerRole.ON, -1),
            lambda: DelayTimer(TimerRole.ON, 1440),
            lambda: SetTimer(TimerRole.ON, time(6, 30, 1)),
            lambda: SetTimer(TimerRole.ON, time(6, 30, 0, 1)),
            lambda: SetTimer(TimerRole.ON, time(6, 30, tzinfo=UTC)),
        ]
        for index, build in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(BeamRefusedError):
                build()
        self.assertTrue(issubclass(BeamRefusedError, HomeAssistantError))

    def test_fresh_capabilities_and_limits_refuse_writes(self) -> None:
        refused = [
            BeamControl(mode=BeamMode.HEAT),
            BeamControl(fan=BeamFan.AUTO),
            BeamControl(celsius=25),
            BeamControl(celsius=15),
            BeamControl(mode=BeamMode.AUTO, celsius=26),
            BeamControl(mode=BeamMode.COOL, celsius=25),
        ]
        for control in refused:
            with self.subTest(control=control), self.assertRaises(BeamRefusedError):
                encode_write(self.unit, control)
        self.assertEqual(
            encode_write(self.unit, BeamControl(mode=BeamMode.AUTO, celsius=25)),
            control_payload(_ID_A, 0, 0, 0x0F, 150),
        )
        silent = unit(status(fans=0, fan=2, mode=0xFF))
        for control in (BeamControl(fan=BeamFan.LOW), BeamControl(celsius=20)):
            with self.subTest(control=control), self.assertRaises(BeamRefusedError):
                encode_write(silent, control)
        self.assertEqual(
            encode_write(silent, BeamControl(mode=BeamMode.COOL, celsius=20)),
            control_payload(_ID_A, 0, 4, 0x0F, 100),
        )


class _OwnerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.console = Console()
        await self.console.start()
        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        self.hass = HomeAssistant(config_dir.name)
        self.entry = _entry()

    async def asyncTearDown(self) -> None:
        await async_release_beam(self.hass, self.entry.entry_id)
        await self.hass.async_stop(force=True)
        await self.console.stop()

    def _owner(self, deadline: float = 2.0) -> BeamCoordinator:
        owner = async_install_beam(
            self.hass,
            self.entry,
            cast("Any", _Api()),
            port=self.console.port,
            deadline=deadline,
        )
        if owner is None:
            raise AssertionError("an AirTouch 5 gets a Beam owner")
        return owner

    async def _ready(self, deadline: float = 2.0) -> BeamCoordinator:
        owner = self._owner(deadline)
        await owner.async_refresh()
        self.console.clear()
        return owner


class BeamOwnerTests(_OwnerCase):
    async def test_write_reads_fresh_then_sends_one_frame_without_readback(
        self,
    ) -> None:
        owner = await self._ready()
        self.console.records = [status(cool=(16, 31), mode=4, power=1)]
        await owner.async_write(BeamId(_ID_A), BeamControl(celsius=31))
        await self.console.settle(2)
        self.assertEqual(
            self.console.raw,
            [
                _request(2, bytes.fromhex("ff21")),
                _request(3, control_payload(_ID_A, 0, 0x0F, 0x0F, 210)),
            ],
        )
        self.assertEqual(owner.data, parse_beam_report(report(*self.console.records)))
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.console.frames), 2)

    async def test_explicit_write_is_sent_even_when_the_fresh_state_matches(
        self,
    ) -> None:
        owner = await self._ready()
        for _ in range(2):
            await owner.async_write(BeamId(_ID_A), BeamControl(power=True))
        await self.console.settle(4)
        self.assertEqual(
            self.console.ordered(),
            [_READ, control_payload(_ID_A, 3, 0x0F, 0x0F, 0xFF)] * 2,
        )

    async def test_fresh_limits_and_capabilities_decide_under_contention(
        self,
    ) -> None:
        owner = await self._ready()
        self.console.records = [status(cool=(20, 24), modes=0x10, fans=0x04)]
        for control in (
            BeamControl(celsius=18),
            BeamControl(mode=BeamMode.HEAT),
            BeamControl(fan=BeamFan.HIGH),
        ):
            with self.subTest(control=control), self.assertRaises(BeamRefusedError):
                await owner.async_write(BeamId(_ID_A), control)
        self.assertEqual(self.console.writes(), [])
        self.assertEqual(owner.data.units[0].cool_limits, (20, 24))
        self.console.records = [status(cool=(10, 18))]
        await owner.async_write(BeamId(_ID_A), BeamControl(celsius=18))
        await self.console.settle(5)
        self.assertEqual(
            self.console.writes(), [control_payload(_ID_A, 0, 0x0F, 0x0F, 80)]
        )

    async def test_missing_offline_and_unreadable_units_send_nothing(self) -> None:
        owner = await self._ready()
        self.console.records = [status(_ID_B)]
        with self.assertRaisesRegex(BeamRefusedError, "not reported"):
            await owner.async_write(BeamId(_ID_A), BeamControl(power=True))
        self.console.records = [status(error=0xFFFE)]
        with self.assertRaisesRegex(BeamRefusedError, "offline"):
            await owner.async_write(BeamId(_ID_A), BeamControl(power=True))
        self.assertTrue(owner.last_update_success)
        self.console.body = report(status(), status())
        with self.assertRaises(BeamExchangeError):
            await owner.async_write(BeamId(_ID_A), BeamControl(power=True))
        self.assertFalse(owner.last_update_success)
        self.console.body = None
        self.console.read_mode = "close"
        with self.assertRaises(BeamExchangeError):
            await owner.async_write(BeamId(_ID_A), BeamControl(power=True))
        self.assertEqual(self.console.writes(), [])
        self.assertEqual(self.console.reads(), 4)

    async def test_errors_do_not_expose_identity_or_payload(self) -> None:
        owner = await self._ready()
        self.console.records = [status(_ID_B)]
        with self.assertRaises(BeamRefusedError) as raised:
            await owner.async_write(BeamId(_ID_A), BeamControl(power=True))
        self.assertNotIn("BEAM", str(raised.exception))
        self.assertNotIn(_ID_A.hex(), str(raised.exception))

    async def test_uncertain_send_keeps_the_pre_write_report_and_is_not_retried(
        self,
    ) -> None:
        owner = await self._ready(deadline=0.3)
        self.console.records = [status(power=0)]
        with (
            patch.object(ConsoleClient, "send_extended", stalled_send()),
            self.assertRaises(BeamUncertainError),
        ):
            await owner.async_write(BeamId(_ID_A), BeamControl(power=True))
        self.assertEqual(
            self.console.writes(), [control_payload(_ID_A, 3, 15, 15, 255)]
        )
        self.assertTrue(owner.last_update_success)
        self.assertIs(owner.data.units[0].is_on, False)  # noqa: FBT003

    async def test_connection_refused_before_writing_is_not_sent(self) -> None:
        owner = await self._ready()

        async def refused(client: ConsoleClient, payload: bytes) -> None:
            del client, payload
            raise ConnectionRefusedError

        with (
            patch.object(ConsoleClient, "send_extended", refused),
            self.assertRaisesRegex(BeamExchangeError, "not sent"),
        ):
            await owner.async_write(BeamId(_ID_A), BeamControl(power=True))
        self.assertEqual(self.console.frames, [_READ])

    async def test_polls_and_writes_run_one_at_a_time(self) -> None:
        owner = await self._ready()
        self.console.read_mode = "hold"
        write = asyncio.create_task(
            owner.async_write(BeamId(_ID_A), BeamControl(power=False))
        )
        await asyncio.wait_for(self.console.read_started.wait(), 1)
        poll = asyncio.create_task(owner.async_refresh())
        await asyncio.sleep(0.1)
        self.assertEqual(self.console.frames, [_READ])
        self.console.read_mode = "ok"
        self.console.release.set()
        await asyncio.wait_for(asyncio.gather(write, poll), 2)
        await self.console.settle(3)
        self.assertEqual(
            self.console.ordered(),
            [_READ, control_payload(_ID_A, 2, 15, 15, 255), _READ],
        )

    async def test_cancel_once_or_twice_during_the_fresh_read_sends_nothing(
        self,
    ) -> None:
        owner = await self._ready()
        for cancels in (1, 2):
            with self.subTest(cancels=cancels):
                self.console.read_mode = "hold"
                write = asyncio.create_task(
                    owner.async_write(BeamId(_ID_A), BeamControl(power=True))
                )
                await asyncio.wait_for(self.console.read_started.wait(), 1)
                for _ in range(cancels):
                    write.cancel()
                    await asyncio.sleep(0)
                with self.assertRaises(asyncio.CancelledError):
                    await write
                self.console.read_started.clear()
        self.console.read_mode = "ok"
        await owner.async_write(BeamId(_ID_A), BeamControl(power=False))
        await self.console.settle(4)
        self.assertEqual(
            self.console.ordered(),
            [_READ, _READ, _READ, control_payload(_ID_A, 2, 15, 15, 255)],
        )

    async def test_cancel_once_or_twice_during_send_propagates_after_cleanup(
        self,
    ) -> None:
        owner = await self._ready()
        before = owner.data
        for cancels in (1, 2):
            with self.subTest(cancels=cancels):
                self.console.clear()
                with patch.object(ConsoleClient, "send_extended", stalled_send()):
                    write = asyncio.create_task(
                        owner.async_write(BeamId(_ID_A), BeamControl(power=True))
                    )
                    await asyncio.wait_for(self.console.write_seen.wait(), 1)
                    for _ in range(cancels):
                        write.cancel()
                        await asyncio.sleep(0)
                    with self.assertRaises(asyncio.CancelledError):
                        await write
                self.assertEqual(len(self.console.writes()), 1)
                self.assertEqual(owner.data, before)
                self.assertTrue(owner.last_update_success)
        self.console.clear()
        await owner.async_write(BeamId(_ID_A), BeamControl(power=False))
        await self.console.settle(2)
        self.assertEqual(len(self.console.writes()), 1)

    async def test_shutdown_cancels_an_owned_poll_without_publishing(self) -> None:
        owner = await self._ready()
        before = owner.data
        self.console.read_mode = "hold"
        poll = asyncio.create_task(owner.async_refresh())
        await asyncio.wait_for(self.console.read_started.wait(), 1)
        await asyncio.wait_for(owner.async_shutdown(), 1)
        await asyncio.wait_for(poll, 1)
        self.assertTrue(owner.closed)
        self.assertIs(owner.data, before)
        self.assertEqual(self.console.writes(), [])

    async def test_shutdown_refuses_a_write_during_its_fresh_read(self) -> None:
        owner = await self._ready()
        self.console.read_mode = "hold"
        write = asyncio.create_task(
            owner.async_write(BeamId(_ID_A), BeamControl(power=True))
        )
        await asyncio.wait_for(self.console.read_started.wait(), 1)
        await asyncio.wait_for(owner.async_shutdown(), 1)
        with self.assertRaisesRegex(BeamRefusedError, "closed"):
            await write
        self.assertEqual(self.console.frames, [_READ])

    async def test_shutdown_after_a_finished_read_neither_publishes_nor_sends(
        self,
    ) -> None:
        owner = await self._ready()
        before = owner.data
        original = ConsoleClient.request
        shutdowns: list[asyncio.Task[None]] = []

        async def read_finishes_as_shutdown_starts(
            client: ConsoleClient,
            payload: bytes,
            *,
            response_prefix: bytes | None = None,
        ) -> bytes:
            body = await original(client, payload, response_prefix=response_prefix)
            shutdowns.append(asyncio.create_task(owner.async_shutdown()))
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
            return body

        with (
            patch.object(ConsoleClient, "request", read_finishes_as_shutdown_starts),
            self.assertRaisesRegex(BeamRefusedError, "closed"),
        ):
            await owner.async_write(BeamId(_ID_A), BeamControl(power=True))
        await asyncio.wait_for(shutdowns[0], 1)
        await asyncio.sleep(0.1)
        self.assertIs(owner.data, before)
        self.assertEqual(self.console.frames, [_READ])

    async def test_shutdown_drains_an_admitted_send_and_refuses_queued_writes(
        self,
    ) -> None:
        owner = await self._ready(deadline=0.5)
        events: list[str] = []

        async def write(control: BeamControl) -> None:
            try:
                await owner.async_write(BeamId(_ID_A), control)
            except BeamUncertainError:
                events.append("uncertain")
            except BeamRefusedError:
                events.append("refused")

        with patch.object(ConsoleClient, "send_extended", stalled_send()):
            admitted = asyncio.create_task(write(BeamControl(power=True)))
            await asyncio.wait_for(self.console.write_seen.wait(), 1)
            queued = asyncio.create_task(write(BeamControl(power=False)))
            await asyncio.sleep(0)
            shutdown = asyncio.create_task(owner.async_shutdown())
            await asyncio.sleep(0.1)
            self.assertFalse(shutdown.done())
            await asyncio.wait_for(shutdown, 2)
            events.append("shutdown")
            await asyncio.wait_for(asyncio.gather(admitted, queued), 1)
        self.assertEqual(events, ["uncertain", "refused", "shutdown"])
        self.assertEqual(
            self.console.frames, [_READ, control_payload(_ID_A, 3, 15, 15, 255)]
        )
        with self.assertRaisesRegex(BeamRefusedError, "closed"):
            await owner.async_write(BeamId(_ID_A), BeamControl(power=True))
        await owner.async_refresh()
        self.assertEqual(len(self.console.frames), 2)

    async def test_polling_continues_from_zero_until_shutdown(self) -> None:
        self.console.records = []
        with patch.object(beam_module, "_UPDATE_INTERVAL", timedelta(seconds=1)):
            owner = await self._ready()
        self.assertEqual(owner.data, BeamSnapshot(()))
        remove = owner.async_add_listener(lambda: None)
        self.addCleanup(remove)
        for _ in range(300):
            if self.console.reads():
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.console.reads(), 1)
        await owner.async_shutdown()
        await asyncio.sleep(2.2)
        self.assertEqual(self.console.reads(), 1)

    async def test_install_identity_release_and_airtouch_4(self) -> None:
        owner = self._owner()
        self.assertIs(get_beam(self.hass, self.entry.entry_id), owner)
        with self.assertRaisesRegex(RuntimeError, "already installed"):
            self._owner()
        self.assertIsNone(
            async_install_beam(
                self.hass,
                _entry("four"),
                cast("Any", _Api(model=AirTouchModel.AIRTOUCH_4)),
            )
        )
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_beam(self.hass, "four")
        await async_release_beam(self.hass, self.entry.entry_id)
        await async_release_beam(self.hass, self.entry.entry_id)
        self.assertTrue(owner.closed)
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_beam(self.hass, self.entry.entry_id)
        self.assertEqual(self.console.frames, [])


if __name__ == "__main__":
    unittest.main()
