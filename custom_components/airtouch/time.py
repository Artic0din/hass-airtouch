"""Polyaire AirTouch time sensor entities.

Time entities are used to represent:
- The on/off quick timers for each AC.
- The on/off timers for each Beam, in the console's local time.
"""

import datetime
import logging
from typing import Final

import pyairtouch
import voluptuous
from homeassistant.components import time
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import devices, entities
from .beam import (
    BeamCoordinator,
    BeamEntity,
    BeamId,
    BeamRefusedError,
    BeamUnit,
    DelayTimer,
    SetTimer,
    TimerRole,
    get_beam,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the AirTouch binary sensors."""
    airtouch: pyairtouch.AirTouch = hass.data[DOMAIN][config_entry.entry_id]

    discovered_entities: list[time.TimeEntity] = []

    airtouch_device = devices.AirTouchDevice(hass, config_entry.entry_id, airtouch)
    for airtouch_ac in airtouch.air_conditioners:
        ac_device = airtouch_device.ac_device(airtouch_ac)

        for timer_type in pyairtouch.AcTimerType:
            timer_entity = AcQuickTimerEntity(
                timer_type=timer_type,
                ac_device=ac_device,
                airtouch=airtouch,
                airtouch_ac=airtouch_ac,
            )
            discovered_entities.append(timer_entity)

    _LOGGER.debug("Found entities %s", discovered_entities)
    async_add_devices(discovered_entities)

    if airtouch.model == pyairtouch.AirTouchModel.AIRTOUCH_5:
        beams = get_beam(hass, config_entry.entry_id)

        def add_beams(units: list[BeamUnit]) -> None:
            async_add_devices(
                [
                    BeamTimerEntity(
                        beams,
                        airtouch_device.beam_device(unit.beam_id, unit.name),
                        unit.beam_id,
                        role,
                    )
                    for unit in units
                    for role in TimerRole
                ]
            )

        config_entry.async_on_unload(beams.async_add_discovery(add_beams))

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        name="clear_timer",
        schema={},
        func="async_clear_timer",
    )
    platform.async_register_entity_service(
        name="set_timer_from_delay",
        schema={voluptuous.Required("delay"): config_validation.positive_time_period},
        func="async_set_timer_from_delay",
    )


_TIMER_TYPE_NAME_MAPPING = {
    pyairtouch.AcTimerType.OFF_TIMER: "Off Timer",
    pyairtouch.AcTimerType.ON_TIMER: "On Timer",
}


class AcQuickTimerEntity(entities.AirTouchAcEntity, time.TimeEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Time entity for an AirTouch AC Quick Timer."""

    _attr_icon = "mdi:fan-clock"

    def __init__(
        self,
        timer_type: pyairtouch.AcTimerType,
        ac_device: devices.AcDevice,
        airtouch: pyairtouch.AirTouch,
        airtouch_ac: pyairtouch.AirConditioner,
    ) -> None:
        super().__init__(
            ac_device=ac_device,
            airtouch=airtouch,
            airtouch_ac=airtouch_ac,
            id_suffix="_" + timer_type.name.lower(),
        )
        self._timer_type = timer_type
        self._attr_name = _TIMER_TYPE_NAME_MAPPING[timer_type]

    @property
    def native_value(self) -> datetime.time | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        return self._airtouch_ac.next_quick_timer(self._timer_type)

    async def async_set_value(self, value: datetime.time) -> None:
        await self._airtouch_ac.set_quick_timer(self._timer_type, value)

    async def async_set_timer_from_delay(self, delay: datetime.timedelta) -> None:
        await self._airtouch_ac.set_quick_timer(self._timer_type, delay)

    async def async_clear_timer(self) -> None:
        await self._airtouch_ac.clear_quick_timer(self._timer_type)


_BEAM_TIMER_NAMES: Final = {TimerRole.OFF: "Off Timer", TimerRole.ON: "On Timer"}
_ONE_MINUTE: Final = datetime.timedelta(minutes=1)


class BeamTimerEntity(BeamEntity, time.TimeEntity):
    """A Beam on or off timer, in the console's local time.

    The console keeps the timer. Setting, clearing, or delaying one timer
    leaves the other as reported.
    """

    _attr_icon = "mdi:fan-clock"

    def __init__(
        self,
        coordinator: BeamCoordinator,
        beam_device: devices.BeamDevice,
        beam_id: BeamId,
        role: TimerRole,
    ) -> None:
        super().__init__(
            coordinator,
            beam_id,
            f"{beam_device.unique_id}_{role.name.lower()}_timer",
            beam_device.device_info,
        )
        self._role = role
        self._attr_name = _BEAM_TIMER_NAMES[role]

    @property
    def native_value(self) -> datetime.time | None:
        unit = self._unit
        return None if unit is None else unit.timer(self._role).value

    async def async_set_value(self, value: datetime.time) -> None:
        await self._write(SetTimer(self._role, value))

    async def async_clear_timer(self) -> None:
        await self._write(SetTimer(self._role, None))

    async def async_set_timer_from_delay(self, delay: datetime.timedelta) -> None:
        minutes, remainder = divmod(delay, _ONE_MINUTE)
        if remainder:
            raise BeamRefusedError("Beam timer delay must be whole minutes")
        await self._write(DelayTimer(self._role, minutes))
