"""The console keeps smart settings in one information block.

A write sends that block back with the requested bytes changed, an action
count of 0, and a stride of 0. It does not send the state records from the
last read. Those records can change an air-conditioner or zone setpoint.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import TYPE_CHECKING, Final, assert_never, cast

from homeassistant.config_entries import current_entry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pyairtouch import AirTouchModel
from pyairtouch.at5.api import DEFAULT_PORT_NUMBER

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from pyairtouch import AirTouch

from .console import DEFAULT_DEADLINE, ConsoleClient, ConsoleProtocolError
from .preferences import whole_degrees

_LOGGER = logging.getLogger(__name__)

_OWNERS_KEY: Final = "airtouch_smart_settings"
_SUBCOMMAND: Final = 0xFF
_SMART_SETTING_ID: Final = 0x18
_QUERY: Final = bytes((_SUBCOMMAND, _SMART_SETTING_ID))
_RESPONSE_PREFIX: Final = _QUERY
_INFO_LENGTH_OFFSET: Final = 0
_COUNT_OFFSET: Final = 1
_STRIDE_OFFSET: Final = 2
_INFO_OFFSET: Final = 3
# The console reads the flag at this index only when the information length
# is greater than 5.
_ENERGY_INDEX: Final = 5
_MINIMUM_INFO_LENGTH: Final = _ENERGY_INDEX + 1
_DIFFERENTIAL_INDICES: Final = (1, 3)
_DISABLED_DIFFERENTIAL: Final = 0xFF
_DISABLED_BYTE: Final = 0
_ENABLED_BYTE: Final = 1
_WRITE_COUNT: Final = 0
_WRITE_STRIDE: Final = 0
_UPDATE_INTERVAL: Final = 60


class SmartSettingsParseError(Exception):
    """The smart-settings body was truncated or internally inconsistent."""


class SmartSettingsRejectedError(Exception):
    """The requested smart-settings value was not accepted."""


class SmartSettingsExchangeError(Exception):
    """The console did not complete a read or a confirmed write."""


class SmartFlag(Enum):
    """One byte that the console reports as off at zero and on otherwise."""

    ADVANCED = "weather_adaptation_advanced"
    ENERGY_WASTAGE = "energy_wastage_detection"
    BASIC = "weather_adaptation_basic"
    AUTO_OFF = "weather_adaptation_auto_off"


class SmartNumber(Enum):
    """One console byte stored as a whole number of degrees Celsius."""

    COOL_DIFFERENTIAL = "weather_adaptation_cool_differential"
    COOL_TARGET = "weather_adaptation_cool_target"
    HEAT_DIFFERENTIAL = "weather_adaptation_heat_differential"
    HEAT_TARGET = "weather_adaptation_heat_target"


@dataclass(frozen=True, slots=True)
class SmartNumberRange:
    """Inclusive Celsius bounds and whether Home Assistant converts them."""

    minimum: int
    maximum: int
    absolute: bool


SMART_NUMBER_RANGES: Final[dict[SmartNumber, SmartNumberRange]] = {
    SmartNumber.COOL_DIFFERENTIAL: SmartNumberRange(0, 15, absolute=False),
    SmartNumber.HEAT_DIFFERENTIAL: SmartNumberRange(0, 15, absolute=False),
    SmartNumber.COOL_TARGET: SmartNumberRange(16, 32, absolute=True),
    SmartNumber.HEAT_TARGET: SmartNumberRange(16, 32, absolute=True),
}

_FLAG_INDEX: Final[dict[SmartFlag, int]] = {
    SmartFlag.ADVANCED: 0,
    SmartFlag.ENERGY_WASTAGE: _ENERGY_INDEX,
    SmartFlag.BASIC: 6,
    SmartFlag.AUTO_OFF: 7,
}
_NUMBER_INDEX: Final[dict[SmartNumber, int]] = {
    SmartNumber.COOL_DIFFERENTIAL: 1,
    SmartNumber.COOL_TARGET: 2,
    SmartNumber.HEAT_DIFFERENTIAL: 3,
    SmartNumber.HEAT_TARGET: 4,
}
# The console applies basic and auto-off only when the information length is
# greater than 7. A shorter report is left as received.
_FLAG_PAIR_LENGTH: Final = 8


def _flag_present(info: bytes, field: SmartFlag) -> bool:
    match field:
        case SmartFlag.ADVANCED | SmartFlag.ENERGY_WASTAGE:
            return _FLAG_INDEX[field] < len(info)
        case SmartFlag.BASIC | SmartFlag.AUTO_OFF:
            return len(info) >= _FLAG_PAIR_LENGTH
        case unreachable:
            assert_never(unreachable)


def _flag_changes(field: SmartFlag, *, enabled: bool) -> tuple[tuple[int, int], ...]:
    value = _ENABLED_BYTE if enabled else _DISABLED_BYTE
    match field:
        case SmartFlag.ADVANCED | SmartFlag.ENERGY_WASTAGE:
            return ((_FLAG_INDEX[field], value),)
        case SmartFlag.BASIC:
            if enabled:
                return ((_FLAG_INDEX[SmartFlag.BASIC], value),)
            return (
                (_FLAG_INDEX[SmartFlag.BASIC], _DISABLED_BYTE),
                (_FLAG_INDEX[SmartFlag.AUTO_OFF], _DISABLED_BYTE),
            )
        case SmartFlag.AUTO_OFF:
            if enabled:
                return (
                    (_FLAG_INDEX[SmartFlag.BASIC], _ENABLED_BYTE),
                    (_FLAG_INDEX[SmartFlag.AUTO_OFF], _ENABLED_BYTE),
                )
            return ((_FLAG_INDEX[SmartFlag.AUTO_OFF], value),)
        case unreachable:
            assert_never(unreachable)


def _info_with_flags(info: bytes, field: SmartFlag, *, enabled: bool) -> bytes:
    if not _flag_present(info, field):
        raise SmartSettingsRejectedError("smart settings omit the requested flag")
    updated = bytearray(info)
    for index, value in _flag_changes(field, enabled=enabled):
        updated[index] = value
    return bytes(updated)


def _info_with_number(info: bytes, field: SmartNumber, value: int) -> bytes:
    index = _NUMBER_INDEX[field]
    if index >= len(info):
        raise SmartSettingsRejectedError("smart settings omit the requested number")
    updated = bytearray(info)
    updated[index] = value
    return bytes(updated)


@dataclass(frozen=True, slots=True)
class SmartSettingsSnapshot:
    """Information bytes from one reply."""

    info: bytes

    def __post_init__(self) -> None:
        if len(self.info) < _MINIMUM_INFO_LENGTH:
            raise SmartSettingsParseError("smart settings omit energy wastage")

    def flag(self, field: SmartFlag) -> bool | None:
        """Return the reported flag, or None when this report omits it."""
        if not _flag_present(self.info, field):
            return None
        return self.info[_FLAG_INDEX[field]] != _DISABLED_BYTE

    def number(self, field: SmartNumber) -> int | None:
        """Return one Celsius byte, or None when it is absent or unsupported."""
        index = _NUMBER_INDEX[field]
        if index >= len(self.info):
            return None
        value = self.info[index]
        limits = SMART_NUMBER_RANGES[field]
        if value < limits.minimum or value > limits.maximum:
            return None
        return value


def parse_smart_settings(body: bytes) -> SmartSettingsSnapshot:
    """Parse the bytes that follow FF 18 in a smart-settings reply."""
    if len(body) < _INFO_OFFSET:
        raise SmartSettingsParseError("truncated smart settings header")
    info_length = body[_INFO_LENGTH_OFFSET]
    count = body[_COUNT_OFFSET]
    stride = body[_STRIDE_OFFSET]
    if info_length < _MINIMUM_INFO_LENGTH:
        raise SmartSettingsParseError("smart settings omit energy wastage")
    info_end = _INFO_OFFSET + info_length
    if info_end > len(body):
        raise SmartSettingsParseError("truncated smart settings")
    if _record_length(count, stride) != len(body) - info_end:
        raise SmartSettingsParseError("smart settings record length mismatch")
    return SmartSettingsSnapshot(body[_INFO_OFFSET:info_end])


class SmartSettingsCoordinator(DataUpdateCoordinator[SmartSettingsSnapshot]):
    """Poll one entry's smart settings and publish a write only after readback.

    A failed poll leaves the previous snapshot stored and marks the update
    failed, so the switch becomes unavailable. A failed write leaves that
    snapshot unchanged and raises.
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
        self._transaction_lock = asyncio.Lock()
        self._closed = False
        token = current_entry.set(entry)
        try:
            super().__init__(
                hass,
                _LOGGER,
                name="AirTouch smart settings",
                update_interval=timedelta(seconds=_UPDATE_INTERVAL),
            )
        finally:
            current_entry.reset(token)

    async def async_set_flag(self, field: SmartFlag, *, enabled: bool) -> None:
        """Change one flag, including the console's basic and auto-off pairing."""
        if not isinstance(enabled, bool):
            raise SmartSettingsRejectedError("smart settings flag must be on or off")
        await self._async_patch(
            lambda snapshot: _info_with_flags(snapshot.info, field, enabled=enabled)
        )

    async def async_set_number(self, field: SmartNumber, value: float) -> None:
        """Store one number. Advanced stays as the console last reported it.

        A stored differential or comfort target is used when advanced mode is on.
        """
        try:
            native = whole_degrees(value)
        except (TypeError, ValueError) as err:
            raise SmartSettingsRejectedError(str(err)) from err
        limits = SMART_NUMBER_RANGES[field]
        if native < limits.minimum or native > limits.maximum:
            raise SmartSettingsRejectedError("temperature is outside the console range")
        await self._async_patch(
            lambda snapshot: _info_with_number(snapshot.info, field, native)
        )

    async def async_shutdown(self) -> None:
        """Refuse new work, stop scheduling, and wait for an admitted transaction."""
        self._closed = True
        await super().async_shutdown()
        async with self._transaction_lock:
            pass

    async def _async_patch(
        self, patch: Callable[[SmartSettingsSnapshot], bytes]
    ) -> None:
        async with self._transaction_lock:
            if self._closed:
                raise SmartSettingsExchangeError("smart settings owner is closed")
            fresh = await self._read_snapshot()
            info = patch(fresh)
            await self._write_info(info)
            confirmed = await self._read_snapshot()
            if confirmed.info != info:
                raise SmartSettingsExchangeError("smart settings readback differed")
            self.async_set_updated_data(confirmed)

    async def _async_update_data(self) -> SmartSettingsSnapshot:
        async with self._transaction_lock:
            if self._closed:
                raise UpdateFailed("smart settings owner is closed")
            try:
                return await self._read_snapshot()
            except SmartSettingsExchangeError as err:
                raise UpdateFailed(str(err)) from err

    async def _read_snapshot(self) -> SmartSettingsSnapshot:
        try:
            body = await self._client.request(_QUERY, response_prefix=_RESPONSE_PREFIX)
            return parse_smart_settings(body)
        except (
            ConsoleProtocolError,
            SmartSettingsParseError,
            OSError,
            TimeoutError,
        ) as err:
            raise SmartSettingsExchangeError("smart settings read failed") from err

    async def _write_info(self, info: bytes) -> None:
        # The console's write handler turns a disabled -1 differential into 255.
        if any(
            info[index] == _DISABLED_DIFFERENTIAL for index in _DIFFERENTIAL_INDICES
        ):
            raise SmartSettingsRejectedError(
                "Cannot update smart settings with a disabled weather differential"
            )
        try:
            body = await self._client.request(
                _write_payload(info),
                response_prefix=_RESPONSE_PREFIX,
            )
            confirmed = parse_smart_settings(body)
        except (
            ConsoleProtocolError,
            SmartSettingsParseError,
            OSError,
            TimeoutError,
        ) as err:
            raise SmartSettingsExchangeError("smart settings write failed") from err
        if confirmed.info != info:
            raise SmartSettingsExchangeError("smart settings write was not stored")


def async_install(
    hass: HomeAssistant,
    entry: ConfigEntry,
    airtouch: AirTouch,
    *,
    port: int = DEFAULT_PORT_NUMBER,
    deadline: float = DEFAULT_DEADLINE,
) -> SmartSettingsCoordinator | None:
    """Install one AirTouch 5 owner for this entry without network I/O.

    A second install is rejected. Reload installs a new owner after release.
    """
    if airtouch.model != AirTouchModel.AIRTOUCH_5:
        return None
    owners = _owners(hass)
    if entry.entry_id in owners:
        raise RuntimeError("smart settings owner is already installed")
    coordinator = SmartSettingsCoordinator(
        hass, entry, airtouch, port=port, deadline=deadline
    )
    owners[entry.entry_id] = coordinator
    return coordinator


def get_smart_settings(hass: HomeAssistant, entry_id: str) -> SmartSettingsCoordinator:
    """Return the owner installed for this config entry."""
    stored = hass.data.get(_OWNERS_KEY)
    if isinstance(stored, dict):
        coordinator = stored.get(entry_id)
        if isinstance(coordinator, SmartSettingsCoordinator):
            return coordinator
    raise RuntimeError("smart settings owner is not installed")


async def async_release(hass: HomeAssistant, entry_id: str) -> None:
    """Stop and forget this entry's owner. A missing owner is left unchanged."""
    stored = hass.data.get(_OWNERS_KEY)
    if not isinstance(stored, dict):
        return
    owners = cast("dict[str, SmartSettingsCoordinator]", stored)
    coordinator = owners.pop(entry_id, None)
    if not owners:
        hass.data.pop(_OWNERS_KEY, None)
    if isinstance(coordinator, SmartSettingsCoordinator):
        await coordinator.async_shutdown()


def _owners(hass: HomeAssistant) -> dict[str, SmartSettingsCoordinator]:
    stored = hass.data.get(_OWNERS_KEY)
    if stored is None:
        created: dict[str, SmartSettingsCoordinator] = {}
        hass.data[_OWNERS_KEY] = created
        return created
    if not isinstance(stored, dict):
        raise TypeError("smart settings owner map is invalid")
    return cast("dict[str, SmartSettingsCoordinator]", stored)


def _record_length(count: int, stride: int) -> int:
    if count > 0 and stride == 0:
        raise SmartSettingsParseError("invalid smart settings record size")
    return count * stride


def _write_payload(info: bytes) -> bytes:
    return (
        bytes((_SUBCOMMAND, _SMART_SETTING_ID, len(info), _WRITE_COUNT, _WRITE_STRIDE))
        + info
    )
