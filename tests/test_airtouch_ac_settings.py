from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import struct
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from homeassistant.components.number import NumberEntity
    from homeassistant.helpers.entity import Entity

from homeassistant.components import number as number_component
from homeassistant.components.number import NumberDeviceClass, NumberMode
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pyairtouch import AcFanSpeed, AcMode, AirTouchModel
from pyairtouch.at5.comms.hdr import At5Header, HeaderDecoder, HeaderEncoder
from pyairtouch.at5.comms.xC0_ctrl_status import MESSAGE_ID as CONTROL_MESSAGE_ID
from pyairtouch.comms.crc16 import Crc16Modbus

from custom_components.airtouch import (
    async_setup_entry as async_setup_integration,
)
from custom_components.airtouch import (
    async_unload_entry as async_unload_integration,
)
from custom_components.airtouch import (
    climate,
)
from custom_components.airtouch.ac_settings import (
    AcSettingField,
    AcSettingNumber,
    AcSettingsCoordinator,
    AcSettingsParseError,
    AcSettingsRejectedError,
    async_collect_ac_setting_numbers,
    ensure_ac_settings,
    parse_ac_settings_report,
)
from custom_components.airtouch.const import DOMAIN
from custom_components.airtouch.const import AirTouchModel as SelectedModel
from custom_components.airtouch.number import PreferenceNumber, async_setup_entry
from custom_components.airtouch.preferences import (
    async_install_preferences,
    async_release_preferences,
)
from custom_components.airtouch.smart_settings import async_install, async_release

_OUTER = b"\x55\x55\x55\xab"
_ENCODER = HeaderEncoder()
_DECODER = HeaderDecoder()
_CRC = Crc16Modbus()
_QUERY = bytes.fromhex("5b00000000000000")
_HEADER = bytes.fromhex("02000499eeef")
_RECORD_0 = bytes.fromhex(
    "a011020301020677f01f11223344556620101e101c121a1145617374abcdef"
)
_RECORD_1 = bytes.fromhex(
    "b121040503040888352f12233445566720101e101b13191257657374badcfe"
)
_COOL_MIN_WRITE = bytes.fromhex(
    "5a000006001f0001"
    "02000499eeef"
    "a011020301020677f01f"
    "112233445566"
    "20101e101c131a11"
    "45617374abcdef"
)
_NAMES = {
    AcSettingField.COOL_MINIMUM: "Cool minimum",
    AcSettingField.COOL_MAXIMUM: "Cool maximum",
    AcSettingField.HEAT_MINIMUM: "Heat minimum",
    AcSettingField.HEAT_MAXIMUM: "Heat maximum",
    AcSettingField.AUTO_OFF_DURATION: "Auto off duration",
}
_ICONS = {
    "cool_minimum": "mdi:thermometer-chevron-down",
    "cool_maximum": "mdi:thermometer-chevron-up",
    "heat_minimum": "mdi:thermometer-lines",
    "heat_maximum": "mdi:radiator",
    "auto_off_duration": "mdi:timer-sand",
}


class _NumberServiceModule(Protocol):
    async def async_set_value(
        self, entity: NumberEntity, service_call: ServiceCall
    ) -> None: ...


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
        message_id=CONTROL_MESSAGE_ID,
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
    restored = (
        _OUTER
        + b"\x00\x00"
        + struct.pack("!HH", len(logical), len(logical))
        + bytes(logical)
    )
    decoded = _DECODER.decode(restored[: _DECODER.header_length])
    decoded.assert_complete()
    rest = bytes(logical)[10:]
    payload = rest[: decoded.header.message_length]
    return decoded.header, payload


def _reply(packet_id: int, payload: bytes) -> bytes:
    return _oracle_frame(
        to_address=0xB0,
        from_address=0x80,
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
        self.title = "ac-settings-test"
        self.domain = DOMAIN
        self.version = 2
        self.minor_version = 2
        self.data = {
            "host": "127.0.0.1",
            "airtouch_model": SelectedModel.AIRTOUCH_5.value,
        }
        self.options: dict[str, Any] = {}
        self.pref_disable_polling = False
        self.unloads: list[Any] = []
        self.listeners: list[Any] = []

    def async_on_unload(self, func: Callable[[], object]) -> None:
        self.unloads.append(func)

    def add_update_listener(self, listener: object) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: None


class _Hass:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.config = _Config()
        self.data: dict[str, Any] = {}
        self.config_entries = SimpleNamespace()

    @property
    def is_stopping(self) -> bool:
        return False


class _Config:
    def __init__(self) -> None:
        self.time_zone = "UTC"
        self.units = METRIC_SYSTEM


class _Ac:
    def __init__(self, ac_id: int, name: str) -> None:
        self.ac_id = ac_id
        self.name = name
        self.zones: list[object] = []
        self.supported_modes = [AcMode.COOL, AcMode.HEAT]
        self.supported_fan_speeds = [AcFanSpeed.LOW]
        self.supported_power_controls: list[object] = []
        self.target_temperature_resolution = 1.0

    def subscribe_ac_state(self, callback: object) -> None:
        del callback

    def unsubscribe_ac_state(self, callback: object) -> None:
        del callback


class _AirTouch:
    def __init__(
        self,
        model: AirTouchModel = AirTouchModel.AIRTOUCH_5,
        acs: list[Any] | None = None,
    ) -> None:
        self.host = "127.0.0.1"
        self.model = model
        self.airtouch_id = "hub"
        self.name = "Hub"
        self.connected = True
        self.air_conditioners = [object(), object(), object()] if acs is None else acs

    async def init(self) -> bool:
        return True

    async def shutdown(self) -> None:
        self.connected = False

    def subscribe(self, callback: object) -> None:
        del callback

    def unsubscribe(self, callback: object) -> None:
        del callback


class _AcDevice:
    def __init__(self, ac: _Ac) -> None:
        self.unique_id = f"hub_ac{ac.ac_id}"
        self.device_info = {
            "identifiers": {("airtouch", self.unique_id)},
            "name": ac.name,
        }


class _Console:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def ac_device(self, ac: _Ac) -> _AcDevice:
        return _AcDevice(ac)


class _Platform:
    def async_register_entity_service(self, *args: object, **kwargs: object) -> None:
        del args, kwargs


def _device_info(ac_id: int) -> dict[str, object]:
    return {"identifiers": {("airtouch", f"hub_ac{ac_id}")}, "name": f"AC {ac_id}"}


class AcSettingsReportTests(unittest.TestCase):
    def test_two_records_keep_opaque_bytes_and_masked_ids(self) -> None:
        payload = (
            struct.pack("!BxHHH", 0x5B, len(_HEADER), len(_RECORD_0), 2)
            + _HEADER
            + _RECORD_0
            + _RECORD_1
        )
        snapshot = parse_ac_settings_report(payload)
        self.assertEqual(snapshot.header, _HEADER)
        self.assertEqual(snapshot.records[0].raw, _RECORD_0)
        self.assertEqual(snapshot.records[1].raw, _RECORD_1)
        self.assertEqual(snapshot.records[0].ac_id, 0)
        self.assertEqual(snapshot.records[1].ac_id, 1)
        self.assertEqual(snapshot.records[0].raw[24:28], b"East")
        self.assertEqual(snapshot.records[1].raw[24:28], b"West")
        self.assertEqual(snapshot.records[0].raw[-3:], bytes.fromhex("abcdef"))
        east = snapshot.records[0]
        self.assertEqual(east.value(AcSettingField.COOL_MINIMUM), 18)
        self.assertEqual(east.value(AcSettingField.COOL_MAXIMUM), 28)
        self.assertEqual(east.value(AcSettingField.HEAT_MINIMUM), 17)
        self.assertEqual(east.value(AcSettingField.HEAT_MAXIMUM), 26)
        self.assertEqual(east.value(AcSettingField.AUTO_OFF_DURATION), 0)
        self.assertEqual(east.reported_limits(AcSettingField.COOL_MINIMUM), (16, 32))
        self.assertEqual(east.reported_limits(AcSettingField.HEAT_MAXIMUM), (16, 30))
        west = snapshot.records[1]
        self.assertEqual(west.value(AcSettingField.AUTO_OFF_DURATION), 5)
        self.assertEqual(west.value(AcSettingField.COOL_MINIMUM), 19)
        self.assertEqual(west.value(AcSettingField.COOL_MAXIMUM), 27)
        self.assertEqual(west.value(AcSettingField.HEAT_MINIMUM), 18)
        self.assertEqual(west.value(AcSettingField.HEAT_MAXIMUM), 25)
        self.assertIsNone(snapshot.record(7))

    def test_hand_built_cool_minimum_payload_changes_one_byte(self) -> None:
        self.assertEqual(_RECORD_0[8], 0xF0)
        self.assertEqual(_RECORD_0[21], 0x12)
        self.assertEqual(_COOL_MIN_WRITE[35], 0x13)
        self.assertEqual(_COOL_MIN_WRITE[34], 0x1C)
        patched = bytearray(_RECORD_0)
        patched[21] = 0x13
        self.assertEqual(
            _COOL_MIN_WRITE,
            bytes.fromhex("5a000006001f0001") + _HEADER + bytes(patched),
        )
        self.assertEqual(len(_COOL_MIN_WRITE), 45)
        self.assertNotIn(b"West", _COOL_MIN_WRITE)
        changed = [
            index
            for index, byte in enumerate(_COOL_MIN_WRITE[14:])
            if byte != _RECORD_0[index]
        ]
        self.assertEqual(changed, [21])

    def test_minimum_record_and_equal_brand_bounds(self) -> None:
        header = bytes((1, 0, 0, 0xAB))
        record = bytearray(24)
        record[0] = 0x20
        record[8] = 0x08
        record[16:24] = bytes((20, 20, 18, 18, 20, 20, 18, 18))
        payload = struct.pack("!BxHHH", 0x5B, 4, 24, 1) + header + bytes(record)
        snapshot = parse_ac_settings_report(payload)
        self.assertEqual(snapshot.header, header)
        parsed = snapshot.records[0]
        self.assertEqual(parsed.ac_id, 0)
        self.assertEqual(parsed.value(AcSettingField.COOL_MINIMUM), 20)
        self.assertEqual(parsed.value(AcSettingField.AUTO_OFF_DURATION), 8)
        outside = bytearray(record)
        outside[20] = 21
        self.assertIsNone(
            parse_ac_settings_report(
                struct.pack("!BxHHH", 0x5B, 4, 24, 1) + header + bytes(outside)
            )
            .records[0]
            .value(AcSettingField.COOL_MAXIMUM)
        )

    def test_malformed_reports_are_rejected(self) -> None:
        header = bytes((1, 0, 0, 0))
        record = bytes(24)
        valid = struct.pack("!BxHHH", 0x5B, 4, 24, 1) + header + record
        cases = (
            b"\x5b\x00",
            struct.pack("!BxHHH", 0x5A, 0, 0, 0),
            struct.pack("!BxHHH", 0x5B, 3, 0, 0) + b"\x00\x00\x00",
            valid + b"\x00",
            struct.pack("!BxHHH", 0x5B, 4, 24, 0) + header,
            struct.pack("!BxHHH", 0x5B, 4, 24, 2) + header + record + record,
            struct.pack("!BxHHH", 0x5B, 4, 27, 1) + bytes((1, 0, 4, 0)) + bytes(27),
            struct.pack("!BxHHH", 0x5B, 4, 24, 2)
            + bytes((2, 0, 0, 0))
            + bytes([0xA0])
            + bytes(23)
            + bytes([0xB0])
            + bytes(23),
        )
        for payload in cases:
            with (
                self.subTest(payload=payload.hex()),
                self.assertRaises(AcSettingsParseError),
            ):
                parse_ac_settings_report(payload)

    def test_metadata_names_and_icons_match(self) -> None:
        root = Path(__file__).parents[1]
        strings = json.loads(
            (root / "custom_components/airtouch/strings.json").read_text()
        )
        english = json.loads(
            (root / "custom_components/airtouch/translations/en.json").read_text()
        )
        icons = json.loads((root / "custom_components/airtouch/icons.json").read_text())
        self.assertEqual(strings["entity"], english["entity"])
        for field, name in _NAMES.items():
            self.assertEqual(strings["entity"]["number"][field.value]["name"], name)
            self.assertEqual(
                icons["entity"]["number"][field.value]["default"], _ICONS[field.value]
            )
        changelog = (root / "CHANGELOG.md").read_text()
        readme = (root / "README.md").read_text()
        self.assertIn(
            "Added per-air-conditioner cool and heat limits and an auto-off duration "
            "for AirTouch 5.",
            changelog,
        )
        self.assertIn("`number.<ac_name>_auto_off_duration`", readme)


class AcSettingsServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.header = _HEADER
        self.records = [_RECORD_0, _RECORD_1]
        self.raw: list[bytes] = []
        self.writes: list[bytes] = []
        self.connections = 0
        self.active = 0
        self.max_active = 0
        self.mode = "ok"
        self.hold_once = False
        self._held = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sockets = self.server.sockets
        if not sockets:
            raise RuntimeError("test server did not bind a socket")
        self.port = sockets[0].getsockname()[1]
        self.hass = _Hass()
        self.entry = _Entry()
        self.coordinators: list[AcSettingsCoordinator] = []

    async def asyncTearDown(self) -> None:
        self.release.set()
        for coordinator in self.coordinators:
            await coordinator.async_shutdown()
        self.server.close()
        await self.server.wait_closed()

    def _coordinator(self, *, deadline: float = 1.0) -> AcSettingsCoordinator:
        coordinator = ensure_ac_settings(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            port=self.port,
            deadline=deadline,
        )
        if coordinator is None:
            raise AssertionError("expected an AirTouch 5 settings owner")
        if coordinator not in self.coordinators:
            self.coordinators.append(coordinator)
        return coordinator

    def _numbers(
        self, coordinator: AcSettingsCoordinator, ac_id: int
    ) -> dict[AcSettingField, AcSettingNumber]:
        numbers: dict[AcSettingField, AcSettingNumber] = {}
        for field in AcSettingField:
            number = AcSettingNumber(
                coordinator,
                f"hub_ac{ac_id}",
                cast("Any", _device_info(ac_id)),
                ac_id,
                field,
            )
            number.hass = cast("Any", self.hass)
            number.entity_id = f"number.ac{ac_id}_{field.value}"
            numbers[field] = number
        return numbers

    def _poke(self, index: int, offset: int, value: int) -> None:
        record = bytearray(self.records[index])
        record[offset] = value
        self.records[index] = bytes(record)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        try:
            raw = await _read_raw(reader)
        except asyncio.IncompleteReadError:
            writer.close()
            return
        self.raw.append(raw)
        header, payload = _oracle_decode(raw)
        await self._pause()
        try:
            if self.mode == "drop":
                return
            if self.mode == "truncate":
                writer.write(b"\x55\x55\x55")
                await writer.drain()
                return
            frame = bytearray(_reply(header.packet_id, self._answer(payload)))
            if self.mode == "crc":
                frame[-1] ^= 0xFF
            writer.write(frame)
            await writer.drain()
        except (ConnectionError, OSError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _pause(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.hold_once and not self._held:
                self._held = True
                self.started.set()
                await self.release.wait()
            else:
                await asyncio.sleep(0.01)
        finally:
            self.active -= 1

    def _report(self) -> bytes:
        repeat = len(self.records[0]) if self.records else 0
        return (
            struct.pack("!BxHHH", 0x5B, len(self.header), repeat, len(self.records))
            + self.header
            + b"".join(self.records)
        )

    def _answer(self, payload: bytes) -> bytes:
        if self.mode == "malformed":
            return struct.pack("!BxHHH", 0x5B, 4, 24, 1) + b"\x00\x00"
        if payload[:1] != b"\x5a":
            return self._report()
        self.writes.append(payload)
        if self.mode == "error":
            return struct.pack("!BxHHH", 0x01, 0, 0, 0)
        if self.mode != "mismatch":
            self._apply(payload)
        return self._report()

    def _apply(self, payload: bytes) -> None:
        if len(payload) < 8:
            return
        _sub, normal, repeat, count = struct.unpack_from("!BxHHH", payload)
        if count != 1:
            return
        start = 8 + normal
        record = payload[start : start + repeat]
        if len(record) != repeat or not record:
            return
        ac_id = record[0] & 15
        for index, current in enumerate(self.records):
            if (current[0] & 15) == ac_id:
                self.records[index] = record
                return

    async def test_query_frame_is_exact(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        header, payload = _oracle_decode(self.raw[0])
        self.assertEqual(header.to_address, 0x80)
        self.assertEqual(header.from_address, 0xB0)
        self.assertEqual(header.message_id, CONTROL_MESSAGE_ID)
        self.assertEqual(header.packet_id, 1)
        self.assertEqual(payload, _QUERY)
        self.assertEqual(
            self.raw[0],
            _oracle_frame(
                to_address=0x80,
                from_address=0xB0,
                packet_id=1,
                payload=_QUERY,
            ),
        )
        east = self._numbers(coordinator, 0)
        west = self._numbers(coordinator, 1)
        missing = self._numbers(coordinator, 7)[AcSettingField.COOL_MINIMUM]
        self.assertEqual(east[AcSettingField.COOL_MINIMUM].native_value, 18)
        self.assertEqual(east[AcSettingField.COOL_MINIMUM].native_min_value, 16)
        self.assertEqual(east[AcSettingField.COOL_MINIMUM].native_max_value, 32)
        self.assertEqual(west[AcSettingField.AUTO_OFF_DURATION].native_value, 5)
        self.assertEqual(
            east[AcSettingField.COOL_MINIMUM].unique_id, "hub_ac0_cool_minimum"
        )
        self.assertEqual(east[AcSettingField.COOL_MINIMUM].name, "Cool minimum")
        self.assertEqual(east[AcSettingField.AUTO_OFF_DURATION].native_step, 1)
        self.assertEqual(
            east[AcSettingField.AUTO_OFF_DURATION].device_class,
            NumberDeviceClass.DURATION,
        )
        self.assertEqual(
            east[AcSettingField.AUTO_OFF_DURATION].native_unit_of_measurement,
            UnitOfTime.HOURS,
        )
        self.assertEqual(
            east[AcSettingField.COOL_MINIMUM].device_class,
            NumberDeviceClass.TEMPERATURE,
        )
        self.assertEqual(east[AcSettingField.COOL_MINIMUM].mode, NumberMode.BOX)
        self.assertFalse(missing.available)
        self.assertIsNone(missing.native_value)
        self.assertNotEqual(
            (missing.native_min_value, missing.native_max_value), (16, 32)
        )

    async def test_cool_minimum_write_matches_the_hand_built_payload(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        number = self._numbers(coordinator, 0)[AcSettingField.COOL_MINIMUM]
        await number.async_set_native_value(19)
        self.assertEqual(self.writes, [_COOL_MIN_WRITE])
        write_header, write_payload = _oracle_decode(self.raw[2])
        self.assertEqual(write_header.packet_id, 3)
        self.assertEqual(write_header.to_address, 0x80)
        self.assertEqual(write_header.message_id, CONTROL_MESSAGE_ID)
        self.assertEqual(write_payload, _COOL_MIN_WRITE)
        self.assertEqual(number.native_value, 19)
        snapshot = coordinator.data
        assert snapshot is not None
        self.assertEqual(snapshot.header, _HEADER)
        self.assertEqual(snapshot.records[1].raw, _RECORD_1)
        self.assertEqual(self.records[1], _RECORD_1)
        self.assertTrue(coordinator.last_update_success)

    async def test_other_fields_change_only_the_selected_byte(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        numbers = self._numbers(coordinator, 0)
        cases = (
            (AcSettingField.COOL_MAXIMUM, 27, 20),
            (AcSettingField.HEAT_MINIMUM, 18, 23),
            (AcSettingField.HEAT_MAXIMUM, 25, 22),
        )
        for field, value, offset in cases:
            before = self.records[0]
            other = self.records[1]
            await numbers[field].async_set_native_value(value)
            sent = self.writes[-1][14:]
            changed = [
                index for index, byte in enumerate(sent) if byte != before[index]
            ]
            self.assertEqual(changed, [offset])
            self.assertEqual(sent[offset], value)
            self.assertEqual(self.records[1], other)
            self.assertEqual(numbers[field].native_value, value)
            self.assertNotIn(b"West", self.writes[-1])

    async def test_duration_zero_and_eight_preserve_flags(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        number = self._numbers(coordinator, 0)[AcSettingField.AUTO_OFF_DURATION]
        self.assertEqual(number.native_value, 0)
        self.assertEqual(number.native_min_value, 0)
        self.assertEqual(number.native_max_value, 8)
        before = self.connections
        for value in (9, -1, 8.2, True):
            with self.subTest(value=value), self.assertRaises(HomeAssistantError):
                await number.async_set_native_value(cast("float", value))
        self.assertEqual(self.connections, before)
        self.assertEqual(self.writes, [])
        await number.async_set_native_value(8)
        self.assertEqual(self.records[0][8], 0xF8)
        self.assertEqual(self.writes[-1][14:], self.records[0])
        unchanged = bytearray(_RECORD_0)
        unchanged[8] = 0xF8
        self.assertEqual(self.records[0], bytes(unchanged))
        self.assertEqual(self.records[1], _RECORD_1)
        await number.async_set_native_value(0)
        self.assertEqual(self.records[0][8], 0xF0)
        self.assertEqual(self.records[0], _RECORD_0)
        self.assertEqual(number.native_value, 0)

    async def test_fresh_opposite_limit_uses_the_unread_server_value(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        number = self._numbers(coordinator, 0)[AcSettingField.COOL_MINIMUM]
        self._poke(0, 20, 20)
        before = self.connections
        with self.assertRaises(HomeAssistantError):
            await number.async_set_native_value(24)
        self.assertEqual(self.connections, before + 1)
        self.assertEqual(self.writes, [])
        self.assertEqual(number.native_value, 18)
        self.assertEqual(self.records[0][20], 20)
        await number.async_set_native_value(19)
        sent = self.writes[-1][14:]
        self.assertEqual(sent[20], 20)
        self.assertEqual(sent[21], 19)
        self.assertEqual(number.native_value, 19)

    async def test_brand_bounds_follow_the_fresh_report(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        numbers = self._numbers(coordinator, 0)
        cool = numbers[AcSettingField.COOL_MINIMUM]
        self.assertEqual(cool.native_min_value, 16)
        self.assertEqual(cool.native_max_value, 32)
        self._poke(0, 16, 30)
        self._poke(0, 17, 18)
        await coordinator.async_refresh()
        self.assertEqual(cool.native_min_value, 18)
        self.assertEqual(cool.native_max_value, 30)
        self.assertEqual(numbers[AcSettingField.COOL_MAXIMUM].native_max_value, 30)
        before = self.connections
        with self.assertRaises(HomeAssistantError):
            await cool.async_set_native_value(17)
        self.assertEqual(self.connections, before + 1)
        self.assertEqual(self.writes, [])
        await cool.async_set_native_value(18)
        self.assertEqual(self.records[0][21], 18)
        self._poke(0, 16, 10)
        self._poke(0, 17, 30)
        await coordinator.async_refresh()
        self.assertFalse(cool.available)
        self.assertFalse(numbers[AcSettingField.COOL_MAXIMUM].available)
        self.assertEqual(cool.native_min_value, 30)
        self.assertEqual(cool.native_max_value, 10)
        self.assertNotEqual((cool.native_min_value, cool.native_max_value), (16, 32))
        self.assertTrue(numbers[AcSettingField.HEAT_MINIMUM].available)
        duration = numbers[AcSettingField.AUTO_OFF_DURATION]
        self.assertTrue(duration.available)
        self.assertEqual(duration.native_value, 0)
        self.assertEqual(duration.native_min_value, 0)
        self.assertEqual(duration.native_max_value, 8)
        before = self.connections
        with self.assertRaises(HomeAssistantError):
            await cool.async_set_native_value(20)
        self.assertEqual(self.connections, before + 1)
        self.assertEqual(self.records[0][21], 18)
        await duration.async_set_native_value(8)
        self.assertEqual(self.records[0][8], 0xF8)

    async def test_values_outside_the_accepted_range_stay_unknown(self) -> None:
        coordinator = self._coordinator()
        self._poke(0, 20, 40)
        self._poke(0, 8, 0x1F)
        await coordinator.async_refresh()
        numbers = self._numbers(coordinator, 0)
        cool = numbers[AcSettingField.COOL_MAXIMUM]
        duration = numbers[AcSettingField.AUTO_OFF_DURATION]
        self.assertTrue(cool.available)
        self.assertIsNone(cool.native_value)
        self.assertTrue(duration.available)
        self.assertIsNone(duration.native_value)
        await cool.async_set_native_value(28)
        self.assertEqual(cool.native_value, 28)
        self.assertEqual(self.records[0][20], 28)
        await duration.async_set_native_value(8)
        self.assertEqual(self.records[0][8], 0x18)
        self.assertEqual(duration.native_value, 8)

    async def test_rejected_types_do_not_connect(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        number = self._numbers(coordinator, 0)[AcSettingField.COOL_MINIMUM]
        before = self.connections
        for value in (True, 18.5, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(HomeAssistantError):
                await number.async_set_native_value(cast("float", value))
        self.assertEqual(self.connections, before)
        self.assertEqual(self.writes, [])
        with self.assertRaises(AcSettingsRejectedError):
            await coordinator.async_set(0, AcSettingField.COOL_MINIMUM, True)  # noqa: FBT003

    async def test_read_write_and_readback_failures_keep_the_previous_value(
        self,
    ) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        number = self._numbers(coordinator, 0)[AcSettingField.COOL_MINIMUM]
        self.mode = "error"
        before = self.connections
        with self.assertRaises(HomeAssistantError):
            await number.async_set_native_value(19)
        self.assertEqual(self.connections, before + 2)
        self.assertEqual(number.native_value, 18)
        self.assertEqual(self.records[0], _RECORD_0)
        self.assertTrue(coordinator.last_update_success)
        self.mode = "mismatch"
        before = self.connections
        with self.assertRaises(HomeAssistantError):
            await number.async_set_native_value(19)
        self.assertEqual(self.connections, before + 3)
        self.assertEqual(number.native_value, 18)
        snapshot = coordinator.data
        assert snapshot is not None
        self.assertEqual(snapshot.records[0].raw, _RECORD_0)
        self.mode = "crc"
        await coordinator.async_refresh()
        self.assertFalse(coordinator.last_update_success)
        self.assertFalse(number.available)
        self.assertEqual(coordinator.data, snapshot)
        self.mode = "drop"
        await coordinator.async_refresh()
        self.assertFalse(number.available)
        self.mode = "ok"
        await coordinator.async_refresh()
        self.assertTrue(number.available)
        self.assertEqual(number.native_value, 18)

    async def test_malformed_frames_make_numbers_unavailable(self) -> None:
        self.mode = "truncate"
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        number = self._numbers(coordinator, 0)[AcSettingField.COOL_MINIMUM]
        self.assertFalse(coordinator.last_update_success)
        self.assertIsNone(coordinator.data)
        self.assertFalse(number.available)
        self.assertIsNone(number.native_value)
        self.mode = "malformed"
        await coordinator.async_refresh()
        self.assertFalse(number.available)
        self.mode = "ok"
        await coordinator.async_refresh()
        self.assertTrue(number.available)
        self.assertEqual(number.native_value, 18)

    async def test_missing_air_conditioner_rejects_the_write(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        missing = self._numbers(coordinator, 7)[AcSettingField.HEAT_MINIMUM]
        before = self.connections
        with self.assertRaises(HomeAssistantError):
            await missing.async_set_native_value(18)
        self.assertEqual(self.connections, before + 1)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.records[0], _RECORD_0)

    async def test_concurrent_updates_keep_both_changes(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        await asyncio.gather(
            coordinator.async_set(0, AcSettingField.COOL_MINIMUM, 19),
            coordinator.async_set(0, AcSettingField.AUTO_OFF_DURATION, 8),
        )
        self.assertEqual(self.max_active, 1)
        self.assertEqual(len(self.writes), 2)
        final = bytearray(_RECORD_0)
        final[8] = 0xF8
        final[21] = 19
        first = self.writes[0][14:]
        second = self.writes[1][14:]
        changed = [
            index for index, byte in enumerate(first) if byte != _RECORD_0[index]
        ]
        self.assertEqual(len(changed), 1)
        self.assertIn(changed[0], (8, 21))
        self.assertEqual(second, bytes(final))
        self.assertEqual(self.records[0], bytes(final))
        self.assertEqual(self.records[1], _RECORD_1)
        for payload in self.writes:
            self.assertNotIn(b"West", payload)
            self.assertEqual(len(payload), 45)

    async def test_cancel_releases_the_lock(self) -> None:
        coordinator = self._coordinator(deadline=2)
        await coordinator.async_refresh()
        self.hold_once = True
        task = asyncio.create_task(
            coordinator.async_set(0, AcSettingField.COOL_MINIMUM, 19)
        )
        await asyncio.wait_for(self.started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.release.set()
        await asyncio.wait_for(
            coordinator.async_set(0, AcSettingField.HEAT_MINIMUM, 18), 2
        )
        snapshot = coordinator.data
        assert snapshot is not None
        self.assertEqual(snapshot.records[0].value(AcSettingField.HEAT_MINIMUM), 18)
        self.assertEqual(snapshot.records[0].value(AcSettingField.COOL_MINIMUM), 18)

    async def test_shutdown_drains_admitted_work_and_rejects_late_writes(self) -> None:
        coordinator = self._coordinator(deadline=2)
        numbers = self._numbers(coordinator, 0)
        await coordinator.async_refresh()
        self.hold_once = True
        admitted = asyncio.create_task(
            numbers[AcSettingField.COOL_MINIMUM].async_set_native_value(19)
        )
        await asyncio.wait_for(self.started.wait(), 1)
        queued = asyncio.create_task(
            numbers[AcSettingField.COOL_MAXIMUM].async_set_native_value(27)
        )
        poll = asyncio.create_task(coordinator.async_refresh())
        await asyncio.sleep(0.05)
        self.assertFalse(queued.done())
        self.assertFalse(poll.done())
        self.assertEqual(self.writes, [])
        closing = asyncio.create_task(coordinator.async_shutdown())
        await asyncio.sleep(0.05)
        self.assertFalse(admitted.done())
        self.assertFalse(closing.done())
        self.release.set()
        await asyncio.wait_for(admitted, 2)
        with self.assertRaises(HomeAssistantError):
            await asyncio.wait_for(queued, 2)
        await asyncio.wait_for(poll, 2)
        await asyncio.wait_for(closing, 2)
        snapshot = coordinator.data
        assert snapshot is not None
        self.assertEqual(snapshot.records[0].value(AcSettingField.COOL_MINIMUM), 19)
        self.assertEqual(snapshot.records[0].value(AcSettingField.COOL_MAXIMUM), 28)
        self.assertFalse(coordinator.last_update_success)
        self.assertFalse(numbers[AcSettingField.COOL_MINIMUM].available)
        seen = self.connections
        with self.assertRaises(HomeAssistantError):
            await numbers[AcSettingField.HEAT_MINIMUM].async_set_native_value(18)
        await coordinator.async_refresh()
        self.assertEqual(self.connections, seen)
        replacement = self._coordinator()
        self.assertIsNot(replacement, coordinator)
        await replacement.async_refresh()
        self.assertGreater(self.connections, seen)

    async def test_failed_setup_leaves_numbers_unavailable_and_climate_loads(
        self,
    ) -> None:
        self.mode = "truncate"
        airtouch = _AirTouch(acs=[_Ac(0, "East"), _Ac(1, "West"), _Ac(7, "North")])
        self.hass.data.setdefault(DOMAIN, {})[self.entry.entry_id] = airtouch
        owner = ensure_ac_settings(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", airtouch),
            port=self.port,
        )
        if owner is None:
            raise AssertionError("expected an owner")
        self.coordinators.append(owner)
        with patch(
            "custom_components.airtouch.ac_settings.devices.AirTouchDevice",
            _Console,
        ):
            entities = await async_collect_ac_setting_numbers(
                cast("Any", self.hass),
                cast("Any", self.entry),
                cast("Any", airtouch),
            )
        self.assertEqual(len(entities), 15)
        self.assertTrue(all(not entity.available for entity in entities))
        seen = self.connections
        added: list[Entity] = []

        def add_entities(
            new_entities: Iterable[Entity],
            update_before_add: bool = False,  # noqa: FBT001, FBT002
        ) -> None:
            del update_before_add
            added.extend(new_entities)

        with (
            patch(
                "custom_components.airtouch.climate.devices.AirTouchDevice",
                _Console,
            ),
            patch(
                "custom_components.airtouch.climate.entity_platform.async_get_current_platform",
                return_value=_Platform(),
            ),
        ):
            await climate.async_setup_entry(
                cast("Any", self.hass), cast("Any", self.entry), add_entities
            )
        self.assertEqual(self.connections, seen)
        self.assertEqual(
            sorted(entity.unique_id for entity in added),
            ["hub_ac0", "hub_ac1", "hub_ac7"],
        )
        self.assertTrue(all(not entity.available for entity in entities))

    async def test_airtouch_4_and_missing_ids_add_no_numbers(self) -> None:
        before = self.connections
        self.assertIsNone(
            ensure_ac_settings(
                cast("Any", self.hass),
                cast("Any", self.entry),
                cast("Any", _AirTouch(AirTouchModel.AIRTOUCH_4)),
                port=self.port,
            )
        )
        self.assertEqual(
            await async_collect_ac_setting_numbers(
                cast("Any", self.hass),
                cast("Any", self.entry),
                cast("Any", _AirTouch(AirTouchModel.AIRTOUCH_4)),
            ),
            [],
        )
        self.assertEqual(self.connections, before)
        stand_in = _AirTouch()
        self.hass.data.setdefault(DOMAIN, {})[self.entry.entry_id] = stand_in
        installed = async_install(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", stand_in),
            port=self.port,
        )
        if installed is None:
            raise AssertionError("expected smart settings")
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            closed_port = closed.getsockname()[1]
        preference_owner = async_install_preferences(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", stand_in),
            port=closed_port,
            deadline=0.2,
        )
        if preference_owner is None:
            raise AssertionError("expected preferences")
        added: list[list[Entity]] = []

        def add_entities(
            new_entities: Iterable[Entity],
            update_before_add: bool = False,  # noqa: FBT001, FBT002
        ) -> None:
            del update_before_add
            added.append(list(new_entities))

        with patch(
            "custom_components.airtouch.number.devices.AirTouchDevice",
            return_value=SimpleNamespace(unique_id="hub", device_info={}),
        ):
            await async_setup_entry(
                cast("Any", self.hass), cast("Any", self.entry), add_entities
            )
        self.assertEqual(added[2], [])
        self.assertEqual(self.connections, before)
        preference = added[0][0]
        assert isinstance(preference, PreferenceNumber)
        await async_release_preferences(cast("Any", self.hass), self.entry.entry_id)
        await async_release(cast("Any", self.hass), self.entry.entry_id)

    async def test_fahrenheit_service_conversion_leaves_duration_step(self) -> None:
        self._poke(0, 20, 21)
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        numbers = self._numbers(coordinator, 0)
        cool = numbers[AcSettingField.COOL_MAXIMUM]
        duration = numbers[AcSettingField.AUTO_OFF_DURATION]
        self.hass.config.units = US_CUSTOMARY_SYSTEM
        self.assertEqual(cool.native_step, 1.8)
        self.assertEqual(cool.step, 1.8)
        self.assertEqual(cool.unit_of_measurement, UnitOfTemperature.FAHRENHEIT)
        self.assertEqual(duration.native_step, 1)
        self.assertEqual(duration.step, 1)
        assert cool.value is not None
        await cast("_NumberServiceModule", number_component).async_set_value(
            cool,
            ServiceCall(
                cast("Any", self.hass),
                "number",
                "set_value",
                {"value": cool.value + cool.step},
            ),
        )
        self.assertEqual(cool.native_value, 22)
        self.assertEqual(self.records[0][20], 22)
        self.hass.config.units = METRIC_SYSTEM
        self.assertEqual(cool.native_step, 1)
        self.assertEqual(duration.native_step, 1)

    async def test_duration_registry_keeps_hours_and_service_step(self) -> None:
        coordinator = self._coordinator()
        duration = self._numbers(coordinator, 0)[AcSettingField.AUTO_OFF_DURATION]
        duration.platform = cast(
            "Any",
            SimpleNamespace(
                platform_name=DOMAIN,
                domain="number",
                default_language_platform_translations={},
            ),
        )
        for unit in (UnitOfTime.MINUTES, UnitOfTime.SECONDS, UnitOfTime.DAYS):
            self._poke(0, 8, 0xF1)
            await coordinator.async_refresh()
            with self.subTest(unit=unit):
                duration.registry_entry = cast(
                    "Any",
                    SimpleNamespace(options={"number": {"unit_of_measurement": unit}}),
                )
                duration.async_registry_entry_updated()
                self.assertEqual(duration.unit_of_measurement, UnitOfTime.HOURS)
                self.assertEqual(duration.step, 1)
                assert duration.value is not None
                await cast("_NumberServiceModule", number_component).async_set_value(
                    duration,
                    ServiceCall(
                        cast("Any", self.hass),
                        "number",
                        "set_value",
                        {"value": duration.value + duration.step},
                    ),
                )
                self.assertEqual(duration.native_value, 2)
                self.assertEqual(self.records[0][8], 0xF2)

    async def test_root_setup_failure_and_cancellation_close_ac_owner(self) -> None:
        api = _AirTouch()
        for error in (RuntimeError("forward failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):

                async def forward(
                    entry: object,
                    platforms: object,
                    *,
                    failure: BaseException = error,
                ) -> None:
                    del entry, platforms
                    await self._coordinator().async_refresh()
                    raise failure

                self.hass.config_entries = SimpleNamespace(
                    async_forward_entry_setups=forward
                )
                owner = self._coordinator()
                with (
                    patch(
                        "custom_components.airtouch.pyairtouch.connect",
                        return_value=api,
                    ),
                    self.assertRaises(type(error)),
                ):
                    await async_setup_integration(
                        cast("Any", self.hass), cast("Any", self.entry)
                    )
                before = self.connections
                with self.assertRaises(HomeAssistantError):
                    await self._numbers(owner, 0)[
                        AcSettingField.COOL_MINIMUM
                    ].async_set_native_value(19)
                self.assertEqual(self.connections, before)
                self.assertNotIn("airtouch_ac_settings", self.hass.data)

    async def test_root_unload_retains_or_closes_ac_owner(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        api = _AirTouch()
        self.hass.data[DOMAIN] = {self.entry.entry_id: api}
        number = self._numbers(coordinator, 0)[AcSettingField.COOL_MINIMUM]
        for accepted in (False, True):

            async def unload(
                entry: object, platforms: object, *, result: bool = accepted
            ) -> bool:
                del entry, platforms
                return result

            self.hass.config_entries = SimpleNamespace(async_unload_platforms=unload)
            self.assertIs(
                await async_unload_integration(
                    cast("Any", self.hass), cast("Any", self.entry)
                ),
                accepted,
            )
            if not accepted:
                self.assertTrue(api.connected)
                await number.async_set_native_value(19)
        before = self.connections
        with self.assertRaises(HomeAssistantError):
            await number.async_set_native_value(20)
        self.assertEqual(self.connections, before)
        self.assertFalse(api.connected)
        self.assertNotIn("airtouch_ac_settings", self.hass.data)

    async def test_unload_stops_later_refreshes(self) -> None:
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
        again = ensure_ac_settings(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            port=self.port,
        )
        self.assertIsNot(again, coordinator)
        if again is not None:
            self.coordinators.append(again)


if __name__ == "__main__":
    unittest.main()
