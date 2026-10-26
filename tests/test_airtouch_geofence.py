from __future__ import annotations

import asyncio
import contextlib
import json
import struct
import tempfile
import traceback
import unittest
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator

    from homeassistant.helpers.entity import Entity

import voluptuous as vol
from homeassistant import config_entries, loader
from homeassistant.auth import auth_manager_from_config
from homeassistant.auth.const import GROUP_ID_READ_ONLY, GROUP_ID_USER
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import (
    Context,
    CoreState,
    HassJob,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    State,
)
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotSupported,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import (
    area_registry,
    category_registry,
    device_registry,
    entity_registry,
    floor_registry,
    issue_registry,
    label_registry,
)
from homeassistant.helpers import entity_platform as entity_platform_helper
from homeassistant.helpers import (
    service as service_helper,
)
from homeassistant.helpers.json import json_bytes
from pyairtouch import AirTouchModel
from pyairtouch.at5.comms.hdr import At5Header, HeaderDecoder, HeaderEncoder
from pyairtouch.comms.crc16 import Crc16Modbus

from custom_components.airtouch import (
    async_setup_entry as async_setup_integration,
)
from custom_components.airtouch import (
    async_unload_entry as async_unload_integration,
)
from custom_components.airtouch import geofence as geofence_module
from custom_components.airtouch.console import DEFAULT_DEADLINE, ConsoleClient
from custom_components.airtouch.const import CONF_AIRTOUCH_MODEL, DOMAIN
from custom_components.airtouch.const import AirTouchModel as SelectedModel
from custom_components.airtouch.geofence import (
    EventAccepted,
    GeofenceAccount,
    GeofenceCoordinator,
    GeofenceEventError,
    GeofenceParseError,
    GeofenceSettings,
    GeofenceSnapshot,
    GeofenceUncertainError,
    Transition,
    async_install_geofence,
    async_release_geofence,
    encode_event,
    get_geofence,
    parse_accounts,
    parse_event_reply,
    parse_settings,
    safe_text,
    stored_email,
)
from custom_components.airtouch.sensor import (
    GeofenceEntity,
    GeofenceFeature,
    async_collect_geofence_entities,
)

_NORTH = "North"
_NORTH_EMAIL = "north@example.test"
_SOUTH = "South"
_SOUTH_EMAIL = "south@example.test"
_NO_SETTINGS = GeofenceSettings((), (), ())
_SETTINGS = bytes.fromhex("ff70")
_ACCOUNTS = bytes.fromhex("ff71")
_EVENT = bytes.fromhex("ff73")
_PROGRAMS = bytes.fromhex("ff41")
_NO_PROGRAMS = _PROGRAMS + bytes(8) + bytes.fromhex("0010ff1001ff04ff")
_NO_PROGRAM_BEAMS = bytes.fromhex("ff45") + bytes(8) + bytes.fromhex("0000")
_NATIVE_TEXT = "SECRET-RESULT Café 🙂"
_SERVICE = "post_geofence_presence"
_OUTER = b"\x55\x55\x55\xab"
_ENCODER = HeaderEncoder()
_DECODER = HeaderDecoder()
_CRC = Crc16Modbus()
_DEVICE_INFO: dict[str, object] = {
    "identifiers": {("airtouch", "hub")},
    "name": "Console",
}
_CLIENT_MODULES = (
    "ac_settings",
    "geofence",
    "io_outputs",
    "preferences",
    "programs",
    "smart_settings",
    "usage",
)


def _record(identity: bytes, email: bytes, inside: int) -> bytes:
    return (
        bytes([len(identity)])
        + identity
        + bytes([len(email)])
        + email
        + bytes([inside])
    )


def _wire(flag: int, identity: bytes, email: bytes) -> bytes:
    return (
        _EVENT
        + bytes([flag, 1, len(identity)])
        + identity
        + bytes([len(email)])
        + email
    )


_ENTER_NORTH = _wire(1, b"North", b"north@example.test")
_EXIT_NORTH = _wire(0, b"North", b"north@example.test")


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


class _Console:
    def __init__(self) -> None:
        self.on: tuple[int, ...] = (0,)
        self.off: tuple[int, ...] = (0,)
        self.away: tuple[int, ...] = (1,)
        self.records: list[list[Any]] = []
        self.settings_body: bytes | None = None
        self.accounts_body: bytes | None = None
        self.read_mode = "ok"
        self.event_mode = "ok"
        self.reply: bytes | None = None
        self.payloads: list[bytes] = []
        self.raw: list[bytes] = []
        self.connections = 0
        self.other = 0
        self.abandoned = 0
        self.started = asyncio.Event()
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

    def add(self, identity: bytes, email: bytes, *, inside: bool = False) -> None:
        self.records.append([identity, email, inside])

    def events(self) -> list[bytes]:
        return [payload for payload in self.payloads if payload.startswith(_EVENT)]

    async def until_abandoned(self, count: int) -> None:
        for _ in range(100):
            if self.abandoned >= count:
                return
            await asyncio.sleep(0.01)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        try:
            raw = await _read_raw(reader)
            header, payload = _oracle_decode(raw)
            prefix = payload[:2]
            if header.message_id == 0x1F and payload == _PROGRAMS:
                await self._write_empty_program_reports(writer, header.packet_id)
                await asyncio.wait_for(reader.read(1), 2)
                return
            if header.message_id != 0x1F or prefix not in (
                _SETTINGS,
                _ACCOUNTS,
                _EVENT,
            ):
                self.other += 1
                return
            self.payloads.append(payload)
            self.raw.append(raw)
            if prefix == _EVENT:
                body = await self._event(reader, payload)
            else:
                body = await self._report(reader, prefix)
            if body is None:
                return
            writer.write(_frame(header.packet_id, prefix + body))
            await writer.drain()
            await asyncio.wait_for(reader.read(1), 2)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, TimeoutError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _write_empty_program_reports(
        self, writer: asyncio.StreamWriter, packet_id: int
    ) -> None:
        writer.write(
            _frame(packet_id, _NO_PROGRAMS) + _frame(packet_id, _NO_PROGRAM_BEAMS)
        )
        await writer.drain()

    async def _report(
        self, reader: asyncio.StreamReader, prefix: bytes
    ) -> bytes | None:
        if self.read_mode == "close":
            return None
        if self.read_mode == "hold":
            self.started.set()
            await reader.read(1)
            self.abandoned += 1
            return None
        if prefix == _SETTINGS and self.settings_body is not None:
            return self.settings_body
        if prefix == _SETTINGS:
            return b"".join(
                bytes([len(items), *items]) for items in (self.on, self.off, self.away)
            )
        if self.accounts_body is not None:
            return self.accounts_body
        return b"".join(
            _record(identity, email, int(inside))
            for identity, email, inside in self.records
        )

    async def _event(
        self, reader: asyncio.StreamReader, payload: bytes
    ) -> bytes | None:
        queued = self._apply(payload)
        if self.event_mode == "hold":
            self.started.set()
            released = asyncio.ensure_future(self.release.wait())
            closed = asyncio.ensure_future(reader.read(1))
            done, pending = await asyncio.wait(
                {released, closed}, return_when=asyncio.FIRST_COMPLETED
            )
            for waiter in pending:
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter
            if closed in done:
                self.abandoned += 1
                return None
        elif self.event_mode == "reset":
            return None
        elif self.event_mode == "silent":
            await reader.read(1)
            return None
        if self.reply is not None:
            return self.reply
        units = len(_NATIVE_TEXT.encode("utf-16-le")) // 2
        return bytes([queued]) + units.to_bytes(2, "big") + _NATIVE_TEXT.encode()

    def _apply(self, payload: bytes) -> int:
        entering = payload[2] == 1
        size = payload[4]
        identity = payload[5 : 5 + size]
        email = payload[6 + size : 6 + size + payload[5 + size]]
        was_nobody_in = not any(record[2] for record in self.records)
        record = next((item for item in self.records if item[0] == identity), None)
        if record is None:
            record = [identity, email, False]
            self.records.append(record)
        record[1] = email
        record[2] = entering
        if entering:
            return int(was_nobody_in and bool(self.on))
        nobody_in = not any(item[2] for item in self.records)
        return int(nobody_in and bool(self.off or self.away))


class _Entry:
    def __init__(self, entry_id: str = "entry") -> None:
        self.entry_id = entry_id
        self.title = "geofence-test"
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
        self.tasks: list[str] = []

    def async_create_task(self, target: Coroutine[Any, Any, object]) -> None:
        self.tasks.append(target.__qualname__)
        target.close()

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
    def __init__(
        self,
        model: AirTouchModel = AirTouchModel.AIRTOUCH_5,
        host: str = "127.0.0.1",
    ) -> None:
        super().__init__(model)
        self.host = host
        self.airtouch_id = host
        self.name = host.replace("-", " ").title()
        self.air_conditioners: list[object] = []
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


def _entity(owner: GeofenceCoordinator) -> GeofenceEntity:
    return GeofenceEntity(owner, "hub", cast("Any", _DEVICE_INFO))


_RECONCILE = "DataUpdateCoordinator.async_refresh"
_ENTER_SOUTH = _wire(1, b"South", b"south@example.test")
_EXIT_SOUTH = _wire(0, b"South", b"south@example.test")
_CLOSED = "geofence owner is closed"
_UNCERTAIN = "the console may have received the event, which was not sent again"
_SECRETS = (_NORTH, _NORTH_EMAIL, _SOUTH, _SOUTH_EMAIL, "SECRET")


async def _post(
    owner: GeofenceCoordinator,
    transition: Transition,
    identity: str,
    email: str | None = None,
) -> EventAccepted:
    return await owner.async_post_presence(
        transition, safe_text(identity), None if email is None else safe_text(email)
    )


def _error_text(err: BaseException) -> str:
    return "".join(traceback.format_exception(err))


def _combined_unrecorded(entity: GeofenceEntity) -> frozenset[str]:
    # Home Assistant hands this set to the recorder in each state's info.
    return cast(
        "frozenset[str]", vars(type(entity))["_Entity__combined_unrecorded_attributes"]
    )


class GeofenceCodecTests(unittest.TestCase):
    def test_safe_text_keeps_exact_printable_ascii(self) -> None:
        for value in ("North", "north", "Ab Cd", "UUU", "a@b.test", "~!#", "x" * 255):
            with self.subTest(value=value[:8]):
                self.assertEqual(safe_text(value), value)

    def test_unsafe_text_is_refused_with_one_message_that_echoes_nothing(self) -> None:
        refused = (
            "",
            "x" * 256,
            "North,South",
            " North",
            "North ",
            "No\trth",
            "No\x00rth",
            "North\x7f",
            "Café",
            "\u00a0North",
        )
        messages: set[str] = set()
        for value in refused:
            with self.subTest(length=len(value)), self.assertRaises(ValueError) as err:
                safe_text(value)
            messages.add(str(err.exception))
        self.assertEqual(len(messages), 1)
        self.assertNotEqual(messages, {""})

    def test_enter_and_exit_follow_the_account_mode_layout(self) -> None:
        self.assertEqual(
            encode_event(Transition.ENTER, safe_text(_NORTH), safe_text(_NORTH_EMAIL)),
            bytes.fromhex("ff73 01 01 05")
            + b"North"
            + bytes([18])
            + b"north@example.test",
        )
        self.assertEqual(
            encode_event(Transition.EXIT, safe_text(_SOUTH), safe_text(_SOUTH_EMAIL)),
            bytes.fromhex("ff73 00 01 05")
            + b"South"
            + bytes([18])
            + b"south@example.test",
        )

    def test_255_byte_strings_use_the_full_count(self) -> None:
        payload = encode_event(
            Transition.ENTER, safe_text("N" * 255), safe_text("e" * 255)
        )
        self.assertEqual(
            payload, b"\xff\x73\x01\x01\xff" + b"N" * 255 + b"\xff" + b"e" * 255
        )

    def test_case_interior_spaces_and_uuu_are_sent_unchanged(self) -> None:
        self.assertEqual(
            encode_event(
                Transition.EXIT, safe_text("Ab Cd UUU"), safe_text("UUU@Example.Test")
            ),
            b"\xff\x73\x00\x01\x09Ab Cd UUU\x10UUU@Example.Test",
        )

    def test_settings_are_three_counted_lists_in_on_off_away_order(self) -> None:
        self.assertEqual(parse_settings(bytes.fromhex("000000")), _NO_SETTINGS)
        self.assertEqual(
            parse_settings(bytes.fromhex("02 0003 01 02 01 04")),
            GeofenceSettings((0, 3), (2,), (4,)),
        )

    def test_partial_or_trailing_settings_fail(self) -> None:
        for body in ("", "00", "0000", "000001", "010000", "02000000", "00000000"):
            with self.subTest(body=body), self.assertRaises(GeofenceParseError):
                parse_settings(bytes.fromhex(body))

    def test_empty_accounts_body_is_no_accounts(self) -> None:
        self.assertEqual(parse_accounts(b""), ())

    def test_account_records_repeat_without_a_count(self) -> None:
        body = _record(b"North", b"north@example.test", 1) + _record(b"South", b"", 0)
        self.assertEqual(
            parse_accounts(body),
            (
                GeofenceAccount(
                    "North", "north@example.test", inside=True, addressable=True
                ),
                GeofenceAccount("South", "", inside=False, addressable=True),
            ),
        )

    def test_partial_trailing_or_invalid_flag_rejects_the_whole_report(self) -> None:
        good = _record(b"North", b"n@x.test", 1)
        for body in (
            good[:-1],
            good[:3],
            b"\x00",
            b"\x00\x00",
            good + b"\x05No",
            good + _record(b"South", b"", 1)[:-1],
            _record(b"North", b"", 2),
            good + _record(b"South", b"", 0xFF),
        ):
            with self.subTest(size=len(body)), self.assertRaises(GeofenceParseError):
                parse_accounts(body)

    def test_legacy_text_is_displayed_json_safely_and_not_addressable(self) -> None:
        body = (
            _record(b"\xffNorth", b"n\xc3@x", 1)
            + _record(b"Caf\xc3\xa9", b"c@x.test", 0)
            + _record(b"", b"", 0)
            + _record(b"A,B", b"a@x.test", 0)
            + _record(b"South", b"s@x.test", 0)
        )
        accounts = parse_accounts(body)
        self.assertEqual(
            [account.identity for account in accounts],
            ["�North", "Café", "", "A,B", "South"],
        )
        self.assertEqual(
            [account.email for account in accounts],
            ["n�@x", "c@x.test", "", "a@x.test", "s@x.test"],
        )
        self.assertEqual(
            [account.addressable for account in accounts],
            [False, False, False, False, True],
        )
        attributes = GeofenceSnapshot(_NO_SETTINGS, accounts).attributes()
        decoded = json.loads(json_bytes(attributes))
        self.assertEqual(decoded["accounts"][0]["identity"], "�North")

    def test_duplicate_identities_stay_visible_but_unaddressable(self) -> None:
        accounts = parse_accounts(
            _record(b"North", b"a@x.test", 1)
            + _record(b"North", b"b@x.test", 0)
            + _record(b"South", b"s@x.test", 1)
        )
        self.assertEqual(
            [account.addressable for account in accounts], [False, False, True]
        )
        self.assertEqual(GeofenceSnapshot(_NO_SETTINGS, accounts).inside_count, 2)

    def test_snapshot_attributes_show_native_indexes_and_accounts(self) -> None:
        snapshot = GeofenceSnapshot(
            parse_settings(bytes.fromhex("02 0003 01 02 01 04")),
            parse_accounts(
                _record(b"North", b"north@example.test", 1)
                + _record(b"South", b"south@example.test", 0)
            ),
        )
        self.assertEqual(snapshot.inside_count, 1)
        self.assertEqual(
            snapshot.attributes(),
            {
                "on_ac_indexes": [0, 3],
                "off_ac_indexes": [2],
                "away_ac_indexes": [4],
                "accounts": [
                    {
                        "identity": "North",
                        "email": "north@example.test",
                        "inside": True,
                        "addressable": True,
                    },
                    {
                        "identity": "South",
                        "email": "south@example.test",
                        "inside": False,
                        "addressable": True,
                    },
                ],
            },
        )

    def test_reprs_hide_identities_and_emails(self) -> None:
        accounts = parse_accounts(_record(b"North", b"north@example.test", 1))
        for text in (repr(accounts[0]), repr(GeofenceSnapshot(_NO_SETTINGS, accounts))):
            self.assertNotIn("North", text)
            self.assertNotIn("north@example.test", text)

    def test_omitted_email_uses_one_exact_case_sensitive_identity(self) -> None:
        accounts = parse_accounts(
            _record(b"North", b"north@example.test", 0)
            + _record(b"north", b"lower@example.test", 1)
        )
        self.assertEqual(stored_email(accounts, safe_text("North")), _NORTH_EMAIL)
        self.assertEqual(
            stored_email(accounts, safe_text("north")), "lower@example.test"
        )

    def test_missing_ambiguous_empty_or_unsafe_metadata_refuses(self) -> None:
        cases = {
            "no accounts": b"",
            "other identity": _record(b"South", b"s@x.test", 0),
            "duplicate": _record(b"North", b"a@x.test", 0) * 2,
            "empty email": _record(b"North", b"", 0),
            "comma email": _record(b"North", b"a,b@x.test", 0),
            "legacy email": _record(b"North", b"\xff@x.test", 0),
        }
        for name, body in cases.items():
            with self.subTest(name), self.assertRaises(GeofenceEventError) as err:
                stored_email(parse_accounts(body), safe_text(_NORTH))
            self.assertNotIn(_NORTH, str(err.exception))

    def test_reply_flag_reports_whether_commands_were_queued(self) -> None:
        self.assertEqual(
            parse_event_reply(b"\x00\x00\x00"), EventAccepted(commands_queued=False)
        )
        self.assertEqual(
            parse_event_reply(b"\x01\x00\x02OK"), EventAccepted(commands_queued=True)
        )

    def test_reply_length_counts_java_utf16_units(self) -> None:
        accented = "é".encode()
        emoji = "🙂".encode()
        self.assertEqual(len(accented), 2)
        self.assertEqual(len(emoji), 4)
        self.assertEqual(
            parse_event_reply(b"\x00\x00\x01" + accented),
            EventAccepted(commands_queued=False),
        )
        self.assertEqual(
            parse_event_reply(b"\x01\x00\x02" + emoji),
            EventAccepted(commands_queued=True),
        )
        mixed = "AC Café 🙂".encode()
        self.assertEqual(len(mixed), 13)
        self.assertEqual(
            parse_event_reply(b"\x01\x00\x0a" + mixed),
            EventAccepted(commands_queued=True),
        )

    def test_malformed_replies_fail_without_their_text(self) -> None:
        emoji = "🙂".encode()
        for body in (
            b"",
            b"\x00",
            b"\x00\x00",
            b"\x02\x00\x00",
            b"\x01\x00\x01" + emoji,
            b"\x01\x00\x04" + emoji,
            b"\x01\x00\x02" + emoji[:3],
            b"\x00\x00\x01",
            b"\x00\x00\x00SECRET",
            b"\x00\x00\x07SECRET\xff",
        ):
            with (
                self.subTest(size=len(body)),
                self.assertRaises(GeofenceParseError) as err,
            ):
                parse_event_reply(body)
            text = "".join(traceback.format_exception(err.exception))
            self.assertNotIn("SECRET", text)


class _OwnerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.console = _Console()
        await self.console.start()
        self.hass = _Hass()
        self.entry = _Entry()

    async def asyncTearDown(self) -> None:
        self.console.release.set()
        await async_release_geofence(cast("Any", self.hass), self.entry.entry_id)
        await self.console.stop()

    def _owner(self, *, deadline: float = 1.0) -> GeofenceCoordinator:
        owner = async_install_geofence(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            port=self.console.port,
            deadline=deadline,
        )
        if owner is None:
            raise AssertionError("expected an AirTouch 5 geofence owner")
        return owner


class GeofenceOwnerTests(_OwnerCase):
    async def test_sensor_counts_inside_accounts_and_shows_both_reports(self) -> None:
        self.console.add(b"North", b"north@example.test", inside=True)
        self.console.add(b"South", b"south@example.test")
        owner = self._owner()
        entity = _entity(owner)
        self.assertEqual(entity.unique_id, "hub_geofence")
        self.assertEqual(entity.translation_key, "geofence")
        self.assertEqual(entity.supported_features, GeofenceFeature.POST_PRESENCE)
        await owner.async_refresh()
        self.assertTrue(entity.available)
        self.assertEqual(entity.native_value, 1)
        self.assertEqual(
            entity.extra_state_attributes,
            {
                "on_ac_indexes": [0],
                "off_ac_indexes": [0],
                "away_ac_indexes": [1],
                "accounts": [
                    {
                        "identity": "North",
                        "email": "north@example.test",
                        "inside": True,
                        "addressable": True,
                    },
                    {
                        "identity": "South",
                        "email": "south@example.test",
                        "inside": False,
                        "addressable": True,
                    },
                ],
            },
        )
        self.assertEqual(self.console.payloads, [_SETTINGS, _ACCOUNTS])
        self.assertIn("accounts", _combined_unrecorded(entity))
        self.assertNotIn("on_ac_indexes", _combined_unrecorded(entity))

    async def test_a_failed_or_partial_read_shows_unknown_and_stays_available(
        self,
    ) -> None:
        self.console.add(b"North", b"north@example.test", inside=True)
        owner = self._owner()
        entity = _entity(owner)
        await owner.async_refresh()
        good = owner.data
        failures: dict[str, tuple[str, bytes | None, bytes | None]] = {
            "closed": ("close", None, None),
            "partial accounts": ("ok", None, _record(b"North", b"n@x.test", 1)[:-1]),
            "trailing settings": ("ok", b"\x00\x00\x00\x00", None),
        }
        for name, (mode, settings, accounts) in failures.items():
            with self.subTest(name):
                self.console.read_mode = mode
                self.console.settings_body = settings
                self.console.accounts_body = accounts
                await owner.async_refresh()
                self.assertFalse(owner.last_update_success)
                self.assertTrue(entity.available)
                self.assertIsNone(entity.native_value)
                self.assertIsNone(entity.extra_state_attributes)
                self.assertIs(owner.data, good)
        self.console.read_mode = "ok"
        self.console.settings_body = self.console.accounts_body = None
        await owner.async_refresh()
        self.assertEqual(entity.native_value, 1)
        self.assertIsNot(owner.data, good)
        self.assertEqual(owner.data, good)

    async def test_a_first_failed_read_still_creates_an_available_sensor(self) -> None:
        owner = self._owner()
        self.console.read_mode = "close"
        entities = await async_collect_geofence_entities(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", _AirTouch()),
            cast("Any", _Device()),
        )
        self.assertEqual(len(entities), 1)
        entity = cast("GeofenceEntity", entities[0])
        self.assertIs(entity.coordinator, owner)
        self.assertTrue(entity.available)
        self.assertIsNone(entity.native_value)
        self.assertIsNone(entity.extra_state_attributes)
        self.assertEqual(self.console.payloads, [_SETTINGS])

    async def test_omitted_email_is_read_fresh_right_before_the_event(self) -> None:
        self.console.add(b"North", b"old@example.test")
        owner = self._owner()
        await owner.async_refresh()
        polled = owner.data
        self.console.records[0][1] = b"north@example.test"
        self.assertEqual(
            await _post(owner, Transition.ENTER, _NORTH),
            EventAccepted(commands_queued=True),
        )
        self.assertEqual(
            self.console.payloads, [_SETTINGS, _ACCOUNTS, _ACCOUNTS, _ENTER_NORTH]
        )
        self.assertIs(owner.data, polled)
        self.assertEqual(self.hass.tasks, [_RECONCILE])

    async def test_missing_ambiguous_empty_or_unreadable_metadata_sends_nothing(
        self,
    ) -> None:
        owner = self._owner()
        cases = {
            "no accounts": ("ok", b""),
            "other identity": ("ok", _record(b"South", b"s@x.test", 0)),
            "other case": ("ok", _record(b"north", b"n@x.test", 0)),
            "duplicate": ("ok", _record(b"North", b"a@x.test", 0) * 2),
            "empty email": ("ok", _record(b"North", b"", 0)),
            "legacy email": ("ok", _record(b"North", b"\xff@x.test", 0)),
            "partial report": ("ok", _record(b"North", b"a@x.test", 0)[:-1]),
            "closed": ("close", b""),
        }
        for name, (mode, body) in cases.items():
            with self.subTest(name):
                self.console.read_mode, self.console.accounts_body = mode, body
                with self.assertRaises(GeofenceEventError) as err:
                    await _post(owner, Transition.ENTER, _NORTH)
                for secret in _SECRETS:
                    self.assertNotIn(secret, _error_text(err.exception))
        self.assertEqual(self.console.payloads, [_ACCOUNTS] * len(cases))
        self.assertEqual(self.hass.tasks, [])

    async def test_first_exit_registers_the_supplied_email(self) -> None:
        owner = self._owner()
        with self.assertRaises(GeofenceEventError):
            await _post(owner, Transition.EXIT, _NORTH)
        self.assertEqual(self.console.events(), [])
        self.assertEqual(
            await _post(owner, Transition.EXIT, _NORTH, _NORTH_EMAIL),
            EventAccepted(commands_queued=True),
        )
        self.assertEqual(
            self.console.records, [[b"North", b"north@example.test", False]]
        )
        self.assertEqual(
            await _post(owner, Transition.ENTER, _NORTH),
            EventAccepted(commands_queued=True),
        )
        self.assertEqual(
            self.console.payloads, [_ACCOUNTS, _EXIT_NORTH, _ACCOUNTS, _ENTER_NORTH]
        )

    async def test_explicit_email_posts_while_reads_fail(self) -> None:
        owner = self._owner()
        entity = _entity(owner)
        self.console.read_mode = "close"
        await owner.async_refresh()
        self.assertTrue(entity.available)
        self.assertIsNone(entity.native_value)
        self.assertEqual(
            await _post(owner, Transition.ENTER, _NORTH, _NORTH_EMAIL),
            EventAccepted(commands_queued=True),
        )
        self.assertEqual(self.console.payloads, [_SETTINGS, _ENTER_NORTH])
        self.assertIsNone(entity.native_value)

    async def test_six_literal_events_follow_native_first_and_last_rules(self) -> None:
        owner = self._owner()
        steps: tuple[tuple[Transition, str, str | None], ...] = (
            (Transition.ENTER, _NORTH, _NORTH_EMAIL),
            (Transition.ENTER, _NORTH, None),
            (Transition.ENTER, _SOUTH, _SOUTH_EMAIL),
            (Transition.EXIT, _NORTH, None),
            (Transition.EXIT, _SOUTH, None),
            (Transition.EXIT, _SOUTH, None),
        )
        queued = [(await _post(owner, *step)).commands_queued for step in steps]
        self.assertEqual(queued, [True, False, False, False, True, True])
        self.assertEqual(
            self.console.events(),
            [
                _ENTER_NORTH,
                _ENTER_NORTH,
                _ENTER_SOUTH,
                _EXIT_NORTH,
                _EXIT_SOUTH,
                _EXIT_SOUTH,
            ],
        )
        self.assertEqual(self.hass.tasks, [_RECONCILE] * 6)

    async def test_flag_zero_is_acceptance_and_broken_replies_are_uncertain(
        self,
    ) -> None:
        owner = self._owner()
        self.console.reply = b"\x00\x00\x00"
        self.assertEqual(
            await _post(owner, Transition.ENTER, _NORTH, _NORTH_EMAIL),
            EventAccepted(commands_queued=False),
        )
        broken = (
            b"",
            b"\x01",
            b"\x02\x00\x00",
            b"\x01\x00\x05SECRET",
            b"\x01\x00\x07SECRET\xff",
        )
        for reply in broken:
            with self.subTest(size=len(reply)):
                self.console.reply = reply
                with self.assertRaises(GeofenceUncertainError) as err:
                    await _post(owner, Transition.ENTER, _NORTH, _NORTH_EMAIL)
                self.assertEqual(str(err.exception), _UNCERTAIN)
                for secret in _SECRETS:
                    self.assertNotIn(secret, _error_text(err.exception))
        self.assertEqual(self.console.events(), [_ENTER_NORTH] * (1 + len(broken)))
        self.assertEqual(self.hass.tasks, [_RECONCILE] * (1 + len(broken)))

    async def test_reset_and_timeout_after_sending_are_uncertain_and_not_retried(
        self,
    ) -> None:
        owner = self._owner(deadline=0.3)
        for mode in ("reset", "silent"):
            with self.subTest(mode):
                self.console.event_mode = mode
                with self.assertRaises(GeofenceUncertainError) as err:
                    await _post(owner, Transition.EXIT, _NORTH, _NORTH_EMAIL)
                self.assertEqual(str(err.exception), _UNCERTAIN)
        self.assertEqual(self.console.events(), [_EXIT_NORTH, _EXIT_NORTH])
        self.assertEqual(self.hass.tasks, [_RECONCILE] * 2)

    async def test_a_caller_cancelled_twice_waits_for_its_event_then_cancels(
        self,
    ) -> None:
        owner = self._owner(deadline=5)
        self.console.event_mode = "hold"
        caller = asyncio.create_task(
            _post(owner, Transition.ENTER, _NORTH, _NORTH_EMAIL)
        )
        await asyncio.wait_for(self.console.started.wait(), 1)
        queued = asyncio.create_task(owner.async_refresh())
        for _ in range(2):
            caller.cancel()
            await asyncio.sleep(0.05)
            self.assertFalse(caller.done())
        self.assertEqual(self.console.payloads, [_ENTER_NORTH])
        self.console.release.set()
        await asyncio.wait({caller}, timeout=2)
        self.assertTrue(caller.cancelled())
        await asyncio.wait_for(queued, 2)
        self.assertTrue(owner.last_update_success)
        self.assertEqual(self.console.payloads, [_ENTER_NORTH, _SETTINGS, _ACCOUNTS])
        self.assertEqual(self.console.abandoned, 0)
        self.assertEqual(
            self.console.records, [[b"North", b"north@example.test", True]]
        )
        self.assertEqual(self.hass.tasks, [_RECONCILE])

    async def test_caller_cancellation_before_the_event_sends_nothing(self) -> None:
        self.console.add(b"North", b"north@example.test")
        owner = self._owner(deadline=5)
        self.console.read_mode = "hold"
        caller = asyncio.create_task(_post(owner, Transition.ENTER, _NORTH))
        await asyncio.wait_for(self.console.started.wait(), 1)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        await self.console.until_abandoned(1)
        self.assertEqual(self.console.abandoned, 1)
        self.assertEqual(self.hass.tasks, [])
        self.console.read_mode = "ok"
        self.assertEqual(
            await _post(owner, Transition.ENTER, _NORTH),
            EventAccepted(commands_queued=True),
        )
        self.assertEqual(self.console.payloads, [_ACCOUNTS, _ACCOUNTS, _ENTER_NORTH])

    async def test_release_drains_an_admitted_event_and_refuses_queued_and_late_work(
        self,
    ) -> None:
        self.console.add(b"North", b"north@example.test")
        owner = self._owner(deadline=5)
        entity = _entity(owner)
        await owner.async_refresh()
        polled = owner.data
        self.console.event_mode = "hold"
        admitted = asyncio.create_task(_post(owner, Transition.ENTER, _NORTH))
        await asyncio.wait_for(self.console.started.wait(), 1)
        queued = asyncio.create_task(
            _post(owner, Transition.EXIT, _NORTH, _NORTH_EMAIL)
        )
        queued_read = asyncio.create_task(owner.async_refresh())
        await asyncio.sleep(0.05)
        release = asyncio.create_task(
            async_release_geofence(cast("Any", self.hass), self.entry.entry_id)
        )
        await asyncio.sleep(0.05)
        self.assertFalse(release.done())
        self.assertFalse(admitted.done())
        self.assertFalse(entity.available)
        self.console.release.set()
        await asyncio.wait_for(release, 2)
        self.assertEqual(await admitted, EventAccepted(commands_queued=True))
        with self.assertRaises(GeofenceEventError) as err:
            await queued
        self.assertEqual(str(err.exception), _CLOSED)
        await queued_read
        with self.assertRaises(GeofenceEventError):
            await _post(owner, Transition.EXIT, _NORTH, _NORTH_EMAIL)
        self.assertEqual(
            self.console.payloads, [_SETTINGS, _ACCOUNTS, _ACCOUNTS, _ENTER_NORTH]
        )
        self.assertIs(owner.data, polled)
        self.assertFalse(owner.last_update_success)
        self.assertEqual(self.hass.tasks, [])
        self.assertEqual(self.console.abandoned, 0)

    async def test_release_cancels_a_blocked_poll_without_cancelling_its_caller(
        self,
    ) -> None:
        owner = self._owner(deadline=5)
        self.console.read_mode = "hold"
        caller = asyncio.create_task(owner.async_refresh())
        await asyncio.wait_for(self.console.started.wait(), 1)
        await asyncio.wait_for(
            async_release_geofence(cast("Any", self.hass), self.entry.entry_id), 1
        )
        await asyncio.wait_for(caller, 1)
        self.assertFalse(caller.cancelled())
        await self.console.until_abandoned(1)
        self.assertEqual(self.console.abandoned, 1)
        self.assertIsNone(owner.data)
        self.assertFalse(_entity(owner).available)
        self.assertEqual(self.console.payloads, [_SETTINGS])

    async def test_release_cancels_an_email_read_and_sends_no_event(self) -> None:
        self.console.add(b"North", b"north@example.test")
        owner = self._owner(deadline=5)
        self.console.read_mode = "hold"
        caller = asyncio.create_task(_post(owner, Transition.ENTER, _NORTH))
        await asyncio.wait_for(self.console.started.wait(), 1)
        await asyncio.wait_for(
            async_release_geofence(cast("Any", self.hass), self.entry.entry_id), 1
        )
        with self.assertRaises(GeofenceEventError) as err:
            await asyncio.wait_for(caller, 1)
        self.assertEqual(str(err.exception), _CLOSED)
        await self.console.until_abandoned(1)
        self.assertEqual(self.console.abandoned, 1)
        self.assertEqual(self.console.payloads, [_ACCOUNTS])
        self.assertEqual(self.hass.tasks, [])

    async def test_shutdown_leaves_no_scheduled_refresh_with_listeners_attached(
        self,
    ) -> None:
        with patch.object(geofence_module, "_UPDATE_INTERVAL", timedelta(seconds=0.01)):
            owner = self._owner(deadline=5)
        owner.async_add_listener(lambda: None)
        for _ in range(100):
            if self.entry.scheduled:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(
            self.entry.scheduled, ["AirTouch geofence - geofence-test - refresh"]
        )
        self.console.event_mode = "hold"
        caller = asyncio.create_task(
            _post(owner, Transition.ENTER, _NORTH, _NORTH_EMAIL)
        )
        await asyncio.wait_for(self.console.started.wait(), 1)
        shutdown = asyncio.create_task(owner.async_shutdown())
        await asyncio.sleep(0.05)
        self.assertFalse(shutdown.done())
        self.console.release.set()
        await asyncio.wait_for(shutdown, 1)
        self.assertEqual(await caller, EventAccepted(commands_queued=True))
        self.entry.scheduled.clear()
        await asyncio.sleep(0.7)
        self.assertEqual(self.entry.scheduled, [])
        self.assertEqual(self.hass.tasks, [])
        self.assertEqual(self.console.payloads, [_ENTER_NORTH])

    async def test_airtouch_4_has_no_owner_entity_or_connection(self) -> None:
        fourth = _AirTouch(AirTouchModel.AIRTOUCH_4)
        self.assertIsNone(
            async_install_geofence(
                cast("Any", self.hass), cast("Any", self.entry), cast("Any", fourth)
            )
        )
        entities = await async_collect_geofence_entities(
            cast("Any", self.hass),
            cast("Any", self.entry),
            cast("Any", fourth),
            cast("Any", _Device()),
        )
        self.assertEqual(entities, [])
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_geofence(cast("Any", self.hass), self.entry.entry_id)
        self.assertEqual(self.console.connections, 0)

    async def test_install_identity_repeated_release_and_reinstall(self) -> None:
        owner = self._owner()
        self.assertIs(get_geofence(cast("Any", self.hass), self.entry.entry_id), owner)
        with self.assertRaisesRegex(RuntimeError, "already installed"):
            self._owner()
        await async_release_geofence(cast("Any", self.hass), self.entry.entry_id)
        await async_release_geofence(cast("Any", self.hass), self.entry.entry_id)
        await owner.async_shutdown()
        self.assertNotIn("airtouch_geofence", self.hass.data)
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_geofence(cast("Any", self.hass), self.entry.entry_id)
        await owner.async_refresh()
        self.assertEqual(self.console.payloads, [])
        fresh = self._owner()
        self.assertIsNot(fresh, owner)
        await fresh.async_refresh()
        self.assertTrue(fresh.last_update_success)
        self.assertEqual(self.console.payloads, [_SETTINGS, _ACCOUNTS])

    async def test_logs_errors_and_reprs_carry_no_identity_email_or_reply_text(
        self,
    ) -> None:
        self.console.add(b"North", b"north@example.test", inside=True)
        owner = self._owner(deadline=0.3)
        texts: list[str] = []
        with self.assertLogs("custom_components.airtouch", level="DEBUG") as logs:
            await owner.async_refresh()
            await _post(owner, Transition.ENTER, _NORTH)
            for reply, mode in ((b"\x01\x00\x05SECRET", "ok"), (None, "reset")):
                self.console.reply, self.console.event_mode = reply, mode
                with self.assertRaises(GeofenceUncertainError) as uncertain:
                    await _post(owner, Transition.EXIT, _SOUTH, _SOUTH_EMAIL)
                texts.append(_error_text(uncertain.exception))
            self.console.accounts_body = _record(b"North", b"a@x.test", 0) * 2
            with self.assertRaises(GeofenceEventError) as refused:
                await _post(owner, Transition.EXIT, _NORTH)
            texts.append(_error_text(refused.exception))
            self.console.read_mode = "close"
            await owner.async_refresh()
        texts.extend([*logs.output, repr(owner.data), repr(owner)])
        for text in texts:
            for secret in _SECRETS:
                self.assertNotIn(secret, text)


class GeofenceRootTests(_OwnerCase):
    @contextlib.contextmanager
    def _root(self, api: _Api) -> Iterator[None]:
        def connect(*, model: object, host: str) -> _Api:
            del model, host
            return api

        def client(
            host: str, port: int, *, deadline: float = DEFAULT_DEADLINE
        ) -> ConsoleClient:
            del port
            return ConsoleClient(host, self.console.port, deadline=deadline)

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch("custom_components.airtouch.pyairtouch.connect", connect)
            )
            for module in _CLIENT_MODULES:
                stack.enter_context(
                    patch(f"custom_components.airtouch.{module}.ConsoleClient", client)
                )
            yield

    async def _setup(self, entry: _RootEntry, api: _Api) -> bool:
        with self._root(api):
            return await async_setup_integration(
                cast("Any", self.hass), cast("Any", entry)
            )

    async def test_root_installs_without_reading_and_airtouch_4_gets_no_owner(
        self,
    ) -> None:
        api = _Api()
        entry = _RootEntry("root")
        self.hass.config_entries = _ConfigEntries(None)
        self.assertTrue(await self._setup(entry, api))
        self.addAsyncCleanup(
            async_release_geofence, cast("Any", self.hass), entry.entry_id
        )
        get_geofence(cast("Any", self.hass), entry.entry_id)
        self.assertEqual(self.console.payloads, [])

        fourth = _Api(AirTouchModel.AIRTOUCH_4)
        at4 = _RootEntry("at4", SelectedModel.AIRTOUCH_4.value)
        self.assertTrue(await self._setup(at4, fourth))
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_geofence(cast("Any", self.hass), at4.entry_id)
        self.assertEqual(self.console.payloads, [])

    async def test_root_failure_and_cancellation_release_the_owner(self) -> None:
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
                    get_geofence(
                        cast("Any", self.hass), cast("Any", config_entry).entry_id
                    )
                    raise failure

                self.hass.config_entries = _ConfigEntries(forward)
                with self.assertRaises(type(error)):
                    await self._setup(entry, api)
                with self.assertRaisesRegex(RuntimeError, "not installed"):
                    get_geofence(cast("Any", self.hass), entry.entry_id)
        self.assertEqual(self.console.payloads, [])
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
            patch("custom_components.airtouch.async_release_geofence", bad_release),
            self.assertLogs("custom_components.airtouch", level="ERROR") as logged,
            self.assertRaisesRegex(RuntimeError, "forward failed"),
        ):
            await self._setup(entry, api)
        self.assertIn("Error releasing AirTouch geofence", "\n".join(logged.output))
        await async_release_geofence(cast("Any", self.hass), entry.entry_id)
        self.assertEqual(api.shutdown_calls, 0)

    async def test_unload_false_keeps_and_unload_drains_an_event_before_api_shutdown(
        self,
    ) -> None:
        api = _Api()
        entry = _RootEntry("root")
        self.addAsyncCleanup(
            async_release_geofence, cast("Any", self.hass), entry.entry_id
        )
        self.hass.config_entries = _ConfigEntries(None, unload_ok=False)
        self.assertTrue(await self._setup(entry, api))
        owner = get_geofence(cast("Any", self.hass), entry.entry_id)
        self.assertFalse(
            await async_unload_integration(cast("Any", self.hass), cast("Any", entry))
        )
        self.assertIs(get_geofence(cast("Any", self.hass), entry.entry_id), owner)
        self.assertEqual(
            await _post(owner, Transition.ENTER, _NORTH, _NORTH_EMAIL),
            EventAccepted(commands_queued=True),
        )

        self.console.event_mode = "hold"
        admitted = asyncio.create_task(
            _post(owner, Transition.EXIT, _NORTH, _NORTH_EMAIL)
        )
        await asyncio.wait_for(self.console.started.wait(), 1)
        self.hass.config_entries.unload_ok = True
        unload = asyncio.create_task(
            async_unload_integration(cast("Any", self.hass), cast("Any", entry))
        )
        await asyncio.sleep(0.05)
        self.assertFalse(unload.done())
        self.assertEqual(api.events, [])
        self.console.release.set()
        self.assertTrue(await asyncio.wait_for(unload, 2))
        self.assertEqual(await admitted, EventAccepted(commands_queued=True))
        self.assertEqual(api.events, ["api-shutdown"])
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            get_geofence(cast("Any", self.hass), entry.entry_id)
        with self.assertRaises(GeofenceEventError):
            await _post(owner, Transition.ENTER, _NORTH, _NORTH_EMAIL)
        self.assertEqual(self.console.events(), [_ENTER_NORTH, _EXIT_NORTH])

        self.console.event_mode = "ok"
        self.assertTrue(await self._setup(entry, api))
        reloaded = get_geofence(cast("Any", self.hass), entry.entry_id)
        self.assertIsNot(reloaded, owner)
        self.assertEqual(
            await _post(reloaded, Transition.ENTER, _NORTH),
            EventAccepted(commands_queued=True),
        )
        self.assertEqual(
            self.console.events(), [_ENTER_NORTH, _EXIT_NORTH, _ENTER_NORTH]
        )


class GeofenceActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.consoles = {
            host: _Console() for host in ("console-a", "console-b", "console-4")
        }
        for console in self.consoles.values():
            await console.start()
        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        hass = HomeAssistant(config_dir.name)
        hass.config.skip_pip = True
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        loader.async_setup(hass)
        await asyncio.gather(
            area_registry.async_load(hass),
            category_registry.async_load(hass),
            device_registry.async_load(hass),
            entity_registry.async_load(hass),
            floor_registry.async_load(hass),
            issue_registry.async_load(hass),
            label_registry.async_load(hass),
        )
        hass.auth = await auth_manager_from_config(hass, [], [])
        hass.set_state(CoreState.running)
        self.hass = hass
        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(
            patch("custom_components.airtouch.PLATFORMS", [Platform.SENSOR])
        )
        patches.enter_context(
            patch("custom_components.airtouch.pyairtouch.connect", self._connect)
        )
        for module in _CLIENT_MODULES:
            patches.enter_context(
                patch(
                    f"custom_components.airtouch.{module}.ConsoleClient", self._client
                )
            )
        self.entries: dict[str, config_entries.ConfigEntry] = {}
        for host, model in (
            ("console-a", SelectedModel.AIRTOUCH_5),
            ("console-b", SelectedModel.AIRTOUCH_5),
            ("console-4", SelectedModel.AIRTOUCH_4),
        ):
            entry = config_entries.ConfigEntry(
                data={CONF_HOST: host, CONF_AIRTOUCH_MODEL: model.value},
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
            await hass.config_entries.async_add(entry)
            self.entries[host] = entry
        await hass.async_block_till_done(wait_background_tasks=True)
        for console in self.consoles.values():
            console.payloads.clear()
            console.connections = 0

    async def asyncTearDown(self) -> None:
        for console in self.consoles.values():
            console.release.set()
        for entry in self.entries.values():
            if entry.state is config_entries.ConfigEntryState.LOADED:
                await self.hass.config_entries.async_unload(entry.entry_id)
        await self.hass.async_stop(force=True)
        for console in self.consoles.values():
            await console.stop()

    def _connect(self, *, model: AirTouchModel, host: str) -> _Api:
        return _Api(model, host)

    def _client(
        self, host: str, port: int, *, deadline: float = DEFAULT_DEADLINE
    ) -> ConsoleClient:
        del port
        return ConsoleClient("127.0.0.1", self.consoles[host].port, deadline=deadline)

    def _entity_id(self, host: str, kind: str = "geofence") -> str:
        registry = entity_registry.async_get(self.hass)
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{host}_{kind}")
        if entity_id is None:
            raise AssertionError(f"{host} has no {kind} sensor")
        return entity_id

    def _device_id(self, host: str) -> str:
        registry = device_registry.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, host)})
        if device is None:
            raise AssertionError(f"{host} has no device")
        return device.id

    def _state(self, entity_id: str) -> State:
        state: State | None = self.hass.states.get(entity_id)
        if state is None:
            raise AssertionError(f"{entity_id} has no state")
        return state

    def _events(self) -> dict[str, list[bytes]]:
        return {host: console.events() for host, console in self.consoles.items()}

    async def _call(
        self, target: dict[str, object], *, user_id: str | None = None, **data: object
    ) -> ServiceResponse:
        response: ServiceResponse = await self.hass.services.async_call(
            DOMAIN,
            _SERVICE,
            data,
            blocking=True,
            target=target,
            context=Context(user_id=user_id),
            return_response=True,
        )
        return response

    async def test_the_action_requires_a_response_and_returns_commands_queued(
        self,
    ) -> None:
        sensor = self._entity_id("console-a")
        data = {"transition": "enter", "identity": "Ab Cd UUU", "email": "UUU@x.test"}
        with self.assertRaises(ServiceValidationError):
            await self.hass.services.async_call(
                DOMAIN, _SERVICE, data, blocking=True, target={"entity_id": sensor}
            )
        self.assertEqual(self.consoles["console-a"].connections, 0)
        self.assertEqual(
            await self._call({"entity_id": sensor}, **data),
            {sensor: {"commands_queued": True}},
        )
        console = self.consoles["console-a"]
        self.assertEqual(console.events(), [_wire(1, b"Ab Cd UUU", b"UUU@x.test")])
        self.assertIn(b"Ab Cd UUU\x00\x0aUUU\x00@x.test", console.raw[-1])
        self.assertEqual(self.consoles["console-b"].connections, 0)

    async def test_the_schema_rejects_unsafe_or_missing_fields_without_connecting(
        self,
    ) -> None:
        sensor = self._entity_id("console-a")
        cases: tuple[dict[str, str], ...] = (
            {"transition": "arrive", "identity": _NORTH},
            {"transition": "enter"},
            {"transition": "enter", "identity": "North,South"},
            {"transition": "enter", "identity": " North"},
            {"transition": "enter", "identity": "Café"},
            {"transition": "enter", "identity": "N" * 256},
            {"transition": "exit", "identity": _NORTH, "email": ""},
            {"transition": "exit", "identity": _NORTH, "email": "a,b@x.test"},
        )
        for index, data in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(vol.Invalid) as err:
                await self._call({"entity_id": sensor}, **data)
            for value in data.values():
                if value not in ("", "enter", "exit"):
                    self.assertNotIn(value, str(err.exception))
        self.assertEqual(self.consoles["console-a"].connections, 0)

    async def test_identity_and_email_require_strings_before_connecting(self) -> None:
        sensor = self._entity_id("console-a")
        invalid: tuple[object, ...] = (123, 1.5, True, None, [], {})
        for field in ("identity", "email"):
            for value in invalid:
                data: dict[str, object] = {
                    "transition": "enter",
                    "identity": _NORTH,
                    "email": _NORTH_EMAIL,
                }
                data[field] = value
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(vol.Invalid),
                ):
                    await self.hass.services.async_call(
                        DOMAIN,
                        _SERVICE,
                        data,
                        target={"entity_id": sensor},
                        blocking=True,
                        return_response=True,
                    )
        self.assertEqual(self.consoles["console-a"].connections, 0)

    async def test_wrong_missing_and_airtouch_4_targets_send_nothing(self) -> None:
        data = {"transition": "enter", "identity": _NORTH, "email": _NORTH_EMAIL}
        with self.assertRaises(ServiceNotSupported):
            await self._call(
                {"entity_id": self._entity_id("console-a", "programs")}, **data
            )
        with self.assertRaises(vol.Invalid):
            await self.hass.services.async_call(
                DOMAIN, _SERVICE, data, blocking=True, return_response=True
            )
        for target in (
            {"device_id": self._device_id("console-4")},
            {"entity_id": "sensor.missing_geofence"},
        ):
            with (
                self.subTest(target=target),
                self.assertRaises(HomeAssistantError) as err,
            ):
                await self._call(cast("dict[str, object]", target), **data)
            self.assertIn("did not match any entities", str(err.exception))
        self.assertEqual(
            self._events(), {"console-a": [], "console-b": [], "console-4": []}
        )
        self.assertEqual(self.consoles["console-4"].connections, 0)

    async def test_control_permission_comes_from_home_assistant_users(self) -> None:
        sensor = self._entity_id("console-a")
        owner = await self.hass.auth.async_create_user("Owner")
        self.assertTrue(owner.is_owner)
        reader = await self.hass.auth.async_create_user(
            "Reader", group_ids=[GROUP_ID_READ_ONLY]
        )
        self.assertFalse(reader.is_admin)
        member = await self.hass.auth.async_create_user(
            "Member", group_ids=[GROUP_ID_USER]
        )
        self.assertFalse(member.is_admin)
        data = {"transition": "enter", "identity": _NORTH, "email": _NORTH_EMAIL}
        for target in (
            {"entity_id": sensor},
            {"device_id": self._device_id("console-a")},
        ):
            with self.subTest(target=sorted(target)), self.assertRaises(Unauthorized):
                await self._call(
                    cast("dict[str, object]", target), user_id=reader.id, **data
                )
        self.assertEqual(self.consoles["console-a"].events(), [])
        self.assertEqual(
            await self._call({"entity_id": sensor}, user_id=member.id, **data),
            {sensor: {"commands_queued": True}},
        )
        self.assertEqual(self.consoles["console-a"].events(), [_ENTER_NORTH])

    async def test_device_and_entity_targets_post_one_event(self) -> None:
        sensor = self._entity_id("console-a")
        self.assertEqual(
            await self._call(
                {"device_id": self._device_id("console-a"), "entity_id": sensor},
                transition="enter",
                identity=_NORTH,
                email=_NORTH_EMAIL,
            ),
            {sensor: {"commands_queued": True}},
        )
        self.assertEqual(
            self._events(),
            {"console-a": [_ENTER_NORTH], "console-b": [], "console-4": []},
        )

    async def test_two_consoles_share_one_action_through_reload_and_unload(
        self,
    ) -> None:
        first, second = self._entity_id("console-a"), self._entity_id("console-b")
        south = {"identity": _SOUTH, "email": _SOUTH_EMAIL}
        self.assertEqual(
            await self._call({"entity_id": second}, transition="enter", **south),
            {second: {"commands_queued": True}},
        )
        self.assertEqual(
            await self._call(
                {"entity_id": [first, second]}, transition="exit", **south
            ),
            {first: {"commands_queued": True}, second: {"commands_queued": True}},
        )
        entry = self.entries["console-a"]
        self.assertTrue(await self.hass.config_entries.async_reload(entry.entry_id))
        await self.hass.async_block_till_done(wait_background_tasks=True)
        self.assertEqual(
            await self._call({"entity_id": first}, transition="enter", **south),
            {first: {"commands_queued": True}},
        )
        self.assertTrue(await self.hass.config_entries.async_unload(entry.entry_id))
        self.assertTrue(self.hass.services.has_service(DOMAIN, _SERVICE))
        with self.assertRaises(HomeAssistantError):
            await self._call({"entity_id": first}, transition="exit", **south)
        self.assertEqual(
            await self._call({"entity_id": second}, transition="enter", **south),
            {second: {"commands_queued": True}},
        )
        self.assertEqual(
            self._events(),
            {
                "console-a": [_EXIT_SOUTH, _ENTER_SOUTH],
                "console-b": [_ENTER_SOUTH, _EXIT_SOUTH, _ENTER_SOUTH],
                "console-4": [],
            },
        )

    async def test_events_refresh_the_sensor_and_read_failures_keep_it_usable(
        self,
    ) -> None:
        sensor = self._entity_id("console-a")
        console = self.consoles["console-a"]
        owner = get_geofence(self.hass, self.entries["console-a"].entry_id)
        self.assertEqual(self._state(sensor).state, "0")
        with self.assertLogs("custom_components.airtouch", level="DEBUG") as logs:
            console.read_mode = "close"
            await owner.async_refresh()
            self.assertEqual(self._state(sensor).state, "unknown")
            self.assertNotIn("accounts", self._state(sensor).attributes)
            self.assertEqual(
                await self._call(
                    {"entity_id": sensor},
                    transition="exit",
                    identity=_NORTH,
                    email=_NORTH_EMAIL,
                ),
                {sensor: {"commands_queued": True}},
            )
            await self.hass.async_block_till_done(wait_background_tasks=True)
            self.assertEqual(self._state(sensor).state, "unknown")
            console.read_mode = "ok"
            self.assertEqual(
                await self._call(
                    {"entity_id": sensor}, transition="enter", identity=_NORTH
                ),
                {sensor: {"commands_queued": True}},
            )
            await self.hass.async_block_till_done(wait_background_tasks=True)
        state = self._state(sensor)
        self.assertEqual(state.state, "1")
        self.assertEqual(
            state.attributes["accounts"],
            [
                {
                    "identity": "North",
                    "email": "north@example.test",
                    "inside": True,
                    "addressable": True,
                }
            ],
        )
        self.assertEqual(
            console.payloads,
            [
                _SETTINGS,
                _EXIT_NORTH,
                _SETTINGS,
                _ACCOUNTS,
                _ENTER_NORTH,
                _SETTINGS,
                _ACCOUNTS,
            ],
        )
        for secret in (_NORTH, _NORTH_EMAIL, "SECRET"):
            self.assertNotIn(secret, "\n".join(logs.output))

    async def test_unload_drains_an_admitted_event_and_later_calls_send_nothing(
        self,
    ) -> None:
        sensor = self._entity_id("console-a")
        console = self.consoles["console-a"]
        console.event_mode = "hold"
        call = asyncio.create_task(
            self._call(
                {"entity_id": sensor},
                transition="enter",
                identity=_NORTH,
                email=_NORTH_EMAIL,
            )
        )
        await asyncio.wait_for(console.started.wait(), 2)
        unload = asyncio.create_task(
            self.hass.config_entries.async_unload(self.entries["console-a"].entry_id)
        )
        await asyncio.sleep(0.1)
        self.assertFalse(unload.done())
        console.release.set()
        self.assertTrue(await asyncio.wait_for(unload, 3))
        self.assertEqual(await call, {sensor: {"commands_queued": True}})
        after_unload = self.hass.states.get(sensor)
        with self.assertRaises(HomeAssistantError):
            await self._call(
                {"entity_id": sensor},
                transition="exit",
                identity=_NORTH,
                email=_NORTH_EMAIL,
            )
        await self.hass.async_block_till_done(wait_background_tasks=True)
        await asyncio.sleep(0.1)
        self.assertEqual(console.payloads, [_ENTER_NORTH])
        self.assertEqual(self.hass.states.get(sensor), after_unload)


class GeofencePlatformListActionTests(GeofenceActionTests):
    async def asyncSetUp(self) -> None:
        native_dispatch = service_helper.entity_service_call

        def register_without_response_parameter(
            platform: entity_platform_helper.EntityPlatform,
            name: str,
            schema: object,
            func: object,
            required_features: Iterable[int] | None = None,
        ) -> None:
            del platform, name, schema, func, required_features
            raise AssertionError("the response-dropping registration was called")

        async def dispatch_with_platforms(
            hass: HomeAssistant,
            platforms: Iterable[entity_platform_helper.EntityPlatform],
            func: Callable[[Entity, ServiceCall], Awaitable[ServiceResponse]],
            call: ServiceCall,
            required_features: Iterable[int],
        ) -> ServiceResponse:
            registered = {
                entity_id: entity
                for platform in platforms
                for entity_id, entity in platform.entities.items()
            }
            selected = await service_helper.async_extract_entities(
                hass, registered.values(), call
            )
            eligible = [
                entity
                for entity in selected
                if entity.available
                and entity.supported_features
                and any(
                    entity.supported_features & feature == feature
                    for feature in required_features
                )
            ]
            if len(eligible) > 1:
                raise HomeAssistantError(
                    "Service call requested response data but matched more than "
                    "one entity"
                )
            response = await native_dispatch(
                hass, registered, HassJob(func), call, required_features
            )
            if response is None:
                return None
            self.assertEqual(len(response), 1)
            return next(iter(response.values()))

        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(
            patch.object(
                entity_platform_helper.EntityPlatform,
                "async_register_entity_service",
                register_without_response_parameter,
            )
        )
        patches.enter_context(
            patch.object(service_helper, "entity_service_call", dispatch_with_platforms)
        )
        await super().asyncSetUp()

    async def test_two_consoles_share_one_action_through_reload_and_unload(
        self,
    ) -> None:
        first, second = self._entity_id("console-a"), self._entity_id("console-b")
        data = {"transition": "exit", "identity": _SOUTH, "email": _SOUTH_EMAIL}
        with self.assertRaisesRegex(HomeAssistantError, "more than one entity"):
            await self._call({"entity_id": [first, second]}, **data)
        self.assertEqual(self.consoles["console-a"].events(), [])
        self.assertEqual(self.consoles["console-b"].events(), [])
        for sensor in (first, second):
            self.assertEqual(
                await self._call({"entity_id": sensor}, **data),
                {sensor: {"commands_queued": True}},
            )
        entry = self.entries["console-a"]
        self.assertTrue(await self.hass.config_entries.async_reload(entry.entry_id))
        await self.hass.async_block_till_done(wait_background_tasks=True)
        self.assertEqual(
            await self._call({"entity_id": first}, **data),
            {first: {"commands_queued": True}},
        )
        self.assertTrue(await self.hass.config_entries.async_unload(entry.entry_id))
        self.assertTrue(self.hass.services.has_service(DOMAIN, _SERVICE))
        with self.assertRaises(HomeAssistantError):
            await self._call({"entity_id": first}, **data)
        self.assertEqual(
            await self._call({"entity_id": second}, **data),
            {second: {"commands_queued": True}},
        )
        self.assertEqual(self.consoles["console-a"].events(), [_EXIT_SOUTH] * 2)
        self.assertEqual(self.consoles["console-b"].events(), [_EXIT_SOUTH] * 2)


if __name__ == "__main__":
    unittest.main()
