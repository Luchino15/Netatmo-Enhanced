"""Synthetic Netatmo HTTP and webhook inputs for offline runtime tests."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from functools import lru_cache
import hmac
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit

from multidict import CIMultiDict

from push_payloads import HOME, MODULE, ROOM, SUMMER, WINTER, WINTER_ALT


SECRET = "synthetic-signing-key"
CALLBACK = "https://example.test/api/webhook/synthetic"
HA_AVAILABLE = importlib.util.find_spec("homeassistant") is not None


class FakeUpdateFailed(Exception):
    pass


class FakeConfigEntryAuthFailed(Exception):
    pass


class FakeHomeAssistantError(Exception):
    pass


class OfflineUpdateCoordinator:
    """Model the HA scheduler, update lock and synchronous listener publication."""

    def __init__(self, hass, logger, *, name, config_entry, update_interval):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.config_entry = config_entry
        self.update_interval = update_interval
        self.data = None
        self.last_update_success = True
        self.last_exception = None
        self._listeners = []
        self._unsub_refresh = None
        self._shutdown_requested = False
        self._refresh_lock = asyncio.Lock()
        config_entry.async_on_unload(self.async_shutdown)

    def async_add_listener(self, callback):
        self._listeners.append(callback)
        if len(self._listeners) == 1:
            self._schedule_refresh()
        def remove():
            self._listeners.remove(callback)
            if not self._listeners:
                self._unschedule_refresh()

        return remove

    def async_update_listeners(self):
        for callback in self._listeners:
            callback()

    def _async_unsub_refresh(self):
        if self._unsub_refresh is not None:
            self._unsub_refresh()
            self._unsub_refresh = None

    def _unschedule_refresh(self):
        self._async_unsub_refresh()

    def _schedule_refresh(self):
        if self.update_interval is None or self.config_entry.pref_disable_polling:
            return
        self._async_unsub_refresh()
        loop = self.hass.loop
        when = loop.time() + self.update_interval.total_seconds()
        self._unsub_refresh = loop.call_at(
            when, lambda: asyncio.create_task(self._handle_refresh_interval()),
        ).cancel

    async def _handle_refresh_interval(self):
        self._unsub_refresh = None
        async with self._refresh_lock:
            await self._async_refresh(scheduled=True)

    def async_set_updated_data(self, data):
        self._async_unsub_refresh()
        self.data = data
        self.last_update_success = True
        if self._listeners:
            self._schedule_refresh()
        self.async_update_listeners()

    async def async_shutdown(self):
        self._shutdown_requested = True
        self._async_unsub_refresh()

    async def async_config_entry_first_refresh(self):
        async with self._refresh_lock:
            await self._async_refresh(raise_on_entry_error=True)
        if not self.last_update_success:
            raise self.last_exception

    async def async_refresh(self):
        async with self._refresh_lock:
            await self._async_refresh()

    def _async_refresh_finished(self):
        pass

    async def _async_refresh(self, **kwargs):
        self._async_unsub_refresh()
        if self._shutdown_requested:
            return
        previous_success = self.last_update_success
        try:
            self.data = await self._async_update_data()
        except (FakeUpdateFailed, FakeConfigEntryAuthFailed, TimeoutError) as exc:
            self.last_exception = exc
            self.last_update_success = False
        else:
            self.last_update_success = True
        finally:
            if self._listeners:
                self._schedule_refresh()
        self._async_refresh_finished()
        if self.last_update_success or previous_success:
            self.async_update_listeners()


@lru_cache(maxsize=2)
def load_runtime(real_ha):
    package = ModuleType(f"netatmo_runtime_test_{'ha' if real_ha else 'stub'}")
    package.__path__ = [str(Path(__file__).resolve().parents[1]
                            / "custom_components" / "netatmo_enhanced")]
    sys.modules[package.__name__] = package
    substitutions = {} if real_ha else {
        "homeassistant": ModuleType("homeassistant"),
        "homeassistant.components": SimpleNamespace(webhook=None),
        "homeassistant.core": SimpleNamespace(callback=lambda func: func),
        "homeassistant.exceptions": SimpleNamespace(
            ConfigEntryAuthFailed=FakeConfigEntryAuthFailed,
            HomeAssistantError=FakeHomeAssistantError,
        ),
        "homeassistant.helpers.aiohttp_client": SimpleNamespace(async_get_clientsession=None),
        "homeassistant.helpers.network": SimpleNamespace(NoURLAvailableError=LookupError),
        "homeassistant.helpers.update_coordinator": SimpleNamespace(
            DataUpdateCoordinator=OfflineUpdateCoordinator, UpdateFailed=FakeUpdateFailed,
        ),
    }
    names = (f"{package.__name__}.coordinator", f"{package.__name__}.webhook_v1")
    if real_ha:
        modules = tuple(importlib.import_module(name) for name in names)
    else:
        with patch.dict(sys.modules, substitutions):
            modules = tuple(importlib.import_module(name) for name in names)
    return modules


class PushHarness:
    """Drive the real integration code through a signed request and fake HTTP."""

    @classmethod
    async def create(cls, mode="push", *, multi=False, real_ha=HA_AVAILABLE):
        self = cls(mode=mode, multi=multi, real_ha=real_ha)
        try:
            await self.coordinator.async_config_entry_first_refresh()
            self.remove_listener = self.coordinator.async_add_listener(self._on_update)
            self.adapter.start()
            if self.adapter._registration_task is not None:
                await self.adapter._registration_task
        except BaseException:
            await self.stop()
            raise
        return self

    def __init__(self, *, mode, multi, real_ha):
        self.coordinator_module, self.adapter_module = load_runtime(real_ha)
        self.real_ha = real_ha
        self.home = {
            "id": HOME,
            "temperature_control_mode": "heating",
            "therm_mode": "schedule",
            "cooling_mode": "schedule",
            "rooms": [{"id": ROOM, "name": "Test room"}],
            "modules": [{"id": MODULE, "room_id": ROOM, "type": "BNS"}],
            "schedules": [
                {"id": WINTER, "name": "Winter", "type": "therm", "selected": True},
                {"id": WINTER_ALT, "name": "Winter alternate", "type": "therm"},
                {"id": SUMMER, "name": "Summer", "type": "cooling", "selected": True},
            ],
        }
        self.status = {
            "id": HOME,
            "rooms": [{
                "id": ROOM,
                "therm_measured_temperature": 19.4,
                "humidity": 52,
                "therm_setpoint_temperature": 20,
                "therm_setpoint_mode": "home",
                "cooling_setpoint_temperature": 26,
                "cooling_setpoint_mode": "home",
            }],
            "modules": [{
                "id": MODULE, "boiler_status": False, "cooler_status": False,
                "reachable": True,
            }],
        }
        if multi:
            self.home["rooms"].append({"id": "demo-room-2", "name": "Other test room"})
            self.home["modules"].extend((
                {"id": "demo-module-2", "room_id": "demo-room-2", "type": "BNS"},
                {"id": "demo-module-3", "room_id": ROOM, "type": "BNS"},
            ))
            self.status["rooms"].append(dict(self.status["rooms"][0], id="demo-room-2"))
            self.status["modules"].extend(
                dict(self.status["modules"][0], id=module)
                for module in ("demo-module-2", "demo-module-3")
            )
        self.api_calls = []
        self.api_payloads = []
        self.http_hook = None
        self.updates = []
        self.unloads = []
        self.background_tasks = set()
        self.remove_listener = None
        self.config_dir = None
        self.webhook = SimpleNamespace(
            async_generate_id=Mock(return_value="synthetic"),
            async_generate_url=Mock(return_value=CALLBACK),
            async_register=Mock(),
            async_unregister=Mock(),
        )
        if real_ha:
            from homeassistant.config_entries import ConfigEntryState
            from homeassistant.core import HomeAssistant

            self.config_dir = TemporaryDirectory()
            self.hass = HomeAssistant(self.config_dir.name)
            self.entry_state = ConfigEntryState.SETUP_IN_PROGRESS
        else:
            self.hass = SimpleNamespace(
                data={}, loop=asyncio.get_running_loop(), is_stopping=False,
            )
            self.entry_state = "setup_in_progress"
        self.hass.config_entries = SimpleNamespace(
            async_update_entry=lambda entry, **changes: vars(entry).update(changes)
        )
        self.entry = SimpleNamespace(
            entry_id="test-entry",
            title="Offline thermostat",
            options={"webhook_mode": mode},
            data={"enhanced_webhook_id": "synthetic"},
            pref_disable_polling=False,
            state=self.entry_state,
            async_on_unload=self.unloads.append,
            async_start_reauth_if_available=Mock(),
            async_create_background_task=self._background_task,
        )
        self.oauth = SimpleNamespace(
            token={
                "access_token": "synthetic-oauth-token",
                "scope": sorted(self.coordinator_module.SCOPES),
            },
            implementation=SimpleNamespace(client_secret=SECRET),
            async_ensure_token_valid=AsyncMock(),
        )
        with patch.object(self.coordinator_module, "async_get_clientsession",
                          return_value=SimpleNamespace(request=self._http)):
            self.coordinator = self.coordinator_module.EnhancedCoordinator(
                self.hass, self.entry, self.oauth,
            )
        self.adapter = self.adapter_module.WebhookV1(
            self.hass, self.entry, self.coordinator,
        )
        self.coordinator.webhook_v1 = self.adapter
        self.entry.runtime_data = self.coordinator
        self._webhook_patch = patch.object(self.adapter_module, "webhook", self.webhook)
        self._webhook_patch.start()

    def _background_task(self, hass, work, name, **kwargs):
        task = asyncio.create_task(work, name=name)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    def _on_update(self):
        self.updates.append(self.state)

    @property
    def state(self):
        return self.coordinator.data

    @asynccontextmanager
    async def _http(self, method, url, **kwargs):
        path = urlsplit(url).path
        endpoint = f"webhook_{method.lower()}" if path == "/webhooks/v1" else path.rsplit("/", 1)[1]
        self.api_calls.append(endpoint)
        self.api_payloads.append(kwargs)
        reply = None if self.http_hook is None else await self.http_hook(endpoint, kwargs)
        if reply is None:
            if endpoint == "webhook_get":
                reply = 200, {"body": [{"url": CALLBACK}]}, {}
            elif endpoint == "homesdata":
                reply = 200, {"status": "ok", "body": {"homes": [deepcopy(self.home)]}}, {}
            elif endpoint == "homestatus":
                reply = 200, {"status": "ok", "body": {"home": deepcopy(self.status)}}, {}
            else:
                reply = 200, {"status": "ok"}, {}
        code, body, headers = reply
        yield SimpleNamespace(status=code, headers=headers, json=AsyncMock(return_value=body))

    async def push(self, payload, signature=None):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        if signature is None:
            signature = hmac.digest(SECRET.encode(), raw, "sha256").hex()

        async def chunks(_size):
            yield raw[:10]
            yield raw[10:]

        request = SimpleNamespace(
            headers=CIMultiDict({"X-Netatmo-secret": signature}),
            content=SimpleNamespace(iter_chunked=chunks),
        )
        response = await self.adapter._handle(self.hass, "synthetic", request)
        return response.status

    async def stop(self):
        try:
            await self.adapter.stop()
            if self.remove_listener is not None:
                self.remove_listener()
                self.remove_listener = None
            await self.coordinator.async_shutdown()
            tasks = list(self.background_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._webhook_patch.stop()
            if self.config_dir is not None:
                self.config_dir.cleanup()
