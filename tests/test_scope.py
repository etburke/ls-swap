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


def test_depth_shortfall_warns_and_is_recorded_in_meta():
    f = FakeVisa()
    real_query = f.query
    f.query = lambda c: "1M" if c == ":ACQuire:MDEPth?" else real_query(c)
    s = RigolScope(inst=f)
    cfg = CaptureSettings()
    cfg.channels = cfg.channels[:1]
    s.configure(cfg)
    assert any(m.startswith("WARNING") and "memory depth" in m for m in s.log)
    d = s.fetch(cfg)
    assert d.meta["depth_requested"] == "10M" and d.meta["depth_reported"] == "1M"
    assert d.meta["points"] == 300_000 and "srate" in d.meta
    assert any(m.startswith("WARNING") and "points" in m for m in s.log)


def test_log_is_cleared_on_each_configure():
    s = RigolScope(inst=FakeVisa())
    s.log.append("stale")
    s.configure(CaptureSettings())
    assert "stale" not in s.log


def test_configure_turns_roll_off_before_the_timebase():
    f = FakeVisa()
    RigolScope(inst=f).configure(CaptureSettings())
    names = [c.split()[0] for c in f.writes]
    assert ":TIMebase:ROLL 0" in f.writes
    assert names.index(":TIMebase:ROLL") < names.index(":TIMebase:MAIN:SCALe")


def test_rejected_roll_command_is_logged_not_fatal():
    s = RigolScope(inst=FakeVisa(reject=(":TIMebase:ROLL",)))
    s.configure(CaptureSettings())
    assert any("ROLL" in m and "rejected" in m for m in s.log)


def test_sweep_that_does_not_stick_warns():
    f = FakeVisa()
    real_query = f.query
    f.query = lambda c: "AUTO" if c == ":TRIGger:SWEep?" else real_query(c)
    s = RigolScope(inst=f)
    s.configure(CaptureSettings())
    assert any(m.startswith("WARNING") and "sweep" in m and "AUTO" in m for m in s.log)


def test_self_check_accepts_short_form_answers():
    f = FakeVisa()
    real_query = f.query
    short = {":TRIGger:EDGE:SLOPe?": "POS", ":TRIGger:SWEep?": "SING", ":TRIGger:EDGE:SOURce?": "CHAN1"}
    f.query = lambda c: short.get(c) or real_query(c)
    results = RigolScope(inst=f).self_check(CaptureSettings())
    assert all(ok for ok, _ in results), [t for ok, t in results if not ok]

    f2 = FakeVisa()
    real2 = f2.query
    f2.query = lambda c: "AUTO" if c == ":TRIGger:SWEep?" else real2(c)
    bad = [t for ok, t in RigolScope(inst=f2).self_check(CaptureSettings()) if not ok]
    assert len(bad) == 1 and "sweep" in bad[0]


class WindowedVisa(FakeVisa):
    """Like the DHO804: the preamble's point count is the current STARt..STOP window, which
    stays wherever the last read left it."""
    def __init__(self, mem=600_000, **kw):
        super().__init__(**kw)
        self.mem, self.start, self.stop = mem, 1, mem

    def write(self, cmd):
        super().write(cmd)
        name, _, value = cmd.partition(" ")
        if name == ":WAVeform:STARt":
            self.start = int(value)
        elif name == ":WAVeform:STOP":
            self.stop = min(int(float(value)), self.mem)

    def query(self, cmd):
        if cmd == ":WAVeform:PREamble?":
            return f"0,0,{self.stop - self.start + 1},1,1e-5,-0.3,0,0.02,0,127"
        return super().query(cmd)

    def query_binary_values(self, cmd, datatype, container):
        return np.full(self.stop - self.start + 1, 200, dtype=np.uint8)


def test_every_channel_gets_full_memory_after_chunked_reads():
    f = WindowedVisa()
    s = RigolScope(inst=f)
    cfg = CaptureSettings()
    s.configure(cfg)
    d = s.fetch(cfg)
    assert {len(v) for v in d.channels.values()} == {600_000} and len(d.t) == 600_000
    # the next capture starts from a clean window too
    f.start, f.stop = 550_001, 600_000
    d = s.fetch(cfg)
    assert {len(v) for v in d.channels.values()} == {600_000}


def test_channels_of_different_lengths_raise_a_clear_error():
    f = WindowedVisa()
    s = RigolScope(inst=f)
    cfg = CaptureSettings()
    s.configure(cfg)
    real = f.query
    calls = {"n": 0}
    def query(c):
        if c == ":WAVeform:PREamble?":
            calls["n"] += 1
            if calls["n"] == 2:
                return "0,0,250000,1,1e-5,-0.3,0,0.02,0,127"
        return real(c)
    f.query = query
    with pytest.raises(ScopeError, match="points"):
        s.fetch(cfg)


def test_scope_fetch_records_roles():
    cfg = CaptureSettings()
    cfg.channels[1].role = "cam"
    cfg.channels[2].role, cfg.channels[2].num = "est", 2
    d = RigolScope(inst=FakeVisa()).fetch(cfg)
    assert d.meta["roles"]["2"] == {"role": "cam", "num": None} and d.meta["roles"]["3"] == {"role": "est", "num": 2}


def test_sim_scope_fetch_records_roles():
    cfg = CaptureSettings()
    cfg.channels[3].role = "ecm_ground"
    d = SimScope("healthy").fetch(cfg)
    assert d.meta["roles"]["4"] == {"role": "ecm_ground", "num": None}
