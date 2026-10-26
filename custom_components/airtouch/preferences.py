"""AirTouch 5 sleep and away temperature limits.

The four targets belong to the console, not to an individual air conditioner.
A write sends the first 32 preference bytes back with one temperature changed.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import TYPE_CHECKING, Final, assert_never, cast

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

# The console keeps five bits. A zero sleep byte means 32 °C cool or 16 °C heat.
_SLEEP_MASK: Final = 31
_SLEEP_COOL_WHEN_ZERO: Final = 32
_SLEEP_HEAT_WHEN_ZERO: Final = 16
_SLEEP_LOW: Final = 16
_SLEEP_HIGH: Final = 32
_SLEEP_HEAT_HIGH: Final = 31
_AWAY_LOW: Final = 0
_AWAY_HIGH: Final = 50
_AWAY_MAXIMUM_OFFSET: Final = 20
_AWAY_MINIMUM_OFFSET: Final = 21
_SLEEP_COOL_OFFSET: Final = 22
_SLEEP_HEAT_OFFSET: Final = 23
_PREFIX_LENGTH: Final = 32
_REPORT_LENGTH: Final = 52
_REPORT_ID: Final = 0x45
_SET_ID: Final = 0x44
_NO_REPEAT: Final = 0
_UPDATE_INTERVAL: Final = 60
_WHOLE_DEGREE_TOLERANCE: Final = 1e-6
_OWNERS_KEY: Final = "airtouch_preferences"
_SUBHEADER: Final = struct.Struct("!BxHHH")


class PreferenceField(Enum):
    """One console temperature stored in the preference prefix."""

    SLEEP_COOL = "sleep_cool"
    SLEEP_HEAT = "sleep_heat"
    AWAY_MINIMUM = "away_minimum"
    AWAY_MAXIMUM = "away_maximum"


class PreferenceRejectedError(Exception):
    """The requested temperature is outside the limits the console accepts."""


class PreferenceParseError(Exception):
    """The frame was not a 52-byte preference report."""


class PreferenceExchangeError(Exception):
    """The console did not complete a preference read or confirmed write."""


@dataclass(frozen=True, slots=True)
class PreferenceSnapshot:
    """Decoded targets plus the exact 32-byte prefix they came from."""

    prefix: bytes
    away_minimum_c: int
    away_maximum_c: int
    sleep_cool_c: int
    sleep_heat_c: int

    def value(self, field: PreferenceField) -> int:
        """Return one decoded temperature."""
        match field:
            case PreferenceField.SLEEP_COOL:
                return self.sleep_cool_c
            case PreferenceField.SLEEP_HEAT:
                return self.sleep_heat_c
            case PreferenceField.AWAY_MINIMUM:
                return self.away_minimum_c
            case PreferenceField.AWAY_MAXIMUM:
                return self.away_maximum_c
            case _ as unreachable:
                assert_never(unreachable)


def parse_preference_report(payload: bytes) -> PreferenceSnapshot:
    """Parse a 0x45 preference report, including its retained prefix."""
    if len(payload) < _SUBHEADER.size:
        raise PreferenceParseError("truncated preference header")
    sub_id, normal_length, repeat_length, repeat_count = _SUBHEADER.unpack_from(payload)
    if (
        sub_id != _REPORT_ID
        or repeat_length != _NO_REPEAT
        or repeat_count != _NO_REPEAT
        or normal_length < _REPORT_LENGTH
        or len(payload) < _SUBHEADER.size + normal_length
    ):
        raise PreferenceParseError("truncated preference report")
    prefix = payload[_SUBHEADER.size : _SUBHEADER.size + _PREFIX_LENGTH]
    return PreferenceSnapshot(
        prefix=prefix,
        away_maximum_c=prefix[_AWAY_MAXIMUM_OFFSET],
        away_minimum_c=prefix[_AWAY_MINIMUM_OFFSET],
        sleep_cool_c=_sleep_temperature(
            prefix[_SLEEP_COOL_OFFSET], zero_means=_SLEEP_COOL_WHEN_ZERO
        ),
        sleep_heat_c=_sleep_temperature(
            prefix[_SLEEP_HEAT_OFFSET], zero_means=_SLEEP_HEAT_WHEN_ZERO
        ),
    )


class PreferenceCoordinator(DataUpdateCoordinator[PreferenceSnapshot]):
    """Poll console preferences and publish a write only after readback."""

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
        self._preference_transaction_lock = asyncio.Lock()
        self._closed = False
        token = current_entry.set(entry)
        try:
            super().__init__(
                hass,
                _LOGGER,
                name="AirTouch preferences",
                update_interval=timedelta(seconds=_UPDATE_INTERVAL),
            )
        finally:
            current_entry.reset(token)

    async def async_set(self, field: PreferenceField, value: float) -> None:
        """Change one temperature after a fresh read and a matching readback."""
        try:
            whole = whole_degrees(value)
        except (TypeError, ValueError) as err:
            raise PreferenceRejectedError(str(err)) from err
        _require_range(field, whole)
        async with self._preference_transaction_lock:
            if self._closed:
                raise PreferenceExchangeError("preference owner is closed")
            fresh = await self._read_snapshot()
            prefix = _patched_prefix(fresh, field, whole)
            await self._write_prefix(prefix)
            confirmed = await self._read_snapshot()
            if confirmed.prefix != prefix or confirmed.value(field) != whole:
                raise PreferenceExchangeError("preference readback differed")
            self.async_set_updated_data(confirmed)

    async def async_shutdown(self) -> None:
        """Refuse new work, stop scheduling, and wait for an admitted transaction."""
        self._closed = True
        await super().async_shutdown()
        async with self._preference_transaction_lock:
            pass

    async def _async_update_data(self) -> PreferenceSnapshot:
        async with self._preference_transaction_lock:
            if self._closed:
                raise UpdateFailed("preference owner is closed")
            try:
                return await self._read_snapshot()
            except PreferenceExchangeError as err:
                raise UpdateFailed(str(err)) from err

    async def _read_snapshot(self) -> PreferenceSnapshot:
        try:
            payload = await self._client.exchange_control(_query_payload())
            return parse_preference_report(payload)
        except (
            ConsoleProtocolError,
            PreferenceParseError,
            OSError,
            TimeoutError,
        ) as err:
            raise PreferenceExchangeError("preference read failed") from err

    async def _write_prefix(self, prefix: bytes) -> None:
        # A control write is answered by the status report. The console updates
        # its copy only from that 0x45 report, so anything else is a failed write.
        try:
            payload = await self._client.exchange_control(_write_payload(prefix))
            parse_preference_report(payload)
        except (
            ConsoleProtocolError,
            PreferenceParseError,
            OSError,
            TimeoutError,
        ) as err:
            raise PreferenceExchangeError("preference write failed") from err


def async_install_preferences(
    hass: HomeAssistant,
    entry: ConfigEntry,
    airtouch: AirTouch,
    *,
    port: int = DEFAULT_PORT_NUMBER,
    deadline: float = DEFAULT_DEADLINE,
) -> PreferenceCoordinator | None:
    """Install one AirTouch 5 preference owner without reading the console.

    A second install is rejected. Reload installs a new owner after release.
    """
    if airtouch.model != AirTouchModel.AIRTOUCH_5:
        return None
    owners = _owners(hass)
    if entry.entry_id in owners:
        raise RuntimeError("preference owner is already installed")
    coordinator = PreferenceCoordinator(
        hass, entry, airtouch, port=port, deadline=deadline
    )
    owners[entry.entry_id] = coordinator
    return coordinator


def get_preferences(hass: HomeAssistant, entry_id: str) -> PreferenceCoordinator:
    """Return the preference owner installed for this config entry."""
    stored = hass.data.get(_OWNERS_KEY)
    if isinstance(stored, dict):
        coordinator = stored.get(entry_id)
        if isinstance(coordinator, PreferenceCoordinator):
            return coordinator
    raise RuntimeError("preference owner is not installed")


async def async_release_preferences(hass: HomeAssistant, entry_id: str) -> None:
    """Stop and forget this entry's preference owner. Missing owners stay absent."""
    stored = hass.data.get(_OWNERS_KEY)
    if not isinstance(stored, dict):
        return
    owners = cast("dict[str, PreferenceCoordinator]", stored)
    coordinator = owners.pop(entry_id, None)
    if not owners:
        hass.data.pop(_OWNERS_KEY, None)
    if isinstance(coordinator, PreferenceCoordinator):
        await coordinator.async_shutdown()


def _owners(hass: HomeAssistant) -> dict[str, PreferenceCoordinator]:
    stored = hass.data.get(_OWNERS_KEY)
    if stored is None:
        created: dict[str, PreferenceCoordinator] = {}
        hass.data[_OWNERS_KEY] = created
        return created
    if not isinstance(stored, dict):
        raise TypeError("preference owner map is invalid")
    return cast("dict[str, PreferenceCoordinator]", stored)


def _query_payload() -> bytes:
    return _SUBHEADER.pack(_REPORT_ID, _NO_REPEAT, _NO_REPEAT, _NO_REPEAT)


def _write_payload(prefix: bytes) -> bytes:
    return _SUBHEADER.pack(_SET_ID, _PREFIX_LENGTH, _NO_REPEAT, _NO_REPEAT) + prefix


def _sleep_temperature(raw: int, *, zero_means: int) -> int:
    masked = raw & _SLEEP_MASK
    if masked == 0:
        return zero_means
    return masked


def whole_degrees(value: object) -> int:
    """Return a whole number of degrees.

    Floats within 1e-6 of an integer are accepted. Booleans and non-numeric
    types raise TypeError; non-finite values and other fractions raise ValueError.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("temperature must be a whole number of degrees")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("temperature must be finite")
        whole = round(value)
        if not math.isclose(value, whole, rel_tol=0.0, abs_tol=_WHOLE_DEGREE_TOLERANCE):
            raise ValueError("temperature must be a whole number of degrees")
        return whole
    return value


def _require_range(field: PreferenceField, value: int) -> None:
    low, high = PREFERENCE_LIMITS[field]
    if value < low or value > high:
        raise PreferenceRejectedError("temperature is outside the console range")


def _patched_prefix(
    snapshot: PreferenceSnapshot, field: PreferenceField, value: int
) -> bytes:
    _require_order(snapshot, field, value)
    prefix = bytearray(snapshot.prefix)
    prefix[_FIELD_OFFSET[field]] = _wire_byte(field, value)
    return bytes(prefix)


def _require_order(
    snapshot: PreferenceSnapshot, field: PreferenceField, value: int
) -> None:
    if field is PreferenceField.AWAY_MINIMUM and value > snapshot.away_maximum_c:
        raise PreferenceRejectedError("away minimum is above away maximum")
    if field is PreferenceField.AWAY_MAXIMUM and snapshot.away_minimum_c > value:
        raise PreferenceRejectedError("away minimum is above away maximum")


def _wire_byte(field: PreferenceField, value: int) -> int:
    if field in {PreferenceField.SLEEP_COOL, PreferenceField.SLEEP_HEAT}:
        return value & _SLEEP_MASK
    return value


PREFERENCE_LIMITS: Final = {
    PreferenceField.SLEEP_COOL: (_SLEEP_LOW, _SLEEP_HIGH),
    PreferenceField.SLEEP_HEAT: (_SLEEP_LOW, _SLEEP_HEAT_HIGH),
    PreferenceField.AWAY_MINIMUM: (_AWAY_LOW, _AWAY_HIGH),
    PreferenceField.AWAY_MAXIMUM: (_AWAY_LOW, _AWAY_HIGH),
}
_FIELD_OFFSET: Final = {
    PreferenceField.SLEEP_COOL: _SLEEP_COOL_OFFSET,
    PreferenceField.SLEEP_HEAT: _SLEEP_HEAT_OFFSET,
    PreferenceField.AWAY_MINIMUM: _AWAY_MINIMUM_OFFSET,
    PreferenceField.AWAY_MAXIMUM: _AWAY_MAXIMUM_OFFSET,
}
