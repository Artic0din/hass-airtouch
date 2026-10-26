from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pyairtouch
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import devices
from .ac_settings import async_collect_ac_setting_numbers
from .const import DOMAIN
from .preferences import (
    PREFERENCE_LIMITS,
    PreferenceCoordinator,
    PreferenceExchangeError,
    PreferenceField,
    PreferenceRejectedError,
)
from .smart_settings import (
    SMART_NUMBER_RANGES,
    SmartNumber,
    SmartSettingsCoordinator,
    SmartSettingsExchangeError,
    SmartSettingsRejectedError,
    get_smart_settings,
)


@dataclass(frozen=True, slots=True)
class _FieldSpec:
    key: str
    name: str


_SPECS: Final = {
    PreferenceField.SLEEP_COOL: _FieldSpec("sleep_cool", "Sleep cool"),
    PreferenceField.SLEEP_HEAT: _FieldSpec("sleep_heat", "Sleep heat"),
    PreferenceField.AWAY_MINIMUM: _FieldSpec("away_minimum", "Away minimum"),
    PreferenceField.AWAY_MAXIMUM: _FieldSpec("away_maximum", "Away maximum"),
}

_NUMBER_NAMES: Final[dict[SmartNumber, str]] = {
    SmartNumber.COOL_DIFFERENTIAL: "Weather adaptation cool differential",
    SmartNumber.COOL_TARGET: "Weather adaptation cool target",
    SmartNumber.HEAT_DIFFERENTIAL: "Weather adaptation heat differential",
    SmartNumber.HEAT_TARGET: "Weather adaptation heat target",
}

_CELSIUS_STEP: Final = 1.0
_FAHRENHEIT_STEP: Final = 1.8


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    airtouch: pyairtouch.AirTouch = hass.data[DOMAIN][config_entry.entry_id]
    if airtouch.model != pyairtouch.AirTouchModel.AIRTOUCH_5:
        async_add_entities([])
        return
    device = devices.AirTouchDevice(hass, config_entry.entry_id, airtouch)
    async_add_entities(
        await async_collect_preference_numbers(
            hass,
            config_entry,
            airtouch,
            unique_id=device.unique_id,
            device_info=device.device_info,
        )
    )
    async_add_entities(
        await async_collect_smart_setting_numbers(
            hass,
            config_entry,
            airtouch,
            unique_id=device.unique_id,
            device_info=device.device_info,
        )
    )
    async_add_entities(
        await async_collect_ac_setting_numbers(hass, config_entry, airtouch)
    )


async def async_collect_preference_numbers(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    airtouch: pyairtouch.AirTouch,
    *,
    unique_id: str,
    device_info: DeviceInfo,
) -> list[NumberEntity]:
    """Create the four console numbers for an AirTouch 5.

    Other models get no numbers. A failed preference read does not fail
    platform setup.
    """
    if airtouch.model != pyairtouch.AirTouchModel.AIRTOUCH_5:
        return []
    coordinator = PreferenceCoordinator(hass, config_entry, airtouch)
    await coordinator.async_refresh()
    return [
        PreferenceNumber(coordinator, unique_id, device_info, field)
        for field in PreferenceField
    ]


class PreferenceNumber(CoordinatorEntity[PreferenceCoordinator], NumberEntity):
    _attr_has_entity_name = True
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        coordinator: PreferenceCoordinator,
        unique_id: str,
        device_info: DeviceInfo,
        field: PreferenceField,
    ) -> None:
        super().__init__(coordinator)
        spec = _SPECS[field]
        self._field = field
        self._attr_translation_key = spec.key
        self._attr_name = spec.name
        self._attr_unique_id = f"{unique_id}_{spec.key}"
        self._attr_device_info = device_info
        minimum, maximum = PREFERENCE_LIMITS[field]
        self._attr_native_min_value = float(minimum)
        self._attr_native_max_value = float(maximum)

    @property
    def native_step(self) -> float:
        # HA converts display temperatures but leaves native_step unchanged.
        if self.unit_of_measurement == UnitOfTemperature.FAHRENHEIT:
            return _FAHRENHEIT_STEP
        return _CELSIUS_STEP

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def native_value(self) -> float | None:
        if not self.available:
            return None
        data = self.coordinator.data
        if data is None:
            return None
        return float(data.value(self._field))

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_set(self._field, value)
        except (PreferenceRejectedError, PreferenceExchangeError) as err:
            raise HomeAssistantError(str(err)) from err


async def async_collect_smart_setting_numbers(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    airtouch: pyairtouch.AirTouch,
    *,
    unique_id: str,
    device_info: DeviceInfo,
) -> list[SmartSettingNumber]:
    """Create the four weather numbers for an AirTouch 5.

    Other models get none. These share the installed smart-settings owner and
    do not open a connection during setup.
    """
    if airtouch.model != pyairtouch.AirTouchModel.AIRTOUCH_5:
        return []
    coordinator = get_smart_settings(hass, config_entry.entry_id)
    return [
        SmartSettingNumber(coordinator, unique_id, device_info, field)
        for field in SmartNumber
    ]


class SmartSettingNumber(CoordinatorEntity[SmartSettingsCoordinator], NumberEntity):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        coordinator: SmartSettingsCoordinator,
        unique_id: str,
        device_info: DeviceInfo,
        field: SmartNumber,
    ) -> None:
        super().__init__(coordinator)
        limits = SMART_NUMBER_RANGES[field]
        self._field = field
        self._attr_translation_key = field.value
        self._attr_name = _NUMBER_NAMES[field]
        self._attr_unique_id = f"{unique_id}_{field.value}"
        self._attr_device_info = device_info
        self._attr_native_min_value = float(limits.minimum)
        self._attr_native_max_value = float(limits.maximum)
        # An interval must not use the temperature device class. Home Assistant
        # would convert it as an absolute temperature and add the Fahrenheit offset.
        if limits.absolute:
            self._attr_device_class = NumberDeviceClass.TEMPERATURE

    @property
    def native_step(self) -> float:
        # HA converts display temperatures but leaves native_step unchanged.
        if (
            self.device_class == NumberDeviceClass.TEMPERATURE
            and self.unit_of_measurement == UnitOfTemperature.FAHRENHEIT
        ):
            return _FAHRENHEIT_STEP
        return _CELSIUS_STEP

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if not self.available or data is None:
            return None
        value = data.number(self._field)
        if value is None:
            return None
        return float(value)

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_set_number(self._field, value)
        except (SmartSettingsRejectedError, SmartSettingsExchangeError) as err:
            raise HomeAssistantError(str(err)) from err
