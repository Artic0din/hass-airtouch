from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Callable

import pyairtouch
from homeassistant import config_entries, loader
from homeassistant.components import climate
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import CoreState, HomeAssistant, State
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotSupported,
    ServiceValidationError,
)
from homeassistant.helpers import (
    area_registry,
    category_registry,
    device_registry,
    entity_registry,
    floor_registry,
    issue_registry,
    label_registry,
)
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pyairtouch import AirTouchModel

from custom_components.airtouch import async_setup_entry as async_setup_integration
from custom_components.airtouch import beam as beam_module
from custom_components.airtouch.beam import (
    BeamControl,
    BeamCoordinator,
    BeamRefusedError,
    async_release_beam,
    get_beam,
)
from custom_components.airtouch.const import (
    CONF_AIRTOUCH_MODEL,
    DOMAIN,
    OPTIONS_MIN_TARGET_TEMPERATURE_STEP,
)
from custom_components.airtouch.const import AirTouchModel as SelectedModel
from tests.test_airtouch_beam import (
    Console,
    control_payload,
    report,
    status,
    timer_payload,
)

_ID_A = b"BEAM-A"
_ID_B = b"BEAM-B"
_READ = bytes.fromhex("ff21")
_CLIENT_MODULES = (
    "ac_settings",
    "beam",
    "geofence",
    "io_outputs",
    "preferences",
    "programs",
    "smart_settings",
    "usage",
)


class _DuctedAc:
    ac_id = 0
    name = "Ducted"
    target_temperature_resolution = 1.0
    supported_modes = (pyairtouch.AcMode.COOL,)
    supported_fan_speeds = (pyairtouch.AcFanSpeed.LOW,)
    supported_power_controls = ()
    zones = ()
    current_temperature = 23.0
    target_temperature = 21.0
    min_target_temperature = 16.0
    max_target_temperature = 30.0
    selected_fan_speed = pyairtouch.AcFanSpeed.LOW
    power_state = pyairtouch.AcPowerState.ON
    selected_mode = pyairtouch.AcMode.COOL
    active_mode = pyairtouch.AcMode.COOL

    def __init__(self) -> None:
        self.targets: list[float] = []

    def subscribe_ac_state(self, callback: object) -> None:
        del callback

    def unsubscribe_ac_state(self, callback: object) -> None:
        del callback

    def next_quick_timer(self, timer_type: object) -> None:
        del timer_type

    async def set_target_temperature(self, value: float) -> None:
        self.targets.append(value)


class _Api:
    def __init__(self, host: str, model: AirTouchModel) -> None:
        self.host = host
        self.model = model
        self.airtouch_id = host
        self.name = "Console"
        self.connected = True
        self.air_conditioners = [_DuctedAc()]
        self.shutdown_calls = 0

    async def init(self) -> bool:
        return True

    async def shutdown(self) -> None:
        self.shutdown_calls += 1

    def subscribe(self, callback: object) -> None:
        del callback

    def unsubscribe(self, callback: object) -> None:
        del callback


class _BeamHassCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.console = Console()
        await self.console.start()
        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        hass = HomeAssistant(config_dir.name)
        hass.config.skip_pip = True
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        loader.async_setup(hass)
        await asyncio.gather(
            area_registry.async_load(hass),
            category_registry.async_load(hass),
            device_registry.async_load(hass),
            entity_registry.async_load(hass),
            floor_registry.async_load(hass),
            issue_registry.async_load(hass),
            label_registry.async_load(hass),
        )
        hass.set_state(CoreState.running)
        self.hass = hass
        self.apis: dict[str, _Api] = {}
        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(
            patch(
                "custom_components.airtouch.PLATFORMS",
                [Platform.CLIMATE, Platform.TIME],
            )
        )
        patches.enter_context(
            patch("custom_components.airtouch.pyairtouch.connect", self._connect)
        )
        for module in _CLIENT_MODULES:
            patches.enter_context(
                patch(
                    f"custom_components.airtouch.{module}.ConsoleClient",
                    self.console.client,
                )
            )

    async def asyncTearDown(self) -> None:
        self.console.release.set()
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.state is config_entries.ConfigEntryState.LOADED:
                await self.hass.config_entries.async_unload(entry.entry_id)
        await self.hass.async_stop(force=True)
        await self.console.stop()

    def _connect(self, *, model: AirTouchModel, host: str) -> _Api:
        api = _Api(host, model)
        self.apis[host] = api
        return api

    async def _add(
        self,
        host: str = "console",
        model: SelectedModel = SelectedModel.AIRTOUCH_5,
        options: dict[str, Any] | None = None,
    ) -> config_entries.ConfigEntry:
        entry = config_entries.ConfigEntry(
            data={CONF_HOST: host, CONF_AIRTOUCH_MODEL: model.value},
            discovery_keys=MappingProxyType({}),
            domain=DOMAIN,
            minor_version=2,
            options=options or {},
            source=config_entries.SOURCE_USER,
            subentries_data=None,
            title=host,
            unique_id=host,
            version=2,
        )
        await self.hass.config_entries.async_add(entry)
        await self.hass.async_block_till_done()
        return entry

    def _owner(self, entry: config_entries.ConfigEntry) -> BeamCoordinator:
        return get_beam(self.hass, entry.entry_id)

    def _entity_id(self, domain: str, unique_id: str) -> str | None:
        return entity_registry.async_get(self.hass).async_get_entity_id(
            domain, DOMAIN, unique_id
        )

    def _beam_ids(self, beam_id: bytes = _ID_A, host: str = "console") -> list[str]:
        prefix = f"{host}_beam_{beam_id.hex()}"
        found = [
            self._entity_id("climate", prefix),
            self._entity_id("time", f"{prefix}_on_timer"),
            self._entity_id("time", f"{prefix}_off_timer"),
        ]
        if None in found:
            raise AssertionError(f"missing Beam entities: {found}")
        return [entity_id for entity_id in found if entity_id is not None]

    def _state(self, entity_id: str) -> State:
        state: State | None = self.hass.states.get(entity_id)
        if state is None:
            raise AssertionError(f"{entity_id} has no state")
        return state

    async def _refresh(self, entry: config_entries.ConfigEntry) -> None:
        await self._owner(entry).async_refresh()
        await self.hass.async_block_till_done()

    async def _call(
        self, domain: str, service: str, entity_id: str, **data: object
    ) -> None:
        await self.hass.services.async_call(
            domain, service, data, target={"entity_id": entity_id}, blocking=True
        )


class BeamClimateServiceTests(_BeamHassCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.console.records = [status(cool=(16, 31), heat=(18, 26), mode=4)]
        self.entry = await self._add()
        self.climate, self.on_timer, self.off_timer = self._beam_ids()
        self.console.clear()

    async def test_celsius_state_controls_and_registered_services(self) -> None:
        state = self._state(self.climate)
        self.assertEqual(state.state, "cool")
        self.assertEqual(
            state.attributes["hvac_modes"],
            ["off", "heat_cool", "heat", "dry", "fan_only", "cool"],
        )
        self.assertEqual(
            state.attributes["fan_modes"],
            ["auto", "quiet", "low", "medium", "high", "powerful", "turbo"],
        )
        self.assertEqual(state.attributes["fan_mode"], "low")
        self.assertEqual(
            (state.attributes["min_temp"], state.attributes["max_temp"]), (16, 31)
        )
        self.assertEqual(state.attributes["target_temp_step"], 1)
        self.assertEqual(state.attributes["temperature"], 21)
        self.assertIsNone(state.attributes["current_temperature"])
        self.assertEqual(state.attributes["error_code"], 0)
        self.assertNotIn("reported_fan_mode", state.attributes)
        self.assertNotIn("hvac_action", state.attributes)
        device = device_registry.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, f"console_beam_{_ID_A.hex()}")}
        )
        self.assertIsNotNone(device)
        assert device is not None
        self.assertEqual(device.name, "Lounge")
        self.assertIsNotNone(device.via_device_id)

        await self._call("climate", "set_temperature", self.climate, temperature=31)
        await self._call("climate", "set_temperature", self.climate, temperature=16.4)
        await self._call("climate", "set_temperature", self.climate, temperature=20.5)
        await self._call("climate", "turn_on", self.climate)
        await self._call("climate", "turn_off", self.climate)
        await self._call("climate", "set_hvac_mode", self.climate, hvac_mode="off")
        await self._call("climate", "set_hvac_mode", self.climate, hvac_mode="heat")
        await self._call("climate", "set_fan_mode", self.climate, fan_mode="medium")
        await self._call(
            "climate",
            "set_temperature",
            self.climate,
            temperature=26,
            hvac_mode="heat",
        )
        await self._call(
            DOMAIN, "set_hvac_mode_only", self.climate, hvac_mode="heat_cool"
        )
        self.assertEqual(
            self.console.writes(),
            [
                control_payload(_ID_A, 0, 15, 15, 210),
                control_payload(_ID_A, 0, 15, 15, 60),
                control_payload(_ID_A, 0, 15, 15, 110),
                control_payload(_ID_A, 3, 15, 15, 255),
                control_payload(_ID_A, 2, 15, 15, 255),
                control_payload(_ID_A, 2, 15, 15, 255),
                control_payload(_ID_A, 3, 1, 15, 255),
                control_payload(_ID_A, 0, 15, 3, 255),
                control_payload(_ID_A, 3, 1, 15, 160),
                control_payload(_ID_A, 0, 0, 15, 255),
            ],
        )
        self.assertEqual(self.console.reads(), 10)

    async def test_home_assistant_and_owner_refuse_without_writing(self) -> None:
        for value in (32, 15):
            with self.subTest(value=value), self.assertRaises(ServiceValidationError):
                await self._call(
                    "climate", "set_temperature", self.climate, temperature=value
                )
        self.assertEqual(self.console.frames, [])
        with self.assertRaisesRegex(HomeAssistantError, "finite"):
            await self._call(
                "climate", "set_temperature", self.climate, temperature=float("nan")
            )
        with self.assertRaisesRegex(ValueError, "Unsupported HVAC Mode"):
            await self._call(
                DOMAIN, "set_hvac_mode_only", self.climate, hvac_mode="off"
            )
        self.assertEqual(self.console.frames, [])
        with self.assertRaises(BeamRefusedError):
            await self._call(
                "climate",
                "set_temperature",
                self.climate,
                temperature=31,
                hvac_mode="heat",
            )
        self.console.records = [status(modes=0x10, fans=0x10, cool=(16, 31))]
        for service, data in (
            ("set_hvac_mode", {"hvac_mode": "heat"}),
            ("set_fan_mode", {"fan_mode": "low"}),
        ):
            with self.subTest(service=service), self.assertRaises(HomeAssistantError):
                await self._call("climate", service, self.climate, **data)
        self.assertEqual(self.console.writes(), [])
        state = self._state(self.climate)
        self.assertEqual(state.attributes["hvac_modes"], ["off", "cool"])
        self.assertEqual(state.attributes["fan_modes"], ["high"])

    async def test_fahrenheit_presentation_and_ordinary_inputs(self) -> None:
        self.hass.config.units = US_CUSTOMARY_SYSTEM
        await self._refresh(self.entry)
        state = self._state(self.climate)
        self.assertEqual(
            (state.attributes["min_temp"], state.attributes["max_temp"]), (60.8, 87.8)
        )
        self.assertEqual(state.attributes["target_temp_step"], 1.8)
        self.assertEqual(state.attributes["temperature"], 69.8)
        self.console.clear()
        for shown in (60.8, 87.8, 69.8, 69.8 + 1.8, 70, 72, 73):
            await self._call(
                "climate", "set_temperature", self.climate, temperature=shown
            )
        self.assertEqual(
            [frame[-3] for frame in self.console.writes()],
            [60, 210, 110, 120, 110, 120, 130],
        )
        self.console.clear()
        for shown in (88, 60.7):
            with self.subTest(shown=shown), self.assertRaises(ServiceValidationError):
                await self._call(
                    "climate", "set_temperature", self.climate, temperature=shown
                )
        self.assertEqual(self.console.frames, [])
        self.hass.config.units = METRIC_SYSTEM

    async def test_display_limits_and_steps_for_every_whole_celsius(self) -> None:
        for units in (METRIC_SYSTEM, US_CUSTOMARY_SYSTEM):
            fahrenheit = units is US_CUSTOMARY_SYSTEM
            self.hass.config.units = units
            for low in range(16, 32):
                for limits in ((low, low), (16, 31)):
                    self.console.records = [status(cool=limits, setpoint=0)]
                    await self._refresh(self.entry)
                    state = self._state(self.climate)
                    shown = (state.attributes["min_temp"], state.attributes["max_temp"])
                    self.assertEqual(
                        shown,
                        tuple(
                            round(c * 1.8 + 32, 1) if fahrenheit else c for c in limits
                        ),
                    )
                    self.console.clear()
                    for value in shown:
                        await self._call(
                            "climate",
                            "set_temperature",
                            self.climate,
                            temperature=value,
                        )
                    self.assertEqual(
                        [frame[-3] for frame in self.console.writes()],
                        [(limits[0] - 10) * 10, (limits[1] - 10) * 10],
                    )
        self.hass.config.units = METRIC_SYSTEM

    async def test_no_usable_range_omits_target_temperature(self) -> None:
        self.console.records = [status(cool=(0, 0xFF), setpoint=120)]
        await self._refresh(self.entry)
        state = self._state(self.climate)
        self.assertEqual(
            (state.attributes["min_temp"], state.attributes["max_temp"]), (22, 22)
        )
        self.assertNotIn("temperature", state.attributes)
        self.assertFalse(
            state.attributes["supported_features"]
            & climate.ClimateEntityFeature.TARGET_TEMPERATURE
        )
        self.console.clear()
        with self.assertRaises(ServiceNotSupported):
            await self._call("climate", "set_temperature", self.climate, temperature=22)
        self.assertEqual(self.console.frames, [])

    async def test_fan_mask_zero_unknown_modes_errors_and_offline(self) -> None:
        self.console.records = [status(fans=0, fan=3, mode=9, error=0x0102)]
        await self._refresh(self.entry)
        state = self._state(self.climate)
        self.assertEqual(state.state, "heat_cool")
        self.assertNotIn("fan_mode", state.attributes)
        self.assertNotIn("fan_modes", state.attributes)
        self.assertEqual(state.attributes["reported_fan_mode"], "medium")
        self.assertEqual(state.attributes["error_code"], 0x0102)
        self.console.records = [status(mode=0xFF, power=1)]
        await self._refresh(self.entry)
        self.assertEqual(self._state(self.climate).state, "unknown")
        self.console.records = [status(power=0, mode=8)]
        await self._refresh(self.entry)
        self.assertEqual(self._state(self.climate).state, "off")
        self.console.records = [status(error=0xFFFE)]
        await self._refresh(self.entry)
        for entity_id in (self.climate, self.on_timer, self.off_timer):
            self.assertEqual(self._state(entity_id).state, "unavailable")

    async def test_step_option_updates_existing_and_later_entities(self) -> None:
        self.hass.config_entries.async_update_entry(
            self.entry, options={OPTIONS_MIN_TARGET_TEMPERATURE_STEP: 2.0}
        )
        await self.hass.async_block_till_done()
        self.assertEqual(self._state(self.climate).attributes["target_temp_step"], 2)
        self.console.records = [status(), status(_ID_B, name=b"Den")]
        await self._refresh(self.entry)
        later = self._beam_ids(_ID_B)[0]
        self.assertEqual(self._state(later).attributes["target_temp_step"], 2)
        self.hass.config.units = US_CUSTOMARY_SYSTEM
        await self._refresh(self.entry)
        self.assertEqual(self._state(later).attributes["target_temp_step"], 3.6)
        self.hass.config.units = METRIC_SYSTEM
        self.hass.config_entries.async_update_entry(
            self.entry, options={OPTIONS_MIN_TARGET_TEMPERATURE_STEP: 0.1}
        )
        await self.hass.async_block_till_done()
        for entity_id in (self.climate, later):
            self.assertEqual(self._state(entity_id).attributes["target_temp_step"], 1)

    async def test_ducted_climate_is_unchanged(self) -> None:
        ducted = self._entity_id("climate", "console_ac0")
        self.assertIsNotNone(ducted)
        assert ducted is not None
        state = self._state(ducted)
        self.assertEqual(state.attributes["target_temp_step"], 1)
        self.assertEqual(state.attributes["min_temp"], 16)
        await self._call("climate", "set_temperature", ducted, temperature=20.6)
        self.assertEqual(self.apis["console"].air_conditioners[0].targets, [20.6])
        self.assertEqual(self.console.frames, [])


class BeamTimerServiceTests(_BeamHassCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.console.records = [
            status(on_timer=bytes.fromhex("8a1e"), off_timer=bytes.fromhex("0615"))
        ]
        self.entry = await self._add()
        _, self.on_timer, self.off_timer = self._beam_ids()
        self.console.clear()

    async def test_timer_values_and_existing_time_services(self) -> None:
        self.assertEqual(self._state(self.on_timer).state, "unknown")
        self.assertEqual(self._state(self.off_timer).state, "06:21:00")
        await self._call("time", "set_value", self.on_timer, time="06:30:00")
        await self._call(DOMAIN, "clear_timer", self.off_timer)
        await self._call(
            DOMAIN, "set_timer_from_delay", self.on_timer, delay="00:01:00"
        )
        await self._call(
            DOMAIN, "set_timer_from_delay", self.off_timer, delay={"minutes": 1439}
        )
        self.assertEqual(
            self.console.writes(),
            [
                timer_payload(_ID_A, 2, bytes.fromhex("061e0615")),
                timer_payload(_ID_A, 2, bytes.fromhex("8a1e803f")),
                timer_payload(_ID_A, 1, bytes.fromhex("01000100")),
                timer_payload(_ID_A, 1, bytes.fromhex("00173b00")),
            ],
        )

    async def test_invalid_times_and_delays_send_nothing(self) -> None:
        cases: list[tuple[str, str, dict[str, object]]] = [
            ("time", "set_value", {"time": "06:30:15"}),
            (DOMAIN, "set_timer_from_delay", {"delay": "00:00:00"}),
            (DOMAIN, "set_timer_from_delay", {"delay": "00:01:30"}),
            (DOMAIN, "set_timer_from_delay", {"delay": "24:00:00"}),
            (DOMAIN, "set_timer_from_delay", {"delay": {"days": 2}}),
        ]
        for domain, service, data in cases:
            with self.subTest(data=data), self.assertRaises(BeamRefusedError):
                await self._call(domain, service, self.on_timer, **data)
        self.assertEqual(self.console.frames, [])


class BeamDiscoveryTests(_BeamHassCase):
    async def test_zero_then_failed_reads_keep_polling_until_units_appear(
        self,
    ) -> None:
        self.console.records = []
        with patch.object(beam_module, "_UPDATE_INTERVAL", timedelta(seconds=1)):
            entry = await self._add()
        self.assertIsNone(self._entity_id("climate", f"console_beam_{_ID_A.hex()}"))
        self.assertEqual(self._state("climate.ducted").state, "cool")
        self.console.read_mode = "close"
        reads = self.console.reads()
        await self._until(lambda: self.console.reads() > reads)
        self.assertFalse(self._owner(entry).last_update_success)
        self.assertEqual(self._state("climate.ducted").state, "cool")
        self.console.read_mode = "ok"
        self.console.records = [status()]
        await self._until(
            lambda: (
                self._entity_id("climate", f"console_beam_{_ID_A.hex()}") is not None
            )
        )
        await self.hass.async_block_till_done()
        climate_id = self._beam_ids()[0]
        self.assertEqual(self._state(climate_id).state, "cool")

    async def _until(self, condition: Callable[[], bool]) -> None:
        for _ in range(400):
            if condition():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition was not reached")

    async def test_disappearance_and_return_keep_registry_entries(self) -> None:
        self.console.records = [status(), status(_ID_B, name=b"Den")]
        entry = await self._add()
        first = self._beam_ids()
        second = self._beam_ids(_ID_B)
        registry = entity_registry.async_get(self.hass)
        registry.async_update_entity(first[0], name="Custom")
        self.console.records = [status(_ID_B, name=b"Den")]
        await self._refresh(entry)
        for entity_id in first:
            self.assertEqual(self._state(entity_id).state, "unavailable")
            self.assertIsNotNone(registry.async_get(entity_id))
        self.assertEqual(self._state(second[0]).state, "cool")
        self.console.records = [status(name=b"Renamed"), status(_ID_B, name=b"Den")]
        await self._refresh(entry)
        self.assertEqual(self._beam_ids(), first)
        self.assertEqual(self._state(first[0]).attributes["friendly_name"], "Custom")
        self.assertEqual(self._state(first[0]).state, "cool")
        self.assertEqual(
            len(
                [
                    item
                    for item in registry.entities.values()
                    if item.platform == DOMAIN and "_beam_" in item.unique_id
                ]
            ),
            6,
        )

    async def test_padded_identity_is_one_stable_unit(self) -> None:
        padded = (b"BEAM-A\x00junk").ljust(64, b"\x00")
        self.console.records = [status(id_slot=padded)]
        entry = await self._add()
        first = self._beam_ids()
        self.console.records = [status(_ID_A)]
        await self._refresh(entry)
        self.assertEqual(self._beam_ids(), first)
        self.console.body = report(status(_ID_A), status(id_slot=padded))
        await self._refresh(entry)
        self.assertFalse(self._owner(entry).last_update_success)
        for entity_id in first:
            self.assertEqual(self._state(entity_id).state, "unavailable")

    async def test_failed_unload_keeps_owner_and_adds_no_new_entities(self) -> None:
        self.console.records = [status()]
        entry = await self._add()
        owner = self._owner(entry)
        self.addAsyncCleanup(async_release_beam, self.hass, entry.entry_id)
        climate_id, on_timer, _ = self._beam_ids()

        async def failed_unload(hass: HomeAssistant, entry: object) -> bool:
            del hass, entry
            return False

        with patch("homeassistant.components.time.async_unload_entry", failed_unload):
            self.assertFalse(
                await self.hass.config_entries.async_unload(entry.entry_id)
            )
        self.assertIs(entry.state, config_entries.ConfigEntryState.FAILED_UNLOAD)
        self.assertIs(self._owner(entry), owner)
        self.assertFalse(owner.closed)
        self.assertIs(self.hass.data[DOMAIN][entry.entry_id], self.apis["console"])
        self.assertEqual(self.apis["console"].shutdown_calls, 0)
        removed = self._state(climate_id)
        self.assertEqual(removed.state, "unavailable")
        self.assertIs(removed.attributes.get("restored"), True)  # noqa: FBT003
        self.console.records = [
            status(on_timer=bytes.fromhex("0102")),
            status(_ID_B, name=b"Den"),
        ]
        await self._refresh(entry)
        self.assertEqual(self._state(on_timer).state, "01:02:00")
        self.assertIsNone(
            self._entity_id("time", f"console_beam_{_ID_B.hex()}_on_timer")
        )
        self.assertIsNone(self._entity_id("climate", f"console_beam_{_ID_B.hex()}"))

    async def test_unload_releases_and_reload_reuses_entities(self) -> None:
        self.console.records = [status()]
        entry = await self._add()
        owner = self._owner(entry)
        first = self._beam_ids()
        self.assertTrue(await self.hass.config_entries.async_unload(entry.entry_id))
        self.assertTrue(owner.closed)
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            self._owner(entry)
        self.assertNotIn(entry.entry_id, self.hass.data[DOMAIN])
        self.assertEqual(self.apis["console"].shutdown_calls, 1)
        self.console.clear()
        await owner.async_refresh()
        with self.assertRaisesRegex(BeamRefusedError, "closed"):
            await owner.async_write(
                owner.data.units[0].beam_id, BeamControl(power=True)
            )
        self.assertEqual(self.console.frames, [])
        self.assertTrue(await self.hass.config_entries.async_setup(entry.entry_id))
        await self.hass.async_block_till_done()
        reloaded = self._owner(entry)
        self.assertIsNot(reloaded, owner)
        self.assertEqual(self._beam_ids(), first)
        self.assertEqual(self._state(first[0]).state, "cool")

    async def test_airtouch_4_gets_no_beam_owner_or_entities(self) -> None:
        entry = await self._add("four", SelectedModel.AIRTOUCH_4)
        self.assertIs(entry.state, config_entries.ConfigEntryState.LOADED)
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            self._owner(entry)
        self.assertEqual(self.console.frames, [])
        self.assertIsNotNone(self._entity_id("climate", "four_ac0"))

    async def test_setup_failure_and_cancellation_release_the_owner(self) -> None:
        async def failed_forward(entry: object, platforms: object) -> None:
            del entry, platforms
            raise RuntimeError("forward failed")

        with patch.object(
            self.hass.config_entries, "async_forward_entry_setups", failed_forward
        ):
            entry = await self._add()
        self.assertIs(entry.state, config_entries.ConfigEntryState.SETUP_ERROR)
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            self._owner(entry)

        async def forwarded(entry: object, platforms: object) -> None:
            del entry, platforms

        held = config_entries.ConfigEntry(
            data={
                CONF_HOST: "held",
                CONF_AIRTOUCH_MODEL: SelectedModel.AIRTOUCH_5.value,
            },
            discovery_keys=MappingProxyType({}),
            domain=DOMAIN,
            minor_version=2,
            options={},
            source=config_entries.SOURCE_USER,
            subentries_data=None,
            title="held",
            unique_id="held",
            version=2,
        )
        self.console.read_mode = "hold"
        self.console.clear()
        with patch.object(
            self.hass.config_entries, "async_forward_entry_setups", forwarded
        ):
            setup = asyncio.create_task(async_setup_integration(self.hass, held))
            await self._until_read()
            owner = self._owner(held)
            setup.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await setup
        self.assertTrue(owner.closed)
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            self._owner(held)
        self.assertIsNone(owner.data)
        self.assertEqual(self.console.frames, [_READ])

    async def _until_read(self) -> None:
        await asyncio.wait_for(self.console.read_started.wait(), 2)


if __name__ == "__main__":
    unittest.main()
