"""Home Assistant adapter for the experimental Netatmo webhook."""

import asyncio
from contextlib import suppress
import logging
from time import monotonic
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientTimeout, web
from homeassistant.components import webhook
from homeassistant.helpers.network import NoURLAvailableError

from .const import DOMAIN
from .webhook_core import (
    API_URL, BODY_LIMIT, MIN_REFRESH_SECONDS, MODE_KEY, MODE_OFF,
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
        self.receiver = None
        self._id = None
        self._registration_task = None
        self._refresh_task = None
        self._pending = False
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
        self.registration = await ensure_subscription(self.coordinator.async_webhook_request, url)
        if self.registration not in {"registered", "already_registered"}:
            _LOGGER.warning("Registrazione webhook Netatmo Enhanced: %s", self.registration)

    async def _handle(self, _hass, _webhook_id, request):
        if self._id is None:
            return web.Response(status=503)
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
        return web.Response(status=self.receiver.accept(body, signatures[0]))

    def _known_home(self, home_id):
        return any(item["home_id"] == home_id
                   for item in (self.coordinator.data or {}).values())

    def _signal(self):
        if self.mode != MODE_REFRESH or self._id is None:
            return
        self._pending = True
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = self.entry.async_create_background_task(
                self.hass, self._refresh(), "Netatmo Enhanced webhook refresh"
            )

    async def _refresh(self):
        while self._pending:
            async with self.coordinator.command_lock:
                guard = self.coordinator.guard
                ready_at = max(guard.get("until", 0),
                               guard.get("refresh_started", 0) + MIN_REFRESH_SECONDS)
                delay = ready_at - monotonic()
                if delay <= 0:
                    self._pending = False
                    try:
                        await self.coordinator.async_refresh()
                        if self.coordinator.last_update_success:
                            self._refresh_count += 1
                        else:
                            self._refresh_errors += 1
                    except Exception:
                        self._refresh_errors += 1
                    continue
            await asyncio.sleep(delay)

    def diagnostics(self):
        return {
            "mode": self.mode,
            "registration": self.registration,
            "events": dict(self.receiver.counts) if self.receiver else {},
            "refresh_success": self._refresh_count,
            "refresh_failed": self._refresh_errors,
            "refresh_pending": self._pending,
        }
