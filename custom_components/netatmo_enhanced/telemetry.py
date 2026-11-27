"""Bounded webhook and HTTP telemetry. Export only projected values."""

from collections import Counter, deque
from datetime import UTC, datetime
import hmac
from math import isfinite
import secrets
from time import monotonic

EVENT_LIMIT = 100
MAX_DEPTH = 8
MAX_ITEMS = 24
MAX_NODES = 160

CONTAINERS = frozenset({
    "extra_params", "data", "body", "payload", "event", "events", "device",
    "devices", "module", "modules", "room", "rooms", "home", "homes",
    "schedule", "schedules", "state", "status", "properties", "changes", "params",
})
IDENTIFIERS = frozenset({
    "id", "user_id", "home_id", "device_id", "module_id", "room_id",
    "schedule_id", "active_schedule_id", "gateway_id",
})
TEMPERATURES = frozenset({
    "temperature", "temperature_ambient", "temperature_setpoint", "current_temperature",
    "target_temperature", "setpoint_temperature", "measured_temperature",
    "therm_measured_temperature", "therm_setpoint_temperature",
    "cooling_measured_temperature", "cooling_setpoint_temperature", "therm_temperature",
    "ambient_temperature", "room_temperature",
})
TIMES = frozenset({
    "duration", "duration_seconds", "setpoint_end_time", "end_time",
    "therm_setpoint_end_time", "cooling_setpoint_end_time",
    "therm_setpoint_duration", "cooling_setpoint_duration",
})
FLAGS = frozenset({
    "heating", "cooling", "manual", "away", "frostguard", "reachable",
    "boiler_status", "cooler_status", "selected", "on", "enabled",
    "active", "heating_status", "cooling_status", "heating_active", "cooling_active",
})
ENUMS = {
    "type": frozenset({"BNS", "BAC", "therm", "cooling"}),
    "variant": frozenset({"BNS", "BAC"}),
    "source": frozenset({"netcom"}),
    "event_type": frozenset({"schedule", "therm_mode", "cooling_mode"}),
    "change": frozenset({"home_owner_added", "home_owner_removed"}),
    "push_type": frozenset({"webhook_activation", "webhook_deletion",
                            "home_event_changed", "device_event", "topology_changed"}),
}
MODES = frozenset({
    "home", "schedule", "manual", "away", "hg", "frostguard", "off", "auto", "max",
    "heating", "cooling", "idle", "on", "frost_guard",
})
MODE_FIELDS = frozenset({
    "mode", "room_mode", "home_mode", "therm_mode", "cooling_mode",
    "therm_setpoint_mode", "cooling_setpoint_mode", "setpoint_mode",
    "temperature_control_mode", "operating_mode", "hvac_mode", "hvac_action", "status",
    "operating_state", "therm_state", "cooling_state",
})
HUMIDITIES = frozenset({"humidity", "relative_humidity", "measured_humidity"})
COUNTERS = frozenset({"sequence_id", "correlation_id"})
PUBLIC_FIELDS = (
    CONTAINERS | IDENTIFIERS | TEMPERATURES | TIMES | FLAGS | MODE_FIELDS
    | HUMIDITIES | COUNTERS | ENUMS.keys()
)


def utc_now():
    return datetime.now(UTC)


def elapsed_ms(start, end):
    return round(max(0, end - start) * 1000, 3)


class EventHistory:
    def __init__(self, clock=monotonic, wall_clock=utc_now):
        self._clock = clock
        self._wall_clock = wall_clock
        self._key = secrets.token_bytes(32)
        self.records = deque(maxlen=EVENT_LIMIT)

    def stamp(self):
        return self._clock(), self._wall_clock()

    def _pseudonym(self, category, value):
        digest = hmac.digest(
            self._key, f"{category}:{value}".encode("utf-8", errors="surrogatepass"), "sha256"
        )
        return f"{category}_{digest[:12].hex()}"

    def _project(self, payload):
        remaining = MAX_NODES

        def visit(value, field, depth, known_path):
            nonlocal remaining
            remaining -= 1
            if depth >= MAX_DEPTH:
                return {"truncated": True}
            if isinstance(value, dict):
                result = {}
                known_path = known_path and (not field or field in CONTAINERS)
                for index, (key, child) in enumerate(value.items()):
                    if index >= MAX_ITEMS or remaining <= 0:
                        result["_truncated"] = True
                        break
                    known = known_path and key in PUBLIC_FIELDS
                    label = key if known else self._pseudonym("field", key)
                    result[label] = visit(child, key, depth + 1, known)
                return result
            if isinstance(value, list):
                items = []
                known_path = known_path and field in CONTAINERS
                for child in value[:MAX_ITEMS]:
                    if remaining <= 0:
                        break
                    items.append(visit(child, field, depth + 1, known_path))
                if len(items) < len(value):
                    items.append({"truncated": True})
                return items
            if known_path:
                if field in IDENTIFIERS and (
                    isinstance(value, str) and 0 < len(value) <= 256
                    or type(value) is int and -(2**63) <= value < 2**63
                ):
                    return self._pseudonym("id", value)
                if field in FLAGS and (type(value) is bool or type(value) is int and value in (0, 1)):
                    return value
                options = MODES if field in MODE_FIELDS else ENUMS.get(field, ())
                if isinstance(value, str) and value in options:
                    return value
                if (field in COUNTERS | TIMES and type(value) is int
                        and 0 <= value < 2**53):
                    return value
                if (field in TEMPERATURES | HUMIDITIES and type(value) in (int, float)
                        and -100 <= value <= 150 and isfinite(value)):
                    if field not in HUMIDITIES or 0 <= value <= 100:
                        return value
            return {"redacted_type": type(value).__name__}

        return visit(payload, "", 0, True)

    def record(self, received, kind, payload, duplicate, known_home, disposition):
        started, wall_time = received
        projected = self._project(payload)
        record = {
            "received_at_utc": wall_time.isoformat(),
            "received_timestamp_utc": wall_time.timestamp(),
            "received_monotonic_s": started,
            "event_type": kind,
            "hmac": "verified",
            "duplicate": duplicate,
            "known_home": known_home,
            "refresh": disposition,
            "payload": projected,
            "api_limit_wait_ms": 0.0,
        }
        if isinstance(projected, dict):
            extra = projected.get("extra_params")
            sequence = projected.get("sequence_id")
            if type(sequence) is not int and isinstance(extra, dict):
                sequence = extra.get("sequence_id")
            if type(sequence) is int:
                record["sequence_id"] = sequence
        record["processing_ms"] = elapsed_ms(started, self._clock())
        self.records.append(record)
        return record


class ApiMetrics:
    def __init__(self):
        self._rows = {}
        self._updates = {}

    def request(self, source, method, endpoint, duration_ms, status, outcome):
        row = self._rows.setdefault((source, method, endpoint), {
            "count": 0, "success": 0, "failed": 0, "http_429": 0,
            "status_codes": Counter(), "failures": Counter(),
            "duration_ms_total": 0.0, "duration_ms_max": 0.0,
        })
        row["count"] += 1
        row["duration_ms_total"] += duration_ms
        row["duration_ms_max"] = max(row["duration_ms_max"], duration_ms)
        if status is not None:
            row["status_codes"][str(status)] += 1
        if status == 429:
            row["http_429"] += 1
        if outcome == "success":
            row["success"] += 1
        else:
            row["failed"] += 1
            row["failures"][outcome] += 1

    def update(self, source, outcome):
        self._updates.setdefault(source, Counter())[outcome] += 1

    def snapshot(self):
        return {
            "http_requests_total": sum(row["count"] for row in self._rows.values()),
            "http_requests": [
                {"source": source, "method": method, "endpoint": endpoint, **row,
                 "status_codes": dict(row["status_codes"]), "failures": dict(row["failures"]),
                 "duration_ms_total": round(row["duration_ms_total"], 3)}
                for (source, method, endpoint), row in sorted(self._rows.items())
            ],
            "coordinator_updates": {source: dict(counts) for source, counts in self._updates.items()},
        }
