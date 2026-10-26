from __future__ import annotations

from typing import TYPE_CHECKING

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
    SmartSettingsCoordinator,
    SmartSettingsExchangeError,
    SmartSettingsRejectedError,
    SmartSettingsSnapshot,
    get_smart_settings,
)


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
        await async_collect_energy_wastage_switches(
            hass,
            config_entry,
            airtouch,
            unique_id=device.unique_id,
            device_info=device.device_info,
        )
    )


async def async_collect_energy_wastage_switches(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    airtouch: pyairtouch.AirTouch,
    *,
    unique_id: str,
    device_info: DeviceInfo,
) -> list[EnergyWastageSwitch]:
    """Create the console switch for an AirTouch 5.

    Other models get no switch. A failed settings read does not fail platform
    setup.
    """
    if airtouch.model != pyairtouch.AirTouchModel.AIRTOUCH_5:
        return []
    coordinator = get_smart_settings(hass, config_entry.entry_id)
    return [EnergyWastageSwitch(coordinator, unique_id, device_info)]


class EnergyWastageSwitch(CoordinatorEntity[SmartSettingsCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "energy_wastage_detection"
    _attr_name = "Energy wastage detection"

    def __init__(
        self,
        coordinator: SmartSettingsCoordinator,
        unique_id: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{unique_id}_energy_wastage_detection"
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def is_on(self) -> bool | None:
        data: SmartSettingsSnapshot | None = self.coordinator.data
        if not self.available or data is None:
            return None
        return data.energy_wastage

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
            await self.coordinator.async_set(enabled=enabled)
        except (SmartSettingsRejectedError, SmartSettingsExchangeError) as err:
            raise HomeAssistantError(str(err)) from err
