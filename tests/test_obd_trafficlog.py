import re
import threading

import pytest

from crank.obd.errors import ObdCancelled, ObdTimeout
from crank.obd.reader import ObdReader
from crank.obd.trafficlog import LoggingTransport, TrafficLog, tail_lines
from crank.obd.transport import SimTransport

LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3} \[[^\]]*\] (?:(?:hb|sim) )?(?:>>|<<|!!|\*\*) .*$")


def make(tmp_path, session=None, **kw):
    return TrafficLog(tmp_path / "logs", session=session, **kw)


def lines(path):
    return path.read_text().splitlines()


def test_lines_have_the_documented_format(tmp_path):
    log = make(tmp_path, session=lambda: ("mx", None))
    log.event("connected"); log.command("010C"); log.reply("41 0C\r0B B8\r\r", 43)
    log.error(ObdTimeout("no answer")); log.command("ATRV", tag="hb")
    got = lines(log.path)
    assert all(LINE.match(l) for l in got) and len(got) == 5
    assert "[mx] ** connected" in got[0] and ">> 010C" in got[1]
    assert "<< 41 0C | 0B B8 (43 ms)" in got[2] and "!! ObdTimeout: no answer" in got[3]
    assert "[mx] hb >> ATRV" in got[4]


def test_the_session_copy_has_no_heartbeat_lines(tmp_path):
    sdir = tmp_path / "sessions" / "mx"
    sdir.mkdir(parents=True)
    log = make(tmp_path, session=lambda: ("mx", sdir))
    log.command("010C"); log.command("ATRV", tag="hb"); log.event("hello")
    app, copy = lines(log.path), lines(sdir / "obd.log")
    assert len(app) == 3 and len(copy) == 2 and not any(" hb " in l for l in copy)


def test_no_session_means_only_the_app_log(tmp_path):
    log = make(tmp_path)
    log.event("x")
    assert lines(log.path) and "[-]" in lines(log.path)[0]


def test_rotation_keeps_three_files(tmp_path):
    log = make(tmp_path, max_bytes=200)
    for i in range(60):
        log.event(f"line {i} " + "x" * 40)
    names = sorted(p.name for p in (tmp_path / "logs").iterdir())
    assert names == ["obd.log", "obd.log.1", "obd.log.2"]
    assert all(p.stat().st_size < 400 for p in (tmp_path / "logs").iterdir())


def test_a_write_error_is_counted_and_never_raised(tmp_path):
    blocked = tmp_path / "logs"
    blocked.write_text("a file where the directory should be")
    log = TrafficLog(blocked)
    log.event("x"); log.command("010C")
    assert log.errors == 2 and log.last_error


def test_logging_transport_records_commands_replies_and_errors(tmp_path):
    log = make(tmp_path)
    t = LoggingTransport(SimTransport("healthy"), log)
    t.open(); t.send("ATZ"); t.send("ATI")
    t.close()
    got = lines(log.path)
    assert any("sim >> ATZ" in l for l in got) and any("sim << ELM327 v1.5" in l for l in got)
    silent = LoggingTransport(SimTransport("silent_adapter"), log)
    silent.open()
    with pytest.raises(ObdTimeout):
        silent.send("ATZ")
    assert any("!! ObdTimeout" in l for l in lines(log.path))


def test_logging_transport_passes_sim_and_cancel_through(tmp_path):
    inner = SimTransport("healthy")
    t = LoggingTransport(inner, make(tmp_path))
    ev = threading.Event()
    t.cancel = ev
    assert inner.cancel is ev and t.sim is True


def test_a_cancelled_read_leaves_its_commands_in_the_log(tmp_path):
    log, ev = make(tmp_path), threading.Event()
    t = LoggingTransport(SimTransport("healthy"), log)
    inner = t.send

    def send(cmd, timeout=5.0):
        r = inner(cmd, timeout)
        if cmd == "ATI":
            ev.set()
        return r
    t.send = send
    with pytest.raises(ObdCancelled):
        ObdReader(t, cancel=ev, retry_pause=0).read()
    assert any(">> ATI" in l for l in lines(log.path))


def test_tail_lines_of_a_missing_or_empty_file(tmp_path):
    assert tail_lines(tmp_path / "nope.log") == []
    (tmp_path / "e.log").write_text("")
    assert tail_lines(tmp_path / "e.log") == []


def test_tail_lines_returns_the_last_n_in_order(tmp_path):
    p = tmp_path / "t.log"
    p.write_text("".join(f"line {i}\n" for i in range(1000)))
    assert tail_lines(p, 3) == ["line 997", "line 998", "line 999"]
    assert len(tail_lines(p, 5000)) == 1000


def test_tail_lines_of_a_large_file_is_the_end_of_it(tmp_path):
    p = tmp_path / "big.log"
    p.write_text("".join(f"line {i:06d} " + "x" * 80 + "\n" for i in range(50000)))
    got = tail_lines(p, 200)
    assert len(got) == 200 and got[-1].startswith("line 049999")


def test_tail_lines_can_hide_heartbeats(tmp_path):
    log = make(tmp_path)
    for i in range(10):
        log.command("ATRV", tag="hb")
        log.command(f"01{i:02X}")
    shown = tail_lines(log.path, 4, hide_heartbeats=True)
    assert len(shown) == 4 and not any(" hb " in l for l in shown) and ">> 0109" in shown[-1]


def test_logging_transport_logs_stream_start_and_end_but_not_frames(tmp_path):
    class Streamer(SimTransport):
        def stream(self, cmd, on_line, stop, max_seconds, silence_s=1.0):
            from crank.obd.transport import StreamEnd
            on_line(0.0, "0C9 8 00 0C 80 00 00 00 00 00"); on_line(0.1, "1A1 2 01 02")
            return StreamEnd("time")
    log = make(tmp_path)
    t = LoggingTransport(Streamer(), log)
    got = []
    end = t.stream("ATMA", lambda ts, l: got.append(l), threading.Event(), max_seconds=1)
    text = log.path.read_text()
    assert end.reason == "time" and len(got) == 2
    assert ">> ATMA" in text and "watch stream started" in text
    assert "watch stream ended (time, 2 lines)" in text and "0C9" not in text
