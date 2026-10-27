from __future__ import annotations

import asyncio
import contextlib
import struct
import unittest
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterable, Iterator

    from homeassistant.helpers.entity import Entity

from homeassistant.const import CONF_HOST, Platform
from homeassistant.exceptions import HomeAssistantError
from pyairtouch import AirTouchModel
from pyairtouch.at5.comms.hdr import At5Header, HeaderDecoder, HeaderEncoder
from pyairtouch.comms.crc16 import Crc16Modbus

from custom_components.airtouch import (
    async_setup_entry as async_setup_integration,
)
from custom_components.airtouch import (
    async_unload_entry as async_unload_integration,
)
from custom_components.airtouch import button, switch
from custom_components.airtouch.button import RelayPulseButton
from custom_components.airtouch.console import (
    DEFAULT_DEADLINE,
    ConsoleClient,
    ConsoleSendUncertainError,
)
from custom_components.airtouch.const import CONF_AIRTOUCH_MODEL, DOMAIN
from custom_components.airtouch.const import AirTouchModel as SelectedModel
from custom_components.airtouch.io_outputs import (
    IoExchangeError,
    IoOutputsCoordinator,
    IoParseError,
    IoRejectedError,
    IoSnapshot,
    IoUncertainError,
    IoUnconfirmedError,
    PulseOutput,
    ToggleOutput,
    async_install_io_outputs,
    async_release_io_outputs,
    get_io_outputs,
    parse_io_names,
    parse_io_report,
)
from custom_components.airtouch.smart_settings import async_install
from custom_components.airtouch.switch import RelayOutputSwitch

_OUTER = b"\x55\x55\x55\xab"
_ENCODER = HeaderEncoder()
_DECODER = HeaderDecoder()
_CRC = Crc16Modbus()
_UNUSED = 0
_TOGGLE = 2
_PULSE = 3
_QUERY_HEX = "5d00000000000000"
_DEVICE_INFO: dict[str, object] = {
    "identifiers": {("airtouch", "hub")},
    "name": "Console",
}
_SMART_SWITCH_IDS = {
    "hub_energy_wastage_detection",
    "hub_weather_adaptation_advanced",
    "hub_weather_adaptation_auto_off",
    "hub_weather_adaptation_basic",
}


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
    *,
    to_address: int,
    from_address: int,
    packet_id: int,
    message_id: int,
    payload: bytes,
) -> bytes:
    header = At5Header(
        to_address=to_address,
        from_address=from_address,
        packet_id=packet_id,
        message_id=message_id,
        message_length=len(payload),
    )
    encoded = _ENCODER.encode(header)
    crc = _CRC.calculate(encoded.checksum_data + payload)
    escaped = _oracle_escape(encoded.header_bytes[10:] + payload + crc)
    return (
        _OUTER + b"\x00\x00" + struct.pack("!HH", len(escaped), len(escaped)) + escaped
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


def _report(
    rows: Iterable[tuple[int, int, int]], *, closed: int = 0, inputs: int = 0
) -> bytes:
    body = b"".join(bytes(row) for row in rows)
    count = len(body) // 3
    return struct.pack("!BxHHH", 0x5D, 2, 3, count) + bytes((closed, inputs)) + body


def _console_rows(kinds: dict[int, tuple[int, int]]) -> list[tuple[int, int, int]]:
    rows = []
    for channel in range(8):
        output_type, parameter = kinds.get(channel, (_UNUSED, channel))
        rows.append((channel, parameter, output_type))
    return [*rows, (0x80, 0, 0), (0x81, 0, 0)]


def _names(names: dict[int, bytes], *, state: int = 0) -> bytes:
    """Build the bytes after FF 04 in writer order with 16-byte names."""
    records = b""
    for index in range(8):
        name = names.get(index, b"").ljust(16, b"\x00")
        records += bytes((0, index, state)) + name
    for index in range(2):
        records += bytes((1, index, 2)) + b"Input".ljust(16, b"\x00")
    return bytes((16, 10, 19)) + records


class _Entry:
    def __init__(self, entry_id: str = "entry") -> None:
        self.entry_id = entry_id
        self.title = "io-test"
        self.domain = DOMAIN
        self.pref_disable_polling = False
        self.unloads: list[Callable[[], object]] = []

    def async_on_unload(self, func: Callable[[], object]) -> None:
        self.unloads.append(func)


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
    def __init__(self, entry_id: str) -> None:
        super().__init__(entry_id)
        self.version = 2
        self.minor_version = 2
        self.unique_id = entry_id
        self.data = {
            CONF_HOST: "127.0.0.1",
            CONF_AIRTOUCH_MODEL: SelectedModel.AIRTOUCH_5.value,
        }


class _ConfigEntries:
    def __init__(
        self,
        forward: Callable[[object, object], Any] | None,
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


class IoReportParseTests(unittest.TestCase):
    def test_capture_shaped_report_with_unused_outputs_is_empty(self) -> None:
        payload = _report(_console_rows({}))
        self.assertEqual(payload[:8].hex(), "5d0000020003000a")
        self.assertEqual(parse_io_report(payload), ())

    def test_closed_bits_and_pulse_widths(self) -> None:
        rows = [
            (0, 0, _TOGGLE),
            (1, 0x00, _PULSE),
            (2, 0x01, _PULSE),
            (3, 0x13, _PULSE),
            (4, 0x18, _PULSE),
            (5, 0x00, _TOGGLE),
            (6, 0x06, _PULSE),
            (7, 0x05, _TOGGLE),
        ]
        self.assertEqual(
            parse_io_report(_report(rows, closed=0b1000_0001)),
            (
                ToggleOutput(channel=0, name=None, closed=True),
                PulseOutput(channel=1, name=None, width_ms=350),
                PulseOutput(channel=2, name=None, width_ms=50),
                PulseOutput(channel=3, name=None, width_ms=150),
                PulseOutput(channel=4, name=None, width_ms=350),
                ToggleOutput(channel=5, name=None, closed=False),
                PulseOutput(channel=6, name=None, width_ms=300),
                ToggleOutput(channel=7, name=None, closed=True),
            ),
        )

    def test_inputs_unused_unsupported_and_missing_channels_are_absent(self) -> None:
        rows = [
            (0x81, 0, _TOGGLE),
            (1, 0, 7),
            (2, 0, _UNUSED),
            (6, 0, _TOGGLE),
            (0x80, 0, _PULSE),
        ]
        self.assertEqual(
            parse_io_report(_report(rows, closed=0b0100_0010, inputs=0b11)),
            (ToggleOutput(channel=6, name=None, closed=True),),
        )

    def test_duplicate_output_fails_even_when_one_is_unsupported(self) -> None:
        for second in ((4, 0, 9), (0x0C, 0, _UNUSED), (4, 0, _PULSE)):
            with self.subTest(second=second), self.assertRaises(IoParseError):
                parse_io_report(_report([(4, 0, _TOGGLE), second]))

    def test_malformed_reports_fail(self) -> None:
        good = _report([(0, 0, _TOGGLE)])
        cases = {
            "truncated header": good[:7],
            "other subcommand": b"\x5e" + good[1:],
            "short normal": struct.pack("!BxHHH", 0x5D, 1, 3, 1) + b"\x00\x00\x00\x02",
            "short repeat": struct.pack("!BxHHH", 0x5D, 2, 2, 1) + b"\x00\x00\x00\x02",
            "extra byte": good + b"\x00",
            "missing byte": good[:-1],
        }
        for name, payload in cases.items():
            with self.subTest(case=name), self.assertRaises(IoParseError):
                parse_io_report(payload)
        no_repeats = struct.pack("!BxHHH", 0x5D, 2, 0, 0) + b"\x00\x00"
        self.assertEqual(parse_io_report(no_repeats), ())


class IoNameParseTests(unittest.TestCase):
    def test_writer_order_names_stop_at_nul_and_replace_bad_utf8(self) -> None:
        body = _names({0: b"Pool pump", 2: b"Gate\x00junk", 5: b"Fan \xff"})
        self.assertEqual(body[:3].hex(), "100a13")
        self.assertEqual(len(body), 193)
        self.assertEqual(
            parse_io_names(body),
            {
                0: "Pool pump",
                1: "",
                2: "Gate",
                3: "",
                4: "",
                5: "Fan \ufffd",
                6: "",
                7: "",
            },
        )

    def test_invalid_layouts_are_ignored(self) -> None:
        good = _names({0: b"Pool pump"})
        second = 3 + 19
        cases = {
            "swapped count and stride": bytes((16, 19, 10)) + good[3:],
            "stride shorter than a name": bytes((16, 10, 18)) + good[3 : 3 + 180],
            "extra byte": good + b"\x00",
            "truncated header": good[:2],
            "unknown kind": good[:second] + b"\x02" + good[second + 1 :],
            "output index 8": good[:second] + b"\x00\x08" + good[second + 2 :],
            "duplicate output": good[:second] + b"\x00\x00" + good[second + 2 :],
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(parse_io_names(body))


class IoOutputServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kinds: dict[int, tuple[int, int]] = {}
        self.closed: set[int] = set()
        self.names: bytes | None = None
        self.mode = "ok"
        self.raw: list[bytes] = []
        self.payloads: list[bytes] = []
        self.other_messages = 0
        self.connections = 0
        self.active = 0
        self.max_active = 0
        self.hold_connection = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.on_trigger: Callable[[int, int], None] = self._apply_toggle
        self.before_report: Callable[[], None] | None = None
        self.drop_after_trigger = False
        self.close_on_trigger = False
        self.refuse_after_query = False
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sockets = self.server.sockets
        if not sockets:
            raise RuntimeError("test server did not bind a socket")
        self.port = sockets[0].getsockname()[1]
        self.hass = _Hass()
        self.entry = _Entry()
        self.added: list[Entity] = []

    async def asyncTearDown(self) -> None:
        self.release.set()
        await async_release_io_outputs(cast("Any", self.hass), self.entry.entry_id)
        self.server.close()
        await self.server.wait_closed()

    def _owner(self, *, deadline: float = 1.0) -> IoOutputsCoordinator:
        owner = async_install_io_outputs(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            port=self.port,
            deadline=deadline,
        )
        if owner is None:
            raise AssertionError("expected an AirTouch 5 output owner")
        return owner

    def _switch(self, owner: IoOutputsCoordinator, channel: int) -> RelayOutputSwitch:
        return RelayOutputSwitch(owner, "hub", cast("Any", _DEVICE_INFO), channel)

    def _button(self, owner: IoOutputsCoordinator, channel: int) -> RelayPulseButton:
        return RelayPulseButton(owner, "hub", cast("Any", _DEVICE_INFO), channel)

    # Home Assistant AddEntitiesCallback: Iterable[Entity], update_before_add=False.
    def _add(
        self,
        new_entities: Iterable[Entity],
        update_before_add: bool = False,  # noqa: FBT001, FBT002
    ) -> None:
        del update_before_add
        self.added.extend(new_entities)

    def _events(self) -> list[str]:
        return [
            "trigger" if payload[:1] == b"\x5c" else "query"
            for payload in self.payloads
        ]

    async def _triggers(self, count: int) -> list[str]:
        for _ in range(100):
            if self._events().count("trigger") >= count:
                break
            await asyncio.sleep(0.01)
        return self._events()

    def _relays(self) -> list[RelayOutputSwitch | RelayPulseButton]:
        return [
            entity
            for entity in self.added
            if isinstance(entity, RelayOutputSwitch | RelayPulseButton)
        ]

    def _apply_toggle(self, channel: int, action: int) -> None:
        if self.kinds.get(channel, (_UNUSED, 0))[0] != _TOGGLE:
            return
        if action == 1:
            self.closed.add(channel)
        elif action == 2:
            self.closed.discard(channel)

    def _status(self) -> bytes:
        if self.before_report is not None:
            self.before_report()
        closed = sum(1 << channel for channel in self.closed)
        return _report(_console_rows(self.kinds), closed=closed)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        connection = self.connections
        try:
            raw = await _read_raw(reader)
            header, payload = _oracle_decode(raw)
            self.raw.append(raw)
            if header.message_id != 0xC0:
                self.other_messages += 1
                return
            self.payloads.append(payload)
            if payload[:1] == b"\x5c":
                await self._triggered(payload, reader)
                return
            await self._pause(connection)
            if self.drop_after_trigger and "trigger" in self._events():
                return
            writer.write(self._reply(header.packet_id))
            await writer.drain()
            if self.refuse_after_query:
                self.server.close()
        except (asyncio.IncompleteReadError, ConnectionError, OSError, TimeoutError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _triggered(self, payload: bytes, reader: asyncio.StreamReader) -> None:
        for channel, action in enumerate(payload[8:16]):
            if action:
                self.on_trigger(channel, action)
        if not self.close_on_trigger:
            await asyncio.wait_for(reader.read(1), 2)

    def _reply(self, packet_id: int) -> bytes:
        if self.mode == "truncate":
            return b"\x55\x55\x55"
        report = self._status()
        if self.mode == "malformed":
            report += b"\x00"
        frames = b""
        if self.names is not None:
            frames += _frame(
                to_address=0xFF,
                from_address=0x90,
                packet_id=180,
                message_id=0x1F,
                payload=b"\xff\x04" + self.names,
            )
        return frames + _frame(
            to_address=0xB0,
            from_address=0x80,
            packet_id=packet_id,
            message_id=0xC0,
            payload=report,
        )

    async def _pause(self, connection: int) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if connection == self.hold_connection:
                self.started.set()
                await self.release.wait()
            else:
                await asyncio.sleep(0.01)
        finally:
            self.active -= 1

    async def test_status_and_trigger_frames_are_exact(self) -> None:
        self.kinds = {0: (_TOGGLE, 0), 3: (_PULSE, 2), 7: (_TOGGLE, 0)}
        self.closed = {7}
        owner = self._owner()
        await owner.async_set_closed(0, closed=True)
        await owner.async_set_closed(7, closed=False)
        await owner.async_pulse(3)
        await self._triggers(3)
        expected = [
            _QUERY_HEX,
            "5c000008000000000100000000000000",
            _QUERY_HEX,
            _QUERY_HEX,
            "5c000008000000000000000000000002",
            _QUERY_HEX,
            _QUERY_HEX,
            "5c000008000000000000000100000000",
        ]
        self.assertEqual([payload.hex() for payload in self.payloads], expected)
        self.assertEqual(
            self.raw,
            [
                _frame(
                    to_address=0x80,
                    from_address=0xB0,
                    packet_id=packet_id,
                    message_id=0xC0,
                    payload=bytes.fromhex(payload),
                )
                for packet_id, payload in enumerate(expected, start=1)
            ],
        )
        self.assertEqual(owner.update_interval, timedelta(seconds=60))

    async def test_names_come_from_the_earlier_broadcast_and_fall_back(self) -> None:
        self.kinds = {0: (_TOGGLE, 0), 2: (_PULSE, 0)}
        self.closed = {0}
        self.names = _names({0: b"Pool pump", 2: b"Gate\x00junk"}, state=2)
        owner = self._owner()
        pump = self._switch(owner, 0)
        gate = self._button(owner, 2)
        spare = self._switch(owner, 5)
        await owner.async_refresh()
        self.assertEqual(
            (pump.name, gate.name, spare.name), ("Pool pump", "Gate", "Output 6")
        )
        self.assertTrue(pump.is_on)
        self.assertFalse(spare.available)

        self.names = None
        self.closed = set()
        await owner.async_refresh()
        self.assertEqual((pump.name, gate.name), ("Pool pump", "Gate"))
        self.assertFalse(pump.is_on)

        self.names = _names({0: b"", 2: b"Fan \xff"})
        await owner.async_refresh()
        self.assertEqual((pump.name, gate.name), ("Output 1", "Fan \ufffd"))
        self.assertEqual(
            owner.data.outputs,
            (
                ToggleOutput(channel=0, name=None, closed=False),
                PulseOutput(channel=2, name="Fan \ufffd", width_ms=350),
            ),
        )

        self.names = bytes((16, 19, 10)) + _names({0: b"Renamed"})[3:]
        self.closed = {0}
        await owner.async_refresh()
        self.assertTrue(owner.last_update_success)
        self.assertEqual((pump.name, gate.name), ("Output 1", "Fan \ufffd"))
        self.assertTrue(pump.is_on)

    async def test_matching_toggle_publishes_the_fresh_state_without_a_trigger(
        self,
    ) -> None:
        self.kinds = {4: (_TOGGLE, 0)}
        owner = self._owner()
        relay = self._switch(owner, 4)
        self.closed = {4}
        await relay.async_turn_on()
        self.assertTrue(relay.is_on)
        await relay.async_turn_on()
        self.closed = set()
        await relay.async_turn_off()
        self.assertFalse(relay.is_on)
        self.assertEqual(self._events(), ["query", "query", "query"])

    async def test_grouped_readback_publishes_every_peer(self) -> None:
        self.kinds = {1: (_TOGGLE, 0), 2: (_TOGGLE, 0), 3: (_TOGGLE, 1)}
        self.closed = {3}

        def grouped(channel: int, action: int) -> None:
            if channel == 1 and action == 1:
                self.closed = {1, 2}

        self.on_trigger = grouped
        owner = self._owner()
        first, peer, other = (self._switch(owner, channel) for channel in (1, 2, 3))
        await first.async_turn_on()
        self.assertEqual((first.is_on, peer.is_on, other.is_on), (True, True, False))
        self.assertEqual(self._events(), ["query", "trigger", "query"])

    async def test_valid_mismatch_publishes_actual_state_after_three_reads(
        self,
    ) -> None:
        self.kinds = {1: (_TOGGLE, 0), 6: (_TOGGLE, 0)}
        reads = 0

        def unrelated_change() -> None:
            nonlocal reads
            reads += 1
            if reads == 4:
                self.closed = {6}

        self.on_trigger = lambda _channel, _action: None
        self.before_report = unrelated_change
        owner = self._owner()
        relay = self._switch(owner, 1)
        peer = self._switch(owner, 6)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.assertRaises(IoUnconfirmedError):
            await owner.async_set_closed(1, closed=True)
        self.assertGreaterEqual(loop.time() - started, 0.9)
        self.assertEqual(
            self._events(), ["query", "trigger", "query", "query", "query"]
        )
        self.assertTrue(relay.available)
        self.assertFalse(relay.is_on)
        self.assertTrue(peer.is_on)
        with self.assertRaises(HomeAssistantError):
            await relay.async_turn_on()
        self.assertEqual(self._events().count("trigger"), 2)

    async def test_later_readback_can_confirm_the_change(self) -> None:
        self.kinds = {5: (_TOGGLE, 0)}
        pending: list[int] = []
        reads_after = 0

        def settle() -> None:
            nonlocal reads_after
            if pending:
                reads_after += 1
                if reads_after == 2:
                    self.closed.add(pending.pop())

        self.on_trigger = lambda channel, _action: pending.append(channel)
        self.before_report = settle
        owner = self._owner()
        relay = self._switch(owner, 5)
        await relay.async_turn_on()
        self.assertTrue(relay.is_on)
        self.assertEqual(self._events(), ["query", "trigger", "query", "query"])

    async def test_type_change_after_the_trigger_stops_without_a_second_trigger(
        self,
    ) -> None:
        self.kinds = {2: (_TOGGLE, 0)}

        def retype(channel: int, action: int) -> None:
            del action
            self.kinds[channel] = (_PULSE, 0)

        self.on_trigger = retype
        owner = self._owner()
        relay = self._switch(owner, 2)
        pulse = self._button(owner, 2)
        with self.assertRaises(IoUnconfirmedError):
            await owner.async_set_closed(2, closed=True)
        self.assertEqual(self._events(), ["query", "trigger", "query"])
        self.assertFalse(relay.available)
        self.assertTrue(pulse.available)

    async def test_readback_failure_is_uncertain_and_keeps_the_last_data(
        self,
    ) -> None:
        self.kinds = {3: (_TOGGLE, 0)}
        self.drop_after_trigger = True
        owner = self._owner()
        relay = self._switch(owner, 3)
        with self.assertRaises(HomeAssistantError) as raised:
            await relay.async_turn_on()
        self.assertIsInstance(raised.exception.__cause__, IoUncertainError)
        self.assertEqual(self._events(), ["query", "trigger", "query"])
        self.assertFalse(owner.last_update_success)
        self.assertFalse(relay.available)
        self.assertIsNone(relay.is_on)
        self.assertEqual(
            owner.data.outputs, (ToggleOutput(channel=3, name=None, closed=False),)
        )
        self.drop_after_trigger = False
        self.payloads.clear()
        await owner.async_refresh()
        self.assertTrue(relay.available)
        self.assertTrue(relay.is_on)
        self.assertEqual(self._events(), ["query"])

    async def test_stale_kind_is_refused_after_publishing_the_fresh_state(
        self,
    ) -> None:
        owner = self._owner()
        relay = self._switch(owner, 3)
        pulse = self._button(owner, 3)
        self.kinds = {3: (_TOGGLE, 0)}
        await owner.async_refresh()
        self.assertTrue(relay.available)
        self.assertFalse(pulse.available)
        self.kinds = {3: (_PULSE, 0)}
        with self.assertRaises(HomeAssistantError) as raised:
            await relay.async_turn_on()
        self.assertIsInstance(raised.exception.__cause__, IoRejectedError)
        self.assertFalse(relay.available)
        self.assertTrue(pulse.available)
        for kinds in ({3: (_TOGGLE, 0)}, {}):
            with self.subTest(kinds=kinds):
                self.kinds = kinds
                with self.assertRaises(HomeAssistantError):
                    await pulse.async_press()
                self.assertFalse(pulse.available)
        self.assertNotIn("trigger", self._events())
        self.assertEqual(len(self.payloads), 4)

    async def test_pulse_sends_one_trigger_and_returns_without_a_readback(
        self,
    ) -> None:
        self.kinds = {6: (_PULSE, 4)}
        owner = self._owner()
        pulse = self._button(owner, 6)
        await pulse.async_press()
        self.assertEqual(await self._triggers(1), ["query", "trigger"])
        self.assertEqual(self.payloads[1][8:].hex(), "0000000000000100")
        self.assertEqual(
            owner.data.outputs, (PulseOutput(channel=6, name=None, width_ms=200),)
        )
        self.assertTrue(pulse.available)

    async def test_uncertain_or_cancelled_pulse_is_not_retried(self) -> None:
        self.kinds = {6: (_PULSE, 0)}
        owner = self._owner()
        pulse = self._button(owner, 6)
        original = ConsoleClient.send_control

        async def lost_after_writing(client: ConsoleClient, payload: bytes) -> None:
            await original(client, payload)
            raise ConsoleSendUncertainError("lost after writing")

        with patch.object(ConsoleClient, "send_control", lost_after_writing):
            with self.assertRaises(HomeAssistantError) as raised:
                await pulse.async_press()
            self.assertIsInstance(raised.exception.__cause__, IoUncertainError)
        self.assertEqual(await self._triggers(1), ["query", "trigger"])
        self.assertFalse(owner.last_update_success)
        self.assertFalse(pulse.available)
        self.assertEqual(
            owner.data.outputs, (PulseOutput(channel=6, name=None, width_ms=350),)
        )

        async def stalled_after_writing(client: ConsoleClient, payload: bytes) -> None:
            await original(client, payload)
            await asyncio.Event().wait()

        with patch.object(ConsoleClient, "send_control", stalled_after_writing):
            task = asyncio.create_task(owner.async_pulse(6))
            await self._triggers(2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self._events(), ["query", "trigger", "query", "trigger"])
        self.assertFalse(owner.last_update_success)
        self.assertFalse(pulse.available)
        await owner.async_refresh()
        self.assertTrue(pulse.available)

        self.close_on_trigger = True
        self.payloads.clear()
        try:
            await owner.async_pulse(6)
        except IoUncertainError:
            outcome = "uncertain"
        else:
            outcome = "submitted"
        self.assertIn(outcome, {"submitted", "uncertain"})
        self.assertEqual(await self._triggers(1), ["query", "trigger"])

    async def test_cancelled_toggle_readback_invalidates_contact(self) -> None:
        self.kinds = {0: (_TOGGLE, 0)}
        owner = self._owner()
        relay = self._switch(owner, 0)
        await owner.async_refresh()
        stored = owner.data
        self.hold_connection = self.connections + 3
        task = asyncio.create_task(relay.async_turn_on())
        await asyncio.wait_for(self.started.wait(), 1)
        task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(owner.last_update_success)
            self.assertFalse(relay.available)
            self.assertIsNone(relay.is_on)
            self.assertEqual(owner.data, stored)
            self.assertEqual(self._events().count("trigger"), 1)
        finally:
            self.release.set()
        await owner.async_refresh()
        self.assertTrue(relay.is_on)

    async def test_cancelled_toggle_settling_invalidates_contact(self) -> None:
        self.kinds = {0: (_TOGGLE, 0)}
        self.on_trigger = lambda _channel, _action: None
        owner = self._owner()
        relay = self._switch(owner, 0)
        settling = asyncio.Event()
        real_sleep = asyncio.sleep

        async def sleep(delay: float) -> None:
            if delay == 10:
                settling.set()
                await asyncio.Event().wait()
            else:
                await real_sleep(delay)

        with (
            patch(
                "custom_components.airtouch.io_outputs._UNMEASURED_TOGGLE_SETTLING_INTERVAL",
                10,
            ),
            patch("custom_components.airtouch.io_outputs.asyncio.sleep", sleep),
        ):
            task = asyncio.create_task(relay.async_turn_on())
            await asyncio.wait_for(settling.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(owner.last_update_success)
        self.assertFalse(relay.available)
        self.assertIsNone(relay.is_on)
        self.assertEqual(self._events(), ["query", "trigger", "query"])

    async def test_refused_trigger_connection_sends_nothing(self) -> None:
        self.kinds = {6: (_PULSE, 0)}
        owner = self._owner()
        self.refuse_after_query = True
        with self.assertRaises(IoExchangeError):
            await owner.async_pulse(6)
        self.assertEqual(self._events(), ["query"])
        self.assertTrue(owner.last_update_success)

    async def test_failed_pre_action_read_marks_all_outputs_unavailable(self) -> None:
        self.kinds = {0: (_TOGGLE, 0), 7: (_PULSE, 1)}
        self.closed = {0}
        owner = self._owner()
        relay = self._switch(owner, 0)
        pulse = self._button(owner, 7)
        for action in (relay.async_turn_off, pulse.async_press):
            with self.subTest(action=action.__name__):
                self.mode = "ok"
                await owner.async_refresh()
                stored = owner.data
                self.assertTrue(relay.available)
                self.assertTrue(pulse.available)
                self.mode = "truncate"
                with self.assertRaises(HomeAssistantError):
                    await action()
                self.assertFalse(owner.last_update_success)
                self.assertFalse(relay.available)
                self.assertFalse(pulse.available)
                self.assertIsNone(relay.is_on)
                self.assertIs(owner.data, stored)
                self.assertNotIn("trigger", self._events())

    async def test_failed_or_malformed_poll_keeps_the_last_data_unavailable(
        self,
    ) -> None:
        self.kinds = {0: (_TOGGLE, 0)}
        self.closed = {0}
        owner = self._owner()
        relay = self._switch(owner, 0)
        await owner.async_refresh()
        stored = owner.data
        for mode in ("truncate", "malformed"):
            with self.subTest(mode=mode):
                self.mode = mode
                await owner.async_refresh()
                self.assertFalse(owner.last_update_success)
                self.assertFalse(relay.available)
                self.assertIsNone(relay.is_on)
                self.assertIs(owner.data, stored)
        self.mode = "ok"
        await owner.async_refresh()
        self.assertTrue(relay.is_on)

    async def test_concurrent_actions_and_polls_run_one_at_a_time(self) -> None:
        self.kinds = {1: (_TOGGLE, 0)}
        owner = self._owner()
        await owner.async_refresh()
        self.hold_connection = self.connections + 1
        first = asyncio.create_task(owner.async_set_closed(1, closed=True))
        await asyncio.wait_for(self.started.wait(), 1)
        second = asyncio.create_task(owner.async_set_closed(1, closed=True))
        poll = asyncio.create_task(owner.async_refresh())
        await asyncio.sleep(0.05)
        self.assertFalse(second.done())
        self.assertFalse(poll.done())
        self.release.set()
        await asyncio.wait_for(asyncio.gather(first, second, poll), 2)
        self.assertEqual(self.max_active, 1)
        self.assertEqual(
            self._events(), ["query", "query", "trigger", "query", "query", "query"]
        )

    async def _setup_platforms(self) -> IoOutputsCoordinator:
        self.hass.data.setdefault(DOMAIN, {})[self.entry.entry_id] = _AirTouch()
        smart = async_install(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            port=self.port,
        )
        if smart is None:
            raise AssertionError("expected a smart settings owner")
        self.addAsyncCleanup(smart.async_shutdown)
        owner = self._owner()
        with patch(
            "custom_components.airtouch.devices.AirTouchDevice",
            return_value=_Device(),
        ):
            await switch.async_setup_entry(
                cast("Any", self.hass), cast("Any", self.entry), self._add
            )
            await button.async_setup_entry(
                cast("Any", self.hass), cast("Any", self.entry), self._add
            )
        return owner

    async def test_platforms_discover_each_kind_once_and_keep_retyped_entities(
        self,
    ) -> None:
        owner = await self._setup_platforms()
        self.assertEqual(self.connections, 0)
        self.assertEqual({entity.unique_id for entity in self.added}, _SMART_SWITCH_IDS)
        self.mode = "truncate"
        await owner.async_refresh()
        self.mode = "ok"
        await owner.async_refresh()
        self.assertEqual(self._relays(), [])

        self.kinds = {2: (_TOGGLE, 0)}
        await owner.async_refresh()
        await owner.async_refresh()
        relays = self._relays()
        self.assertEqual(len(relays), 1)
        relay = relays[0]
        self.assertIsInstance(relay, RelayOutputSwitch)
        self.assertEqual((relay.unique_id, relay.name), ("hub_output_3", "Output 3"))
        self.assertTrue(relay.available)

        self.kinds = {2: (_PULSE, 1)}
        await owner.async_refresh()
        relays = self._relays()
        self.assertEqual(len(relays), 2)
        pulse = relays[1]
        self.assertIsInstance(pulse, RelayPulseButton)
        self.assertEqual(pulse.unique_id, "hub_output_3")
        self.assertFalse(relay.available)
        self.assertTrue(pulse.available)

        self.kinds = {2: (_TOGGLE, 0)}
        await owner.async_refresh()
        self.assertEqual(self._relays(), [relay, pulse])
        self.assertTrue(relay.available)
        self.assertFalse(pulse.available)

    async def test_empty_outputs_keep_polling_until_configuration_appears(self) -> None:
        with patch("custom_components.airtouch.io_outputs._UPDATE_INTERVAL", 1):
            owner = await self._setup_platforms()
        tasks: list[asyncio.Task[None]] = []
        refreshed = asyncio.Event()

        def schedule(
            hass: object,
            coro: Coroutine[object, object, None],
            name: str,
            *,
            eager_start: bool,
        ) -> asyncio.Task[None]:
            del hass, eager_start
            task = asyncio.create_task(coro, name=name)
            task.add_done_callback(lambda _: refreshed.set())
            tasks.append(task)
            return task

        with patch.object(
            self.entry,
            "async_create_background_task",
            side_effect=schedule,
            create=True,
        ):
            try:
                await owner.async_refresh()
                self.assertEqual(self._relays(), [])
                initial_connections = self.connections
                await asyncio.wait_for(refreshed.wait(), 3)
                self.assertGreater(self.connections, initial_connections)
                self.assertEqual(self._relays(), [])
                refreshed.clear()
                self.kinds = {4: (_PULSE, 2)}
                await asyncio.wait_for(refreshed.wait(), 3)
                self.assertIsInstance(self._relays()[0], RelayPulseButton)
                self.assertEqual(self._relays()[0].unique_id, "hub_output_5")
            finally:
                await owner.async_shutdown()
                await asyncio.gather(*tasks)

    async def test_shutdown_drains_admitted_work_without_late_publication(
        self,
    ) -> None:
        self.kinds = {1: (_TOGGLE, 0)}
        owner = await self._setup_platforms()
        await owner.async_refresh()
        self.assertEqual(len(self._relays()), 1)
        self.hold_connection = self.connections + 3
        admitted = asyncio.create_task(owner.async_set_closed(1, closed=True))
        await asyncio.wait_for(self.started.wait(), 1)
        self.kinds[5] = (_PULSE, 0)
        late = asyncio.create_task(owner.async_pulse(5))
        closing = asyncio.create_task(
            async_release_io_outputs(cast("Any", self.hass), self.entry.entry_id)
        )
        await asyncio.sleep(0.05)
        self.assertFalse(admitted.done())
        self.assertFalse(closing.done())
        seen = self.connections
        self.release.set()
        await asyncio.wait_for(admitted, 2)
        with self.assertRaises(IoRejectedError):
            await asyncio.wait_for(late, 2)
        await asyncio.wait_for(closing, 2)
        self.assertEqual(self.connections, seen)
        self.assertEqual(len(self._relays()), 1)
        self.assertEqual(
            owner.data.outputs, (ToggleOutput(channel=1, name=None, closed=False),)
        )
        owner.async_set_updated_data(
            IoSnapshot((PulseOutput(channel=5, name=None, width_ms=350),))
        )
        self.assertEqual(len(self._relays()), 1)
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_io_outputs(cast("Any", self.hass), self.entry.entry_id)
        await async_release_io_outputs(cast("Any", self.hass), self.entry.entry_id)
        for callback in list(self.entry.unloads):
            result = callback()
            if asyncio.iscoroutine(result):
                await result
        await owner.async_refresh()
        self.assertEqual(self.connections, seen)
        with self.assertRaises(IoRejectedError):
            await owner.async_set_closed(1, closed=False)
        self.assertEqual(self.connections, seen)

    async def test_install_identity_and_airtouch_4(self) -> None:
        owner = self._owner()
        self.assertIs(
            get_io_outputs(cast("Any", self.hass), self.entry.entry_id), owner
        )
        with self.assertRaisesRegex(RuntimeError, "already installed"):
            self._owner()
        other_entry = _Entry("other")
        other = async_install_io_outputs(
            cast("Any", self.hass),
            cast("Any", other_entry),
            cast("Any", _AirTouch()),
            port=self.port,
        )
        self.assertIsNotNone(other)
        self.assertIsNot(other, owner)
        await async_release_io_outputs(cast("Any", self.hass), other_entry.entry_id)
        self.assertIs(
            get_io_outputs(cast("Any", self.hass), self.entry.entry_id), owner
        )
        fourth = _Entry("airtouch-4")
        self.assertIsNone(
            async_install_io_outputs(
                cast("Any", self.hass),
                cast("Any", fourth),
                cast("Any", _AirTouch(AirTouchModel.AIRTOUCH_4)),
                port=self.port,
            )
        )
        self.hass.data.setdefault(DOMAIN, {})[fourth.entry_id] = _AirTouch(
            AirTouchModel.AIRTOUCH_4
        )
        await button.async_setup_entry(
            cast("Any", self.hass), cast("Any", fourth), self._add
        )
        self.assertEqual(self.added, [])
        self.assertEqual(fourth.unloads, [])
        self.assertEqual(self.connections, 0)

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
            patch("custom_components.airtouch.beam.ConsoleClient", client),
            patch(
                "custom_components.airtouch.devices.AirTouchDevice",
                return_value=_Device(),
            ),
        ):
            yield

    async def test_root_registers_discovery_before_the_first_refresh(self) -> None:
        self.kinds = {4: (_PULSE, 0)}
        api = _Api()
        entry = _RootEntry("root")
        forwarded: list[object] = []

        async def forward(config_entry: object, platforms: object) -> None:
            forwarded.append(platforms)
            self.assertEqual(self.connections, 0)
            await button.async_setup_entry(
                cast("Any", self.hass), cast("Any", config_entry), self._add
            )

        self.hass.config_entries = _ConfigEntries(forward)
        with self._root(api):
            self.assertTrue(
                await async_setup_integration(
                    cast("Any", self.hass), cast("Any", entry)
                )
            )
        self.addAsyncCleanup(
            async_release_io_outputs, cast("Any", self.hass), entry.entry_id
        )
        self.assertIn(Platform.BUTTON, cast("list[Platform]", forwarded[0]))
        relays = self._relays()
        self.assertEqual([relay.unique_id for relay in relays], ["hub_output_5"])
        self.assertTrue(relays[0].available)
        self.assertEqual(self._events(), ["query"])
        self.assertIs(self.hass.data[DOMAIN][entry.entry_id], api)

        self.mode = "truncate"
        failed = _RootEntry("failed-read")
        self.hass.config_entries = _ConfigEntries(None)
        with self._root(api):
            self.assertTrue(
                await async_setup_integration(
                    cast("Any", self.hass), cast("Any", failed)
                )
            )
        self.addAsyncCleanup(
            async_release_io_outputs, cast("Any", self.hass), failed.entry_id
        )
        self.assertFalse(
            get_io_outputs(cast("Any", self.hass), failed.entry_id).last_update_success
        )
        self.assertIs(self.hass.data[DOMAIN][failed.entry_id], api)
        self.assertEqual(api.shutdown_calls, 0)

    async def test_root_failure_and_cancellation_release_outputs(self) -> None:
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
                    get_io_outputs(
                        cast("Any", self.hass), cast("Any", config_entry).entry_id
                    )
                    raise failure

                self.hass.config_entries = _ConfigEntries(forward)
                with self._root(api), self.assertRaises(type(error)):
                    await async_setup_integration(
                        cast("Any", self.hass), cast("Any", entry)
                    )
                with self.assertRaisesRegex(RuntimeError, "not installed"):
                    get_io_outputs(cast("Any", self.hass), entry.entry_id)
                self.assertIs(self.hass.data[DOMAIN][entry.entry_id], api)
        self.assertEqual(self.connections, 0)

        held = _RootEntry("held-refresh")
        self.hold_connection = 2
        self.hass.config_entries = _ConfigEntries(None)
        with self._root(api):
            task = asyncio.create_task(
                async_setup_integration(cast("Any", self.hass), cast("Any", held))
            )
            await asyncio.wait_for(self.started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_io_outputs(cast("Any", self.hass), held.entry_id)
        self.assertEqual(self._events(), ["query"])

        async def bad_release(hass: object, entry_id: str) -> None:
            del hass, entry_id
            raise RuntimeError("release failed")

        async def fail_forward(config_entry: object, platforms: object) -> None:
            del config_entry, platforms
            raise RuntimeError("forward failed")

        logged_entry = _RootEntry("logged")
        self.hass.config_entries = _ConfigEntries(fail_forward)
        with (
            self._root(api),
            patch("custom_components.airtouch.async_release_io_outputs", bad_release),
            self.assertLogs("custom_components.airtouch", level="ERROR") as logged,
            self.assertRaisesRegex(RuntimeError, "forward failed"),
        ):
            await async_setup_integration(
                cast("Any", self.hass), cast("Any", logged_entry)
            )
        self.assertIn(
            "Error releasing AirTouch relay outputs", "\n".join(logged.output)
        )
        await async_release_io_outputs(cast("Any", self.hass), logged_entry.entry_id)
        self.assertEqual(api.shutdown_calls, 0)

    async def test_root_unload_false_keeps_and_success_releases_first(self) -> None:
        api = _Api()
        entry = _RootEntry("root")
        self.hass.config_entries = _ConfigEntries(None, unload_ok=False)
        with self._root(api):
            await async_setup_integration(cast("Any", self.hass), cast("Any", entry))
        owner = get_io_outputs(cast("Any", self.hass), entry.entry_id)
        self.addAsyncCleanup(owner.async_shutdown)
        self.assertFalse(
            await async_unload_integration(cast("Any", self.hass), cast("Any", entry))
        )
        self.assertIs(get_io_outputs(cast("Any", self.hass), entry.entry_id), owner)
        self.assertEqual(api.shutdown_calls, 0)

        async def tracked_release(hass: object, entry_id: str) -> None:
            api.events.append("io-release-start")
            await async_release_io_outputs(cast("Any", hass), entry_id)
            api.events.append("io-release-done")

        self.hass.config_entries.unload_ok = True
        with patch(
            "custom_components.airtouch.async_release_io_outputs", tracked_release
        ):
            self.assertTrue(
                await async_unload_integration(
                    cast("Any", self.hass), cast("Any", entry)
                )
            )
        self.assertEqual(
            api.events, ["io-release-start", "io-release-done", "api-shutdown"]
        )
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_io_outputs(cast("Any", self.hass), entry.entry_id)
        seen = self.connections
        await owner.async_refresh()
        self.assertEqual(self.connections, seen)


if __name__ == "__main__":
    unittest.main()
