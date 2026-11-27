"""Pure, read-only interpretation of the Netatmo API responses."""
from math import isfinite

def objects(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

def number(value):
    return value if type(value) in (int, float) and isfinite(value) else None

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
    prefix = {"heating": "therm", "cooling": "cooling"}.get(season)
    schedules = {}
    for kind in ("therm", "cooling"):
        selected = [x for x in objects(topology.get("schedules"))
                    if x.get("type") == kind and x.get("selected") is True]
        schedules[kind] = selected[0] if len(selected) == 1 else {}
    mode = r.get(f"{prefix}_setpoint_mode") if prefix else None
    room_mode = mode
    home_mode = status.get(f"{prefix}_mode", topology.get(f"{prefix}_mode")) if prefix else None
    if mode is None or (mode in ("home", "schedule") and home_mode in ("away", "hg", "off")):
        mode = home_mode
    humidity = number(r.get("humidity"))
    if humidity is not None and not 0 <= humidity <= 100:
        humidity = None
    return {
        "home_id": topology["id"],
        "room_id": rid,
        "schedule_options": schedule_options(topology, prefix),
        "boiler_status": m.get("boiler_status"),
        "cooler_status": m.get("cooler_status"),
        "season": season,
        "current_temperature": number(r.get("therm_measured_temperature")),
        "target_temperature": number(r.get(f"{prefix}_setpoint_temperature")) if prefix else None,
        "humidity": humidity,
        "mode": mode,
        "room_mode": room_mode,
        "home_mode": home_mode,
        "heating_schedule": schedules["therm"].get("name"),
        "cooling_schedule": schedules["cooling"].get("name"),
        "active_schedule": schedules[prefix].get("name") if prefix else None,
        "active_schedule_id": schedules[prefix].get("id") if prefix else None,
        "setpoint_end_time": number(r.get(f"{prefix}_setpoint_end_time")) if prefix else None,
        "reachable": m.get("reachable") if type(m.get("reachable")) is bool else None,
        "room_name": room.get("name") or "Smarther",
    }


def schedule_options(topology, prefix):
    entries = [(s.get("name"), s.get("id")) for s in objects(topology.get("schedules")) if s.get("type") == prefix and isinstance(s.get("name"), str) and isinstance(s.get("id"), str)]
    return {name if sum(n == name for n, _ in entries) == 1 else f"{name} ({sid})": sid for name, sid in entries}
