from __future__ import annotations

import asyncio
import contextlib
import json
import struct
import unittest
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from logging import Logger

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity

from homeassistant.const import CONF_HOST
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pyairtouch import AirTouchModel
from pyairtouch.at5.comms.hdr import (
    ADDRESS_AIRTOUCH_EXTENDED,
    ADDRESS_CLIENT,
    At5Header,
    HeaderDecoder,
    HeaderEncoder,
)
from pyairtouch.at5.comms.x1F_ext import MESSAGE_ID as EXTENDED_MESSAGE_ID
from pyairtouch.comms.crc16 import Crc16Modbus

from custom_components.airtouch import (
    async_setup_entry as async_setup_integration,
)
from custom_components.airtouch import (
    async_unload_entry,
)
from custom_components.airtouch.console import DEFAULT_DEADLINE, ConsoleClient
from custom_components.airtouch.const import CONF_AIRTOUCH_MODEL, DOMAIN
from custom_components.airtouch.const import AirTouchModel as SelectedModel
from custom_components.airtouch.smart_settings import (
    SmartSettingsCoordinator,
    SmartSettingsParseError,
    SmartSettingsRejectedError,
    SmartSettingsSnapshot,
    async_install,
    async_release,
    get_smart_settings,
    parse_smart_settings,
)
from custom_components.airtouch.switch import (
    EnergyWastageSwitch,
    async_collect_energy_wastage_switches,
    async_setup_entry,
)

_OUTER = b"\x55\x55\x55\xab"
_ENCODER = HeaderEncoder()
_DECODER = HeaderDecoder()
_CRC = Crc16Modbus()
_QUERY = bytes.fromhex("ff18")
_RECORDS = bytes.fromhex("00808180828083808480")
_SAMPLE_INFO = bytes.fromhex("01051b0412000001")
_SAMPLE_PAYLOAD = bytes.fromhex("ff1808050201051b041200000100808180828083808480")
_SAMPLE_REQUEST = bytes.fromhex("555555ab0000000e000e555555aa90b0071f0002ff18e38c")
_SAMPLE_RESPONSE = bytes.fromhex(
    "555555ab000000230023555555aab090071f0017ff1808050201051b0412000001008081808280838084806066"
)
_RICH_INFO = bytes.fromhex("a10318031400b1c2d3e4")
_SHORT_INFO = bytes.fromhex("a10318031407")
_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "airtouch"


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


def _oracle_frame(
    *,
    to_address: int,
    from_address: int,
    packet_id: int,
    payload: bytes,
) -> bytes:
    header = At5Header(
        to_address=to_address,
        from_address=from_address,
        packet_id=packet_id,
        message_id=EXTENDED_MESSAGE_ID,
        message_length=len(payload),
    )
    encoded = _ENCODER.encode(header)
    crc = _CRC.calculate(encoded.checksum_data + payload)
    logical = encoded.header_bytes[10:] + payload + crc
    escaped = _oracle_escape(logical)
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
    payload = rest[: decoded.header.message_length]
    return decoded.header, payload


def _status(info: bytes) -> bytes:
    return bytes((0xFF, 0x18, len(info), 5, 2)) + info + _RECORDS


def _reply(packet_id: int, payload: bytes, *, from_address: int = 0x90) -> bytes:
    return _oracle_frame(
        to_address=0xB0,
        from_address=from_address,
        packet_id=packet_id,
        payload=payload,
    )


async def _read_raw(reader: asyncio.StreamReader) -> bytes:
    outer = await reader.readexactly(10)
    length = struct.unpack_from("!H", outer, 6)[0]
    return outer + await reader.readexactly(length)


class _Entry:
    def __init__(self, entry_id: str = "entry") -> None:
        self.entry_id = entry_id
        self.title = "wastage-test"
        self.domain = DOMAIN
        self.pref_disable_polling = False
        self.unloads: list[Any] = []

    def async_on_unload(self, func: Callable[[], object]) -> None:
        self.unloads.append(func)


class _Hass:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.config = _Config()
        self.data: dict[str, Any] = {}
        self.config_entries: Any = None

    @property
    def is_stopping(self) -> bool:
        return False


class _Config:
    def __init__(self) -> None:
        self.time_zone = "UTC"


class _AirTouch:
    def __init__(self, model: AirTouchModel = AirTouchModel.AIRTOUCH_5) -> None:
        self.host = "127.0.0.1"
        self.model = model
        self.air_conditioners: list[Any] = [object()]


_DEVICE_INFO: dict[str, object] = {
    "identifiers": {("airtouch", "hub")},
    "name": "Console",
}


class _Device:
    unique_id = "hub"

    def __init__(self) -> None:
        self.device_info = _DEVICE_INFO


class SmartSettingsParseTests(unittest.TestCase):
    def test_synthetic_frames_and_information(self) -> None:
        self.assertEqual(ADDRESS_AIRTOUCH_EXTENDED, 0x90)
        self.assertEqual(ADDRESS_CLIENT, 0xB0)
        self.assertEqual(EXTENDED_MESSAGE_ID, 0x1F)
        self.assertEqual(
            _oracle_frame(
                to_address=0x90,
                from_address=0xB0,
                packet_id=7,
                payload=_QUERY,
            ),
            _SAMPLE_REQUEST,
        )
        self.assertEqual(
            _oracle_frame(
                to_address=0xB0,
                from_address=0x90,
                packet_id=7,
                payload=_SAMPLE_PAYLOAD,
            ),
            _SAMPLE_RESPONSE,
        )
        snapshot = parse_smart_settings(_SAMPLE_PAYLOAD[2:])
        self.assertEqual(snapshot.info, _SAMPLE_INFO)
        self.assertFalse(snapshot.energy_wastage)
        self.assertNotIn(_RECORDS, snapshot.info)
        self.assertEqual(snapshot.info[1:5], bytes.fromhex("051b0412"))

    def test_nonzero_extended_and_empty_records(self) -> None:
        info = bytearray(_SAMPLE_INFO)
        info[5] = 0x02
        info[6] = 0xB1
        info[7] = 0xC2
        body = bytes((8, 5, 2)) + bytes(info) + _RECORDS
        snapshot = parse_smart_settings(body)
        self.assertTrue(snapshot.energy_wastage)
        self.assertEqual(snapshot.info[6:], bytes.fromhex("b1c2"))
        extended = _RICH_INFO
        parsed = parse_smart_settings(bytes((len(extended), 0, 2)) + extended)
        self.assertEqual(parsed.info, extended)
        self.assertFalse(parsed.energy_wastage)
        quiet = parse_smart_settings(bytes((len(_SAMPLE_INFO), 0, 0)) + _SAMPLE_INFO)
        self.assertEqual(quiet.info, _SAMPLE_INFO)

    def test_rejects_truncated_and_inconsistent_records(self) -> None:
        cases = (
            b"",
            b"\x08\x05",
            bytes((8, 5, 2)) + _SAMPLE_INFO[:-1],
            bytes((5, 0, 0)) + b"\x01\x02\x03\x04\x05\x01",
            bytes((8, 5, 2)) + _SAMPLE_INFO + _RECORDS[:-1],
            bytes((8, 5, 2)) + _SAMPLE_INFO + _RECORDS + b"\x00",
            bytes((8, 3, 0)) + _SAMPLE_INFO,
            bytes((8, 1, 2)) + _SAMPLE_INFO + b"\x00",
            bytes((0, 0, 0)),
        )
        for body in cases:
            with (
                self.subTest(body=body.hex()),
                self.assertRaises(SmartSettingsParseError),
            ):
                parse_smart_settings(body)


class WastageServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.info = _RICH_INFO
        self.raw: list[bytes] = []
        self.writes: list[bytes] = []
        self.events: list[str] = []
        self.other: list[bytes] = []
        self.connections = 0
        self.active = 0
        self.max_active = 0
        self.dropped = 0
        self.silent_eof: list[bytes] = []
        self.closed_during_hold: list[bytes] = []
        self.watch_close = False
        self.hold_all = False
        self.mode = "ok"
        self.decoy = False
        self.hold_connection = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.settled = asyncio.Event()
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sockets = self.server.sockets
        if not sockets:
            raise RuntimeError("test server did not bind a socket")
        self.port = sockets[0].getsockname()[1]
        self.hass = _Hass()
        self.entry = _Entry()
        self.coordinators: list[SmartSettingsCoordinator] = []

    async def asyncTearDown(self) -> None:
        self.release.set()
        for coordinator in self.coordinators:
            await coordinator.async_shutdown()
        self.server.close()
        await self.server.wait_closed()

    def _coordinator(self, *, deadline: float = 2.0) -> SmartSettingsCoordinator:
        coordinator = async_install(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            port=self.port,
            deadline=deadline,
        )
        if coordinator is None:
            raise AssertionError("expected an AirTouch 5 owner")
        self.coordinators.append(coordinator)
        return coordinator

    def _switch(self, coordinator: SmartSettingsCoordinator) -> EnergyWastageSwitch:
        return EnergyWastageSwitch(coordinator, "hub", cast("Any", _DEVICE_INFO))

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        try:
            raw = await _read_raw(reader)
        except asyncio.IncompleteReadError:
            self.dropped += 1
            writer.close()
            return
        self.raw.append(raw)
        header, payload = _oracle_decode(raw)
        try:
            if self.mode == "silent":
                self.silent_eof.append(await asyncio.wait_for(reader.read(1), 2))
                return
            if self.mode == "truncate":
                writer.write(b"\x55\x55\x55")
                await writer.drain()
                return
            if payload.startswith(b"\xff\x81"):
                reply = payload + b"OK"
            elif self.mode == "malformed":
                self._accept(payload)
                reply = bytes.fromhex("ff18080502")
            else:
                if self.decoy:
                    await self._write_decoys(writer, header.packet_id)
                reply = _status(self._accept(payload))
            closer: asyncio.Task[bytes] | None = None
            if self.watch_close and self.connections == self.hold_connection:
                closer = asyncio.create_task(reader.read(1))
            await self._pause()
            if closer is not None:
                self.closed_during_hold.append(await asyncio.wait_for(closer, 1))
            writer.write(_reply(header.packet_id, reply))
            await writer.drain()
        except (ConnectionError, OSError, TimeoutError):
            self.dropped += 1
        finally:
            self.settled.set()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _write_decoys(self, writer: asyncio.StreamWriter, packet_id: int) -> None:
        lied = bytearray(self.info)
        lied[5] = 1
        writer.write(_reply(packet_id, _status(bytes(lied)), from_address=0x80))
        await writer.drain()
        writer.write(_reply(packet_id, b"\xff\x81\x00"))
        await writer.drain()

    async def _pause(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.hold_all or self.connections == self.hold_connection:
                self.started.set()
                await self.release.wait()
            else:
                await asyncio.sleep(0.01)
        finally:
            self.active -= 1

    def _accept(self, payload: bytes) -> bytes:
        if payload == _QUERY:
            self.events.append("query")
            return self.info
        if (
            len(payload) >= 5
            and payload[:2] == _QUERY
            and payload[3] == 0
            and payload[4] == 0
            and len(payload) == 5 + payload[2]
        ):
            requested = payload[5:]
            self.events.append("write")
            self.writes.append(payload)
            if self.mode == "corrupt":
                flipped = bytearray(requested)
                flipped[5] ^= 0x01
                return bytes(flipped)
            if self.mode == "mismatch":
                return requested
            self.info = requested
            return self.info
        self.events.append("other")
        self.other.append(payload)
        return self.info

    async def test_disabled_weather_differential_blocks_write(self) -> None:
        coordinator = self._coordinator()
        switch = self._switch(coordinator)
        for index in (1, 3):
            with self.subTest(differential_index=index):
                info = bytearray(_SAMPLE_INFO)
                info[index] = 0xFF
                self.info = bytes(info)
                await coordinator.async_refresh()
                snapshot = coordinator.data
                before = len(self.writes)
                self.assertTrue(switch.available)
                self.assertFalse(switch.is_on)
                with self.assertRaisesRegex(
                    HomeAssistantError, "disabled weather differential"
                ):
                    await switch.async_turn_on()
                self.assertEqual(len(self.writes), before)
                self.assertEqual(self.info, bytes(info))
                self.assertIs(coordinator.data, snapshot)

    async def test_query_and_write_frames_keep_the_other_bytes(self) -> None:
        self.decoy = True
        coordinator = self._coordinator()
        switch = self._switch(coordinator)
        await coordinator.async_refresh()
        header, payload = _oracle_decode(self.raw[0])
        self.assertEqual(header.to_address, 0x90)
        self.assertEqual(header.from_address, 0xB0)
        self.assertEqual(header.message_id, 0x1F)
        self.assertEqual(header.packet_id, 1)
        self.assertEqual(payload, _QUERY)
        self.assertEqual(
            self.raw[0],
            _oracle_frame(
                to_address=0x90,
                from_address=0xB0,
                packet_id=1,
                payload=_QUERY,
            ),
        )
        self.assertFalse(switch.is_on)
        self.assertEqual(switch.translation_key, "energy_wastage_detection")
        self.assertEqual(switch.name, "Energy wastage detection")
        self.assertEqual(switch.unique_id, "hub_energy_wastage_detection")
        self.assertTrue(switch.has_entity_name)
        await switch.async_turn_on()
        write_header, write_payload = _oracle_decode(self.raw[2])
        self.assertEqual(write_header.to_address, 0x90)
        self.assertEqual(write_header.from_address, 0xB0)
        self.assertEqual(write_header.message_id, 0x1F)
        self.assertEqual(write_header.packet_id, 3)
        self.assertEqual(write_payload.hex(), "ff180a0000a10318031401b1c2d3e4")
        self.assertNotIn(_RECORDS, write_payload)
        self.assertTrue(switch.is_on)
        self.assertEqual(self.other, [])
        await switch.async_turn_off()
        self.assertEqual(self.writes[-1].hex(), "ff180a0000a10318031400b1c2d3e4")
        self.assertFalse(switch.is_on)
        edited = bytearray(_RICH_INFO)
        edited[5] = 0x02
        self.info = bytes(edited)
        await coordinator.async_refresh()
        self.assertTrue(switch.is_on)
        await switch.async_turn_on()
        self.assertEqual(self.writes[-1].hex(), "ff180a0000a10318031401b1c2d3e4")

    async def test_short_information_is_not_padded(self) -> None:
        self.info = _SHORT_INFO
        coordinator = self._coordinator()
        switch = self._switch(coordinator)
        await coordinator.async_refresh()
        self.assertTrue(switch.is_on)
        await switch.async_turn_off()
        self.assertEqual(self.writes[-1].hex(), "ff18060000a10318031400")
        self.assertEqual(len(self.writes[-1]), 11)
        await switch.async_turn_on()
        self.assertEqual(self.writes[-1].hex(), "ff18060000a10318031401")
        self.assertTrue(switch.is_on)

    async def test_failures_keep_the_confirmed_state(self) -> None:
        coordinator = self._coordinator()
        switch = self._switch(coordinator)
        await coordinator.async_refresh()
        self.mode = "malformed"
        before = self.connections
        with self.assertRaises(HomeAssistantError):
            await switch.async_turn_on()
        self.assertEqual(self.connections, before + 1)
        self.assertEqual(self.writes, [])
        self.assertFalse(switch.is_on)
        self.assertTrue(switch.available)
        self.mode = "corrupt"
        before = self.connections
        with self.assertRaises(HomeAssistantError):
            await switch.async_turn_on()
        self.assertEqual(self.connections, before + 2)
        self.assertFalse(switch.is_on)
        self.assertTrue(coordinator.last_update_success)
        self.mode = "mismatch"
        before = self.connections
        with self.assertRaises(HomeAssistantError):
            await switch.async_turn_on()
        self.assertEqual(self.connections, before + 3)
        self.assertFalse(switch.is_on)
        self.assertTrue(switch.available)
        snapshot = coordinator.data
        assert snapshot is not None
        self.assertEqual(snapshot.info, _RICH_INFO)
        self.mode = "ok"
        before = self.connections
        with self.assertRaises(SmartSettingsRejectedError):
            await coordinator.async_set(enabled=cast("bool", 1))
        self.assertEqual(self.connections, before)

    async def test_state_stays_off_until_readback_matches(self) -> None:
        coordinator = self._coordinator()
        switch = self._switch(coordinator)
        await coordinator.async_refresh()
        self.hold_connection = 4
        task = asyncio.create_task(switch.async_turn_on())
        await asyncio.wait_for(self.started.wait(), 1)
        self.assertEqual(self.writes[-1].hex(), "ff180a0000a10318031401b1c2d3e4")
        self.assertFalse(switch.is_on)
        self.assertTrue(switch.available)
        stored = coordinator.data
        assert stored is not None
        self.assertEqual(stored.info, _RICH_INFO)
        self.release.set()
        await asyncio.wait_for(task, 2)
        self.assertTrue(switch.is_on)
        confirmed = coordinator.data
        assert confirmed is not None
        self.assertEqual(confirmed.info[5], 1)
        self.assertEqual(confirmed.info[:5], _RICH_INFO[:5])
        self.assertEqual(confirmed.info[6:], _RICH_INFO[6:])

    async def test_poll_and_second_change_wait_for_the_transaction(self) -> None:
        coordinator = self._coordinator()
        switch = self._switch(coordinator)
        await coordinator.async_refresh()
        self.hold_connection = 2
        set_task = asyncio.create_task(switch.async_turn_on())
        await asyncio.wait_for(self.started.wait(), 1)
        poll = asyncio.create_task(coordinator.async_refresh())
        await asyncio.sleep(0.05)
        self.assertFalse(poll.done())
        self.assertEqual(self.connections, 2)
        self.assertEqual(self.events, ["query", "query"])
        self.release.set()
        await asyncio.wait_for(set_task, 2)
        await asyncio.wait_for(poll, 2)
        self.assertEqual(
            self.events,
            ["query", "query", "write", "query", "query"],
        )
        self.assertEqual(self.max_active, 1)
        self.events.clear()
        self.max_active = 0
        await asyncio.gather(switch.async_turn_off(), switch.async_turn_on())
        self.assertEqual(
            self.events,
            ["query", "write", "query", "query", "write", "query"],
        )
        self.assertEqual(self.max_active, 1)
        self.assertEqual(self.other, [])
        for payload in self.writes:
            self.assertEqual(payload[:2], b"\xff\x18")
            self.assertEqual(payload[3:5], b"\x00\x00")
            self.assertEqual(len(payload), 5 + payload[2])
            self.assertNotIn(_RECORDS, payload)

    async def test_timeout_closes_and_keeps_the_confirmed_state(self) -> None:
        coordinator = self._coordinator(deadline=0.4)
        switch = self._switch(coordinator)
        await coordinator.async_refresh()
        self.mode = "silent"
        self.settled = asyncio.Event()
        with self.assertRaises(HomeAssistantError):
            await switch.async_turn_on()
        await asyncio.wait_for(self.settled.wait(), 2)
        self.assertEqual(self.silent_eof, [b""])
        self.assertFalse(switch.is_on)
        self.assertTrue(switch.available)
        self.assertEqual(self.writes, [])
        self.mode = "ok"
        await switch.async_turn_on()
        self.assertTrue(switch.is_on)

    async def test_cancel_closes_and_releases_the_lock(self) -> None:
        coordinator = self._coordinator()
        switch = self._switch(coordinator)
        await coordinator.async_refresh()
        self.hold_connection = 2
        self.watch_close = True
        self.settled = asyncio.Event()
        task = asyncio.create_task(switch.async_turn_on())
        await asyncio.wait_for(self.started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.release.set()
        await asyncio.wait_for(self.settled.wait(), 2)
        self.assertEqual(self.closed_during_hold, [b""])
        self.hold_connection = 0
        self.watch_close = False
        await asyncio.wait_for(switch.async_turn_on(), 2)
        self.assertTrue(switch.is_on)

    async def test_periodic_read_failure_keeps_the_snapshot_unavailable(self) -> None:
        coordinator = self._coordinator()
        switch = self._switch(coordinator)
        await coordinator.async_refresh()
        self.mode = "truncate"
        await coordinator.async_refresh()
        self.assertFalse(coordinator.last_update_success)
        self.assertFalse(switch.available)
        self.assertIsNone(switch.is_on)
        stored = coordinator.data
        assert stored is not None
        self.assertEqual(stored.info, _RICH_INFO)
        self.mode = "malformed"
        await coordinator.async_refresh()
        self.assertFalse(switch.available)
        self.assertEqual(coordinator.data, stored)
        self.mode = "ok"
        await coordinator.async_refresh()
        self.assertTrue(switch.available)
        self.assertFalse(switch.is_on)

    async def test_default_request_still_strips_the_full_echo(self) -> None:
        client = ConsoleClient("127.0.0.1", self.port, deadline=2)
        body = await client.request(b"\xff\x81" + b"2510")
        self.assertEqual(body, b"OK")
        header, payload = _oracle_decode(self.raw[0])
        self.assertEqual(header.to_address, 0x90)
        self.assertEqual(header.from_address, 0xB0)
        self.assertEqual(header.message_id, 0x1F)
        self.assertEqual(header.packet_id, 1)
        self.assertEqual(payload, b"\xff\x812510")

    async def test_airtouch_4_failed_setup_and_unload(self) -> None:
        entities = await async_collect_energy_wastage_switches(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch(AirTouchModel.AIRTOUCH_4)),
            unique_id="hub",
            device_info=cast("Any", _DEVICE_INFO),
        )
        self.assertEqual(entities, [])
        self.assertEqual(self.connections, 0)
        touch = _AirTouch(AirTouchModel.AIRTOUCH_4)
        self.hass.data.setdefault(DOMAIN, {})[self.entry.entry_id] = touch
        added: list[list[Entity]] = []

        # Home Assistant AddEntitiesCallback: Iterable[Entity], update_before_add=False.
        def add_entities(
            new_entities: Iterable[Entity],
            update_before_add: bool = False,  # noqa: FBT001, FBT002
        ) -> None:
            del update_before_add
            added.append(list(new_entities))

        await async_setup_entry(
            cast("Any", self.hass), cast("Any", self.entry), add_entities
        )
        self.assertEqual(added, [[]])
        self.assertIs(self.hass.data[DOMAIN][self.entry.entry_id], touch)
        self.mode = "truncate"
        live = _AirTouch()
        self.hass.data[DOMAIN][self.entry.entry_id] = live
        installed = async_install(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", live),
            port=self.port,
        )
        if installed is None:
            raise AssertionError("expected an AirTouch 5 owner")
        self.coordinators.append(installed)
        seen = self.connections
        with patch(
            "custom_components.airtouch.switch.devices.AirTouchDevice",
            return_value=_Device(),
        ):
            await async_setup_entry(
                cast("Any", self.hass), cast("Any", self.entry), add_entities
            )
        self.assertEqual(self.connections, seen)
        self.assertEqual(len(added[1]), 1)
        switch = added[1][0]
        assert isinstance(switch, EnergyWastageSwitch)
        self.assertFalse(switch.available)
        self.assertEqual(switch.unique_id, "hub_energy_wastage_detection")
        self.assertIs(switch.coordinator, installed)
        self.assertIs(self.hass.data[DOMAIN][self.entry.entry_id], live)
        self.assertEqual(set(self.hass.data[DOMAIN]), {self.entry.entry_id})
        await installed.async_refresh()
        self.assertGreater(self.connections, seen)
        self.assertFalse(switch.available)
        await async_release(cast("Any", self.hass), self.entry.entry_id)
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        self.assertEqual(coordinator.update_interval, timedelta(seconds=60))
        self.assertIn(coordinator.async_shutdown, self.entry.unloads)
        seen = self.connections
        for callback in list(self.entry.unloads):
            result = callback()
            if asyncio.iscoroutine(result):
                await result
        await coordinator.async_refresh()
        self.assertEqual(self.connections, seen)

    async def test_legacy_constructor_keeps_the_entry_unload_binding(self) -> None:
        original_init = DataUpdateCoordinator.__init__

        def legacy_init(
            instance: DataUpdateCoordinator[SmartSettingsSnapshot],
            hass: HomeAssistant,
            logger: Logger,
            *,
            name: str,
            update_interval: timedelta | None = None,
        ) -> None:
            original_init(
                instance, hass, logger, name=name, update_interval=update_interval
            )

        with patch.object(DataUpdateCoordinator, "__init__", legacy_init):
            coordinator = self._coordinator()
        self.assertIs(coordinator.config_entry, self.entry)
        self.assertIn(coordinator.async_shutdown, self.entry.unloads)

    async def test_switch_setup_does_not_construct_an_owner(self) -> None:
        live = _AirTouch()
        self.hass.data.setdefault(DOMAIN, {})[self.entry.entry_id] = live
        added: list[list[Entity]] = []

        def add_entities(
            new_entities: Iterable[Entity],
            update_before_add: bool = False,  # noqa: FBT001, FBT002
        ) -> None:
            del update_before_add
            added.append(list(new_entities))

        with (
            patch(
                "custom_components.airtouch.switch.devices.AirTouchDevice",
                return_value=_Device(),
            ),
            self.assertRaisesRegex(RuntimeError, "not installed"),
        ):
            await async_setup_entry(
                cast("Any", self.hass), cast("Any", self.entry), add_entities
            )
        self.assertEqual(added, [])
        self.assertEqual(self.connections, 0)
        self.assertIs(self.hass.data[DOMAIN][self.entry.entry_id], live)

    async def test_install_identity_and_airtouch_4(self) -> None:
        before = self.connections
        owner = async_install(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            port=self.port,
        )
        if owner is None:
            raise AssertionError("expected an AirTouch 5 owner")
        self.coordinators.append(owner)
        self.assertEqual(self.connections, before)
        self.assertIs(
            get_smart_settings(cast("Any", self.hass), self.entry.entry_id), owner
        )
        with self.assertRaisesRegex(RuntimeError, "already installed"):
            async_install(
                cast("Any", self.hass),
                cast("Any", self.entry),
                cast("Any", _AirTouch()),
                port=self.port,
            )
        self.assertIsNone(
            async_install(
                cast("Any", self.hass),
                cast("Any", _Entry("airtouch-4")),
                cast("Any", _AirTouch(AirTouchModel.AIRTOUCH_4)),
                port=self.port,
            )
        )
        self.assertEqual(self.connections, before)
        api_marker = object()
        self.hass.data[DOMAIN] = {"lock": object(), self.entry.entry_id: api_marker}
        self.assertIs(
            get_smart_settings(cast("Any", self.hass), self.entry.entry_id), owner
        )
        self.assertEqual(set(self.hass.data[DOMAIN]), {"lock", self.entry.entry_id})
        self.assertIs(self.hass.data[DOMAIN][self.entry.entry_id], api_marker)

    async def test_two_entries_release_reload_and_overlap(self) -> None:
        left_entry = _Entry("left")
        right_entry = _Entry("right")
        left = async_install(
            cast("Any", self.hass),
            cast("Any", left_entry),
            cast("Any", _AirTouch()),
            port=self.port,
        )
        right = async_install(
            cast("Any", self.hass),
            cast("Any", right_entry),
            cast("Any", _AirTouch()),
            port=self.port,
        )
        if left is None or right is None:
            raise AssertionError("expected two owners")
        self.coordinators.extend((left, right))
        self.assertIsNot(left, right)
        self.hass.data[DOMAIN] = {
            "lock": object(),
            left_entry.entry_id: object(),
            right_entry.entry_id: object(),
        }
        domain_ids = set(self.hass.data[DOMAIN])
        await async_release(cast("Any", self.hass), left_entry.entry_id)
        await async_release(cast("Any", self.hass), left_entry.entry_id)
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_smart_settings(cast("Any", self.hass), left_entry.entry_id)
        self.assertIs(
            get_smart_settings(cast("Any", self.hass), right_entry.entry_id), right
        )
        self.assertEqual(set(self.hass.data[DOMAIN]), domain_ids)
        seen = self.connections
        await left.async_refresh()
        self.assertEqual(self.connections, seen)
        await right.async_refresh()
        self.assertGreater(self.connections, seen)
        reloaded = async_install(
            cast("Any", self.hass),
            cast("Any", left_entry),
            cast("Any", _AirTouch()),
            port=self.port,
        )
        if reloaded is None:
            raise AssertionError("expected a reloaded owner")
        self.coordinators.append(reloaded)
        self.assertIsNot(reloaded, left)
        self.assertIs(
            get_smart_settings(cast("Any", self.hass), left_entry.entry_id), reloaded
        )
        self.assertIs(
            get_smart_settings(cast("Any", self.hass), right_entry.entry_id), right
        )
        self.hold_all = True
        self.max_active = 0
        both = asyncio.gather(reloaded.async_refresh(), right.async_refresh())
        await self._wait_until_active(2)
        self.assertEqual(self.max_active, 2)
        self.release.set()
        await asyncio.wait_for(both, 2)

    async def test_shutdown_drains_admitted_work_and_refuses_late_writes(self) -> None:
        coordinator = self._coordinator()
        switch = self._switch(coordinator)
        await coordinator.async_refresh()
        self.hold_connection = 2
        self.watch_close = False
        admitted = asyncio.create_task(switch.async_turn_on())
        await asyncio.wait_for(self.started.wait(), 1)
        queued = asyncio.create_task(switch.async_turn_off())
        poll = asyncio.create_task(coordinator.async_refresh())
        await asyncio.sleep(0.05)
        self.assertFalse(queued.done())
        self.assertFalse(poll.done())
        self.assertEqual(self.writes, [])
        self.assertEqual(self.events, ["query", "query"])
        closing = asyncio.create_task(
            async_release(cast("Any", self.hass), self.entry.entry_id)
        )
        await asyncio.sleep(0.05)
        self.assertFalse(admitted.done())
        self.assertFalse(closing.done())
        self.release.set()
        await asyncio.wait_for(admitted, 2)
        with self.assertRaises(HomeAssistantError):
            await asyncio.wait_for(queued, 2)
        await asyncio.wait_for(poll, 2)
        await asyncio.wait_for(closing, 2)
        self.assertEqual(self.events, ["query", "query", "write", "query"])
        self.assertEqual(self.writes[-1].hex(), "ff180a0000a10318031401b1c2d3e4")
        self.assertEqual(self.max_active, 1)
        seen = self.connections
        with self.assertRaises(HomeAssistantError):
            await switch.async_turn_on()
        with self.assertRaises(HomeAssistantError):
            await switch.async_turn_off()
        self.assertEqual(self.connections, seen)
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_smart_settings(cast("Any", self.hass), self.entry.entry_id)

    async def _wait_until_active(self, count: int) -> None:
        for _ in range(50):
            if self.active >= count:
                return
            await asyncio.sleep(0.02)
        self.fail(f"timed out waiting for {count} active connections")

    def _bind_root(self, api: _Api) -> tuple[Any, Any]:
        def connect(*, model: object, host: str) -> _Api:
            del model, host
            return api

        def factory(
            host: str, port: int, *, deadline: float = DEFAULT_DEADLINE
        ) -> ConsoleClient:
            del port
            return ConsoleClient(host, self.port, deadline=deadline)

        return (
            patch("custom_components.airtouch.pyairtouch.connect", connect),
            patch(
                "custom_components.airtouch.smart_settings.ConsoleClient",
                factory,
            ),
        )

    async def test_setup_orders_install_forward_and_nonfatal_refresh(self) -> None:
        api = _Api(AirTouchModel.AIRTOUCH_5)
        entry = _RootEntry("root", SelectedModel.AIRTOUCH_5.value)
        forwarded: list[str] = []

        async def forward(config_entry: object, platforms: object) -> None:
            del platforms
            forwarded.append("forward")
            self.assertEqual(self.connections, 0)
            owner = get_smart_settings(cast("Any", self.hass), entry.entry_id)
            self.coordinators.append(owner)
            self.assertIs(config_entry, entry)

        self.hass.config_entries = _ConfigEntries(forward)
        connect, client = self._bind_root(api)
        with connect, client:
            result = await async_setup_integration(
                cast("Any", self.hass), cast("Any", entry)
            )
        self.assertTrue(result)
        self.assertEqual(forwarded, ["forward"])
        self.assertGreater(self.connections, 0)
        owner = get_smart_settings(cast("Any", self.hass), entry.entry_id)
        self.assertTrue(owner.last_update_success)
        self.assertIs(self.hass.data[DOMAIN][entry.entry_id], api)
        self.assertEqual(api.shutdown_calls, 0)
        stored = owner.data
        assert stored is not None
        self.assertEqual(stored.info, _RICH_INFO)

        seen = self.connections
        self.mode = "truncate"
        failed_entry = _RootEntry("failed-read", SelectedModel.AIRTOUCH_5.value)

        async def forward_again(config_entry: object, platforms: object) -> None:
            del config_entry, platforms
            self.assertEqual(self.connections, seen)

        self.hass.config_entries = _ConfigEntries(forward_again)
        connect, client = self._bind_root(api)
        with connect, client:
            result = await async_setup_integration(
                cast("Any", self.hass), cast("Any", failed_entry)
            )
        self.assertTrue(result)
        failed = get_smart_settings(cast("Any", self.hass), failed_entry.entry_id)
        self.coordinators.append(failed)
        self.assertFalse(failed.last_update_success)
        self.assertGreater(self.connections, seen)

    async def test_setup_failure_and_cancellation_release_only_the_owner(self) -> None:
        api = _Api(AirTouchModel.AIRTOUCH_5)
        entry = _RootEntry("root", SelectedModel.AIRTOUCH_5.value)

        async def fail_forward(config_entry: object, platforms: object) -> None:
            del platforms
            self.assertIsNotNone(
                get_smart_settings(
                    cast("Any", self.hass), cast("Any", config_entry).entry_id
                )
            )
            self.assertEqual(self.connections, 0)
            raise RuntimeError("forward failed")

        self.hass.config_entries = _ConfigEntries(fail_forward)
        connect, client = self._bind_root(api)
        with (
            connect,
            client,
            self.assertRaisesRegex(RuntimeError, "forward failed"),
        ):
            await async_setup_integration(cast("Any", self.hass), cast("Any", entry))
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_smart_settings(cast("Any", self.hass), entry.entry_id)
        self.assertIs(self.hass.data[DOMAIN][entry.entry_id], api)
        self.assertEqual(api.shutdown_calls, 0)
        self.assertEqual(self.connections, 0)

    async def test_release_failure_preserves_the_setup_error(self) -> None:
        api = _Api(AirTouchModel.AIRTOUCH_5)

        async def fail_forward(config_entry: object, platforms: object) -> None:
            del platforms
            self.assertIsNotNone(
                get_smart_settings(
                    cast("Any", self.hass), cast("Any", config_entry).entry_id
                )
            )
            raise RuntimeError("forward failed")

        async def bad_release(hass: object, entry_id: str) -> None:
            del hass, entry_id
            raise RuntimeError("release failed")

        preserved = _RootEntry("preserved", SelectedModel.AIRTOUCH_5.value)
        self.hass.config_entries = _ConfigEntries(fail_forward)
        connect, client = self._bind_root(api)
        with (
            connect,
            client,
            patch("custom_components.airtouch.async_release", bad_release),
            self.assertLogs("custom_components.airtouch", level="ERROR") as logged,
            self.assertRaisesRegex(RuntimeError, "forward failed"),
        ):
            await async_setup_integration(
                cast("Any", self.hass), cast("Any", preserved)
            )
        self.assertIn("Error releasing AirTouch smart settings", logged.output[0])
        self.assertIs(self.hass.data[DOMAIN][preserved.entry_id], api)
        await async_release(cast("Any", self.hass), preserved.entry_id)

    async def test_setup_cancellation_releases_the_owner(self) -> None:
        api = _Api(AirTouchModel.AIRTOUCH_5)
        cancelled = _RootEntry("cancelled", SelectedModel.AIRTOUCH_5.value)
        started = asyncio.Event()

        async def wait_forward(config_entry: object, platforms: object) -> None:
            del config_entry, platforms
            started.set()
            await asyncio.Event().wait()

        self.hass.config_entries = _ConfigEntries(wait_forward)
        connect, client = self._bind_root(api)
        with connect, client:
            task = asyncio.create_task(
                async_setup_integration(cast("Any", self.hass), cast("Any", cancelled))
            )
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(self.connections, 0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_smart_settings(cast("Any", self.hass), cancelled.entry_id)
        self.assertIs(self.hass.data[DOMAIN][cancelled.entry_id], api)
        self.assertEqual(api.shutdown_calls, 0)

        self.hold_connection = 1
        self.watch_close = True
        self.settled = asyncio.Event()
        refresh_entry = _RootEntry("refresh-cancel", SelectedModel.AIRTOUCH_5.value)
        self.hass.config_entries = _ConfigEntries(None)
        connect, client = self._bind_root(api)
        with connect, client:
            refresh_task = asyncio.create_task(
                async_setup_integration(
                    cast("Any", self.hass), cast("Any", refresh_entry)
                )
            )
            await asyncio.wait_for(self.started.wait(), 1)
            refresh_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await refresh_task
        self.release.set()
        await asyncio.wait_for(self.settled.wait(), 2)
        self.assertEqual(self.closed_during_hold, [b""])
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_smart_settings(cast("Any", self.hass), refresh_entry.entry_id)
        self.assertIs(self.hass.data[DOMAIN][refresh_entry.entry_id], api)
        self.assertEqual(api.shutdown_calls, 0)

    async def test_unload_false_keeps_both_and_success_releases_first(self) -> None:
        api = _Api(AirTouchModel.AIRTOUCH_5)
        entry = _RootEntry("root", SelectedModel.AIRTOUCH_5.value)
        self.hass.config_entries = _ConfigEntries(None, unload_ok=False)
        connect, client = self._bind_root(api)
        with connect, client:
            self.assertTrue(
                await async_setup_integration(
                    cast("Any", self.hass), cast("Any", entry)
                )
            )
        owner = get_smart_settings(cast("Any", self.hass), entry.entry_id)
        self.coordinators.append(owner)
        self.assertFalse(
            await async_unload_entry(cast("Any", self.hass), cast("Any", entry))
        )
        self.assertIs(get_smart_settings(cast("Any", self.hass), entry.entry_id), owner)
        self.assertIs(self.hass.data[DOMAIN][entry.entry_id], api)
        self.assertEqual(api.shutdown_calls, 0)
        seen = self.connections
        await owner.async_refresh()
        self.assertGreater(self.connections, seen)

        async def tracked_release(hass: object, entry_id: str) -> None:
            api.events.append("release-start")
            await async_release(cast("Any", hass), entry_id)
            api.events.append("release-done")

        self.hass.config_entries.unload_ok = True
        with patch("custom_components.airtouch.async_release", tracked_release):
            removed = await async_unload_entry(
                cast("Any", self.hass), cast("Any", entry)
            )
        self.assertTrue(removed)
        self.assertEqual(api.events, ["release-start", "release-done", "api-shutdown"])
        self.assertNotIn(entry.entry_id, self.hass.data[DOMAIN])
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_smart_settings(cast("Any", self.hass), entry.entry_id)

        fourth = _Api(AirTouchModel.AIRTOUCH_4)
        at4 = _RootEntry("at4", SelectedModel.AIRTOUCH_4.value)
        seen = self.connections
        self.hass.config_entries = _ConfigEntries(None)
        connect, client = self._bind_root(fourth)
        with connect, client:
            self.assertTrue(
                await async_setup_integration(cast("Any", self.hass), cast("Any", at4))
            )
        self.assertEqual(self.connections, seen)
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_smart_settings(cast("Any", self.hass), at4.entry_id)
        self.assertIs(self.hass.data[DOMAIN][at4.entry_id], fourth)


class _Api:
    def __init__(self, model: AirTouchModel) -> None:
        self.model = model
        self.host = "127.0.0.1"
        self.shutdown_calls = 0
        self.events: list[str] = []

    async def init(self) -> bool:
        return True

    async def shutdown(self) -> None:
        self.events.append("api-shutdown")
        self.shutdown_calls += 1


class _RootEntry:
    def __init__(self, entry_id: str, model: str) -> None:
        self.entry_id = entry_id
        self.title = "root"
        self.domain = DOMAIN
        self.version = 2
        self.minor_version = 2
        self.unique_id = entry_id
        self.data = {CONF_HOST: "127.0.0.1", CONF_AIRTOUCH_MODEL: model}
        self.pref_disable_polling = False
        self.unloads: list[Any] = []

    def async_on_unload(self, func: Callable[[], object]) -> None:
        self.unloads.append(func)


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


class WastageMetadataTests(unittest.TestCase):
    def test_strings_and_icon(self) -> None:
        strings = json.loads((_COMPONENT / "strings.json").read_text())
        english = json.loads((_COMPONENT / "translations" / "en.json").read_text())
        icons = json.loads((_COMPONENT / "icons.json").read_text())
        name = "Energy wastage detection"
        self.assertEqual(
            strings["entity"]["switch"]["energy_wastage_detection"]["name"],
            name,
        )
        self.assertEqual(
            english["entity"]["switch"]["energy_wastage_detection"]["name"],
            name,
        )
        self.assertEqual(
            icons["entity"]["switch"]["energy_wastage_detection"]["default"],
            "mdi:window-open",
        )


if __name__ == "__main__":
    unittest.main()
