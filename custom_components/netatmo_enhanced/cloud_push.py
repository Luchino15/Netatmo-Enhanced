"""Interpret authenticated Netatmo events against the current API inventory."""

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from time import monotonic

from .model import number, project

UNSEQUENCED_WINDOW = 60
RESET_WINDOW = 120
MAX_TRACKED_FIELDS = 4096
MAX_RECENT_VALUES = 8
BOOST_FRAGMENT_WINDOW = 60
ROOM_FIELDS = {
    "therm_measured_temperature": "current_temperature",
    "humidity": "humidity",
    **{
        f"{prefix}_setpoint_{suffix}": f"{prefix}_setpoint_{suffix}"
        for prefix in ("therm", "cooling")
        for suffix in ("temperature", "mode", "end_time")
    },
}
MODULE_FIELDS = ("boiler_status", "cooler_status")
HOME_FIELDS = {
    "temperature_control_mode": "season",
    "therm_mode": "therm_mode",
    "cooling_mode": "cooling_mode",
    "schedule_id": "schedule_id",
}
HOME_MODES = ("schedule", "away", "hg", "off")
ROOM_MODES = ("home", "manual", "hg")
BOOST_MODE = "max"
SETPOINT_FIELDS = {
    f"{prefix}_setpoint_mode": (
        f"{prefix}_setpoint_temperature", f"{prefix}_setpoint_end_time",
    )
    for prefix in ("therm", "cooling")
}
SETPOINT_DETAILS = {
    name: mode_field for mode_field, fields in SETPOINT_FIELDS.items() for name in fields
}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _sources(payload):
    return payload, _mapping(payload.get("extra_params"))


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 256


def home_identity(payload):
    """Resolve the four observed nesting paths without trusting one over another."""
    candidates = [
        source[key]
        for location in _sources(payload)
        for source, key in ((location, "home_id"), (_mapping(location.get("home")), "id"))
        if key in source
    ]
    if not candidates or all(value is None for value in candidates):
        return None, "missing_home_id"
    if any(not _identifier(value) for value in candidates):
        return None, "invalid_home_id"
    if any(value != candidates[0] for value in candidates):
        return None, "conflicting_home_id"
    return candidates[0], None


def _valid(field_name, value):
    if field_name == "season":
        return value in ("heating", "cooling")
    if field_name in ("therm_mode", "cooling_mode"):
        return value in HOME_MODES
    if field_name in MODULE_FIELDS:
        return type(value) is bool
    if field_name == "schedule_id":
        return _identifier(value)
    if field_name.endswith("_end_time"):
        return value is None or type(value) is int and 0 <= value < 2**53
    if field_name.endswith("_mode"):
        return (value in ROOM_MODES or field_name in SETPOINT_FIELDS and value == BOOST_MODE
                or field_name == "cooling_setpoint_mode" and value == "off")
    if field_name in SETPOINT_DETAILS and value is None:
        # Authorization of a null target requires the corresponding room mode.
        return True
    if field_name == "humidity":
        return number(value) is not None and 0 <= value <= 100
    return number(value) is not None and -100 <= value <= 150


def _sequence(source):
    if "sequence_id" not in source:
        return None
    value = source["sequence_id"]
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("Invalid sequence")
    return value


def _room_identifier(value):
    return _identifier(value) or type(value) is int and 0 <= value < 2**53


def _member_scope(payload, member_type):
    field_name = f"{member_type}s"
    for source in _sources(payload):
        home = _mapping(source.get("home"))
        if field_name in home:
            yield home[field_name]


def _home_fields(payload, reasons):
    candidates = {}
    schedule_event = False
    for source in _sources(payload):
        home = _mapping(source.get("home"))
        for fields in (source, home):
            for original, normalized in HOME_FIELDS.items():
                if original in fields:
                    candidates.setdefault(normalized, []).append(fields[original])
        if source.get("event_type") in ("therm_mode", "cooling_mode") and "mode" in source:
            candidates.setdefault(source["event_type"], []).append(source["mode"])
        schedule_event |= source.get("event_type") == "schedule"
    updates = {}
    for name, values in candidates.items():
        if any(not _valid(name, value) or value != values[0] for value in values):
            reasons.add("invalid_fields")
        else:
            updates[name] = values[0]
    if (schedule_event and "schedule_id" not in candidates) or not (updates or reasons):
        reasons.add("missing_fields")
    return updates


@dataclass(frozen=True)
class PushResult:
    data: dict
    accepted: dict
    changed: bool
    reasons: frozenset
    outcomes: frozenset
    partial: bool = False


@dataclass
class _FieldOrder:
    sequence: int | None = None
    value: object = None
    recent: deque = field(default_factory=lambda: deque(maxlen=MAX_RECENT_VALUES))
    candidate: tuple | None = None
    retired: tuple | None = None
    boost_floor: int | None = None


class PushState:
    """Apply partial updates with ordering scoped to each home, target and field."""

    def __init__(self, clock=monotonic):
        self._clock = clock
        self._orders = OrderedDict()
        self._boost_nulls = OrderedDict()

    def _decision(self, key, sequence, value, current, now, reasons, remember_noop):
        order = self._orders.get(key)
        if sequence is None:
            if value == current:
                if remember_noop and (order is None or order.sequence is None):
                    self._remember(key, sequence, value, now)
                return "unchanged"
            if order is not None and (
                order.sequence is not None
                or any(old_value == value and now - at < UNSEQUENCED_WINDOW
                       for old_value, at in order.recent)
            ):
                reasons.add("unsequenced_reversal")
                return "ambiguous"
        elif order is not None and order.sequence is not None:
            if sequence == order.sequence:
                if value == order.value:
                    return "duplicate"
                reasons.add("sequence_conflict")
                return "ambiguous"
            if sequence < order.sequence:
                if value != current:
                    order.candidate = (sequence, value, now)
                    reasons.add("sequence_out_of_order")
                return "obsolete"
            if order.retired is not None:
                old_high, until = order.retired
                if now < until and order.sequence + 1 < sequence <= old_high:
                    if value != current:
                        order.candidate = (sequence, value, now)
                        reasons.add("sequence_reset_uncertain")
                    return "ambiguous"
        return "accepted"

    def _remember(self, key, sequence, value, now):
        order = self._orders.get(key)
        if order is None:
            order = self._orders[key] = _FieldOrder()
        self._orders.move_to_end(key)
        if sequence is None:
            if order.recent and order.recent[-1][0] == value:
                order.recent.pop()
            order.recent.append((value, now))
        else:
            order.sequence = sequence
            order.value = value
            order.candidate = None
        if len(self._orders) > MAX_TRACKED_FIELDS:
            self._orders.popitem(last=False)

    def reconciled(self, data, started=None):
        """Rebase a suspect sequence only with an independent matching API read."""
        current = {}
        for mid, state in data.items():
            hid = state["home_id"]
            current[(hid, "home", hid)] = state
            current[(hid, "room", state["room_id"])] = state
            current[(hid, "module", mid)] = state
        now = self._clock()
        for key, order in list(self._orders.items()):
            state = current.get(key[:3])
            if state is None:
                del self._orders[key]
            elif order.candidate is not None:
                sequence, value, at = order.candidate
                if started is not None and at > started:
                    continue
                if state.get(key[3]) == value:
                    if sequence < order.sequence:
                        old_high = order.sequence
                        if order.retired is not None:
                            old_high = max(old_high, order.retired[0])
                        order.retired = (old_high, now + RESET_WINDOW)
                    if key[3] in SETPOINT_FIELDS and (
                        order.boost_floor is not None or value == BOOST_MODE
                    ):
                        order.boost_floor = sequence
                    order.sequence = sequence
                    order.value = value
                order.candidate = None
        for key, (_, received) in list(self._boost_nulls.items()):
            if key[:3] not in current or now - received >= BOOST_FRAGMENT_WINDOW:
                del self._boost_nulls[key]

    def apply(self, data, payload):
        """Return a copy-on-write state and the independently verified field updates."""
        hid, problem = home_identity(payload)
        members = {mid: state for mid, state in (data or {}).items()
                   if hid is not None and state.get("home_id") == hid}
        if not members:
            return PushResult(data, {}, False, frozenset(),
                              frozenset({problem or "unrecognized"}))
        kind = payload.get("push_type")
        if kind == "topology_changed":
            return PushResult(data, {}, False, frozenset({"topology_changed"}), frozenset())

        now = self._clock()
        result = data.copy()
        accepted = {}
        reasons = set()
        outcomes = set()
        partial = False
        if kind == "home_event_changed":
            self._apply_home(payload, hid, members, result, accepted, now, reasons, outcomes)
        elif kind == "device_event":
            partial = self._apply_device(
                payload, hid, members, result, accepted, now, reasons, outcomes
            )
        else:
            outcomes.add("unrecognized")
        if reasons and not outcomes:
            outcomes.add("ambiguous")
        changed = any(result[mid] is not members[mid] for mid in accepted)
        return PushResult(
            result if changed else data, accepted, changed,
            frozenset(reasons), frozenset(outcomes), partial and changed,
        )

    def _apply_home(self, payload, hid, members, result, accepted, now, reasons, outcomes):
        updates = _home_fields(payload, reasons)
        schedule_id = updates.pop("schedule_id", None)
        companions = {}
        incidental_schedules = set()
        if schedule_id is not None:
            state = next(iter(members.values()))
            season = updates.get("season", state.get("season"))
            matching_seasons = [
                choice for choice in ("heating", "cooling")
                if schedule_id in state.get(f"{choice}_schedule_names", {})
            ]
            if "season" not in updates and len(matching_seasons) == 1:
                if season != matching_seasons[0] and not (
                    "therm_mode" in updates or "cooling_mode" in updates
                ):
                    reasons.add("missing_fields")
                season = matching_seasons[0]
            if season in matching_seasons:
                selection = f"{season}_schedule_id"
                updates[selection] = schedule_id
                companions[selection] = {
                    f"{season}_schedule": state[f"{season}_schedule_names"][schedule_id],
                }
                if not any(source.get("event_type") == "schedule" for source in _sources(payload)):
                    # Embedded schedules describe the mode/season event. When
                    # unchanged, they do not evidence a schedule transition.
                    incidental_schedules.add(selection)
            else:
                reasons.add("unknown_schedule")
        if updates:
            self._merge_fields(
                (hid, "home", hid), members, result, accepted,
                {name: (None, value) for name, value in updates.items()},
                companions, now, reasons, outcomes,
                ignore_noop=incidental_schedules,
            )
        if "season" in updates and any(
            result[mid].get("target_temperature") is None
            or result[mid].get("home_mode") is None
            for mid in members
        ):
            reasons.add("missing_fields")

    def _apply_device(self, payload, hid, members, result, accepted, now, reasons, outcomes):
        room_members = {}
        for mid, state in members.items():
            room_members.setdefault(state["room_id"], {})[mid] = state
        partial = False
        recognized = False
        for scope, targets, patterns in (
            ("room", room_members, ROOM_FIELDS),
            ("module", {mid: {mid: state} for mid, state in members.items()},
             {name: name for name in MODULE_FIELDS}),
        ):
            fields = self._member_updates(payload, scope, targets, patterns, reasons)
            for target, updates in fields.items():
                if not updates:
                    continue
                recognized = True
                target_members = targets[target]
                filtered = self._merge_fields(
                    (hid, scope, target), target_members, result, accepted,
                    updates, {}, now, reasons, outcomes,
                )
                if not filtered:
                    continue
                partial |= len(updates) < len(patterns)
                if (scope == "room" and "therm_setpoint_mode" in filtered
                        and filtered["therm_setpoint_mode"] == "hg"
                        and any(state.get("therm_mode") != "hg" and
                                state.get("therm_setpoint_mode") != "hg"
                                for state in target_members.values())):
                    reasons.add("local_frost_guard")
        if not recognized and reasons <= {"missing_fields"}:
            outcomes.add("unrecognized")
        return partial

    def _member_updates(self, payload, scope, targets, patterns, reasons):
        updates = {}
        conflicts = set()
        for container in _member_scope(payload, scope):
            if not isinstance(container, list):
                reasons.add("invalid_fields")
                continue
            for source in container:
                if not isinstance(source, dict):
                    reasons.add("invalid_fields")
                    continue
                target = source.get("id")
                valid_id = _room_identifier(target) if scope == "room" else _identifier(target)
                if not valid_id or target not in targets:
                    continue
                if scope == "module" and source.get("type", "BNS") != "BNS":
                    reasons.add("invalid_fields")
                    continue
                try:
                    sequence = _sequence(source)
                except ValueError:
                    reasons.add("invalid_fields")
                    continue
                fields = updates.setdefault(target, {})
                for name, normalized in patterns.items():
                    if name not in source:
                        continue
                    value = source[name]
                    pair = (target, normalized)
                    if pair in conflicts:
                        continue
                    if not _valid(normalized, value) or (
                        normalized in fields and fields[normalized] != (sequence, value)
                    ):
                        reasons.add("invalid_fields")
                        conflicts.add(pair)
                        fields.pop(normalized, None)
                    else:
                        fields[normalized] = (sequence, value)
                if not fields and not reasons:
                    reasons.add("missing_fields")
        if scope == "module":
            for target, fields in updates.items():
                if (any((target, name) in conflicts for name in MODULE_FIELDS)
                        or len(fields) == len(MODULE_FIELDS)
                        and all(fields[name][1] for name in MODULE_FIELDS)):
                    reasons.add("invalid_fields")
                    fields.clear()
        return updates

    def _with_held_boost_details(self, scope, updates, now):
        """Pair a detail fragment with a matching explicit Boost transition."""
        for mode_field, (target, expiry) in SETPOINT_FIELDS.items():
            if mode_field not in updates:
                continue
            sequence, mode = updates[mode_field]
            detail = target if mode == BOOST_MODE else expiry
            held = self._boost_nulls.get((*scope, detail))
            if (detail not in updates and held is not None and sequence == held[0]
                    and now - held[1] < BOOST_FRAGMENT_WINDOW):
                updates = {**updates, detail: (sequence, None)}
        return updates

    def _hold_boost_null(self, key, sequence, now):
        held = self._boost_nulls.get(key)
        if (sequence is not None and (
            held is None or sequence >= held[0] or now - held[1] >= BOOST_FRAGMENT_WINDOW
        )):
            self._boost_nulls[key] = (sequence, now)
            self._boost_nulls.move_to_end(key)
            if len(self._boost_nulls) > MAX_TRACKED_FIELDS:
                self._boost_nulls.popitem(last=False)

    def _boost_field(self, scope, name, updates, filtered, previous, now):
        """Check the mode and ordering context of a temporary setpoint field."""
        sequence, value = updates[name]
        mode_field = SETPOINT_DETAILS[name]
        old_mode = previous.get(mode_field)
        current_mode = filtered.get(mode_field, old_mode)
        order = self._orders.get((*scope, mode_field))
        floor = order.boost_floor if order is not None else None
        incoming_mode = updates.get(mode_field)
        if mode_field in filtered and (old_mode == BOOST_MODE) != (current_mode == BOOST_MODE):
            floor = incoming_mode[0]
        if floor is not None:
            if sequence is None and value != previous.get(name):
                return "ambiguous", "unsequenced_reversal"
            if sequence is not None and sequence < floor:
                return "obsolete", "sequence_out_of_order"
        target, expiry = SETPOINT_FIELDS[mode_field]
        if name == target and value is None:
            if current_mode == BOOST_MODE and (
                incoming_mode is None or incoming_mode[1] == BOOST_MODE
            ):
                return None, None
            if incoming_mode is None:
                self._hold_boost_null((*scope, name), sequence, now)
            return "ambiguous", "boost_context_missing"
        if name == expiry and current_mode == BOOST_MODE and not value:
            if incoming_mode is None and value is None:
                self._hold_boost_null((*scope, name), sequence, now)
            return "ambiguous", "boost_details_missing"
        return None, None

    def _remember_boost_transition(self, scope, updates, filtered, previous, reasons):
        for mode_field, details in SETPOINT_FIELDS.items():
            for name in details:
                if name in filtered:
                    self._boost_nulls.pop((*scope, name), None)
            old_boost = previous.get(mode_field) == BOOST_MODE
            boost = filtered.get(mode_field, previous.get(mode_field)) == BOOST_MODE
            expiry = details[1]
            if mode_field in filtered:
                sequence = updates[mode_field][0]
                order = self._orders[(*scope, mode_field)]
                if old_boost != boost:
                    order.boost_floor = sequence
                    self._associate_boost_details(scope, details, sequence, filtered, previous,
                                                  boost, reasons)
                elif boost and order.boost_floor is None:
                    order.boost_floor = sequence
            if expiry in filtered:
                prefix = mode_field.removesuffix("_setpoint_mode")
                filtered[f"{prefix}_end_time_is_boost"] = boost

    def _associate_boost_details(self, scope, details, sequence, filtered, previous, boost, reasons):
        for name in details:
            if name in filtered:
                continue
            order = self._orders.get((*scope, name))
            if (sequence is None or order is None or order.sequence != sequence
                    or previous.get(name) != order.value):
                reasons.add("boost_details_missing")
            elif name == details[1]:
                if boost and not order.value:
                    reasons.add("boost_details_missing")
                else:
                    # The deadline was reported first in the same update.
                    filtered[name] = order.value

    def _merge_fields(
        self, scope, members, result, accepted, updates, companions, now, reasons, outcomes,
        *, ignore_noop=(),
    ):
        filtered = {}
        boost_scope = scope[1] == "room"
        if boost_scope:
            updates = self._with_held_boost_details(scope, updates, now)
        previous = next(iter(members.values()))
        # Resolve the mode first, so obsolete/conflicting modes cannot legalize a null.
        ordered_updates = sorted(updates, key=lambda name: name not in SETPOINT_FIELDS)
        for name in ordered_updates:
            sequence, value = updates[name]
            current = next((state.get(name) for state in members.values()
                            if state.get(name) != value), value)
            if boost_scope and name in SETPOINT_DETAILS:
                outcome, reason = self._boost_field(
                    scope, name, updates, filtered, previous, now
                )
                if outcome is not None:
                    outcomes.add(outcome)
                    if outcome == "ambiguous" or current != value:
                        reasons.add(reason)
                    continue
            decision = self._decision(
                (*scope, name), sequence, value, current, now, reasons, name not in ignore_noop
            )
            if decision == "accepted":
                filtered[name] = value
            else:
                outcomes.add(decision)
        if not filtered:
            return filtered
        if scope[1] == "module":
            for mid in members:
                flags = tuple(filtered.get(name, result[mid].get(name)) for name in MODULE_FIELDS)
                if any(type(flag) is not bool for flag in flags) or all(flags):
                    reasons.add("invalid_fields")
                    outcomes.add("ambiguous")
                    return {}
        for name, value in tuple(filtered.items()):
            self._remember((*scope, name), updates[name][0], value, now)
            filtered.update(companions.get(name, {}))
        if boost_scope:
            self._remember_boost_transition(scope, updates, filtered, previous, reasons)
        for mid in members:
            existing = result[mid]
            accepted.setdefault(mid, set()).update(filtered)
            if any(existing.get(name) != value for name, value in filtered.items()):
                result[mid] = project({**existing, **filtered})
                outcomes.add("applied")
            else:
                outcomes.add("unchanged")
        return filtered
