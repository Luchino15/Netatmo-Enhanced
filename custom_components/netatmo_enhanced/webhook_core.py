"""Protocol and refresh scheduling for Netatmo Connect webhooks."""

import asyncio
from collections import Counter, OrderedDict
import hashlib
import hmac
import json
import re
import time

from .telemetry import EventHistory

API_URL = "https://api.netatmo.com/webhooks/v1"
BODY_LIMIT = 256 * 1024
DEDUP_SECONDS = 900
DEDUP_LIMIT = 2048
MIN_REFRESH_SECONDS = 60
MODE_KEY = "webhook_mode"
MODE_OFF = "off"
MODE_RECEIVE = "receive"
MODE_REFRESH = "refresh"
EVENTS = frozenset({
    "webhook_activation", "webhook_deletion", "device_event",
    "home_event_changed", "topology_changed",
})
REFRESH_EVENTS = EVENTS - {"webhook_activation", "webhook_deletion"}


def _listed_urls(payload):
    if (not isinstance(payload, dict) or payload.get("error")
            or payload.get("status") not in (None, "ok")):
        return None
    entries = payload.get("body")
    if not isinstance(entries, list):
        return None
    if any(not isinstance(item, dict) or not isinstance(item.get("url"), str)
           for item in entries):
        return None
    return {item["url"] for item in entries}


async def ensure_subscription(request, callback_url, *, sleep=asyncio.sleep, details=None):
    """Check before appending. Each attempt issues at most one POST."""
    phase = "get"
    try:
        status, payload = await request("GET")
        if status != 200:
            return f"get_http_{status}"
        urls = _listed_urls(payload)
        if urls is None:
            return "get_unexpected_response"
        if callback_url in urls:
            return "already_registered"

        phase = "post"
        status, payload = await request("POST", {"url": callback_url})
        if details is not None:
            details["post_http_status"] = status
        if not 200 <= status < 300:
            return f"post_http_{status}"
        urls = _listed_urls(payload)
        if details is not None:
            if urls is None:
                details["post_response"] = (
                    "empty_or_unreadable" if payload is None else "unexpected_schema"
                )
            else:
                details["post_response"] = "url_present" if callback_url in urls else "url_absent"
        if urls is not None and callback_url in urls:
            return "registered"

        phase = "verify"
        await sleep(2)
        status, payload = await request("GET")
        if status != 200:
            return f"verify_http_{status}"
        urls = _listed_urls(payload)
        if urls is None:
            return "verify_unexpected_response"
        return "registered_verified" if callback_url in urls else "post_unconfirmed"
    except Exception:
        return f"{phase}_request_failed"


class Receiver:
    """Authenticate, classify and coalesce webhook notifications."""

    def __init__(self, secret, known_home, signal_refresh, clock=time.monotonic):
        if not isinstance(secret, str) or not secret:
            raise ValueError("Netatmo client secret required")
        self._key = secret.encode("utf-8")
        self._known_home = known_home
        self._signal = signal_refresh
        self._clock = clock
        self._seen = OrderedDict()
        self.counts = Counter()
        self.history = EventHistory(clock)

    def accept(self, raw, signature, received=None):
        if received is None:
            received = self.history.stamp()
        if len(raw) > BODY_LIMIT:
            self.counts["oversized"] += 1
            return 413
        expected = hmac.digest(self._key, raw, "sha256").hex()
        if not isinstance(signature, str) or not re.fullmatch("[0-9a-f]{64}", signature):
            self.counts["invalid_signature"] += 1
            return 403
        if not hmac.compare_digest(expected, signature):
            self.counts["invalid_signature"] += 1
            return 403
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            data = None
        if (not isinstance(data, dict)
                or not isinstance(data.get("push_type"), str)
                or not isinstance(data.get("user_id"), str)
                or not data["user_id"]):
            self.counts["invalid_event"] += 1
            self.history.record(received, "malformed", data, None, None, "malformed_event")
            return 400

        kind = data["push_type"]
        home_id = data.get("home_id")
        known_home = self._known_home(home_id) if isinstance(home_id, str) else None
        if kind not in EVENTS:
            self.counts["unknown_event"] += 1
            self.history.record(received, "unknown", data, None, known_home, "unknown_event")
            return 200
        now = self._clock()
        while self._seen and next(iter(self._seen.values())) <= now - DEDUP_SECONDS:
            self._seen.popitem(last=False)
        digest = hashlib.sha256(raw).digest()
        if digest in self._seen:
            self.counts["duplicate"] += 1
            self.history.record(received, kind, data, True, known_home, "duplicate")
            return 200
        self._seen[digest] = now
        if len(self._seen) > DEDUP_LIMIT:
            self._seen.popitem(last=False)

        self.counts[kind] += 1
        disposition = "registration_event"
        if kind in REFRESH_EVENTS and not known_home:
            self.counts["unmatched_home"] += 1
            disposition = "unmatched_home" if known_home is False else "missing_home_id"
        elif kind in REFRESH_EVENTS:
            disposition = "eligible"
        record = self.history.record(received, kind, data, False, known_home, disposition)
        if disposition == "eligible":
            self._signal(record)
        return 200
