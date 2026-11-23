"""What a scope channel is connected to. Pin hints are from the 2008 Vortec 5.3 E38 ECM pinout (lt1swap.com):
J1/J2 are the ECM connectors; sensor-side letters are the sensor connector's pins."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Role:
    key: str
    name: str
    pins: str
    kind: str              # crank: crank checks · ref: 5V sag check · low: ground-lift check · basic: plot + numbers
    numbered: bool = False


ROLES = {r.key: r for r in (
    Role("crank_ecm", "Crank signal at ECM", "J2-68", "crank"),
    Role("crank_sensor", "Crank signal at sensor", "CKP A", "crank"),
    Role("crank_ref", "Crank 5V reference", "J2-2 / CKP C", "ref"),
    Role("crank_low", "Crank low reference", "J2-69 / CKP B", "low"),
    Role("cam", "Cam signal", "J2-64 / CMP C", "basic"),
    Role("cam_ref", "Cam 5V reference", "J2-44 / CMP A", "ref"),
    Role("cam_low", "Cam low reference", "J2-66 / CMP B", "low"),
    Role("est", "Ignition control (EST)", "IC 1-8: J2-70/73/77/76/75/74/72/71", "basic", True),
    Role("coil_low", "Coil low reference", "1 odd bank J2-78 / 2 even bank J2-79", "low", True),
    Role("injector", "Injector control (low side)", "inj 1-8: J2-20/37/38/39/18/19/40/17", "basic", True),
    Role("ecm_ground", "ECM ground", "J1-73", "low"),
    Role("ecm_ign", "ECM ignition 1 voltage", "J1-19 / J1-47", "basic"),
    Role("ecm_batt", "ECM battery voltage", "J1-20", "basic"),
    Role("tp", "Throttle position", "1 J2-65 / 2 J2-63", "basic", True),
    Role("tac", "Throttle actuator motor", "1 J2-6 / 2 J2-5", "basic", True),
    Role("app", "Pedal position", "1 J1-29 / 2 J1-32", "basic", True),
    Role("map", "MAP signal", "J2-58", "basic"),
    Role("fuel_pump_relay", "Fuel pump relay control", "J1-50", "basic"),
    Role("starter_relay", "Starter enable relay control", "J1-52", "basic"),
    Role("other", "Other", "", "basic"),
)}

# How every capture was wired before roles existed (and still the default channel table).
LEGACY_ROLES: dict[int, tuple[str, int | None]] = {
    1: ("crank_ecm", None), 2: ("crank_sensor", None), 3: ("crank_ref", None), 4: ("crank_low", None)}


def role_label(key: str, num: int | None) -> str:
    r = ROLES[key]
    if key == "coil_low" and num in (1, 2):
        return f"{r.name} ({'odd' if num == 1 else 'even'} bank)"
    return f"{r.name} {num}" if r.numbered and num else r.name


def parse_role(text: str) -> tuple[str, int | None]:
    """'cam' -> ('cam', None); 'est:3' -> ('est', 3). ValueError for an unknown role or a bad number."""
    key, _, n = (text or "").strip().lower().partition(":")
    if key not in ROLES:
        raise ValueError(f"Unknown role {text!r}. Roles: {', '.join(ROLES)}.")
    if not n:
        return key, None
    if not ROLES[key].numbered:
        raise ValueError(f"Role {key!r} takes no number.")
    if not n.isdigit() or not 1 <= int(n) <= 8:
        raise ValueError(f"Role number must be 1 to 8, not {n!r}.")
    return key, int(n)


def roles_meta(channels) -> dict:
    """{"n": {"role", "num"}} for the enabled channels of a channel table, as saved with a capture."""
    return {str(c.n): {"role": c.role, "num": c.num} for c in channels if c.enabled}


def legacy_meta(channel_numbers) -> dict:
    """The pre-roles layout for the channels a capture has (channels beyond CH4 are 'other')."""
    out = {}
    for n in sorted(channel_numbers):
        key, num = LEGACY_ROLES.get(int(n), ("other", None))
        out[str(n)] = {"role": key, "num": num}
    return out


def check_role(key: str, num: int | None) -> tuple[str, int | None]:
    """Validate a (role, number) pair the way parse_role validates text."""
    return parse_role(f"{key}:{num}" if num is not None else key)


def roles_of(data) -> dict[int, tuple[str, int | None]]:
    """{channel: (role, num)} for a capture: its saved or corrected roles, else the legacy layout."""
    meta = (getattr(data, "meta", None) or {}).get("roles") or legacy_meta(data.channels)
    out = {}
    for n in data.channels:
        r = meta.get(str(n)) or {"role": "other", "num": None}
        key = r.get("role") if r.get("role") in ROLES else "other"
        out[int(n)] = (key, r.get("num"))
    return out
