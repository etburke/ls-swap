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


def test_on_done_runs_after_analysis_with_the_saved_id():
    seen = []
    j = CaptureJob(SimScope("ref_sag"), CaptureSettings(), poll_s=0.0, save=lambda d: "007",
                   on_done=lambda job: seen.append((job.saved_id, job.analysis.codes)))
    j.start(); j.join(5)
    assert seen == [("007", {"ref_sag"})] and j.state == "done"


def test_on_done_still_runs_when_analysis_fails_after_a_save():
    seen = []
    j = CaptureJob(SimScope("healthy"), CaptureSettings(), poll_s=0.0, save=lambda d: "001",
                   thresholds=object(), on_done=lambda job: seen.append(job.analysis))
    j.start(); j.join(5)
    assert j.state == "error" and seen == [None]


def test_a_failing_hook_never_breaks_the_capture():
    def boom(job):
        raise RuntimeError("log full")
    j = CaptureJob(SimScope("healthy"), CaptureSettings(), poll_s=0.0, save=lambda d: "001", on_done=boom)
    j.start(); j.join(5)
    assert j.state == "done" and "log full" in j.hook_error


def test_no_hook_call_when_nothing_was_saved():
    seen = []
    j = CaptureJob(SimScope("healthy", fault="fetch_error"), CaptureSettings(), poll_s=0.0,
                   save=lambda d: "001", on_done=seen.append)
    j.start(); j.join(5)
    assert j.state == "error" and seen == []
