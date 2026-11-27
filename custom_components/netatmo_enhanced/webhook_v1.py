"""Home Assistant adapter for the experimental Netatmo webhook."""

import asyncio
from collections import deque
from contextlib import suppress
from copy import deepcopy
import logging
from time import monotonic
from urllib.parse import urlsplit

from aiohttp import ClientError, web
from homeassistant.components import webhook
from homeassistant.helpers.network import NoURLAvailableError

from .const import DOMAIN
from .telemetry import EVENT_LIMIT, elapsed_ms
from .webhook_core import (
    BODY_LIMIT, MIN_REFRESH_SECONDS, MODE_KEY, MODE_OFF,
    MODE_RECEIVE, MODE_REFRESH, Receiver, ensure_subscription,
)

_LOGGER = logging.getLogger(__name__)
_ID_KEY = "enhanced_webhook_id"


class WebhookV1:
    def __init__(self, hass, entry, coordinator):
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.mode = entry.options.get(MODE_KEY, MODE_OFF)
        self.registration = "disabled"
        self.registration_details = {}
        self.receiver = None
        self._id = None
        self._registration_task = None
        self._refresh_task = None
        self._pending = deque(maxlen=EVENT_LIMIT)
        self._refresh_count = 0
        self._refresh_errors = 0

    def start(self):
        if self.mode not in {MODE_RECEIVE, MODE_REFRESH}:
            return
        secret = getattr(self.coordinator.oauth.implementation, "client_secret", None)
        if not isinstance(secret, str) or not secret:
            self.registration = "missing_client_secret"
            return
        webhook_id = self.entry.data.get(_ID_KEY) or webhook.async_generate_id()
        try:
            url = webhook.async_generate_url(self.hass, webhook_id, allow_internal=False)
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.query or parsed.fragment):
                self.registration = "invalid_external_https_url"
                return
        except (NoURLAvailableError, ValueError):
            self.registration = "invalid_external_https_url"
            return

        self.receiver = Receiver(secret, self._known_home, self._signal)
        try:
            webhook.async_register(
                self.hass, DOMAIN, "Netatmo Enhanced v1", webhook_id, self._handle,
                local_only=False, allowed_methods=("POST",),
            )
        except ValueError:
            self.registration = "local_registration_conflict"
            self.receiver = None
            return
        self._id = webhook_id
        self.entry.async_on_unload(self.stop)
        if self.entry.data.get(_ID_KEY) != webhook_id:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, _ID_KEY: webhook_id}
            )
        self.registration = "pending"
        self._registration_task = self.entry.async_create_background_task(
            self.hass, self._register_remote(url), "Netatmo Enhanced webhook registration"
        )

    async def stop(self):
        if self._id is not None:
            webhook.async_unregister(self.hass, self._id)
            self._id = None
        tasks = (self._registration_task, self._refresh_task)
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task

    async def _register_remote(self, url):
        self.registration = await ensure_subscription(
            self.coordinator.async_webhook_request, url, details=self.registration_details
        )
        if self.registration not in {"registered", "already_registered", "registered_verified"}:
            _LOGGER.warning("Registrazione webhook Netatmo Enhanced: %s", self.registration)

    async def _handle(self, _hass, _webhook_id, request):
        if self._id is None:
            return web.Response(status=503)
        received = self.receiver.history.stamp()
        if request.headers.get("Content-Encoding", "identity").lower() != "identity":
            self.receiver.counts["unsupported_encoding"] += 1
            return web.Response(status=415)
        signatures = request.headers.getall("X-Netatmo-secret", [])
        if len(signatures) != 1:
            self.receiver.counts["invalid_signature"] += 1
            return web.Response(status=403)
        body = bytearray()
        try:
            async with asyncio.timeout(3):
                async for chunk in request.content.iter_chunked(16384):
                    if len(body) + len(chunk) > BODY_LIMIT:
                        self.receiver.counts["oversized"] += 1
                        return web.Response(status=413)
                    body.extend(chunk)
        except (TimeoutError, ClientError, OSError):
            self.receiver.counts["body_read_failed"] += 1
            return web.Response(status=400)
        return web.Response(status=self.receiver.accept(body, signatures[0], received))

    def _known_home(self, home_id):
        return any(item["home_id"] == home_id
                   for item in (self.coordinator.data or {}).values())

    def _signal(self, record):
        if self._id is None:
            record["refresh"] = "unloading"
            return
        if self.mode != MODE_REFRESH:
            record["refresh"] = "receive_only"
            return
        record["refresh"] = "coalesced" if self._pending else "queued"
        if len(self._pending) == EVENT_LIMIT:
            self._pending[0]["refresh"] = "coalesced_trace_evicted"
        self._pending.append(record)
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = self.entry.async_create_background_task(
                self.hass, self._refresh(), "Netatmo Enhanced webhook refresh"
            )

    async def _refresh(self):
        while self._pending:
            lock_wait_started = monotonic()
            async with self.coordinator.command_lock:
                now = monotonic()
                self._record_wait("command_lock_wait_ms", lock_wait_started, now)
                guard = self.coordinator.guard
                ready_at = max(guard.get("until", 0),
                               guard.get("refresh_started", 0) + MIN_REFRESH_SECONDS)
                delay = ready_at - now
                if delay <= 0:
                    batch = list(self._pending)
                    self._pending.clear()
                    timing = {}
                    for record in batch:
                        record["update"] = timing
                    try:
                        await self.coordinator.async_refresh_for("webhook", timing)
                        if timing.get("outcome") == "success":
                            self._refresh_count += 1
                        else:
                            self._refresh_errors += 1
                    except Exception:
                        self._refresh_errors += 1
                    finally:
                        timing.setdefault("outcome", "not_completed")
                        if timing["outcome"] == "not_completed":
                            timing["failure_reason"] = "update_not_completed"
                        if timing["outcome"] == "success":
                            for record in batch:
                                record["receipt_to_publication_ms"] = elapsed_ms(
                                    record["received_monotonic_s"], timing["completed_monotonic_s"]
                                )
                    continue
                wait_reason = "http_429_cooldown" if ready_at == guard.get("until") else "minimum_interval"
                for record in self._pending:
                    record["last_api_wait_reason"] = wait_reason
            try:
                await asyncio.sleep(delay)
            finally:
                self._record_wait("api_limit_wait_ms", now, min(ready_at, monotonic()))

    def _record_wait(self, field, started, ended):
        for record in self._pending:
            duration = elapsed_ms(max(started, record["received_monotonic_s"]), ended)
            record[field] = round(record.get(field, 0.0) + duration, 3)

    def diagnostics(self):
        return {
            "mode": self.mode,
            "registration": self.registration,
            "registration_details": dict(self.registration_details),
            "events": dict(self.receiver.counts) if self.receiver else {},
            "recent_events": deepcopy(list(self.receiver.history.records)) if self.receiver else [],
            "refresh_success": self._refresh_count,
            "refresh_failed": self._refresh_errors,
            "refresh_pending": bool(self._pending),
        }
