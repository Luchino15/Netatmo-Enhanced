"""Pure, read-only interpretation of the Netatmo API responses."""
from math import isfinite

def objects(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

def number(value):
    return value if type(value) is int or type(value) is float and isfinite(value) else None

def discover(homes):
    """Find BNS devices without hard-coded account identifiers."""
    result = {}
    for home in objects(homes):
        hid = home.get("id")
        if not isinstance(hid, str):
            continue
        rooms = {r.get("id"): r for r in objects(home.get("rooms"))}
        for module in objects(home.get("modules")):
            rid, mid = module.get("room_id"), module.get("id")
            if module.get("type") != "BNS" or rid not in rooms or not isinstance(mid, str):
                continue
            result[(hid, mid)] = (home, rooms[rid], module)
    return result

def snapshot(topology, status, room, module):
    """Separate the two seasons; do not invent a missing setpoint."""
    rid, mid = room["id"], module["id"]
    r = next((x for x in objects(status.get("rooms")) if x.get("id") == rid), {})
    m = next((x for x in objects(status.get("modules")) if x.get("id") == mid), {})
    season = status.get("temperature_control_mode", topology.get("temperature_control_mode"))
    if season not in ("heating", "cooling"):
        season = None
    schedules = {}
    for kind in ("therm", "cooling"):
        selected = [x for x in objects(topology.get("schedules"))
                    if x.get("type") == kind and x.get("selected") is True]
        schedules[kind] = selected[0] if len(selected) == 1 else {}
    humidity = number(r.get("humidity"))
    if humidity is not None and not 0 <= humidity <= 100:
        humidity = None
    state = {
        "home_id": topology["id"],
        "room_id": rid,
        "boiler_status": m.get("boiler_status"),
        "cooler_status": m.get("cooler_status"),
        "season": season,
        "current_temperature": number(r.get("therm_measured_temperature")),
        "humidity": humidity,
        "reachable": m.get("reachable") if type(m.get("reachable")) is bool else None,
        "room_name": room.get("name") or "Smarther",
    }
    for kind, schedule_season in (("therm", "heating"), ("cooling", "cooling")):
        room_mode = r.get(f"{kind}_setpoint_mode")
        state.update({
            f"{kind}_mode": status.get(f"{kind}_mode", topology.get(f"{kind}_mode")),
            f"{kind}_setpoint_mode": room_mode,
            f"{kind}_setpoint_temperature": number(r.get(f"{kind}_setpoint_temperature")),
            f"{kind}_setpoint_end_time": number(r.get(f"{kind}_setpoint_end_time")),
            f"{kind}_end_time_is_boost": room_mode == "max",
            f"{schedule_season}_schedule": schedules[kind].get("name"),
            f"{schedule_season}_schedule_id": schedules[kind].get("id"),
            f"{schedule_season}_schedule_names": schedule_names(topology, kind),
            f"{schedule_season}_schedule_options": schedule_options(topology, kind),
        })
    return project(state)


def project(state):
    """Derive the existing entity view from the active season's stored fields."""
    season = state.get("season")
    prefix = {"heating": "therm", "cooling": "cooling"}.get(season)
    room_mode = state.get(f"{prefix}_setpoint_mode")
    home_mode = state.get(f"{prefix}_mode")
    mode = room_mode
    if mode is None or (mode in ("home", "schedule") and home_mode in ("away", "hg", "off")):
        mode = home_mode
    target = state.get(f"{prefix}_setpoint_temperature")
    expiry = state.get(f"{prefix}_setpoint_end_time")
    boost = room_mode == "max"
    if boost:
        target = None
    if boost != bool(state.get(f"{prefix}_end_time_is_boost")):
        expiry = None
    return {
        **state,
        "schedule_options": state.get(f"{season}_schedule_options", {}),
        "target_temperature": target,
        "mode": mode,
        "room_mode": room_mode,
        "home_mode": home_mode,
        "active_schedule": state.get(f"{season}_schedule"),
        "active_schedule_id": state.get(f"{season}_schedule_id"),
        "setpoint_end_time": expiry,
    }


def schedule_names(topology, prefix):
    return {
        entry["id"]: entry["name"]
        for entry in objects(topology.get("schedules"))
        if (
            entry.get("type") == prefix
            and isinstance(entry.get("name"), str)
            and isinstance(entry.get("id"), str)
        )
    }


def schedule_options(topology, prefix):
    entries = [(s.get("name"), s.get("id")) for s in objects(topology.get("schedules")) if s.get("type") == prefix and isinstance(s.get("name"), str) and isinstance(s.get("id"), str)]
    return {name if sum(n == name for n, _ in entries) == 1 else f"{name} ({sid})": sid for name, sid in entries}
