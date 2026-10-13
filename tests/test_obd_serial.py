import pytest

from crank.obd.errors import ObdForbiddenCommand, ObdNoAdapter, ObdTimeout
from crank.obd.reader import ObdReader
from crank.obd.serial_transport import SerialTransport
from crank.obd.transport import SimTransport


class FakeSerial:
    """Scripted port: each write queues the chunks for the next reads. A chunk that is an
    Exception is raised from read(); when the queue is empty read() returns b'' (a timeout)."""

    in_waiting = 0

    def __init__(self, replies=None, on_write=None):
        self.written, self.queue, self.closed = [], [], False
        self.replies, self.on_write = replies or {}, on_write

    def write(self, data):
        self.written.append(data)
        key = data.decode().strip()
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
