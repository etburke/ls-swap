from crank.procedure import STEPS, step_by_id


def test_step_ids_unique_and_fields_unique_per_step():
    assert len({s.id for s in STEPS}) == len(STEPS)
    for s in STEPS:
        assert len({f.key for f in s.fields}) == len(s.fields)
        assert s.kind in ("manual", "capture", "checks")


def test_step_by_id():
    assert step_by_id("static").title.startswith("1.")
