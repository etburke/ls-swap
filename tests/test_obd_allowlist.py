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


@pytest.mark.parametrize("name", [n for n, s in SCENARIOS.items()
                                  if (not s.fault or s.fault.startswith("drop")) and not s.delay])
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


def test_atrv_is_allowed_but_not_with_arguments():
    assert check("atrv") == "ATRV" and check(" AT RV ") == "ATRV"
    for bad in ("ATRV1", "ATRV 12", "ATRVX"):
        with pytest.raises(ObdForbiddenCommand):
            check(bad)


@pytest.mark.parametrize("cmd", ["ATMA", "at csm1", "ATCAF0", "ATCAF1", "ATD0", "ATD1"])
def test_listen_only_monitor_commands_are_allowed(cmd):
    assert check(cmd) == "".join(cmd.split()).upper()


@pytest.mark.parametrize("cmd", ["ATCSM0", "ATSH7E0", "STMA", "ATMR0C9", "ATMT0C9", "STCSM0", "ATBD", "ATRTR"])
def test_transmit_capable_and_unlisted_monitor_commands_stay_refused(cmd):
    with pytest.raises(ObdForbiddenCommand):
        check(cmd)


@pytest.mark.parametrize("cmd", ["010C", "010C0E", "010C0E11454749", "010C 0E 11"])
def test_mode01_one_to_six_pids_allowed(cmd):
    assert check(cmd).startswith("01")


@pytest.mark.parametrize("cmd", ["010C0E1145474949", "010", "010C0", "0904", "22F40C"])
def test_mode01_seven_pids_or_odd_lengths_refused(cmd):
    with pytest.raises(ObdForbiddenCommand):
        check(cmd)
