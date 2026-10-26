from __future__ import annotations

import asyncio
import contextlib
import struct
import tracemalloc
import unittest
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch
from zoneinfo import ZoneInfo

from pyairtouch import AirTouchModel

if TYPE_CHECKING:
    from collections.abc import Callable
    from logging import Logger

    from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pyairtouch.at5.comms.hdr import (
    ADDRESS_AIRTOUCH_EXTENDED,
    ADDRESS_CLIENT,
    At5Header,
    HeaderDecoder,
    HeaderEncoder,
)
from pyairtouch.at5.comms.x1F_ext import MESSAGE_ID as EXTENDED_MESSAGE_ID
from pyairtouch.comms.crc16 import Crc16Modbus

from custom_components.airtouch.console import (
    ConsoleChecksumError,
    ConsoleClient,
    ConsoleProtocolError,
    encode_frame,
)
from custom_components.airtouch.sensor import (
    AcRunTimeEntity,
    ZoneRunTimeEntity,
    async_collect_usage_entities,
)
from custom_components.airtouch.usage import (
    DAILY_RESOLUTION_MINUTES,
    HOURLY_RESOLUTION_MINUTES,
    UsageCoordinator,
    UsageParseError,
    UsageSnapshot,
    parse_usage_history,
)

_OUTER = b"\x55\x55\x55\xab"
_INNER = b"\x55\x55\x55\xaa"
_ENCODER = HeaderEncoder()
_DECODER = HeaderDecoder()
_CRC = Crc16Modbus()
_LONG_ZERO_ON = bytes.fromhex("800214000000000000000000c00504060201028a0834")
# Two readings share these bytes. One is a 4-byte air conditioner, a 12-byte
# air conditioner, and a trailer. The other is a 12-byte air conditioner, a
# 4-byte air conditioner, and the same trailer. The on-time byte is zero in
# both air conditioners, so neither reading can be chosen from that byte alone.
_AMBIGUOUS = bytes.fromhex("8002000081020000000000008202000001060201070007d0")
_UTC = ZoneInfo("UTC")


def _oracle_escape(inner: bytes) -> bytes:
    """Console rule from ConnectionBase.onSend, written separately from the client."""
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
    if len(frame) < 10 or not frame.startswith(_OUTER):
        raise AssertionError("oracle saw a frame without the outer prefix")
    length_a, length_b = struct.unpack_from("!HH", frame, 6)
    if frame[4:6] != b"\x00\x00" or length_a != length_b:
        raise AssertionError("oracle saw inconsistent outer lengths")
    escaped = frame[10 : 10 + length_a]
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
    checksum = rest[decoded.header.message_length :]
    if not _CRC.validate(decoded.checksum_data + payload, checksum):
        raise AssertionError("oracle checksum failed")
    return decoded.header, payload


async def _oracle_read(reader: asyncio.StreamReader) -> tuple[At5Header, bytes]:
    outer = await reader.readexactly(10)
    length = struct.unpack_from("!H", outer, 6)[0]
    body = await reader.readexactly(length)
    return _oracle_decode(outer + body)


class _Entry:
    def __init__(self) -> None:
        self.title = "usage-test"
        self.domain = "airtouch"
        self.pref_disable_polling = False
        self.unloads: list[Any] = []

    def async_on_unload(self, func: Callable[[], object]) -> None:
        self.unloads.append(func)


class _Config:
    def __init__(self) -> None:
        self.time_zone = "UTC"


class _Hass:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.config = _Config()
        self.data: dict[str, Any] = {}

    @property
    def is_stopping(self) -> bool:
        return False


class _AirTouch:
    def __init__(self, model: AirTouchModel = AirTouchModel.AIRTOUCH_5) -> None:
        self.host = "127.0.0.1"
        self.model = model
        self.air_conditioners: list[Any] = []


class UsageParserTests(unittest.TestCase):
    def test_long_daily_record_keeps_zero_on_time(self) -> None:
        """A 12-byte daily record can store on time that rounded to zero."""
        history = parse_usage_history(
            _LONG_ZERO_ON, resolution_minutes=DAILY_RESOLUTION_MINUTES
        )
        self.assertEqual(history.resolution_minutes, DAILY_RESOLUTION_MINUTES)
        self.assertEqual(len(history.buckets), 1)
        bucket = history.buckets[0]
        self.assertEqual(bucket.index, 4)
        self.assertEqual(bucket.minimum_temperature_c, 15.0)
        self.assertIsNone(bucket.maximum_temperature_c)
        usage = history.ac_usage(0, 4)
        assert usage is not None
        self.assertEqual(usage.on_minutes, 0)
        self.assertEqual(usage.cool_minutes, 0)
        self.assertEqual(usage.average_setpoint_c, 12.0)
        self.assertEqual(history.zone_on_minutes(0, 4), 30)
        self.assertIsNone(history.ac_usage(0, 5))

    def test_same_bytes_are_one_minute_units_when_hourly(self) -> None:
        history = parse_usage_history(
            _LONG_ZERO_ON, resolution_minutes=HOURLY_RESOLUTION_MINUTES
        )
        self.assertEqual(history.zone_on_minutes(0, 4), 5)
        usage = history.ac_usage(0, 4)
        assert usage is not None
        self.assertEqual(usage.on_minutes, 0)

    def test_short_record_when_the_long_duplicate_does_not_match(self) -> None:
        body = (
            bytes([0x80, 2, 10, 0, 0xC0, 4, 3, 6, 2, 1]) + (2000).to_bytes(2, "big") * 2
        )
        history = parse_usage_history(body, resolution_minutes=DAILY_RESOLUTION_MINUTES)
        usage = history.ac_usage(0, 3)
        assert usage is not None
        self.assertEqual(usage.on_minutes, 0)
        self.assertEqual(usage.average_setpoint_c, 11.0)
        self.assertEqual(history.zone_on_minutes(0, 3), 24)
        self.assertIsNone(history.buckets[0].minimum_temperature_c)

    def test_omitted_air_conditioner_is_zero_and_missing_day_is_unknown(self) -> None:
        body = (
            bytes([0x82, 2, 30, 4, 1, 0, 2, 0, 0, 0, 0, 4])
            + bytes([0xC0, 1, 2, 6, 2, 1])
            + (2000).to_bytes(2, "big") * 2
        )
        history = parse_usage_history(body, resolution_minutes=DAILY_RESOLUTION_MINUTES)
        present = history.ac_usage(2, 2)
        omitted = history.ac_usage(0, 2)
        assert present is not None
        assert omitted is not None
        self.assertEqual(present.on_minutes, 24)
        self.assertEqual(present.away_minutes, 6)
        self.assertEqual(present.cool_minutes, 12)
        self.assertEqual(present.average_setpoint_c, 13.0)
        self.assertEqual(omitted.on_minutes, 0)
        self.assertIsNone(omitted.average_setpoint_c)
        self.assertIsNone(history.ac_usage(2, 1))
        self.assertIsNone(history.zone_on_minutes(1, 2))

    def test_day_31_is_kept_for_a_short_month(self) -> None:
        body = bytes([31, 6, 0, 1]) + (2000).to_bytes(2, "big") * 2
        history = parse_usage_history(body, resolution_minutes=DAILY_RESOLUTION_MINUTES)
        self.assertEqual(history.buckets[0].index, 31)

    def test_mode_names_follow_the_console_counter_order(self) -> None:
        counters = (5, 4, 3, 2, 1, 6, 7, 8)
        body = (
            bytes([0x80, 2, 0, *counters, counters[0], 1, 6, 1, 1])
            + (2000).to_bytes(2, "big") * 2
        )
        usage = parse_usage_history(
            body, resolution_minutes=HOURLY_RESOLUTION_MINUTES
        ).ac_usage(0, 1)
        assert usage is not None
        self.assertEqual(
            (
                usage.on_minutes,
                usage.away_minutes,
                usage.sleep_minutes,
                usage.cool_minutes,
                usage.heat_minutes,
                usage.fan_minutes,
                usage.dry_minutes,
                usage.weather_adapt_minutes,
            ),
            counters,
        )
        self.assertIsNone(usage.average_setpoint_c)

    def test_rejects_malformed_and_duplicate_days(self) -> None:
        valid = bytes([1, 6, 0, 1]) + (2000).to_bytes(2, "big") * 2
        with self.assertRaisesRegex(UsageParseError, "malformed"):
            parse_usage_history(valid + b"\xff", resolution_minutes=1)
        with self.assertRaisesRegex(UsageParseError, "malformed"):
            parse_usage_history(valid + valid, resolution_minutes=6)
        with self.assertRaisesRegex(UsageParseError, "malformed"):
            parse_usage_history(
                bytes([0xC1, 1, 1, 6, 1, 1, 0, 0, 0, 0]),
                resolution_minutes=6,
            )
        with self.assertRaisesRegex(ValueError, "resolution_minutes"):
            parse_usage_history(b"", resolution_minutes=3)

    def test_empty_report_has_no_buckets(self) -> None:
        history = parse_usage_history(b"", resolution_minutes=6)
        self.assertEqual(history.buckets, ())

    def test_rejects_an_ambiguous_record(self) -> None:
        with self.assertRaisesRegex(UsageParseError, "ambiguous"):
            parse_usage_history(_AMBIGUOUS, resolution_minutes=DAILY_RESOLUTION_MINUTES)

    def test_rejects_expanding_ambiguity_with_bounded_memory(self) -> None:
        groups = 14
        body = bytearray()
        for group in range(groups):
            fragment = bytearray(_AMBIGUOUS[:16])
            for offset in (0, 4, 12):
                fragment[offset] += group * 3
            body.extend(fragment)
        body.extend(bytes([1, 6, groups * 2, 1, 7, 208, 7, 208]))
        tracemalloc.start()
        try:
            with self.assertRaisesRegex(UsageParseError, "ambiguous"):
                parse_usage_history(
                    bytes(body), resolution_minutes=DAILY_RESOLUTION_MINUTES
                )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1_000_000)


class UsageViewTests(unittest.TestCase):
    def test_future_and_invalid_days_stay_out_of_the_attribute(self) -> None:
        day_one = (
            bytes([0x80, 2, 0, 2, 0, 0, 1, 0, 0, 0, 0, 2, 1, 6, 1, 1])
            + (600).to_bytes(2, "big") * 2
        )
        day_ten = (
            bytes([0x80, 2, 0, 2, 0, 0, 1, 0, 0, 0, 0, 2, 10, 6, 1, 1])
            + (600).to_bytes(2, "big") * 2
        )
        day_31 = bytes([31, 6, 0, 1]) + (2000).to_bytes(2, "big") * 2
        hour_zero = (
            bytes([0x80, 2, 0, 9, 0, 0, 9, 0, 0, 0, 0, 9, 0, 6, 1, 1])
            + (2000).to_bytes(2, "big") * 2
        )
        hour_three = bytes([3, 6, 0, 1]) + (2000).to_bytes(2, "big") * 2
        snapshot = UsageSnapshot(
            local_date=datetime(2026, 2, 10, tzinfo=_UTC).date(),
            daily=parse_usage_history(
                day_one + day_ten + day_31,
                resolution_minutes=DAILY_RESOLUTION_MINUTES,
            ),
            hourly=parse_usage_history(
                hour_zero + hour_three, resolution_minutes=HOURLY_RESOLUTION_MINUTES
            ),
        )
        now = datetime(2026, 2, 10, 2, 15, tzinfo=_UTC)
        attributes = snapshot.ac_attributes(0, now=now)
        assert attributes is not None
        daily = cast("list[dict[str, Any]]", attributes["daily"])
        hourly = cast("list[dict[str, Any]]", attributes["hourly"])
        self.assertEqual([row["day"] for row in daily], list(range(1, 11)))
        self.assertEqual(daily[0]["on_minutes"], 12)
        self.assertEqual(daily[0]["cool_minutes"], 6)
        self.assertEqual(daily[0]["minimum_temperature_c"], 10.0)
        self.assertIsNone(daily[1]["on_minutes"])
        self.assertEqual(daily[9]["on_minutes"], 12)
        self.assertNotEqual(sum(row["on_minutes"] or 0 for row in hourly), 12)
        self.assertEqual([row["hour"] for row in hourly], [0, 1, 2])
        self.assertEqual(hourly[0]["on_minutes"], 9)
        self.assertIsNone(hourly[1]["on_minutes"])
        self.assertEqual(
            attributes["mode_breakdown"],
            {
                "on_minutes": 12,
                "away_minutes": 0,
                "sleep_minutes": 0,
                "cool_minutes": 6,
                "heat_minutes": 0,
                "fan_minutes": 0,
                "dry_minutes": 0,
                "weather_adapt_minutes": 0,
                "average_setpoint_c": None,
            },
        )
        self.assertIsNone(
            snapshot.ac_attributes(0, now=datetime(2026, 2, 11, 0, 5, tzinfo=_UTC))
        )
        self.assertIsNone(
            snapshot.ac_today_minutes(0, now=datetime(2026, 3, 1, tzinfo=_UTC))
        )
        self.assertEqual(snapshot.ac_today_minutes(0, now=now), 12)
        zones = snapshot.zone_attributes(0, now=now)
        assert zones is not None
        self.assertEqual(
            cast("list[dict[str, Any]]", zones["daily"])[0]["on_minutes"], None
        )


class ConsoleFrameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server_payloads: list[bytes] = []
        self._handlers: list[asyncio.Task[None]] = []
        self.server = await asyncio.start_server(self._dispatch, "127.0.0.1", 0)
        sockets = self.server.sockets
        if not sockets:
            raise RuntimeError("test server did not bind a socket")
        self.port = sockets[0].getsockname()[1]
        self.response_payloads: list[bytes | None] = []
        self.bad_checksum = False
        self.prefix_only = False
        self.mismatch_lengths = False
        self.close_immediately = False
        self.extra_frame = False
        self.fragment = False

    async def asyncTearDown(self) -> None:
        for task in self._handlers:
            task.cancel()
        if self._handlers:
            await asyncio.wait(self._handlers)
        self.server.close()
        await self.server.wait_closed()

    async def _dispatch(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.append(task)
        try:
            await self._handle(reader, writer)
        finally:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self.prefix_only:
            writer.write(b"\x00" * 10)
            await writer.drain()
            await asyncio.sleep(5)
            return
        if self.mismatch_lengths:
            writer.write(
                _OUTER + b"\x00\x00" + (10).to_bytes(2, "big") + (11).to_bytes(2, "big")
            )
            await writer.drain()
            await asyncio.sleep(5)
            return
        if self.close_immediately:
            return
        header, payload = await _oracle_read(reader)
        self.server_payloads.append(payload)
        action = self.response_payloads.pop(0)
        if action is None:
            await reader.read(1)
            return
        frame = _oracle_frame(
            to_address=ADDRESS_CLIENT,
            from_address=ADDRESS_AIRTOUCH_EXTENDED,
            packet_id=header.packet_id,
            payload=payload + action,
        )
        if self.bad_checksum:
            frame = frame[:-1] + bytes([frame[-1] ^ 0xFF])
        if self.extra_frame:
            other = _oracle_frame(
                to_address=ADDRESS_CLIENT,
                from_address=ADDRESS_AIRTOUCH_EXTENDED,
                packet_id=(header.packet_id + 1) & 0xFF,
                payload=payload + b"\x01",
            )
            frame = other + frame
        if self.fragment:
            for index in range(len(frame)):
                writer.write(frame[index : index + 1])
                await writer.drain()
            return
        writer.write(frame)
        await writer.drain()

    def _client(self, deadline: float = 2.0) -> ConsoleClient:
        return ConsoleClient("127.0.0.1", self.port, deadline=deadline)

    async def test_escaped_frame_matches_the_console_rule(self) -> None:
        payload = b"\xff\x81" + (b"\x55" * 6)
        header = At5Header(
            to_address=ADDRESS_AIRTOUCH_EXTENDED,
            from_address=ADDRESS_CLIENT,
            packet_id=7,
            message_id=EXTENDED_MESSAGE_ID,
            message_length=len(payload),
        )
        actual = encode_frame(header, payload)
        expected = _oracle_frame(
            to_address=ADDRESS_AIRTOUCH_EXTENDED,
            from_address=ADDRESS_CLIENT,
            packet_id=7,
            payload=payload,
        )
        self.assertEqual(actual, expected)
        self.assertIn(b"\x55\x55\x55\x00", actual)
        decoded_header, decoded_payload = _oracle_decode(actual)
        self.assertEqual(decoded_header.packet_id, 7)
        self.assertEqual(decoded_payload, payload)

    async def test_fragmented_response_returns_the_body(self) -> None:
        self.fragment = True
        self.response_payloads = [b"abc"]
        body = await self._client().request(b"\xff\x81" + b"2510")
        self.assertEqual(body, b"abc")
        self.assertEqual(self.server_payloads, [b"\xff\x81" + b"2510"])

    async def test_unrelated_valid_frame_is_skipped(self) -> None:
        self.extra_frame = True
        self.response_payloads = [b"wanted"]
        body = await self._client().request(b"\xff\x82" + b"251026")
        self.assertEqual(body, b"wanted")

    async def test_checksum_error_fails_without_a_resync(self) -> None:
        self.bad_checksum = True
        self.response_payloads = [b"body"]
        with self.assertRaises(ConsoleChecksumError):
            await self._client().request(b"\xff\x81" + b"2510")

    async def test_eof_and_bad_headers_fail_quickly(self) -> None:
        self.close_immediately = True
        with self.assertRaises(ConsoleProtocolError):
            await self._client().request(b"\xff\x81" + b"2510")
        self.close_immediately = False
        self.prefix_only = True
        started = asyncio.get_running_loop().time()
        with self.assertRaises(ConsoleProtocolError):
            await self._client(deadline=2).request(b"\xff\x81" + b"2510")
        self.assertLess(asyncio.get_running_loop().time() - started, 0.5)
        self.prefix_only = False
        self.mismatch_lengths = True
        started = asyncio.get_running_loop().time()
        with self.assertRaises(ConsoleProtocolError):
            await self._client(deadline=2).request(b"\xff\x81" + b"2510")
        self.assertLess(asyncio.get_running_loop().time() - started, 0.5)

    async def test_timeout_and_cancellation_close_and_release_the_lock(self) -> None:
        self.response_payloads = [None, b"second"]
        client = self._client(deadline=0.3)
        with self.assertRaises(TimeoutError):
            await client.request(b"\xff\x81" + b"2510")
        body = await client.request(b"\xff\x81" + b"2510")
        self.assertEqual(body, b"second")

        self.response_payloads = [None, b"after-cancel"]
        client = self._client(deadline=2)
        task = asyncio.create_task(client.request(b"\xff\x81" + b"2510"))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        body = await client.request(b"\xff\x81" + b"2510")
        self.assertEqual(body, b"after-cancel")


class UsageCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.payloads: list[bytes] = []
        self.responses: list[bytes] = [b"", b""]
        self.connections = 0
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sockets = self.server.sockets
        if not sockets:
            raise RuntimeError("test server did not bind a socket")
        self.port = sockets[0].getsockname()[1]
        self.hass = _Hass()
        self.entry = _Entry()

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        try:
            header, payload = await _oracle_read(reader)
            self.payloads.append(payload)
            body = self.responses.pop(0)
            writer.write(
                _oracle_frame(
                    to_address=ADDRESS_CLIENT,
                    from_address=ADDRESS_AIRTOUCH_EXTENDED,
                    packet_id=header.packet_id,
                    payload=payload + body,
                )
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def _coordinator(self) -> UsageCoordinator:
        return UsageCoordinator(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            port=self.port,
            deadline=1.0,
        )

    async def test_legacy_constructor_keeps_the_entry_unload_binding(self) -> None:
        original_init = DataUpdateCoordinator.__init__

        def legacy_init(
            instance: DataUpdateCoordinator[UsageSnapshot],
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
        await coordinator.async_shutdown()

    async def test_both_reports_publish_and_a_later_failure_stays_unavailable(
        self,
    ) -> None:
        daily = bytes([1, 6, 0, 1]) + (2000).to_bytes(2, "big") * 2
        self.responses = [daily, b""]
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        self.assertTrue(coordinator.last_update_success)
        snapshot = coordinator.data
        assert snapshot is not None
        self.assertEqual(snapshot.daily.buckets[0].index, 1)
        self.assertEqual(snapshot.hourly.buckets, ())
        self.assertEqual(self.payloads[0][:2], b"\xff\x81")
        self.assertEqual(self.payloads[1][:2], b"\xff\x82")
        self.assertEqual(
            self.payloads[0][2:].decode("ascii"), snapshot.local_date.strftime("%y%m")
        )
        self.assertEqual(
            self.payloads[1][2:].decode("ascii"),
            snapshot.local_date.strftime("%y%m%d"),
        )
        self.responses = [b"", b"\xff"]
        await coordinator.async_refresh()
        self.assertFalse(coordinator.last_update_success)
        self.assertEqual(coordinator.data, snapshot)
        entity = AcRunTimeEntity(
            coordinator,
            "hub_ac0",
            {"identifiers": {("airtouch", "hub_ac0")}, "name": "AC"},
            0,
        )
        entity.hass = cast("Any", self.hass)
        self.assertFalse(entity.available)
        self.assertIsNone(entity.native_value)

    async def test_date_rollover_hides_the_previous_day(self) -> None:
        coordinator = self._coordinator()
        now = datetime.now(_UTC)
        untouched = AcRunTimeEntity(
            coordinator,
            "hub_ac0",
            {"identifiers": {("airtouch", "hub_ac0")}, "name": "AC"},
            0,
        )
        untouched.hass = cast("Any", self.hass)
        self.assertFalse(untouched.available)
        self.assertIsNone(untouched.native_value)
        previous = (now - timedelta(days=1)).date()
        snapshot = UsageSnapshot(
            local_date=previous,
            daily=parse_usage_history(
                bytes([previous.day, 6, 0, 1]) + (2000).to_bytes(2, "big") * 2,
                resolution_minutes=6,
            ),
            hourly=parse_usage_history(b"", resolution_minutes=1),
        )
        coordinator.data = snapshot
        coordinator.last_update_success = True
        entity = AcRunTimeEntity(
            coordinator,
            "hub_ac0",
            {"identifiers": {("airtouch", "hub_ac0")}, "name": "AC"},
            0,
        )
        entity.hass = cast("Any", self.hass)
        self.assertFalse(entity.available)
        self.assertIsNone(entity.native_value)
        current = UsageSnapshot(
            local_date=now.date(),
            daily=parse_usage_history(
                bytes([now.day, 6, 0, 1]) + (2000).to_bytes(2, "big") * 2,
                resolution_minutes=6,
            ),
            hourly=parse_usage_history(b"", resolution_minutes=1),
        )
        coordinator.data = current
        self.assertTrue(entity.available)
        self.assertEqual(entity.native_value, 0)
        zone = ZoneRunTimeEntity(
            coordinator,
            "hub_zone0",
            {"identifiers": {("airtouch", "hub_zone0")}, "name": "Zone"},
            0,
        )
        zone.hass = cast("Any", self.hass)
        self.assertTrue(zone.available)
        self.assertIsNone(zone.native_value)

    async def test_airtouch_4_adds_no_usage_entities(self) -> None:
        entities = await async_collect_usage_entities(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch(AirTouchModel.AIRTOUCH_4)),
            cast("Any", object()),
        )
        self.assertEqual(entities, [])
        self.assertEqual(self.connections, 0)

    async def test_local_midnight_notifies_entities_before_the_next_poll(self) -> None:
        self.responses = [b"", b""]
        coordinator = self._coordinator()
        entity = AcRunTimeEntity(
            coordinator,
            "hub_ac0",
            {"identifiers": {("airtouch", "hub_ac0")}, "name": "AC"},
            0,
        )
        entity.hass = cast("Any", self.hass)
        clock = [datetime(2026, 10, 26, 23, 59, 59, 950000, tzinfo=_UTC)]
        expired = asyncio.Event()

        def state_changed() -> None:
            if not entity.available:
                expired.set()

        with (
            patch(
                "custom_components.airtouch.usage.local_now",
                side_effect=lambda _: clock[0],
            ),
            patch(
                "custom_components.airtouch.sensor.local_now",
                side_effect=lambda _: clock[0],
            ),
        ):
            remove_listener = coordinator.async_add_listener(state_changed)
            try:
                await coordinator.async_refresh()
                self.assertTrue(entity.available)
                clock[0] = datetime(2026, 10, 27, tzinfo=_UTC)
                await asyncio.wait_for(expired.wait(), 0.5)
            finally:
                remove_listener()
                await coordinator.async_shutdown()

    async def test_unload_stops_later_refreshes(self) -> None:
        self.responses = [b"", b""]
        coordinator = self._coordinator()
        await coordinator.async_refresh()
        self.assertEqual(self.connections, 2)
        for callback in self.entry.unloads:
            result = callback()
            if asyncio.iscoroutine(result):
                await result
        self.responses = [b"", b""]
        await coordinator.async_refresh()
        self.assertEqual(self.connections, 2)


if __name__ == "__main__":
    unittest.main()
