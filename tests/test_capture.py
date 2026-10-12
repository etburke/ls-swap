import time

import pytest

from crank.capture import CaptureJob
from crank.model import CaptureSettings
from crank.scope import SimScope


def _job(scope, **kw):
    s = CaptureSettings(wait=kw.pop("wait", 5))
    j = CaptureJob(scope, s, poll_s=0.0, never_armed_s=0.05, **kw)
    j.start()
    j.join(5)
    return j


def test_job_happy_path_saves_before_analysis(tmp_path):
    saved = []
    j = _job(SimScope("healthy"), save=saved.append)
    assert j.state == "done" and saved and j.analysis.codes == {"signal_clean"}


@pytest.mark.parametrize("fault,expect", [("never_arms", "never armed"),
                                          ("fetch_error", "download failure")])
def test_job_faults_become_error_state(fault, expect):
    j = _job(SimScope("healthy", fault=fault))
    assert j.state == "error" and expect in j.message


def test_job_timeout():
    j = _job(SimScope("healthy", fault="stuck_waiting"), wait=0.2)
    assert j.state == "error" and "Timed out" in j.message


def test_job_cancel_disarms():
    sc = SimScope("healthy", fault="stuck_waiting")
    j = CaptureJob(sc, CaptureSettings(wait=30), poll_s=0.01)
    j.start()
    time.sleep(0.1)
    j.cancel()
    j.join(5)
    assert j.state == "cancelled" and sc._armed is False


def test_analysis_crash_keeps_saved_data():
    saved = []
    j = CaptureJob(SimScope("healthy"), CaptureSettings(), poll_s=0.0, save=saved.append,
                   thresholds=object())          # breaks analysis on purpose
    j.start(); j.join(5)
    assert j.state == "error" and saved


class ScriptedScope(SimScope):
    def __init__(self, script):
        super().__init__("healthy")
        self.script = list(script)

    def status(self):
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


def test_trigger_near_the_end_of_the_wait_is_not_killed():
    sc = ScriptedScope(["WAIT"] * 2 + ["TD"] * 10 + ["STOP"])
    j = CaptureJob(sc, CaptureSettings(wait=0.05, seconds=6.0), poll_s=0.02)
    j.start(); j.join(10)
    assert j.state == "done", j.message
