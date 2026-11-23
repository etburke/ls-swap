import numpy as np
import pytest

from crank.model import CaptureData
from crank.obd.transport import SimTransport
from crank.obd.watch import WatchJob
from crank.sim import generate
from crank.store import SessionStore
from crank.watch_plot import build_watch_figure


@pytest.fixture(scope="module")
def multi_watch_and_capture(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("w")
    t = SimTransport("crank_poll_multi")
    t.open()
    ses = SessionStore(tmp).open("s")
    job = WatchJob(t, "poll", ses.new_watch(), seconds=1.2, sim=True)
    job.start()
    job.join(10)
    meta, rows = ses.load_watch(job.meta["id"])
    meta["trigger_t"] = meta["started"] + 0.3
    return meta, rows, generate("healthy")


def _yaxes(fig):
    return {tr.yaxis for tr in fig.data}


def test_figure_one_subplot_per_unit_and_overlay(multi_watch_and_capture):
    meta, rows, cap = multi_watch_and_capture
    fig = build_watch_figure(meta, rows, [0x0C, 0x4C, 0x11, 0x0E], cap)
    names = [tr.name for tr in fig.data]
    assert "Scope CH1 (per rev)" in names and "Commanded throttle actuator" in names and "Throttle position" in names
    assert len(_yaxes(fig)) == 3                                   # rpm, %, ° BTDC
    scope = next(tr for tr in fig.data if tr.name == "Scope CH1 (per rev)")
    rpm = next(tr for tr in fig.data if tr.name == "Engine RPM")
    assert scope.yaxis == rpm.yaxis and min(scope.x) > 0.3        # shifted to the trigger time
    assert any("Scope trigger" in (s.text or "") for s in fig.layout.annotations)


def test_figure_without_usable_capture_has_no_overlay(multi_watch_and_capture):
    meta, rows, _ = multi_watch_and_capture
    flat = CaptureData(np.linspace(0, 1, 1000), {1: np.zeros(1000)}, {1: "Crank @ PCM"})
    assert [tr.name for tr in build_watch_figure(meta, rows, [0x0C], flat).data] == ["Engine RPM"]
    assert [tr.name for tr in build_watch_figure(meta, rows, [0x0C], None).data] == ["Engine RPM"]


def test_figure_with_no_pids_says_so(multi_watch_and_capture):
    meta, rows, _ = multi_watch_and_capture
    fig = build_watch_figure(meta, rows, [])
    assert not fig.data and any("Pick a PID" in a.text for a in fig.layout.annotations)
