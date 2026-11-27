"""Verify config entry options against the installed Home Assistant Core."""

from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from push_harness import CALLBACK, HA_AVAILABLE, SECRET

if HA_AVAILABLE:
    from homeassistant import config_entries, data_entry_flow
    from homeassistant.core import HomeAssistant

    from custom_components import netatmo_enhanced as integration
    from custom_components.netatmo_enhanced.config_flow import NetatmoEnhancedFlow
    from custom_components.netatmo_enhanced.const import DOMAIN, SCOPES
    from custom_components.netatmo_enhanced.webhook_core import (
        MODE_KEY, MODE_OFF, MODE_PUSH, MODE_RECEIVE, MODE_REFRESH,
    )
    from custom_components.netatmo_enhanced.webhook_v1 import webhook


@unittest.skipUnless(HA_AVAILABLE, "requires Home Assistant Core")
class WebhookOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_options_switch_without_reloading_the_entry(self):
        directory = self.enterContext(TemporaryDirectory())
        hass = HomeAssistant(directory)
        self.addCleanup(hass.import_executor.shutdown)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        entry = config_entries.ConfigEntry(
            data={"token": {"scope": sorted(SCOPES)}, "enhanced_webhook_id": "synthetic"},
            discovery_keys=MappingProxyType({}), domain=DOMAIN, minor_version=1,
            options={MODE_KEY: MODE_REFRESH, "other_option": True}, source="user",
            state=config_entries.ConfigEntryState.LOADED, subentries_data=None,
            title="Offline thermostat", unique_id=DOMAIN, version=1,
        )
        hass.config_entries._entries[entry.entry_id] = entry
        oauth = SimpleNamespace(implementation=SimpleNamespace(client_secret=SECRET))
        coordinator = SimpleNamespace(
            oauth=oauth,
            async_config_entry_first_refresh=AsyncMock(),
            async_set_cloud_push=Mock(),
            async_webhook_request=AsyncMock(return_value=(200, {"body": [{"url": CALLBACK}]})),
        )
        self.enterContext(patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()))
        reload_entry = self.enterContext(patch.object(hass.config_entries, "async_schedule_reload"))
        self.enterContext(patch.object(config_entries, "_async_get_flow_handler",
                                      AsyncMock(return_value=NetatmoEnhancedFlow)))
        self.enterContext(patch.object(integration, "async_get_config_entry_implementation", AsyncMock()))
        self.enterContext(patch.object(integration, "OAuth2Session", return_value=oauth))
        self.enterContext(patch.object(integration, "EnhancedCoordinator", return_value=coordinator))
        self.enterContext(patch.object(webhook, "async_generate_url", return_value=CALLBACK))
        register = self.enterContext(patch.object(webhook, "async_register", wraps=webhook.async_register))
        unregister = self.enterContext(patch.object(webhook, "async_unregister", wraps=webhook.async_unregister))

        self.assertTrue(await integration.async_setup_entry(hass, entry))
        self.addAsyncCleanup(entry._async_process_on_unload, hass)
        adapter = coordinator.webhook_v1
        await adapter._registration_task
        self.assertEqual(adapter.mode, MODE_REFRESH)

        for next_mode in (MODE_PUSH, MODE_RECEIVE, MODE_REFRESH, MODE_OFF, MODE_PUSH):
            form = await hass.config_entries.options.async_init(entry.entry_id)
            schema = form["data_schema"]
            self.assertEqual(schema({})[MODE_KEY], entry.options[MODE_KEY])
            for available_mode in (MODE_OFF, MODE_RECEIVE, MODE_REFRESH, MODE_PUSH):
                self.assertEqual(schema({MODE_KEY: available_mode})[MODE_KEY], available_mode)
            with self.assertRaises(data_entry_flow.InvalidData):
                await hass.config_entries.options.async_configure(form["flow_id"], {MODE_KEY: "invalid"})
            result = await hass.config_entries.options.async_configure(form["flow_id"], {MODE_KEY: next_mode})
            self.assertIs(result["type"], data_entry_flow.FlowResultType.CREATE_ENTRY)
            await hass.async_block_till_done()
            self.assertEqual(adapter.mode, next_mode)
            self.assertTrue(entry.options["other_option"])
            self.assertEqual(entry.data["enhanced_webhook_id"], "synthetic")
            self.assertIs(entry.runtime_data, coordinator)

        await adapter._registration_task
        self.assertEqual([request.args[3] for request in register.call_args_list], ["synthetic", "synthetic"])
        unregister.assert_called_once_with(hass, "synthetic")
        self.assertEqual(coordinator.async_webhook_request.await_args_list, [call("GET"), call("GET")])
        self.assertEqual(coordinator.async_set_cloud_push.call_args_list, [call(True), call(False), call(True)])
        coordinator.async_config_entry_first_refresh.assert_awaited_once()
        reload_entry.assert_not_called()
