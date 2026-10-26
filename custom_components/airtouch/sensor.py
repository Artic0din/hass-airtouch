"""Polyaire AirTouch sensor entities."""

import enum
import inspect
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

import pyairtouch
import voluptuous
from homeassistant.components import sensor
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation, entity_platform, service
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from homeassistant.helpers.entity import Entity

from . import climate, devices, entities
from .const import CONF_SPILL_BYPASS, CONF_SPILL_ZONES, DOMAIN, SpillBypass
from .geofence import (
    GeofenceCoordinator,
    GeofenceEventError,
    GeofenceSnapshot,
    GeofenceUncertainError,
    SafeText,
    Transition,
    get_geofence,
    safe_text,
)
from .programs import ProgramCoordinator, get_programs
from .usage import UsageCoordinator, UsageSnapshot, local_now

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Set up the AirTouch sensors."""
    airtouch: pyairtouch.AirTouch = hass.data[DOMAIN][config_entry.entry_id]

    # When reading serialised configuration, the config data will be the
    # underlying value not the enum value so it needs to be converted to an enum
    # literal for future comparisons.
    spill_bypass = SpillBypass(
        config_entry.data.get(CONF_SPILL_BYPASS, SpillBypass.SPILL)
    )
    spill_zones: list[int] = config_entry.data.get(CONF_SPILL_ZONES, [])

    discovered_entities: list[sensor.SensorEntity] = []

    airtouch_device = devices.AirTouchDevice(hass, config_entry.entry_id, airtouch)
    for airtouch_ac in airtouch.air_conditioners:
        ac_device = airtouch_device.ac_device(airtouch_ac)
        ac_temperature_entity = AcTemperatureEntity(
            ac_device=ac_device,
            airtouch=airtouch,
            airtouch_ac=airtouch_ac,
        )
        discovered_entities.append(ac_temperature_entity)

        ac_error_entity = AcErrorEntity(
            ac_device=ac_device,
            airtouch=airtouch,
            airtouch_ac=airtouch_ac,
        )
        discovered_entities.append(ac_error_entity)

        # Active fan speed is only useful for systems the support intelligent auto.
        if pyairtouch.AcFanSpeed.INTELLIGENT_AUTO in airtouch_ac.supported_fan_speeds:
            ac_fan_speed_entity = AcActiveFanSpeedEntity(
                ac_device=ac_device,
                airtouch=airtouch,
                airtouch_ac=airtouch_ac,
            )
            discovered_entities.append(ac_fan_speed_entity)

        spill_zone_count = 0

        for airtouch_zone in airtouch_ac.zones:
            zone_device = ac_device.zone_device(airtouch_zone)
            zone_percentage_entity = ZonePercentageEntity(
                zone_device=zone_device,
                airtouch=airtouch,
                airtouch_zone=airtouch_zone,
            )
            discovered_entities.append(zone_percentage_entity)

            if airtouch_zone.has_temp_sensor:
                zone_temperature_entity = ZoneTemperatureEntity(
                    zone_device=zone_device,
                    airtouch=airtouch,
                    airtouch_zone=airtouch_zone,
                )
                discovered_entities.append(zone_temperature_entity)

            if airtouch_zone.zone_id in spill_zones:
                spill_zone_count += 1

        if (
            spill_bypass == SpillBypass.BYPASS
            and airtouch.model == pyairtouch.AirTouchModel.AIRTOUCH_5
        ):
            # The AirTouch 5 supports fractional bypass so we can create a
            # bypass percentage sensor. The logic is the same as having a single
            # spill zone.
            spill_zone_count = 1

        if spill_zone_count > 0:
            ac_spill_bypass_percentage_entity = SpillBypassPercentageEntity(
                spill_bypass=spill_bypass,
                ac_device=ac_device,
                airtouch=airtouch,
                airtouch_ac=airtouch_ac,
                spill_zone_count=spill_zone_count,
            )
            discovered_entities.append(ac_spill_bypass_percentage_entity)

    discovered_entities.extend(
        await async_collect_usage_entities(
            hass, config_entry, airtouch, airtouch_device
        )
    )
    discovered_entities.extend(
        await async_collect_program_entities(
            hass, config_entry, airtouch, airtouch_device
        )
    )
    discovered_entities.extend(
        await async_collect_geofence_entities(
            hass, config_entry, airtouch, airtouch_device
        )
    )

    _LOGGER.debug("Found entities: %s", discovered_entities)
    async_add_devices(discovered_entities)

    platform = entity_platform.async_get_current_platform()
    _register_geofence_presence(platform)


class _PlatformListServiceCall(Protocol):
    def __call__(
        self,
        hass: HomeAssistant,
        platforms: "Iterable[entity_platform.EntityPlatform]",
        func: "Callable[[Entity, ServiceCall], Awaitable[ServiceResponse]]",
        call: ServiceCall,
        required_features: "Iterable[int]",
    ) -> "Awaitable[ServiceResponse]": ...


def _register_geofence_presence(platform: entity_platform.EntityPlatform) -> None:
    hass = platform.hass
    name = "post_geofence_presence"
    if hass.services.has_service(DOMAIN, name):
        return
    schema: dict[str | voluptuous.Marker, object] = {
        voluptuous.Required("transition"): voluptuous.In(("enter", "exit")),
        voluptuous.Required("identity"): _safe_text,
        voluptuous.Optional("email"): _safe_text,
    }
    features = [GeofenceFeature.POST_PRESENCE]
    if (
        "supports_response"
        in inspect.signature(platform.async_register_entity_service).parameters
    ):
        platform.async_register_entity_service(
            name=name,
            schema=schema,
            func="async_post_geofence_presence",
            required_features=features,
            supports_response=SupportsResponse.ONLY,
        )
        return
    domain = platform.domain
    dispatch = cast("_PlatformListServiceCall", service.entity_service_call)

    async def handle(call: ServiceCall) -> ServiceResponse:
        platforms = [
            item
            for item in hass.data[entity_platform.DATA_ENTITY_PLATFORM][DOMAIN]
            if item.domain == domain
        ]
        return await dispatch(hass, platforms, _post_keyed_presence, call, features)

    hass.services.async_register(
        DOMAIN,
        name,
        handle,
        voluptuous.Schema(config_validation.make_entity_service_schema(schema)),
        supports_response=SupportsResponse.ONLY,
    )


async def _post_keyed_presence(entity: "Entity", call: ServiceCall) -> ServiceResponse:
    geofence_entity = cast("GeofenceEntity", entity)
    response = await geofence_entity.async_post_geofence_presence(
        call.data["transition"], call.data["identity"], call.data.get("email")
    )
    return {entity.entity_id: response}


def _safe_text(value: object) -> SafeText:
    if not isinstance(value, str):
        raise voluptuous.Invalid("value must be text")
    try:
        return safe_text(value)
    except ValueError as err:
        raise voluptuous.Invalid(str(err)) from None


async def async_collect_usage_entities(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    airtouch: pyairtouch.AirTouch,
    airtouch_device: devices.AirTouchDevice,
) -> list[sensor.SensorEntity]:
    """Create AirTouch 5 run-time sensors.

    Other models keep the existing sensor set. A failed history read does not
    fail platform setup.
    """
    if airtouch.model != pyairtouch.AirTouchModel.AIRTOUCH_5:
        return []

    coordinator = UsageCoordinator(hass, config_entry, airtouch)
    await coordinator.async_refresh()
    discovered: list[sensor.SensorEntity] = []
    for airtouch_ac in airtouch.air_conditioners:
        ac_device = airtouch_device.ac_device(airtouch_ac)
        discovered.append(
            AcRunTimeEntity(
                coordinator=coordinator,
                unique_id=ac_device.unique_id,
                device_info=ac_device.device_info,
                ac_id=airtouch_ac.ac_id,
            )
        )
        for airtouch_zone in airtouch_ac.zones:
            zone_device = ac_device.zone_device(airtouch_zone)
            discovered.append(
                ZoneRunTimeEntity(
                    coordinator=coordinator,
                    unique_id=zone_device.unique_id,
                    device_info=zone_device.device_info,
                    zone_id=airtouch_zone.zone_id,
                )
            )
    return discovered


async def async_collect_program_entities(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    airtouch: pyairtouch.AirTouch,
    airtouch_device: devices.AirTouchDevice,
) -> list[sensor.SensorEntity]:
    """Create the AirTouch 5 programs sensor.

    Other models have no programs sensor. A failed read does not fail
    platform setup, and the sensor is created unavailable.
    """
    if airtouch.model != pyairtouch.AirTouchModel.AIRTOUCH_5:
        return []

    coordinator = get_programs(hass, config_entry.entry_id)
    await coordinator.async_refresh()
    return [
        ProgramsEntity(
            coordinator=coordinator,
            unique_id=airtouch_device.unique_id,
            device_info=airtouch_device.device_info,
        )
    ]


async def async_collect_geofence_entities(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    airtouch: pyairtouch.AirTouch,
    airtouch_device: devices.AirTouchDevice,
) -> list[sensor.SensorEntity]:
    """Create the AirTouch 5 geofence sensor.

    Other models have no geofence sensor. A failed read does not fail
    platform setup.
    """
    if airtouch.model != pyairtouch.AirTouchModel.AIRTOUCH_5:
        return []

    coordinator = get_geofence(hass, config_entry.entry_id)
    await coordinator.async_refresh()
    return [
        GeofenceEntity(
            coordinator=coordinator,
            unique_id=airtouch_device.unique_id,
            device_info=airtouch_device.device_info,
        )
    ]


class AcTemperatureEntity(entities.AirTouchAcEntity, sensor.SensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Sensor reporting the current temperature of an air-conditioner."""

    _attr_name = "Temperature"
    _attr_device_class = sensor.SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = sensor.SensorStateClass.MEASUREMENT

    def __init__(
        self,
        ac_device: devices.AcDevice,
        airtouch: pyairtouch.AirTouch,
        airtouch_ac: pyairtouch.AirConditioner,
    ) -> None:
        super().__init__(
            ac_device=ac_device,
            airtouch=airtouch,
            airtouch_ac=airtouch_ac,
            id_suffix="_temperature",
        )

    @property
    def native_value(self) -> float:  # pyright: ignore[reportIncompatibleVariableOverride]
        return self._airtouch_ac.current_temperature


class AcActiveFanSpeedEntity(entities.AirTouchAcEntity, sensor.SensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Sensor reporting the active fan speed of an air-conditioner."""

    _attr_name = "Active Fan Speed"
    _attr_device_class = sensor.SensorDeviceClass.ENUM
    _attr_translation_key = "ac_active_fan_speed"

    def __init__(
        self,
        ac_device: devices.AcDevice,
        airtouch: pyairtouch.AirTouch,
        airtouch_ac: pyairtouch.AirConditioner,
    ) -> None:
        super().__init__(
            ac_device=ac_device,
            airtouch=airtouch,
            airtouch_ac=airtouch_ac,
            id_suffix="_active_fan_speed",
        )
        self._attr_options = list(climate.AC_TO_CLIMATE_FAN_MODE.values())

    @property
    def native_value(self) -> str | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        if self._airtouch_ac.active_fan_speed:
            return climate.AC_TO_CLIMATE_FAN_MODE[self._airtouch_ac.active_fan_speed]
        return None


_NO_ERROR = "none"


class AcErrorEntity(entities.AirTouchAcEntity, sensor.SensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Sensor reporting the error state of an air-conditioner."""

    _attr_name = "Error Code"
    _attr_device_class = None  # No appropriate device classes are available.
    _attr_translation_key = "ac_error_code"

    def __init__(
        self,
        ac_device: devices.AcDevice,
        airtouch: pyairtouch.AirTouch,
        airtouch_ac: pyairtouch.AirConditioner,
    ) -> None:
        super().__init__(
            ac_device=ac_device,
            airtouch=airtouch,
            airtouch_ac=airtouch_ac,
            id_suffix="_error",
        )

    @property
    def native_value(self) -> int | str:  # pyright: ignore[reportIncompatibleVariableOverride]
        error_info = self._airtouch_ac.error_info
        if error_info:
            return error_info.code
        return _NO_ERROR

    @property
    def extra_state_attributes(self) -> dict[str, Any]:  # pyright: ignore[reportIncompatibleVariableOverride]
        error_description: str | None = _NO_ERROR
        error_info = self._airtouch_ac.error_info
        if error_info:
            error_description = error_info.description

        return {"error_description": error_description}


class ZoneTemperatureEntity(entities.AirTouchZoneEntity, sensor.SensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Sensor reporting the current temperature of a zone."""

    _attr_name = "Temperature"
    _attr_device_class = sensor.SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = sensor.SensorStateClass.MEASUREMENT

    def __init__(
        self,
        zone_device: devices.ZoneDevice,
        airtouch: pyairtouch.AirTouch,
        airtouch_zone: pyairtouch.Zone,
    ) -> None:
        super().__init__(
            zone_device=zone_device,
            airtouch=airtouch,
            airtouch_zone=airtouch_zone,
            id_suffix="_temperature",
        )

    @property
    def native_value(self) -> float | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        return self._airtouch_zone.current_temperature


class ZonePercentageEntity(entities.AirTouchZoneEntity, sensor.SensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Sensor reporting the current open percentage of a zone's damper."""

    _attr_name = "Damper Open Percentage"
    _attr_device_class = None  # No appropriate device classes are available
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = sensor.SensorStateClass.MEASUREMENT

    def __init__(
        self,
        zone_device: devices.ZoneDevice,
        airtouch: pyairtouch.AirTouch,
        airtouch_zone: pyairtouch.Zone,
    ) -> None:
        super().__init__(
            zone_device=zone_device,
            airtouch=airtouch,
            airtouch_zone=airtouch_zone,
            id_suffix="_open_percentage",
        )

    @property
    def native_value(self) -> int:  # pyright: ignore[reportIncompatibleVariableOverride]
        if self._airtouch_zone.power_state == pyairtouch.ZonePowerState.OFF:
            # Force the value to zero when the zone is turned off to have a more
            # accurate record of zone damper percentage history.
            return 0
        return self._airtouch_zone.current_damper_percentage


class SpillBypassPercentageEntity(entities.AirTouchAcEntity, sensor.SensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Sensor reporting the current spill/bypass percentage for an AC.

    The value may be greater than 100% for ACs with multiple spill zones.
    """

    _attr_device_class = None  # No appropriate device classes are available
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = sensor.SensorStateClass.MEASUREMENT

    def __init__(
        self,
        spill_bypass: SpillBypass,
        ac_device: devices.AcDevice,
        airtouch: pyairtouch.AirTouch,
        airtouch_ac: pyairtouch.AirConditioner,
        spill_zone_count: int,
    ) -> None:
        super().__init__(
            ac_device=ac_device,
            airtouch=airtouch,
            airtouch_ac=airtouch_ac,
            id_suffix=(
                "_bypass_percentage"
                if spill_bypass == SpillBypass.BYPASS
                else "_spill_percentage"
            ),
            include_zone_subscription=True,
        )

        self._attr_name = (
            "Bypass Percentage"
            if spill_bypass == SpillBypass.BYPASS
            else "Spill Percentage"
        )

        # The AirTouch algorithm will always ensure that the sum of zone opening
        # percentages remains a minimum opening percentage of:
        #    spill_zone_count * 100
        self._spill_percentage_limit = spill_zone_count * 100

    @property
    def native_value(self) -> int:  # pyright: ignore[reportIncompatibleVariableOverride]
        if self._airtouch_ac.power_state in [
            pyairtouch.AcPowerState.OFF,
            pyairtouch.AcPowerState.OFF_AWAY,
            pyairtouch.AcPowerState.OFF_FORCED,
        ]:
            return 0

        zone_percentage_sum = sum(
            [
                z.current_damper_percentage
                for z in self._airtouch_ac.zones
                if z.power_state != pyairtouch.ZonePowerState.OFF
            ]
        )
        # The spill percentage can never be less than zero
        return max(0, self._spill_percentage_limit - zone_percentage_sum)


class _UsageEntity(CoordinatorEntity[UsageCoordinator], sensor.SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Run time today"
    _attr_device_class = sensor.SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = sensor.SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        coordinator: UsageCoordinator,
        unique_id: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{unique_id}_run_time_today"
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        now = _usage_now(self.hass)
        data = self.coordinator.data
        return now is not None and data is not None and data.local_date == now.date()

    def _snapshot_now(self) -> tuple[UsageSnapshot, datetime] | None:
        if not self.available:
            return None
        now = _usage_now(self.hass)
        data = self.coordinator.data
        if now is None or data is None:
            return None
        return data, now


class AcRunTimeEntity(_UsageEntity):
    """Today's air-conditioner run time, with mode and history attributes."""

    _attr_translation_key = "ac_run_time_today"

    def __init__(
        self,
        coordinator: UsageCoordinator,
        unique_id: str,
        device_info: DeviceInfo,
        ac_id: int,
    ) -> None:
        super().__init__(coordinator, unique_id, device_info)
        self._ac_id = ac_id

    @property
    def native_value(self) -> int | None:
        current = self._snapshot_now()
        if current is None:
            return None
        data, now = current
        return data.ac_today_minutes(self._ac_id, now=now)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        current = self._snapshot_now()
        if current is None:
            return None
        data, now = current
        return data.ac_attributes(self._ac_id, now=now)


class ZoneRunTimeEntity(_UsageEntity):
    """Today's zone run time, with daily and hourly history attributes."""

    _attr_translation_key = "zone_run_time_today"

    def __init__(
        self,
        coordinator: UsageCoordinator,
        unique_id: str,
        device_info: DeviceInfo,
        zone_id: int,
    ) -> None:
        super().__init__(coordinator, unique_id, device_info)
        self._zone_id = zone_id

    @property
    def native_value(self) -> int | None:
        current = self._snapshot_now()
        if current is None:
            return None
        data, now = current
        return data.zone_today_minutes(self._zone_id, now=now)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        current = self._snapshot_now()
        if current is None:
            return None
        data, now = current
        return data.zone_attributes(self._zone_id, now=now)


class ProgramsEntity(CoordinatorEntity[ProgramCoordinator], sensor.SensorEntity):
    """The number of saved programs, with their configuration as attributes."""

    _attr_has_entity_name = True
    _attr_translation_key = "programs"
    # Every program, zone and Beam setting can exceed the recorder's size limit.
    _unrecorded_attributes = frozenset({"programs", "reported_beam_ids"})

    def __init__(
        self,
        coordinator: ProgramCoordinator,
        unique_id: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{unique_id}_programs"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        return None if data is None else len(data.programs)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        data = self.coordinator.data
        return None if data is None else data.attributes()


class GeofenceFeature(enum.IntFlag):  # noqa: D101
    POST_PRESENCE = 1


class GeofenceEntity(CoordinatorEntity[GeofenceCoordinator], sensor.SensorEntity):
    """How many native geofence accounts are inside, with settings and accounts.

    The sensor stays available while its owner is open. After a failed read it
    shows unknown, and an action with an explicit email can still post.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "geofence"
    _attr_supported_features = GeofenceFeature.POST_PRESENCE
    _unrecorded_attributes = frozenset({"accounts"})

    def __init__(
        self,
        coordinator: GeofenceCoordinator,
        unique_id: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{unique_id}_geofence"
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        return not self.coordinator.closed

    @property
    def native_value(self) -> int | None:
        data = self._snapshot()
        return None if data is None else data.inside_count

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        data = self._snapshot()
        return None if data is None else data.attributes()

    async def async_post_geofence_presence(
        self, transition: str, identity: SafeText, email: SafeText | None = None
    ) -> ServiceResponse:
        try:
            accepted = await self.coordinator.async_post_presence(
                Transition[transition.upper()], identity, email
            )
        except (GeofenceEventError, GeofenceUncertainError) as err:
            raise HomeAssistantError(str(err)) from err
        return {"commands_queued": accepted.commands_queued}

    def _snapshot(self) -> GeofenceSnapshot | None:
        return self.coordinator.data if self.coordinator.last_update_success else None


def _usage_now(hass: HomeAssistant | None) -> datetime | None:
    if hass is None:
        return None
    try:
        return local_now(hass)
    except (KeyError, ValueError):
        return None
