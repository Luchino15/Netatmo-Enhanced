"""Offline climate controls, native action validation, and expiry presentation."""

from datetime import UTC, datetime
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, call

from push_harness import HA_AVAILABLE, MODULE
from test_push_integration import entity_platforms

if HA_AVAILABLE:
    from probatio import Invalid

    from homeassistant import config_entries
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import DATA_DOMAIN_PLATFORM_ENTITIES

    from custom_components.netatmo_enhanced import async_setup
    from custom_components.netatmo_enhanced.const import DOMAIN


class ClimateControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        climate, sensor, _select = entity_platforms()
        self.state = {
            "season": "heating", "mode": "home", "room_name": "Synthetic thermostat",
            "reachable": True,
        }
        self.coordinator = SimpleNamespace(data={MODULE: self.state}, last_update_success=True,
                                           async_command=AsyncMock())
        self.climate = climate.EnhancedClimate(self.coordinator, MODULE)
        self.expiry = sensor.OverrideExpirySensor(self.coordinator, MODULE)
        self.error = climate.HomeAssistantError

    async def test_boost_preset_and_commands_keep_thermal_state_authoritative(self):
        for season in ("heating", "cooling"):
            with self.subTest(season=season):
                self.state["season"] = season
                self.assertIn("boost", self.climate.preset_modes)
                self.assertEqual(self.climate.preset_mode, "schedule")
                self.coordinator.async_command.reset_mock()
                await self.climate.async_set_preset_mode("boost")
                await self.climate.async_set_boost(duration=30)
                await self.climate.async_set_boost(duration=60)
                await self.climate.async_cycle_boost()
                await self.climate.async_stop_boost()
                self.assertEqual(self.coordinator.async_command.await_args_list, [
                    call(MODULE, "boost", 90), call(MODULE, "boost", 30), call(MODULE, "boost", 60),
                    call(MODULE, "cycle_boost", None), call(MODULE, "stop_boost", None),
                ])
                self.assertEqual(self.climate.preset_mode, "schedule")
                self.assertIsNone(self.expiry.native_value)

    async def test_standard_and_timed_manual_temperature_use_distinct_commands(self):
        await self.climate.async_set_temperature(temperature=21.5)
        await self.climate.async_set_manual_temperature(temperature=19.5, duration=45)
        self.assertEqual(self.coordinator.async_command.await_args_list, [
            call(MODULE, "temperature", 21.5), call(MODULE, "temperature_duration", (19.5, 45)),
        ])

    async def test_native_auto_and_off_expose_only_supported_seasons(self):
        self.assertEqual(self.climate.hvac_modes, ["auto"])
        self.assertIn("frost_guard", self.climate.preset_modes)
        with self.assertRaises(self.error):
            await self.climate.async_set_hvac_mode("off")
        self.coordinator.async_command.assert_not_called()

        self.state["season"] = "cooling"
        self.assertEqual(self.climate.hvac_modes, ["auto", "off"])
        self.assertEqual(self.climate.preset_modes, ["schedule", "boost"])
        await self.climate.async_set_hvac_mode("off")
        await self.climate.async_set_hvac_mode("auto")
        with self.assertRaises(self.error):
            await self.climate.async_set_preset_mode("frost_guard")
        with self.assertRaises(self.error):
            await self.climate.async_set_hvac_mode("heat")
        self.assertEqual(self.coordinator.async_command.await_args_list, [
            call(MODULE, "power_off", None), call(MODULE, "preset", "schedule"),
        ])

    def test_expiry_comes_from_reported_active_mode_and_timestamps(self):
        self.assertEqual(self.expiry._attr_unique_id, f"{MODULE}_override_expires_at")
        self.assertEqual(self.expiry._attr_device_class, "timestamp")
        self.assertFalse(hasattr(self.expiry, "_attr_name"))
        self.assertIsNone(self.expiry.native_value)

        for mode, season, timestamp in (("max", "heating", 2_000_000_000),
                                        ("manual", "cooling", 2_000_006_000)):
            with self.subTest(mode=mode, season=season):
                self.state.update(mode=mode, season=season, setpoint_end_time=timestamp)
                self.assertEqual(self.expiry.native_value, datetime.fromtimestamp(timestamp, UTC))
        self.state.update(mode="max", setpoint_end_time=2_000)
        self.assertEqual(self.expiry.native_value, datetime.fromtimestamp(2_000, UTC))
        self.assertEqual(self.climate.preset_mode, "boost")
        self.state.update(mode="home")
        self.assertIsNone(self.expiry.native_value)

        for timestamp in (None, True, "2000000000", 0, float("inf"), float("nan"), 10**100):
            with self.subTest(timestamp=timestamp):
                self.state.update(mode="manual", setpoint_end_time=timestamp)
                self.assertIsNone(self.expiry.native_value)


@unittest.skipUnless(HA_AVAILABLE, "requires Home Assistant Core")
class NativeEntityServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.hass = HomeAssistant(directory)
        self.addCleanup(self.hass.import_executor.shutdown)
        self.hass.config_entries = config_entries.ConfigEntries(self.hass, {})
        self.assertTrue(await async_setup(self.hass, {}))
        climate, _sensor, _select = entity_platforms()
        self.coordinator = SimpleNamespace(data={MODULE: {"season": "heating"}},
                                           last_update_success=True, async_command=AsyncMock())
        self.entity = climate.EnhancedClimate(self.coordinator, MODULE)
        self.entity.hass = self.hass
        self.entity.entity_id = "climate.synthetic_enhanced"
        self.hass.data[DATA_DOMAIN_PLATFORM_ENTITIES] = {
            ("climate", DOMAIN): {self.entity.entity_id: self.entity},
        }

    async def invoke(self, service, **data):
        await self.hass.services.async_call(DOMAIN, service, data, blocking=True,
                                            target={"entity_id": self.entity.entity_id})

    async def test_registered_actions_accept_native_automation_targets(self):
        await self.invoke("set_boost", duration=90.0)
        await self.invoke("set_boost", duration=30)
        await self.invoke("cycle_boost")
        await self.invoke("stop_boost")
        await self.invoke("set_manual_temperature", temperature=19.5, duration=45.0)
        self.assertEqual(self.coordinator.async_command.await_args_list, [
            call(MODULE, "boost", 90), call(MODULE, "boost", 30), call(MODULE, "cycle_boost", None),
            call(MODULE, "stop_boost", None), call(MODULE, "temperature_duration", (19.5, 45)),
        ])

    async def test_invalid_duration_or_temperature_never_reaches_coordinator(self):
        for service, data in (
            ("set_boost", {}), ("set_boost", {"duration": 35}), ("set_boost", {"duration": "30"}),
            ("set_manual_temperature", {"temperature": 19.5, "duration": True}),
            ("set_manual_temperature", {"temperature": 19.5, "duration": 0}),
            ("set_manual_temperature", {"temperature": 19.5, "duration": 721}),
            ("set_manual_temperature", {"temperature": 19.5, "duration": 9.5}),
            ("set_manual_temperature", {"temperature": 19.4, "duration": 10}),
            ("set_manual_temperature", {"temperature": float("nan"), "duration": 10}),
        ):
            with self.subTest(service=service, data=data), self.assertRaises(Invalid):
                await self.invoke(service, **data)
        self.coordinator.async_command.assert_not_called()
