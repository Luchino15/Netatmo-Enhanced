"""Exercise signed webhook delivery, entity reads and diagnostics offline."""

import asyncio
from contextlib import nullcontext
from copy import deepcopy
from enum import IntFlag, StrEnum
import importlib
import json
import sys
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from push_harness import CALLBACK, HA_AVAILABLE, SECRET, PushHarness
from push_payloads import (
    HOME, MODULE, ROOM, WINTER, WINTER_ALT, device_payload, heating_mode_event, home_payload,
    module_event, room_event, schedule_event, season_event,
)
from test_webhook import core, package


def entity_platforms():
    """Use the installed HA platforms when available and small doubles otherwise."""
    platform_doubles = {}
    if not HA_AVAILABLE:
        class CoordinatorEntity:
            def __init__(self, coordinator):
                self.coordinator = coordinator

            @property
            def available(self):
                return self.coordinator.last_update_success

        class HVACMode(StrEnum):
            OFF = "off"
            AUTO = "auto"

        class HVACAction(StrEnum):
            OFF = "off"
            HEATING = "heating"
            COOLING = "cooling"
            IDLE = "idle"

        class ClimateEntityFeature(IntFlag):
            TARGET_TEMPERATURE = 1
            PRESET_MODE = 2

        platform_doubles = {
            "homeassistant.components.climate": SimpleNamespace(
                ClimateEntity=object, ClimateEntityFeature=ClimateEntityFeature,
                HVACMode=HVACMode, HVACAction=HVACAction,
            ),
            "homeassistant.components.sensor": SimpleNamespace(
                SensorEntity=object, SensorDeviceClass=SimpleNamespace(TEMPERATURE="temperature",
                                                                      HUMIDITY="humidity"),
                SensorStateClass=SimpleNamespace(MEASUREMENT="measurement"),
            ),
            "homeassistant.components.select": SimpleNamespace(SelectEntity=object),
            "homeassistant.const": SimpleNamespace(
                ATTR_TEMPERATURE="temperature", UnitOfTemperature=SimpleNamespace(CELSIUS="°C"),
                PERCENTAGE="%",
            ),
            "homeassistant.exceptions": SimpleNamespace(HomeAssistantError=ValueError),
            "homeassistant.helpers.device_registry": SimpleNamespace(DeviceInfo=dict),
            "homeassistant.helpers.update_coordinator": SimpleNamespace(CoordinatorEntity=CoordinatorEntity),
        }
    with nullcontext() if HA_AVAILABLE else patch.dict(sys.modules, platform_doubles):
        return tuple(importlib.import_module(f"{package.__name__}.{name}")
                     for name in ("climate", "sensor", "select"))


async def setup_entities(coordinator):
    entry = SimpleNamespace(runtime_data=coordinator)
    entities = []
    for platform in entity_platforms():
        await platform.async_setup_entry(None, entry, entities.extend)
    return {entity._attr_unique_id: entity for entity in entities}


class PushIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.harness = await PushHarness.create()
        self.addAsyncCleanup(self.harness.stop)

    async def deliver(self, payload):
        self.assertEqual(await self.harness.push(payload), 200)

    async def test_observed_device_replay_updates_existing_entities_with_zero_reads(self):
        h = self.harness
        entities = await setup_entities(h.coordinator)
        climate = entities[f"{MODULE}_climate"]
        humidity = entities[f"{MODULE}_humidity"]
        schedule = entities[f"{HOME}_schedule_select"]
        target = entities[f"{MODULE}_target_temperature"]
        api_calls_before = list(h.api_calls)

        # Paired room, module and humidity deliveries share a sequence number.
        await self.deliver(room_event(400, therm_measured_temperature=22.7,
                                      therm_setpoint_mode="home", therm_setpoint_temperature=24.5))
        await self.deliver(module_event(400, True, False))
        await self.deliver(room_event(400, humidity=57))
        self.assertEqual((climate.current_temperature, target.native_value, humidity.native_value),
                         (22.7, 24.5, 57))
        self.assertEqual(climate.hvac_action, "heating")

        await self.deliver(room_event(401, therm_setpoint_mode="manual", therm_setpoint_temperature=25,
                                      therm_setpoint_end_time=2_000_000_000))
        self.assertEqual(climate.preset_mode, "manual")
        await self.deliver(room_event(402, therm_setpoint_mode="home", therm_setpoint_temperature=17,
                                      therm_setpoint_end_time=None))
        self.assertEqual((target.native_value, climate.preset_mode), (17, "schedule"))
        self.assertIsNone(h.state[MODULE]["setpoint_end_time"])
        self.assertEqual(climate.hvac_action, "heating", "Hardware state decides HVAC action")
        await self.deliver(module_event(403, False, False))
        self.assertEqual(climate.hvac_action, "idle")

        await self.deliver(heating_mode_event("away"))
        await self.deliver(room_event(404, therm_setpoint_mode="home", therm_setpoint_temperature=13))
        self.assertEqual((climate.preset_mode, target.native_value), ("away", 13))
        self.assertEqual(h.state[MODULE]["room_mode"], "home")
        self.assertEqual(h.state[MODULE]["home_mode"], "away")
        await self.deliver(heating_mode_event("schedule"))

        await self.deliver(schedule_event(WINTER_ALT, nested=True))
        self.assertEqual(h.state[MODULE]["active_schedule_id"], WINTER_ALT)
        self.assertIn(schedule.current_option, schedule.options)
        await self.deliver(schedule_event(WINTER))
        self.assertEqual(h.state[MODULE]["active_schedule_id"], WINTER)

        await self.deliver(room_event(405, cooling_setpoint_mode="home", cooling_setpoint_temperature=26))
        await self.deliver(module_event(405, False, True))
        self.assertEqual(h.state[MODULE]["season"], "heating")
        self.assertEqual(climate.hvac_action, "cooling")
        await self.deliver(season_event("cooling"))
        self.assertEqual((climate.hvac_action, target.native_value), ("cooling", 26))
        await self.deliver(module_event(406, False, False))
        self.assertEqual(climate.hvac_action, "idle")
        await self.deliver(season_event("heating", nested=True))
        self.assertEqual((climate.hvac_action, target.native_value), ("idle", 13))

        await asyncio.sleep(0)
        self.assertEqual(h.api_calls, api_calls_before)
        self.assertFalse(h.adapter.diagnostics()["refresh_pending"])
        self.assertGreaterEqual(sum("receipt_to_publication_ms" in event for event in
                                    h.adapter.diagnostics()["recent_events"]), 12)

    async def test_same_signed_burst_compares_cloud_push_and_legacy_http_requests(self):
        legacy = await PushHarness.create(mode=core.MODE_REFRESH)
        self.addAsyncCleanup(legacy.stop)
        frames = (
            room_event(450, therm_measured_temperature=22.7, therm_setpoint_mode="home",
                       therm_setpoint_temperature=24.5),
            module_event(450, True, False),
            room_event(450, humidity=58),
            schedule_event(WINTER_ALT),
            room_event(451, therm_setpoint_mode="manual", therm_setpoint_temperature=25),
            room_event(452, therm_setpoint_mode="home", therm_setpoint_temperature=18.5),
            module_event(452, False, False),
        )

        async def replay(harness):
            before = len(harness.api_calls)
            harness.coordinator.guard["refresh_started"] = monotonic() - core.MIN_REFRESH_SECONDS - 1
            async with harness.coordinator.command_lock:
                for payload in frames:
                    self.assertEqual(await harness.push(payload), 200)
                self.assertEqual(len(harness.api_calls), before)
            if harness.adapter._refresh_task is not None:
                await asyncio.wait_for(harness.adapter._refresh_task, 2)
            return harness.api_calls[before:]

        self.assertEqual(await replay(legacy), ["homesdata", "homestatus"])
        self.assertEqual(await replay(self.harness), [])
        self.assertEqual(self.harness.state[MODULE]["active_schedule_id"], WINTER_ALT)
        self.assertEqual(self.harness.state[MODULE]["target_temperature"], 18.5)

    async def test_invalid_hmac_and_unauthorized_home_room_and_module_never_publish(self):
        h = self.harness
        baseline = deepcopy(h.state)
        invalid = room_event(500, humidity=72)
        self.assertEqual(await h.push(invalid, signature="0" * 64), 403)
        await self.deliver(home_payload(home_id="unconfigured-home", home={"therm_mode": "away"}))
        conflicting = season_event("cooling", nested=True)
        conflicting["home_id"] = "unconfigured-home"
        await self.deliver(conflicting)
        await self.deliver(room_event(501, room_id="unconfigured-room", humidity=72))
        await self.deliver(module_event(501, True, False, module_id="unconfigured-module"))
        await self.deliver(module_event(502, True, False, module_type="BAC"))
        self.assertEqual(h.state, baseline)

    async def test_repeated_unsequenced_season_is_reconciled_instead_of_deduplicated(self):
        h = self.harness
        cooling = season_event("cooling")
        await self.deliver(cooling)
        self.assertEqual(h.state[MODULE]["season"], "cooling")
        await self.deliver(season_event("heating", nested=True))
        self.assertEqual(h.state[MODULE]["season"], "heating")
        await self.deliver(cooling)
        row = h.adapter.diagnostics()["recent_events"][-1]
        self.assertFalse(row["duplicate"])
        self.assertEqual(row["reconciliation_reasons"], ["unsequenced_reversal"])

        h.home["temperature_control_mode"] = "cooling"
        h.coordinator.guard["refresh_started"] = monotonic() - core.MIN_REFRESH_SECONDS - 1
        with patch.object(h.coordinator_module, "PUSH_API_GRACE_SECONDS", 0):
            await asyncio.wait_for(h.adapter._refresh_task, 2)
        self.assertEqual(h.state[MODULE]["season"], "cooling")
        self.assertEqual(h.api_calls[-2:], ["homesdata", "homestatus"])

    async def test_webhook_mode_switch_preserves_route_and_restores_read_path(self):
        h = self.harness
        callback_id = h.adapter._id
        registered_calls = h.api_calls.count("webhook_get")
        before = await setup_entities(h.coordinator)
        await self.deliver(room_event(600, humidity=54))
        self.assertEqual(h.state[MODULE]["humidity"], 54)
        entry = h.adapter.entry
        entry.options = {**entry.options, core.MODE_KEY: core.MODE_REFRESH}
        await h.adapter.async_set_mode(core.MODE_REFRESH)
        self.assertEqual(h.adapter._id, callback_id)
        self.assertEqual(h.api_calls.count("webhook_get"), registered_calls)

        status_room = h.status["rooms"][0]
        status_room["humidity"] = 63
        h.coordinator.guard["refresh_started"] = monotonic() - core.MIN_REFRESH_SECONDS - 1
        await self.deliver(room_event(601, humidity=75))
        await asyncio.wait_for(h.adapter._refresh_task, 2)
        self.assertEqual(h.state[MODULE]["humidity"], 63)
        self.assertEqual(h.api_calls[-2:], ["homesdata", "homestatus"])

        after = await setup_entities(h.coordinator)
        self.assertEqual(set(after), set(before))
        self.assertEqual(len(after), 10)
        for entity_id in before:
            with self.subTest(entity_id=entity_id):
                before_ids = before[entity_id]._attr_device_info["identifiers"]
                after_ids = after[entity_id]._attr_device_info["identifiers"]
                self.assertEqual(before_ids, after_ids)
                self.assertEqual(after_ids, {("netatmo_enhanced", MODULE)})

    async def test_downloaded_diagnostics_are_bounded_and_anonymized(self):
        h = self.harness
        for sequence in range(700, 804):
            raw = device_payload(rooms=[{
                "id": ROOM, "sequence_id": sequence, "humidity": 40 + sequence % 2,
                "access_token": "synthetic-access-token", "clients_private_name": "synthetic-value",
            }])
            await self.deliver(raw)
        diagnostics = importlib.import_module(f"{package.__name__}.diagnostics")
        report = await diagnostics.async_get_config_entry_diagnostics(
            None, SimpleNamespace(runtime_data=h.coordinator),
        )
        recent = report["webhook_v1"]["recent_events"]
        self.assertEqual(len(recent), 100)
        encoded = json.dumps(report)
        for value in (SECRET, CALLBACK, h.oauth.token["access_token"], HOME, MODULE, ROOM, "demo-user",
                      "synthetic-access-token", "clients_private_name", "synthetic-value"):
            with self.subTest(value=value):
                self.assertNotIn(value, encoded)
