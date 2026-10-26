"""Native AirTouch 5 geofence settings, accounts, and presence events.

FF 70 reports the AC indexes the console turns on at the first arrival and
turns off or sets to Away at the last departure. FF 71 reports each native
account's identity, email, and inside flag. One FF 73 event moves one account
in or out. The console then decides whether to queue AC commands, and it
creates or updates that account with the email sent in the event.

Each event is posted once, exactly as asked. A repeated exit can turn ACs off
again, so events are never skipped, merged, or retried.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from enum import IntEnum
from typing import TYPE_CHECKING, Final, NewType, cast

from homeassistant.config_entries import current_entry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pyairtouch import AirTouchModel
from pyairtouch.at5.api import DEFAULT_PORT_NUMBER

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from pyairtouch import AirTouch

from .console import DEFAULT_DEADLINE, ConsoleClient, ConsoleProtocolError

_LOGGER = logging.getLogger(__name__)

_SETTINGS_QUERY: Final = b"\xff\x70"
_ACCOUNTS_QUERY: Final = b"\xff\x71"
_EVENT_PREFIX: Final = b"\xff\x73"
_ACCOUNT_MODE: Final = 1
_MAX_TEXT: Final = 255
_REPLY_HEADER: Final = 3
_OWNERS_KEY: Final = "airtouch_geofence"
_UPDATE_INTERVAL: Final = timedelta(seconds=60)

_UNSAFE_TEXT: Final = (
    "must be 1 to 255 printable ASCII characters "
    "with no comma or leading or trailing space"
)
_CLOSED: Final = "geofence owner is closed"
_UNCERTAIN: Final = "the console may have received the event, which was not sent again"

SafeText = NewType("SafeText", str)


class GeofenceParseError(Exception):
    """A geofence report or event reply was truncated, trailing, or invalid."""


class GeofenceEventError(Exception):
    """The geofence event was refused before anything was sent."""


class GeofenceUncertainError(Exception):
    """An event exchange began without a valid reply, so delivery is uncertain."""


class _ReadFailedOrClosedError(Exception):
    pass


class Transition(IntEnum):
    """The account-mode event flag."""

    EXIT = 0
    ENTER = 1


@dataclass(frozen=True)
class GeofenceSettings:
    """Native AC indexes for the first arrival and the last departure."""

    on_ac_indexes: tuple[int, ...]
    off_ac_indexes: tuple[int, ...]
    away_ac_indexes: tuple[int, ...]


@dataclass(frozen=True)
class GeofenceAccount:
    """One native account record, decoded for display.

    addressable is true only when the identity is safe text that no other
    record repeats, so an event can name exactly this account.
    """

    identity: str = field(repr=False)
    email: str = field(repr=False)
    inside: bool
    addressable: bool


@dataclass(frozen=True)
class GeofenceSnapshot:
    """Settings and accounts from one complete pair of reports."""

    settings: GeofenceSettings
    accounts: tuple[GeofenceAccount, ...]

    @property
    def inside_count(self) -> int:
        """The number of account records the console reports inside."""
        return sum(account.inside for account in self.accounts)

    def attributes(self) -> dict[str, object]:
        return {
            "on_ac_indexes": list(self.settings.on_ac_indexes),
            "off_ac_indexes": list(self.settings.off_ac_indexes),
            "away_ac_indexes": list(self.settings.away_ac_indexes),
            "accounts": [
                {
                    "identity": account.identity,
                    "email": account.email,
                    "inside": account.inside,
                    "addressable": account.addressable,
                }
                for account in self.accounts
            ],
        }


@dataclass(frozen=True)
class EventAccepted:
    """The console accepted an event. Queued commands are not AC results."""

    commands_queued: bool


def safe_text(value: str) -> SafeText:
    """Return value unchanged if the console can store and send it back intact.

    The console saves accounts as comma-separated lines and trims each field,
    so commas, controls, and surrounding spaces would not survive.
    """
    if not _is_safe(value):
        raise ValueError(_UNSAFE_TEXT)
    return SafeText(value)


def parse_settings(body: bytes) -> GeofenceSettings:
    """Parse the body after the FF 70 prefix."""
    cursor = _Cursor(body)
    on, off, away = (tuple(cursor.take(cursor.byte())) for _ in range(3))
    cursor.finish()
    return GeofenceSettings(on, off, away)


def parse_accounts(body: bytes) -> tuple[GeofenceAccount, ...]:
    """Parse the body after the FF 71 prefix. An empty body has no accounts."""
    cursor = _Cursor(body)
    records: list[tuple[str, str, bool]] = []
    while not cursor.done():
        identity = _display(cursor.take(cursor.byte()))
        email = _display(cursor.take(cursor.byte()))
        inside = cursor.byte()
        if inside > 1:
            raise GeofenceParseError("account inside flag is invalid")
        records.append((identity, email, bool(inside)))
    identity_counts = Counter(identity for identity, _, _ in records)
    return tuple(
        GeofenceAccount(
            identity,
            email,
            inside=inside,
            addressable=_is_unique_safe_identity(identity, identity_counts),
        )
        for identity, email, inside in records
    )


def _is_unique_safe_identity(identity: str, counts: Counter[str]) -> bool:
    return _is_safe(identity) and counts[identity] == 1


def stored_email(accounts: tuple[GeofenceAccount, ...], identity: SafeText) -> SafeText:
    """Return the email the console holds for exactly this identity."""
    matches = [account for account in accounts if account.identity == identity]
    if not matches:
        raise GeofenceEventError(
            "no native account has that identity, so an email is required"
        )
    if len(matches) > 1:
        raise GeofenceEventError(
            "more than one native account has that identity, so it is ambiguous"
        )
    email = matches[0].email
    if not _is_safe(email):
        raise GeofenceEventError(
            "the native account has no usable email, so an email is required"
        )
    return SafeText(email)


def encode_event(transition: Transition, identity: SafeText, email: SafeText) -> bytes:
    """Encode one account-mode FF 73 event."""
    payload = bytearray(_EVENT_PREFIX)
    payload += bytes((transition, _ACCOUNT_MODE))
    for text in (identity, email):
        raw = text.encode("ascii")
        payload.append(len(raw))
        payload += raw
    return bytes(payload)


def parse_event_reply(body: bytes) -> EventAccepted:
    """Parse the body after the FF 73 reply prefix.

    The reply is a queued flag, the message's Java UTF-16 length, and the
    message as UTF-8. The message is checked and dropped.
    """
    if len(body) < _REPLY_HEADER:
        raise GeofenceParseError("event reply is truncated")
    queued = body[0]
    if queued > 1:
        raise GeofenceParseError("event reply flag is invalid")
    try:
        message = body[_REPLY_HEADER:].decode("utf-8")
    except UnicodeDecodeError:
        raise GeofenceParseError("event reply text is not UTF-8") from None
    units = int.from_bytes(body[1:_REPLY_HEADER], "big")
    if len(message.encode("utf-16-le")) // 2 != units:
        raise GeofenceParseError("event reply length does not match its text")
    return EventAccepted(commands_queued=bool(queued))


def _is_safe(value: str) -> bool:
    return (
        0 < len(value) <= _MAX_TEXT
        and value.isascii()
        and value.isprintable()
        and "," not in value
        and value == value.strip()
    )


def _display(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


class _Cursor:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def take(self, size: int) -> bytes:
        end = self._offset + size
        if end > len(self._data):
            raise GeofenceParseError("report is truncated")
        chunk = self._data[self._offset : end]
        self._offset = end
        return chunk

    def byte(self) -> int:
        return self.take(1)[0]

    def done(self) -> bool:
        return self._offset == len(self._data)

    def finish(self) -> None:
        if not self.done():
            raise GeofenceParseError("report has trailing bytes")


class GeofenceCoordinator(DataUpdateCoordinator[GeofenceSnapshot]):
    """Poll one console's geofence reports and post its presence events.

    One lock serializes polls, the account read before an event, and the
    event. Shutdown refuses new work, cancels only an owned read, and waits
    for an admitted event, which runs to its reply or deadline even when its
    caller is cancelled.
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
        self._lock = asyncio.Lock()
        self._read: asyncio.Task[bytes] | None = None
        self._closed = False
        token = current_entry.set(entry)
        try:
            super().__init__(
                hass,
                _LOGGER,
                name="AirTouch geofence",
                update_interval=_UPDATE_INTERVAL,
            )
        finally:
            current_entry.reset(token)

    @property
    def closed(self) -> bool:
        """Whether shutdown has begun."""
        return self._closed

    async def async_post_presence(
        self, transition: Transition, identity: SafeText, email: SafeText | None
    ) -> EventAccepted:
        """Post one enter or exit event and return the console's reply.

        Without an email, the account's stored email is read just before
        sending. GeofenceEventError means nothing was sent.
        GeofenceUncertainError means the event may have arrived. A refresh
        follows any event that was sent.
        """
        sent = False
        try:
            async with self._lock:
                if email is None:
                    email = await self._stored_email(identity)
                if self._closed:
                    raise GeofenceEventError(_CLOSED)
                sent = True
                return await _await_event_preserving_caller_cancellation(
                    asyncio.create_task(
                        self._send(encode_event(transition, identity, email))
                    )
                )
        finally:
            if sent and not self._closed:
                self.hass.async_create_task(self.async_refresh())

    async def async_shutdown(self) -> None:
        """Refuse new work, cancel an owned read, and drain an admitted event."""
        self._closed = True
        await super().async_shutdown()
        if self._read is not None:
            self._read.cancel()
        async with self._lock:
            pass
        # A drained refresh reschedules itself while listeners remain.
        await super().async_shutdown()

    async def _async_update_data(self) -> GeofenceSnapshot:
        async with self._lock:
            try:
                settings = parse_settings(await self._owned_read(_SETTINGS_QUERY))
                accounts = parse_accounts(await self._owned_read(_ACCOUNTS_QUERY))
            except (_ReadFailedOrClosedError, GeofenceParseError) as err:
                raise UpdateFailed(
                    _CLOSED if self._closed else "geofence read failed"
                ) from err
            if self._closed:
                raise UpdateFailed(_CLOSED)
            return GeofenceSnapshot(settings, accounts)

    async def _stored_email(self, identity: SafeText) -> SafeText:
        try:
            accounts = parse_accounts(await self._owned_read(_ACCOUNTS_QUERY))
        except (_ReadFailedOrClosedError, GeofenceParseError) as err:
            if self._closed:
                raise GeofenceEventError(_CLOSED) from err
            raise GeofenceEventError(
                "native accounts could not be read, so an email is required"
            ) from err
        return stored_email(accounts, identity)

    async def _owned_read(self, query: bytes) -> bytes:
        if self._closed:
            raise _ReadFailedOrClosedError(_CLOSED)
        read = asyncio.create_task(self._client.request(query))
        self._read = read
        try:
            return await read
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if not self._closed or (caller is not None and caller.cancelling()):
                raise
            raise _ReadFailedOrClosedError(_CLOSED) from None
        except (ConsoleProtocolError, OSError, TimeoutError) as err:
            raise _ReadFailedOrClosedError("geofence read failed") from err
        finally:
            self._read = None

    async def _send(self, payload: bytes) -> EventAccepted:
        try:
            body = await self._client.request(payload, response_prefix=_EVENT_PREFIX)
            return parse_event_reply(body)
        except (ConsoleProtocolError, GeofenceParseError, OSError, TimeoutError) as err:
            raise GeofenceUncertainError(_UNCERTAIN) from err


async def _await_event_preserving_caller_cancellation(
    event: asyncio.Task[EventAccepted],
) -> EventAccepted:
    cancelled = False
    while not event.done():
        try:
            await asyncio.wait({event})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        # Retrieve the outcome so an event error is not reported as unhandled.
        event.exception()
        raise asyncio.CancelledError
    return event.result()


def async_install_geofence(
    hass: HomeAssistant,
    entry: ConfigEntry,
    airtouch: AirTouch,
    *,
    port: int = DEFAULT_PORT_NUMBER,
    deadline: float = DEFAULT_DEADLINE,
) -> GeofenceCoordinator | None:
    """Install one AirTouch 5 geofence owner without reading the console.

    A second install is rejected. Reload installs a new owner after release.
    """
    if airtouch.model != AirTouchModel.AIRTOUCH_5:
        return None
    owners = _owners(hass)
    if entry.entry_id in owners:
        raise RuntimeError("geofence owner is already installed")
    coordinator = GeofenceCoordinator(
        hass, entry, airtouch, port=port, deadline=deadline
    )
    owners[entry.entry_id] = coordinator
    return coordinator


def get_geofence(hass: HomeAssistant, entry_id: str) -> GeofenceCoordinator:  # noqa: D103
    stored = hass.data.get(_OWNERS_KEY)
    if isinstance(stored, dict):
        coordinator = stored.get(entry_id)
        if isinstance(coordinator, GeofenceCoordinator):
            return coordinator
    raise RuntimeError("geofence owner is not installed")


async def async_release_geofence(hass: HomeAssistant, entry_id: str) -> None:
    """Stop and forget this entry's geofence owner. A missing owner is left absent."""
    stored = hass.data.get(_OWNERS_KEY)
    if not isinstance(stored, dict):
        return
    owners = cast("dict[str, GeofenceCoordinator]", stored)
    coordinator = owners.pop(entry_id, None)
    if not owners:
        hass.data.pop(_OWNERS_KEY, None)
    if isinstance(coordinator, GeofenceCoordinator):
        await coordinator.async_shutdown()


def _owners(hass: HomeAssistant) -> dict[str, GeofenceCoordinator]:
    stored = hass.data.get(_OWNERS_KEY)
    if stored is None:
        created: dict[str, GeofenceCoordinator] = {}
        hass.data[_OWNERS_KEY] = created
        return created
    if not isinstance(stored, dict):
        raise TypeError("geofence owner map is invalid")
    return cast("dict[str, GeofenceCoordinator]", stored)
