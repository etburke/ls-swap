from crank.obd.errors import ObdAdapterError, ObdBusy
from crank.obd.reasons import REASON_TEXT, describe_unavailable


def test_obd_busy_is_a_transient_adapter_error_carrying_the_nrc():
    e = ObdBusy(0x22)
    assert isinstance(e, ObdAdapterError) and e.transient and not e.reset
    assert e.nrc == 0x22 and e.kind == "busy (NRC 22)"


def test_reason_text_covers_every_reason():
    assert set(REASON_TEXT) == {"not_in_map", "refused", "busy", "no_answer", "stopped"}


def test_describe_unavailable_groups_by_reason_worst_first():
    items = [{"name": "D", "reason": "not_in_map"}, {"name": "A", "reason": "refused"},
             {"name": "C", "reason": "no_answer"}, {"name": "B", "reason": "refused"},
             {"name": "E", "reason": "busy"}]
    assert describe_unavailable(items) == ("refused by the PCM: A, B; PCM busy: E; no answer: C; "
                                           "not in the PCM's support map: D")


def test_describe_unavailable_is_empty_for_nothing():
    assert describe_unavailable([]) == ""


def test_describe_unavailable_puts_stopped_after_the_worrying_reasons():
    items = [{"name": "C", "reason": "stopped"}, {"name": "A", "reason": "no_answer"},
             {"name": "D", "reason": "not_in_map"}]
    assert describe_unavailable(items) == ("no answer: A; not read, stopped by you: C; "
                                           "not in the PCM's support map: D")
