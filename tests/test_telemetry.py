"""Offline coverage of export limits, API classification and timing."""

import asyncio
from datetime import UTC, datetime
import importlib
import json
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_webhook import CALLBACK, SECRET, body, core, package, signed, urls

telemetry = importlib.import_module(f"{package.__name__}.telemetry")


class ProjectionTests(unittest.TestCase):
    def test_allowlist_pseudonyms_and_unknown_shape(self):
        personal = "secret-value-123"
        raw = body(home=personal, home_name=personal, ip_address="10.41.77.11",
                   client_secret=personal, **{
                       "https://example.test/private": {"address": "private-address"},
                       "extra_params": {
                           "sequence_id": 17, "refresh_token": personal,
                           "rooms": [{"id": personal, "humidity": 47, "name": personal,
                                      "therm_measured_temperature": 20.5,
                                      "therm_setpoint_temperature": 16.5,
                                      "therm_setpoint_mode": "manual",
                                      "therm_setpoint_end_time": 1795784321}],
                           "modules": [{"room_id": personal, "type": "BNS",
                                        "boiler_status": True, "status": personal}],
                           "future_technical_object": [{"new_mode": "sample"}],
                       },
                   })
        receiver = core.Receiver(SECRET, lambda _: True, Mock())
        receiver.accept(raw, signed(raw))
        receiver.accept(raw, signed(raw))
        first, second = receiver.history.records
        projected = first["payload"]
        self.assertEqual(first["sequence_id"], 17)
        room = projected["extra_params"]["rooms"][0]
        module = projected["extra_params"]["modules"][0]
        self.assertEqual(room["id"], projected["home_id"])
        self.assertEqual(room["id"], module["room_id"])
        self.assertEqual(room["id"], second["payload"]["home_id"])
        self.assertEqual(room["therm_setpoint_temperature"], 16.5)
        self.assertEqual(room["therm_measured_temperature"], 20.5)
        self.assertEqual(room["humidity"], 47)
        self.assertEqual(room["therm_setpoint_mode"], "manual")
        self.assertEqual(room["therm_setpoint_end_time"], 1795784321)
        self.assertEqual(module["status"], {"redacted_type": "str"})
        self.assertIs(second["duplicate"], True)
        unknown_shapes = [value for key, value in projected["extra_params"].items()
                          if key.startswith("field_") and isinstance(value, list)]
        self.assertEqual(list(unknown_shapes[0][0].values()), [{"redacted_type": "str"}])
        report = json.dumps([first, second])
        for private in (personal, "10.41.77.11", "private-address", "example.test",
                        "home_name", "refresh_token", "future_technical_object", signed(raw)):
            self.assertNotIn(private, report)

    def test_authenticated_malformed_and_unknown_and_invalid_signature(self):
        receiver = core.Receiver(SECRET, lambda _: True, Mock())
        self.assertEqual(receiver.accept(b"{", signed(b"{")), 400)
        raw = body("unexpected-private-kind")
        self.assertEqual(receiver.accept(raw, signed(raw)), 200)
        self.assertEqual(receiver.accept(raw, "bad"), 403)
        malformed, unknown = receiver.history.records
        self.assertEqual(malformed["refresh"], "malformed_event")
        self.assertEqual(unknown["event_type"], "unknown")
        self.assertNotIn("unexpected-private-kind", json.dumps(unknown))
        self.assertTrue(unknown["known_home"])
        receiver._signal.assert_not_called()

    def test_bounded_history_projection_and_clocks(self):
        wall = datetime(2026, 11, 27, 12, tzinfo=UTC)
        clock = Mock(side_effect=[10.0, 10.013])
        history = telemetry.EventHistory(clock, lambda: wall)
        row = history.record(history.stamp(), "device_event",
                             {"extra_params": {"modules": [{"id": str(n)} for n in range(100)]}},
                             False, True, "queued")
        self.assertEqual(row["received_at_utc"], "2026-11-27T12:00:00+00:00")
        self.assertEqual(row["received_timestamp_utc"], wall.timestamp())
        self.assertEqual(row["received_monotonic_s"], 10)
        self.assertEqual(row["processing_ms"], 13)
        self.assertEqual(row["payload"]["extra_params"]["modules"][-1], {"truncated": True})
        history = telemetry.EventHistory()
        for n in range(telemetry.EVENT_LIMIT + 4):
            history.record(history.stamp(), "device_event", {"sequence_id": n},
                           False, None, "missing_home_id")
        self.assertEqual(len(history.records), telemetry.EVENT_LIMIT)
        self.assertEqual(history.records[0]["sequence_id"], 4)


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_2xx_get_reconciliation_without_second_post(self):
        for response, reason in [((204, None), "empty_or_unreadable"),
                                 ((200, {"status": "ok"}), "unexpected_schema"),
                                 ((200, urls("other")), "url_absent")]:
            request = AsyncMock(side_effect=[(200, urls("other")), response, (200, urls(CALLBACK))])
            sleep = AsyncMock()
            details = {}
            status = await core.ensure_subscription(request, CALLBACK, sleep=sleep, details=details)
            self.assertEqual(status, "registered_verified")
            self.assertEqual(details["post_response"], reason)
            self.assertEqual([call.args[0] for call in request.await_args_list], ["GET", "POST", "GET"])
            sleep.assert_awaited_once_with(2)

    async def test_confirmation_can_be_blocked_by_cooldown_or_malformed(self):
        for response, expected in [((429, None), "verify_http_429"),
                                   ((200, {}), "verify_unexpected_response"),
                                   ((200, urls("other")), "post_unconfirmed")]:
            request = AsyncMock(side_effect=[(200, urls()), (201, None), response])
            self.assertEqual(await core.ensure_subscription(request, CALLBACK, sleep=AsyncMock()), expected)
            self.assertEqual(request.await_count, 3)


class FakeCoordinator:
    def __init__(self, *args, **kwargs):
        self.last_update_success = True
        self._lock = asyncio.Lock()

    async def async_refresh(self):
        async with self._lock:
            await self._async_refresh()

    async def _async_refresh(self, **kwargs):
        try:
            await self._async_update_data()
        except UpdateFailed:
            self.last_update_success = False
        else:
            self.last_update_success = True


class UpdateFailed(Exception):
    pass


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http = SimpleNamespace(request=Mock())
        fake = {
            "aiohttp": SimpleNamespace(ClientError=OSError, ClientTimeout=Mock()),
            "homeassistant": ModuleType("homeassistant"),
            "homeassistant.exceptions": SimpleNamespace(
                ConfigEntryAuthFailed=LookupError, HomeAssistantError=ValueError
            ),
            "homeassistant.helpers.aiohttp_client": SimpleNamespace(
                async_get_clientsession=lambda hass: self.http
            ),
            "homeassistant.helpers.update_coordinator": SimpleNamespace(
                DataUpdateCoordinator=FakeCoordinator, UpdateFailed=UpdateFailed
            ),
        }
        with patch.dict(sys.modules, fake):
            mod = importlib.import_module(f"{package.__name__}.coordinator")
        oauth = SimpleNamespace(token={"access_token": "private-oauth"}, async_ensure_token_valid=AsyncMock())
        self.coordinator = mod.EnhancedCoordinator(SimpleNamespace(data={}), SimpleNamespace(entry_id="e"), oauth)

    def reply(self, status, payload=None, headers=None):
        response = SimpleNamespace(status=status, headers=headers or {}, json=AsyncMock(return_value=payload))
        context = AsyncMock()
        context.__aenter__.return_value = response
        return context

    async def test_counts_physical_requests_and_respects_shared_cooldown(self):
        self.http.request.side_effect = [self.reply(200, {"status": "ok"}),
                                         self.reply(429, headers={"Retry-After": "1000"})]
        await self.coordinator._request("setstate")
        self.assertEqual(await self.coordinator.async_webhook_request("GET"), (429, None))
        self.assertEqual(await self.coordinator.async_webhook_request("GET"), (429, None))
        with self.assertRaises(UpdateFailed):
            await self.coordinator._request("homesdata")
        report = self.coordinator.metrics.snapshot()
        self.assertEqual(report["http_requests_total"], 2)
        write, registration = report["http_requests"]
        self.assertEqual((write["source"], write["endpoint"], write["success"]), ("command", "/api/setstate", 1))
        self.assertEqual((registration["http_429"], registration["failed"], registration["status_codes"]),
                         (1, 1, {"429": 1}))
        self.assertNotIn("private-oauth", json.dumps(report))

    async def test_transport_errors_and_json_errors_are_counted(self):
        broken = self.reply(200)
        broken.__aenter__.return_value.json.side_effect = ValueError("private response")
        self.http.request.side_effect = [broken, TimeoutError("private URL")]
        self.assertEqual(await self.coordinator.async_webhook_request("POST", {"url": CALLBACK}), (200, None))
        with self.assertRaises(TimeoutError):
            await self.coordinator.async_webhook_request("POST", {"url": CALLBACK})
        row = self.coordinator.metrics.snapshot()["http_requests"][0]
        self.assertEqual(row["failures"], {"invalid_response": 1, "timeout": 1})
        self.assertEqual(row["status_codes"], {"200": 1})
        self.assertNotIn("private", json.dumps(row))

    async def test_update_sources_and_http_timing(self):
        self.http.request.return_value = self.reply(200, {"body": {}})

        async def snapshots():
            for name in ("homesdata", "homestatus"):
                await self.coordinator._read(name)
            return {}

        self.coordinator._read_snapshots = snapshots
        await self.coordinator._async_refresh(raise_on_entry_error=True)
        await self.coordinator._async_refresh(scheduled=True)
        await self.coordinator.async_refresh()
        timing = {}
        await asyncio.gather(self.coordinator.async_refresh_for("webhook", timing),
                             self.coordinator.async_refresh_for("command"))
        report = self.coordinator.metrics.snapshot()
        self.assertEqual(report["http_requests_total"], 10)
        self.assertEqual(report["coordinator_updates"],
                         {kind: {"success": 1} for kind in ("startup", "periodic", "manual", "command", "webhook")})
        self.assertEqual(timing["http_requests"], 2)
        self.assertLessEqual(timing["started_monotonic_s"], timing["first_http_started_monotonic_s"])
        self.assertLessEqual(timing["last_http_completed_monotonic_s"], timing["completed_monotonic_s"])
        self.assertGreaterEqual(timing["duration_ms"], timing["http_span_ms"])
