from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pyairtouch
from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import devices
from .const import DOMAIN
from .smart_settings import (
    SmartFlag,
    SmartSettingsCoordinator,
    SmartSettingsExchangeError,
    SmartSettingsRejectedError,
    SmartSettingsSnapshot,
    get_smart_settings,
)

_FLAG_NAMES: Final[dict[SmartFlag, str]] = {
    SmartFlag.ADVANCED: "Advanced weather adaptation",
    SmartFlag.ENERGY_WASTAGE: "Energy wastage detection",
    SmartFlag.BASIC: "Weather adaptation",
    SmartFlag.AUTO_OFF: "Weather adaptation auto off",
}


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
        await async_collect_smart_setting_switches(
            hass,
            config_entry,
            airtouch,
            unique_id=device.unique_id,
            device_info=device.device_info,
        )
    )


async def async_collect_smart_setting_switches(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    airtouch: pyairtouch.AirTouch,
    *,
    unique_id: str,
    device_info: DeviceInfo,
) -> list[SmartSettingSwitch]:
    """Create the console switches for an AirTouch 5.

    Other models get no switches. A failed settings read does not fail
    platform setup.
    """
    if airtouch.model != pyairtouch.AirTouchModel.AIRTOUCH_5:
        return []
    coordinator = get_smart_settings(hass, config_entry.entry_id)
    return [
        SmartSettingSwitch(coordinator, unique_id, device_info, field)
        for field in SmartFlag
    ]


class SmartSettingSwitch(CoordinatorEntity[SmartSettingsCoordinator], SwitchEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SmartSettingsCoordinator,
        unique_id: str,
        device_info: DeviceInfo,
        field: SmartFlag,
    ) -> None:
        super().__init__(coordinator)
        self._field = field
        self._attr_translation_key = field.value
        self._attr_name = _FLAG_NAMES[field]
        self._attr_unique_id = f"{unique_id}_{field.value}"
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return (
            super().available
            and data is not None
            and data.flag(self._field) is not None
        )

    @property
    def is_on(self) -> bool | None:
        data: SmartSettingsSnapshot | None = self.coordinator.data
        if not self.available or data is None:
            return None
        return data.flag(self._field)

    async def async_turn_on(
        self,
        **kwargs: object,
    ) -> None:
        del kwargs
        await self._apply(enabled=True)

    async def async_turn_off(
        self,
        **kwargs: object,
    ) -> None:
        del kwargs
        await self._apply(enabled=False)

    async def _apply(self, *, enabled: bool) -> None:
        try:
            await self.coordinator.async_set_flag(self._field, enabled=enabled)
        except (SmartSettingsRejectedError, SmartSettingsExchangeError) as err:
            raise HomeAssistantError(str(err)) from err
