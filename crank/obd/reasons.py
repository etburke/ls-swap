"""Why a PID has no value. The reader records one of these strings; reports print the words."""
from __future__ import annotations

REASON_TEXT = {
    "not_in_map": "not in the PCM's support map",
    "refused": "refused by the PCM",
    "busy": "PCM busy",
    "no_answer": "no answer",
}
_ORDER = ("refused", "busy", "no_answer", "not_in_map")      # the worrying ones first


def describe_unavailable(items) -> str:
    """'refused by the PCM: A, B; no answer: C' for [{"name", "reason"}, ...]."""
    groups = []
    for reason in _ORDER:
        names = [i.get("name", "?") for i in items if i.get("reason") == reason]
        if names:
            groups.append(f"{REASON_TEXT[reason]}: {', '.join(names)}")
    return "; ".join(groups)
