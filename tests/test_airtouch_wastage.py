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

from custom_components.airtouch.console import ConsoleClient
from custom_components.airtouch.const import DOMAIN
from custom_components.airtouch.smart_settings import (
    EnergyWastageCoordinator,
    SmartSettingsParseError,
    SmartSettingsRejectedError,
    SmartSettingsSnapshot,
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
    def __init__(self) -> None:
        self.entry_id = "entry"
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
        self.coordinators: list[EnergyWastageCoordinator] = []

    async def asyncTearDown(self) -> None:
        self.release.set()
        for coordinator in self.coordinators:
            await coordinator.async_shutdown()
        self.server.close()
        await self.server.wait_closed()

    def _coordinator(self, *, deadline: float = 2.0) -> EnergyWastageCoordinator:
        coordinator = EnergyWastageCoordinator(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            port=self.port,
            deadline=deadline,
        )
        self.coordinators.append(coordinator)
        return coordinator

    def _switch(self, coordinator: EnergyWastageCoordinator) -> EnergyWastageSwitch:
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
            if self.connections == self.hold_connection:
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
        for index in (1, 3):
            with self.subTest(differential_index=index):
                info = bytearray(_SAMPLE_INFO)
                info[index] = 0xFF
                self.info = bytes(info)
                coordinator = self._coordinator()
                await coordinator.async_refresh()
                switch = self._switch(coordinator)
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
        with patch(
            "custom_components.airtouch.switch.devices.AirTouchDevice",
            return_value=_Device(),
        ):
            await async_setup_entry(
                cast("Any", self.hass), cast("Any", self.entry), add_entities
            )
        self.assertEqual(len(added[1]), 1)
        self.assertFalse(added[1][0].available)
        self.assertEqual(added[1][0].unique_id, "hub_energy_wastage_detection")
        self.assertIs(self.hass.data[DOMAIN][self.entry.entry_id], live)
        self.assertEqual(set(self.hass.data[DOMAIN]), {self.entry.entry_id})
        switch = added[1][0]
        assert isinstance(switch, EnergyWastageSwitch)
        await switch.coordinator.async_shutdown()
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
