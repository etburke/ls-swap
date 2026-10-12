import numpy as np
import pytest

from crank.sim import SCENARIOS, generate


def test_every_scenario_generates_four_channels():
    for s in SCENARIOS:
        d = generate(s)
        assert set(d.channels) == {1, 2, 3, 4}
        assert all(len(v) == len(d.t) for v in d.channels.values())
        assert d.meta["scenario"] == s


def test_unknown_scenario_rejected():
    with pytest.raises(ValueError):
        generate("bogus")


def test_same_seed_is_deterministic():
    a, b = generate("runt_pulses", seed=3), generate("runt_pulses", seed=3)
    assert np.array_equal(a.channels[1], b.channels[1])
