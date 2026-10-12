import numpy as np
import pytest

from crank.model import CaptureSettings
from crank.scope import RigolScope, ScopeError, SimScope


class FakeVisa:
    """Replays canned SCPI answers; optionally rejects commands."""
    def __init__(self, reject=()):
        self.writes, self.reject, self._errs, self.state = [], reject, [], {}
        self.timeout = self.chunk_size = None

    def write(self, cmd):
        self.writes.append(cmd)
        name, _, value = cmd.partition(" ")
        self.state[name] = value
        if any(cmd.startswith(r) for r in self.reject):
            self._errs.append('-113,"Undefined header"')

    def query(self, cmd):
        if cmd == ":SYSTem:ERRor:NEXT?":
            return self._errs.pop(0) if self._errs else '0,"No error"'
        if cmd.endswith("?") and cmd[:-1] in self.state:
            return self.state[cmd[:-1]]
        return {"*IDN?": "RIGOL,DHO804", ":ACQuire:MDEPth?": "10M",
                ":ACQuire:SRATe?": "1.6E6", ":TRIGger:STATus?": "STOP",
                ":WAVeform:PREamble?": "0,0,300000,1,1e-5,-0.3,0,0.02,0,127"}[cmd]

    def query_binary_values(self, cmd, datatype, container):
        return np.full(250_000, 200, dtype=np.uint8)

    def close(self):
        pass


def test_configure_sends_critical_commands_in_order():
    f = FakeVisa()
    RigolScope(inst=f).configure(CaptureSettings())
    order = [c for c in f.writes if c.startswith((":STOP", ":TIMebase:MAIN:SCALe", ":ACQuire:MDEPth",
                                                  ":TRIGger:SWEep"))]
    assert [c.split()[0] for c in order] == [":STOP", ":TIMebase:MAIN:SCALe", ":ACQuire:MDEPth",
                                             ":TRIGger:SWEep"]
    assert ":ACQuire:TYPE NORMal" in f.writes and ":ACQuire:MDEPth 10M" in f.writes


def test_critical_rejection_raises_noncritical_logs():
    with pytest.raises(ScopeError, match="MDEPth"):
        RigolScope(inst=FakeVisa(reject=(":ACQuire:MDEPth",))).configure(CaptureSettings())
    s = RigolScope(inst=FakeVisa(reject=(":CHANnel1:BWLimit",)))
    s.configure(CaptureSettings())
    assert any("BWLimit" in m for m in s.log)


def test_fetch_converts_to_volts_and_time():
    s = RigolScope(inst=FakeVisa())
    cfg = CaptureSettings()
    cfg.channels = cfg.channels[:1]
    d = s.fetch(cfg, chunk=100_000)
    assert len(d.t) == 300_000 and np.isclose(d.t[0], -0.3)
    assert np.isclose(d.channels[1][0], (200 - 0 - 127) * 0.02)


def test_short_record_logs_warning():
    f = FakeVisa()
    f.query_orig = f.query
    f.query = lambda cmd: ("0,0,5000,1,1e-5,-0.3,0,0.02,0,127" if cmd == ":WAVeform:PREamble?"
                           else f.query_orig(cmd))
    f.query_binary_values = lambda cmd, datatype, container: np.full(5000, 100, dtype=np.uint8)
    s = RigolScope(inst=f)
    cfg = CaptureSettings()
    cfg.channels = cfg.channels[:1]
    s.fetch(cfg)
    assert any("only 5000 points" in m for m in s.log)


def test_sim_scope_flow_and_channel_filter():
    sc = SimScope("healthy", arm_polls=2)
    cfg = CaptureSettings()
    cfg.channels[1].enabled = False
    sc.configure(cfg)
    sc.arm()
    assert [sc.status() for _ in range(5)] == ["WAIT", "WAIT", "TD", "STOP", "STOP"]
    assert set(sc.fetch(cfg).channels) == {1, 3, 4}


def test_self_check_passes_then_flags_a_clamped_setting():
    results = RigolScope(inst=FakeVisa()).self_check(CaptureSettings())
    assert results and all(ok for ok, _ in results)

    f = FakeVisa()
    sc = RigolScope(inst=f)
    real_query = f.query
    f.query = lambda c: "1M" if c == ":ACQuire:MDEPth?" else real_query(c)   # scope clamped the depth
    bad = [t for ok, t in sc.self_check(CaptureSettings()) if not ok]
    assert len(bad) == 1 and "memory depth" in bad[0]
