import time

from crank.obd.errors import ObdNoAdapter
from crank.obd.reader import INIT
from crank.obd.transport import DelegatingTransport, SimTransport, StreamEnd
from crank.obd.watch import WatchJob, summarize_poll, summarize_raw
from crank.store import SessionStore


class Recording(DelegatingTransport):
    """Remembers every command sent and streamed, in order."""

    def __init__(self, inner):
        super().__init__(inner)
        self.sent = []

    def send(self, cmd, timeout=5.0):
        self.sent.append(cmd)
        return super().send(cmd, timeout)

    def stream(self, cmd, on_line, stop, max_seconds, silence_s=1.0):
        self.sent.append(("stream", cmd))
        return super().stream(cmd, on_line, stop, max_seconds, silence_s)


class DropsMidStream(SimTransport):
    """The Bluetooth link drops after a few monitor lines: the stream and everything after it fails."""

    def stream(self, cmd, on_line, stop, max_seconds, silence_s=1.0):
        for _ in range(5):
            on_line(time.time(), "1A180000000000000000")
        self.opened = False
        raise ObdNoAdapter("The adapter link dropped.")


def make(tmp_path, scenario="cranking_rpm_seen", mode="raw", seconds=1.6, transport=None, **kw):
    t = transport or Recording(SimTransport(scenario))
    t.open()
    ses = SessionStore(tmp_path).open("s")
    return WatchJob(t, mode, ses.new_watch(), seconds=seconds, sim=True, **kw), ses, t


def run(tmp_path, scenario="cranking_rpm_seen", mode="raw", **kw):
    job, ses, t = make(tmp_path, scenario, mode, **kw)
    job.start()
    job.join(10)
    assert not job.running
    return job


def watch_files(tmp_path):
    folder = tmp_path / "s" / "watches"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_raw_watch_records_frames_and_summarizes_engine_speed(tmp_path):
    job = run(tmp_path, "cranking_rpm_seen", "raw")
    assert job.state == "done" and job.meta["summary"]["engine_speed"]["best_effort"] is True
    assert 150 <= job.meta["summary"]["engine_speed"]["max"] <= 250
    assert set(job.meta["summary"]["ids"]) == {"0C9", "1A1", "3C1"}
    assert job.meta["sim"] is True and job.meta["protocol"] == "A6" and job.meta["end_reason"] == "time"
    assert job.meta["decoders"] == ["0C9-engine-speed-v1: bytes[1:3] BE x0.25"]
    assert job.meta["capture_id"] is None and job.meta["trigger_t"] is None
    assert job.monitoring.is_set() and 150 <= job.max_rpm <= 250 and job.frames == job.lines > 0
    meta, rows = SessionStore(tmp_path).open("s").load_watch(job.meta["id"])
    rpm_rows = [r for r in rows if r.get("id") == "0C9"]
    assert rpm_rows[0]["decoded"][0] == {"name": "Engine speed", "value": 0.0, "unit": "rpm",
                                         "decoder": "0C9-engine-speed-v1: bytes[1:3] BE x0.25",
                                         "best_effort": True}
    assert {"t", "raw", "id", "dlc", "data"} <= set(rpm_rows[0])


def test_raw_watch_no_rpm_reports_max_zero(tmp_path):
    job = run(tmp_path, "cranking_no_rpm", "raw")
    es = job.meta["summary"]["engine_speed"]
    assert job.state == "done" and es["max"] == 0 and es["samples"] > 0
    assert es["median_nonzero"] is None and es["first_nonzero_s"] is None


def test_poll_watch_samples_010C_and_atrv_every_fourth_cycle(tmp_path):
    job = run(tmp_path, "cranking_rpm_seen", "poll")
    assert job.state == "done" and job.monitoring.is_set()
    _, rows = SessionStore(tmp_path).open("s").load_watch(job.meta["id"])
    rpm = [r for r in rows if r["cmd"] == "010C"]
    volts = [r for r in rows if r["cmd"] == "ATRV"]
    assert volts and len(rpm) >= 4 * len(volts)
    assert all(set(r) == {"t", "cmd", "req", "raw", "value", "unit", "reason"} for r in rows if r["cmd"] != "ATRV")
    assert all(set(r) == {"t", "cmd", "raw", "value", "unit", "reason"} for r in volts)
    s = job.meta["summary"]
    assert s["rpm"]["source"] == "010C (official)" and s["samples"] == len(rpm)
    assert 150 <= s["rpm"]["max"] <= 250 and s["voltage"] == {"min": 12.4, "max": 12.4}
    assert job.meta["mode"] == "poll"


def test_poll_busy_reply_is_a_sample_with_a_reason(tmp_path):
    job = run(tmp_path, "pid_busy_always", "poll", seconds=0.5)
    _, rows = SessionStore(tmp_path).open("s").load_watch(job.meta["id"])
    rpm = [r for r in rows if r["cmd"] == "010C"]
    assert job.state == "done" and rpm and all(r["value"] is None and r["reason"] == "busy" for r in rpm)
    assert job.meta["summary"]["rpm"]["answered"] == 0


def test_cancel_discards_everything(tmp_path):
    job, ses, t = make(tmp_path, seconds=5)
    job.start()
    assert job.monitoring.wait(3)
    job.cancel()
    job.join(5)
    assert job.state == "cancelled" and job.meta is None
    assert watch_files(tmp_path) == [] and ses.list_watches() == []
    assert t.sent[-2:] == ["ATCAF1", "ATD0"] and t.cancel is None


def test_stop_before_any_frame_saves_an_empty_watch(tmp_path):
    job, ses, _ = make(tmp_path)
    job.stop()
    job.start()
    job.join(5)
    assert job.state == "stopped" and job.frames == 0
    assert job.meta["summary"]["engine_speed"] is None and ses.list_watches()[0]["id"] == job.meta["id"]


def test_overflow_keeps_frames_with_a_note(tmp_path):
    job = run(tmp_path, "monitor_overflow", "raw")
    assert job.state == "stopped" and job.frames > 0 and job.meta["end_reason"] == "buffer_full"
    assert any("BUFFER FULL" in n for n in job.meta["notes"])


def test_reset_mid_watch_keeps_frames_and_marks_cold(tmp_path):
    job, ses, t = make(tmp_path, "reset_mid_watch")
    job.start()
    job.join(10)
    assert job.state == "stopped" and job.frames > 0 and job.meta["end_reason"] == "reset"
    after = t.sent[t.sent.index(("stream", "ATMA")) + 1:]
    assert tuple(after) == INIT and not job.restore_failed
    assert any("reset" in n for n in job.meta["notes"])


def test_link_drop_mid_stream_keeps_frames(tmp_path):
    job = run(tmp_path, transport=DropsMidStream("cranking_rpm_seen"))
    assert job.state == "stopped" and job.frames == 5 and job.restore_failed
    assert any("link dropped" in n for n in job.meta["notes"])
    assert job.meta["summary"]["frames"] == 5


def test_restore_sends_atcaf1_and_atd0_after_a_normal_end(tmp_path):
    job, _, t = make(tmp_path, seconds=0.3)
    job.start()
    job.join(5)
    i = t.sent.index(("stream", "ATMA"))
    assert t.sent[i - 4:i] == ["ATH1", "ATCSM1", "ATCAF0", "ATD1"]
    assert t.sent[i + 1:] == ["ATCAF1", "ATD0"] and not job.restore_failed and t.cancel is None


def test_cold_connection_runs_init_before_the_protocol_check(tmp_path):
    job, _, t = make(tmp_path, seconds=0.2, cold=True)
    job.start()
    job.join(5)
    assert tuple(t.sent[:len(INIT)]) == INIT and t.sent[len(INIT):len(INIT) + 2] == ["0100", "ATDPN"]


def stop_soon(job, after=0.2):
    job.start()
    time.sleep(after)
    t0 = time.monotonic()
    job.stop()
    job.join(5)
    return time.monotonic() - t0


def test_stop_during_a_slow_0100_ends_at_once_and_never_enters_monitor_mode(tmp_path):
    job, ses, t = make(tmp_path, "slow_adapter", seconds=5)       # every reply takes 1 s
    assert stop_soon(job) < 0.5
    assert job.state == "stopped" and job.frames == 0 and t.sent == ["0100"] and t.cancel is None
    assert ses.list_watches()[0]["id"] == job.meta["id"] and job.meta["end_reason"] == "stopped"


def test_until_during_a_slow_0100_stops_without_monitoring(tmp_path):
    t0 = time.monotonic()
    job, _, t = make(tmp_path, "slow_adapter", seconds=5, until=lambda: time.monotonic() - t0 > 0.2)
    job.start()
    job.join(5)
    assert time.monotonic() - t0 < 0.7
    assert job.state == "stopped" and job.meta["end_reason"] == "until" and t.sent == ["0100"]


def test_poll_stop_during_a_slow_first_request_ends_at_once(tmp_path):
    job, _, _ = make(tmp_path, "slow_adapter", "poll", seconds=5)
    assert stop_soon(job) < 0.5 and job.state == "stopped" and job.lines == 0


def test_poll_monitoring_waits_for_an_010C_value(tmp_path):
    sim = SimTransport("healthy")
    sim.sc.no_answer_pids = ("010C",)
    job = run(tmp_path, "healthy", "poll", seconds=0.3, transport=Recording(sim))
    _, rows = SessionStore(tmp_path).open("s").load_watch(job.meta["id"])
    assert job.state == "done" and rows and all(r["reason"] == "no_answer" for r in rows if r["cmd"] == "010C")
    assert not job.monitoring.is_set()


def test_until_predicate_stops_and_keeps(tmp_path):
    holder = {}
    job, ses, _ = make(tmp_path, seconds=5, until=lambda: holder["job"].frames >= 20)
    holder["job"] = job
    job.start()
    job.join(5)
    assert job.state == "stopped" and job.meta["end_reason"] == "until" and 20 <= job.frames < 60
    assert ses.list_watches()[0]["id"] == job.meta["id"]


def test_on_saved_and_on_finish_are_called_once(tmp_path):
    calls = []
    job = run(tmp_path, seconds=0.2, on_saved=lambda j: calls.append(("saved", j.meta["id"])),
              on_finish=lambda: calls.append("finish"))
    assert calls == [("saved", job.meta["id"]), "finish"]


def test_silent_vehicle_before_monitoring_is_an_error_and_saves_nothing(tmp_path):
    job = run(tmp_path, "no_vehicle", "raw")
    assert job.state == "error" and job.message == "The vehicle did not answer. Is the key on?"
    assert not job.monitoring.is_set() and watch_files(tmp_path) == [] and job.meta is None


def test_non_can_vehicle_is_refused(tmp_path):
    sim = SimTransport("healthy")
    sim.sc.protocol = "A3"
    job = run(tmp_path, transport=Recording(sim))
    assert job.state == "error"
    assert job.message == "Raw watch needs a CAN vehicle; this one answered on protocol A3."
    assert "ATCAF0" not in job.t.sent and watch_files(tmp_path) == []


def test_summaries_on_handmade_rows():
    raw = [{"t": 10.0, "raw": "junk"},
           {"t": 10.5, "raw": "", "id": "0C9", "dlc": 8, "data": "",
            "decoded": [{"name": "Engine speed", "value": 0.0, "unit": "rpm", "decoder": "v1", "best_effort": True}]},
           {"t": 11.0, "raw": "", "id": "0C9", "dlc": 8, "data": "",
            "decoded": [{"name": "Engine speed", "value": 200.0, "unit": "rpm", "decoder": "v1", "best_effort": True}]},
           {"t": 12.0, "raw": "", "id": "1A1", "dlc": 8, "data": "", "decoded": []}]
    s = summarize_raw(raw, 10.0)
    assert s["lines"] == 4 and s["frames"] == 3 and s["max_gap_s"] == 1.0
    assert s["ids"] == {"0C9": {"count": 2, "hz": 1.0}, "1A1": {"count": 1, "hz": 0.5}}
    assert s["engine_speed"] == {"decoder": "v1", "best_effort": True, "samples": 2, "max": 200.0,
                                 "median_nonzero": 200.0, "first_nonzero_s": 1.0}
    poll = [{"t": 1.0, "cmd": "010C", "raw": "", "value": None, "unit": "rpm", "reason": "no_answer"},
            {"t": 1.5, "cmd": "010C", "raw": "", "value": 180.0, "unit": "rpm", "reason": ""},
            {"t": 2.0, "cmd": "ATRV", "raw": "", "value": 11.9, "unit": "V", "reason": ""}]
    p = summarize_poll(poll, 1.0)
    assert p == {"samples": 2, "rpm": {"source": "010C (official)", "answered": 1, "max": 180.0,
                                       "median_nonzero": 180.0, "first_nonzero_s": 0.5},
                 "voltage": {"min": 11.9, "max": 11.9},
                 "pids": {"0C": {"name": "Engine RPM", "unit": "rpm", "answered": 1, "min": 180.0, "max": 180.0}}}
    assert summarize_raw([], 0.0)["engine_speed"] is None and summarize_poll([], 0.0)["voltage"] is None


class RestoreAnswers(Recording):
    """Answers the restore commands with `reply` (e.g. STOPPED: the adapter was still monitoring)."""

    def __init__(self, inner, reply):
        super().__init__(inner)
        self.reply = reply

    def send(self, cmd, timeout=5.0):
        if cmd in ("ATCAF1", "ATD0"):
            self.sent.append(cmd)
            return self.reply
        return super().send(cmd, timeout)


def test_a_restore_reply_without_ok_fails_the_restore(tmp_path):
    job = run(tmp_path, seconds=0.3, transport=RestoreAnswers(SimTransport("cranking_rpm_seen"), "STOPPED"))
    assert job.restore_failed and any("could not be restored" in n for n in job.meta["notes"])


def test_an_atz_restore_without_a_banner_fails_the_restore(tmp_path):
    class NoBanner(Recording):
        def send(self, cmd, timeout=5.0):
            return "" if cmd == "ATZ" and ("stream", "ATMA") in self.sent else super().send(cmd, timeout)
    job = run(tmp_path, "reset_mid_watch", transport=NoBanner(SimTransport("reset_mid_watch")))
    assert job.meta["end_reason"] == "reset" and job.restore_failed


class EndsWith(Recording):
    """The stream records a few lines then ends with `end`, or raises `exc`."""

    def __init__(self, inner, end=None, exc=None):
        super().__init__(inner)
        self.end, self.exc = end, exc

    def stream(self, cmd, on_line, stop, max_seconds, silence_s=1.0):
        self.sent.append(("stream", cmd))
        for _ in range(3):
            on_line(time.time(), "1A180000000000000000")
        if self.exc:
            raise self.exc
        return self.end


def test_an_adapter_left_monitoring_fails_the_restore(tmp_path):
    t = EndsWith(SimTransport("cranking_rpm_seen"), end=StreamEnd("time", at_prompt=False))
    job = run(tmp_path, transport=t)
    assert job.state == "done" and job.restore_failed and job.frames == 3


def test_monitor_stuck_on_the_way_out_fails_the_restore(tmp_path):
    from crank.obd.errors import ObdMonitorStuck
    job = run(tmp_path, transport=EndsWith(SimTransport("cranking_rpm_seen"), exc=ObdMonitorStuck()))
    assert job.restore_failed and job.error is not None and job.frames == 3


def test_a_failing_on_finish_still_ends_the_job(tmp_path):
    def boom():
        raise RuntimeError("callback bug")
    job = run(tmp_path, seconds=0.2, on_finish=boom)
    assert job.state == "done" and job.finished_at


def test_reset_and_linked_are_public(tmp_path):
    job = run(tmp_path, "reset_mid_watch")
    assert job.reset is True and job.linked is False
    linked, _, _ = make(tmp_path, seconds=None, until=lambda: False)
    assert linked.linked is True and linked.reset is False


def test_poll_groups_lead_with_rpm_and_hold_six():
    from crank.obd.pids import poll_groups
    sup = {0x0C, 0x0E, 0x11, 0x45, 0x47, 0x49, 0x4A, 0x4C, 0x42}
    groups = poll_groups(sup)
    assert all(g[0] == 0x0C and len(g) <= 6 for g in groups)
    assert {p for g in groups for p in g} == sup
    assert poll_groups({0x0C}) == [(0x0C,)] and poll_groups(set()) == [(0x0C,)]


def test_poll_watch_records_many_pids(tmp_path):
    job = run(tmp_path, "crank_poll_multi", "poll", seconds=1.5)
    _, rows = SessionStore(tmp_path).open("s").load_watch(job.meta["id"])
    cmds = {r["cmd"] for r in rows}
    assert {"010C", "010E", "014C"} <= cmds
    rpm = [r for r in rows if r["cmd"] == "010C"]
    assert rpm and all(len(r["req"]) > 4 and "0C" in [r["req"][i:i + 2] for i in range(2, len(r["req"]), 2)]
                       for r in rpm)
    s = job.meta["summary"]
    assert "4C" in s["pids"] and s["pids"]["4C"]["unit"] == "%" and s["rpm"]["answered"] > 0
    assert 150 <= s["rpm"]["max"] <= 250


def test_poll_watch_falls_back_to_single_pids(tmp_path):
    job = run(tmp_path, "no_multi_pid", "poll", seconds=1.5)
    _, rows = SessionStore(tmp_path).open("s").load_watch(job.meta["id"])
    assert any("one PID at a time" in n for n in job.meta["notes"])
    singles = [r for r in rows if r.get("req") and len(r["req"]) == 4]
    assert any(r["cmd"] == "010C" and r["value"] is not None for r in singles)
    assert any(r["cmd"] != "010C" and r["value"] is not None for r in singles)


def test_a_pid_missing_from_a_group_reply_is_no_answer(tmp_path):
    sim = SimTransport("crank_poll_multi")
    sim.sc.no_answer_pids = ("014C",)
    job = run(tmp_path, "crank_poll_multi", "poll", seconds=0.6, transport=Recording(sim))
    _, rows = SessionStore(tmp_path).open("s").load_watch(job.meta["id"])
    thr = [r for r in rows if r["cmd"] == "014C"]
    assert thr and all(r["value"] is None and r["reason"] == "no_answer" for r in thr)
    assert any(r["cmd"] == "010C" and r["value"] is not None for r in rows)
    assert not any("one PID at a time" in n for n in job.meta["notes"])
