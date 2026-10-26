"""The console keeps the flag in the smart-settings information block.

A write sends that block back with the flag changed, an action count of 0, and a
stride of 0. It does not send the state records from the last read. Those
records can change an air-conditioner or zone setpoint.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from homeassistant.config_entries import current_entry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pyairtouch.at5.api import DEFAULT_PORT_NUMBER

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from pyairtouch import AirTouch

from .console import DEFAULT_DEADLINE, ConsoleClient, ConsoleProtocolError

_LOGGER = logging.getLogger(__name__)

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
_DISABLED_BYTE: Final = 0
_ENABLED_BYTE: Final = 1
_WRITE_COUNT: Final = 0
_WRITE_STRIDE: Final = 0
_UPDATE_INTERVAL: Final = 60


class SmartSettingsParseError(Exception):
    """The smart-settings body was truncated or internally inconsistent."""


class SmartSettingsRejectedError(Exception):
    """The requested energy-wastage value was not on or off."""


class SmartSettingsExchangeError(Exception):
    """The console did not complete a read or a confirmed write."""


@dataclass(frozen=True, slots=True)
class SmartSettingsSnapshot:
    """Information bytes from one reply, including the energy-wastage flag."""

    info: bytes

    def __post_init__(self) -> None:
        if len(self.info) < _MINIMUM_INFO_LENGTH:
            raise SmartSettingsParseError("smart settings omit energy wastage")

    @property
    def energy_wastage(self) -> bool:
        """Whether the console reported detection as enabled."""
        return self.info[_ENERGY_INDEX] != _DISABLED_BYTE

    def with_energy_wastage(self, *, enabled: bool) -> SmartSettingsSnapshot:
        """Return a copy whose energy-wastage byte is 0 or 1."""
        info = bytearray(self.info)
        info[_ENERGY_INDEX] = _ENABLED_BYTE if enabled else _DISABLED_BYTE
        return SmartSettingsSnapshot(bytes(info))


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


class EnergyWastageCoordinator(DataUpdateCoordinator[SmartSettingsSnapshot]):
    """Poll the flag and publish a write only after readback.

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
        token = current_entry.set(entry)
        try:
            super().__init__(
                hass,
                _LOGGER,
                name="AirTouch energy wastage",
                update_interval=timedelta(seconds=_UPDATE_INTERVAL),
            )
        finally:
            current_entry.reset(token)

    async def async_set(self, *, enabled: bool) -> None:
        """Change detection after a fresh read and a matching readback."""
        if not isinstance(enabled, bool):
            raise SmartSettingsRejectedError("energy wastage must be on or off")
        async with self._transaction_lock:
            fresh = await self._read_snapshot()
            patched = fresh.with_energy_wastage(enabled=enabled)
            await self._write_info(patched.info)
            confirmed = await self._read_snapshot()
            if confirmed.info != patched.info:
                raise SmartSettingsExchangeError("energy wastage readback differed")
            self.async_set_updated_data(confirmed)

    async def _async_update_data(self) -> SmartSettingsSnapshot:
        async with self._transaction_lock:
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
            raise SmartSettingsExchangeError("energy wastage read failed") from err

    async def _write_info(self, info: bytes) -> None:
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
            raise SmartSettingsExchangeError("energy wastage write failed") from err
        if confirmed.info != info:
            raise SmartSettingsExchangeError("energy wastage write was not stored")


def _record_length(count: int, stride: int) -> int:
    if count > 0 and stride == 0:
        raise SmartSettingsParseError("invalid smart settings record size")
    return count * stride


def _write_payload(info: bytes) -> bytes:
    return (
        bytes((_SUBCOMMAND, _SMART_SETTING_ID, len(info), _WRITE_COUNT, _WRITE_STRIDE))
        + info
    )
