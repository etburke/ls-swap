import pytest

from crank.obd.allowlist import check
from crank.obd.errors import ObdError, ObdForbiddenCommand
from crank.obd.reader import ObdReader
from crank.obd.transport import SimTransport, SCENARIOS


@pytest.mark.parametrize("cmd", ["STRSTNVM", "STSAVCAL", "STSLCS", "STSLEEP", "STBTSET", "STWBR 115200",
                                 "ATPP 0C SV 01", "ATPP", "STPX h:7DF, d:0100", "0902", "04", "STPTO 100", "STIX", "STDIX",
                                 "STSNX", "STSN 1",
                                 "ATZZ", "", "AT", "0", "ATSP6", "ATWS"])
def test_forbidden_commands_are_rejected(cmd):
    with pytest.raises(ObdForbiddenCommand):
        check(cmd)


@pytest.mark.parametrize("cmd,norm", [("atz", "ATZ"), ("AT E0", "ATE0"), (" 010c ", "010C"), ("03", "03"),
                                      ("0a", "0A"), ("07", "07"), ("020200", "020200"), ("ATH1", "ATH1"),
                                      ("ATDPN", "ATDPN"), ("ATSP0", "ATSP0"), ("0100", "0100")])
def test_allowed_commands_are_normalized(cmd, norm):
    assert check(cmd) == norm


@pytest.mark.parametrize("cmd,norm", [("ATI", "ATI"), ("ati", "ATI"), ("STI", "STI"), ("sti", "STI"),
                                      ("ST DI", "STDI"), (" stsn ", "STSN")])
def test_identification_commands_are_allowed(cmd, norm):
    assert check(cmd) == norm


def test_forbidden_is_an_obd_error():
    assert issubclass(ObdForbiddenCommand, ObdError)


@pytest.mark.parametrize("name", [n for n, s in SCENARIOS.items() if not s.fault or s.fault.startswith("drop")])
def test_every_command_the_reader_sends_passes(name):
    sent = []

    class Rec(SimTransport):
        def send(self, cmd, timeout=5.0):
            sent.append(cmd)
            return super().send(cmd, timeout)

    try:
        ObdReader(Rec(name)).read()
    except ObdError:
        pass
    assert sent
    for cmd in sent:
        check(cmd)
