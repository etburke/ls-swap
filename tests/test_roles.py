import pytest

from crank.roles import LEGACY_ROLES, ROLES, parse_role, role_label


def test_every_role_has_kind_and_pins():
    assert set(ROLES) >= {"crank_ecm", "crank_sensor", "crank_ref", "crank_low", "cam", "cam_ref", "cam_low", "est",
                          "coil_low", "injector", "ecm_ground", "ecm_ign", "ecm_batt", "tp", "tac", "app", "map",
                          "fuel_pump_relay", "starter_relay", "other"}
    assert all(r.kind in ("crank", "ref", "low", "basic") for r in ROLES.values())
    assert ROLES["crank_ecm"].pins == "J2-68" and ROLES["cam"].kind == "basic" and ROLES["cam_ref"].kind == "ref"
    assert ROLES["ecm_ground"].kind == "low" and ROLES["crank_sensor"].kind == "crank"
    assert {k for k, r in ROLES.items() if r.numbered} == {"est", "injector", "coil_low", "tp", "tac", "app"}


def test_parse_role():
    assert parse_role("cam") == ("cam", None) and parse_role("est:3") == ("est", 3)
    assert parse_role(" EST:3 ") == ("est", 3)
    for bad in ("nope", "est:9", "cam:2", "est:x", ""):
        with pytest.raises(ValueError):
            parse_role(bad)


def test_role_label():
    assert role_label("est", 3) == "Ignition control (EST) 3"
    assert role_label("coil_low", 1) == "Coil low reference (odd bank)"
    assert role_label("coil_low", 2) == "Coil low reference (even bank)"
    assert role_label("cam", None) == "Cam signal" and role_label("est", None) == "Ignition control (EST)"


def test_default_channels_carry_legacy_roles():
    from crank.model import default_channels
    assert {c.n: (c.role, c.num) for c in default_channels()} == LEGACY_ROLES
