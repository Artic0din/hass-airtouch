from __future__ import annotations

from typing import TYPE_CHECKING

import pyairtouch
from homeassistant.components.button import ButtonEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import devices
from .const import DOMAIN
from .io_outputs import (
    IoOutputEntity,
    IoOutputsCoordinator,
    PulseOutput,
    get_io_outputs,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add a button for each pulse output, now and as outputs are configured."""
    airtouch: pyairtouch.AirTouch = hass.data[DOMAIN][config_entry.entry_id]
    if airtouch.model != pyairtouch.AirTouchModel.AIRTOUCH_5:
        async_add_entities([])
        return
    device = devices.AirTouchDevice(hass, config_entry.entry_id, airtouch)
    outputs = get_io_outputs(hass, config_entry.entry_id)
    config_entry.async_on_unload(
        outputs.async_add_discovery(
            PulseOutput,
            lambda channels: async_add_entities(
                [
                    RelayPulseButton(
                        outputs, device.unique_id, device.device_info, channel
                    )
                    for channel in channels
                ]
            ),
        )
    )


class RelayPulseButton(IoOutputEntity, ButtonEntity):
    """Send one native pulse. A press is not proof that the contact closed."""

    def __init__(
        self,
        coordinator: IoOutputsCoordinator,
        unique_id: str,
        device_info: DeviceInfo,
        channel: int,
    ) -> None:
        super().__init__(coordinator, PulseOutput, unique_id, device_info, channel)

    async def async_press(self) -> None:
        await self._act(self.coordinator.async_pulse(self._channel))
