import threading
import time

import pytest

from crank.obd.errors import ObdCancelled, ObdForbiddenCommand, ObdNoAdapter, ObdTimeout
from crank.obd.reader import ObdReader
from crank.obd.serial_transport import SerialTransport
from crank.obd.transport import SimTransport


class FakeSerial:
    """Scripted port: each write queues the chunks for the next reads. A chunk that is an
    Exception is raised from read(); when the queue is empty read() returns b'' (a timeout)."""

    in_waiting = 0

    def __init__(self, replies=None, on_write=None, stream_chunks=None):
        self.written, self.queue, self.closed = [], [], False
        self.stream_chunks = stream_chunks or []          # served after ATMA is written
        self.replies, self.on_write = replies or {}, on_write

    def write(self, data):
        self.written.append(data)
        key = data.decode().strip()
        if key == "ATMA" and self.stream_chunks:
            self.queue.extend(self.stream_chunks)
        if self.on_write:
            self.queue.extend(self.on_write(key))
        else:
            self.queue.extend(self.replies.get(key, [b"OK\r\r>"]))

    def read(self, n=1):
        if not self.queue:
            return b""
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def reset_input_buffer(self):
        pass

    def close(self):
        self.closed = True


def make(fake, **kw):
    return SerialTransport("/dev/cu.fake", serial_factory=lambda port, baud, timeout: fake, **kw)


def test_send_returns_text_before_prompt_and_writes_cr():
    f = FakeSerial({"010C": [b"41 0C 0B B8\r\r>"]})
    t = make(f); t.open()
    assert t.send("010c", 1.0).strip() == "41 0C 0B B8"
    assert f.written[-1] == b"010C\r"


def test_reply_split_across_reads_with_prompt_in_its_own_chunk():
    f = FakeSerial({"0100": [b"41 00 ", b"BE 3F", b"\r\r", b">"]})
    t = make(f); t.open()
    assert t.send("0100", 1.0).split() == ["41", "00", "BE", "3F"]


def test_no_prompt_raises_timeout():
    f = FakeSerial({"0100": [b"SEARCHING..."]})
    t = make(f); t.open()
    with pytest.raises(ObdTimeout):
        t.send("0100", 0.2)


def test_unplug_after_partial_reply_raises_and_returns_nothing():
    f = FakeSerial({"0100": [b"41 00 BE", OSError("device disconnected")]})
    t = make(f); t.open()
    with pytest.raises(ObdNoAdapter):
        t.send("0100", 1.0)


def test_forbidden_command_never_reaches_the_port():
    f = FakeSerial()
    t = make(f); t.open()
    before = len(f.written)
    for cmd in ("STSAVCAL", "atpp 0c sv 01", "STSLEEP", "04"):
        with pytest.raises(ObdForbiddenCommand):
            t.send(cmd)
    assert len(f.written) == before


def test_send_before_open_raises():
    with pytest.raises(ObdNoAdapter):
        make(FakeSerial()).send("ATZ")


def test_open_failure_is_plain_language():
    def boom(port, baud, timeout):
        raise OSError(16, "Resource busy")
    t = SerialTransport("/dev/cu.fake", serial_factory=boom)
    with pytest.raises(ObdNoAdapter) as e:
        t.open()
    assert "/dev/cu.fake" in str(e.value)


def test_close_twice_is_safe_and_closes_the_port():
    f = FakeSerial()
    t = make(f); t.open(); t.close(); t.close()
    assert f.closed


def test_falls_back_to_38400_when_atz_gets_no_prompt():
    opened = []

    def factory(port, baud, timeout):
        opened.append(baud)
        return FakeSerial({"ATZ": [b"ELM327 v1.5\r\r>"]}) if baud == 38400 else FakeSerial({"ATZ": []})

    SerialTransport("/dev/cu.fake", serial_factory=factory, atz_timeout=0.2).open()
    assert opened == [115200, 38400]


def test_both_bauds_silent_raises_timeout():
    t = SerialTransport("/dev/cu.fake", serial_factory=lambda p, b, to: FakeSerial({"ATZ": []}),
                        atz_timeout=0.2)
    with pytest.raises(ObdTimeout):
        t.open()


def test_full_obd_read_through_serial_transport():
    sim = SimTransport("p2119_zero_rpm")
    sim.open()
    f = FakeSerial(on_write=lambda cmd: [(sim.send(cmd) + "\r\r>").encode()])
    out = ObdReader(make(f)).read()
    assert out.codes[0]["code"] == "P2119" and not out.sim
    assert f.closed


from types import SimpleNamespace

from crank.obd.serial_transport import list_ports, resolve_port


def _ports(*devices):
    return lambda: [SimpleNamespace(device=d, description="x") for d in devices]


def test_list_ports_keeps_only_adapter_looking_ports():
    cp = _ports("/dev/cu.OBDLinkMXPlus", "/dev/cu.Bluetooth-Incoming-Port", "/dev/cu.usbserial-110",
                "/dev/tty.OBDLinkMXPlus", "/dev/cu.MyOBDII")
    assert [p for _, p in list_ports(cp)] == ["/dev/cu.OBDLinkMXPlus", "/dev/cu.usbserial-110",
                                              "/dev/cu.MyOBDII"]


def test_resolve_auto_with_one_match():
    assert resolve_port("auto", _ports("/dev/cu.OBDLinkMXPlus")) == "/dev/cu.OBDLinkMXPlus"


def test_resolve_auto_with_none_or_several_lists_candidates():
    with pytest.raises(ObdNoAdapter) as none:
        resolve_port("auto", _ports("/dev/cu.Bluetooth-Incoming-Port"))
    assert "paired" in str(none.value)
    with pytest.raises(ObdNoAdapter) as many:
        resolve_port("auto", _ports("/dev/cu.OBDLinkA", "/dev/cu.OBDLinkB"))
    assert "/dev/cu.OBDLinkA" in str(many.value) and "/dev/cu.OBDLinkB" in str(many.value)


def test_resolve_explicit_path_is_used_as_is():
    assert resolve_port("/dev/cu.whatever", _ports()) == "/dev/cu.whatever"


def test_a_lost_first_atz_is_retried_before_changing_baud():
    opened, seen = [], []

    def on_write(cmd):
        seen.append(cmd)
        return [] if len(seen) == 1 else [b"ELM327 v1.5\r\r>"]

    def factory(port, baud, timeout):
        opened.append(baud)
        return FakeSerial(on_write=on_write)

    SerialTransport("/dev/cu.fake", serial_factory=factory, atz_timeout=0.2).open()
    assert opened == [115200] and seen == ["ATZ", "ATZ"]


def test_both_atz_attempts_silent_at_both_bauds_still_raises():
    seen = []

    def factory(port, baud, timeout):
        return FakeSerial(on_write=lambda cmd: seen.append(cmd) or [])

    with pytest.raises(ObdTimeout):
        SerialTransport("/dev/cu.fake", serial_factory=factory, atz_timeout=0.1).open()
    assert seen == ["ATZ"] * 4


def test_cancel_interrupts_the_wait_for_a_reply():
    f = FakeSerial({"0100": [b"SEARCHING..."]})            # no prompt ever arrives
    t = make(f); t.open()
    t.cancel = threading.Event()
    threading.Timer(0.05, t.cancel.set).start()
    start = time.monotonic()
    with pytest.raises(ObdCancelled):
        t.send("0100", 20.0)
    assert time.monotonic() - start < 1.0


def test_a_set_cancel_stops_before_anything_is_written():
    f = FakeSerial()
    t = make(f); t.open()
    written = len(f.written)
    t.cancel = threading.Event(); t.cancel.set()
    with pytest.raises(ObdCancelled):
        t.send("0100", 1.0)
    assert len(f.written) == written


def test_cancel_during_open_closes_the_port():
    f = FakeSerial()
    t = make(f)
    t.cancel = threading.Event(); t.cancel.set()
    with pytest.raises(ObdCancelled):
        t.open()
    assert f.closed


def test_a_transport_without_a_cancel_event_sends_as_before():
    f = FakeSerial({"010C": [b"41 0C 0B B8\r\r>"]})
    t = make(f); t.open()
    assert t.cancel is None and t.send("010C", 1.0).strip() == "41 0C 0B B8"


def _mon(chunks, ati=None, **kw):
    """A port that streams `chunks` after ATMA; a stop space gets a prompt; ATI answers like an ELM327."""
    replies = {"": [b"\r>"], "ATI": ati or [b"ELM327 v1.4b\r\r>"]}
    f = FakeSerial(on_write=lambda k: replies.get(k, [b"OK\r\r>"]) if k != "ATMA" else [], stream_chunks=chunks, **kw)
    t = make(f)
    t.open()
    return f, t


def test_stream_delivers_lines_split_across_reads_and_skips_the_echo():
    f = FakeSerial(on_write=lambda k: {"ATMA": [b"ATMA\r0C9 8 00 0C", b" 80 00 00 00 00 00\r1A1 2 01 02\r"],
                                       "": [b"\r>"], "ATI": [b"ELM327 v1.4b\r\r>"]}.get(k, [b"OK\r\r>"]))
    t = make(f); t.open(); got = []
    end = t.stream("ATMA", lambda ts, line: got.append(line), threading.Event(), max_seconds=0.3)
    assert got == ["0C9 8 00 0C 80 00 00 00 00 00", "1A1 2 01 02"]
    assert end.reason in ("time", "silent")
    assert b" " in f.written and b"\r" not in f.written      # stop byte is a space; never a bare CR
    assert f.written[-1] == b"ATI\r"                         # resynced after the stop


def test_stop_event_ends_the_stream_with_reason_stopped():
    f, t = _mon([b"ATMA\r0C9 8 00\r1A1 2 01\r3B0 1 02\r"])
    stop, got = threading.Event(), []

    def on_line(ts, line):
        got.append(line)
        if len(got) == 2:
            stop.set()
    end = t.stream("ATMA", on_line, stop, max_seconds=2)
    assert end.reason == "stopped" and got == ["0C9 8 00", "1A1 2 01"]
    assert b" " in f.written


def test_buffer_full_ends_without_a_stop_byte():
    f, t = _mon([b"0C9 8 00\rBUFFER FULL\r\r>"])
    got = []
    end = t.stream("ATMA", lambda ts, line: got.append(line), threading.Event(), max_seconds=1)
    assert end.reason == "buffer_full" and b" " not in f.written and got == ["0C9 8 00"]


def test_adapter_returning_to_the_prompt_ends_with_reason_prompt():
    f, t = _mon([b"0C9 8 00\r\r>"])
    end = t.stream("ATMA", lambda ts, line: None, threading.Event(), max_seconds=1)
    assert end.reason == "prompt" and b" " not in f.written


def test_reset_banner_mid_stream_ends_with_reason_reset():
    f, t = _mon([b"0C9 8 00\r", b"\rELM327 v1.4b\r"])
    end = t.stream("ATMA", lambda ts, line: None, threading.Event(), max_seconds=1)
    assert end.reason == "reset" and b" " not in f.written


def test_lv_reset_line_ends_with_reason_reset():
    f, t = _mon([b"LV RESET\r"])
    end = t.stream("ATMA", lambda ts, line: None, threading.Event(), max_seconds=1)
    assert end.reason == "reset" and b" " not in f.written


def test_silence_ends_the_stream_with_reason_silent():
    f, t = _mon([b"0C9 8 00\r"])
    end = t.stream("ATMA", lambda ts, line: None, threading.Event(), max_seconds=3, silence_s=0.1)
    assert end.reason == "silent" and b" " in f.written


def test_a_stray_space_is_cleared_by_resending_ati():
    f, t = _mon([b"0C9 8 00\r"], ati=[b"?\r\r>", b"ELM327 v1.4b\r\r>"])
    t.stream("ATMA", lambda ts, line: None, threading.Event(), max_seconds=0.1)
    assert [w for w in f.written if w.startswith(b"ATI")][-2:] == [b"ATI\r", b"ATI\r"]


def test_adapter_that_does_not_come_back_raises_timeout():
    f, t = _mon([b"0C9 8 00\r"], ati=[b"?\r\r>", b"?\r\r>"])
    with pytest.raises(ObdTimeout):
        t.stream("ATMA", lambda ts, line: None, threading.Event(), max_seconds=0.1)


def test_stream_refuses_an_unlisted_command():
    f = FakeSerial()
    t = make(f); t.open()
    before = len(f.written)
    with pytest.raises(ObdForbiddenCommand):
        t.stream("STMA", lambda ts, line: None, threading.Event(), max_seconds=1)
    assert len(f.written) == before


def test_link_drop_mid_stream_raises_no_adapter():
    f, t = _mon([b"0C9 8 00\r", OSError("gone")])
    with pytest.raises(ObdNoAdapter):
        t.stream("ATMA", lambda ts, line: None, threading.Event(), max_seconds=1)


def test_cancel_mid_stream_stops_the_monitor_then_raises():
    f, t = _mon([b"0C9 8 00\r"])
    t.cancel = threading.Event()
    got = []

    def on_line(ts, line):
        got.append(line)
        t.cancel.set()
    with pytest.raises(ObdCancelled):
        t.stream("ATMA", on_line, threading.Event(), max_seconds=1)
    assert got and b" " in f.written


def test_held_transport_passes_stream_through():
    from crank.obd.transport import HeldTransport
    f, t = _mon([b"0C9 8 00\r"])
    got = []
    end = HeldTransport(t).stream("ATMA", lambda ts, line: got.append(line), threading.Event(), max_seconds=0.1)
    assert got == ["0C9 8 00"] and end.reason in ("time", "silent")
