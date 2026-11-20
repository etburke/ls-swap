import threading
from types import SimpleNamespace

from crank.obd.errors import ObdCancelled, ObdProtocolError
from crank.obd.job import ObdJob


def job_of(run, **kw):
    j = ObdJob(run, **kw)
    j.start()
    return j


def test_a_finished_read_is_done_with_its_readout():
    j = job_of(lambda c, s: "R")
    j.join(5)
    assert j.state == "done" and j.readout == "R" and not j.running and j.message == ""


def test_a_readout_flagged_stopped_makes_the_job_stopped():
    r = SimpleNamespace(stopped=True)
    j = job_of(lambda c, s: r); j.join(5)
    assert j.state == "stopped" and j.readout is r and not j.running


def test_stop_sets_the_stop_event_and_cancel_sets_the_cancel_event():
    j = ObdJob(lambda c, s: "R")
    assert not j.stop_event.is_set() and not j.cancel_event.is_set()
    j.stop(); assert j.stop_event.is_set() and not j.cancel_event.is_set()
    j.cancel(); assert j.cancel_event.is_set()


def test_cancel_ends_a_waiting_read_as_cancelled():
    def run(c, s):
        c.wait(5)
        raise ObdCancelled()
    j = job_of(run)
    assert j.running
    j.cancel(); j.join(5)
    assert j.state == "cancelled" and j.readout is None and "nothing was recorded" in j.message


def test_an_obd_error_is_kept_as_the_message():
    def run(c, s):
        raise ObdProtocolError("The vehicle did not answer. Is the key on?")
    j = job_of(run); j.join(5)
    assert j.state == "error" and j.message == "The vehicle did not answer. Is the key on?"


def test_an_unexpected_error_names_its_type():
    def run(c, s):
        raise ValueError("boom")
    j = job_of(run); j.join(5)
    assert j.state == "error" and j.message == "ValueError: boom"


def test_a_cancel_after_the_read_finished_does_not_undo_it():
    def run(c, s):
        c.set()                                            # the cancel lands as the read completes
        return "R"
    j = job_of(run); j.join(5)
    assert j.state == "done" and j.readout == "R"


def test_on_finish_runs_before_the_final_state_is_visible():
    seen, holder = [], {}
    j = ObdJob(lambda c, s: "R", on_finish=lambda: seen.append(holder["j"].state))
    holder["j"] = j
    j.start(); j.join(5)
    assert seen == ["running"] and j.state == "done"


def test_seconds_counts_while_running_and_stops_when_finished():
    gate = threading.Event()
    j = job_of(lambda c, s: gate.wait(5) and "R")
    assert j.running and j.seconds >= 0
    gate.set(); j.join(5)
    done_at = j.seconds
    assert j.seconds == done_at


def test_a_job_that_was_never_started_is_idle():
    j = ObdJob(lambda c, s: "R")
    assert j.state == "idle" and not j.running and j.seconds == 0
