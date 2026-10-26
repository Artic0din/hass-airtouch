from __future__ import annotations

import asyncio
import contextlib
import struct
import unittest
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol, cast
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from logging import Logger

    from homeassistant.components.number import NumberEntity
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity

from homeassistant.components import number as number_component
from homeassistant.components.number import NumberDeviceClass, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.unit_conversion import TemperatureConverter
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pyairtouch import AirTouchModel
from pyairtouch.at5.comms.hdr import At5Header, HeaderDecoder, HeaderEncoder
from pyairtouch.at5.comms.xC0_ctrl_status import MESSAGE_ID as CONTROL_MESSAGE_ID
from pyairtouch.comms.crc16 import Crc16Modbus

from custom_components.airtouch import (
    async_setup_entry as async_setup_integration,
)
from custom_components.airtouch import (
    async_unload_entry as async_unload_integration,
)
from custom_components.airtouch import climate
from custom_components.airtouch.console import DEFAULT_DEADLINE, ConsoleClient
from custom_components.airtouch.const import DOMAIN
from custom_components.airtouch.const import AirTouchModel as SelectedModel
from custom_components.airtouch.number import (
    PreferenceNumber,
    async_collect_preference_numbers,
    async_setup_entry,
)
from custom_components.airtouch.preferences import (
    PreferenceCoordinator,
    PreferenceField,
    PreferenceParseError,
    PreferenceRejectedError,
    PreferenceSnapshot,
    async_install_preferences,
    async_release_preferences,
    get_preferences,
    parse_preference_report,
    whole_degrees,
)
from custom_components.airtouch.smart_settings import (
    async_install,
    async_release,
    get_smart_settings,
)

_OUTER = b"\x55\x55\x55\xab"
_ENCODER = HeaderEncoder()
_DECODER = HeaderDecoder()
_CRC = Crc16Modbus()
_TAIL = bytes(range(0x30, 0x44))
_UNKNOWN = bytes((0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88))
_QUERY = bytes.fromhex("4500000000000000")
_WRITE_HEADER = bytes.fromhex("4400002000000000")


def _console_prefix() -> bytes:
    prefix = bytearray(32)
    prefix[0:4] = b"UNIT"
    prefix[16] = 0x1C
    prefix[17] = 0x5A
    prefix[18] = 0x11
    prefix[19] = 0x22
    prefix[20] = 28
    prefix[21] = 18
    prefix[22] = 0x20
    prefix[23] = 16
    prefix[24:32] = _UNKNOWN
    return bytes(prefix)


def _report(prefix: bytes) -> bytes:
    body = prefix + _TAIL
    if len(body) != 52:
        raise AssertionError(len(body))
    return struct.pack("!BxHHH", 0x45, 52, 0, 0) + body


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


async def _read_raw(reader: asyncio.StreamReader) -> bytes:
    outer = await reader.readexactly(10)
    length = struct.unpack_from("!H", outer, 6)[0]
    return outer + await reader.readexactly(length)


class _Entry:
    def __init__(self) -> None:
        self.entry_id = "entry"
        self.title = "preference-test"
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
        self.units = METRIC_SYSTEM


class _AirTouch:
    def __init__(self, model: AirTouchModel = AirTouchModel.AIRTOUCH_5) -> None:
        self.host = "127.0.0.1"
        self.model = model
        self.air_conditioners: list[Any] = [object(), object(), object()]


_DEVICE_INFO: dict[str, object] = {
    "identifiers": {("airtouch", "hub")},
    "name": "Console",
}


class _Device:
    unique_id = "hub"

    def __init__(self) -> None:
        self.device_info = _DEVICE_INFO


class _Api:
    def __init__(self, model: AirTouchModel = AirTouchModel.AIRTOUCH_5) -> None:
        self.model = model
        self.host = "127.0.0.1"
        self.shutdown_calls = 0
        self.connected = True
        self.air_conditioners: list[Any] = []

    async def init(self) -> bool:
        return True

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.connected = False


class _RootEntry:
    def __init__(self, entry_id: str, model: str) -> None:
        self.entry_id = entry_id
        self.title = "preference-root"
        self.domain = DOMAIN
        self.version = 2
        self.minor_version = 2
        self.unique_id = entry_id
        self.data = {"host": "127.0.0.1", "airtouch_model": model}
        self.options: dict[str, Any] = {}
        self.pref_disable_polling = False
        self.unloads: list[Any] = []
        self.listeners: list[Any] = []

    def async_on_unload(self, func: Callable[[], object]) -> None:
        self.unloads.append(func)

    def add_update_listener(self, listener: object) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: None


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


class _Ac:
    def __init__(self, ac_id: int, name: str) -> None:
        self.ac_id = ac_id
        self.name = name
        self.zones: list[object] = []
        self.supported_modes: list[object] = []
        self.supported_fan_speeds: list[object] = []
        self.supported_power_controls: list[object] = []
        self.target_temperature_resolution = 1.0

    def subscribe_ac_state(self, callback: object) -> None:
        del callback

    def unsubscribe_ac_state(self, callback: object) -> None:
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


class _NumberServiceModule(Protocol):
    async def async_set_value(
        self, entity: NumberEntity, service_call: ServiceCall
    ) -> None: ...


class PreferenceReportTests(unittest.TestCase):
    def test_whole_degree_validation_exception_types(self) -> None:
        for value in (True, None, "21"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                whole_degrees(value)
        for invalid_number in (21.25, float("nan"), float("inf")):
            with self.subTest(value=invalid_number), self.assertRaises(ValueError):
                whole_degrees(invalid_number)
        self.assertEqual(whole_degrees(20.999999999999996), 21)

    def test_sentinels_and_retained_prefix(self) -> None:
        prefix = bytearray(_console_prefix())
        prefix[22] = 0
        prefix[23] = 0
        report = parse_preference_report(_report(bytes(prefix)))
        self.assertEqual(report.sleep_cool_c, 32)
        self.assertEqual(report.sleep_heat_c, 16)
        self.assertEqual(report.away_maximum_c, 28)
        self.assertEqual(report.away_minimum_c, 18)
        self.assertEqual(report.prefix[17], 0x5A)
        self.assertEqual(report.prefix[22], 0)
        wire = bytearray(_console_prefix())
        wire[22] = 25
        wire[23] = 18
        decoded = parse_preference_report(_report(bytes(wire)))
        self.assertEqual(decoded.sleep_cool_c, 25)
        self.assertEqual(decoded.sleep_heat_c, 18)
        masked = bytearray(_console_prefix())
        decoded_mask = parse_preference_report(_report(bytes(masked)))
        self.assertEqual(decoded_mask.sleep_cool_c, 32)

    def test_truncated_and_unexpected_reports_fail(self) -> None:
        with self.assertRaises(PreferenceParseError):
            parse_preference_report(b"\x45\x00\x00\x34")
        short = struct.pack("!BxHHH", 0x45, 52, 0, 0) + b"\x00\x00"
        with self.assertRaises(PreferenceParseError):
            parse_preference_report(short)
        other = struct.pack("!BxHHH", 0x01, 0, 0, 0)
        with self.assertRaises(PreferenceParseError):
            parse_preference_report(other)


class PreferenceServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = _console_prefix()
        self.raw: list[bytes] = []
        self.writes: list[bytes] = []
        self.connections = 0
        self.active = 0
        self.max_active = 0
        self.mode = "ok"
        self.decoy = False
        self.hold_once = False
        self.hold_all = False
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
        self.coordinators: list[PreferenceCoordinator] = []

    async def asyncTearDown(self) -> None:
        self.release.set()
        for coordinator in self.coordinators:
            await coordinator.async_shutdown()
        self.server.close()
        await self.server.wait_closed()

    def _coordinator(self, *, deadline: float = 1.0) -> PreferenceCoordinator:
        coordinator = PreferenceCoordinator(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            port=self.port,
            deadline=deadline,
        )
        self.coordinators.append(coordinator)
        return coordinator

    def _numbers(
        self, coordinator: PreferenceCoordinator
    ) -> dict[PreferenceField, PreferenceNumber]:
        numbers = {
            field: PreferenceNumber(
                coordinator, "hub", cast("Any", _DEVICE_INFO), field
            )
            for field in PreferenceField
        }
        for field, number in numbers.items():
            number.hass = cast("HomeAssistant", self.hass)
            number.entity_id = f"number.{field.name.lower()}"
        return numbers

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
            if self.mode == "truncate":
                writer.write(b"\x55\x55\x55")
                await writer.drain()
                return
            if self.decoy:
                writer.write(_reply(99, _report(_decoy_prefix())))
                await writer.drain()
            writer.write(_reply(header.packet_id, self._answer(payload)))
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
            if self.hold_all or (self.hold_once and not self._held):
                if not self.hold_all:
                    self._held = True
                self.started.set()
                await self.release.wait()
            else:
                await asyncio.sleep(0.01)
        finally:
            self.active -= 1

    def _answer(self, payload: bytes) -> bytes:
        if self.mode == "malformed":
            return struct.pack("!BxHHH", 0x45, 52, 0, 0) + b"\x00\x00"
        if payload[:1] != b"\x44":
            return _report(self.prefix)
        requested = payload[8:40]
        self.writes.append(requested)
        if self.mode == "error":
            return struct.pack("!BxHHH", 0x01, 0, 0, 0)
        if self.mode == "mismatch":
            return _report(requested)
        self.prefix = requested
        return _report(self.prefix)

    async def test_query_frame_and_decoy_are_exact(self) -> None:
        self.decoy = True
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
        snapshot = coordinator.data
        assert snapshot is not None
        self.assertEqual(snapshot.away_maximum_c, 28)
        self.assertEqual(snapshot.sleep_cool_c, 32)

    async def test_write_retains_other_bytes_and_round_trips_sleep_32(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        numbers = self._numbers(coordinator)
        await numbers[PreferenceField.AWAY_MINIMUM].async_set_native_value(10)
        write = self.writes[0]
        self.assertEqual(write[21], 10)
        self.assertEqual(write[22], 0x20)
        self.assertEqual(write[17], 0x5A)
        self.assertEqual(write[18:20], bytes((0x11, 0x22)))
        self.assertEqual(write[24:32], _UNKNOWN)
        self.assertEqual(write[0:4], b"UNIT")
        write_header, write_payload = _write_frames(self.raw)[0]
        self.assertEqual(write_header.to_address, 0x80)
        self.assertEqual(write_header.from_address, 0xB0)
        self.assertEqual(write_header.message_id, CONTROL_MESSAGE_ID)
        self.assertEqual(write_header.packet_id, 3)
        self.assertEqual(write_payload[:8], _WRITE_HEADER)
        self.assertEqual(write_payload[8:], write)
        self.assertNotIn(_TAIL, write_payload)
        self.assertEqual(numbers[PreferenceField.AWAY_MINIMUM].native_value, 10)
        self.assertEqual(numbers[PreferenceField.SLEEP_COOL].native_value, 32)
        await numbers[PreferenceField.SLEEP_COOL].async_set_native_value(32)
        cool = self.writes[1]
        self.assertEqual(cool[22], 0)
        self.assertEqual(cool[21], 10)
        self.assertEqual(cool[23], 16)
        self.assertEqual(cool[17], 0x5A)
        self.assertEqual(cool[24:32], _UNKNOWN)
        self.assertEqual(numbers[PreferenceField.SLEEP_COOL].native_value, 32)
        self.assertEqual(numbers[PreferenceField.SLEEP_HEAT].native_value, 16)

    async def test_sleep_heat_rejects_unrepresentable_32_without_writing(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        number = self._numbers(coordinator)[PreferenceField.SLEEP_HEAT]
        self.assertEqual(number.native_max_value, 31)
        before = self.connections
        with self.assertRaises(HomeAssistantError):
            await number.async_set_native_value(32)
        self.assertEqual(self.connections, before)
        self.assertEqual(self.writes, [])
        await number.async_set_native_value(31)
        self.assertEqual(number.native_value, 31)
        self.assertEqual(self.writes[-1][23], 31)

    async def test_whole_degrees_survive_fahrenheit_conversion(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        number = self._numbers(coordinator)[PreferenceField.SLEEP_HEAT]
        celsius = TemperatureConverter.convert(
            69.8, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
        )
        await number.async_set_native_value(celsius)
        self.assertEqual(number.native_value, 21)
        self.assertEqual(self.writes[-1][23], 21)
        before = self.connections
        for value in (21.01, float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(HomeAssistantError):
                await number.async_set_native_value(value)
        self.assertEqual(self.connections, before)

    async def test_displayed_temperature_values_bounds_and_steps(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        numbers = self._numbers(coordinator)
        cases = (
            (PreferenceField.SLEEP_COOL, 32, 16, 32, 89.6, 60.8, 89.6),
            (PreferenceField.SLEEP_HEAT, 16, 16, 31, 60.8, 60.8, 87.9),
            (PreferenceField.AWAY_MINIMUM, 18, 0, 50, 64.4, 32, 122),
            (PreferenceField.AWAY_MAXIMUM, 28, 0, 50, 82.4, 32, 122),
        )
        for field, value_c, low_c, high_c, value_f, low_f, high_f in cases:
            number = numbers[field]
            with self.subTest(field=field):
                self.hass.config.units = METRIC_SYSTEM
                self.assertEqual(number.unit_of_measurement, UnitOfTemperature.CELSIUS)
                self.assertEqual(number.step, 1.0)
                self.assertEqual(number.value, value_c)
                self.assertEqual((number.min_value, number.max_value), (low_c, high_c))
                self.hass.config.units = US_CUSTOMARY_SYSTEM
                self.assertEqual(
                    number.unit_of_measurement, UnitOfTemperature.FAHRENHEIT
                )
                self.assertEqual(number.step, 1.8)
                self.assertEqual(number.value, value_f)
                self.assertEqual((number.min_value, number.max_value), (low_f, high_f))
                self.assertEqual(number.native_value, value_c)
                self.assertEqual(
                    (number.native_min_value, number.native_max_value), (low_c, high_c)
                )
                self.assertEqual(
                    number.native_unit_of_measurement, UnitOfTemperature.CELSIUS
                )
                self.assertEqual(number.unique_id, f"hub_{field.name.lower()}")

    async def test_step_tracks_entity_unit_overrides_and_later_changes(self) -> None:
        numbers = self._numbers(self._coordinator())
        for field, number in numbers.items():
            with self.subTest(field=field):
                self.assertEqual(number.step, 1.0)
                with patch.object(
                    number,
                    "_number_option_unit_of_measurement",
                    UnitOfTemperature.FAHRENHEIT,
                ):
                    self.assertEqual(number.step, 1.8)
                self.assertEqual(number.step, 1.0)
                self.hass.config.units = US_CUSTOMARY_SYSTEM
                self.assertEqual(number.step, 1.8)
                with patch.object(
                    number,
                    "_number_option_unit_of_measurement",
                    UnitOfTemperature.CELSIUS,
                ):
                    self.assertEqual(number.step, 1.0)
                self.assertEqual(number.step, 1.8)
                self.hass.config.units = METRIC_SYSTEM
                self.assertEqual(number.step, 1.0)

    async def test_advertised_fahrenheit_step_works_through_number_service(
        self,
    ) -> None:
        prefix = bytearray(self.prefix)
        prefix[20:24] = bytes((21, 21, 21, 21))
        self.prefix = bytes(prefix)
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        self.hass.config.units = US_CUSTOMARY_SYSTEM
        numbers = self._numbers(coordinator)
        for field, offset in (
            (PreferenceField.SLEEP_COOL, 22),
            (PreferenceField.SLEEP_HEAT, 23),
            (PreferenceField.AWAY_MAXIMUM, 20),
            (PreferenceField.AWAY_MINIMUM, 21),
        ):
            with self.subTest(field=field):
                number = numbers[field]
                self.assertEqual(number.step, 1.8)
                self.assertEqual(number.value, 69.8)
                assert number.value is not None
                before = len(self.writes)
                await cast("_NumberServiceModule", number_component).async_set_value(
                    number,
                    ServiceCall(
                        cast("HomeAssistant", self.hass),
                        "number",
                        "set_value",
                        {"value": number.value + number.step},
                    ),
                )
                prefix[offset] = 22
                self.assertEqual(len(self.writes), before + 1)
                self.assertEqual(self.writes[-1], bytes(prefix))
                self.assertEqual(number.native_value, 22)
                self.assertEqual(number.value, 71.6)
                snapshot = coordinator.data
                assert snapshot is not None
                self.assertEqual(snapshot.value(field), 22)

    async def test_number_service_keeps_fractional_and_range_rejections(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        self.hass.config.units = US_CUSTOMARY_SYSTEM
        before = self.connections
        for field, number in self._numbers(coordinator).items():
            for value in (70.8, number.min_value - 1.8, number.max_value + 1.8):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(HomeAssistantError),
                ):
                    await cast(
                        "_NumberServiceModule", number_component
                    ).async_set_value(
                        number,
                        ServiceCall(
                            cast("HomeAssistant", self.hass),
                            "number",
                            "set_value",
                            {"value": value},
                        ),
                    )
        self.assertEqual(self.connections, before)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.prefix, _console_prefix())

    async def test_bounds_and_rejected_values_do_not_write(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        numbers = self._numbers(coordinator)
        before = self.connections
        for field, value in (
            (PreferenceField.AWAY_MINIMUM, 51),
            (PreferenceField.SLEEP_COOL, 15),
            (PreferenceField.SLEEP_HEAT, 18.5),
        ):
            with self.assertRaises(HomeAssistantError):
                await numbers[field].async_set_native_value(value)
        self.assertEqual(self.connections, before)
        self.assertEqual(self.writes, [])
        with self.assertRaises(HomeAssistantError):
            await numbers[PreferenceField.AWAY_MINIMUM].async_set_native_value(29)
        with self.assertRaises(HomeAssistantError):
            await numbers[PreferenceField.AWAY_MAXIMUM].async_set_native_value(17)
        self.assertEqual(self.writes, [])
        self.assertEqual(numbers[PreferenceField.AWAY_MINIMUM].native_value, 18)
        self.assertTrue(numbers[PreferenceField.AWAY_MINIMUM].available)
        await numbers[PreferenceField.AWAY_MAXIMUM].async_set_native_value(50)
        await numbers[PreferenceField.AWAY_MINIMUM].async_set_native_value(0)
        self.assertEqual(self.writes[-2][20], 50)
        self.assertEqual(self.writes[-1][21], 0)
        self.assertEqual(numbers[PreferenceField.AWAY_MINIMUM].native_value, 0)
        self.assertEqual(numbers[PreferenceField.AWAY_MAXIMUM].native_value, 50)
        with self.assertRaises(PreferenceRejectedError):
            await coordinator.async_set(PreferenceField.AWAY_MINIMUM, True)  # noqa: FBT003

    async def test_server_error_and_readback_mismatch_keep_the_previous_value(
        self,
    ) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        numbers = self._numbers(coordinator)
        self.mode = "error"
        before = self.connections
        with self.assertRaises(HomeAssistantError):
            await numbers[PreferenceField.AWAY_MINIMUM].async_set_native_value(12)
        self.assertEqual(self.connections, before + 2)
        self.assertEqual(self.prefix[21], 18)
        self.assertEqual(numbers[PreferenceField.AWAY_MINIMUM].native_value, 18)
        self.assertTrue(coordinator.last_update_success)
        self.mode = "mismatch"
        before = self.connections
        with self.assertRaises(HomeAssistantError):
            await numbers[PreferenceField.SLEEP_HEAT].async_set_native_value(20)
        self.assertEqual(self.connections, before + 3)
        self.assertEqual(numbers[PreferenceField.SLEEP_HEAT].native_value, 16)
        snapshot = coordinator.data
        assert snapshot is not None
        self.assertEqual(snapshot.prefix[23], 16)

    async def test_malformed_frames_make_numbers_unavailable(self) -> None:
        self.mode = "truncate"
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        number = self._numbers(coordinator)[PreferenceField.SLEEP_COOL]
        self.assertFalse(coordinator.last_update_success)
        self.assertFalse(number.available)
        self.assertIsNone(number.native_value)
        self.mode = "malformed"
        await coordinator.async_refresh()
        self.assertFalse(number.available)
        self.mode = "ok"
        await coordinator.async_refresh()
        self.assertTrue(number.available)
        self.assertEqual(number.native_value, 32)
        self.assertEqual(number.native_min_value, 16)
        self.assertEqual(number.native_max_value, 32)
        self.assertEqual(number.native_step, 1)
        self.assertEqual(number.device_class, NumberDeviceClass.TEMPERATURE)
        self.assertEqual(number.mode, NumberMode.BOX)
        self.assertEqual(number.native_unit_of_measurement, UnitOfTemperature.CELSIUS)

    async def test_concurrent_writes_run_one_at_a_time(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        await asyncio.gather(
            coordinator.async_set(PreferenceField.AWAY_MINIMUM, 10),
            coordinator.async_set(PreferenceField.SLEEP_COOL, 24),
        )
        self.assertEqual(self.max_active, 1)
        self.assertEqual(len(self.writes), 2)
        first, second = self.writes
        original = _console_prefix()
        changed = [index for index, byte in enumerate(first) if byte != original[index]]
        self.assertEqual(len(changed), 1)
        self.assertEqual(second[21], 10)
        self.assertEqual(second[22], 24)
        for index, byte in enumerate(original):
            if index not in (21, 22):
                self.assertEqual(second[index], byte)

    async def test_cancel_releases_the_lock(self) -> None:
        coordinator = self._coordinator(deadline=2)
        await coordinator.async_refresh()
        self.hold_once = True
        task = asyncio.create_task(
            coordinator.async_set(PreferenceField.AWAY_MINIMUM, 12)
        )
        await asyncio.wait_for(self.started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.release.set()
        await asyncio.wait_for(coordinator.async_set(PreferenceField.SLEEP_HEAT, 20), 2)
        snapshot = coordinator.data
        assert snapshot is not None
        self.assertEqual(snapshot.sleep_heat_c, 20)

    async def test_airtouch_4_and_failed_setup_do_not_raise(self) -> None:
        entities = await async_collect_preference_numbers(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch(AirTouchModel.AIRTOUCH_4)),
            unique_id="hub",
            device_info=cast("Any", _DEVICE_INFO),
        )
        self.assertEqual(entities, [])
        self.assertEqual(self.connections, 0)
        self.hass.data.setdefault(DOMAIN, {})[self.entry.entry_id] = _AirTouch(
            AirTouchModel.AIRTOUCH_4
        )
        added: list[list[Entity]] = []

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
        self.assertEqual(self.connections, 0)
        self.mode = "truncate"
        live = _AirTouch()
        self.hass.data[DOMAIN][self.entry.entry_id] = live
        installed = async_install(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", live),
            port=self.port,
        )
        self.assertIsNotNone(installed)
        preference_owner = async_install_preferences(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", live),
            port=self.port,
        )
        if preference_owner is None:
            raise AssertionError("expected a preference owner")
        self.coordinators.append(preference_owner)
        with patch(
            "custom_components.airtouch.number.devices.AirTouchDevice",
            return_value=_Device(),
        ):
            await async_setup_entry(
                cast("Any", self.hass), cast("Any", self.entry), add_entities
            )
        self.assertEqual(len(added[1]), 4)
        self.assertFalse(added[1][0].available)
        self.assertEqual(
            sorted(entity.unique_id for entity in added[1]),
            [
                "hub_away_maximum",
                "hub_away_minimum",
                "hub_sleep_cool",
                "hub_sleep_heat",
            ],
        )
        self.assertEqual(
            sorted(entity.unique_id for entity in added[2]),
            [
                "hub_weather_adaptation_cool_differential",
                "hub_weather_adaptation_cool_target",
                "hub_weather_adaptation_heat_differential",
                "hub_weather_adaptation_heat_target",
            ],
        )
        number = added[1][0]
        assert isinstance(number, PreferenceNumber)
        await async_release_preferences(cast("Any", self.hass), self.entry.entry_id)
        await async_release(cast("Any", self.hass), self.entry.entry_id)

    async def test_shutdown_drains_admitted_work_and_rejects_late_writes(self) -> None:
        coordinator = self._coordinator(deadline=2)
        numbers = self._numbers(coordinator)
        await coordinator.async_refresh()
        self.hold_once = True
        admitted = asyncio.create_task(
            numbers[PreferenceField.AWAY_MINIMUM].async_set_native_value(12)
        )
        await asyncio.wait_for(self.started.wait(), 1)
        queued = asyncio.create_task(
            numbers[PreferenceField.AWAY_MAXIMUM].async_set_native_value(30)
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
        self.assertEqual(snapshot.away_minimum_c, 12)
        self.assertEqual(snapshot.away_maximum_c, 28)
        self.assertFalse(coordinator.last_update_success)
        self.assertFalse(numbers[PreferenceField.AWAY_MINIMUM].available)
        seen = self.connections
        with self.assertRaises(HomeAssistantError):
            await numbers[PreferenceField.SLEEP_COOL].async_set_native_value(24)
        await coordinator.async_refresh()
        self.assertEqual(self.connections, seen)

    async def test_unload_stops_later_refreshes(self) -> None:
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        self.assertEqual(coordinator.update_interval, timedelta(seconds=60))
        self.assertIn(coordinator.async_shutdown, self.entry.unloads)
        seen = self.connections
        for callback in self.entry.unloads:
            result = callback()
            if asyncio.iscoroutine(result):
                await result
        await coordinator.async_refresh()
        self.assertEqual(self.connections, seen)

    async def test_legacy_constructor_keeps_the_entry_unload_binding(self) -> None:
        original_init = DataUpdateCoordinator.__init__

        def legacy_init(
            instance: DataUpdateCoordinator[PreferenceSnapshot],
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

    def _bind_root(self, api: _Api) -> tuple[Any, Any, Any]:
        def connect(*, model: object, host: str) -> _Api:
            del model, host
            return api

        def preference_client(
            host: str, port: int, *, deadline: float = DEFAULT_DEADLINE
        ) -> ConsoleClient:
            del port
            return ConsoleClient(host, self.port, deadline=deadline)

        def smart_client(
            host: str, port: int, *, deadline: float = DEFAULT_DEADLINE
        ) -> ConsoleClient:
            del port
            return ConsoleClient(host, self.port, deadline=deadline)

        return (
            patch("custom_components.airtouch.pyairtouch.connect", connect),
            patch(
                "custom_components.airtouch.preferences.ConsoleClient",
                preference_client,
            ),
            patch(
                "custom_components.airtouch.smart_settings.ConsoleClient",
                smart_client,
            ),
        )

    async def test_install_is_shared_and_excludes_airtouch_4(self) -> None:
        before = self.connections
        owner = async_install_preferences(
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
            get_preferences(cast("Any", self.hass), self.entry.entry_id), owner
        )
        with self.assertRaisesRegex(RuntimeError, "already installed"):
            async_install_preferences(
                cast("Any", self.hass),
                cast("Any", self.entry),
                cast("Any", _AirTouch()),
                port=self.port,
            )
        self.assertIsNone(
            async_install_preferences(
                cast("Any", self.hass),
                cast("Any", _Entry()),
                cast("Any", _AirTouch(AirTouchModel.AIRTOUCH_4)),
                port=self.port,
            )
        )
        empty = await async_collect_preference_numbers(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch(AirTouchModel.AIRTOUCH_4)),
            unique_id="hub",
            device_info=cast("Any", _DEVICE_INFO),
        )
        self.assertEqual(empty, [])
        self.assertEqual(self.connections, before)
        first = await async_collect_preference_numbers(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            unique_id="hub",
            device_info=cast("Any", _DEVICE_INFO),
        )
        second = await async_collect_preference_numbers(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            unique_id="hub",
            device_info=cast("Any", _DEVICE_INFO),
        )
        self.assertEqual(len(first), 4)
        self.assertTrue(
            all(
                isinstance(number, PreferenceNumber) and number.coordinator is owner
                for number in first
            )
        )
        self.assertTrue(
            all(
                isinstance(number, PreferenceNumber) and number.coordinator is owner
                for number in second
            )
        )
        assert isinstance(first[0], PreferenceNumber)
        assert isinstance(second[0], PreferenceNumber)
        self.assertIs(first[0].coordinator.data, second[0].coordinator.data)
        api_marker = object()
        lock = object()
        self.hass.data[DOMAIN] = {"lock": lock, self.entry.entry_id: api_marker}
        self.assertIs(
            get_preferences(cast("Any", self.hass), self.entry.entry_id), owner
        )
        self.assertIs(self.hass.data[DOMAIN][self.entry.entry_id], api_marker)
        self.assertIs(self.hass.data[DOMAIN]["lock"], lock)

    async def test_two_entries_release_reload_and_overlap(self) -> None:
        left_entry = _Entry()
        left_entry.entry_id = "left"
        right_entry = _Entry()
        right_entry.entry_id = "right"
        left = async_install_preferences(
            cast("Any", self.hass),
            cast("Any", left_entry),
            cast("Any", _AirTouch()),
            port=self.port,
        )
        right = async_install_preferences(
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
        await async_release_preferences(cast("Any", self.hass), left_entry.entry_id)
        await async_release_preferences(cast("Any", self.hass), left_entry.entry_id)
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_preferences(cast("Any", self.hass), left_entry.entry_id)
        self.assertIs(
            get_preferences(cast("Any", self.hass), right_entry.entry_id), right
        )
        self.assertEqual(set(self.hass.data[DOMAIN]), domain_ids)
        seen = self.connections
        await left.async_refresh()
        self.assertEqual(self.connections, seen)
        await right.async_refresh()
        self.assertGreater(self.connections, seen)
        reloaded = async_install_preferences(
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
            get_preferences(cast("Any", self.hass), left_entry.entry_id), reloaded
        )
        self.hold_all = True
        self.max_active = 0
        self.release.clear()
        both = asyncio.gather(reloaded.async_refresh(), right.async_refresh())
        await self._wait_until_active(2)
        self.assertEqual(self.max_active, 2)
        self.release.set()
        await asyncio.wait_for(both, 2)

    async def test_root_setup_installs_before_forward_without_reading(self) -> None:
        api = _Api()
        entry = _RootEntry("root", SelectedModel.AIRTOUCH_5.value)
        forwarded: list[str] = []

        async def forward(config_entry: object, platforms: object) -> None:
            del platforms
            forwarded.append("forward")
            self.assertEqual(self.connections, 0)
            owner = get_preferences(
                cast("Any", self.hass), cast("Any", config_entry).entry_id
            )
            self.coordinators.append(owner)
            self.assertIs(self.hass.data[DOMAIN][entry.entry_id], api)

        self.hass.config_entries = _ConfigEntries(forward)
        connect, preferences, smart = self._bind_root(api)
        with connect, preferences, smart:
            result = await async_setup_integration(
                cast("Any", self.hass), cast("Any", entry)
            )
        self.assertTrue(result)
        self.assertEqual(forwarded, ["forward"])
        owner = get_preferences(cast("Any", self.hass), entry.entry_id)
        self.assertIs(owner.data, None)
        self.assertIs(self.hass.data[DOMAIN][entry.entry_id], api)
        self.assertEqual(api.shutdown_calls, 0)

    async def test_failed_initial_read_does_not_block_climate(self) -> None:
        api = _Api()
        api.air_conditioners = [_Ac(0, "East")]
        entry = _RootEntry("root", SelectedModel.AIRTOUCH_5.value)
        self.mode = "truncate"
        added: list[Any] = []

        async def forward(config_entry: object, platforms: object) -> None:
            del platforms
            self.assertEqual(self.connections, 0)

            added.extend(
                await async_collect_preference_numbers(
                    cast("Any", self.hass),
                    cast("Any", config_entry),
                    cast("Any", api),
                    unique_id="hub",
                    device_info=cast("Any", _DEVICE_INFO),
                )
            )

            def add_climate(
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
                    cast("Any", self.hass), cast("Any", config_entry), add_climate
                )

        self.hass.config_entries = _ConfigEntries(forward)
        connect, preferences, smart = self._bind_root(api)
        with connect, preferences, smart:
            result = await async_setup_integration(
                cast("Any", self.hass), cast("Any", entry)
            )
        self.assertTrue(result)
        preference_numbers = [
            entity for entity in added if isinstance(entity, PreferenceNumber)
        ]
        self.assertEqual(len(preference_numbers), 4)
        self.assertTrue(all(not entity.available for entity in preference_numbers))
        self.assertIn("hub_ac0", [entity.unique_id for entity in added])
        owner = get_preferences(cast("Any", self.hass), entry.entry_id)
        self.coordinators.append(owner)
        self.assertFalse(owner.last_update_success)
        self.assertEqual(api.shutdown_calls, 0)
        self.assertIs(self.hass.data[DOMAIN][entry.entry_id], api)

    async def test_forward_failure_and_cancellation_release_the_owner(self) -> None:
        api = _Api()
        for error in (RuntimeError("forward failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                entry = _RootEntry(type(error).__name__, SelectedModel.AIRTOUCH_5.value)

                async def forward(
                    config_entry: object,
                    platforms: object,
                    *,
                    failure: BaseException = error,
                ) -> None:
                    del platforms
                    self.assertEqual(self.connections, 0)
                    get_preferences(
                        cast("Any", self.hass), cast("Any", config_entry).entry_id
                    )
                    raise failure

                self.hass.config_entries = _ConfigEntries(forward)
                connect, preferences, smart = self._bind_root(api)
                with (
                    connect,
                    preferences,
                    smart,
                    self.assertRaises(type(error)),
                ):
                    await async_setup_integration(
                        cast("Any", self.hass), cast("Any", entry)
                    )
                with self.assertRaisesRegex(RuntimeError, "not installed"):
                    get_preferences(cast("Any", self.hass), entry.entry_id)
                self.assertIs(self.hass.data[DOMAIN][entry.entry_id], api)
                self.assertEqual(api.shutdown_calls, 0)
                self.assertEqual(self.connections, 0)

        started = asyncio.Event()
        cancelled = _RootEntry("cancelled", SelectedModel.AIRTOUCH_5.value)

        async def wait_forward(config_entry: object, platforms: object) -> None:
            del config_entry, platforms
            started.set()
            await asyncio.Event().wait()

        self.hass.config_entries = _ConfigEntries(wait_forward)
        connect, preferences, smart = self._bind_root(api)
        with connect, preferences, smart:
            task = asyncio.create_task(
                async_setup_integration(cast("Any", self.hass), cast("Any", cancelled))
            )
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(self.connections, 0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_preferences(cast("Any", self.hass), cancelled.entry_id)
        self.assertIs(self.hass.data[DOMAIN][cancelled.entry_id], api)
        self.assertEqual(api.shutdown_calls, 0)

    async def test_cleanup_errors_keep_the_setup_error(self) -> None:
        api = _Api()
        entry = _RootEntry("cleanup", SelectedModel.AIRTOUCH_5.value)

        async def fail_forward(config_entry: object, platforms: object) -> None:
            del platforms
            get_preferences(cast("Any", self.hass), cast("Any", config_entry).entry_id)
            raise RuntimeError("forward failed")

        async def bad_preferences(hass: object, entry_id: str) -> None:
            del hass, entry_id
            raise RuntimeError("preference release failed")

        async def bad_ac(hass: object, entry_id: str) -> None:
            del hass, entry_id
            raise RuntimeError("ac release failed")

        self.hass.config_entries = _ConfigEntries(fail_forward)
        connect, preferences, smart = self._bind_root(api)
        with (
            connect,
            preferences,
            smart,
            patch("custom_components.airtouch.async_release_ac_settings", bad_ac),
            patch(
                "custom_components.airtouch.async_release_preferences",
                bad_preferences,
            ),
            self.assertLogs("custom_components.airtouch", level="ERROR") as logged,
            self.assertRaisesRegex(RuntimeError, "forward failed"),
        ):
            await async_setup_integration(cast("Any", self.hass), cast("Any", entry))
        output = "\n".join(logged.output)
        self.assertIn("Error releasing AirTouch AC settings", output)
        self.assertIn("Error releasing AirTouch preferences", output)
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_smart_settings(cast("Any", self.hass), entry.entry_id)
        self.assertIs(self.hass.data[DOMAIN][entry.entry_id], api)
        self.assertEqual(api.shutdown_calls, 0)
        await async_release_preferences(cast("Any", self.hass), entry.entry_id)

        created = _RootEntry("create", SelectedModel.AIRTOUCH_5.value)

        def broken_client(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise RuntimeError("preference create failed")

        async def quiet_forward(config_entry: object, platforms: object) -> None:
            del config_entry, platforms

        self.hass.config_entries = _ConfigEntries(quiet_forward)
        connect, preferences, smart = self._bind_root(api)
        with (
            connect,
            preferences,
            smart,
            patch(
                "custom_components.airtouch.preferences.ConsoleClient",
                broken_client,
            ),
            self.assertRaisesRegex(RuntimeError, "preference create failed"),
        ):
            await async_setup_integration(cast("Any", self.hass), cast("Any", created))
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_preferences(cast("Any", self.hass), created.entry_id)
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_smart_settings(cast("Any", self.hass), created.entry_id)
        self.assertIs(self.hass.data[DOMAIN][created.entry_id], api)
        self.assertEqual(api.shutdown_calls, 0)

    async def test_unload_false_retains_and_success_closes(self) -> None:
        api = _Api()
        entry = _RootEntry("root", SelectedModel.AIRTOUCH_5.value)

        async def forward(config_entry: object, platforms: object) -> None:
            del platforms
            owner = get_preferences(
                cast("Any", self.hass), cast("Any", config_entry).entry_id
            )
            self.coordinators.append(owner)

        self.hass.config_entries = _ConfigEntries(forward, unload_ok=False)
        connect, preferences, smart = self._bind_root(api)
        with connect, preferences, smart:
            self.assertTrue(
                await async_setup_integration(
                    cast("Any", self.hass), cast("Any", entry)
                )
            )
        owner = get_preferences(cast("Any", self.hass), entry.entry_id)
        self.assertFalse(
            await async_unload_integration(cast("Any", self.hass), cast("Any", entry))
        )
        self.assertIs(get_preferences(cast("Any", self.hass), entry.entry_id), owner)
        self.assertEqual(api.shutdown_calls, 0)
        seen = self.connections
        await owner.async_refresh()
        self.assertGreater(self.connections, seen)
        number = self._numbers(owner)[PreferenceField.AWAY_MINIMUM]
        await number.async_set_native_value(19)
        self.hass.config_entries.unload_ok = True
        self.assertTrue(
            await async_unload_integration(cast("Any", self.hass), cast("Any", entry))
        )
        self.assertEqual(api.shutdown_calls, 1)
        self.assertNotIn(entry.entry_id, self.hass.data[DOMAIN])
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_preferences(cast("Any", self.hass), entry.entry_id)
        before = self.connections
        with self.assertRaises(HomeAssistantError):
            await number.async_set_native_value(20)
        self.assertEqual(self.connections, before)

    async def _wait_until_active(self, count: int) -> None:
        for _ in range(50):
            if self.active >= count:
                return
            await asyncio.sleep(0.02)
        self.fail(f"timed out waiting for {count} active connections")


def _preference_numbers(
    coordinator: PreferenceCoordinator,
) -> list[PreferenceNumber]:
    return [
        PreferenceNumber(
            coordinator,
            "hub",
            cast("Any", _DEVICE_INFO),
            field,
        )
        for field in PreferenceField
    ]


def _write_frames(raw_frames: list[bytes]) -> list[tuple[At5Header, bytes]]:
    found: list[tuple[At5Header, bytes]] = []
    for raw in raw_frames:
        header, payload = _oracle_decode(raw)
        if payload.startswith(_WRITE_HEADER):
            found.append((header, payload))
    return found


def _reply(packet_id: int, payload: bytes) -> bytes:
    return _oracle_frame(
        to_address=0xB0,
        from_address=0x80,
        packet_id=packet_id,
        payload=payload,
    )


def _decoy_prefix() -> bytes:
    prefix = bytearray(_console_prefix())
    prefix[20] = 1
    prefix[21] = 0
    return bytes(prefix)


if __name__ == "__main__":
    unittest.main()
