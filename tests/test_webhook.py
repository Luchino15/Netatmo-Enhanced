"""Offline tests of the protocol and the HA adapter using test doubles."""

import asyncio
import hmac
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

package = ModuleType("enhanced_test_component")
package.__path__ = [str(Path(__file__).resolve().parents[1]
                        / "custom_components" / "netatmo_enhanced")]
sys.modules[package.__name__] = package
core = importlib.import_module(f"{package.__name__}.webhook_core")

CALLBACK = "https://example.test/api/webhook/synthetic"
SECRET = "offline-test-key"


def signed(raw):
    return hmac.digest(SECRET.encode(), raw, "sha256").hex()


def body(kind="device_event", home="known", **fields):
    return json.dumps({"push_type": kind, "user_id": "example", "home_id": home,
                       **fields}).encode()


def urls(*values):
    return {"status": "ok", "body": [{"url": value} for value in values]}


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_url_skips_post(self):
        request = AsyncMock(return_value=(200, urls("other", CALLBACK)))
        self.assertEqual(await core.ensure_subscription(request, CALLBACK), "already_registered")
        request.assert_awaited_once_with("GET")

    async def test_append_and_confirm(self):
        request = AsyncMock(side_effect=[(200, urls("other")), (200, urls("other", CALLBACK))])
        self.assertEqual(await core.ensure_subscription(request, CALLBACK), "registered")
        self.assertEqual([call.args for call in request.await_args_list],
                         [("GET",), ("POST", {"url": CALLBACK})])

    async def test_bad_get_blocks_post(self):
        for response in [(403, None), (429, None), (500, None),
                         (200, {}), (200, {"status": "ok", "body": [{}]})]:
            with self.subTest(response=response):
                request = AsyncMock(return_value=response)
                result = await core.ensure_subscription(request, CALLBACK)
                self.assertTrue(result.startswith("get_"))
                request.assert_awaited_once_with("GET")

    async def test_conflict_has_no_destructive_fallback(self):
        request = AsyncMock(side_effect=[(200, urls("other")), (409, None)])
        self.assertEqual(await core.ensure_subscription(request, CALLBACK), "post_http_409")
        self.assertEqual(request.await_count, 2)

    async def test_ambiguous_post_reconciles_on_next_check(self):
        request = AsyncMock(side_effect=[(200, urls()), TimeoutError(), (200, urls(CALLBACK))])
        self.assertEqual(await core.ensure_subscription(request, CALLBACK), "post_request_failed")
        self.assertEqual(await core.ensure_subscription(request, CALLBACK), "already_registered")
        self.assertEqual([call.args[0] for call in request.await_args_list], ["GET", "POST", "GET"])


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.signal = Mock()
        self.clock = Mock(return_value=100)
        self.receiver = core.Receiver(SECRET, lambda home: home == "known", self.signal, self.clock)

    def test_signature_covers_raw_bytes(self):
        raw = body()
        self.assertEqual(self.receiver.accept(raw + b" ", signed(raw)), 403)
        self.assertEqual(self.receiver.accept(raw, "bogus"), 403)
        self.assertEqual(self.receiver.accept(raw, signed(raw)), 200)
        self.signal.assert_called_once()

    def test_limits_and_malformed_messages(self):
        self.assertEqual(self.receiver.accept(b"x" * (core.BODY_LIMIT + 1), ""), 413)
        for raw in (b"[]", b"{", b"\xff", b"{}", b'{"push_type":"device_event","user_id":3}'):
            self.assertEqual(self.receiver.accept(raw, signed(raw)), 400)
        self.signal.assert_not_called()

    def test_retries_are_deduplicated_and_expire(self):
        raw = body()
        for _ in range(3):
            self.assertEqual(self.receiver.accept(raw, signed(raw)), 200)
        self.assertEqual(self.receiver.counts["duplicate"], 2)
        self.signal.assert_called_once()
        self.clock.return_value += core.DEDUP_SECONDS
        self.assertEqual(self.receiver.accept(raw, signed(raw)), 200)
        self.assertEqual(self.signal.call_count, 2)

    def test_order_has_no_effect_on_authoritative_read(self):
        for sequence in (99, 1):
            raw = body(extra_params={"sequence_id": sequence, "modules": [{"undeclared": 21}]})
            self.assertEqual(self.receiver.accept(raw, signed(raw)), 200)
        self.assertEqual(self.signal.call_args_list[0].args[0]["refresh"], "eligible")
        self.assertEqual(self.signal.call_count, 2)

    def test_other_notifications_do_not_request_updates(self):
        for raw in (body("webhook_activation"), body("webhook_deletion"), body("not_documented"),
                    body(home="other"), body(home=None)):
            self.assertEqual(self.receiver.accept(raw, signed(raw)), 200)
        self.signal.assert_not_called()
        self.assertEqual(self.receiver.counts["unmatched_home"], 2)
        self.assertEqual(self.receiver.counts["unknown_event"], 1)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.webhook = SimpleNamespace(
            async_generate_id=Mock(return_value="private-id"),
            async_generate_url=Mock(return_value=CALLBACK),
            async_register=Mock(),
            async_unregister=Mock(),
        )
        fake = {
            "aiohttp": SimpleNamespace(ClientError=OSError, ClientTimeout=Mock(),
                                       web=SimpleNamespace(Response=SimpleNamespace)),
            "homeassistant": ModuleType("homeassistant"),
            "homeassistant.components": SimpleNamespace(webhook=self.webhook),
            "homeassistant.helpers.network": SimpleNamespace(NoURLAvailableError=LookupError),
        }
        name = f"{package.__name__}.webhook_v1"
        sys.modules.pop(name, None)
        with patch.dict(sys.modules, fake):
            self.adapter = importlib.import_module(name)
        self.coordinator = SimpleNamespace(
            oauth=SimpleNamespace(implementation=SimpleNamespace(client_secret=SECRET)),
            data={"private-device": {"home_id": "known"}},
            async_webhook_request=AsyncMock(return_value=(200, urls(CALLBACK))),
            command_lock=asyncio.Lock(), guard={}, async_refresh_for=AsyncMock(),
        )
        self.entry = SimpleNamespace(
            options={}, data={}, async_on_unload=Mock(),
            async_create_background_task=lambda hass, work, name: asyncio.create_task(work),
        )
        self.hass = SimpleNamespace(config_entries=SimpleNamespace(
            async_update_entry=lambda entry, **kwargs: vars(entry).update(kwargs)
        ))

    def start(self, mode):
        self.entry.options[core.MODE_KEY] = mode
        adapter = self.adapter.WebhookV1(self.hass, self.entry, self.coordinator)
        adapter.start()
        self.addAsyncCleanup(adapter.stop)
        return adapter

    async def request(self, adapter, raw, signatures):
        async def chunks(_size):
            yield raw[:10]
            yield raw[10:]
        request = SimpleNamespace(
            headers=SimpleNamespace(get=lambda key, default: default,
                                    getall=lambda key, default: signatures),
            content=SimpleNamespace(iter_chunked=chunks),
        )
        return (await adapter._handle(None, None, request)).status

    async def test_off_has_no_listener_or_network(self):
        adapter = self.start(core.MODE_OFF)
        self.webhook.async_register.assert_not_called()
        self.coordinator.async_webhook_request.assert_not_called()
        self.assertEqual(adapter.diagnostics()["registration"], "disabled")

    async def test_receive_only_registration_and_anonymized_diagnostics(self):
        adapter = self.start(core.MODE_RECEIVE)
        await adapter._registration_task
        self.assertEqual(self.webhook.async_register.call_args.kwargs["allowed_methods"], ("POST",))
        raw = body()
        self.assertEqual(await self.request(adapter, raw, [signed(raw), signed(raw)]), 403)
        self.assertEqual(await self.request(adapter, raw, [signed(raw)]), 200)
        self.coordinator.async_refresh_for.assert_not_called()
        report = json.dumps(adapter.diagnostics())
        for value in (SECRET, CALLBACK, "private-id", "private-device", '"known"', signed(raw)):
            self.assertNotIn(value, report)
        await adapter.stop()
        self.webhook.async_unregister.assert_called_once_with(self.hass, "private-id")

    async def test_events_coalesce_during_cooldown(self):
        adapter = self.start(core.MODE_REFRESH)
        await adapter._registration_task
        self.coordinator.guard["until"] = self.adapter.monotonic() + 100
        first = body(temp=20)
        second = body(temp=21)
        self.assertEqual(await self.request(adapter, first, [signed(first)]), 200)
        await asyncio.sleep(0)
        self.assertEqual(await self.request(adapter, second, [signed(second)]), 200)
        self.coordinator.async_refresh_for.assert_not_called()
        self.assertTrue(adapter.diagnostics()["refresh_pending"])
        await adapter.stop()
        self.coordinator.async_refresh_for.assert_not_called()

    async def test_deferred_read_uses_coordinator_without_payload(self):
        adapter = self.start(core.MODE_REFRESH)
        async def refreshed(source, timing):
            timing.update(outcome="success", completed_monotonic_s=self.adapter.monotonic())
        self.coordinator.async_refresh_for.side_effect = refreshed
        raw = body("home_event_changed", event_type="schedule")
        self.assertEqual(await self.request(adapter, raw, [signed(raw)]), 200)
        await adapter._refresh_task
        self.assertEqual(self.coordinator.async_refresh_for.await_args.args[0], "webhook")
        self.assertEqual(adapter.diagnostics()["refresh_success"], 1)

    async def test_command_lock_and_rate_wait_with_coalesced_events(self):
        adapter = self.start(core.MODE_REFRESH)
        lock = self.coordinator.command_lock
        await lock.acquire()
        first = body(temp=20)
        second = body(temp=21)

        async def refreshed(source, timing):
            self.assertTrue(lock.locked())
            timing.update(outcome="success", completed_monotonic_s=self.adapter.monotonic())

        self.coordinator.async_refresh_for.side_effect = refreshed
        await self.request(adapter, first, [signed(first)])
        await asyncio.sleep(0.05)
        await self.request(adapter, second, [signed(second)])
        self.coordinator.async_refresh_for.assert_not_called()
        self.coordinator.guard["until"] = self.adapter.monotonic() + 0.075
        lock.release()
        await asyncio.wait_for(adapter._refresh_task, 1)
        self.coordinator.async_refresh_for.assert_awaited_once()
        one, two = adapter.diagnostics()["recent_events"]
        self.assertEqual((one["refresh"], two["refresh"]), ("queued", "coalesced"))
        self.assertGreater(one["command_lock_wait_ms"], 0)
        self.assertGreater(one["api_limit_wait_ms"], 0)
        self.assertGreaterEqual(one["receipt_to_publication_ms"], one["api_limit_wait_ms"])
        self.assertEqual(one["update"], two["update"])
