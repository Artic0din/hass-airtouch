from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import TYPE_CHECKING, Final, assert_never, cast

import pyairtouch
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import current_entry
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from pyairtouch import AirTouchModel
from pyairtouch.at5.api import DEFAULT_PORT_NUMBER

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from pyairtouch import AirTouch

from . import devices
from .console import DEFAULT_DEADLINE, ConsoleClient, ConsoleProtocolError
from .preferences import whole_degrees

_LOGGER = logging.getLogger(__name__)

# C0 subheader: subcommand, pad, non-repeat length, repeat length, repeat count.
_SUBHEADER: Final = struct.Struct("!BxHHH")
_REPORT_ID: Final = 0x5B
_SET_ID: Final = 0x5A
_HEADER_FIELDS: Final = 4
_NAME_LENGTH_OFFSET: Final = 2
_RECORD_BEFORE_NAME: Final = 24
_AC_ID_MASK: Final = 0x0F
_DURATION_OFFSET: Final = 8
_DURATION_MASK: Final = 0x0F
# Bits 4-7 are auto-off, auto-on, and two undocumented flags.
_FLAG_MASK: Final = 0xF0
_DURATION_MIN: Final = 0
_DURATION_MAX: Final = 8
_BRAND_COOL_MAX: Final = 16
_BRAND_COOL_MIN: Final = 17
_BRAND_HEAT_MAX: Final = 18
_BRAND_HEAT_MIN: Final = 19
_USER_COOL_MAX: Final = 20
_USER_COOL_MIN: Final = 21
_USER_HEAT_MAX: Final = 22
_USER_HEAT_MIN: Final = 23
_UPDATE_INTERVAL: Final = 60
_OWNERS_KEY: Final = "airtouch_ac_settings"
_CELSIUS_STEP: Final = 1.0
_FAHRENHEIT_STEP: Final = 1.8


class AcSettingField(Enum):
    """The one byte or nibble a settings write may change."""

    COOL_MINIMUM = "cool_minimum"
    COOL_MAXIMUM = "cool_maximum"
    HEAT_MINIMUM = "heat_minimum"
    HEAT_MAXIMUM = "heat_maximum"
    AUTO_OFF_DURATION = "auto_off_duration"


class AcSettingsParseError(Exception):
    """The frame was not a usable 0x5B air-conditioner settings report."""


class AcSettingsRejectedError(Exception):
    """The requested value is outside the limits this report accepts."""


class AcSettingsExchangeError(Exception):
    """The console did not complete a settings read or confirmed write."""


@dataclass(frozen=True, slots=True)
class AcSettingRecord:
    """One repeat from a settings report, including its name and tail bytes."""

    raw: bytes

    @property
    def ac_id(self) -> int:
        """Return the air conditioner id stored in the low nibble."""
        return self.raw[0] & _AC_ID_MASK

    def stored(self, field: AcSettingField) -> int:
        """Return the raw byte or duration nibble without range checks."""
        match field:
            case AcSettingField.AUTO_OFF_DURATION:
                return self.raw[_DURATION_OFFSET] & _DURATION_MASK
            case AcSettingField.COOL_MINIMUM:
                return self.raw[_USER_COOL_MIN]
            case AcSettingField.COOL_MAXIMUM:
                return self.raw[_USER_COOL_MAX]
            case AcSettingField.HEAT_MINIMUM:
                return self.raw[_USER_HEAT_MIN]
            case AcSettingField.HEAT_MAXIMUM:
                return self.raw[_USER_HEAT_MAX]
            case _ as unreachable:
                assert_never(unreachable)

    def reported_limits(self, field: AcSettingField) -> tuple[int, int]:
        """Return minimum then maximum, including an inverted brand range."""
        match field:
            case AcSettingField.AUTO_OFF_DURATION:
                return (_DURATION_MIN, _DURATION_MAX)
            case AcSettingField.COOL_MINIMUM | AcSettingField.COOL_MAXIMUM:
                return (self.raw[_BRAND_COOL_MIN], self.raw[_BRAND_COOL_MAX])
            case AcSettingField.HEAT_MINIMUM | AcSettingField.HEAT_MAXIMUM:
                return (self.raw[_BRAND_HEAT_MIN], self.raw[_BRAND_HEAT_MAX])
            case _ as unreachable:
                assert_never(unreachable)

    def brand_limits(self, field: AcSettingField) -> tuple[int, int] | None:
        """Return usable inclusive bounds, or None when the range is inverted."""
        low, high = self.reported_limits(field)
        if low > high:
            return None
        return (low, high)

    def value(self, field: AcSettingField) -> int | None:
        """Return the stored field when it sits inside the accepted range."""
        stored = self.stored(field)
        limits = self.brand_limits(field)
        if limits is None:
            return None
        low, high = limits
        if stored < low or stored > high:
            return None
        return stored


@dataclass(frozen=True, slots=True)
class AcSettingsSnapshot:
    """One 0x5B report: its full header and the air-conditioner records in order."""

    header: bytes
    records: tuple[AcSettingRecord, ...]

    def record(self, ac_id: int) -> AcSettingRecord | None:
        """Return the record for this air conditioner, if the report has one."""
        for item in self.records:
            if item.ac_id == ac_id:
                return item
        return None

    def with_record(self, updated: AcSettingRecord) -> AcSettingsSnapshot:
        """Return a copy with the matching air conditioner replaced."""
        found = False
        records: list[AcSettingRecord] = []
        for item in self.records:
            if item.ac_id == updated.ac_id:
                records.append(updated)
                found = True
            else:
                records.append(item)
        if not found:
            raise AcSettingsRejectedError("AC settings omit this air conditioner")
        return AcSettingsSnapshot(self.header, tuple(records))


def parse_ac_settings_report(payload: bytes) -> AcSettingsSnapshot:
    """Parse a 0x5B settings report, retaining its header and repeat bytes."""
    normal_length, repeat_length, repeat_count = _report_layout(payload)
    header = payload[_SUBHEADER.size : _SUBHEADER.size + normal_length]
    if header[0] != repeat_count:
        raise AcSettingsParseError("AC settings record count mismatch")
    _require_repeat(header, repeat_length, repeat_count)
    return AcSettingsSnapshot(
        header, _records(payload, normal_length, repeat_length, repeat_count)
    )


class AcSettingsCoordinator(DataUpdateCoordinator[AcSettingsSnapshot]):
    """Poll AC settings and publish a write only after readback."""

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
        self._entry_id = entry.entry_id
        token = current_entry.set(entry)
        try:
            super().__init__(
                hass,
                _LOGGER,
                name="AirTouch AC settings",
                update_interval=timedelta(seconds=_UPDATE_INTERVAL),
            )
        finally:
            current_entry.reset(token)

    async def async_set(self, ac_id: int, field: AcSettingField, value: float) -> None:
        """Change one field after a fresh read and a matching readback."""
        whole = _whole_setting(value)
        if field is AcSettingField.AUTO_OFF_DURATION:
            _require_duration(whole)
        async with self._transaction_lock:
            if self._closed:
                raise AcSettingsExchangeError("AC settings owner is closed")
            fresh = await self._read_snapshot()
            record = fresh.record(ac_id)
            if record is None:
                raise AcSettingsRejectedError("AC settings omit this air conditioner")
            patched = _patched_raw(record, field, whole)
            await self._write_record(fresh.header, patched)
            confirmed = await self._read_snapshot()
            expected = fresh.with_record(AcSettingRecord(patched))
            if confirmed != expected:
                raise AcSettingsExchangeError("AC settings readback differed")
            self.async_set_updated_data(confirmed)

    async def async_shutdown(self) -> None:
        """Stop polling, finish admitted work, and reject anything still waiting."""
        self._closed = True
        await super().async_shutdown()
        async with self._transaction_lock:
            pass
        self._forget()

    async def _async_update_data(self) -> AcSettingsSnapshot:
        async with self._transaction_lock:
            if self._closed:
                raise UpdateFailed("AC settings owner is closed")
            try:
                return await self._read_snapshot()
            except AcSettingsExchangeError as err:
                raise UpdateFailed(str(err)) from err

    async def _read_snapshot(self) -> AcSettingsSnapshot:
        try:
            payload = await self._client.exchange_control(_query_payload())
            return parse_ac_settings_report(payload)
        except (
            ConsoleProtocolError,
            AcSettingsParseError,
            OSError,
            TimeoutError,
        ) as err:
            raise AcSettingsExchangeError("AC settings read failed") from err

    async def _write_record(self, header: bytes, record: bytes) -> None:
        # The 0x5A reply has to be a 0x5B report.
        try:
            payload = await self._client.exchange_control(
                _write_payload(header, record)
            )
            parse_ac_settings_report(payload)
        except (
            ConsoleProtocolError,
            AcSettingsParseError,
            OSError,
            TimeoutError,
        ) as err:
            raise AcSettingsExchangeError("AC settings write failed") from err

    def _forget(self) -> None:
        stored = self.hass.data.get(_OWNERS_KEY)
        if not isinstance(stored, dict):
            return
        owners = cast("dict[str, AcSettingsCoordinator]", stored)
        if owners.get(self._entry_id) is self:
            del owners[self._entry_id]
        if not owners:
            self.hass.data.pop(_OWNERS_KEY, None)


def ensure_ac_settings(
    hass: HomeAssistant,
    entry: ConfigEntry,
    airtouch: AirTouch,
    *,
    port: int = DEFAULT_PORT_NUMBER,
    deadline: float = DEFAULT_DEADLINE,
) -> AcSettingsCoordinator | None:
    """Return the AirTouch 5 settings owner for this entry.

    A second call returns the owner already installed. AirTouch 4 returns None.
    """
    if airtouch.model != AirTouchModel.AIRTOUCH_5:
        return None
    owners = _owners(hass)
    current = owners.get(entry.entry_id)
    if isinstance(current, AcSettingsCoordinator):
        return current
    if current is not None:
        raise TypeError("AC settings owner map is invalid")
    coordinator = AcSettingsCoordinator(
        hass, entry, airtouch, port=port, deadline=deadline
    )
    owners[entry.entry_id] = coordinator
    return coordinator


async def async_release_ac_settings(hass: HomeAssistant, entry_id: str) -> None:
    """Close this entry's owner if installed, draining any admitted write."""
    owners = cast("dict[str, AcSettingsCoordinator]", hass.data.get(_OWNERS_KEY, {}))
    coordinator = owners.get(entry_id)
    if coordinator is not None:
        await coordinator.async_shutdown()


class AcSettingNumber(CoordinatorEntity[AcSettingsCoordinator], NumberEntity):
    """One confirmed limit or auto-off duration for an air conditioner."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: AcSettingsCoordinator,
        unique_id: str,
        device_info: DeviceInfo,
        ac_id: int,
        field: AcSettingField,
    ) -> None:
        super().__init__(coordinator)
        self._ac_id = ac_id
        self._field = field
        self._attr_translation_key = field.value
        self._attr_name = _field_name(field)
        self._attr_unique_id = f"{unique_id}_{field.value}"
        self._attr_device_info = device_info
        if field is AcSettingField.AUTO_OFF_DURATION:
            self._attr_device_class = NumberDeviceClass.DURATION
            self._attr_native_unit_of_measurement = UnitOfTime.HOURS
            return
        self._attr_device_class = NumberDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @property
    def native_step(self) -> float:
        # HA converts displayed temperatures but leaves native_step unchanged.
        if self._field is AcSettingField.AUTO_OFF_DURATION:
            return _CELSIUS_STEP
        if (
            self.device_class == NumberDeviceClass.TEMPERATURE
            and self.unit_of_measurement == UnitOfTemperature.FAHRENHEIT
        ):
            return _FAHRENHEIT_STEP
        return _CELSIUS_STEP

    @property
    def native_min_value(self) -> float:
        limits = self._reported_limits()
        if limits is None:
            return 0.0
        return float(limits[0])

    @property
    def native_max_value(self) -> float:
        limits = self._reported_limits()
        if limits is None:
            return 0.0
        return float(limits[1])

    @property
    def available(self) -> bool:
        record = self._record()
        return (
            super().available
            and record is not None
            and record.brand_limits(self._field) is not None
        )

    @property
    def native_value(self) -> float | None:
        record = self._record()
        if not self.available or record is None:
            return None
        value = record.value(self._field)
        if value is None:
            return None
        return float(value)

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_set(self._ac_id, self._field, value)
        except (AcSettingsRejectedError, AcSettingsExchangeError) as err:
            raise HomeAssistantError(str(err)) from err

    def _record(self) -> AcSettingRecord | None:
        data = self.coordinator.data
        if data is None:
            return None
        found = data.record(self._ac_id)
        if isinstance(found, AcSettingRecord):
            return found
        return None

    def _reported_limits(self) -> tuple[int, int] | None:
        if self._field is AcSettingField.AUTO_OFF_DURATION:
            return (_DURATION_MIN, _DURATION_MAX)
        record = self._record()
        if record is None:
            return None
        return record.reported_limits(self._field)


async def async_collect_ac_setting_numbers(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    airtouch: AirTouch,
) -> list[NumberEntity]:
    """Create five limit numbers for each AirTouch 5 air conditioner.

    Other models, and air conditioners without an id, get none. A failed
    settings read does not fail platform setup.
    """
    if airtouch.model != AirTouchModel.AIRTOUCH_5:
        return []
    selected: list[tuple[pyairtouch.AirConditioner, int]] = []
    for air_conditioner in airtouch.air_conditioners:
        ac_id = getattr(air_conditioner, "ac_id", None)
        if isinstance(ac_id, bool) or not isinstance(ac_id, int):
            continue
        selected.append((air_conditioner, ac_id))
    if not selected:
        return []
    coordinator = ensure_ac_settings(hass, config_entry, airtouch)
    if coordinator is None:
        return []
    await coordinator.async_refresh()
    console = devices.AirTouchDevice(hass, config_entry.entry_id, airtouch)
    entities: list[NumberEntity] = []
    for air_conditioner, ac_id in selected:
        ac_device = console.ac_device(air_conditioner)
        entities.extend(
            AcSettingNumber(
                coordinator,
                ac_device.unique_id,
                ac_device.device_info,
                ac_id,
                field,
            )
            for field in AcSettingField
        )
    return entities


def _report_layout(payload: bytes) -> tuple[int, int, int]:
    if len(payload) < _SUBHEADER.size:
        raise AcSettingsParseError("truncated AC settings header")
    sub_id, normal_length, repeat_length, repeat_count = _SUBHEADER.unpack_from(payload)
    declared = _SUBHEADER.size + normal_length + repeat_length * repeat_count
    if (
        sub_id != _REPORT_ID
        or normal_length < _HEADER_FIELDS
        or declared != len(payload)
    ):
        raise AcSettingsParseError("malformed AC settings report")
    return normal_length, repeat_length, repeat_count


def _require_repeat(header: bytes, repeat_length: int, repeat_count: int) -> None:
    if repeat_count == 0:
        if repeat_length != 0:
            raise AcSettingsParseError("AC settings repeat length mismatch")
        return
    if repeat_length < _RECORD_BEFORE_NAME + header[_NAME_LENGTH_OFFSET]:
        raise AcSettingsParseError("truncated AC settings name")


def _records(
    payload: bytes, normal_length: int, repeat_length: int, repeat_count: int
) -> tuple[AcSettingRecord, ...]:
    start = _SUBHEADER.size + normal_length
    records: list[AcSettingRecord] = []
    seen: set[int] = set()
    for index in range(repeat_count):
        offset = start + index * repeat_length
        raw = payload[offset : offset + repeat_length]
        ac_id = raw[0] & _AC_ID_MASK
        if ac_id in seen:
            raise AcSettingsParseError("duplicate AC id")
        seen.add(ac_id)
        records.append(AcSettingRecord(raw))
    return tuple(records)


def _query_payload() -> bytes:
    return _SUBHEADER.pack(_REPORT_ID, 0, 0, 0)


def _write_payload(header: bytes, record: bytes) -> bytes:
    # The console's own writer zeroes brand identity and brand limits.
    return _SUBHEADER.pack(_SET_ID, len(header), len(record), 1) + header + record


def _whole_setting(value: float) -> int:
    try:
        return whole_degrees(value)
    except (TypeError, ValueError) as err:
        raise AcSettingsRejectedError(str(err)) from err


def _require_duration(value: int) -> None:
    if value < _DURATION_MIN or value > _DURATION_MAX:
        raise AcSettingsRejectedError("auto-off duration is outside 0 to 8 hours")


def _patched_raw(record: AcSettingRecord, field: AcSettingField, value: int) -> bytes:
    updated = bytearray(record.raw)
    if field is AcSettingField.AUTO_OFF_DURATION:
        _require_duration(value)
        updated[_DURATION_OFFSET] = (updated[_DURATION_OFFSET] & _FLAG_MASK) | value
        return bytes(updated)
    _require_temperature(record, field, value)
    updated[_user_offset(field)] = value
    return bytes(updated)


def _require_temperature(
    record: AcSettingRecord, field: AcSettingField, value: int
) -> None:
    limits = record.brand_limits(field)
    if limits is None:
        raise AcSettingsRejectedError("brand temperature range is unusable")
    low, high = limits
    if value < low or value > high:
        raise AcSettingsRejectedError("temperature is outside the brand range")
    opposite = record.stored(_opposite(field))
    if _is_minimum(field):
        if value > opposite:
            raise AcSettingsRejectedError("minimum is above maximum")
        return
    if opposite > value:
        raise AcSettingsRejectedError("minimum is above maximum")


def _opposite(field: AcSettingField) -> AcSettingField:
    match field:
        case AcSettingField.COOL_MINIMUM:
            return AcSettingField.COOL_MAXIMUM
        case AcSettingField.COOL_MAXIMUM:
            return AcSettingField.COOL_MINIMUM
        case AcSettingField.HEAT_MINIMUM:
            return AcSettingField.HEAT_MAXIMUM
        case AcSettingField.HEAT_MAXIMUM:
            return AcSettingField.HEAT_MINIMUM
        case AcSettingField.AUTO_OFF_DURATION:
            raise AcSettingsRejectedError("auto-off duration has no opposite limit")
        case _ as unreachable:
            assert_never(unreachable)


def _is_minimum(field: AcSettingField) -> bool:
    match field:
        case AcSettingField.COOL_MINIMUM | AcSettingField.HEAT_MINIMUM:
            return True
        case (
            AcSettingField.COOL_MAXIMUM
            | AcSettingField.HEAT_MAXIMUM
            | AcSettingField.AUTO_OFF_DURATION
        ):
            return False
        case _ as unreachable:
            assert_never(unreachable)


def _user_offset(field: AcSettingField) -> int:
    match field:
        case AcSettingField.COOL_MINIMUM:
            return _USER_COOL_MIN
        case AcSettingField.COOL_MAXIMUM:
            return _USER_COOL_MAX
        case AcSettingField.HEAT_MINIMUM:
            return _USER_HEAT_MIN
        case AcSettingField.HEAT_MAXIMUM:
            return _USER_HEAT_MAX
        case AcSettingField.AUTO_OFF_DURATION:
            raise AcSettingsRejectedError("auto-off duration is not a temperature")
        case _ as unreachable:
            assert_never(unreachable)


def _field_name(field: AcSettingField) -> str:
    match field:
        case AcSettingField.COOL_MINIMUM:
            return "Cool minimum"
        case AcSettingField.COOL_MAXIMUM:
            return "Cool maximum"
        case AcSettingField.HEAT_MINIMUM:
            return "Heat minimum"
        case AcSettingField.HEAT_MAXIMUM:
            return "Heat maximum"
        case AcSettingField.AUTO_OFF_DURATION:
            return "Auto off duration"
        case _ as unreachable:
            assert_never(unreachable)


def _owners(hass: HomeAssistant) -> dict[str, AcSettingsCoordinator]:
    stored = hass.data.get(_OWNERS_KEY)
    if stored is None:
        created: dict[str, AcSettingsCoordinator] = {}
        hass.data[_OWNERS_KEY] = created
        return created
    if not isinstance(stored, dict):
        raise TypeError("AC settings owner map is invalid")
    return cast("dict[str, AcSettingsCoordinator]", stored)
